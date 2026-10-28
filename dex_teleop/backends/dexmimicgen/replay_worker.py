"""Generate all original/state/action replay frames before the browser plays them."""
import os


def parent_guard():
    expected = int(os.environ.get('DEX_TELEOP_PARENT_PID', '0'))
    if not expected:
        return
    import ctypes
    import signal
    if expected <= 1 or os.getppid() != expected:
        raise SystemExit('Replay parent exited before worker startup')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != expected:
        raise SystemExit('Replay parent exited during worker startup')


if __name__ == '__main__':
    parent_guard()  # Before MuJoCo/EGL imports; no unsafe threaded-parent preexec.

import json
from pathlib import Path
import time

import h5py
import numpy as np
from PIL import Image

from .recording import model_signature, restore_state


def atomic_json(path, payload):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, allow_nan=False))
    temporary.replace(path)


def generate(config, *, model_factory=None, renderer_factory=None):
    import mujoco
    if mujoco.__version__ != config['mujoco_version']:
        raise ValueError('Replay requires the same MuJoCo version used for recording')
    source, directory = Path(config['source']), Path(config['directory'])
    stat = source.stat()
    if [stat.st_ino, stat.st_size, stat.st_mtime_ns] != config['source_stat']:
        raise ValueError('Source episode changed after replay was requested')
    launch = config['launch']
    render_config = launch.get('render', {})
    if model_factory is None:
        def model_factory():
            with h5py.File(source,'r') as file:
                binary=np.asarray(file['replay/model_binary'][:],dtype=np.uint8)
            temporary=directory/'model.mjb'
            temporary.write_bytes(binary.tobytes())
            try:return mujoco.MjModel.from_binary_path(str(temporary))
            finally:temporary.unlink(missing_ok=True)
    if renderer_factory is None:
        from .rendering import Renderer
        renderer_factory=lambda model: Renderer(model,render_config,launch['camera_mapping'])
    model = model_factory()
    renderer = renderer_factory(model)
    try:
        signature = model_signature(model)
        if signature != config['signature']:
            raise ValueError('DexMimicGen model or actuator layout differs from the saved episode')
        data, reference = mujoco.MjData(model), mujoco.MjData(model)
        actual_view = mujoco.MjData(model)
        state_spec = int(config['state_spec'])
        if state_spec != int(mujoco.mjtState.mjSTATE_INTEGRATION):
            raise ValueError('Replay requires the complete MuJoCo integration state')
        integration = np.empty(mujoco.mj_stateSize(model, state_spec))
        steps, cameras, n = config['steps'], config['cameras'], config['frames']
        dt = float(model.opt.timestep)
        if not np.isclose(dt, config['physics_dt'], rtol=0, atol=1e-12):
            raise ValueError('Replay Physics timestep differs from recording')
        cache_bytes = 0
        metrics = []
        started = time.monotonic()
        robot_mask=np.array([(mujoco.mj_id2name(model,mujoco.mjtObj.mjOBJ_BODY,i) or '').startswith(('robot','gripper'))
                             for i in range(model.nbody)],dtype=bool)
        object_mask = ~robot_mask
        object_mask[0] = False
        actuator_addresses = np.asarray([address for address, spec in zip(
            signature['actuator_qpos_addresses'], signature['actuator_specs'])
            if address >= 0 and spec['kind'] == 'joint_position_target' and spec['unit'] == 'rad'], dtype=int)

        def progress(fraction, message):
            atomic_json(directory/'progress.json', dict(progress=fraction, message=message))

        def save(side, camera, index, rgb):
            nonlocal cache_bytes
            image = np.asarray(rgb)
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                raise ValueError('Invalid replay RGB image')
            target = directory/side/camera/f'{index:06d}.jpg'
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(image).save(target, quality=90, subsampling=0)
            cache_bytes += target.stat().st_size
            if cache_bytes > config['max_cache_bytes']:
                raise RuntimeError('Replay cache byte limit exceeded')

        with h5py.File(source, 'r') as h:
            snapshots = h['replay/state/integration']
            if snapshots.shape != (n, len(integration)):
                raise ValueError('Integration-state dimensions do not match the MuJoCo model')
            controls = h['replay/control']
            events = controls['sim_step'][:]
            ctrl = controls['ctrl'][:]
            mocap_pos, mocap_quat = controls['mocap_pos'][:], controls['mocap_quat'][:]
            restore_state(model, data, snapshots[0], state_spec)
            step, event = steps[0], 0
            for index, target_step in enumerate(steps):
                while step < target_step:
                    while event+1 < len(events) and events[event+1] <= step:
                        event += 1
                    mujoco.mj_forward(model,data)
                    data.ctrl[:] = ctrl[event]
                    data.mocap_pos[:] = mocap_pos[event]
                    data.mocap_quat[:] = mocap_quat[event]
                    mujoco.mj_step(model, data)
                    step += 1
                # Forward calculations are confined to copies. Replaying never
                # mutates the action integrator merely to render/measure a frame.
                mujoco.mj_getState(model, data, integration, state_spec)
                restore_state(model, actual_view, integration, state_spec, forward=True)
                restore_state(model, reference, snapshots[index], state_spec, forward=True)
                body_error = np.linalg.norm(actual_view.xpos-reference.xpos, axis=1)
                joint_error = np.abs(data.qpos[actuator_addresses]-reference.qpos[actuator_addresses])
                metrics.append(dict(frame=index, sim_step=target_step,
                    joint_max_rad=float(joint_error.max(initial=0.)),
                    object_position_max_m=float(body_error[object_mask].max(initial=0.)),
                    link_position_max_m=float(body_error[robot_mask].max(initial=0.)),
                    qpos_max_abs=float(np.abs(data.qpos-reference.qpos).max(initial=0.)),
                    qvel_max_abs=float(np.abs(data.qvel-reference.qvel).max(initial=0.))))
                images = renderer.capture(data)
                for camera in cameras:
                    save('action', camera, index, images[camera])
                    save('original', camera, index, h[f'cameras/{camera}/rgb'][index])
                progress(.5*(index+1)/n, f'Action replay {index+1} / {n}')
            for index in range(n):
                restore_state(model, reference, snapshots[index], state_spec)
                images = renderer.capture(reference)
                for camera in cameras:
                    save('state', camera, index, images[camera])
                progress(.5+.5*(index+1)/n, f'State replay {index+1} / {n}')
        maxima = {key: max(row[key] for row in metrics) for key in metrics[0] if key not in ('frame', 'sim_step')}
        manifest = dict(frames=n, cameras=cameras, steps=steps, physics_dt=dt,
                        metrics=metrics, maxima=maxima, cache_bytes=cache_bytes,
                        generation_seconds=time.monotonic()-started, interpolation=False,
                        full_control_timeline=True, physics_warmup_steps=0,
                        note='MuJoCo full integration state and every applied command change are replayed at the recorded Physics steps.')
        atomic_json(directory/'manifest.json', manifest)
        return manifest
    finally:
        renderer.close()


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python -m dex_teleop.backends.dexmimicgen.replay_worker CONFIG.json')
    generate(json.loads(Path(sys.argv[1]).read_text()))
