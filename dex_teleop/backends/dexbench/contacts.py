"""Read actual Newton fingertip normal forces; unsupported solvers return no force."""
import numpy as np

FINGERS = ('thumb', 'index_finger', 'middle_finger', 'ring_finger', 'pinky')


def as_numpy(value):
    if hasattr(value, 'torch'):
        value = value.torch
    if hasattr(value, 'detach'):
        value = value.detach().cpu().numpy()
    return np.asarray(value)


class FingerContacts:
    def __init__(self, env):
        self.sensor = env.scene.sensors.get('fingertip_contact')
        self.reason = None if self.sensor is not None else 'This task/solver has no fingertip contact sensor'
        self.indices = None

    @property
    def available(self):
        return self.sensor is not None

    def read(self):
        if not self.available:
            raise RuntimeError(self.reason)
        names = list(self.sensor.sensor_names)
        if self.indices is None:
            expected = {side:[[f'{side}_{finger}_tip_sensor_frame', f'{side}_{finger}_distal']
                              for finger in FINGERS] for side in ('right','left')}
            missing = [name for groups in expected.values() for group in groups for name in group if name not in names]
            if missing:
                raise RuntimeError(f'Newton fingertip rows missing: {missing}')
            self.indices = {side:[[names.index(name) for name in group] for group in groups]
                            for side,groups in expected.items()}
        # Native DexBench observations use normal force magnitude (N), not motor
        # torque or error between desired and measured finger joint positions.
        value = as_numpy(self.sensor.data.net_normal_forces_w)
        if value.shape != (1,len(names),3) or not np.isfinite(value).all():
            raise ValueError('Invalid Newton fingertip force sample')
        return {side:[float(np.linalg.norm(value[0,ids],axis=1).sum()) for ids in groups]
                for side,groups in self.indices.items()}
