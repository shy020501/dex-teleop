"""Render camera images from an independent MuJoCo state copy."""
import numpy as np
from dex_teleop.dashboard.frame_publisher import FramePublisher
from .scene import copy_data

class Renderer:
    def __init__(self,scene,config):
        import mujoco
        self.scene=scene;self.model=scene.model;self.data=mujoco.MjData(scene.model)
        self.renderers={}
        camera=config.get('main_camera','operator')
        if camera=='operator':camera='back' if len(scene.sides)==2 else 'right'
        if camera!='native':scene.cameras['cam_task']=self.model.camera(camera).id
        try:
            for name in scene.cameras:
                main=name=='cam_task';width=int(config.get('main_width' if main else 'wrist_width',1280 if main else 320))
                height=int(config.get('main_height' if main else 'wrist_height',720 if main else 240))
                if not 16<=width<=1920 or not 16<=height<=1080:raise ValueError('Invalid camera dimensions')
                self.model.vis.global_.offwidth=max(self.model.vis.global_.offwidth,width)
                self.model.vis.global_.offheight=max(self.model.vis.global_.offheight,height)
                self.renderers[name]=mujoco.Renderer(self.model,height=height,width=width)
        except BaseException:
            self.close();raise
    def capture(self,data=None):
        import mujoco
        copy_data(self.model,self.scene.data if data is None else data,self.data)
        mujoco.mj_forward(self.model,self.data)
        frames={}
        for name,renderer in self.renderers.items():
            renderer.update_scene(self.data,camera=self.scene.cameras[name])
            frames[name]=np.asarray(renderer.render()).copy()
        return frames
    def close(self):
        for renderer in self.renderers.values():renderer.close()
        self.renderers.clear()
