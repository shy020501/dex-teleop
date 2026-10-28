"""Native MuJoCo rendering on a separate mjData, preserving live integration."""
import numpy as np
from dex_teleop.dashboard.frame_publisher import FramePublisher

class Renderer:
    def __init__(self,model,config,mapping):
        import mujoco
        self.model=model;self.config=dict(config);self.mapping=dict(mapping)
        # Robosuite separates collision meshes (group 0) from visible meshes
        # (group 1). MuJoCo's default draws both, causing colored z-fighting.
        self.options=mujoco.MjvOption()
        self.options.geomgroup[0]=0
        self.options.geomgroup[1]=1
        self.visual=mujoco.MjData(model)
        self.spec=mujoco.mjtState.mjSTATE_INTEGRATION
        self.state=np.empty(mujoco.mj_stateSize(model,self.spec))
        self.renderers={}
        for canonical,native in self.mapping.items():
            prefix='main' if canonical=='cam_task' else 'wrist'
            width=int(config.get(prefix+'_width',1280 if prefix=='main' else 320))
            height=int(config.get(prefix+'_height',720 if prefix=='main' else 240))
            if not 16<=width<=1920 or not 16<=height<=1080:raise ValueError('Invalid camera resolution')
            model.vis.global_.offwidth=max(model.vis.global_.offwidth,width)
            model.vis.global_.offheight=max(model.vis.global_.offheight,height)
            self.renderers[canonical]=mujoco.Renderer(model,height,width)
    def capture(self,data):
        import mujoco
        mujoco.mj_getState(self.model,data,self.state,self.spec)
        mujoco.mj_setState(self.model,self.visual,self.state,self.spec)
        mujoco.mj_forward(self.model,self.visual)
        result={}
        for name,renderer in self.renderers.items():
            renderer.update_scene(self.visual,camera=self.mapping[name],scene_option=self.options)
            result[name]=np.array(renderer.render(),copy=True)
        return result
    def close(self):
        for renderer in self.renderers.values():renderer.close()
        self.renderers.clear()
