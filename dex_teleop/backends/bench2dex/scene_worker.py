"""Worker-side Isaac calls, all on its one main thread."""
import os
import time

from dex_teleop.backends.bench2dex.scene_state import capture_scene, restore_scene, scene_signature, snapshot_error


def render_snapshot(collector, sim, objects, state, hold_targets):
    restore_scene(state, objects, sim, hold_targets)
    collector.sync_mounted_camera_poses()
    sim.render()
    frames = collector._camera_rig.capture(dt=sim.get_physics_dt())
    errors = list(collector._camera_rig.last_capture_errors)
    missing = set(collector._camera_rig.camera_ids)-set(frames)
    if missing:
        errors.append('Missing cameras: '+', '.join(sorted(missing)))
    # Verify the kinematic state the camera sees, not just the requested qpos.
    error = snapshot_error(state, capture_scene(objects, hold_targets))
    if error['link_position_max_m'] > .0001 or error['object_position_max_m'] > .0001:
        errors.append('Restored render state differs from snapshot: '+str(error))
    return frames, errors


def run_worker(config, collector, sim, objects, controlled, targets, hooks, app):
    sim.set_setting('/app/runLoops/main/rateLimitEnabled', False)
    try:
        import omni.kit.loop._loop as kit_loop
        kit_loop.acquire_loop_interface().set_manual_mode(False)
    except ImportError:
        pass
    from omni.kit.viewport.utility import get_active_viewport
    viewport = get_active_viewport()
    if viewport is not None:
        viewport.updates_enabled = False  # Only the three recording cameras.
    signature = scene_signature(objects)
    if signature != config['signature']:
        raise RuntimeError('Worker scene/joint signature differs from source scene')
    if os.environ.get('BENCH2DEX_REPLAY_JOB') == '1':
        from dex_teleop.backends.bench2dex.replay_runtime import generate
        generate(config, collector, sim, objects, controlled, targets, hooks)
        return
    from multiprocessing.connection import Connection
    connection = Connection(int(os.environ['BENCH2DEX_WORKER_FD']))
    try:
        # Warm only the renderer. Do not advance the authoritative snapshot.
        initial = capture_scene(objects, targets)
        for _ in range(3):
            render_snapshot(collector, sim, objects, initial, targets)
        connection.send(dict(type='ready'))
        while app.is_running():
            if not connection.poll(.5):
                continue
            request = connection.recv()
            started = time.monotonic()
            try:
                if request['type'] != 'render':
                    raise ValueError('Unknown worker request')
                frames, errors = render_snapshot(collector, sim, objects, request['state'], targets)
                result = dict(token=request['token'], frames=frames, errors=errors,
                    sim_step=request['sim_step'], physics_step=request['physics_step'],
                    sim_time=request['sim_time'], completed_ns=time.time_ns(),
                    snapshot_monotonic=request['snapshot_monotonic'],
                    render_seconds=time.monotonic()-started)
            except Exception as exc:
                result = dict(token=request.get('token'), error=str(exc))
            # Disconnection is normal on parent shutdown. Do not try to send a
            # second error response over the same broken pipe.
            connection.send(result)
    except (EOFError, BrokenPipeError, ConnectionResetError):
        pass
    finally:
        connection.close()
