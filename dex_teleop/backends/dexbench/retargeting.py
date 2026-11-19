"""MANUS anatomical points into DexBench's native Wuji Hand 2 DexPilot solver.

The robot's retarget URDF defines the target frame. A palm basis, constructed
from the same MCP landmarks on both hands, removes the input device's global
orientation without reusing OpenXR-specific wrist extrinsics on MANUS data.
No joint angles are estimated by a glove-specific heuristic.
"""
import copy
from pathlib import Path
import threading
import time

import numpy as np

MANUS_TO_HAND21 = np.array([0,1,2,3,4,6,7,8,9,11,12,13,14,16,17,18,19,21,22,23,24])


def palm_basis(points):
    """Orthonormal columns: wrist-to-middle MCP, radial MCP direction, normal."""
    points = np.asarray(points, dtype=np.float64)
    if points.shape != (4, 3) or not np.isfinite(points).all():
        raise ValueError('Expected wrist/index/middle/ring MCP positions')
    forward = points[2]-points[0]
    radial = points[1]-points[3]
    length = np.linalg.norm(forward)
    if length < .005:
        raise ValueError('Degenerate MANUS palm length')
    forward /= length
    radial -= np.dot(radial, forward)*forward
    width = np.linalg.norm(radial)
    if width < .005:
        raise ValueError('Degenerate MANUS palm width')
    radial /= width
    return np.column_stack((forward, radial, np.cross(forward, radial)))


def manus_points(finger, robot_basis):
    if not finger.get('valid') or finger.get('representation') != 'manus25_global_m_xyzw':
        raise ValueError('Expected valid canonical MANUS25 tracking')
    points = np.asarray(finger['positions'], dtype=np.float64)
    if points.shape != (25, 3) or not np.isfinite(points).all():
        raise ValueError('Expected finite MANUS positions with shape (25, 3)')
    points = points[MANUS_TO_HAND21].copy()
    basis = palm_basis(points[[0, 5, 9, 13]])
    return ((points-points[0]) @ basis @ np.asarray(robot_basis).T).astype(np.float32)


class Wuji2Retargeter:
    """Keep upstream geometry, optimizer, projection distances, scaling and filter."""
    def __init__(self, side, joint_names, lower, upper, *, source_root=None, asset_root=None):
        import yaml
        from dex_retargeting.retargeting_config import RetargetingConfig
        from .paths import SOURCE_ROOT, ASSET_ROOT
        if side not in ('right', 'left'):
            raise ValueError('Unknown hand side')
        source_root = Path(source_root or SOURCE_ROOT)
        asset_root = Path(asset_root or ASSET_ROOT)
        config_path = source_root/f'source/dexbench_arena/dexbench_arena/embodiments/rby1_wuji2/data/dex_retargeting/wujihand2_{side}_dexpilot.yml'
        urdf_path = asset_root/f'embodiments/simready/rby1m_wujihand2_beta2/source/urdf/wujihand2_{side}_retarget.urdf'
        config = yaml.safe_load(config_path.read_text())['retargeting']
        config['urdf_path'] = str(urdf_path.resolve())
        self.sequence = RetargetingConfig.from_dict(config).build()
        self.names = list(joint_names)
        robot = self.sequence.optimizer.robot
        if set(self.names) != set(robot.dof_joint_names) or len(self.names) != 20:
            raise ValueError('Wuji2 retarget URDF does not match the robot hand joint names')
        self.to_output = np.array([robot.dof_joint_names.index(n) for n in self.names])
        self.lower = np.maximum(np.asarray(lower), robot.joint_limits[self.to_output, 0])
        self.upper = np.minimum(np.asarray(upper), robot.joint_limits[self.to_output, 1])
        if np.any(self.lower > self.upper):
            raise ValueError('Incompatible hand joint limits')
        robot.compute_forward_kinematics(np.zeros(robot.dof))
        landmarks = [config['wrist_link_name']]+[f'{side}_{finger}_proximal' for finger in
                                               ('index_finger', 'middle_finger', 'ring_finger')]
        self.robot_basis = palm_basis([robot.get_link_pose(robot.get_link_index(n))[:3,3] for n in landmarks])
        self.indices = self.sequence.optimizer.target_link_human_indices
        self.reset(np.zeros(20))

    def reset(self, target):
        full = np.zeros(self.sequence.optimizer.robot.dof, dtype=np.float32)
        full[self.to_output] = np.clip(target, self.lower, self.upper)
        self.sequence.set_qpos(full)
        if self.sequence.filter is not None:
            self.sequence.filter.reset()

    def retarget(self, finger):
        points = manus_points(finger, self.robot_basis)
        reference = points[self.indices[1]]-points[self.indices[0]]
        import torch
        with torch.enable_grad(), torch.inference_mode(False):
            result = np.asarray(self.sequence.retarget(reference))[self.to_output]
        if result.shape != (20,) or not np.isfinite(result).all():
            raise ValueError('DexPilot returned non-finite or incorrect hand targets')
        return np.clip(result, self.lower, self.upper)


class RetargetWorker:
    """One latest sample per hand; resets invalidate even in-flight results."""
    def __init__(self, retargeter, side):
        self.retargeter, self.side = retargeter, side
        self.lock = threading.Lock()
        self.wake, self.closed = threading.Event(), threading.Event()
        self.pending = self.result = self.seed = self.error = self.thread = None
        self.generation = 0

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name=f'dexbench-retarget-{self.side}', daemon=True)
            self.thread.start()

    def submit(self, finger, key, captured_at):
        with self.lock:
            self.pending = (copy.deepcopy(finger), key, captured_at, self.generation)
        self.wake.set()

    def reset(self, target):
        with self.lock:
            self.generation += 1
            self.seed = np.array(target, copy=True)
            self.pending = self.result = self.error = None
        self.wake.set()

    def latest(self):
        with self.lock:
            return None if self.result is None else dict(self.result, target=self.result['target'].copy())

    def _run(self):
        while not self.closed.is_set():
            self.wake.wait(.2)
            with self.lock:
                request, self.pending = self.pending, None
                seed, self.seed = self.seed, None
                generation = self.generation
                self.wake.clear()
            try:
                if seed is not None:
                    self.retargeter.reset(seed)
                if request is None:
                    continue
                finger, key, captured_at, requested_generation = request
                if generation != requested_generation:
                    continue
                started = time.monotonic()
                target = self.retargeter.retarget(finger)
                with self.lock:
                    if self.generation == generation:
                        self.result = dict(target=target, key=key, captured_at=captured_at,
                                           seconds=time.monotonic()-started)
                        self.error = None
            except Exception as exc:
                with self.lock:
                    if self.generation == generation:
                        self.result = None
                        self.error = str(exc)

    def stop(self):
        self.closed.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=3.)
