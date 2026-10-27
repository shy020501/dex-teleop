"""Private, bounded IPC to a fresh Kit process; never fork an initialized Kit.

Only this background thread waits for rendering. The simulation thread submits
immutable CPU snapshots without blocking. Recording overflow fails explicitly.
"""
from concurrent.futures import Future
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time

from dex_teleop.paths import WORKSPACE as ROOT
from .paths import SOURCE_ROOT
from .process_lifecycle import register_worker, stop_worker


class RenderBacklog(RuntimeError):
    pass


@dataclass
class Request:
    payload: dict
    future: Future


class RenderProcess:
    def __init__(self, launch, signature, *, capacity=16, timeout=30., startup_timeout=240.,
                 shutdown_timeout=5., root=None):
        self.timeout, self.startup_timeout = timeout, startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.available = threading.Event()
        self.closed = False
        self.error = None
        self.ready = Future()
        self.preview = None
        self.process = None
        self.thread = None
        root = Path(root or ROOT/'logs/render-workers')
        root.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='worker-', dir=root))
        config = dict(launch=launch, signature=signature)
        (self.directory/'config.json').write_text(json.dumps(config))
        parent, child = socket.socketpair()
        self.connection = Connection(parent.detach())
        self.log = (self.directory/'worker.log').open('wb')
        env = os.environ.copy()
        env.update(BENCH2DEX_DASHBOARD='0', BENCH2DEX_EXECUTION_MODE='serial', DEBUG_MODE='false',
                   BENCH2DEX_HAPTICS='0', DEX2BENCH_AUTO_RECORD='0', DEX_TELEOP_PARENT_PID=str(os.getpid()))
        env['PYTHONPATH'] = str(ROOT)+os.pathsep+str(SOURCE_ROOT)+os.pathsep+env.get('PYTHONPATH', '')
        try:
            self.process = subprocess.Popen([sys.executable, str(ROOT/'scripts/bench2dex/scene_worker.py'),
                '--config', str(self.directory/'config.json'), '--fd', str(child.fileno())],
                pass_fds=(child.fileno(),), env=env, cwd=SOURCE_ROOT,
                stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
            register_worker(self.process)
        except BaseException:
            self.connection.close(); self.log.close(); raise
        finally:
            child.close()
        self.thread = threading.Thread(target=self._run, name='render-ipc', daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def submit(self, payload, *, required=False):
        future = Future()
        with self.lock:
            if self.closed or self.error:
                future.set_exception(RuntimeError(self.error or 'Renderer closed'))
            elif required:
                try:
                    self.queue.put_nowait(Request(payload, future))
                except queue.Full:
                    future.set_exception(RenderBacklog('렌더링 저장 대기열이 가득 찼습니다. 이 episode는 유효하지 않습니다.'))
            else:
                if self.preview is not None:
                    self.preview.future.cancel()
                self.preview = Request(payload, future)
            if not future.done():
                self.available.set()
        return future

    def _receive(self, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.closed:
                raise RuntimeError('Renderer closed')
            if self.connection.poll(.1):
                try:
                    return self.connection.recv()
                except (EOFError, OSError) as exc:
                    code = self.process.poll()
                    status = f' (exit code {code})' if code is not None else ''
                    raise RuntimeError(
                        f'Render worker connection closed{status}; see {self.directory}/worker.log'
                    ) from exc
            if self.process.poll() is not None:
                raise RuntimeError(f'Render worker exited ({self.process.returncode}); {self.directory}/worker.log')
        raise TimeoutError(f'Render worker timeout; {self.directory}/worker.log')

    def _run(self):
        current = None
        try:
            ready = self._receive(self.startup_timeout)
            if ready.get('error'):
                raise RuntimeError(ready['error'])
            if ready.get('type') != 'ready':
                raise RuntimeError('Invalid render worker handshake')
            self.ready.set_result(ready)
            while not self.closed:
                if not self.available.wait(.5):
                    continue
                with self.lock:
                    if not self.queue.empty():
                        current = self.queue.get_nowait()
                    else:
                        current, self.preview = self.preview, None
                    if self.queue.empty() and self.preview is None:
                        self.available.clear()
                if current is None or self.closed:
                    continue
                self.connection.send(current.payload)
                result = self._receive(self.timeout)
                if result.get('error'):
                    raise RuntimeError(result['error'])
                if result.get('token') != current.payload.get('token'):
                    raise RuntimeError('Render response does not match requested snapshot')
                current.future.set_result(result)
                current = None
        except BaseException as exc:
            self.error = str(exc) or type(exc).__name__
            if not self.ready.done():
                self.ready.set_exception(exc)
            if current is not None and not current.future.done():
                current.future.set_exception(exc)
        finally:
            if current is not None and not current.future.done():
                current.future.set_exception(RuntimeError(self.error or 'Renderer closed'))
            with self.lock:
                pending, self.preview = self.preview, None
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(RuntimeError(self.error or 'Renderer closed'))
                while not self.queue.empty():
                    request = self.queue.get_nowait()
                    request.future.set_exception(RuntimeError(self.error or 'Renderer closed'))
            if self.error and not self.closed:
                stop_worker(self.process, self.shutdown_timeout)

    def close(self):
        with self.lock:
            self.closed = True
            self.available.set()
        try:
            stop_worker(self.process, self.shutdown_timeout)
        finally:
            if self.thread is not None and self.thread.ident is not None:
                self.thread.join(timeout=2)
            self.connection.close()
            self.log.close()

