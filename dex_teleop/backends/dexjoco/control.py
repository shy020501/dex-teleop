"""Common device input to official absolute TCP/Allegro policy actions."""
import copy
import json
from pathlib import Path
import time
import uuid
import numpy as np
from scipy.spatial.transform import Rotation,Slerp

from dex_teleop.core.transport import Receiver
from dex_teleop.core.session import describe_server
from dex_teleop.core.haptics import ContactHapticEncoder,FINGERS
from dex_teleop.core.workspace_limits import validate_workspace,workspace_bounds,workspace_from_control
from .contacts import FingerContacts
from .retargeting import GeoRTRetargeter

def limited_rotation(a,b,angle):
    a=np.asarray(a);b=np.asarray(b);b=b/np.linalg.norm(b)
    distance=2*np.arccos(np.clip(abs(np.dot(a,b)),-1,1))
    if distance<=angle:return b.copy()
    r=Rotation.from_quat(np.array([a,b])[:,[1,2,3,0]])
    return Slerp([0.,1.],r)([angle/max(distance,1e-12)]).as_quat()[0][[3,0,1,2]]

class Bridge:
    def __init__(self,scene,config,receiver=None):
        self.scene=scene;self.config=copy.deepcopy(config);self.control=self.config['control']
        self.receiver=receiver or Receiver(config['network']);self.sides=scene.sides
        self.epoch=1;self.enabled=True;self.started=False;self.last_session=None
        self.events={side:dict(home=0,reset=0) for side in self.sides}
        self.state={};self.retargeters={};self.last_seq={};self.active={};self.errors={}
        for side,part in scene.parts.items():
            p,q=scene.pose(side);lo,hi=workspace_bounds(p,workspace_from_control(self.control))
            self.state[side]=dict(home=p.copy(),home_q=q.copy(),p=p.copy(),q=q.copy(),lo=np.array(lo),hi=np.array(hi),
                fingers=np.clip(scene.data.qpos[part['qadr']],part['lower'],part['upper']).copy())
            self.retargeters[side]=GeoRTRetargeter(side,part['lower'],part['upper'])
        self.contacts=FingerContacts(scene);self.haptic_encoder=ContactHapticEncoder(config['haptics'])
        self.haptic_enabled=bool(config['haptics'].get('enabled',True));self.haptic_seq=0;self.stream=uuid.uuid4().hex
        self.haptic_data=dict(ready=False,powers=dict(right=[0.]*5,left=[0.]*5))
        self.source='hold';self.last_report=0.;self.environment_reset_count=0;self._publish()
    @property
    def action_spec(self):
        return dict(type='absolute_tcp_pose_and_allegro_joint_targets',action_dim=23*len(self.sides),
                    layout='right_pose7,left_pose7,right_hand16,left_hand16' if len(self.sides)==2 else 'pose7,hand16',
                    position_unit='metre',joint_unit='radian',quaternion_order='wxyz',
                    coordinate_frame='simulation_world_z_up_m',robot='panda_allegro',sides=self.sides)
    def start(self):self.receiver.start();self.started=True
    def stop(self):
        self.set_enabled(False)
        if self.started:self.receiver.stop();self.started=False
    def _input(self):
        sample=self.receiver.snapshot() if self.enabled else None
        if sample and sample[0].get('epoch')!=self.epoch:return None
        return sample
    def update(self,dt):
        now=time.monotonic();sample=self._input();sources=set();self.source='hold';self.active={s:False for s in self.sides}
        if sample and sample[0]['session']!=self.last_session:
            self.last_session=sample[0]['session'];self.last_seq={}
            self.events={s:dict(home=0,reset=0) for s in self.sides}
        for side in self.sides:
            s=self.state[side];part=self.scene.parts[side];hand=sample[0]['hands'].get(side) if sample else None
            if not hand or not hand['active']:
                if 'finger_target' in s:
                    s.pop('finger_target');s['fingers']=np.clip(self.scene.data.qpos[part['qadr']],part['lower'],part['upper']).copy()
                continue
            pose=hand['pose'];home=hand.get('home',0)>self.events[side]['home']
            if home:self.events[side]['home']=hand['home']
            if pose.get('position_active',True) or home:
                target=s['home'] if home else np.clip(pose['position_xyz'],s['lo'],s['hi'])
                delta=target-s['p'];s['p']+=delta*min(1.,self.control['max_position_speed_m_s']*dt/max(np.linalg.norm(delta),1e-12))
                q=pose['orientation_wxyz'] if pose['source']=='vive' and not home else s['home_q']
                s['q']=limited_rotation(s['q'],q,self.control.get('max_orientation_speed_rad_s',.8)*dt)
                sources.add(pose['source'])
            finger=hand.get('finger',{});fresh=bool(finger.get('valid') and
                now-sample[1]+finger.get('age_ms',1e9)/1000<=self.control['tracking_timeout_s'])
            self.active[side]=fresh
            if fresh:
                try:
                    if self.last_seq.get(side)!=sample[0]['seq']:
                        s['finger_target']=self.retargeters[side].retarget(finger);self.last_seq[side]=sample[0]['seq']
                    target=s['finger_target'];bound=self.control['max_finger_joint_speed_rad_s']*dt
                    s['fingers']+=np.clip(target-s['fingers'],-bound,bound);self.errors.pop(side,None)
                    sources.add(finger.get('source','manus'))
                except (ValueError,RuntimeError) as exc:self.errors[side]=str(exc);self.active[side]=False
            elif 'finger_target' in s:
                s.pop('finger_target');s['fingers']=np.clip(self.scene.data.qpos[part['qadr']],part['lower'],part['upper']).copy()
        self.source='+'.join(sorted(sources)) or 'hold'
        self._publish()
        poses=[np.r_[self.state[s]['p'],self.state[s]['q']] for s in self.sides]
        fingers=[self.state[s]['fingers'] for s in self.sides]
        return np.concatenate(poses+fingers)
    def after_physics_step(self,dt):
        active=dict(right=False,left=False)
        for s in self.sides:active[s]=bool(self.haptic_enabled and self.enabled and self.active.get(s))
        forces=self.contacts.read();powers=self.haptic_encoder.update(forces,active,dt)
        self.haptic_seq+=1
        self.haptic_data=dict(ready=True,stream_id=self.stream,epoch=self.epoch,seq=self.haptic_seq,
            finger_order=list(FINGERS),forces_n=forces,powers=powers,active=active,max_power=self.haptic_encoder.cfg['max_power'])
        self.receiver.set_haptics(self.haptic_data)
    def reset_input_state(self):
        self.epoch+=1;self.active={};self.last_seq={};self.source='hold'
        with self.receiver.lock:self.receiver.latest=None
        for side,s in self.state.items():
            s['p'],s['q']=self.scene.pose(side);part=self.scene.parts[side]
            s['fingers']=np.clip(self.scene.data.qpos[part['qadr']],part['lower'],part['upper']).copy();s.pop('finger_target',None)
        self.haptic_encoder.reset();self.receiver.set_haptics(dict(ready=False,powers=dict(right=[0.]*5,left=[0.]*5)))
        self._publish()
    def set_enabled(self,enabled):
        if bool(enabled)!=self.enabled:self.enabled=bool(enabled);self.reset_input_state()
    def reset(self):self.environment_reset_count+=1;self.reset_input_state()
    def consume_environment_reset(self):
        sample=self._input()
        if sample:
            for side,hand in sample[0]['hands'].items():
                if side in self.events and hand['reset']>self.events[side]['reset']:
                    self.events[side]['reset']=hand['reset'];return True
        return False
    def workspace_status(self):
        return {side:dict(home_position_xyz=s['home'].tolist(),workspace_min_xyz=s['lo'].tolist(),workspace_max_xyz=s['hi'].tolist())
                for side,s in self.state.items()}
    def apply_workspace(self,settings):
        settings=validate_workspace(settings)
        for s in self.state.values():
            lo,hi=workspace_bounds(s['home'],settings);s['lo'],s['hi']=np.array(lo),np.array(hi)
        self.reset_input_state()
    def status(self):return copy.deepcopy(self._status)
    def _publish(self):
        now=time.monotonic();hands={}
        for side,s in self.state.items():
            p,q=self.scene.pose(side);part=self.scene.parts[side]
            hands[side]=dict(home_position_xyz=s['home'].tolist(),home_orientation_wxyz=s['home_q'].tolist(),
                target_position_xyz=s['p'].tolist(),current_position_xyz=p.tolist(),current_orientation_wxyz=q.tolist(),
                workspace_min_xyz=s['lo'].tolist(),workspace_max_xyz=s['hi'].tolist(),
                finger_joint_positions=self.scene.data.qpos[part['qadr']].tolist(),finger_joint_targets=s['fingers'].tolist(),
                home_event_ack=self.events[side]['home'],reset_event_ack=self.events[side]['reset'],
                arm_active=self.enabled and self.source!='hold',finger_active=self.active.get(side,False))
        self._status=dict(ready=self.enabled,epoch=self.epoch,server_id=self.receiver.server_id,simulator='dexjoco',
            teleop=describe_server(self.control,self.receiver.config,haptics=True),hands=hands,
            pose_monotonic_s=now,coordinate_frame='simulation_world_z_up_m',orientation_mode='fixed_home_or_pose_provider',
            network=self.receiver.stats(),haptics=self.haptic_data,retargeting_error=self.errors,
            action_spec=self.action_spec,environment_reset_count=self.environment_reset_count)
        self.receiver.set_status(self._status)
        if now-self.last_report>.5:
            path=Path(self.control['status_path']);path.parent.mkdir(parents=True,exist_ok=True)
            tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(self._status,indent=2)+'\n');tmp.replace(path);self.last_report=now
