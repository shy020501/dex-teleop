"""Common Windows packets to native robosuite absolute-pose/hand actions."""
import copy,json,time,uuid
from pathlib import Path
import numpy as np
from scipy.spatial.transform import Rotation
from dex_teleop.core.transport import Receiver
from dex_teleop.core.session import describe_server
from dex_teleop.core.workspace_limits import validate_workspace,workspace_bounds,workspace_from_control
from dex_teleop.core.haptics import ContactHapticEncoder,FINGERS,scale_powers
from dex_teleop.paths import WORKSPACE
from .scene import arm_bindings
from .retargeting import retarget


def quaternion(matrix):
    q=Rotation.from_matrix(np.asarray(matrix).reshape(3,3)).as_quat()
    return np.r_[q[3],q[:3]]

def rotvec(q):return Rotation.from_quat(np.r_[q[1:],q[0]]).as_rotvec()


class Bridge:
    def __init__(self,env,config,receiver=None):
        self.env=env;self.model=env.sim.model._model;self.data=env.sim.data._data
        self.config=copy.deepcopy(config);self.control=self.config['control']
        self.receiver=receiver or Receiver(self.config['network'])
        self.epoch=1;self.enabled=True;self.source='hold';self.last_session=None
        self.started=False;self.reset_count=0;self.last_report=0.;self.error=None
        self.status_path=Path(self.control.get('status_path','logs/dexmimicgen-live/teleop-status.json'))
        if not self.status_path.is_absolute():self.status_path=WORKSPACE/self.status_path
        self.haptic_encoder=ContactHapticEncoder(copy.deepcopy(config['haptics']))
        self.haptic_enabled=bool(config['haptics'].get('enabled',True));self.haptic_gain=1.
        self.haptic_stream=uuid.uuid4().hex;self.haptic_seq=0;self.haptic_data={}
        self.hands={}
        for side,index,arm in arm_bindings(env):
            robot=env.robots[index];gripper=robot.gripper[arm]
            ids=np.array([env.sim.model.actuator_name2id(n) for n in gripper.actuators])
            expanded=np.asarray(gripper.format_action(np.arange(gripper.dof,dtype=float))).astype(int) if gripper.dof==6 else None
            lower=np.full(gripper.dof,-1.);upper=np.ones(gripper.dof)
            if expanded is not None:
                for channel in range(gripper.dof):
                    group=ids[expanded==channel]
                    joints=self.model.actuator_trnid[group,0]
                    lower[channel]=max(float(self.model.actuator_ctrlrange[group,0].max()),float(self.model.jnt_range[joints,0].max()))
                    upper[channel]=min(float(self.model.actuator_ctrlrange[group,1].min()),float(self.model.jnt_range[joints,1].min()))
            self.hands[side]=dict(index=index,arm=arm,robot=robot,gripper=gripper,site=int(robot.eef_site_id[arm]),
                ids=ids,expanded=expanded,lower=lower,upper=upper,command=np.clip(np.zeros(gripper.dof),lower,upper),
                home_event=0,reset_event=0,active=False,fresh=False)
        self.sides=list(self.hands)
        self._contact_map={}
        for side,hand in self.hands.items():
            if hand['gripper'].dof==1:
                # Native Panda metadata identifies finger1 as left_finger and
                # finger2 as right_finger; both names carry a "right" arm
                # prefix. Map the two pinch pads to thumb/index explicitly.
                for channel,key in enumerate(('left_finger','right_finger')):
                    for name in hand['gripper'].important_geoms[key]:
                        self._contact_map[int(self.model.geom(name).id)]=(side,channel)
                continue
            prefix=hand['gripper'].naming_prefix
            for i in range(self.model.ngeom):
                name=self.model.geom(i).name or ''
                if not name.startswith(prefix):continue
                for f,finger in enumerate(FINGERS):
                    if finger in name:self._contact_map[i]=(side,f);break
        self.robot_geoms={i for i in range(self.model.ngeom) if (self.model.geom(i).name or '').startswith(('robot','gripper'))}
        self._home();self._publish()
    def _position(self,h):return self.data.site_xpos[h['site']].copy()
    def _orientation(self,h):return quaternion(self.data.site_xmat[h['site']])
    def _hold_fingers(self,h):
        if h['expanded'] is None:
            # Parallel grippers interpret zero as hold, +/-1 as increments.
            return np.zeros(h['gripper'].dof)
        joints=self.model.actuator_trnid[h['ids'],0]
        actual=self.data.qpos[self.model.jnt_qposadr[joints]]
        values=[actual[h['expanded']==i].mean() for i in range(h['gripper'].dof)]
        return np.clip(values,h['lower'],h['upper'])
    def _home(self):
        for h in self.hands.values():
            h.update(home=self._position(h),home_q=self._orientation(h),target=self._position(h),q=self._orientation(h))
            lo,hi=workspace_bounds(h['home'],workspace_from_control(self.control));h.update(lo=np.array(lo),hi=np.array(hi))
    @property
    def action_spec(self):
        return dict(type='robosuite_absolute_eef_pose_and_hand_synergies',coordinate_frame='simulation_world_z_up_m',
                    position_unit='metre',rotation_representation='rotation_vector_xyz',rotation_unit='rad',
                    robot=[type(r.robot_model).__name__ for r in self.env.robots],
                    policy_action_dim=self.env.action_dim,actuator_names=[self.model.actuator(i).name for i in range(self.model.nu)])
    def policy_schema(self):
        """Describe the exact native policy vector, before controller expansion."""
        schema=[]
        for index,robot in enumerate(self.env.robots):
            controller=robot.composite_controller
            split=getattr(controller,'_whole_body_controller_action_split_indexes',controller._action_split_indexes)
            rows=[None]*robot.action_dim
            for side,h in self.hands.items():
                if h['index']!=index:continue
                start,end=split[h['arm']]
                names=[f'{side}.position.{axis}' for axis in 'xyz']+[f'{side}.rotation_vector.{axis}' for axis in 'xyz']
                assert end-start==len(names)
                rows[start:end]=[dict(name=name,unit='m' if i<3 else 'rad') for i,name in enumerate(names)]
                start,end=split[h['arm']+'_gripper']
                channels=['pinky_bend','ring_bend','middle_bend','index_bend','thumb_bend','thumb_opposition'] if h['gripper'].dof==6 else ['gripper_increment']
                rows[start:end]=[dict(name=side+'.'+name,unit='rad' if h['gripper'].dof==6 else 'normalized_increment') for name in channels]
            if any(row is None for row in rows):raise ValueError('Undescribed native policy action channel')
            schema.extend(rows)
        return schema
    def start(self):self.receiver.start();self.started=True
    def stop(self):
        self.enabled=False;self._clear_haptics();self.receiver.stop();self.started=False
    def _packet(self):
        sample=self.receiver.snapshot() if self.enabled else None
        if sample is None or sample[0].get('epoch')!=self.epoch:return None,None
        return sample
    def update(self,dt):
        now=time.monotonic();packet,received=self._packet();self.source='hold'
        if packet is not None and packet['session']!=self.last_session:
            self.last_session=packet['session']
            for h in self.hands.values():h['home_event']=h['reset_event']=0
        for side,h in self.hands.items():
            hand=packet['hands'].get(side) if packet else None
            was_fresh=h['fresh']
            h['active']=bool(hand and hand['active']);h['fresh']=False
            if not h['active']:
                if was_fresh:h['command']=self._hold_fingers(h)
                continue
            pose=hand['pose'];home=int(hand.get('home',0))>h['home_event']
            if home:h['home_event']=hand['home']
            if pose.get('position_active',True) or home:
                desired=h['home'] if home else np.clip(pose['position_xyz'],h['lo'],h['hi'])
                delta=desired-h['target'];distance=np.linalg.norm(delta)
                h['target']+=delta*min(1.,self.control['max_position_speed_m_s']*dt/max(distance,1e-12))
                desired_q=np.asarray(pose['orientation_wxyz']) if pose['source']=='vive' and not home else h['home_q'].copy()
                current_rotation=Rotation.from_quat(np.r_[h['q'][1:],h['q'][0]])
                desired_rotation=Rotation.from_quat(np.r_[desired_q[1:],desired_q[0]])
                rotation_delta=(desired_rotation*current_rotation.inv()).as_rotvec()
                angle=np.linalg.norm(rotation_delta)
                rotation_delta*=min(1.,self.control['max_orientation_speed_rad_s']*dt/max(angle,1e-12))
                h['q']=quaternion((Rotation.from_rotvec(rotation_delta)*current_rotation).as_matrix())
                self.source=pose['source']
            finger=hand.get('finger',{})
            fresh=bool(finger.get('valid') and now-received+finger.get('age_ms',1e9)/1000<=self.control['tracking_timeout_s'])
            if fresh:
                try:
                    target=retarget(finger,h['gripper'].dof,h['lower'],h['upper'])
                    h['command']+=np.clip(target-h['command'],-dt*self.control['max_finger_joint_speed_rad_s'],dt*self.control['max_finger_joint_speed_rad_s'])
                    h['fresh']=True;self.source=finger.get('source','manus');self.error=None
                except ValueError as exc:self.error=str(exc)
            if was_fresh and not h['fresh']:h['command']=self._hold_fingers(h)
        dictionaries=[{} for _ in self.env.robots]
        for h in self.hands.values():
            dictionaries[h['index']][h['arm']]=np.r_[h['target'],rotvec(h['q'])]
            dictionaries[h['index']][h['arm']+'_gripper']=h['command'].copy()
        actions=[robot.composite_controller.create_action_vector(values) for robot,values in zip(self.env.robots,dictionaries)]
        self.action=np.concatenate(actions);self._publish();return self.action.copy()
    def after_physics_step(self,dt):
        import mujoco
        forces={side:np.zeros(5) for side in ('right','left')}
        force=np.zeros(6)
        for i in range(self.data.ncon):
            contact=self.data.contact[i]
            for own,other in ((contact.geom1,contact.geom2),(contact.geom2,contact.geom1)):
                if own in self._contact_map and other not in self.robot_geoms:
                    side,f=self._contact_map[own];mujoco.mj_contactForce(self.model,self.data,i,force)
                    forces[side][f]+=float(np.linalg.norm(force[:3]))
        now=time.monotonic();packet,received=self._packet()
        active={side:bool(self.haptic_enabled and self.enabled and packet and side in self.hands and self.hands[side]['fresh']
                         and now-received<=self.control['tracking_timeout_s']) for side in ('right','left')}
        powers=self.haptic_encoder.update(forces,active,dt,now);ceiling=self.haptic_encoder.cfg['max_power']
        self.haptic_seq+=1
        self.haptic_data=dict(ready=True,stream_id=self.haptic_stream,epoch=self.epoch,seq=self.haptic_seq,
            finger_order=list(FINGERS),forces_n={k:v.tolist() for k,v in forces.items()},
            powers=scale_powers(powers,self.haptic_gain,ceiling),active=active,max_power=ceiling)
        self.receiver.set_haptics(self.haptic_data)
    def _clear_haptics(self):
        self.haptic_encoder.reset();self.haptic_data=dict(ready=False,powers=dict(right=[0.]*5,left=[0.]*5));self.receiver.set_haptics(self.haptic_data)
    def set_enabled(self,value):
        if self.enabled!=bool(value):self.enabled=bool(value);self.reset_input_state()
    def reset_input_state(self):
        self.epoch+=1;self.source='hold'
        with self.receiver.lock:self.receiver.latest=None
        for h in self.hands.values():h.update(target=self._position(h),q=self._orientation(h),command=self._hold_fingers(h),active=False,fresh=False)
        self._clear_haptics();self._publish()
    def reset(self):self.reset_count+=1;self._home();self.reset_input_state()
    def consume_environment_reset(self):
        packet,_=self._packet()
        if packet:
            for side,h in self.hands.items():
                value=packet['hands'].get(side,{})
                if int(value.get('reset',0))>h['reset_event']:
                    h['reset_event']=value['reset'];return True
        return False
    def workspace_status(self):return {side:dict(home_position_xyz=h['home'].tolist(),workspace_min_xyz=h['lo'].tolist(),workspace_max_xyz=h['hi'].tolist()) for side,h in self.hands.items()}
    def apply_workspace(self,value):
        settings=validate_workspace(value)
        for h in self.hands.values():
            lo,hi=workspace_bounds(h['home'],settings);h.update(lo=np.array(lo),hi=np.array(hi))
        self.control.update(workspace_lower_radius_xyz=[-x for x in settings['min_offset_xyz']],workspace_radius_xyz=settings['max_offset_xyz'])
        self.reset_input_state();return True
    def _publish(self):
        hands={}
        for side,h in self.hands.items():
            hands[side]=dict(home_position_xyz=h['home'].tolist(),home_orientation_wxyz=h['home_q'].tolist(),
                current_position_xyz=self._position(h).tolist(),current_orientation_wxyz=self._orientation(h).tolist(),
                target_position_xyz=h['target'].tolist(),workspace_min_xyz=h['lo'].tolist(),workspace_max_xyz=h['hi'].tolist(),
                arm_active=h['active'],finger_active=h['fresh'],home_event_ack=h['home_event'],reset_event_ack=h['reset_event'],
                finger_joint_targets=h['command'].tolist(),arm_joint_positions=[],finger_joint_positions=[])
        self._status=dict(ready=self.enabled,server_id=self.receiver.server_id,epoch=self.epoch,hands=hands,
            teleop=describe_server(self.control,self.receiver.config,haptics=True),pose_monotonic_s=time.monotonic(),
            coordinate_frame='simulation_world_z_up_m',simulator='dexmimicgen',action_spec=self.action_spec,
            haptics=self.haptic_data,retargeting_error=self.error,network=self.receiver.stats())
        self.receiver.set_status(self._status)
        now=time.monotonic()
        if now-self.last_report>=.5:
            self.last_report=now
            self.status_path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.status_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self._status,indent=2)+'\n')
            temporary.replace(self.status_path)
    def status(self):return copy.deepcopy(self._status)
