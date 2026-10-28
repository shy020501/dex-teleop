"""DexMimicGen comparison replay jobs exposed through the common browser API."""
import atexit
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

import h5py
import numpy as np

from dex_teleop.paths import WORKSPACE
from .recording import SCHEMA

JOB_ID = re.compile(r'^[0-9a-f]{32}$')
CAMERA_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


def text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


def read_plan(path):
    """Reject incomplete control histories instead of inventing held actions."""
    with h5py.File(path, 'r') as h:
        if 'replay/schema' not in h or text(h['replay/schema'][()]) != SCHEMA:
            raise ValueError('This episode does not contain a DexMimicGen replay state')
        launch = json.loads(h['replay/launch'][()])
        signature = json.loads(h['replay/signature'][()])
        steps = h['time/sim_step'][:].astype(np.int64)
        n = len(steps)
        if not 2 <= n <= 10001 or np.any(np.diff(steps) <= 0) or steps[-1]-steps[0] > 1_000_000:
            raise ValueError('Replay requires 2..10001 increasing observations spanning at most 1,000,000 Physics steps')
        controls = h['replay/control']
        events = controls['sim_step'][:].astype(np.int64)
        if not len(events) or events[0] != steps[0] or events[-1] >= steps[-1] or np.any(np.diff(events) <= 0):
            raise ValueError('Invalid applied-control event timeline')
        for field in ('ctrl', 'mocap_pos', 'mocap_quat'):
            if len(controls[field]) != len(events) or not np.isfinite(controls[field][:]).all():
                raise ValueError('Invalid or incomplete replay control data')
        snapshots = h['replay/state']
        if len(snapshots['integration']) != n or not np.isfinite(snapshots['integration'][:]).all():
            raise ValueError('Invalid MuJoCo integration states')
        if not h['frame_valid'][:].all() or not np.array_equal(h['replay/render_sim_step'][:], steps):
            raise ValueError('Recorded RGB and state steps do not match')
        valid = h['action/action_valid'][:]
        if valid.shape != (n,) or not valid[:-1].all() or valid[-1] or not bool(h['meta/control_history_complete'][()]):
            raise ValueError('Incomplete applied actions or an invalid terminal action')
        names = [text(name) for name in h['action/action_names'][:]]
        if names != signature['actuator_names'] or controls['ctrl'].shape != (len(events), signature['nu']):
            raise ValueError('Actuator names/dimensions differ from the saved model')
        if controls['mocap_pos'].shape != (len(events), signature['nmocap'], 3) or controls['mocap_quat'].shape != (len(events), signature['nmocap'], 4):
            raise ValueError('Mocap control dimensions differ from the saved model')
        dt = float(launch['physics_dt'])
        if not np.isfinite(dt) or not 0 < dt <= .1:
            raise ValueError('Invalid Physics timestep')
        cameras = [name for name, group in h['cameras'].items() if 'rgb' in group and len(group['rgb']) == n]
        if not cameras or any(not CAMERA_ID.fullmatch(name) for name in cameras):
            raise ValueError('Replay requires saved RGB cameras')
        return dict(launch=launch, signature=signature, steps=steps.tolist(), frames=n, cameras=cameras,
                    physics_dt=dt, duration_s=(steps[-1]-steps[0])*dt,
                    state_spec=int(snapshots.attrs['state_spec']), mujoco_version=text(h['meta/mujoco_version'][()]))


