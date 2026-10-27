"""ORCA mocap-root and position-actuator control over the common input protocol."""
import copy
import json
import time
import uuid
from pathlib import Path

import numpy as np

from dex_teleop.core.haptics import ContactHapticEncoder, FINGERS, scale_powers
from dex_teleop.core.session import describe_server
from dex_teleop.core.transport import Receiver
from dex_teleop.core.workspace_limits import validate_workspace, workspace_bounds, workspace_from_control
from dex_teleop.paths import WORKSPACE
from .contacts import FingerContacts
from .retargeting import OrcaRetargeter, RetargetWorker


def conjugate(q):
    return np.asarray(q)*np.array([1., -1., -1., -1.])


def multiply(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array([aw*bw-ax*bx-ay*by-az*bz, aw*bx+ax*bw+ay*bz-az*by,
                     aw*by-ax*bz+ay*bw+az*bx, aw*bz+ax*by-ay*bx+az*bw])


def rotate(q, vector):
    return multiply(multiply(q, np.r_[0., vector]), conjugate(q))[1:]


def limited_rotation(current, target, max_angle):
    target = np.asarray(target, dtype=float)
    target /= np.linalg.norm(target)
    dot = float(np.dot(current, target))
    if dot < 0:
        target, dot = -target, -dot
    half_angle = np.arccos(np.clip(dot, -1., 1.))
    if half_angle < 1e-8 or 2*half_angle <= max_angle:
        return target.copy()
    fraction = max(0., max_angle)/(2*half_angle)
    return (np.sin((1-fraction)*half_angle)*current + np.sin(fraction*half_angle)*target)/np.sin(half_angle)


class Bridge:
    def __init__(self, model, data, config, *, retargeter=None, receiver=None):
        self.model, self.data = model, data
        self.config = copy.deepcopy(config)
        self.control = self.config['control']
        self.sides = ['right']
        if self.control.get('enabled_sides', self.sides) != self.sides:
            raise ValueError('POMDAR currently supports the single right ORCA hand')
        self.receiver = receiver or Receiver(self.config['network'])
        self.root_id = model.body('root').id
        self.mocap_id = int(model.body_mocapid[model.body('hand_mocap').id])
        if self.mocap_id < 0:
            raise ValueError('POMDAR hand_mocap is not a mocap body')
        self.contacts = FingerContacts(model, data)
        # Task mechanisms can have their own actuators (C1 has a bolt motor).
        # Only actuators attached to the hand subtree receive glove targets.
        self.hand_actuator_ids = np.array([i for i, (joint, _) in enumerate(model.actuator_trnid)
            if joint >= 0 and int(model.jnt_bodyid[joint]) in self.contacts.robot_bodies], dtype=int)
        self.joint_ids = np.asarray(model.actuator_trnid[self.hand_actuator_ids, 0], dtype=int).copy()
        self.joint_names = [model.joint(int(i)).name for i in self.joint_ids]
        self.actuator_names = [model.actuator(i).name for i in range(model.nu)]
        self.finger_actuator_names = [self.actuator_names[i] for i in self.hand_actuator_ids]
        self.joint_qpos_indices = np.asarray(model.jnt_qposadr[self.joint_ids], dtype=int)
        self.lower, self.upper = np.asarray(model.actuator_ctrlrange[self.hand_actuator_ids]).T.copy()
        if (not np.all(model.actuator_ctrllimited[self.hand_actuator_ids]) or not np.all(model.jnt_limited[self.joint_ids])
                or not np.allclose(model.actuator_gear[self.hand_actuator_ids, 0], 1.)):
            raise ValueError('POMDAR expects bounded unit-gear joint position actuators')
        self.lower = np.maximum(self.lower, model.jnt_range[self.joint_ids, 0])
        self.upper = np.minimum(self.upper, model.jnt_range[self.joint_ids, 1])
        if np.any(self.lower >= self.upper):
            raise ValueError('Invalid ORCA actuator/joint limits')
        self.ctrl = np.clip(np.asarray(data.ctrl[self.hand_actuator_ids]).copy(), self.lower, self.upper)
        optimizer = retargeter or OrcaRetargeter(self.joint_names, self.lower, self.upper,
                                                self.config.get('retargeting'))
        self.worker = RetargetWorker(optimizer)
        self.haptic_config = copy.deepcopy(self.config['haptics'])
        self.haptic_encoder = ContactHapticEncoder(self.haptic_config)
        self.haptic_enabled = bool(self.haptic_config.get('enabled', True))
        self.haptic_gain = 1.
        self.haptic_seq = 0
        self.haptic_stream = uuid.uuid4().hex
        self.haptic_data = dict(ready=False, powers=dict(right=[0.]*5, left=[0.]*5))
        self.epoch = 1
        self.enabled = True
        self.started = False
        self.last_session = self.last_submitted = None
        self.input_seq = self.finger_seq = -1
        self.source = 'hold'
        self.arm_active = self.finger_active = False
        self.events = dict(home=0, reset=0)
        self.environment_reset_count = 0
        self.last_environment_reset = None
        self.status_path = Path(self.control.get('status_path', 'logs/pomdar/status.json'))
        if not self.status_path.is_absolute():
            self.status_path = WORKSPACE/self.status_path
        self.last_report = self.last_haptic_publish = 0.
        self.last_haptic_active = False
        self._status = {}
        self._calibrate_home()
        self.worker.reset(self.ctrl)
        self._publish()

    @property
    def action_spec(self):
        channels = []
        for i, name in enumerate(self.actuator_names):
            kind, unit = 'task_actuator', 'model_defined'
            if i in self.hand_actuator_ids:
                kind, unit = 'joint_position', 'rad'
            elif self.model.actuator_biastype[i] == 0 and self.model.actuator_gainprm[i, 0] == 1:
                joint = int(self.model.actuator_trnid[i, 0])
                if joint >= 0 and self.model.actuator_gear[i, 0] == 1:
                    kind, unit = ('force', 'N') if self.model.jnt_type[joint] == 2 else ('torque', 'N*m')
            channels.append(dict(name=name, type=kind, unit=unit))
        return dict(type='joint_position_and_mocap_pose', joint_names=self.joint_names,
                    actuator_names=self.actuator_names, joint_unit='radian',
                    hand_actuator_ids=self.hand_actuator_ids.tolist(), channels=channels,
                    position_unit='metre', quaternion_order='wxyz',
                    coordinate_frame='simulation_world_z_up_m', robot='orca_v1', side='right')

    def _calibrate_home(self):
        self.home = self.data.xpos[self.root_id].copy()
        self.home_q = self.data.xquat[self.root_id].copy()
        self.home_q /= np.linalg.norm(self.home_q)
        anchor_p = self.data.mocap_pos[self.mocap_id].copy()
        anchor_q = self.data.mocap_quat[self.mocap_id].copy()
        anchor_q /= np.linalg.norm(anchor_q)
        self.root_offset = rotate(conjugate(anchor_q), self.home-anchor_p)
        self.root_rotation = multiply(conjugate(anchor_q), self.home_q)
        self.target = self.home.copy()
        self.target_q = self.home_q.copy()
        lower, upper = workspace_bounds(self.home, workspace_from_control(self.control))
        self.lower_xyz, self.upper_xyz = np.array(lower), np.array(upper)

    def _write_pose(self):
        # Public pose denotes the physical hand root; the weld anchor is offset.
        anchor_q = multiply(self.target_q, conjugate(self.root_rotation))
        self.data.mocap_quat[self.mocap_id] = anchor_q
        self.data.mocap_pos[self.mocap_id] = self.target-rotate(anchor_q, self.root_offset)

    def start(self):
        if self.started:
            return True
        self.worker.start()
        try:
            self.receiver.start()
        except BaseException:
            self.worker.stop()
            raise
        self.started = True
        return True

    def stop(self):
        self.enabled = False
        self._clear_haptics()
        self.receiver.stop()
        self.worker.stop()
        self.started = False

    def _input(self, now):
        sample = self.receiver.snapshot() if self.enabled else None
        if sample is None or sample[0].get('epoch') != self.epoch:
            return None, None, False, None
        packet, received = sample
        hand = packet['hands'].get('right')
        if hand is None or not hand['active']:
            return packet, None, False, received
        finger = hand.get('finger', {})
        fresh = bool(finger.get('valid') and
                     now-received+finger.get('age_ms', 1e9)/1000 <= self.control['tracking_timeout_s'])
        return packet, hand, fresh, received

    def update(self, dt):
        """Apply one bounded control update; never block on network/retargeting."""
        dt = max(0., min(float(dt), .1))
        now = time.monotonic()
        packet, hand, fresh, received = self._input(now)
        self.source = 'hold'
        self.arm_active = False
        if packet is not None and packet['session'] != self.last_session:
            self.last_session = packet['session']
            self.events = dict(home=0, reset=0)
            self.last_submitted = None
            self.worker.reset(self.ctrl)
        if hand is not None:
            self.input_seq = int(packet['seq'])
            pose = hand['pose']
            home = hand.get('home', 0) > self.events['home']
            if home:
                self.events['home'] = hand['home']
            if pose.get('position_active', True) or home:
                desired = self.home.copy() if home else np.clip(pose['position_xyz'], self.lower_xyz, self.upper_xyz)
                delta = desired-self.target
                distance = np.linalg.norm(delta)
                self.target += delta*min(1., self.control['max_position_speed_m_s']*dt/max(distance, 1e-12))
                # MANUS wrist orientation is never used. A calibrated pose provider
                # such as Vive can replace keyboard's fixed home orientation later.
                desired_q = pose['orientation_wxyz'] if pose['source'] == 'vive' and not home else self.home_q
                self.target_q = limited_rotation(self.target_q, desired_q,
                    self.control.get('max_orientation_speed_rad_s', 1.)*dt)
                self._write_pose()
                self.arm_active = True
                self.source = pose['source']
            if fresh:
                key = (self.epoch, packet['session'], packet['seq'])
                if key != self.last_submitted:
                    captured = received-float(hand['finger'].get('age_ms', 0.))/1000
                    self.worker.submit(hand['finger'], key, captured)
                    self.last_submitted = key
        result = self.worker.latest() if fresh else None
        usable = bool(result is not None and now-result['captured_at'] <= self.control['tracking_timeout_s']
                      and result['key'][:2] == (self.epoch, self.last_session))
        if usable:
            desired = np.asarray(result['ctrl'])
            if desired.shape != self.ctrl.shape:
                raise ValueError('Retargeted actuator dimension differs from the ORCA model')
            bound = self.control['max_finger_joint_speed_rad_s']*dt
            self.ctrl += np.clip(np.clip(desired, self.lower, self.upper)-self.ctrl, -bound, bound)
            self.finger_seq = int(result['key'][2])
            finger_source = result.get('source', 'manus')
            self.source = finger_source if self.source == 'hold' else self.source+'+'+finger_source
        elif self.finger_active:
            # Stop chasing a previously closed target when tracking disappears.
            self.ctrl = np.clip(self.data.qpos[self.joint_qpos_indices], self.lower, self.upper).copy()
        self.finger_active = usable
        self.data.ctrl[self.hand_actuator_ids] = self.ctrl
        self._publish()
        return self.action_snapshot()

    def action_snapshot(self):
        return dict(ctrl=np.array(self.data.ctrl, copy=True), mocap_pos=np.array(self.data.mocap_pos, copy=True),
                    mocap_quat=np.array(self.data.mocap_quat, copy=True), source=self.source,
                    input_seq=self.input_seq, finger_seq=self.finger_seq)

    def after_physics_step(self, dt):
        now = time.monotonic()
        _, _, fresh, _ = self._input(now)
        active = dict(right=bool(self.haptic_enabled and self.enabled and fresh), left=False)
        try:
            forces = self.contacts.read(dt)
            powers = self.haptic_encoder.update(forces, active, dt, now)
            ceiling = self.haptic_encoder.cfg['max_power']
            self.haptic_seq += 1
            self.haptic_data = dict(ready=True, stream_id=self.haptic_stream, epoch=self.epoch,
                seq=self.haptic_seq, finger_order=list(FINGERS), forces_n=forces,
                powers=scale_powers(powers, self.haptic_gain, ceiling), active=active, max_power=ceiling)
        except Exception as exc:
            self.haptic_encoder.reset()
            self.haptic_data = dict(ready=False, error=str(exc), powers=dict(right=[0.]*5, left=[0.]*5))
        # 100 Hz output mailbox; force filtering itself sees every Physics step.
        if now-self.last_haptic_publish >= .01 or active['right'] != self.last_haptic_active:
            self.receiver.set_haptics(self.haptic_data)
            self.last_haptic_publish = now
        self.last_haptic_active = active['right']

    def _clear_haptics(self):
        self.haptic_encoder.reset()
        self.haptic_data = dict(ready=False, powers=dict(right=[0.]*5, left=[0.]*5))
        self.receiver.set_haptics(self.haptic_data)
        self.last_haptic_active = False

    def workspace_status(self):
        return dict(right=dict(home_position_xyz=self.home.tolist(), workspace_min_xyz=self.lower_xyz.tolist(),
                               workspace_max_xyz=self.upper_xyz.tolist()))

    def apply_workspace(self, settings):
        settings = validate_workspace(settings)
        if settings == workspace_from_control(self.control):
            return False
        lower, upper = workspace_bounds(self.home, settings)
        self.lower_xyz, self.upper_xyz = np.array(lower), np.array(upper)
        self.control.update(workspace_lower_radius_xyz=[-x for x in settings['min_offset_xyz']],
                            workspace_radius_xyz=settings['max_offset_xyz'])
        self.reset_input_state()
        return True

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled != self.enabled:
            self.enabled = enabled
            self.reset_input_state()

    def reset_input_state(self):
        self.epoch += 1
        self.arm_active = self.finger_active = False
        self.source = 'hold'
        self.last_submitted = None
        with self.receiver.lock:
            self.receiver.latest = None
        self.target = self.data.xpos[self.root_id].copy()
        self.target_q = self.data.xquat[self.root_id].copy()
        self.ctrl = np.clip(self.data.qpos[self.joint_qpos_indices], self.lower, self.upper).copy()
        self._write_pose()
        self.data.ctrl[self.hand_actuator_ids] = self.ctrl
        self.worker.reset(self.ctrl)
        self._clear_haptics()
        self._publish()

    def reset(self):
        """Call after scene.reset_data; reset robot goals without changing world state."""
        self.environment_reset_count += 1
        self.last_environment_reset = dict(sim_time=float(self.data.time))
        self.reset_input_state()

    def consume_environment_reset(self):
        packet, hand, _, _ = self._input(time.monotonic())
        if hand is not None and hand.get('reset', 0) > self.events['reset']:
            self.events['reset'] = hand['reset']
            return True
        return False

    def status(self):
        return copy.deepcopy(self._status)

    def _publish(self):
        now = time.monotonic()
        hand = dict(home_position_xyz=self.home.tolist(), home_orientation_wxyz=self.home_q.tolist(),
            target_position_xyz=self.target.tolist(), current_position_xyz=self.data.xpos[self.root_id].tolist(),
            current_orientation_wxyz=self.data.xquat[self.root_id].tolist(),
            workspace_min_xyz=self.lower_xyz.tolist(), workspace_max_xyz=self.upper_xyz.tolist(),
            arm_active=self.arm_active, finger_active=self.finger_active, ik_failures=0,
            arm_joint_positions=[], finger_joint_positions=self.data.qpos[self.joint_qpos_indices].tolist(),
            finger_joint_targets=self.ctrl.tolist(),
            home_event_ack=self.events['home'], reset_event_ack=self.events['reset'])
        self._status = dict(ready=self.enabled, epoch=self.epoch, server_id=self.receiver.server_id,
            coordinate_frame='simulation_world_z_up_m', pose_monotonic_s=now,
            teleop=describe_server(self.control, self.receiver.config, haptics=True),
            orientation_mode='keyboard_fixed_home_or_pose_provider', hands=dict(right=hand),
            network=self.receiver.stats(), haptics=self.haptic_data, simulator='pomdar',
            retargeting_error=self.worker.error, action_spec=self.action_spec,
            environment_reset_count=self.environment_reset_count, last_environment_reset=self.last_environment_reset,
            home_key_enabled=self.control.get('home_key_enabled', False))
        self.receiver.set_status(self._status)
        if now-self.last_report >= .5:
            self.last_report = now
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.status_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self._status, indent=2)+'\n')
            temporary.replace(self.status_path)
