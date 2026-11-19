"""Shared network input to the native RBY1-M / Wuji2 joint action and Pink IK."""
import copy
import json
from pathlib import Path
import time
import uuid

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from dex_teleop.core.transport import Receiver
from dex_teleop.core.session import describe_server
from dex_teleop.core.haptics import ContactHapticEncoder, FINGERS
from dex_teleop.core.workspace_limits import validate_workspace, workspace_bounds, workspace_from_control
from .contacts import FingerContacts, as_numpy
from .retargeting import Wuji2Retargeter, RetargetWorker


def as_torch(value):
    import torch
    if isinstance(value, torch.Tensor):
        return value
    if hasattr(value, 'torch'):
        return value.torch
    import warp
    return warp.to_torch(value)


def matrix(quaternion):
    return Rotation.from_quat(np.asarray(quaternion)[[1,2,3,0]]).as_matrix()


def limited_rotation(current, target, max_angle):
    target = np.asarray(target, dtype=float)
    target /= np.linalg.norm(target)
    angle = 2*np.arccos(np.clip(abs(np.dot(current,target)),0,1))
    if angle <= max_angle:
        return target.copy()
    rotations = Rotation.from_quat(np.array([current,target])[:,[1,2,3,0]])
    return Slerp([0,1],rotations)([max_angle/max(angle,1e-12)]).as_quat()[0][[3,0,1,2]]


