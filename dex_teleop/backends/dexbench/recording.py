"""Copied Newton observations, camera frames, and applied joint commands.

The simulation thread captures at control boundaries. The writer only sees
owned CPU arrays. Native scene state is useful for inspection, but does not
include all Newton solver and task state needed for exact comparison replay.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import copy
import json
import math
import os
from pathlib import Path
import time
import uuid
from zoneinfo import ZoneInfo

import h5py
import numpy as np


SCHEMA = 'dexbench_newton_v1'
REPLAY_UNAVAILABLE_REASON = 'Replay 불가: DexBench Newton 상태·action 비교 재생은 아직 지원하지 않습니다.'


def _array(value):
    value = getattr(value, 'torch', value)
    if hasattr(value, 'detach'):
        value = value.detach().cpu().numpy()
    result = np.array(value, copy=True)
    if result.dtype.kind not in 'biuf':
        raise ValueError('Native state must contain numeric arrays')
    if not np.isfinite(result).all():
        raise ValueError('Native state contains non-finite values')
    return result


def _copy_tree(value, *, unbatch=False):
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            if not isinstance(name, str) or not name or '/' in name or '\0' in name:
                raise ValueError('Invalid native state field name')
            result[name] = _copy_tree(item, unbatch=unbatch)
        return result
    result = _array(value)
    if unbatch:
        if result.ndim == 0 or result.shape[0] != 1:
            raise ValueError('DexBench recording requires one environment per state array')
        result = result[0].copy()
    return result


def capture_snapshot(env, sim_time_s=None, robot_name='robot'):
    """Read public Isaac Lab state once, retaining no simulator-owned tensors."""
    if env.num_envs != 1:
        raise ValueError('DexBench teleoperation records exactly one environment')
    scene = _copy_tree(env.scene.get_state(is_relative=True), unbatch=True)
    robot = scene['articulation'][robot_name]
    if sim_time_s is None:
        sim_time_s = getattr(env.sim, 'current_time', None)
        if sim_time_s is None:
            sim_time_s = float(env.common_step_counter)*float(env.step_dt)
    sim_time_s = float(sim_time_s)
    if not math.isfinite(sim_time_s):
        raise ValueError('Invalid simulation timestamp')
    return dict(robot=dict(qpos=robot['joint_position'].copy(), qvel=robot['joint_velocity'].copy(),
                           root_pose=robot['root_pose'].copy(), root_velocity=robot['root_velocity'].copy()),
                scene=scene, sim_time_s=sim_time_s)


def _size(tree):
    return sum(_size(value) for value in tree.values()) if isinstance(tree, dict) else np.asarray(tree).nbytes


def _schema(tree):
    if isinstance(tree, dict):
        return {key: _schema(value) for key, value in tree.items()}
    value = np.asarray(tree)
    return (value.shape, str(value.dtype))


def _dataset(group, name, value):
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return group.create_dataset(name, data=value, dtype=h5py.string_dtype())
    return group.create_dataset(name, data=value)


def _stack_tree(group, trees):
    for key, first in trees[0].items():
        if isinstance(first, dict):
            _stack_tree(group.create_group(key), [tree[key] for tree in trees])
        else:
            # Stream one captured frame at a time instead of allocating a second
            # episode-sized stack for deformable state or camera images.
            shape = (len(trees), *np.asarray(first).shape)
            compressed = bool(np.asarray(first).size)
            dataset = group.create_dataset(key, shape=shape, dtype=np.asarray(first).dtype,
                **(dict(chunks=(1, *shape[1:]), compression='lzf') if compressed else {}))
            for index, tree in enumerate(trees):
                dataset[index] = tree[key]


@dataclass
class Episode:
    folder: Path
    filename: str
    metadata: dict
    launch: dict
    action_names: tuple
    joint_names: tuple
    frames: list
    controls: list

    def write(self, folder=None):
        folder = Path(folder or self.folder)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder/f'.saving-{uuid.uuid4().hex}.hdf5'
        final = folder/self.filename
        try:
            with h5py.File(temporary, 'w') as file:
                self._write(file)
                file.flush()
            os.link(temporary, final)  # Atomic publication without replacing another episode.
            return str(final)
        finally:
            temporary.unlink(missing_ok=True)

    def _write(self, file):
        frames, controls = self.frames, self.controls
        count = len(frames)
        steps = np.array([frame['step'] for frame in frames], dtype=np.int64)
        file.attrs['schema'] = SCHEMA
        meta = file.create_group('meta')
        for name, value in self.metadata.items():
            if value is not None:
                _dataset(meta, name, value)
        _dataset(meta, 'launch_config', self.launch)
        times = file.create_group('time')
        times['frame_index'] = np.arange(count, dtype=np.int64)
        times['sim_step'] = steps
        times['sim_time_s'] = np.array([frame['snapshot']['sim_time_s'] for frame in frames], dtype=np.float64)
        times['timestamp_ns'] = np.array([frame['timestamp_ns'] for frame in frames], dtype=np.int64)
        robot = file.create_group('robot')
        _stack_tree(robot, [frame['snapshot']['robot'] for frame in frames])
        robot.create_dataset('joint_names', data=self.joint_names, dtype=h5py.string_dtype())
        robot.attrs['root_pose_order'] = 'x,y,z,qx,qy,qz,qw'
        robot.attrs['coordinate_frame'] = 'environment_origin_relative_z_up_m'
        state = file.create_group('state')
        _stack_tree(state.create_group('scene'), [frame['snapshot']['scene'] for frame in frames])
        state.attrs['complete_solver_state'] = False
        state.attrs['coordinate_frame'] = 'environment_origin_relative_z_up_m'
        state.attrs['quaternion_order'] = 'xyzw'
        state.attrs['limitations'] = 'No Newton solver caches or task-specific liquid/custom runtime state'

        timeline = file.create_group('control/timeline')
        timeline_steps = np.array([row['step'] for row in controls], dtype=np.int64)
        timeline['sim_step'] = timeline_steps
        timeline['commanded'] = (np.stack([row['action'] for row in controls]) if controls
                                  else np.empty((0, len(self.action_names)), dtype=np.float32))
        timeline.create_dataset('source', data=[row['source'] for row in controls], dtype=h5py.string_dtype())
        action = file.create_group('action')
        commanded = np.full((count, len(self.action_names)), np.nan, dtype=np.float32)
        valid = np.zeros(count, dtype=bool)
        sources = ['none']*count
        for index, step in enumerate(steps[:-1]):
            selected = np.searchsorted(timeline_steps, step, side='right')-1
            if selected < 0:
                raise ValueError('Missing action for observation interval')
            commanded[index] = controls[selected]['action']
            valid[index] = True
            sources[index] = controls[selected]['source']
        action['commanded'] = commanded
        action['action_valid'] = valid
        action.create_dataset('action_names', data=self.action_names, dtype=h5py.string_dtype())
        action.create_dataset('source', data=sources, dtype=h5py.string_dtype())
        _dataset(action, 'control_mode', 'native_joint_position')
        _dataset(action, 'action_type', 'absolute_joint_position_target')
        action.attrs['units'] = 'rad'
        action.attrs['commanded_semantics'] = 'target applied from this observation; all updates in control/timeline'
        action.attrs['terminal_semantics'] = 'last observation has no following action'

        cameras = file.create_group('cameras')
        for name, initial in frames[0]['cameras'].items():
            group = cameras.create_group(name)
            rgb = group.create_dataset('rgb', shape=(count, *initial.shape), dtype=np.uint8,
                                       chunks=(1, *initial.shape), compression='lzf')
            for index, frame in enumerate(frames):
                rgb[index] = frame['cameras'][name]
            _dataset(group, 'camera_model', 'isaac_rtx')
            _dataset(group, 'raw_image_semantics', 'RGB uint8, rectangular, no browser mask')
        file['frame_valid'] = np.ones(count, dtype=bool)
        file.create_dataset('frame_errors', data=['']*count, dtype=h5py.string_dtype())
        episode = file.create_group('episode')
        episode['is_first'] = np.arange(count) == 0
        episode['is_last'] = np.arange(count) == count-1
        episode['done'] = np.arange(count) == count-1
        episode['success'] = (np.arange(count) == count-1) & bool(self.metadata['success'])


class RecordingSession:
    def __init__(self, ui, task_id, launch_config, *, action_names, joint_names,
                 physics_dt=1/60, sample_hz=30., control_hz=30., max_frames=1800, max_buffer_mb=6144):
        self.ui, self.task_id = ui, task_id
        self.launch = copy.deepcopy(launch_config)
        self.physics_dt, self.sample_hz, self.control_hz = map(float, (physics_dt, sample_hz, control_hz))
        self.action_names, self.joint_names = tuple(action_names), tuple(joint_names)
        if (not self.action_names or not self.joint_names or
                any(not isinstance(name, str) or not name for name in (*self.action_names, *self.joint_names)) or
                len(set(self.action_names)) != len(self.action_names) or len(set(self.joint_names)) != len(self.joint_names)):
            raise ValueError('Unique action and joint names are required')
        for value in (self.physics_dt, self.sample_hz, self.control_hz):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid recording/control rate')
        stride = 1/(self.physics_dt*self.control_hz)
        if stride < 1 or not math.isclose(stride, round(stride), abs_tol=1e-8) or self.sample_hz > self.control_hz:
            raise ValueError('Control rate must divide Physics rate; sample rate must not exceed control rate')
        self.control_stride = int(round(stride))
        self.max_frames, self.max_bytes = int(max_frames), int(max_buffer_mb*1024**2)
        if not 2 <= self.max_frames <= 10000 or self.max_bytes <= 0:
            raise ValueError('Invalid episode buffer limits')
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dexbench-hdf5')
        self.active = False
        self._frames, self._controls = [], []
        self._last_frames = self.bytes = 0
        self.job = self.future = self.error = self.last_saved = None

    @property
    def frames(self):
        return len(self._frames) if self.active else self._last_frames

    @property
    def saving(self):
        return self.job is not None

    @property
    def limit_reached(self):
        return self.active and (self.frames >= self.max_frames or
            bool(self._frames and self.bytes+self._frames[-1]['bytes'] > self.max_bytes))

    def start(self, step, snapshot, cameras):
        if self.active or self.saving:
            raise RuntimeError('Finish the current recording/save before starting another episode')
        self.folder = self.ui.snapshot()['folder']
        self.ui.store.folder(self.folder)
        self.number = self.ui.store.next_number(self.folder)
        self._frames, self._controls = [], []
        self.bytes = self._last_frames = 0
        self.started = time.monotonic()
        self.start_step = self._next_action_step = int(step)
        self.error = None
        self.active = True
        try:
            self.append(step, snapshot, cameras)
        except Exception:
            self.active = False
            self._frames, self._controls = [], []
            raise
        self.ui.update(recording=True, phase='recording', error=None, frames=1,
                       wall_seconds=0., sim_seconds=0., success=False)

    def note_action(self, step, action, source='teleop'):
        if not self.active:
            return
        step = int(step)
        if step != self._next_action_step:
            raise ValueError('Record the applied action before every consecutive control interval')
        value = _array(action)
        if value.shape == (1, len(self.action_names)):
            value = value[0]
        if value.shape != (len(self.action_names),):
            raise ValueError('Action shape differs from the native joint command layout')
        value = value.astype(np.float32)
        if not np.isfinite(value).all():
            raise ValueError('Joint commands must fit finite float32 values')
        size = value.nbytes
        if self.bytes+size > self.max_bytes:
            raise MemoryError('DexBench episode buffer limit exceeded')
        self._controls.append(dict(step=step, action=value, source=str(source)))
        self.bytes += size
        self._next_action_step += self.control_stride

    def append(self, step, snapshot, cameras):
        if not self.active:
            return
        step = int(step)
        if self._frames and step <= self._frames[-1]['step']:
            if step == self._frames[-1]['step']:
                return
            raise ValueError('Observation Physics steps must increase')
        if not cameras:
            raise ValueError('Recording requires RGB camera observations')
        images = {}
        for name, rgb in cameras.items():
            image = np.asarray(rgb)
            if not name.replace('_', '').isalnum() or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                raise ValueError('Invalid RGB camera observation')
            images[name] = image.copy()
        captured = _copy_tree(snapshot)
        if set(captured) != {'robot', 'scene', 'sim_time_s'}:
            raise ValueError('Snapshot requires robot, scene and sim_time_s fields')
        if captured['robot']['qpos'].shape != (len(self.joint_names),) or captured['robot']['qvel'].shape != (len(self.joint_names),):
            raise ValueError('Robot state shape differs from the native joint layout')
        if self._frames:
            first = self._frames[0]
            if _schema(images) != _schema(first['cameras']) or _schema(captured) != _schema(first['snapshot']):
                raise ValueError('Snapshot/camera schema changed inside an episode')
        size = _size(captured)+_size(images)
        if self.bytes+size > self.max_bytes:
            raise MemoryError('DexBench episode buffer limit exceeded')
        self._frames.append(dict(step=step, snapshot=captured, cameras=images,
                                 timestamp_ns=time.time_ns(), bytes=size))
        self.bytes += size
        self.ui.update(frames=self.frames, sim_seconds=(step-self.start_step)*self.physics_dt,
                       wall_seconds=time.monotonic()-self.started)

    def finish(self, step, snapshot, cameras, success=False, reason='manual_stop'):
        if not self.active:
            return
        step = int(step)
        if step > self._next_action_step:
            raise ValueError('Missing applied-action history before terminal observation')
        self.append(step, snapshot, cameras)
        # A stop/error may occur after preparing a command but before stepping.
        # Such a command did not govern any recorded physical interval.
        self._controls = [row for row in self._controls if row['step'] < step]
        now = datetime.now(ZoneInfo('Asia/Seoul'))
        count = len(self._frames)
        filename = f'{self.number:04d}_{"success" if success else "fail"}_{count}_{now:%Y%m%d_%H%M%S}.hdf5'
        metadata = dict(schema=SCHEMA, simulator='dexbench', task_name=self.task_id,
            robot_key=self.launch.get('robot_key', 'dexbench_rby1_wuji2_joint'), success=bool(success),
            success_available=True,
            outcome_source='environment' if reason in ('native_success', 'native_failure', 'time_limit') else 'interrupted',
            termination_reason=reason, episode_length_steps=count,
            episode_length_s=(step-self.start_step)*self.physics_dt,
            wall_duration_s=time.monotonic()-self.started, saved_at=now.isoformat(),
            filename_timezone='Asia/Seoul', episode_index=self.number, data_subfolder=self.folder,
            physics_dt=self.physics_dt, effective_fps=self.sample_hz, control_hz=self.control_hz,
            action_dim=len(self.action_names), control_mode='native_joint_position', collection_mode='teleop',
            demo_eligible=count > 1, frame_count=count, control_history_complete=True,
            modalities=['rgb', 'robot', 'action', 'state'], comparison_replay_supported=False,
            replay_unavailable_reason=REPLAY_UNAVAILABLE_REASON)
        settings = self.ui.snapshot()
        for key in ('haptic_enabled', 'haptic_max_power', 'haptic_force_min_n', 'haptic_force_max_n', 'workspace'):
            if key in settings:
                metadata[key] = copy.deepcopy(settings[key])
        self.job = Episode(self.ui.store.folder(self.folder), filename, metadata, self.launch,
                           self.action_names, self.joint_names, self._frames, self._controls)
        self._last_frames = count
        self._frames, self._controls = [], []
        self.active = False
        self._submit()
        return filename

    def finish_cached(self, reason='simulation_error'):
        """End at the last complete copied observation after a failed native step.

        An exception may leave the live Newton world between integration
        boundaries. Never read that world and label it with the old step index.
        Commands prepared after this observation are discarded by finish().
        """
        if not self.active:
            return None
        if not self._frames:
            raise RuntimeError('No complete observation is available for recovery')
        terminal = self._frames[-1]
        return self.finish(terminal['step'], terminal['snapshot'], terminal['cameras'], False, reason)

    def _submit(self):
        self.save_started = time.monotonic()
        self.error = None
        self.future = self.executor.submit(self._write, self.job)
        self.ui.update(recording=False, phase='saving', error=None,
                       success=bool(self.job.metadata['success']), frames=self._last_frames)

    @staticmethod
    def _write(job):
        result = dict(path=job.write(), frames=job.metadata['frame_count'])
        while job.frames:
            del job.frames[-16:]
            time.sleep(0)
        job.controls.clear()
        return result

    def retry(self):
        if self.job is None or self.future is None or not self.future.done() or self.future.exception() is None:
            raise RuntimeError('There is no failed save to retry')
        self._submit()

    def poll(self, wait=False):
        if self.future is None or (not wait and not self.future.done()):
            return None
        try:
            result = self.future.result()
        except Exception as exc:
            self.error = str(exc)
            self.ui.update(recording=False, phase='save_error', error=self.error)
            return dict(error=self.error)
        self.last_saved = result['path']
        result['seconds'] = time.monotonic()-self.save_started
        self.job = self.future = None
        phase = self.ui.snapshot()['phase']
        self.ui.update(last_saved=result['path'], save_seconds=result['seconds'], error=None,
                       phase='idle' if phase in ('saving', 'save_error') else phase)
        return result

    def close(self):
        if self.active:
            raise RuntimeError('Finish the active DexBench episode before closing the recorder')
        try:
            self.poll(wait=True)
            if self.job is not None:
                recovery = self.ui.state_file.parent/'unsaved-episodes'/uuid.uuid4().hex
                recovery.mkdir(parents=True)
                (recovery/'recovery.json').write_text(json.dumps(dict(
                    intended_path=str(self.job.folder/self.job.filename), error=self.error)))
                path = self.job.write(folder=recovery)
                self.job = self.future = None
                raise RuntimeError(f'Original save failed; episode recovered at {path}')
        finally:
            self.executor.shutdown(wait=True)
