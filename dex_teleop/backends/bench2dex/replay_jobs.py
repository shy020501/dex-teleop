"""Single replay job, private disposable cache, bounded lifetime and subprocess.

HTTP only queues a job. The simulation thread explicitly suspends live rendering
before authorizing subprocess startup. No GPU work starts from an HTTP thread.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import h5py
import numpy as np

from dex_teleop.paths import WORKSPACE as ROOT
from .paths import SOURCE_ROOT
from .process_lifecycle import register_worker, forget_worker, stop_worker
JOB_ID = re.compile(r'^[0-9a-f]{32}$')
CAMERA_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


def read_plan(path):
    """Fail closed when older episodes cannot support an exact timing audit."""
    with h5py.File(path, 'r') as h:
        if 'replay/state' not in h:
            raise ValueError('이전 형식에는 전체 초기 상태가 없습니다. 업데이트 후 수집한 episode에서 replay를 사용할 수 있습니다.')
        launch = json.loads(h['replay/launch'][()])
        signature = json.loads(h['replay/signature'][()])
        steps = h['time/sim_step'][:].astype(np.int64)
        n = len(steps)
        if not 2 <= n <= 1801 or np.any(np.diff(steps) <= 0) or steps[-1]-steps[0] > 100_000:
            raise ValueError('Replay requires 2..1801 strictly increasing observation steps.')
        control_stride = int(launch.get('control_stride', 3))
        if control_stride < 1 or np.any((steps[:-1]//control_stride+1)*control_stride < steps[1:]):
            raise ValueError('관측 프레임 사이에 저장되지 않은 제어 갱신 시점이 있어 정확한 action replay가 불가능합니다.')
        actions = h['action/commanded'][:]
        if not h['action/action_valid'][:-1].all() or not np.isfinite(actions[:-1]).all():
            raise ValueError('유효하지 않은 action 구간이 있어 비교 replay를 생성할 수 없습니다.')
        if not h['frame_valid'][:].all() or not np.array_equal(h['replay/render_sim_step'][:], steps):
            raise ValueError('저장 이미지와 상태의 step 불일치 또는 수집 오류가 있습니다.')
        names = [x.decode() if isinstance(x, bytes) else str(x) for x in h['action/action_names'][:]]
        if names != signature.get('global_robot', {}).get('joints'):
            raise ValueError('Action joint order differs from saved robot state.')
        dt = float(launch['physics_dt'])
        if not np.isfinite(dt) or not 0 < dt <= 1:
            raise ValueError('Invalid recorded Physics dt')
        cameras = [name for name, group in h['cameras'].items() if 'rgb' in group and len(group['rgb']) == n]
        if not cameras or any(not CAMERA_ID.fullmatch(c) for c in cameras):
            raise ValueError('No supported saved RGB cameras')
        return dict(launch=launch, signature=signature, steps=steps.tolist(), cameras=cameras,
                    frames=n, physics_dt=dt, duration_s=float((steps[-1]-steps[0])*dt))


class ReplayJobs:
    def __init__(self, store, cache, *, ttl=600., clock=time.monotonic, popen=subprocess.Popen,
                 generation_timeout=1800.):
        self.store, self.cache = store, Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.ttl, self.clock, self.popen = ttl, clock, popen
        self.generation_timeout = generation_timeout
        self.lock = threading.RLock()
        self.job = self.process = self.log = None
        self.stopped = threading.Event()
        # A separate dashboard may be using the same cache root. Only sweep old
        # UUID directories and never follow a symlink.
        for path in self.cache.iterdir():
            if JOB_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                if time.time()-path.stat().st_mtime > 3600:
                    owner_file = path/'owner.json'
                    try:
                        owner = json.loads(owner_file.read_text())
                        os.kill(int(owner['pid']), 0)
                    except (OSError, ValueError, KeyError):
                        shutil.rmtree(path)
        self.sweeper = threading.Thread(target=self._sweep, name='replay-cache', daemon=True)
        self.sweeper.start()

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
            if self.job is not None:
                raise ValueError('현재 replay를 닫은 후 새 replay를 생성하세요.')
            with self.store.lock:
                path = self.store.episode(relative_path)
                plan = read_plan(path)
                if shutil.disk_usage(self.cache).free < 2*1024**3:
                    raise ValueError('Replay 임시 저장 공간이 2 GiB 미만입니다.')
                job_id = uuid.uuid4().hex
                directory = self.cache/job_id
                directory.mkdir(mode=0o700)
                # Keep the source in place; UI deletion is prohibited while the
                # worker reads it. Verify inode/size/mtime again at worker startup.
                stat = path.stat()
            config = dict(plan, source=str(path), source_stat=[stat.st_ino, stat.st_size, stat.st_mtime_ns],
                          directory=str(directory), max_cache_bytes=8*1024**3)
            (directory/'config.json').write_text(json.dumps(config))
            (directory/'owner.json').write_text(json.dumps(dict(pid=os.getpid())))
            self.job = dict(id=job_id, source=relative_path, status='queued', progress=0.,
                            message='시뮬레이션 일시 정지 대기', last_seen=self.clock(),
                            frames=plan['frames'], cameras=plan['cameras'], steps=plan['steps'],
                            physics_dt=plan['physics_dt'])
            return self.snapshot(job_id)

    def reject(self, reason):
        with self.lock:
            if self.job is not None:
                self.job.update(status='error', message=reason)

    def start_pending(self):
        with self.lock:
            if not self.needs_service:
                return
            directory = self.cache/self.job['id']
            env = os.environ.copy()
            env.update(BENCH2DEX_DASHBOARD='0', DEBUG_MODE='false', BENCH2DEX_HAPTICS='0',
                       BENCH2DEX_EXECUTION_MODE='serial', DEX_TELEOP_PARENT_PID=str(os.getpid()))
            env['PYTHONPATH'] = str(ROOT)+os.pathsep+str(SOURCE_ROOT)+os.pathsep+env.get('PYTHONPATH', '')
            self.log = (directory/'worker.log').open('wb')
            try:
                self.process = self.popen([sys.executable, str(ROOT/'scripts/bench2dex/scene_worker.py'),
                    '--config', str(directory/'config.json'), '--replay'],
                    env=env, cwd=SOURCE_ROOT, stdout=self.log, stderr=subprocess.STDOUT,
                    start_new_session=True)
                register_worker(self.process)
                self.job.update(status='running', message='Scene 준비 중', started=self.clock())
            except OSError as exc:
                self.log.close(); self.log = None
                self.job.update(status='error', message=str(exc))

    def _poll(self):
        if self.job is None or self.job['status'] != 'running':
            return
        directory = self.cache/self.job['id']
        try:
            progress = json.loads((directory/'progress.json').read_text())
            self.job.update(progress=progress['progress'], message=progress['message'])
        except (OSError, ValueError, KeyError):
            pass
        code = self.process.poll()
        if code is None and self.clock()-self.job['started'] > self.generation_timeout:
            stop_worker(self.process, timeout=3)
            self.log.close(); self.log = None
            self.job.update(status='error', message='Replay 생성 제한 시간(30분)을 초과했습니다.')
            return
        if code is not None:
            forget_worker(self.process)
            self.log.close(); self.log = None
            if code == 0 and (directory/'manifest.json').is_file():
                self.job.update(status='ready', progress=1., message='생성 완료')
            else:
                tail = (directory/'worker.log').read_text(errors='replace')[-1800:]
                self.job.update(status='error', message=f'Replay 생성 실패 ({code})', detail=tail)

    def snapshot(self, job_id):
        with self.lock:
            self._check(job_id)
            self._poll()
            self.job['last_seen'] = self.clock()
            result = {k:v for k,v in self.job.items() if k not in ('last_seen', 'started')}
            if self.job['status'] == 'ready':
                result['manifest'] = json.loads((self.cache/job_id/'manifest.json').read_text())
            return result

    def _check(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or self.job is None or job_id != self.job['id']:
            raise ValueError('Replay가 만료되었거나 삭제되었습니다.')

    def image(self, job_id, side, camera, index):
        with self.lock:
            self._check(job_id)
            if self.job['status'] != 'ready':
                raise ValueError('Replay 생성 완료 후 표시할 수 있습니다.')
            if side not in ('original', 'state', 'action') or camera not in self.job['cameras']:
                raise ValueError('Unknown replay view')
            if not 0 <= index < self.job['frames']:
                raise ValueError('Invalid frame index')
            self.job['last_seen'] = self.clock()
            return (self.cache/job_id/side/camera/f'{index:06d}.jpg').read_bytes()

    def remove(self, job_id):
        with self.lock:
            self._check(job_id)
            stop_worker(self.process, timeout=3)
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
            if self.job and self.clock()-self.job['last_seen'] > self.ttl:
                self.remove(self.job['id'])

    def close(self):
        self.stopped.set()
        with self.lock:
            if self.job:
                self.remove(self.job['id'])
        self.sweeper.join(timeout=1)

