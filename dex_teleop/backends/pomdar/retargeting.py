"""MANUS canonical skeleton to the pinned POMDAR ORCA retargeter.

This module has no Isaac or SDK imports. Optimization owns its own thread and
consumes one latest sample; physics never waits for it or queues old commands.
"""
import copy
import importlib.util
import math
import os
from pathlib import Path
import sys
import threading
import time

import numpy as np

from dex_teleop.paths import WORKSPACE


MANUS_TO_HAND21 = np.array([0, 1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13, 14,
                            16, 17, 18, 19, 21, 22, 23, 24])


def manus_to_mano(finger, *, palm_length_m=.09):
    """Return MANO22 metres, using the official webcam virtual-forearm rule.

    Canonical MANUS order is defined by joint meanings, not SDK raw indices.
    POMDAR normalizes the local hand orientation itself. We intentionally do
    not apply Rokoko-specific chest-axis flips or MANUS wrist orientation.
    The forearm point is a constructed direction, not a tracked forearm.
    """
    if not finger.get('valid') or finger.get('representation') != 'manus25_global_m_xyzw':
        raise ValueError('Expected valid canonical MANUS25 tracking')
    points = np.asarray(finger['positions'], dtype=np.float32)
    if points.shape != (25, 3) or not np.isfinite(points).all():
        raise ValueError('Expected finite MANUS positions with shape (25, 3)')
    points = points[MANUS_TO_HAND21].copy()
    points -= points[0].copy()
    palm = points[[5, 9, 13, 17]]
    length = float(np.mean(np.linalg.norm(palm, axis=1)))
    # Degenerate bases would cause NaNs in the upstream local-frame solver.
    forward = points[9]
    lateral = points[5]-points[13]
    if length < .005 or np.linalg.norm(np.cross(forward, lateral)) < 1e-7:
        raise ValueError('Degenerate MANUS palm geometry')
    if palm_length_m is not None:
        if not .01 <= float(palm_length_m) <= .2:
            raise ValueError('Invalid retargeting palm scale')
        points *= float(palm_length_m)/length
    return np.vstack((-points[9], points)).astype(np.float32)


