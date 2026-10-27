"""Linux lifecycle for this launcher's own Isaac rendering/replay processes.

The parent completes episode saving before stopping workers. A kernel parent-
death signal is the last resort when native Kit exits without Python cleanup.
No process-name search, global GPU reset or other users' processes are involved.
"""
import atexit
import ctypes
import os
import signal
import subprocess
import sys
import threading


_workers = set()
_lock = threading.Lock()


def arm_parent_death_guard(expected_pid):
    """Call in the fresh child before loading Kit, never from preexec_fn.

Both checks are needed: the parent may exit before or during prctl(). SIGKILL
also works when the child is stuck in native USD/renderer cleanup.
"""
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Isaac worker parent-death protection requires Linux')
    if expected_pid <= 1 or os.getppid() != expected_pid:
        raise SystemExit('Worker parent already exited; skipping Isaac startup')
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    prctl.restype = ctypes.c_int
    if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != expected_pid:
        raise SystemExit('Worker parent exited while installing death protection')


def register_worker(process):
    with _lock:
        _workers.add(process)


def forget_worker(process):
    with _lock:
        _workers.discard(process)


def stop_worker(process, timeout=5.):
    """Bounded TERM → KILL → wait, only for a Popen-owned private session."""
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
        try:
            if os.getpgid(process.pid) != process.pid:
                raise RuntimeError('Refusing to signal a worker without its own process group')
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # poll()/wait() prevent acting on a PID after this child was reaped.
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=timeout)
    finally:
        if process.poll() is not None:
            forget_worker(process)


def close_owned_workers():
    """Last-chance cleanup if initialization or another cleanup hook failed."""
    with _lock:
        workers = list(_workers)
    for process in workers:
        try:
            stop_worker(process)
        except Exception as exc:
            print(f'[SHUTDOWN] Worker {process.pid} cleanup failed: {exc}', flush=True)


atexit.register(close_owned_workers)


class ShutdownSignals:
    """Replace Kit's immediate SIGINT shutdown with main.py's save/finally path."""
    def __init__(self):
        self.previous = {}
        self.requested = False
        self.cleaning = False

    def install(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.previous[signum] = signal.signal(signum, self._handle)

    def _handle(self, signum, frame):
        if self.requested or self.cleaning:
            os.write(2, b'[SHUTDOWN] Saving/cleanup in progress; please wait.\n')
            return
        self.requested = True
        os.write(2, b'[SHUTDOWN] Stop requested; saving episode and closing workers.\n')
        raise KeyboardInterrupt

    def begin_cleanup(self):
        self.cleaning = True

    def restore(self):
        for signum, handler in self.previous.items():
            if signal.getsignal(signum) == self._handle:
                signal.signal(signum, handler)
        self.previous.clear()
