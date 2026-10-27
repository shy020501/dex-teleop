"""Precompute an entire serial action replay, then a kinematic state replay.

State renders never contaminate action dynamics: the two passes are separate.
The action pass holds each recorded target for the actual sim_step difference.
"""
import json
from pathlib import Path
import time

import h5py
import numpy as np
from PIL import Image

from dex_teleop.backends.bench2dex.scene_state import capture_scene, read_replay_state, restore_scene, snapshot_error
from dex_teleop.backends.bench2dex.scene_worker import render_snapshot


def atomic_json(path, payload):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, allow_nan=False))
    temporary.replace(path)


def generate(config, collector, sim, objects, controlled, targets, hooks):
    import torch
    from utils.runtime_helpers import update_sim_objects, write_articulation_targets
    directory = Path(config['directory'])
    source = Path(config['source'])
    stat = source.stat()
    if [stat.st_ino, stat.st_size, stat.st_mtime_ns] != config['source_stat']:
        raise RuntimeError('Source episode changed after replay request')
    n, cameras, steps = config['frames'], config['cameras'], config['steps']
    cache_bytes = 0
    metrics = []

    def progress(fraction, message):
        atomic_json(directory/'progress.json', dict(progress=fraction, message=message))

    def save(side, camera, index, rgb):
        nonlocal cache_bytes
        if rgb is None or rgb.dtype != np.uint8:
            raise RuntimeError(f'Missing RGB: {side}/{camera}/{index}')
        target = directory/side/camera/f'{index:06d}.jpg'
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb).save(target, quality=90, subsampling=0)
        cache_bytes += target.stat().st_size
        if cache_bytes > config['max_cache_bytes']:
            raise RuntimeError('Replay cache limit exceeded (8 GiB)')

    robot = objects['global_robot']
    dt = float(sim.get_physics_dt())
    started = time.monotonic()
    with h5py.File(source, 'r') as h:
        initial = read_replay_state(h, 0)
        restore_scene(initial, objects, sim, targets)
        # Temporal renderer warmup at the exact initial state; no physics warmup.
        for _ in range(3):
            collector.sync_mounted_camera_poses()
            sim.render()
        actions = h['action/commanded'][:]
        for i in range(n):
            if i:
                target = torch.as_tensor(actions[i-1], dtype=robot.data.joint_pos.dtype,
                                         device=robot.data.joint_pos.device).reshape_as(robot.data.joint_pos)
                targets[id(robot)] = target.clone()
                for _ in range(steps[i]-steps[i-1]):
                    for hook in hooks:
                        hook()
                    write_articulation_targets(controlled, targets)
                    sim.step(render=False)
                    update_sim_objects(objects.values(), dt)
            actual = capture_scene(objects, targets)
            reference = read_replay_state(h, i)
            metrics.append(dict(frame=i, sim_step=steps[i], **snapshot_error(reference, actual)))
            collector.sync_mounted_camera_poses()
            sim.render()
            frames = collector._camera_rig.capture(dt=dt*max(1, steps[i]-steps[i-1] if i else 1))
            if collector._camera_rig.last_capture_errors:
                raise RuntimeError(str(collector._camera_rig.last_capture_errors))
            for camera in cameras:
                save('action', camera, i, frames[camera].rgb)
                save('original', camera, i, h[f'cameras/{camera}/rgb'][i])
            progress(.5*(i+1)/n, f'Action replay {i+1} / {n}')
        # Restore each recorded state in a separate pass after action simulation.
        for i in range(n):
            frames, errors = render_snapshot(collector, sim, objects, read_replay_state(h, i), targets)
            if errors:
                raise RuntimeError('; '.join(errors))
            for camera in cameras:
                save('state', camera, i, frames[camera].rgb)
            progress(.5+.5*(i+1)/n, f'State replay {i+1} / {n}')
    maxima = {key: max(row[key] for row in metrics) for key in metrics[0] if key not in ('frame', 'sim_step')}
    atomic_json(directory/'manifest.json', dict(frames=n, cameras=cameras, steps=steps,
        physics_dt=dt, metrics=metrics, maxima=maxima, cache_bytes=cache_bytes,
        generation_seconds=time.monotonic()-started, interpolation=False, physics_warmup_steps=0,
        note='PhysX 접촉 상태·solver 내부 캐시는 복원되지 않습니다. 수치 오차와 원본/상태 영상을 함께 확인하세요.'))