def _load_official(directory):
    """Import only retargeting code, never webcam/GUI/SDK modules."""
    def load(name, file):
        spec = importlib.util.spec_from_file_location(name, directory/file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    helper = load('_dex_pomdar_retarget_utils', 'retarget_utils.py')
    previous = sys.modules.get('retarget_utils')
    sys.modules['retarget_utils'] = helper
    try:
        return load('_dex_pomdar_retargeter', 'retargeter.py').Retargeter
    finally:
        if previous is None:
            sys.modules.pop('retarget_utils', None)
        else:
            sys.modules['retarget_utils'] = previous


class OrcaRetargeter:
    """Official optimizer, with explicit joint-name mapping and actual limits."""
    def __init__(self, joint_names, lower, upper, config=None):
        import torch
        import yaml
        config = config or {}
        self.opt_steps = int(config.get('opt_steps', 2))
        if not 1 <= self.opt_steps <= 20:
            raise ValueError('retargeting.opt_steps must be between 1 and 20')
        self.palm_length_m = config.get('palm_length_m', .09)
        self.thumb_regularization_weight = config.get('thumb_regularization_weight', 0.)
        if (isinstance(self.thumb_regularization_weight, bool)
                or not isinstance(self.thumb_regularization_weight, (int, float))
                or not math.isfinite(self.thumb_regularization_weight)
                or not 0 <= self.thumb_regularization_weight <= 1):
            raise ValueError('retargeting.thumb_regularization_weight must be between 0 and 1')
        directory = Path(config.get('source_dir', WORKSPACE/'third_party/POMDAR/teleop')).resolve()
        torch.set_num_threads(int(config.get('torch_threads', 1)))
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass  # A previous task already initialized this process-wide pool.
        cls = _load_official(directory)
        scheme = yaml.safe_load((directory/'scheme_orca_v1_nowrist.yaml').read_text())
        names = list(scheme['gc_tendons'])
        if len(set(joint_names)) != len(joint_names) or set(joint_names) != set(names):
            raise ValueError('POMDAR ORCA actuator joints do not match the official hand scheme')
        self.to_actuator = np.array([names.index(name) for name in joint_names])
        self.to_scheme = np.array([list(joint_names).index(name) for name in names])
        self.lower, self.upper = np.asarray(lower), np.asarray(upper)
        # Upstream YAML thumb bounds differ from the actual MJCF actuator range.
        scheme['gc_limits_lower'] = np.rad2deg(self.lower[self.to_scheme]).tolist()
        scheme['gc_limits_upper'] = np.rad2deg(self.upper[self.to_scheme]).tolist()
        # Preserve the original abduction prior. The fixed thumb posture prior
        # competed with measured fingertip geometry and pulled MCP/IP toward
        # 0.7 rad even for open or tightly closed inputs. Disable only that prior
        # by default; 0.005 reproduces its previous effective coefficient.
        regularizers = {name: [0., .01] for name in
                        ('index_abd', 'middle_abd', 'ring_abd', 'pinky_abd')}
        if self.thumb_regularization_weight:
            regularizers.update({name: [.7, self.thumb_regularization_weight]
                                 for name in ('thumb_pp2mp', 'thumb_mp2dp')})
        loss = dict(lr=2.5, loss_func_cfg={
            'keyvector_fingers2palm': 2., 'keyvector_fingers2fingers': 1.,
            'zero_regularizor': {'weight': 1., 'kwargs': regularizers}})
        cwd = os.getcwd()
        try:
            self.optimizer = cls(mjcf_filepath=str(directory/'orca_v1_nowrist.xml'),
                hand_scheme=scheme, mano_adjustments=str(directory/'mano_adjustment_orca_v1.yaml'),
                retargeter_cfg=loss, device='cpu')
        finally:
            os.chdir(cwd)  # Upstream may raise after its temporary chdir.
        self.reset(np.zeros(len(joint_names)))

    def reset(self, ctrl):
        import torch
        seed = np.rad2deg(np.clip(ctrl, self.lower, self.upper)[self.to_scheme])
        with torch.no_grad():
            self.optimizer.gc_joints.copy_(torch.as_tensor(seed, dtype=self.optimizer.gc_joints.dtype))
        self.optimizer.opt.state.clear()

    def retarget(self, finger):
        points = manus_to_mano(finger, palm_length_m=self.palm_length_m)
        angles, _ = self.optimizer.retarget(points, opt_steps=self.opt_steps)
        target = np.deg2rad(np.asarray(angles, dtype=float)[self.to_actuator])
        if target.shape != self.lower.shape or not np.isfinite(target).all():
            raise ValueError('Retargeter returned invalid joint targets')
        return np.clip(target, self.lower, self.upper)


class RetargetWorker:
    """A latest-only mailbox; reset invalidates in-flight optimization results."""
    def __init__(self, retargeter):
        self.retargeter = retargeter
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.pending = self.result = self.seed = None
        self.generation = 0
        self.error = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='pomdar-retarget', daemon=True)
        self.thread.start()

    def submit(self, finger, key, captured_at):
        with self.lock:
            self.pending = (copy.deepcopy(finger), key, captured_at, self.generation)
        self.wake.set()

    def reset(self, ctrl):
        with self.lock:
            self.generation += 1
            self.seed = np.array(ctrl, copy=True)
            self.pending = self.result = None
            self.error = None
        self.wake.set()

    def latest(self):
        with self.lock:
            if self.result is None:
                return None
            return dict(self.result, ctrl=self.result['ctrl'].copy())

    def _run(self):
        while not self.closed.is_set():
            self.wake.wait(.2)
            with self.lock:
                request, self.pending = self.pending, None
                seed, self.seed = self.seed, None
                self.wake.clear()
                generation = self.generation
            try:
                if seed is not None and hasattr(self.retargeter, 'reset'):
                    self.retargeter.reset(seed)
                if request is None:
                    continue
                finger, key, captured_at, sample_generation = request
                if sample_generation != generation:
                    continue
                start = time.monotonic()
                ctrl = np.asarray(self.retargeter.retarget(finger), dtype=float).copy()
                if not np.isfinite(ctrl).all():
                    raise ValueError('Non-finite retargeting result')
                with self.lock:
                    if sample_generation == self.generation:
                        self.result = dict(ctrl=ctrl, key=key, captured_at=captured_at,
                                           source=finger.get('source', 'manus'), seconds=time.monotonic()-start)
                        self.error = None
            except Exception as exc:
                with self.lock:
                    if generation == self.generation:
                        self.error = str(exc)
                        self.result = None

    def stop(self):
        self.closed.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=2.)
