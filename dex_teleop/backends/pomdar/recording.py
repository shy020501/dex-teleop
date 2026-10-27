"""MuJoCo observations and the full applied-control timeline, independent of Isaac.

The simulation thread owns mjData. Every saved array is copied at a Physics
boundary; the background HDF5 writer never accesses the live simulator.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import copy
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from zoneinfo import ZoneInfo

import h5py
import numpy as np


SCHEMA = 'pomdar_mujoco_v1'


def capture_state(model, data, derived=None):
    import mujoco
    specification = int(mujoco.mjtState.mjSTATE_INTEGRATION)
    integration = np.empty(mujoco.mj_stateSize(model, specification), dtype=np.float64)
    mujoco.mj_getState(model, data, integration, specification)
    fields = ('qpos', 'qvel', 'act', 'ctrl', 'mocap_pos', 'mocap_quat',
              'eq_active', 'qacc_warmstart')
    result = {name: np.array(getattr(data, name), copy=True) for name in fields}
    # mj_step leaves some derived transforms at the preceding integration
    # state. Recompute only on a private copy, preserving solver warmstarts.
    derived = derived if derived is not None else mujoco.MjData(model)
    mujoco.mj_setState(model, derived, integration, specification)
    mujoco.mj_forward(model, derived)
    result.update(xpos=derived.xpos.copy(), xquat=derived.xquat.copy())
    result.update(integration=integration, state_spec=specification, time=float(data.time))
    return result


def restore_state(model, data, integration, state_spec, *, forward=False):
    import mujoco
    mujoco.mj_setState(model, data, np.asarray(integration, dtype=np.float64), int(state_spec))
    if forward:
        mujoco.mj_forward(model, data)


def model_signature(model):
    import mujoco
    binary = np.empty(mujoco.mj_sizeModel(model), dtype=np.uint8)
    mujoco.mj_saveModel(model, buffer=binary)
    return dict(nq=model.nq, nv=model.nv, nu=model.nu, na=model.na,
                nmocap=model.nmocap, nbody=model.nbody, neq=model.neq,
                binary_model_sha256=hashlib.sha256(binary).hexdigest(),
                actuator_specs=[actuator_spec(model, i) for i in range(model.nu)],
                actuator_names=[mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_ACTUATOR, i) or f'actuator_{i}'
                                for i in range(model.nu)],
                joint_names=[mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, i) or f'joint_{i}'
                             for i in range(model.njnt)],
                body_names=[mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, i) or f'body_{i}'
                            for i in range(model.nbody)],
                actuator_qpos_addresses=[int(model.jnt_qposadr[j]) if (j := int(model.actuator_trnid[i, 0])) >= 0 else -1
                                         for i in range(model.nu)])


def actuator_spec(model, index):
    """Describe controls from the loaded model, including task motors."""
    import mujoco
    joint = int(model.actuator_trnid[index, 0])
    result = dict(kind='model_actuator_control', unit='model_defined')
    if model.actuator_trntype[index] != mujoco.mjtTrn.mjTRN_JOINT or joint < 0:
        return result
    joint_type = model.jnt_type[joint]
    angle = joint_type == mujoco.mjtJoint.mjJNT_HINGE
    slide = joint_type == mujoco.mjtJoint.mjJNT_SLIDE
    if not (angle or slide) or not np.isclose(model.actuator_gear[index, 0], 1.):
        return result
    gain = model.actuator_gainprm[index, 0]
    bias = model.actuator_biasprm[index]
    if model.actuator_biastype[index] == mujoco.mjtBias.mjBIAS_AFFINE and np.isclose(bias[1], -gain) and gain > 0:
        return dict(kind='joint_position_target', unit='rad' if angle else 'm')
    if model.actuator_biastype[index] == mujoco.mjtBias.mjBIAS_AFFINE and np.isclose(bias[2], -gain) and gain > 0:
        return dict(kind='joint_velocity_target', unit='rad/s' if angle else 'm/s')
    if model.actuator_biastype[index] == mujoco.mjtBias.mjBIAS_NONE and np.isclose(gain, 1.):
        return dict(kind='joint_motor_force', unit='N*m' if angle else 'N')
    return result


def _dataset(group, name, value):
    if isinstance(value, (dict, list, tuple)) and not isinstance(value, np.ndarray):
        value = json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return group.create_dataset(name, data=value, dtype=h5py.string_dtype('utf-8'))
    return group.create_dataset(name, data=value)


@dataclass
class Episode:
    folder: Path
    filename: str
    metadata: dict
    launch: dict
    signature: dict
    frames: list
    controls: list

    def write(self, folder=None):
        folder = Path(folder or self.folder)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder/f'.saving-{uuid.uuid4().hex}.hdf5'
        final = folder/self.filename
        try:
            with h5py.File(temporary, 'w') as h:
                self._write(h)
                h.flush()
            # Atomic no-replace publication, including independent server instances.
            os.link(temporary, final)
            return str(final)
        finally:
            temporary.unlink(missing_ok=True)

    def _write(self, h):
        frames, controls = self.frames, self.controls
        n = len(frames)
        state = [frame['state'] for frame in frames]
        steps = np.asarray([frame['step'] for frame in frames], dtype=np.int64)
        h.attrs['schema'] = SCHEMA
        meta = h.create_group('meta')
        for key, value in self.metadata.items():
            if value is not None:
                _dataset(meta, key, value)
        times = h.create_group('time')
        times['frame_index'] = np.arange(n, dtype=np.int64)
        times['sim_step'] = steps
        times['sim_time_s'] = np.asarray([row['time'] for row in state], dtype=np.float64)
        times['timestamp_ns'] = np.asarray([frame['timestamp_ns'] for frame in frames], dtype=np.int64)
        robot = h.create_group('robot')
        for key in ('qpos', 'qvel'):
            robot[key] = np.stack([row[key] for row in state])
        robot.create_dataset('joint_names', data=self.signature['joint_names'], dtype=h5py.string_dtype())
        robot.attrs['qpos_layout'] = 'MuJoCo nq layout: free/ball joints occupy more than one scalar'
        robot.attrs['qvel_layout'] = 'MuJoCo nv layout; differs from nq for quaternion joints'

        action = h.create_group('action')
        timeline_steps = np.asarray([row['step'] for row in controls], dtype=np.int64)
        selected = np.searchsorted(timeline_steps, steps, side='right')-1
        commanded = np.stack([controls[max(0, i)]['ctrl'] for i in selected])
        mocap_position = np.stack([controls[max(0, i)]['mocap_pos'] for i in selected])
        mocap_orientation = np.stack([controls[max(0, i)]['mocap_quat'] for i in selected])
        # A terminal observation has no subsequent action. The dense event log
        # below carries all actual input changes between observation samples.
        commanded[-1] = np.nan
        mocap_position[-1] = np.nan
        mocap_orientation[-1] = np.nan
        action['commanded'] = commanded
        action['mocap_position_xyz'] = mocap_position
        action['mocap_orientation_wxyz'] = mocap_orientation
        action['action_valid'] = np.arange(n) < n-1
        action.create_dataset('action_names', data=self.signature['actuator_names'], dtype=h5py.string_dtype())
        for name, field in (('control_types', 'kind'), ('control_units', 'unit')):
            action.create_dataset(name, data=[entry[field] for entry in self.signature['actuator_specs']], dtype=h5py.string_dtype())
        action.create_dataset('source', data=[controls[max(0, i)]['source'] if k < n-1 else 'none'
                                             for k, i in enumerate(selected)], dtype=h5py.string_dtype())
        _dataset(action, 'control_mode', 'model_actuators_and_mocap_weld')
        _dataset(action, 'action_type', 'actuator_controls_and_world_mocap_pose')
        action.attrs['commanded_semantics'] = 'target active at this observation; see replay/control for all later updates'
        action.attrs['mocap_coordinate_frame'] = 'pomdar_world_z_up_m'

        cameras = h.create_group('cameras')
        for name, initial in frames[0]['cameras'].items():
            group = cameras.create_group(name)
            rgb = group.create_dataset('rgb', shape=(n, *initial.shape), dtype=np.uint8,
                                       chunks=(1, *initial.shape), compression='lzf')
            for index, frame in enumerate(frames):
                rgb[index] = frame['cameras'][name]
            _dataset(group, 'camera_model', 'mujoco_pinhole')
            _dataset(group, 'raw_image_semantics', 'RGB uint8, rectangular, no browser mask')
        h['frame_valid'] = np.ones(n, dtype=bool)
        h.create_dataset('frame_errors', data=['']*n, dtype=h5py.string_dtype())
        episode = h.create_group('episode')
        episode['is_first'] = np.arange(n) == 0
        episode['is_last'] = np.arange(n) == n-1
        episode['done'] = np.arange(n) == n-1
        episode['success'] = (np.arange(n) == n-1) & bool(self.metadata['success'])
        replay = h.create_group('replay')
        _dataset(replay, 'schema', SCHEMA)
        _dataset(replay, 'launch', self.launch)
        _dataset(replay, 'signature', self.signature)
        replay['render_sim_step'] = steps
        snapshots = replay.create_group('state')
        snapshots.attrs['state_spec'] = int(state[0]['state_spec'])
        for key in ('integration', 'qpos', 'qvel', 'act', 'ctrl', 'mocap_pos', 'mocap_quat',
                    'eq_active', 'qacc_warmstart', 'xpos', 'xquat'):
            snapshots[key] = np.stack([row[key] for row in state])
        timeline = replay.create_group('control')
        timeline['sim_step'] = timeline_steps
        for key in ('ctrl', 'mocap_pos', 'mocap_quat'):
            timeline[key] = np.stack([row[key] for row in controls])
        timeline.create_dataset('source', data=[row['source'] for row in controls], dtype=h5py.string_dtype())


class RecordingSession:
    def __init__(self, ui, model, task_id, launch_config, *, sample_hz=20., max_frames=1800,
                 max_buffer_mb=6144):
        self.ui, self.model, self.task_id = ui, model, task_id
        self.launch = copy.deepcopy(launch_config)
        self.launch.update(task=task_id, simulator='pomdar', physics_dt=float(model.opt.timestep))
        self.signature = model_signature(model)
        import mujoco
        self._derived_data = mujoco.MjData(model)
        self.sample_hz = float(sample_hz)
        self.max_frames, self.max_bytes = int(max_frames), int(max_buffer_mb*1024**2)
        if not np.isfinite(self.sample_hz) or self.sample_hz <= 0 or not 2 <= self.max_frames <= 10000 or self.max_bytes <= 0:
            raise ValueError('Invalid POMDAR recording rate, frame limit or buffer size')
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pomdar-hdf5')
        self.active = False
        self._frames, self._controls = [], []
        self._last_frames = 0
        self.bytes = 0
        self.job = self.future = None
        self.error = self.last_saved = None
        self._last_control_step = None
        self._source = 'none'

    @property
    def frames(self):
        return len(self._frames) if self.active else self._last_frames

    @property
    def saving(self):
        return self.job is not None

    @property
    def limit_reached(self):
        # Reserve room for a final observation without dropping camera/state pairs.
        return self.active and (len(self._frames) >= self.max_frames or
                                (self._frames and self.bytes+self._frames[-1]['bytes'] > self.max_bytes))

    def start(self, step, data, cameras):
        if self.active or self.saving:
            raise RuntimeError('Finish the current recording/save before starting another episode')
        self.folder = self.ui.snapshot()['folder']
        self.ui.store.folder(self.folder)
        self.number = self.ui.store.next_number(self.folder)
        self._frames, self._controls = [], []
        self.bytes = 0
        self._last_frames = 0
        self.started = time.monotonic()
        self.start_step = int(step)
        self._last_control_step = int(step)-1
        self.error = None
        self.active = True
        try:
            self._controls.append(self._control(step, data, 'none'))
            self.append(step, data, cameras)
        except Exception:
            self.active = False
            self._frames, self._controls = [], []
            raise
        self.ui.update(recording=True, phase='recording', error=None, frames=1,
                       wall_seconds=0., sim_seconds=0., success=False)

    @staticmethod
    def _control(step, data, source):
        return dict(step=int(step), source=str(source),
                    **{key: np.array(getattr(data, key), dtype=np.float64, copy=True)
                       for key in ('ctrl', 'mocap_pos', 'mocap_quat')})

    def note_control(self, step, data, source='teleop'):
        if not self.active:
            return
        step = int(step)
        if step != self._last_control_step+1:
            raise ValueError('Recording requires note_control before every consecutive Physics step')
        self._last_control_step = step
        current = self._control(step, data, source)
        previous = self._controls[-1]
        if any(not np.array_equal(current[key], previous[key]) for key in ('ctrl', 'mocap_pos', 'mocap_quat')) or source != previous['source']:
            if previous['step'] == step:
                self._controls[-1] = current
            else:
                self._controls.append(current)

    def append(self, step, data, cameras):
        if not self.active:
            return
        step = int(step)
        if self._frames and step <= self._frames[-1]['step']:
            if step == self._frames[-1]['step']:
                return
            raise ValueError('Observation Physics steps must increase')
        if not cameras:
            raise ValueError('Recording requires at least one RGB camera')
        images = {}
        for name, rgb in cameras.items():
            image = np.asarray(rgb)
            if not name.replace('_', '').isalnum() or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                raise ValueError('Invalid RGB camera sample')
            if self._frames and (name not in self._frames[0]['cameras'] or image.shape != self._frames[0]['cameras'][name].shape):
                raise ValueError('Camera schema changed inside an episode')
            images[name] = image.copy()
        if self._frames and set(images) != set(self._frames[0]['cameras']):
            raise ValueError('Missing recorded camera')
        state = capture_state(self.model, data, self._derived_data)
        size = sum(array.nbytes for array in images.values())+sum(v.nbytes for v in state.values() if isinstance(v, np.ndarray))
        if self.bytes+size > self.max_bytes:
            raise MemoryError('POMDAR episode buffer limit exceeded')
        self._frames.append(dict(step=step, timestamp_ns=time.time_ns(), state=state, cameras=images, bytes=size))
        self.bytes += size
        self.ui.update(frames=len(self._frames), sim_seconds=(step-self.start_step)*self.model.opt.timestep,
                       wall_seconds=time.monotonic()-self.started)

    def finish(self, step, data, cameras, success=False, reason='manual_stop', *, stopped_at=None):
        if not self.active:
            return
        step = int(step)
        if step > self.start_step and self._last_control_step != step-1:
            raise ValueError('Missing applied-control history before terminal observation')
        self.append(step, data, cameras)
        now = datetime.now(ZoneInfo('Asia/Seoul'))
        count = len(self._frames)
        filename = f'{self.number:04d}_{"success" if success else "fail"}_{count}_{now:%Y%m%d_%H%M%S}.hdf5'
        metadata = dict(schema=SCHEMA, simulator='pomdar', task_name=self.task_id, robot_key='orca_v1',
                        success=bool(success), success_available=False,
                        outcome_source='manual' if reason == 'manual_stop' else 'interrupted',
                        termination_reason=reason, episode_length_steps=count,
                        episode_length_s=(step-self.start_step)*float(self.model.opt.timestep),
                        wall_duration_s=(time.monotonic() if stopped_at is None else stopped_at)-self.started,
                        saved_at=now.isoformat(),
                        filename_timezone='Asia/Seoul', episode_index=self.number,
                        data_subfolder=self.folder, physics_dt=float(self.model.opt.timestep),
                        effective_fps=self.sample_hz, action_dim=self.model.nu,
                        control_mode='model_actuators_and_mocap_weld', collection_mode='teleop',
                        demo_eligible=count > 1, frame_count=count, control_history_complete=True,
                        modalities=['rgb', 'robot', 'action', 'state'])
        import mujoco
        metadata['mujoco_version'] = mujoco.__version__
        settings = self.ui.snapshot()
        for key in ('haptic_enabled', 'haptic_max_power', 'haptic_force_min_n', 'haptic_force_max_n', 'workspace'):
            if key in settings:
                metadata[key] = copy.deepcopy(settings[key])
        job = Episode(self.ui.store.folder(self.folder), filename, metadata, self.launch,
                      self.signature, self._frames, self._controls)
        self._last_frames = count
        self._frames, self._controls = [], []
        self.active = False
        self.job = job
        self._submit()
        return filename

    def _submit(self):
        self.save_started = time.monotonic()
        self.error = None
        self.future = self.executor.submit(self._write, self.job)
        self.ui.update(recording=False, phase='saving', error=None,
                       success=bool(self.job.metadata['success']), frames=self._last_frames)

    @staticmethod
    def _write(job):
        result = dict(path=job.write(), frames=job.metadata['frame_count'])
        # Free potentially gigabytes on the save worker in small batches. Failed
        # writes skip this block and keep all original arrays available to retry.
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
            raise RuntimeError('Finish the active POMDAR episode before closing the recorder')
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
