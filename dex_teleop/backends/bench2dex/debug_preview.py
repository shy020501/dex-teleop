"""Static task preview: render once, then handle UI task changes without Kit updates."""
import os
import queue


def debug_enabled():
    return os.environ.get('DEBUG_MODE', 'false').strip().lower() in ('true', '1', 'yes', 'on')


def run_debug_preview(dashboard, sim, probe, session):
    """Called only after scene/home/camera initialization, before the simulation loop.

    The HTTP server and JPEG encoder use CPU threads. No sim.step(), sim.render(),
    app.update(), IK or network input runs while waiting for a different task.
    Kit retains its GPU allocation until process exit or the existing task restart.
    """
    if probe is None:
        raise RuntimeError('DEBUG_MODE requires the browser launcher')
    dashboard.update(ready=False, phase='loading')
    probe.capture_snapshot(sim)
    # Use the mounted wrist cameras once, at the same stationary scene state.
    if session is not None:
        session.after_render(int(sim.current_time_step_index))
    sim.pause()
    dashboard.update(ready=True, recording=False, phase='debug', error=None)
    print('[DEBUG] Static task preview ready. Physics/render/teleop stopped; task switching remains available.',
          flush=True)
    while True:
        try:
            command = dashboard.commands.get(timeout=1.)
        except queue.Empty:
            continue
        if command.get('action') != 'task':
            dashboard.update(error='DEBUG 모드에서는 로봇 조작·리셋·수집을 사용할 수 없습니다.')
            continue
        if dashboard.assets.snapshot()['busy']:
            dashboard.update(error='다운로드 완료 또는 취소 후 task를 전환하세요.')
            continue
        dashboard.restart_task = command['task']
        dashboard.update(ready=False, phase='switching')
        return  # main's normal finally block closes resources and execs the selected task.
