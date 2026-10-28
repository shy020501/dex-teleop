"""Offscreen native MuJoCo cameras and latest-frame JPEG publication.

The simulation thread owns all mjData/GL access. Only immutable RGB arrays go
to the encoder; this provides a serial reference for recording/replay alignment.
"""
import numpy as np
from dex_teleop.dashboard.frame_publisher import FramePublisher


class Renderer:
    def __init__(self, model, config):
        import mujoco
        self.model = model
        self.visual_data = mujoco.MjData(model)
        self.state_spec = mujoco.mjtState.mjSTATE_INTEGRATION
        self.state_buffer = np.empty(mujoco.mj_stateSize(model, self.state_spec))
        self.config = dict(config)
        sizes = dict(cam_task=(config.get('main_width', 1280), config.get('main_height', 720)),
                     cam_wrist_right=(config.get('wrist_width', 320), config.get('wrist_height', 240)))
        self.renderers = {}
        for camera, (width, height) in sizes.items():
            if not (16 <= width <= 1920 and 16 <= height <= 1080):
                raise ValueError('Camera resolution must be between 16x16 and 1920x1080')
            if mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, camera) < 0:
                raise ValueError(f'Missing POMDAR camera: {camera}')
            model.vis.global_.offwidth = max(model.vis.global_.offwidth, width)
            model.vis.global_.offheight = max(model.vis.global_.offheight, height)
        try:
            for camera, (width, height) in sizes.items():
                self.renderers[camera] = mujoco.Renderer(model, height=height, width=width)
        except BaseException:
            self.close()
            raise

    def capture(self, data):
        import mujoco
        # mj_step integrates qpos after computing derived poses. Compute fresh
        # post-step camera/body poses on an independent state, without changing
        # the live solver warmstart or the next authoritative physics step.
        mujoco.mj_getState(self.model, data, self.state_buffer, self.state_spec)
        mujoco.mj_setState(self.model, self.visual_data, self.state_buffer, self.state_spec)
        mujoco.mj_forward(self.model, self.visual_data)
        frames = {}
        for camera, renderer in self.renderers.items():
            renderer.update_scene(self.visual_data, camera=camera)
            frames[camera] = np.array(renderer.render(), copy=True, order='C')
        return frames

    def close(self):
        for renderer in self.renderers.values():
            renderer.close()
        self.renderers.clear()