def stop_worker(process, timeout=3.):
    """Only signal the private process group created by this ReplayJobs instance."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.getpgid(process.pid) != process.pid:
            raise RuntimeError('Replay worker has no private process group')
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=timeout)


class ReplayJobs:
    def __init__(self, store, cache, *, ttl=600., generation_timeout=1800.,
                 clock=time.monotonic, popen=subprocess.Popen):
        self.store, self.cache = store, Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.ttl, self.generation_timeout = ttl, generation_timeout
        self.clock, self.popen = clock, popen
        self.lock = threading.RLock()
        self.job = self.process = self.log = None
        self.closed = False
        self.stopped = threading.Event()
        self.sweeper = threading.Thread(target=self._sweep, name='dexmimicgen-replay-cache', daemon=True)
        self.sweeper.start()
        atexit.register(self.close)

    @property
    def needs_service(self):
        with self.lock:
            return self.job is not None and self.job['status'] == 'queued'

    @property
    def busy(self):
        with self.lock:
            self._poll()
            return self.job is not None and self.job['status'] in ('queued', 'running')

    def request(self, relative_path):
        with self.lock:
            if self.closed or self.job is not None:
                raise ValueError('Close the previous replay before requesting another')
            with self.store.lock:
                source = self.store.episode(relative_path)
                plan = read_plan(source)
                if shutil.disk_usage(self.cache).free < 2*1024**3:
                    raise ValueError('Replay requires at least 2 GiB of free cache space')
                job_id = uuid.uuid4().hex
                directory = self.cache/job_id
                directory.mkdir(mode=0o700)
                stat = source.stat()
            config = dict(plan, source=str(source), source_stat=[stat.st_ino, stat.st_size, stat.st_mtime_ns],
                          directory=str(directory), max_cache_bytes=8*1024**3)
            (directory/'config.json').write_text(json.dumps(config))
            (directory/'owner.json').write_text(json.dumps(dict(pid=os.getpid())))
            self.job = dict(id=job_id, source=relative_path, status='queued', progress=0.,
                            message='Waiting for live simulation to pause', last_seen=self.clock(),
                            frames=plan['frames'], cameras=plan['cameras'], steps=plan['steps'], physics_dt=plan['physics_dt'])
            return self.snapshot(job_id)

    def reject(self, reason):
        with self.lock:
            if self.job is not None:
                self.job.update(status='error', message=str(reason))

    def start_pending(self):
        with self.lock:
            if not self.needs_service:
                return
            directory = self.cache/self.job['id']
            env = dict(os.environ, DEX_TELEOP_PARENT_PID=str(os.getpid()), PYTHONUNBUFFERED='1')
            env['PYTHONPATH'] = str(WORKSPACE)+os.pathsep+env.get('PYTHONPATH', '')
            self.log = (directory/'worker.log').open('wb')
            try:
                self.process = self.popen([sys.executable, '-m', 'dex_teleop.backends.dexmimicgen.replay_worker',
                                           str(directory/'config.json')], cwd=WORKSPACE, env=env,
                                          stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
                self.job.update(status='running', message='Preparing DexMimicGen replay', started=self.clock())
            except OSError as exc:
                self.log.close(); self.log = None
                self.job.update(status='error', message=str(exc))

    def _poll(self):
        if self.job is None or self.job['status'] != 'running':
            return
        directory = self.cache/self.job['id']
        try:
            progress = json.loads((directory/'progress.json').read_text())
            self.job.update(progress=float(progress['progress']), message=str(progress['message']))
        except (OSError, ValueError, KeyError):
            pass
        code = self.process.poll()
        if code is None and self.clock()-self.job['started'] > self.generation_timeout:
            stop_worker(self.process)
            self.log.close(); self.log = None
            self.job.update(status='error', message='Replay generation exceeded its time limit')
        elif code is not None:
            self.log.close(); self.log = None
            if code == 0 and (directory/'manifest.json').is_file():
                self.job.update(status='ready', progress=1., message='Replay ready')
            else:
                self.job.update(status='error', message=f'Replay generation failed ({code})',
                                detail=(directory/'worker.log').read_text(errors='replace')[-1800:])

    def _check(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or self.job is None or self.job['id'] != job_id:
            raise ValueError('Replay has expired or was deleted')

    def snapshot(self, job_id):
        with self.lock:
            self._check(job_id)
            self._poll()
            self.job['last_seen'] = self.clock()
            result = {key: value for key, value in self.job.items() if key not in ('last_seen', 'started')}
            if self.job['status'] == 'ready':
                result['manifest'] = json.loads((self.cache/job_id/'manifest.json').read_text())
            return result

    def image(self, job_id, side, camera, index):
        with self.lock:
            self._check(job_id)
            if self.job['status'] != 'ready' or side not in ('original', 'state', 'action'):
                raise ValueError('Replay is not ready or its view is invalid')
            if camera not in self.job['cameras'] or not 0 <= index < self.job['frames']:
                raise ValueError('Invalid replay camera/frame')
            self.job['last_seen'] = self.clock()
            return (self.cache/job_id/side/camera/f'{index:06d}.jpg').read_bytes()

    def remove(self, job_id):
        with self.lock:
            self._check(job_id)
            stop_worker(self.process)
            if self.log is not None:
                self.log.close()
            self.process = self.log = None
            shutil.rmtree(self.cache/job_id)
            self.job = None
            return dict(deleted=True)

    def _sweep(self):
        while not self.stopped.wait(10):
            self.expire()

    def expire(self):
        with self.lock:
            if self.job is not None and self.clock()-self.job['last_seen'] > self.ttl:
                self.remove(self.job['id'])

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stopped.set()
        with self.lock:
            if self.job is not None:
                self.remove(self.job['id'])
        if threading.current_thread() is not self.sweeper:
            self.sweeper.join(timeout=1)
        atexit.unregister(self.close)
