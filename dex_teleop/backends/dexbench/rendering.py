"""Own CPU snapshots from DexBench's three native RGB camera sensors."""
import numpy as np
from dex_teleop.dashboard.frame_publisher import FramePublisher
from .scene import CAMERAS


def numpy_copy(value):
    if hasattr(value, 'torch'):
        value = value.torch
    if hasattr(value, 'detach'):
        value = value.detach().cpu().numpy()
    elif hasattr(value, 'numpy'):
        value = value.numpy()
    return np.array(value, copy=True)


class Renderer:
    def __init__(self, env):
        self.env = env
        self.cameras = {name: env.scene.sensors[source] for name, source in CAMERAS.items()}

    def capture(self):
        images = {}
        for name, camera in self.cameras.items():
            pixels = numpy_copy(camera.data.output['rgb'])
            if pixels.ndim == 4:
                if pixels.shape[0] != 1:
                    raise ValueError('DexBench teleoperation requires exactly one environment')
                pixels = pixels[0]
            if pixels.ndim != 3 or pixels.shape[2] not in (3, 4) or pixels.dtype != np.uint8:
                raise RuntimeError(f'Invalid native RGB image from {name}: {pixels.shape}, {pixels.dtype}')
            images[name] = np.ascontiguousarray(pixels[:, :, :3])
        return images

    def warmup(self):
        # Render-only warmup does not advance Physics or the episode counter.
        for _ in range(3):
            self.env.sim.render()
            for camera in self.cameras.values():
                camera.update(0., force_recompute=True)
        return self.capture()

    def close(self):
        self.cameras.clear()
