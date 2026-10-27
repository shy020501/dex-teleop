"""Offscreen native MuJoCo cameras and latest-frame JPEG publication.

The simulation thread owns all mjData/GL access. Only immutable RGB arrays go
to the encoder; this provides a serial reference for recording/replay alignment.
"""
import io
import threading
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image


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


class FramePublisher:
    def __init__(self, latest, previews, quality=75):
        self.latest, self.previews, self.quality = latest, previews, quality
        self.condition = threading.Condition()
        self.pending = None
        self.closed = False
        self.generation = 0
        self.thread = threading.Thread(target=self._run, name='pomdar-jpeg', daemon=True)
        self.thread.start()

    def offer(self, cameras, step, **metadata):
        stamp = time.monotonic()
        # Arrays returned by Renderer are owned snapshots, never mutated again.
        self.previews.offer({key:SimpleNamespace(rgb=value) for key,value in cameras.items()}, step)
        with self.condition:
            self.pending = (cameras['cam_task'], stamp, dict(physics_step=int(step), **metadata), self.generation)
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed: return
                rgb, stamp, metadata, generation = self.pending
                self.pending = None
            output = io.BytesIO()
            Image.fromarray(rgb).save(output, format='JPEG', quality=self.quality)
            with self.condition:
                if not self.closed and generation == self.generation:
                    self.latest.publish(output.getvalue(), stamp, metadata)

    def reset(self):
        with self.condition:
            self.generation += 1
            self.pending = None
        self.previews.reset()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending = None
            self.condition.notify()
        self.thread.join(timeout=2)
