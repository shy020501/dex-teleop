"""Reuse Bench2Dex's TeleopController, articulation mapping and Pinocchio CLIK."""
import copy
import json
import time
import uuid
from pathlib import Path
import numpy as np
import torch
import pinocchio as pin
from scipy.spatial.transform import Rotation
from teleop.teleop_controller import TeleopController
from teleop.shm_reader import HandFrame, ShmFrame, ShmReader
from teleop.isaaclab_bridge import TeleopIsaacLabBridge
from teleop.arm_ik_controller import ArmIKController, ARM_JOINT_CONFIGS
from dex_teleop.core.protocol import load_config, SIDES
from dex_teleop.core.transport import Receiver
from dex_teleop.core.workspace_limits import validate_workspace, workspace_from_control, workspace_bounds


def rotation(q):
    return Rotation.from_quat(np.asarray(q)[[1,2,3,0]]).as_matrix()


def quat(matrix):
    return Rotation.from_matrix(matrix).as_quat()[[3,0,1,2]]


class NetworkSkeletonReader:
    """Drop-in ShmReader interface over a latest network snapshot; no POSIX SHM."""
    get_pos_dict = ShmReader.get_pos_dict

    def __init__(self, receiver, tracking_timeout):
        self.receiver, self.tracking_timeout = receiver, tracking_timeout
        self.last_key = None

    def open(self): return True
    def close(self): pass
    def wait_for_data(self, timeout=0): return self.receiver.snapshot() is not None

    def read_if_new(self):
        snapshot = self.receiver.snapshot()
        if snapshot is None:
            return None
        packet, received = snapshot
        key = packet['session'], packet['seq']
        if key == self.last_key:
            return None
        self.last_key = key
        result = ShmFrame(seq=packet['seq'], timestamp_us=packet.get('timestamp_unix_ns',0)//1000)
        for side in SIDES:
            hand = packet['hands'].get(side)
            if not hand or not hand['active']:
                continue
            finger = hand['finger']
            if not finger['valid']:
                continue
            age = finger.get('age_ms',1e9)/1000 + time.monotonic()-received
            if not finger['valid'] or age > self.tracking_timeout:
                continue
            frame = HandFrame(valid=True, side=2 if side=='right' else 1, node_count=25,
                              positions=np.array(finger['positions'],dtype=np.float32),
                              quaternions=np.array(finger['quaternions'],dtype=np.float32))
            setattr(result, side, frame)
            result.hand_count += 1
        return result


class NetworkTeleopBridge(TeleopIsaacLabBridge):
    def __init__(self, articulation, config_path=None, hand_type='wuji'):
        self.config = load_config(config_path)
        self.control = self.config['control']
        self.sides = self.control['enabled_sides']
        if not self.sides or not set(self.sides).issubset(SIDES):
            raise ValueError('Invalid enabled_sides')
        self.receiver = Receiver(self.config['network'])
        controller = TeleopController(hand_type=hand_type, enable_right='right' in self.sides, enable_left='left' in self.sides)
        controller._reader = NetworkSkeletonReader(self.receiver,self.control['tracking_timeout_s'])
        super().__init__(controller, articulation, enable_arm_teleop=False)
        self.arms, self.poses = {}, {}
        self.arm_active = {s:False for s in self.sides}
        self.finger_active = {s:False for s in self.sides}
        self.events = {s:{'home':0,'reset':0} for s in self.sides}
        self.haptic_config=self.config.get('haptics',{})
        self.contacts=None
        self.haptic_encoder=None
        self.haptic_enabled=True
        self.haptic_seq=0
        self.haptic_stream=uuid.uuid4().hex
        self.haptic_data={'ready':False,'powers':{s:[0.]*5 for s in SIDES}}
        self.environment_reset_count=0
        self.last_environment_reset=None
        self.reset_session=None
        self.reset_counters={s:0 for s in SIDES}
        self.epoch = 1
        self.last_update = time.monotonic()
        self.last_report = 0.
        self.last_session = None
        self.ik_failures = {s:0 for s in self.sides}
        from dex_teleop.paths import WORKSPACE
        self.root = WORKSPACE
        self.status_path = self.root/self.control['status_path']

    def _setup_visualization(self):
        # The browser displays actual robot geometry; avoid optional legacy debug markers.
        pass

    def start(self, wait_timeout=0):
        if self._started:
            return True
        if not super().start(wait_timeout=0):
            return False
        self._initialize_arms()
        if self.haptic_config.get('enabled',False):
            from dex_teleop.backends.bench2dex.contacts import FingerContacts
            from dex_teleop.core.haptics import ContactHapticEncoder
            self.contacts=FingerContacts()
            self.haptic_encoder=ContactHapticEncoder(self.haptic_config)
        self._enable_arm_teleop = True
        self._publish()
        self.receiver.start()
        print(f'[REMOTE] Ready on {self.config["network"]["bind"]}; enabled sides={self.sides}',flush=True)
        return True

    def _initialize_arms(self):
        # Same UR5 CLIK/config, with the exact Wuji URDF and name-based q indices.
        cfg = copy.deepcopy(ARM_JOINT_CONFIGS['ur5'])
        cfg['urdf_path'] = str(self.root/'assets/bench2dex/URDF-Zoo/ur5+wuji/urdf/Multi_UR5_wuji_with_flange.urdf')
        for side in SIDES:
            cfg[side]['pin_arm_q_indices'] = 'auto'
        ARM_JOINT_CONFIGS['ur5_network_wuji'] = cfg
        art = self._articulation
        for side in self.sides:
            ik = ArmIKController('ur5_network_wuji',art.num_instances,str(art.device),side=side,hand_type=self._teleop.hand_type)
            if ik._pin_model is None:
                raise RuntimeError('Arm URDF missing: run scripts/bench2dex/download_teleop_assets.py')
            indices = [self._joint_names.index(n) for n in ik.joint_names]
            ee_idx = list(art.data.body_names).index(ik.ee_body)
            palm_idx = list(art.data.body_names).index(side+'_palm_link')
            p = art.data.body_pos_w[0,palm_idx].cpu().numpy().astype(float)
            q = art.data.body_quat_w[0,palm_idx].cpu().numpy().astype(float)
            ee_p = art.data.body_pos_w[0,ee_idx].cpu().numpy().astype(float)
            ee_q = art.data.body_quat_w[0,ee_idx].cpu().numpy().astype(float)
            # Calibrate the actual palm offset from this USD, not RH56 geometry constants.
            ik._ee_offset = rotation(ee_q).T@(p-ee_p)
            lower, upper = workspace_bounds(p, workspace_from_control(self.control))
            self.arms[side] = dict(ik=ik, indices=indices, ee_idx=ee_idx, palm_idx=palm_idx,
                                   palm_to_ee=rotation(q).T@rotation(ee_q))
            self.poses[side] = dict(home=p.copy(),target=p.copy(),q=q/np.linalg.norm(q),
                                    lower=np.asarray(lower),upper=np.asarray(upper))
            print(f'[REMOTE] {side} home palm={p.tolist()}, orientation(wxyz)={q.tolist()}',flush=True)

    def workspace_status(self):
        return {side:dict(home_position_xyz=pose['home'].tolist(),
                          workspace_min_xyz=pose['lower'].tolist(), workspace_max_xyz=pose['upper'].tolist())
                for side,pose in self.poses.items()}

    def apply_workspace(self, settings):
        """Simulation thread only. Retain actual pose; re-arm input with the existing epoch protocol."""
        settings = validate_workspace(settings)
        bounds = {side:workspace_bounds(pose['home'], settings) for side,pose in self.poses.items()}
        if not bounds:
            raise ValueError('로봇의 Home 위치 준비 중입니다.')
        if settings == workspace_from_control(self.control):
            return False
        for side,(lower,upper) in bounds.items():
            self.poses[side].update(lower=np.asarray(lower), upper=np.asarray(upper))
        self.control.update(workspace_lower_radius_xyz=[-v for v in settings['min_offset_xyz']],
                            workspace_radius_xyz=list(settings['max_offset_xyz']))
        # Do not project the physical palm or current target to a newly narrowed box.
        # Clear stale commands; the next intentional movement keeps the existing speed limits.
        self.reset_input_state()
        return True

    def _freeze_arm(self, side, target):
        if self.arm_active[side]:
            arm = self.arms[side]
            target[:,arm['indices']] = self._articulation.data.joint_pos[:,arm['indices']]
            self.poses[side]['target'] = self._articulation.data.body_pos_w[0,arm['palm_idx']].cpu().numpy().astype(float)
            arm['ik'].reset()
        self.arm_active[side] = False

    def _update_arm_ik(self, target):
        snapshot = self.receiver.snapshot()
        packet = snapshot[0] if snapshot else None
        if packet and packet.get('epoch') != self.epoch:
            packet = None
        changed = False
        if packet and packet['session'] != self.last_session:
            self.last_session = packet['session']
            self.events = {s:{'home':0,'reset':0} for s in self.sides}
            for arm in self.arms.values(): arm['ik'].reset()
            self._teleop.reset_filters()
        for side in self.sides:
            hand = packet['hands'].get(side) if packet else None
            if not hand or not hand['active']:
                self._freeze_arm(side,target)
                continue
            arm, pose = self.arms[side], self.poses[side]
            art, ik = self._articulation, arm['ik']
            desired = np.clip(hand['pose']['position_xyz'],pose['lower'],pose['upper'])
            for event in ('home',):
                if hand[event] > self.events[side][event]:
                    self.events[side][event] = hand[event]
                    desired = pose['home'].copy() if event=='home' else art.data.body_pos_w[0,arm['palm_idx']].cpu().numpy().astype(float)
                    ik.reset()
            if not hand['pose'].get('position_active',True):
                self._freeze_arm(side,target)
                continue
            delta = desired-pose['target']
            distance = np.linalg.norm(delta)
            limit = self.control['max_position_speed_m_s']*self.dt
            next_position = pose['target']+delta*min(1.,limit/max(distance,1e-12))
            # Keyboard orientation is always the measured HOME palm orientation.
            target_q = quat(rotation(pose['q'])@arm['palm_to_ee'])
            try:
                q_des = ik.compute(next_position,target_q,
                    art.data.joint_pos[:,arm['indices']], art.data.body_pos_w[:,arm['ee_idx']],
                    art.data.body_quat_w[:,arm['ee_idx']],art.data.root_pos_w,art.data.root_quat_w)
                if not torch.isfinite(q_des).all():
                    raise ValueError('Non-finite IK output')
                # Inspect the existing IK solution in Cartesian space before accepting it.
                q_full = np.zeros(ik._pin_nq)
                q_full[ik._pin_arm_q_idx] = q_des[0].cpu().numpy()
                pin.forwardKinematics(ik._pin_model,ik._pin_data,q_full)
                pin.updateFramePlacements(ik._pin_model,ik._pin_data)
                predicted = ik._T_base_to_world*ik._pin_data.oMf[ik._ee_frame_id]
                palm_pred = predicted.translation+predicted.rotation@ik._ee_offset
                pos_error = float(np.linalg.norm(palm_pred-next_position))
                ori_error = float(np.linalg.norm(pin.log3(predicted.rotation.T@rotation(target_q))))
                if pos_error>self.control['max_ik_position_error_m'] or ori_error>self.control['max_ik_orientation_error_rad']:
                    raise ValueError(f'IK residual too large: pos={pos_error:.4f}, ori={ori_error:.4f}')
                before = target[:,arm['indices']].clone()
                bound = self.control['max_arm_joint_speed_rad_s']*self.dt
                target[:,arm['indices']] = before+torch.clamp(q_des-before,-bound,bound)
                pose['target'] = next_position
                self.arm_active[side] = True
                changed = True
            except (ValueError,RuntimeError) as exc:
                self.ik_failures[side] += 1
                ik.reset()
                if self.ik_failures[side] in (1,10) or self.ik_failures[side]%100==0:
                    print(f'[REMOTE] Holding {side} after IK failure: {exc}',flush=True)
        return changed

    def update(self, hold_targets):
        now = time.monotonic()
        self.dt = min(max(now-self.last_update,.001),.1)
        self.last_update = now
        target = hold_targets.get(id(self._articulation))
        if target is None:
            return False
        before = target.clone()
        applied = super().update(hold_targets)
        snapshot = self.receiver.snapshot()
        for side in self.sides:
            indices = getattr(self,f'_{side}_art_indices')
            hand = snapshot[0]['hands'].get(side) if snapshot else None
            finger = hand['finger'] if hand else {'valid':False}
            fresh = bool(hand and hand['active'] and finger['valid'] and
                         finger.get('age_ms',1e9)/1000+now-snapshot[1] <= self.control['tracking_timeout_s'])
            if fresh:
                bound = self.control['max_finger_joint_speed_rad_s']*self.dt
                target[:,indices] = before[:,indices]+torch.clamp(target[:,indices]-before[:,indices],-bound,bound)
            elif self.finger_active[side]:
                target[:,indices] = self._articulation.data.joint_pos[:,indices]
            self.finger_active[side] = fresh
        # Respect the actual USD articulation limits as a final guard.
        limits = self._articulation.data.joint_pos_limits[0]
        affected = sorted({i for a in self.arms.values() for i in a['indices']} |
                          {i for s in self.sides for i in getattr(self,f'_{s}_art_indices')})
        target[:,affected] = torch.maximum(torch.minimum(target[:,affected],limits[affected,1]),limits[affected,0])
        self._publish()
        return applied

    def consume_environment_reset(self):
        snapshot=self.receiver.snapshot()
        if snapshot is None:return False
        packet=snapshot[0]
        if packet.get('epoch')!=self.epoch:return False
        if packet['session']!=self.reset_session:
            self.reset_session=packet['session']
            self.reset_counters={s:0 for s in SIDES}
        requested=False
        for side,hand in packet['hands'].items():
            if hand['active'] and hand['reset']>self.reset_counters[side]:
                self.reset_counters[side]=hand['reset']
                self.events[side]['reset']=hand['reset']
                requested=True
        if requested:
            self.last_session=packet['session']
        return requested

    def complete_environment_reset(self,audit):
        self.environment_reset_count+=1
        self.last_environment_reset=audit
        self.reset_input_state()

    def after_physics_step(self,dt):
        if self.contacts is None:return
        snapshot=self.receiver.snapshot()
        active={s:False for s in SIDES}
        if snapshot:
            packet,stamp=snapshot
            if packet.get('epoch')==self.epoch:
                for side,hand in packet['hands'].items():
                    f=hand['finger']
                    active[side]=bool(self.haptic_enabled and hand['active'] and f['valid'] and
                        f.get('age_ms',1e9)/1000+time.monotonic()-stamp<=self.control['tracking_timeout_s'])
        try:
            forces=self.contacts.read(dt)
            powers=self.haptic_encoder.update(forces,active,dt)
            gain=getattr(self,"haptic_gain",1.)
            from dex_teleop.core.haptics import scale_powers
            ceiling=self.haptic_encoder.cfg['max_power']
            powers=scale_powers(powers,gain,ceiling)
            self.haptic_seq+=1
            self.haptic_data={'ready':True,'stream_id':self.haptic_stream,'epoch':self.epoch,
                'seq':self.haptic_seq,'finger_order':['thumb','index','middle','ring','pinky'],
                'forces_n':forces,'powers':powers,'active':active,'max_power':ceiling}
        except Exception as exc:
            self.haptic_encoder.reset()
            self.haptic_data={'ready':False,'error':str(exc),'powers':{s:[0.]*5 for s in SIDES}}
        self.receiver.set_haptics(self.haptic_data)

    def _publish(self):
        from dex_teleop.core.session import describe_server
        art = self._articulation
        hands = {}
        for side,pose in self.poses.items():
            arm = self.arms[side]
            current = art.data.body_pos_w[0,arm['palm_idx']].cpu().numpy()
            current_q = art.data.body_quat_w[0,arm['palm_idx']].cpu().numpy()
            hands[side] = {'home_position_xyz':pose['home'].tolist(), 'home_orientation_wxyz':pose['q'].tolist(),
                          'target_position_xyz':pose['target'].tolist(), 'current_position_xyz':current.tolist(),
                          'current_orientation_wxyz':current_q.tolist(),
                          'workspace_min_xyz':pose['lower'].tolist(), 'workspace_max_xyz':pose['upper'].tolist(),
                          'arm_active':self.arm_active[side], 'finger_active':self.finger_active[side],
                          'ik_failures':self.ik_failures[side],
                          'arm_joint_positions':art.data.joint_pos[0,arm['indices']].cpu().tolist(),
                          'finger_joint_positions':art.data.joint_pos[0,getattr(self,f'_{side}_art_indices')].cpu().tolist(),
                          'home_event_ack':self.events[side]['home'], 'reset_event_ack':self.events[side]['reset']}
        status = {'ready':bool(self.poses), 'epoch':self.epoch, 'coordinate_frame':'simulation_world_z_up_m',
                  'server_id':self.receiver.server_id,
                  'teleop':describe_server(self.control, self.receiver.config, haptics=self.haptic_encoder is not None),
                  'pose_monotonic_s':time.monotonic(),
                  'orientation_mode':'fixed_home', 'hands':hands, 'network':self.receiver.stats(),
                  'haptics':self.haptic_data,'environment_reset_count':self.environment_reset_count,
                  'last_environment_reset':self.last_environment_reset,
                  'home_key_enabled':self.control.get('home_key_enabled',True)}
        self.receiver.set_status(status)
        if time.monotonic()-self.last_report>.5:
            self.last_report = time.monotonic()
            self.status_path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.status_path.with_suffix('.tmp')
            temp.write_text(json.dumps(status,indent=2)+'\n')
            temp.replace(self.status_path)

    def reset_input_state(self):
        self.epoch += 1
        if self.haptic_encoder:self.haptic_encoder.reset()
        self.haptic_data={'ready':False,'powers':{s:[0.]*5 for s in SIDES}}
        self.receiver.set_haptics(self.haptic_data)
        with self.receiver.lock:
            self.receiver.latest = None
        self._teleop.reset_filters()
        for side,arm in self.arms.items():
            arm['ik'].reset()
            self.poses[side]['target'] = self._articulation.data.body_pos_w[0,arm['palm_idx']].cpu().numpy().astype(float)
            self.arm_active[side] = False
        self._publish()

    def reset_articulation(self, new_articulation):
        self._articulation = new_articulation
        self._joint_names = list(new_articulation.data.joint_names)
        self._build_hand_joint_mapping()
        self._initialize_arms()
        if self.contacts is not None:
            from dex_teleop.backends.bench2dex.contacts import FingerContacts
            self.contacts=FingerContacts()
        self.reset_input_state()

    def stop(self):
        self.receiver.set_haptics({'ready':False})
        self.receiver.stop()
        super().stop()


def create_bridge(articulation, config_path=None):
    return NetworkTeleopBridge(articulation,config_path)
