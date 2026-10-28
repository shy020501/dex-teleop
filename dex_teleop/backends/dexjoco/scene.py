"""Run official DexJoCo environments without their UDP or redundant viewers."""
import copy
import importlib
import json
import random
import sys
import numpy as np

from .task_info import task_info
from .paths import SOURCE_REVISION

# Task code changes these model fields (table geometry, visual feedback, cameras).
MODEL_FIELDS=('body_pos','body_quat','body_mass','body_inertia','geom_pos','geom_quat','geom_size',
              'geom_rgba','geom_matid','geom_friction','geom_contype','geom_conaffinity',
              'geom_group','dof_frictionloss','jnt_stiffness','mat_rgba','cam_pos','cam_quat','light_pos','light_dir')

def copy_data(model,source,target):
    import mujoco
    spec=mujoco.mjtState.mjSTATE_INTEGRATION
    state=np.empty(mujoco.mj_stateSize(model,spec))
    mujoco.mj_getState(model,source,state,spec);mujoco.mj_setState(model,target,state,spec)

class NullRenderer:
    """Upstream observations still run; actual camera capture belongs to Renderer."""
    def __init__(self,*args,**kwargs):pass
    def render(self,*args,**kwargs):return np.zeros((1,1,3),np.uint8)
    def close(self):pass

class PhysicsObserver:
    def __init__(self,scene):self.scene=scene
    def __getattr__(self,name):
        import mujoco
        return getattr(mujoco,name)
    def mj_step(self,model,data,*args,**kwargs):
        import mujoco
        if model is self.scene.model and data is self.scene.data:
            if self.scene.before_step:self.scene.before_step()
            mujoco.mj_step(model,data,*args,**kwargs)
            self.scene.physics_step+=1
            if self.scene.after_step:self.scene.after_step()
        else:mujoco.mj_step(model,data,*args,**kwargs)

def encode(value):
    if isinstance(value,np.ndarray):
        if value.dtype.kind not in 'biuf':raise TypeError()
        return {'array':value.tolist(),'dtype':str(value.dtype)}
    if isinstance(value,np.generic):return encode(value.item())
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,float):
        if not np.isfinite(value):raise TypeError()
        return value
    if isinstance(value,tuple):return {'tuple':[encode(v) for v in value]}
    if isinstance(value,list):return [encode(v) for v in value]
    if isinstance(value,dict):return {str(k):encode(v) for k,v in value.items()}
    raise TypeError()

def decode(value):
    if isinstance(value,dict):
        if set(value)=={'array','dtype'}:return np.asarray(value['array'],dtype=value['dtype'])
        if set(value)=={'tuple'}:return tuple(decode(v) for v in value['tuple'])
        return {k:decode(v) for k,v in value.items()}
    if isinstance(value,list):return [decode(v) for v in value]
    return value

