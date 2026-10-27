"""Owned CPU scene snapshots. Isaac imports are deliberately deferred.

Snapshots are captured on the Physics thread, never on an IPC/JPEG thread.
They include velocities and articulated task objects, not just robot qpos.
"""
import copy
import hashlib
import json

import numpy as np


def scene_signature(objects):
    result = {}
    for name, obj in objects.items():
        data = getattr(obj, 'data', None)
        if data is None:
            continue
        if not hasattr(data, 'root_state_w'):
            raise ValueError(f'Split/replay does not support {name}: {type(obj).__name__}')
        result[name] = dict(joints=list(getattr(data, 'joint_names', [])),
                            bodies=list(getattr(obj, 'body_names', [])))
    return result


def cpu(value):
    if hasattr(value, 'detach'):
        value = value.detach().cpu().numpy()
    return np.array(value, dtype=np.float32, copy=True, order='C')


def capture_scene(objects, hold_targets=None):
    states = {}
    for name, obj in objects.items():
        data = getattr(obj, 'data', None)
        if data is None:
            continue
        if not hasattr(data, 'root_state_w'):
            raise ValueError(f'Unsupported scene state: {name} ({type(obj).__name__})')
        state = dict(root_state=cpu(data.root_state_w))
        if hasattr(data, 'joint_pos'):
            state.update(qpos=cpu(data.joint_pos), qvel=cpu(data.joint_vel))
            state['body_pose'] = cpu(data.body_state_w[..., :7])
            target = (hold_targets or {}).get(id(obj))
            if target is not None:
                state['hold_target'] = cpu(target)
        if any(not np.isfinite(value).all() for value in state.values()):
            raise ValueError(f'Non-finite scene state: {name}')
        states[name] = state
    return states


def restore_scene(states, objects, sim, hold_targets=None):
    """No Physics step or settling is allowed after a recorded initial state."""
    import torch
    for name, state in states.items():
        obj = objects[name]
        device = obj.data.root_state_w.device
        tensor = lambda value: torch.as_tensor(value, device=device, dtype=torch.float32)
        obj.reset()
        obj.write_root_state_to_sim(tensor(state['root_state']))
        if 'qpos' in state:
            obj.write_joint_state_to_sim(tensor(state['qpos']), tensor(state['qvel']))
            target = tensor(state.get('hold_target', state['qpos']))
            obj.set_joint_position_target(target)
            obj.set_joint_velocity_target(torch.zeros_like(target))
            if hold_targets is not None and id(obj) in hold_targets:
                hold_targets[id(obj)] = target.clone()
        obj.write_data_to_sim()
    sim.forward()
    for obj in objects.values():
        obj.update(0.)


def snapshot_error(reference, actual):
    """Max errors, with quaternion sign equivalence (q and -q)."""
    joint = position = angle = body_position = 0.
    for name, expected in reference.items():
        observed = actual[name]
        pose_a, pose_b = expected['root_state'][..., :7], observed['root_state'][..., :7]
        position = max(position, float(np.linalg.norm(pose_a[..., :3]-pose_b[..., :3], axis=-1).max()))
        dot = np.abs(np.sum(pose_a[..., 3:7]*pose_b[..., 3:7], axis=-1))
        angle = max(angle, float((2*np.arccos(np.clip(dot, 0, 1))).max()))
        if 'qpos' in expected:
            joint = max(joint, float(np.abs(expected['qpos']-observed['qpos']).max()))
        if 'body_pose' in expected:
            body_position = max(body_position, float(np.linalg.norm(
                expected['body_pose'][..., :3]-observed['body_pose'][..., :3], axis=-1).max()))
    return dict(joint_max_rad=joint, object_position_max_m=position,
                object_rotation_max_rad=angle, link_position_max_m=body_position)


def launch_digest(launch):
    return hashlib.sha256(json.dumps(launch, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def worker_collect_config(launch, output):
    config = copy.deepcopy(launch['collect_config'])
    for camera in config['camera_list']:
        if 'camera_id' in camera:
            camera['id'] = camera.pop('camera_id')
    config['dataset'] = dict(config['dataset'], root=str(output), name='worker')
    return config


def load_worker_collect_config(config_path, output_root_override=None):
    """Restore resolved cameras without applying today's robot profiles again.

    The private worker YAML comes from CollectConfig.to_dict(), with camera IDs
    converted by worker_collect_config(). Robot mounts, calibration and image
    sizes are already resolved. Reusing the user-YAML loader here would require
    profiles that were not serialized, or overwrite the recorded camera poses.
    CollectConfig.from_dict still validates the resolved camera definitions.
    """
    from pathlib import Path
    import yaml
    from collector.config import CollectConfig

    config = yaml.safe_load(Path(config_path).read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError(f'Worker collect config must be a dict: {config_path}')
    if output_root_override:
        config.setdefault('dataset', {})['root'] = str(output_root_override)
    return CollectConfig.from_dict(config)


def write_replay_state(h, frames, launch, signature):
    """Small extra state for faithful rerender/replay; existing schema stays intact."""
    import h5py
    group = h.create_group('replay')
    group.attrs['version'] = 1
    dtype = h5py.string_dtype('utf-8')
    group.create_dataset('launch', data=json.dumps(launch), dtype=dtype)
    group.create_dataset('signature', data=json.dumps(signature), dtype=dtype)
    for name in frames[0].scene_state:
        target = group.require_group('state').create_group(name)
        for key in frames[0].scene_state[name]:
            target.create_dataset(key, data=np.stack([f.scene_state[name][key] for f in frames]),
                                  compression='gzip', compression_opts=1)
    group.create_dataset('render_sim_step', data=np.asarray([f.render_sim_step for f in frames], np.int64))
    group.create_dataset('physics_step', data=np.asarray([f.physics_step for f in frames], np.int64))
    group.create_dataset('render_completed_ns', data=np.asarray([f.render_completed_ns for f in frames], np.int64))


def read_replay_state(h, index):
    return {name: {key: dataset[index] for key, dataset in group.items()}
            for name, group in h['replay/state'].items()}


class StateRecorder:
    def __init__(self, collector, sim, hold_targets):
        self.collector, self.sim, self.hold_targets = collector, sim, hold_targets
        self.signature = scene_signature(collector._interactive_objects)

    def observe(self, frame):
        frame.scene_state = capture_scene(self.collector._interactive_objects, self.hold_targets)
        frame.physics_step = int(self.sim.current_time_step_index)
        frame.render_sim_step = frame.sim_step
        import time
        frame.render_completed_ns = time.time_ns()

