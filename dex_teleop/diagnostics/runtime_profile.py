"""Bounded wall/CPU timers for a manually operated split session.

Enabled only through DEX_TELEOP_PROFILE_DIR. No CUDA synchronization, input,
reset, camera, pacing, or collection settings are changed. A shared monotonic
window starts at the first active input. Nested timers are explicitly marked:
inclusive values cannot be added together; exclusive values can be added only
within the same thread and step. Background/render times run concurrently.
"""
import atexit
import functools
import gc
import json
import os
from pathlib import Path
import queue
import threading
import time


class Meter:
    def __init__(self, directory, role, seconds=300):
        self.directory, self.role = Path(directory), role
        self.directory.mkdir(parents=True, exist_ok=True)
        self.seconds = seconds
        self.local = threading.local()
        self.main_thread = threading.get_ident()
        self.window = None
        self.next_window_check = 0.
        self.previous_end = None
        self.activity = dict(arm_active=False, finger_active=False)
        self.events = queue.SimpleQueue()
        self.finished = False
        self.writer_error = None
        self.installed = []
        self.gc_starts = {}
        self.path = self.directory/f'{role}-{os.getpid()}.jsonl'
        self.writer = threading.Thread(target=self._write, name='profile-log', daemon=True)
        self.writer.start()
        atexit.register(self.close)
        gc.callbacks.append(self.gc_event)

    def _write(self):
        try:
            with self.path.open('w', buffering=262144) as stream:
                last_flush = time.monotonic()
                while True:
                    try:
                        event = self.events.get(timeout=.5)
                    except queue.Empty:
                        stream.flush()
                        continue
                    if event is None:
                        break
                    stream.write(json.dumps(event, separators=(',', ':'))+'\n')
                    if time.monotonic()-last_flush >= 1:
                        stream.flush(); last_flush = time.monotonic()
        except Exception as exc:
            self.writer_error = str(exc)

    def close(self):
        if self.finished:
            return
        self.finished = True
        self.events.put(None)
        self.writer.join(timeout=3)
        if self.gc_event in gc.callbacks:
            gc.callbacks.remove(self.gc_event)
        (self.directory/f'{self.role}-{os.getpid()}-done.json').write_text(json.dumps(dict(
            pid=os.getpid(), role=self.role, closed_at=time.monotonic(),
            writer_error=self.writer_error, writer_alive=self.writer.is_alive()))+'\n')

    def enabled(self):
        if self.finished:
            return False
        now = time.monotonic()
        if self.window is None and now >= self.next_window_check:
            self.next_window_check = now+.5
            try:
                self.window = json.loads((self.directory/'window.json').read_text())
            except (OSError, ValueError):
                pass
        if self.window is None:
            return False
        if now >= self.window['end']:
            self.close()
            print(f'[PROFILE] {self.role}: measurement complete; simulation continues. {self.path}', flush=True)
            return False
        return now >= self.window['start']

    def control_updated(self, bridge):
        self.activity = dict(arm_active=any(bridge.arm_active.values()),
                             finger_active=any(bridge.finger_active.values()))
        if self.role == 'main' and self.window is None and any(self.activity.values()):
            now = time.monotonic()
            self.window = dict(start=now, end=now+self.seconds, seconds=self.seconds,
                               started_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'), trigger='first_active_input')
            temporary = self.directory/'window.tmp'
            temporary.write_text(json.dumps(self.window, indent=2)+'\n')
            temporary.replace(self.directory/'window.json')
            print(f'[PROFILE] START: {self.seconds}s of normal manipulation; no settings changed.', flush=True)

    def begin(self, kind, step=None, recording=False):
        self.local.stack = []
        self.local.row = None
        if self.enabled():
            self.local.row = dict(kind=kind, step=step, recording=bool(recording),
                t=time.monotonic(), cpu_start=time.thread_time(), timings={}, **self.activity)

    def end(self, period_end=None):
        row = getattr(self.local, 'row', None)
        self.local.row = None
        now = time.monotonic()
        if row is not None:
            row['wall_ms'] = (now-row['t'])*1000
            row['cpu_ms'] = (time.thread_time()-row.pop('cpu_start'))*1000
            if period_end is not None and self.previous_end is not None:
                row['period_ms'] = (period_end-self.previous_end)*1000
            row.update(self.activity)
            self.events.put(row)
        if period_end is not None:
            self.previous_end = period_end

    def wrap(self, owner, method, name):
        original = getattr(owner, method)
        self.installed.append(name)
        @functools.wraps(original)
        def measured(*args, **kwargs):
            if not self.enabled():
                return original(*args, **kwargs)
            stack = getattr(self.local, 'stack', None)
            if stack is None:
                stack = self.local.stack = []
            start, cpu = time.monotonic(), time.thread_time()
            children = [0., 0.]
            stack.append(children)
            try:
                return original(*args, **kwargs)
            finally:
                wall = (time.monotonic()-start)*1000
                cpu_ms = (time.thread_time()-cpu)*1000
                stack.pop()
                if stack:
                    stack[-1][0] += wall
                    stack[-1][1] += cpu_ms
                sample = [wall, cpu_ms, max(0., wall-children[0]), max(0., cpu_ms-children[1])]
                row = getattr(self.local, 'row', None)
                if row is not None:
                    row['timings'].setdefault(name, []).append(sample)
                elif not self.finished:
                    self.events.put(dict(kind='background', name=name, t=start,
                        thread=threading.current_thread().name, sample=sample))
        setattr(owner, method, measured)

    def gc_event(self, phase, info):
        key = (threading.get_ident(), info['generation'])
        if phase == 'start':
            if self.window is not None and not self.finished:
                self.gc_starts[key] = time.monotonic()
        elif key in self.gc_starts:
            start = self.gc_starts.pop(key)
            self.events.put(dict(kind='gc', t=start, wall_ms=(time.monotonic()-start)*1000,
                                 generation=key[1], thread=threading.current_thread().name))


def install():
    """Called before AppLauncher; Isaac-dependent wrappers install after Kit starts."""
    directory = Path(os.environ['DEX_TELEOP_PROFILE_DIR']).resolve()
    seconds = int(os.environ.get('DEX_TELEOP_PROFILE_SECONDS', '300'))
    if not 5 <= seconds <= 1800:
        raise ValueError('DEX_TELEOP_PROFILE_SECONDS must be 5..1800')
    from isaaclab.app import AppLauncher
    original_init = AppLauncher.__init__

    def initialize(launcher, *args, **kwargs):
        original_init(launcher, *args, **kwargs)
        role = 'renderer' if os.environ.get('BENCH2DEX_SCENE_WORKER') else 'main'
        meter = Meter(directory, role, seconds)
        from isaaclab.sim import SimulationContext
        from isaaclab.sensors.camera import Camera
        from collector.cameras import CameraRig
        from collector.data_collector import DataCollector
        from utils import runtime_helpers
        from robots.gravity_compensation import GravityCompensator
        from multiprocessing.connection import Connection
        from PIL import Image
        from dex_teleop.backends.bench2dex import scene_state, scene_worker, split_rendering
        from dex_teleop.backends.bench2dex.render_process import RenderProcess
        from dex_teleop.backends.bench2dex.collection import CollectionSession
        from dex_teleop.backends.bench2dex.browser import BrowserProbe
        from dex_teleop.backends.bench2dex.rendering import StepPacer

        for owner, method, name in (
            (SimulationContext, 'step', 'physics.step'),
            (SimulationContext, 'render', 'render.kit'),
            (SimulationContext, 'forward', 'render.forward'),
            (CameraRig, 'capture', 'camera.capture'),
            (CameraRig, '_extract_rgb', 'camera.rgb_cpu_copy'),
            (CameraRig, '_extract_camera_frame', 'camera.extract_frame'),
            (CameraRig, 'sync_mounted_camera_poses', 'camera.mount_sync'),
            (Camera, 'update', 'camera.annotator_update'),
            (DataCollector, 'before_step', 'collector.before_step'),
            (DataCollector, 'after_step', 'collector.after_step'),
            (DataCollector, '_capture_observation', 'collector.observation'),
            (runtime_helpers, 'write_articulation_targets', 'targets.write'),
            (runtime_helpers, 'update_sim_objects', 'objects.update'),
            (GravityCompensator, 'apply', 'gravity.feedforward'),
            (StepPacer, 'wait', 'pacing.wait'),
            (scene_state, 'capture_scene', 'state.capture_cpu'),
            (scene_state, 'restore_scene', 'state.restore'),
            (scene_state, 'snapshot_error', 'state.verify'),
            (split_rendering.SplitRenderer, '_request', 'render.prepare_submit'),
            (split_rendering.SplitRenderer, '_publish', 'render.publish'),
            (RenderProcess, 'submit', 'render.enqueue'),
            (RenderProcess, '_receive', 'ipc.wait_receive'),
            (Connection, 'send', 'ipc.send'),
            (Connection, 'recv', 'ipc.recv'),
            (Image.Image, 'save', 'image.encode'),
            (CollectionSession, 'before_step', 'dashboard.before_step'),
            (CollectionSession, 'after_render', 'dashboard.after_render'),
            (CollectionSession, 'after_step', 'dashboard.after_step'),
        ):
            meter.wrap(owner, method, name)
        # Update early imported aliases too; each operation is wrapped once.
        split_rendering.capture_scene = scene_state.capture_scene
        scene_worker.capture_scene = scene_state.capture_scene
        scene_worker.restore_scene = scene_state.restore_scene
        scene_worker.snapshot_error = scene_state.snapshot_error

        if role == 'main':
            from dex_teleop.backends.bench2dex.sim_adapter import NetworkTeleopBridge
            from teleop.teleop_controller import TeleopController
            for owner, method, name in (
                (NetworkTeleopBridge, 'update', 'control.total'),
                (NetworkTeleopBridge, '_update_arm_ik', 'control.arm_ik'),
                (NetworkTeleopBridge, '_publish', 'control.publish_status'),
                (NetworkTeleopBridge, 'after_physics_step', 'haptics.contacts'),
                (TeleopController, 'step', 'control.finger_retarget'),
            ):
                meter.wrap(owner, method, name)
            original_update = NetworkTeleopBridge.update
            def update(bridge, *a, **kw):
                result = original_update(bridge, *a, **kw)
                meter.control_updated(bridge)
                return result
            NetworkTeleopBridge.update = update
            original_before = CollectionSession.before_step
            def begin_step(session, step, bridge):
                meter.begin('step', step, session.collector.episode_active)
                return original_before(session, step, bridge)
            CollectionSession.before_step = begin_step
            original_timing = BrowserProbe.record_step_timing
            def end_step(probe, dt, now):
                meter.end(now)
                return original_timing(probe, dt, now)
            BrowserProbe.record_step_timing = end_step
        else:
            original_snapshot = scene_worker.render_snapshot
            def frame(*a, **kw):
                meter.begin('frame')
                try:
                    return original_snapshot(*a, **kw)
                finally:
                    meter.end()
            scene_worker.render_snapshot = frame
        original_shutdown = runtime_helpers.shutdown_with_diagnostics
        def shutdown(*a, **kw):
            meter.close()
            return original_shutdown(*a, **kw)
        runtime_helpers.shutdown_with_diagnostics = shutdown
        (directory/f'{role}-{os.getpid()}-manifest.json').write_text(json.dumps(dict(
            pid=os.getpid(), role=role, seconds=seconds, gpu=os.environ.get('CUDA_VISIBLE_DEVICES'),
            timers=meter.installed, sample_fields=['inclusive_wall_ms', 'inclusive_thread_cpu_ms',
                'exclusive_wall_ms', 'exclusive_thread_cpu_ms'],
            no_cuda_synchronization_added=True, inputs_or_settings_changed=False), indent=2)+'\n')
        print(f'[PROFILE] {role}: armed; first active input starts {seconds}s measurement.', flush=True)
    AppLauncher.__init__ = initialize