class Scene:
    def __init__(self,task,settings):
        try:self._initialize(task,settings)
        except BaseException:
            if hasattr(self,'wrapper'):self.wrapper.close()
            if hasattr(self,'native_module'):
                self.native_module.MujocoRenderer=self.original_renderer
                if hasattr(self,'observer') and self.native_module.mujoco is self.observer:
                    self.native_module.mujoco=self.original_mujoco
            raise

    def _initialize(self,task,settings):
        import mujoco
        from dexjoco.tasks.mappings import CONFIG_MAPPING
        self.task=task;self.sides=task_info(task)['sides'];self.settings=dict(settings)
        self.before_step=self.after_step=None;self.physics_step=0
        # Imports are from the pinned source. Only this runtime process owns them.
        # Several native task resets use process RNGs rather than env._random.
        random.seed(int(settings.get('seed',0)));np.random.seed(int(settings.get('seed',0)))
        config=CONFIG_MAPPING[task]()
        module=importlib.import_module(config.__class__.__module__)
        raw_class=next(v for v in vars(module).values() if isinstance(v,type) and v.__name__.startswith('Panda') and v.__name__.endswith('Env'))
        native_module=sys.modules[raw_class.__module__]
        self.native_module = native_module
        self.original_mujoco = native_module.mujoco
        self.original_renderer = native_module.MujocoRenderer
        native_module.MujocoRenderer=NullRenderer
        try:
            self.wrapper=config.get_environment(policy_mode=True,render_mode='rgb_array',randomize=False,
                physics_dt=float(settings.get('physics_dt',.002)),control_dt=1/float(settings.get('control_hz',50)),
                seed=int(settings.get('seed',0)))
        except BaseException:
            native_module.MujocoRenderer=self.original_renderer
            raise
        self.env=self.wrapper.unwrapped;self.model=self.env.model;self.data=self.env.data
        self.env.hz=1e9  # Pacing is owned by the outer dashboard loop.
        self.wrapper.reset(seed=int(settings.get('seed',0)))
        self.n_substeps=int(self.env._n_substeps)
        wanted=1/float(settings.get('control_hz',50))
        if not np.isclose(self.n_substeps*self.model.opt.timestep,wanted,atol=1e-12):
            raise ValueError('Control rate must divide the native Physics rate exactly')
        self.parts={}
        for side in self.sides:
            suffix='_'+side if len(self.sides)==2 else ''
            site=self.model.site('attachment_site'+suffix).id
            joint_names=[f'{finger}j{i}{suffix}' for finger in ('ff','mf','rf','th') for i in range(4)]
            actuator_names=[f'{finger}a{i}{suffix}' for finger in ('ff','mf','rf','th') for i in range(4)]
            jids=np.array([self.model.joint(name).id for name in joint_names])
            aids=np.array([self.model.actuator(name).id for name in actuator_names])
            qadr=self.model.jnt_qposadr[jids].copy()
            self.parts[side]=dict(site=site,joints=jids,actuators=aids,qadr=qadr,
                lower=self.model.actuator_ctrlrange[aids,0].copy(),upper=self.model.actuator_ctrlrange[aids,1].copy())
        self.cameras={'cam_task':int(self.env._front_camera_id)}
        for side in self.sides:
            name='handcam_rgb_'+side if len(self.sides)==2 else 'handcam_rgb'
            self.cameras['cam_wrist_'+side]=self.model.camera(name).id
        self.observer=PhysicsObserver(self)
        native_module.mujoco=self.observer
        self._pose_data=mujoco.MjData(self.model)
        self.initial=self.snapshot()

    def native_state(self):
        attrs={}
        for key,value in vars(self.env).items():
            try:attrs[key]=encode(value)
            except (TypeError,ValueError):pass
        wrappers=[];wrapper=self.wrapper
        while wrapper is not self.env:
            state={}
            for key,value in vars(wrapper).items():
                try:state[key]=encode(value)
                except (TypeError,ValueError):pass
            wrappers.append(state);wrapper=wrapper.env
        return dict(attributes=attrs,wrappers=wrappers,random=encode(random.getstate()),numpy=encode(np.random.get_state()),
                    env_random=encode(self.env._random.get_state()))

    def restore_native(self,state):
        for key,value in state['attributes'].items():setattr(self.env,key,decode(value))
        wrapper=self.wrapper
        for attrs in state.get('wrappers',[]):
            for key,value in attrs.items():setattr(wrapper,key,decode(value))
            wrapper=wrapper.env
        random.setstate(decode(state['random']));np.random.set_state(decode(state['numpy']))
        self.env._random.set_state(decode(state['env_random']))

    def snapshot(self):
        import mujoco
        spec=mujoco.mjtState.mjSTATE_INTEGRATION
        integration=np.empty(mujoco.mj_stateSize(self.model,spec))
        mujoco.mj_getState(self.model,self.data,integration,spec)
        return dict(integration=integration,model={key:getattr(self.model,key).copy() for key in MODEL_FIELDS},
                    native=self.native_state(),step=self.physics_step)

    def restore(self,snapshot):
        import mujoco
        for key,value in snapshot['model'].items():getattr(self.model,key)[:]=value
        mujoco.mj_setConst(self.model,self.data)
        mujoco.mj_setState(self.model,self.data,snapshot['integration'],mujoco.mjtState.mjSTATE_INTEGRATION)
        mujoco.mj_forward(self.model,self.data)
        mujoco.mj_setState(self.model,self.data,snapshot['integration'],mujoco.mjtState.mjSTATE_INTEGRATION)
        self.restore_native(snapshot['native']);self.physics_step=int(snapshot['step'])

    def reset(self):self.restore(copy.deepcopy(self.initial))

    def pose(self,side):
        import mujoco
        part=self.parts[side];q=np.empty(4)
        # Use authoritative post-step transforms on a private copy.
        data=self._pose_data
        copy_data(self.model,self.data,data);mujoco.mj_forward(self.model,data)
        mujoco.mju_mat2Quat(q,data.site_xmat[part['site']])
        return data.site_xpos[part['site']].copy(),q

    def step(self,action,substeps=None):
        previous=self.env._n_substeps
        if substeps is not None:
            if not 1<=int(substeps)<=self.n_substeps:raise ValueError('Invalid native substep count')
            self.env._n_substeps=int(substeps)
        try:result=self.wrapper.step(np.asarray(action,dtype=np.float64))
        finally:self.env._n_substeps=previous
        info=result[-1]
        return dict(success=bool(info.get('succeed',False)),terminated=bool(result[2]),truncated=bool(result[3]))

    def close(self):
        self.wrapper.close()
        if self.native_module.mujoco is self.observer:
            self.native_module.mujoco=self.original_mujoco
        if self.native_module.MujocoRenderer is NullRenderer:
            self.native_module.MujocoRenderer=self.original_renderer
        self.before_step=self.after_step=None
