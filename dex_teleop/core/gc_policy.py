"""Process-local cyclic GC at explicit, non-interactive simulation boundaries.

There is deliberately no per-step/recording/keyboard-idle hook. The browser's
Pause button pauses video only. The owner thread alone may collect, after the
simulation has stopped accepting control at a reset/replay/shutdown boundary.
The memory sampler never collects and never changes robot or viewer state.
"""
import gc
import json
import os
from pathlib import Path
import threading
import time
import uuid


SAFE_POINTS = frozenset(('startup', 'environment_reset', 'replay_pause',
                         'replay_resume', 'task_switch', 'shutdown'))


def resident_mib():
    """Current process RSS, not CUDA memory or the historical RSS high-water mark."""
    try:
        pages = int(Path('/proc/self/statm').read_text().split()[1])
        return pages*os.sysconf('SC_PAGE_SIZE')/(1024*1024)
    except (OSError, ValueError, IndexError):
        return None


class TeleopGCPolicy:
    def __init__(self, directory, *, mode='scheduled', sample_seconds=5.,
                 gc_api=gc, read_memory=resident_mib):
        if mode not in ('scheduled', 'automatic'):
            raise ValueError('GC policy must be scheduled or automatic')
        if sample_seconds <= 0:
            raise ValueError('GC memory sampling interval must be positive')
        self.mode, self.gc = mode, gc_api
        self.read_memory, self.sample_seconds = read_memory, sample_seconds
        self.owner = threading.get_ident()
        self.session = uuid.uuid4().hex[:12]
        self.path = Path(directory)/f'main-{os.getpid()}-{self.session}.jsonl'
        self.active = False
        self.original_enabled = None
        self.stream = None
        self.log_error = None
        self.log_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.monitor = None
        self.started = None
        self.last_collection = None
        self.initial_rss = None
        self.manual_collections = 0

    def _assert_owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError('GC safe points must run on the simulation owner thread')

    def _emit(self, event, **values):
        row = dict(event=event, monotonic_s=time.monotonic(), unix_ns=time.time_ns(),
                   pid=os.getpid(), session=self.session, mode=self.mode, **values)
        try:
            with self.log_lock:
                self.stream.write(json.dumps(row, separators=(',', ':'))+'\n')
                self.stream.flush()
        except (OSError, ValueError) as exc:
            # Diagnostic I/O must not break teleoperation or prevent GC restoration.
            if self.log_error is None:
                print(f'[GC] Diagnostic log error: {exc}', flush=True)
            self.log_error = str(exc)

    def start(self):
        self._assert_owner()
        if self.active:
            return
        if self.original_enabled is not None:
            raise RuntimeError('Create a new GC policy for a new session')
        # Open before taking ownership, so a path error cannot leave GC disabled.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('x', encoding='utf-8')
        self.original_enabled = self.gc.isenabled()
        self.active = True
        self.started = time.monotonic()
        try:
            if self.mode == 'scheduled':
                self.gc.disable()
            self._emit('start', original_auto_enabled=self.original_enabled,
                       auto_enabled=self.gc.isenabled(), rss_mib=self.read_memory())
            self.collect('startup')
            self.initial_rss = self.read_memory()
            self.sample_memory()
            self.monitor = threading.Thread(target=self._sample_loop, name='gc-memory', daemon=True)
            self.monitor.start()
        except BaseException:
            self.close(collect=False)
            raise
        print(f'[GC] policy={self.mode}; memory/collection log: {self.path}', flush=True)

    def collect(self, reason):
        """Caller guarantees no live physics/control work during this boundary."""
        self._assert_owner()
        if reason not in SAFE_POINTS:
            raise ValueError(f'Not an explicit GC safe point: {reason}')
        if not self.active or self.mode != 'scheduled':
            return None
        before = self.read_memory()
        started, cpu_started = time.monotonic(), time.thread_time()
        # Never hold log/UI/network locks across collection or run it from a
        # GC callback: finalizers can execute arbitrary Python code.
        collected = self.gc.collect(2)
        elapsed_ms = (time.monotonic()-started)*1000
        cpu_ms = (time.thread_time()-cpu_started)*1000
        self.last_collection = time.monotonic()
        self.manual_collections += 1
        after = self.read_memory()
        self._emit('collect', reason=reason, generation=2, duration_ms=elapsed_ms,
                   cpu_ms=cpu_ms, collected=collected, rss_before_mib=before,
                   rss_after_mib=after, auto_enabled=self.gc.isenabled())
        print(f'[GC] {reason}: {elapsed_ms:.1f} ms, collected={collected}', flush=True)
        return collected

    def sample_memory(self):
        """Telemetry only, including during idle/recording/input loss. No GC."""
        if not self.active:
            return
        now = time.monotonic()
        rss = self.read_memory()
        self._emit('memory', rss_mib=rss,
                   rss_delta_mib=(rss-self.initial_rss if rss is not None and self.initial_rss is not None else None),
                   elapsed_s=now-self.started,
                   since_collection_s=(now-self.last_collection if self.last_collection is not None else None),
                   auto_enabled=self.gc.isenabled(), manual_collections=self.manual_collections,
                   generation_counts=list(self.gc.get_count()), generation_stats=self.gc.get_stats())

    def _sample_loop(self):
        while not self.stop_event.wait(self.sample_seconds):
            self.sample_memory()

    def close(self, reason='shutdown', *, collect=True):
        self._assert_owner()
        if not self.active:
            return
        self.stop_event.set()
        try:
            # Thread.start() can fail during startup; joining an unstarted
            # thread must not prevent restoration of the original GC setting.
            if self.monitor is not None and self.monitor.ident is not None:
                self.monitor.join(timeout=self.sample_seconds+1)
            if collect:
                self.collect(reason)
        finally:
            # Preserve an embedding application's pre-existing disabled state.
            if self.original_enabled:
                self.gc.enable()
            else:
                self.gc.disable()
            self.active = False
            self._emit('close', reason=reason, auto_enabled=self.gc.isenabled(),
                       rss_mib=self.read_memory(), manual_collections=self.manual_collections)
            with self.log_lock:
                self.stream.close()


def create_teleop_gc_policy(*, network_teleop, debug=False, worker=False, output_dir=None):
    """Only the network-teleop simulation owner opts in; fresh renderers do not."""
    if not network_teleop or debug or worker:
        return None
    from dex_teleop.paths import WORKSPACE
    profile = os.environ.get('DEX_TELEOP_PROFILE_DIR')
    directory = (Path(profile) if profile else Path(output_dir or WORKSPACE/'logs/browser-live'))/'gc-policy'
    return TeleopGCPolicy(directory, mode=os.environ.get('DEX_TELEOP_GC_POLICY', 'scheduled'))