class Bridge:
    def __init__(self, env, config, receiver=None, embodiment=None, *, retargeter_factory=Wuji2Retargeter,
                 worker_factory=RetargetWorker):
        if env.num_envs != 1:
            raise ValueError('Interactive DexBench teleoperation requires one environment')
        if embodiment is None:
            from dexbench_arena.embodiments.rby1_wuji2.rby1_wuji2 import DexBenchRby1Wuji2JointEmbodiment
            embodiment = DexBenchRby1Wuji2JointEmbodiment
        self.env, self.robot = env, env.scene['robot']
        self.config = copy.deepcopy(config)
        self.control = self.config['control']
        self.workspace = workspace_from_control(self.control)
        self.receiver = receiver or Receiver(config['network'])
        self.sides = ['right','left']
        self.ik = embodiment.eef_arm_ik_controller(env, ik_profile='teleop')
        self.layout = self.ik.settings.layout
        self.joint_names = list(self.robot.joint_names)
        self.term = env.action_manager.get_term('joint_pos')
        ids = self.term._joint_ids
        self.action_ids = list(range(len(self.joint_names))) if isinstance(ids,slice) else [int(x) for x in ids]
        self.action_names = [self.joint_names[i] for i in self.action_ids]
        if len(self.action_names) != 62:
            raise ValueError('Expected the native 62-dimensional Wuji2 joint-position embodiment')
        self.pass_names = list(self.layout.passthrough_joint_names)
        self.pass_ids = [self.joint_names.index(n) for n in self.pass_names]
        self.enabled, self.started, self.epoch = True, False, 1
        self.state, self.workers, self.events, self.last_seq = {}, {}, {}, {}
        self.active, self.arm_active, self.errors = {}, {}, {}
        self.last_session, self.source, self.last_report = None, 'hold', 0.
        self.environment_reset_count = 0
        self.contacts = FingerContacts(env)
        self.haptic_encoder = ContactHapticEncoder(config['haptics'])
        self.haptic_enabled = bool(config['haptics'].get('enabled',True))
        self.haptic_seq, self.stream = 0, uuid.uuid4().hex
        self.haptic_data = dict(ready=False,powers=dict(right=[0.]*5,left=[0.]*5))
        self._held_full = as_torch(self.robot.data.joint_pos_target).clone()
        poses = self._poses()
        limits = as_numpy(self.robot.data.soft_joint_pos_limits)[0]
        for side in self.sides:
            names = [n for n in self.pass_names if n.startswith(side+'_')]
            if len(names) != 20:
                raise ValueError(f'Native {side} passthrough is not a 20-joint hand')
            hand_ids = [self.joint_names.index(n) for n in names]
            lower, upper = limits[hand_ids,0], limits[hand_ids,1]
            p,q = poses[side]
            lo,hi = workspace_bounds(p,self.workspace)
            fingers = np.clip(as_numpy(self._held_full)[0,hand_ids],lower,upper)
            self.state[side] = dict(home=p.copy(),home_q=q.copy(),p=p.copy(),q=q.copy(),
                names=names,ids=hand_ids,lower=lower,upper=upper,lo=np.array(lo),hi=np.array(hi),fingers=fingers)
            self.events[side] = dict(home=0,reset=0)
            retargeter = retargeter_factory(side,names,lower,upper)
            worker = worker_factory(retargeter,side)
            worker.reset(fingers)
            self.workers[side] = worker
        self._publish(poses)

    @property
    def action_spec(self):
        return dict(type='native_joint_position_targets',action_dim=len(self.action_names),
            joint_names=self.action_names,joint_unit='radian',position_unit='metre',
            quaternion_order='wxyz',coordinate_frame='simulation_world_z_up_m',
            robot='rby1m_wujihand2_beta2',sides=self.sides,
            arm_ik='upstream PinkIK teleop profile',finger_retargeting='upstream Wuji2 DexPilot')

    def _root(self):
        # Pinned Isaac Lab 6 / Newton data uses XYZW; the shared Windows wire
        # protocol remains WXYZ. Rotation matrices isolate this boundary.
        return (as_numpy(self.robot.data.root_pos_w)[0],
                Rotation.from_quat(as_numpy(self.robot.data.root_quat_w)[0]).as_matrix())

    def _poses(self):
        root_p,root_r = self._root()
        local = self.ik.wrist_targets_from_joint_positions(as_torch(self.robot.data.joint_pos))
        return {side:(root_p+root_r@as_numpy(pos)[0],
                      Rotation.from_matrix(root_r@as_numpy(rot)[0]).as_quat()[[3,0,1,2]])
                for side,(pos,rot) in local.items()}

    def _joint_action(self, full):
        return (full[:,self.action_ids]-self.term._offset)/self.term._scale

    def start(self):
        for worker in self.workers.values():
            worker.start()
        try:
            self.receiver.start()
        except BaseException:
            for worker in self.workers.values(): worker.stop()
            raise
        self.started = True

    def stop(self):
        self.set_enabled(False)
        if self.started:
            self.receiver.stop()
            self.started = False
        for worker in self.workers.values(): worker.stop()

    def _input(self):
        sample = self.receiver.snapshot() if self.enabled else None
        if sample and sample[0].get('epoch') != self.epoch:
            return None
        return sample

    def update(self, dt):
        import torch
        if not np.isfinite(dt) or dt < 0:
            raise ValueError('Control dt must be finite and nonnegative')
        dt = min(float(dt), float(self.env.step_dt))
        now = time.monotonic()
        sample = self._input()
        sources = set()
        self.active, self.arm_active = {}, {}
        measured = as_torch(self.robot.data.joint_pos)
        if sample and sample[0]['session'] != self.last_session:
            self.last_session = sample[0]['session']
            self.last_seq.clear()
            self.events = {side:dict(home=0,reset=0) for side in self.sides}
            for side,worker in self.workers.items(): worker.reset(self.state[side]['fingers'])
        for side,s in self.state.items():
            hand = sample[0]['hands'].get(side) if sample else None
            fresh = False
            if hand and hand['active']:
                pose = hand['pose']
                home = hand.get('home',0) > self.events[side]['home']
                if home: self.events[side]['home'] = hand['home']
                if pose.get('position_active',True) or home:
                    target = s['home'] if home else np.clip(pose['position_xyz'],s['lo'],s['hi'])
                    delta = target-s['p']
                    s['p'] += delta*min(1.,self.control['max_position_speed_m_s']*dt/max(np.linalg.norm(delta),1e-12))
                    q = pose['orientation_wxyz'] if pose['source']=='vive' and not home else s['home_q']
                    s['q'] = limited_rotation(s['q'],q,self.control.get('max_orientation_speed_rad_s',.8)*dt)
                    self.arm_active[side] = True
                    sources.add(pose['source'])
                finger = hand.get('finger',{})
                age = now-sample[1]+finger.get('age_ms',1e9)/1000
                fresh = bool(finger.get('valid') and age <= self.control['tracking_timeout_s'])
                if fresh:
                    key = (self.epoch,sample[0]['session'],sample[0]['seq'])
                    if key != self.last_seq.get(side):
                        self.workers[side].submit(finger,key,sample[1]-finger.get('age_ms',0)/1000)
                        self.last_seq[side] = key
                    result = self.workers[side].latest()
                    if (result and result['key'][:2] == key[:2]
                            and now-result['captured_at'] <= self.control['tracking_timeout_s']):
                        bound = self.control['max_finger_joint_speed_rad_s']*dt
                        target = np.clip(result['target'],s['lower'],s['upper'])
                        s['fingers'] += np.clip(target-s['fingers'],-bound,bound)
                        sources.add(finger.get('source','manus'))
                        self.active[side] = True
                    error = self.workers[side].error
                    if error: self.errors[side] = error
                    else: self.errors.pop(side,None)
            if not fresh and side in self.last_seq:
                s['fingers'] = np.clip(as_numpy(measured)[0,s['ids']],s['lower'],s['upper'])
                self.workers[side].reset(s['fingers'])
                self.last_seq.pop(side,None)
        self.source = '+'.join(sorted(sources)) or 'hold'
        if not self.enabled:
            action = self._joint_action(self._held_full)
        else:
            root_p,root_r = self._root()
            wrist_targets = {side:(torch.as_tensor((root_r.T@(s['p']-root_p))[None],dtype=torch.float32),
                                  torch.as_tensor((root_r.T@matrix(s['q']))[None],dtype=torch.float32))
                             for side,s in self.state.items()}
            passthrough = self._held_full.clone()
            for s in self.state.values():
                passthrough[:,s['ids']] = torch.as_tensor(s['fingers'],dtype=passthrough.dtype,device=passthrough.device)
            action = self.ik.action(measured,as_torch(self.robot.data.joint_pos_target),wrist_targets,
                                    passthrough[:,self.pass_ids])
            if tuple(action.shape) != (1,62) or not torch.isfinite(action).all():
                raise RuntimeError('Native IK returned an invalid joint action')
        self._publish()
        return action

    def after_physics_step(self, dt):
        sample,now = self._input(),time.monotonic()
        active = {}
        for side in self.sides:
            hand = sample[0]['hands'].get(side) if sample else None
            finger = hand.get('finger',{}) if hand else {}
            fresh = bool(hand and hand['active'] and finger.get('valid') and
                         now-sample[1]+finger.get('age_ms',1e9)/1000 <= self.control['tracking_timeout_s'])
            active[side] = bool(self.enabled and self.haptic_enabled and self.active.get(side) and fresh)
        try:
            forces = self.contacts.read()
            powers = self.haptic_encoder.update(forces,active,dt)
            self.haptic_seq += 1
            self.haptic_data = dict(ready=True,stream_id=self.stream,epoch=self.epoch,seq=self.haptic_seq,
                finger_order=list(FINGERS),forces_n=forces,powers=powers,active=active,
                max_power=self.haptic_encoder.cfg['max_power'])
        except (RuntimeError,ValueError,AttributeError) as exc:
            self.haptic_encoder.reset()
            self.haptic_data = dict(ready=False,error=str(exc),powers=dict(right=[0.]*5,left=[0.]*5))
        self.receiver.set_haptics(self.haptic_data)

    def reset_input_state(self, *, capture_home=False):
        self.epoch += 1
        self.active, self.arm_active, self.last_seq = {}, {}, {}
        self.source = 'hold'
        self._held_full = as_torch(self.robot.data.joint_pos_target).clone()
        self.ik.reset()
        with self.receiver.lock: self.receiver.latest = None
        poses = self._poses()
        for side,s in self.state.items():
            s['p'],s['q'] = poses[side]
            if capture_home:
                s['home'],s['home_q'] = s['p'].copy(),s['q'].copy()
            lo,hi = workspace_bounds(s['home'],self.workspace)
            s['lo'],s['hi'] = np.array(lo),np.array(hi)
            s['fingers'] = np.clip(as_numpy(self._held_full)[0,s['ids']],s['lower'],s['upper'])
            self.workers[side].reset(s['fingers'])
        self.haptic_encoder.reset()
        self.haptic_data = dict(ready=False,powers=dict(right=[0.]*5,left=[0.]*5))
        self.receiver.set_haptics(self.haptic_data)
        self._publish(poses)

    def set_enabled(self, enabled):
        if bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            self.reset_input_state()

    def reset(self):
        self.environment_reset_count += 1
        self.reset_input_state(capture_home=True)

    def consume_environment_reset(self):
        sample = self._input()
        if sample:
            for side,hand in sample[0]['hands'].items():
                if side in self.events and hand['reset'] > self.events[side]['reset']:
                    self.events[side]['reset'] = hand['reset']
                    return True
        return False

    def workspace_status(self):
        return {side:dict(home_position_xyz=s['home'].tolist(),workspace_min_xyz=s['lo'].tolist(),
                         workspace_max_xyz=s['hi'].tolist()) for side,s in self.state.items()}

    def apply_workspace(self, settings):
        self.workspace = validate_workspace(settings)
        self.reset_input_state()

    def status(self):
        return copy.deepcopy(self._status)

    def _publish(self, poses=None):
        now = time.monotonic()
        poses = poses or self._poses()
        positions = as_numpy(self.robot.data.joint_pos)[0]
        hands = {}
        for side,s in self.state.items():
            p,q = poses[side]
            hands[side] = dict(home_position_xyz=s['home'].tolist(),home_orientation_wxyz=s['home_q'].tolist(),
                target_position_xyz=s['p'].tolist(),current_position_xyz=p.tolist(),current_orientation_wxyz=q.tolist(),
                workspace_min_xyz=s['lo'].tolist(),workspace_max_xyz=s['hi'].tolist(),
                finger_joint_positions=positions[s['ids']].tolist(),finger_joint_targets=s['fingers'].tolist(),
                home_event_ack=self.events[side]['home'],reset_event_ack=self.events[side]['reset'],
                arm_active=bool(self.arm_active.get(side)),finger_active=bool(self.active.get(side)))
        self._status = dict(ready=self.enabled,epoch=self.epoch,server_id=self.receiver.server_id,simulator='dexbench',
            teleop=describe_server(self.control,self.receiver.config,haptics=self.contacts.available),hands=hands,
            pose_monotonic_s=now,coordinate_frame='simulation_world_z_up_m',orientation_mode='fixed_home_or_pose_provider',
            network=self.receiver.stats(),haptics=self.haptic_data,retargeting_error=self.errors,
            action_spec=self.action_spec,environment_reset_count=self.environment_reset_count)
        self.receiver.set_status(self._status)
        if self.control.get('status_path') and now-self.last_report > .5:
            path = Path(self.control['status_path'])
            path.parent.mkdir(parents=True,exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(self._status,indent=2)+'\n')
            temp.replace(path)
            self.last_report = now
