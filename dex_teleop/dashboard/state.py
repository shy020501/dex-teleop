"""Browser dashboard state. HTTP threads enqueue; only the simulation thread mutates physics/collector."""
import copy
import fcntl
import json
import math
import os
import queue
import re
import stat
import sys
import threading
import time
import uuid
from pathlib import Path
from dex_teleop.core.workspace_limits import validate_workspace, workspace_from_control, workspace_bounds
from dex_teleop.core.backend import DashboardBackend
from dex_teleop.paths import WORKSPACE

HAPTIC_FORCE_LIMIT_N = 20.
HAPTIC_FORCE_STEP_N = .05
EPISODE = re.compile(
    r'^(?:(?P<number>\d{4})_(?P<status>success|fail)_(?P<steps>\d+)_(?P<datetime>\d{8}_\d{6})'
    r'|(?P<legacy_status>success|fail)_\d+\.\d{3}s_[A-Za-z0-9_-]+)\.hdf5$'
)
TRASH_LIMIT = 10
TRASH_EPISODE = re.compile(r'^(?:(?P<deleted_ns>\d{20})_)?[0-9a-f]{32}_(?P<episode>.+)$')


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError('하위 폴더를 입력하세요.')
    if '\\' in value or any(p in ('', '.', '..') for p in value.split('/')) or value.startswith('/'):
        raise ValueError('절대 경로, 빈 구성 요소, . 및 ..는 허용되지 않습니다.')
    if any(ord(c) < 32 or c in ':*?"<>|' for c in value):
        raise ValueError('지원되지 않는 경로 문자입니다.')
    return value


class EpisodeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def resolve(self, value):
        path = self.root / relative_path(value)
        root = self.root.resolve()
        if not path.resolve().is_relative_to(root):
            raise ValueError('데이터 root를 벗어날 수 없습니다.')
        # Reject symlinks even if they currently resolve inside the root.
        cursor = self.root
        for part in Path(value).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError('심볼릭 링크 경로는 허용되지 않습니다.')
        return path

    def writable(self):
        return self.root.is_dir() and os.access(self.root, os.W_OK | os.X_OK)

    def folder(self, value):
        path = self.resolve(value)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def episode(self, value):
        path = self.resolve(value)
        if not EPISODE.fullmatch(path.name) or not path.is_file():
            raise ValueError('저장된 episode 파일이 아닙니다.')
        return path

    def next_number(self, folder):
        """Monotonic per-folder number, retained when episodes move to the trash."""
        path = self.folder(folder)
        with self.lock:
            fd = os.open(path/'.episode_sequence', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'r+', encoding='ascii') as counter:
                fcntl.flock(counter.fileno(), fcntl.LOCK_EX)
                text = counter.read().strip()
                previous = int(text) if text else 0
                for p in path.iterdir():
                    match = EPISODE.fullmatch(p.name)
                    if match and match['number']:
                        previous = max(previous, int(match['number']))
                number = previous + 1
                if not 1 <= number <= 9999:
                    raise ValueError('4자리 episode 번호가 소진되었습니다. 새 하위 폴더를 지정하세요.')
                counter.seek(0)
                counter.write(str(number) + '\n')
                counter.truncate()
                counter.flush()
                os.fsync(counter.fileno())
                return number

    def inventory(self, folder=''):
        """One filesystem snapshot for the tree, including empty visible directories."""
        with self.lock:
            path = self.root if folder == '' else self.resolve(folder)
            files = []
            folders = {}
            if path.is_dir():
                for directory, children, names in os.walk(path, followlinks=False):
                    parent = Path(directory)
                    children[:] = sorted(name for name in children
                                         if not name.startswith('.') and not (parent/name).is_symlink())
                    name = parent.relative_to(self.root).as_posix()
                    name = '' if name == '.' else name
                    folders[name] = dict(path=name, bytes=0, episodes=0)
                    for name in names:
                        match = EPISODE.fullmatch(name)
                        if not match:
                            continue
                        try:
                            p = self.episode((parent/name).relative_to(self.root).as_posix())
                            stat = p.stat()
                            files.append(dict(path=p.relative_to(self.root).as_posix(), name=p.name,
                                              success=(match['status'] or match['legacy_status']) == 'success',
                                              bytes=stat.st_size, modified=stat.st_mtime))
                        except (ValueError, OSError):
                            continue
            files.sort(key=lambda p: p['modified'], reverse=True)
            for item in files:
                parent = Path(item['path']).parent
                for prefix in [parent, *parent.parents]:
                    name = '' if prefix == Path('.') else prefix.as_posix()
                    if name in folders:
                        folders[name]['bytes'] += item['bytes']
                        folders[name]['episodes'] += 1
            return dict(folder=folder, bytes=sum(item['bytes'] for item in files), episodes=len(files),
                        success=sum(item['success'] for item in files),
                        fail=sum(not item['success'] for item in files),
                        folders=sorted(folders.values(), key=lambda row: row['path']), files=files)

    def list(self, folder):
        return self.inventory(folder)['files']

    def summary(self, folder=''):
        result = self.inventory(folder)
        result.pop('files')
        return result

    def metadata(self, value):
        import h5py
        import numpy as np
        from dex_teleop.dashboard.episode_info import describe_episode
        def plain(v):
            if isinstance(v, bytes): return v.decode('utf-8', errors='replace')
            if isinstance(v, np.ndarray): return [plain(x) for x in v.tolist()]
            if isinstance(v, np.generic): return plain(v.item())
            if isinstance(v, float) and not math.isfinite(v): return None
            return v
        with self.lock:
            path = self.episode(value)
            with h5py.File(path, 'r') as f:
                meta = {k: plain(v[()]) for k, v in f.get('meta', {}).items() if isinstance(v, h5py.Dataset)}
                return dict(path=value, metadata=meta, groups=list(f.keys()),
                            replay_available='replay/state' in f,
                            **describe_episode(f, meta, path.stat().st_size))

    @staticmethod
    def _trash_entries(trash):
        """Only this application's regular episode files; never follow links."""
        entries = []
        for path in trash.iterdir():
            match = TRASH_EPISODE.fullmatch(path.name)
            if not match or not EPISODE.fullmatch(match['episode']):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                # Legacy UUID-only names have no deletion timestamp. On Linux,
                # rename updates inode ctime, whereas mtime is the recording time.
                stamp = int(match['deleted_ns']) if match['deleted_ns'] else info.st_ctime_ns
                entries.append((stamp, path.name, path))
        return entries

    def delete(self, value):
        """Keep the latest ten deletions across all task folders in this root."""
        with self.lock:
            path = self.episode(value)
            fd = os.open(self.resolve('.trash.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                # Multiple server instances can share one data root.
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                trash = self.folder('.trash')
                entries = self._trash_entries(trash)
                stamp = max(time.time_ns(), max((entry[0] for entry in entries), default=0) + 1)
                destination = trash / f'{stamp:020d}_{uuid.uuid4().hex}_{path.name}'
                path.rename(destination)
                entries.append((stamp, destination.name, destination))
                entries.sort(key=lambda entry: entry[:2])
                purged, errors = 0, []
                for _, _, old in entries[:-TRASH_LIMIT]:
                    try:
                        old.unlink(missing_ok=True)
                        purged += 1
                    except OSError as exc:
                        errors.append(dict(path=old.relative_to(self.root).as_posix(), error=str(exc)))
                return dict(deleted=value, recoverable=True, trash_path=destination.relative_to(self.root).as_posix(),
                            trash_limit=TRASH_LIMIT, trash_retained=len(entries)-purged,
                            purged_count=purged, cleanup_errors=errors)


class Dashboard:
    def __init__(self, store=None, state_file=None, debug_mode=False, *, backend: DashboardBackend):
        self.backend = backend
        self.debug_mode = bool(debug_mode)
        self.store = store or EpisodeStore(backend.data_root)
        self.assets = backend.assets
        from dex_teleop.dashboard.camera_preview import WristPreviews
        self.previews = WristPreviews()
        self.state_file = Path(state_file or WORKSPACE/'logs/dashboard-settings.json')
        self.lock = threading.RLock()
        self.commands = queue.Queue(maxsize=32)
        from dex_teleop.core.protocol import load_config
        from dex_teleop.core.config import load_client_config
        config = load_config()
        self.default_keys = load_client_config()['keys']
        haptics = config['haptics']
        self.haptic_default_max_power = haptics['max_power']
        self.state = dict(ready=False, recording=False, phase='loading', task='', folder='',
                          sim_seconds=0., wall_seconds=0., frames=0, success=False, error=None, last_saved=None,
                          haptic_strength=1., haptic_max_power=haptics['max_power'], debug_mode=self.debug_mode,
                          haptic_enabled=bool(haptics.get('enabled', True)),
                          haptic_force_min_n=haptics['force_on_n'], haptic_force_max_n=haptics['full_force_n'],
                          haptic_force_limit_n=HAPTIC_FORCE_LIMIT_N, haptic_force_step_n=HAPTIC_FORCE_STEP_N)
        defaults = workspace_from_control(config['control'])
        self.state.update(workspace=copy.deepcopy(defaults), workspace_defaults=defaults,
                          workspace_ready=False, workspace_pending=False, workspace_effective={},
                          workspace_keys={axis:[self.default_keys[axis+'_minus'], self.default_keys[axis+'_plus']]
                                          for axis in ('x','y','z')})
        self.persisted = {}
        try:
            self.persisted = json.loads(self.state_file.read_text())
        except (OSError, ValueError, TypeError): pass
        if not isinstance(self.persisted, dict):
            self.persisted = {}
        try:
            power_setting = (
                {'max_power': self.persisted['haptic_max_power']}
                if 'haptic_max_power' in self.persisted else
                {'strength': self.persisted.get('haptic_strength', 1.)}
            )
            restored = self.haptic_changes({
                **power_setting,
                'enabled': self.persisted.get('haptic_enabled', self.state['haptic_enabled']),
                'force_min_n': self.persisted.get('haptic_force_min_n', self.state['haptic_force_min_n']),
                'force_max_n': self.persisted.get('haptic_force_max_n', self.state['haptic_force_max_n']),
            })
            if ('haptic_max_power' not in self.persisted and 'haptic_enabled' not in self.persisted
                    and restored['haptic_strength'] == 0):
                # The old 0% setting was the only mute switch. Preserve mute while
                # letting the new on/off control restore useful output when enabled.
                restored.update(haptic_enabled=False, haptic_strength=1.,
                                haptic_max_power=self.haptic_default_max_power)
            elif 'haptic_max_power' in self.persisted:
                # Retain legacy metadata, but never multiply the ceiling by it.
                restored['haptic_strength'] = self.strength(self.persisted.get('haptic_strength', 1.))
            self.state.update(restored)
        except (ValueError, TypeError, OverflowError):
            # Malformed persisted output settings must not unexpectedly enable vibration.
            self.state['haptic_enabled'] = False
        if 'workspace' in self.persisted:
            try:
                self.state['workspace'] = validate_workspace(self.persisted['workspace'])
            except ValueError:
                self.state['error'] = '저장된 이동 범위가 유효하지 않아 기본값을 사용합니다.'
        self.restart_task = None
        self.restart_argv = None
        self._pose_receiver = None
        self.replays = None

    def restart_process(self):
        if not self.restart_task or not self.restart_argv:
            raise RuntimeError('Task restart launcher is not configured')
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, *self.restart_argv, '--task', self.restart_task])

    @staticmethod
    def strength(value):
        if isinstance(value, bool): raise ValueError('진동 강도는 0~100%입니다.')
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 1: raise ValueError('진동 강도는 0~100%입니다.')
        return value

    def haptic_changes(self, data):
        """Validate the complete candidate before changing any persisted/live field."""
        changes = {}
        if 'max_power' in data and 'strength' in data:
            raise ValueError('최대 진동과 이전 강도 설정은 동시에 지정할 수 없습니다.')
        if 'max_power' in data:
            from dex_teleop.core.haptics import validate_power
            try:
                changes['haptic_max_power'] = validate_power(data['max_power'])
            except ValueError:
                raise ValueError('최대 진동은 0.00~1.00 사이여야 합니다.') from None
            changes['haptic_strength'] = 1.
        elif 'strength' in data:
            # Compatibility with saved settings and older browser pages.
            changes['haptic_strength'] = self.strength(data['strength'])
            changes['haptic_max_power'] = self.haptic_default_max_power * changes['haptic_strength']
        if 'enabled' in data:
            if type(data['enabled']) is not bool:
                raise ValueError('Haptic 켜기/끄기는 true 또는 false여야 합니다.')
            changes['haptic_enabled'] = data['enabled']
        if 'force_min_n' in data or 'force_max_n' in data:
            low = data.get('force_min_n', self.state['haptic_force_min_n'])
            high = data.get('force_max_n', self.state['haptic_force_max_n'])
            if isinstance(low, bool) or isinstance(high, bool):
                raise ValueError('접촉력은 N 단위 숫자여야 합니다.')
            try:
                low, high = float(low), float(high)
            except (TypeError, ValueError, OverflowError):
                raise ValueError('접촉력은 N 단위 숫자여야 합니다.') from None
            if (not math.isfinite(low) or not math.isfinite(high) or
                    not 0 <= low < high <= HAPTIC_FORCE_LIMIT_N or
                    high-low < HAPTIC_FORCE_STEP_N-1e-9):
                raise ValueError('접촉력은 0~20 N, 최소·최대 간격은 0.05 N 이상이어야 합니다.')
            changes.update(haptic_force_min_n=low, haptic_force_max_n=high)
        if not changes:
            raise ValueError('변경할 Haptic 설정이 없습니다.')
        return changes

    def tasks(self):
        return self.backend.tasks()

    def update(self, **values):
        with self.lock: self.state.update(values)

    def snapshot(self):
        with self.lock: result = copy.deepcopy(self.state)
        keys = self.keyboard_settings()
        result['workspace_keys'] = {axis:[keys[axis+'_minus'], keys[axis+'_plus']] for axis in ('x','y','z')}
        result.update(data_root=str(self.store.root), data_writable=self.store.writable(),
                      assets=self.assets.snapshot(), wrist_cameras=self.previews.snapshot())
        return result

    def keyboard_settings(self):
        with self.lock:
            receiver = self._pose_receiver
        getter = getattr(receiver, 'keyboard_settings', None)
        return (getter() if getter else None) or dict(self.default_keys)

    def eef_snapshot(self):
        """Read existing CPU telemetry; HTTP threads never access Isaac/GPU state."""
        with self.lock:
            receiver, phase = self._pose_receiver, self.state['phase']
            reference = self.state.get('pose_reference', 'palm')
        result = dict(ready=False, mode='debug' if self.debug_mode else 'waiting', age_ms=None,
                      coordinate_frame='simulation_world_z_up_m', reference=reference, hands={})
        if self.debug_mode or receiver is None or phase in ('loading', 'resetting', 'switching'):
            return result
        with receiver.lock:
            status = receiver.status
            # Correlate the browser and control connection without touching simulation state.
            result.update(server_id=status.get('server_id'), epoch=status.get('epoch'))
            stamp = status.get('pose_monotonic_s')
            if not status.get('ready') or stamp is None:
                return result
            age_ms = max(0., (time.monotonic()-stamp)*1000)
            result.update(age_ms=round(age_ms, 1), mode='live' if age_ms <= 1000 else 'stale')
            if age_ms > 1000:
                return result
            for side, hand in status.get('hands', {}).items():
                if side in ('left', 'right'):
                    result['hands'][side] = dict(
                        position_xyz=copy.deepcopy(hand.get('current_position_xyz')),
                        orientation_wxyz=copy.deepcopy(hand.get('current_orientation_wxyz')))
        result['ready'] = bool(result['hands'])
        return result

    def persist(self):
        with self.lock:
            self.persisted.update(haptic_strength=self.state['haptic_strength'],
                                  haptic_max_power=self.state['haptic_max_power'],
                                  haptic_enabled=self.state['haptic_enabled'],
                                  haptic_force_min_n=self.state['haptic_force_min_n'],
                                  haptic_force_max_n=self.state['haptic_force_max_n'],
                                  workspace=copy.deepcopy(self.state['workspace']),
                                  task=self.state['task'], folder=self.state['folder'])
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_file.with_suffix('.tmp')
            temp.write_text(json.dumps(self.persisted))
            temp.replace(self.state_file)

    def submit(self, data):
        action = data.get('action')
        if self.replays is not None and self.replays.busy:
            raise ValueError('Replay 생성 중입니다. 완료하거나 취소한 후 변경하세요.')
        if self.debug_mode and action in ('toggle', 'reset', 'haptics', 'workspace'):
            raise ValueError('DEBUG 모드에서는 로봇 조작·리셋·수집·진동을 사용할 수 없습니다.')
        if action == 'outcome':
            with self.lock:
                if self.state.get('success_mode') != 'manual' or type(data.get('success')) is not bool:
                    raise ValueError('Manual outcome is not supported or the result is invalid')
                request = self.state.get('outcome_request')
                if (self.debug_mode or not self.state['ready'] or self.state['phase'] != 'awaiting_outcome'
                        or not request or data.get('token') != request['token']):
                    raise ValueError('This episode is no longer waiting for an outcome')
                if request.get('submitted'):
                    if request['success'] != data['success']:
                        raise ValueError('An outcome has already been submitted for this episode')
                    return {'accepted': True}
                try:
                    self.commands.put_nowait(dict(action='outcome', token=request['token'], success=data['success']))
                except queue.Full:
                    raise ValueError('명령 처리 중입니다. 잠시 후 다시 시도하세요.') from None
                request.update(submitted=True, success=data['success'])
            return {'accepted': True}
        with self.lock:
            if self.state['phase'] == 'awaiting_outcome':
                raise ValueError('Choose success or failure for the stopped episode first')
        if action == 'workspace':
            settings = validate_workspace(data.get('settings'))
            with self.lock:
                if self.state['recording'] or self.state['phase'] != 'idle':
                    raise ValueError('수집을 종료한 뒤 이동 범위를 변경하세요.')
                if not self.state['ready'] or not self.state['workspace_ready']:
                    raise ValueError('로봇 이동 범위 준비 중입니다.')
                if self.state['workspace_pending'] or not self.commands.empty():
                    raise ValueError('명령 처리 중입니다. 잠시 후 다시 시도하세요.')
                for hand in self.state['workspace_effective'].values():
                    workspace_bounds(hand['home_position_xyz'], settings)
                try:
                    self.commands.put_nowait(dict(action='workspace', settings=settings))
                except queue.Full:
                    raise ValueError('명령 처리 중입니다. 잠시 후 다시 시도하세요.') from None
                self.state.update(workspace_pending=True, error=None)
            return {'accepted': True}
        if action == 'download':
            with self.lock:
                if self.state['recording'] or self.state['phase'] == 'resetting':
                    raise ValueError('수집을 종료한 뒤 다운로드하세요.')
            return {'accepted': True, 'download_id': self.assets.start(data.get('task'))}
        if action == 'download_cancel':
            self.assets.cancel(data.get('task'))
            return {'accepted': True}
        if action == 'haptics':
            with self.lock:
                changes = self.haptic_changes(data)
                previous = {key:self.state[key] for key in changes}
                self.state.update(changes)
                try:
                    self.persist()
                except OSError:
                    self.state.update(previous)
                    self.persisted.update(previous)
                    raise
            return {'accepted': True}
        if action == 'folder':
            with self.lock:
                if self.state['recording'] or self.state['phase'] == 'resetting':
                    raise ValueError('수집 준비·진행 중에는 저장 폴더를 변경할 수 없습니다.')
                self.store.resolve(data.get('folder'))
                if any(p.startswith('.') for p in Path(data['folder']).parts):
                    raise ValueError('숨김 폴더는 저장 위치로 사용할 수 없습니다.')
                self.state['folder'] = data['folder']
            self.persist()
            return {'accepted': True}
        if action == 'task':
            if self.assets.snapshot()['busy']: raise ValueError('다운로드 완료 또는 취소 후 task를 전환하세요.')
            task = data.get('task')
            match = next((t for t in self.tasks() if t['id'] == task), None)
            if not match or not match['available']: raise ValueError('Task가 없거나 필요한 assets가 없습니다.')
            with self.lock:
                if self.state['recording']: raise ValueError('먼저 현재 수집을 종료하세요.')
                if not self.state['ready']: raise ValueError('환경 준비 중입니다.')
        elif action not in ('toggle', 'reset'):
            raise ValueError('지원되지 않는 명령입니다.')
        with self.lock:
            if not self.state['ready']: raise ValueError('환경 준비 중입니다.')
            if self.state['workspace_pending']: raise ValueError('이동 범위 적용 중입니다.')
            if self.state['phase'] == 'saving': raise ValueError('저장 완료 후 다시 시도하세요. 로봇 조작은 계속할 수 있습니다.')
            if self.state['phase'] == 'save_error' and action != 'toggle':
                raise ValueError('F7로 저장을 재시도한 뒤 계속하세요.')
            try: self.commands.put_nowait(dict(data))
            except queue.Full: raise ValueError('명령 처리 중입니다. 잠시 후 다시 시도하세요.')
        return {'accepted': True}


_current = None

def get_dashboard():
    return _current

def install_dashboard(dashboard, task=None):
    global _current
    _current = dashboard
    task = task or _current.persisted.get('task') or dashboard.backend.default_task
    folder = _current.persisted.get('folder') if _current.persisted.get('task') == task else task
    try: _current.store.resolve(folder)
    except (ValueError, TypeError): folder = task
    _current.update(task=task, folder=folder)
    _current.persist()
    return _current

