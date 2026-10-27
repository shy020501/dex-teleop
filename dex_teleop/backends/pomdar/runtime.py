"""Native MuJoCo/POMDAR loop behind the shared Windows and browser interfaces."""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import queue
import signal
import time
import uuid

from dex_teleop.paths import WORKSPACE
from dex_teleop.core.config import load_server_config, resolve_server_config
from dex_teleop.core.step_timing import RollingStepTiming
from dex_teleop.dashboard import state
from dex_teleop.dashboard.http import LatestFrame, serve_viewer
from .assets import AssetDownloads
from .paths import DATA_ROOT
from .task_info import task_catalog, task_info


class Backend:
    name = 'pomdar'
    default_task = 'V1_Wheel'

    def __init__(self, data_root=DATA_ROOT):
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.assets = AssetDownloads()

    def tasks(self):
        return task_catalog(self.assets)


def simulation_settings(config):
    result = dict(physics_dt=.002, control_hz=60., sample_hz=20., preview_hz=20.,
                  max_episode_frames=1800, max_buffer_mb=6144)
    result.update(config.get('simulation', {}))
    for name in ('physics_dt', 'control_hz', 'sample_hz', 'preview_hz'):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'Invalid simulation setting: {name}')
    if result['physics_dt'] > .01 or any(result[key]*result['physics_dt'] > 1
                                        for key in ('control_hz', 'sample_hz', 'preview_hz')):
        raise ValueError('Control/sample/preview rates must not exceed Physics rate; Physics dt <= 0.01')
    stride = 1/(result['sample_hz']*result['physics_dt'])
    if not math.isclose(stride, round(stride), abs_tol=1e-8):
        raise ValueError('Sampling rate must divide the Physics rate for exact observation steps')
    for name, lo, hi in [('max_episode_frames', 2, 10000), ('max_buffer_mb', 16, 16384)]:
        if type(result[name]) is not int or not lo <= result[name] <= hi:
            raise ValueError(f'{name} must be an integer in {lo}..{hi}')
    return result


class Runtime:
    def __init__(self, args, config):
        self.args, self.config = args, config
        self.settings = simulation_settings(config)
        self.render_config = dict(config.get('render', {}))
        self.stopping = False
        self.bridge = self.renderer = self.recorder = self.publisher = self.server = None
        self.model = self.data = None
        self.step = self.completed_steps = 0
        self.action_source = 'hold'
        self.replay_paused = False
        self.pending_outcome = None
        backend = Backend(args.data_root)
        self.ui = state.install_dashboard(state.Dashboard(backend=backend,
            state_file=args.output_dir/'settings.json', debug_mode=args.debug), args.task)
        self.ui.update(simulator='pomdar', success_mode='manual', outcome_request=None,
                       main_camera=self.render_config.get('main_camera', 'oblique_v1'),
                       execution_mode='serial', enabled_sides=['right'],
                       pose_reference='hand_root', max_episode_frames=self.settings['max_episode_frames'],
                       sample_hz=self.settings['sample_hz'])
        self.frames = LatestFrame()
        self.timing = RollingStepTiming(args.timing_window)

    def start(self):
        from .rendering import FramePublisher
        self.server = serve_viewer(self.frames, self.args.port)
        self.publisher = FramePublisher(self.frames, self.ui.previews, self.render_config.get('jpeg_quality', 75))
        self.load(self.ui.snapshot()['task'])
        print(f'[POMDAR] Browser: http://127.0.0.1:{self.server.server_address[1]}; '
              f'input: {self.config["network"]["bind"]}; task={self.ui.state["task"]}', flush=True)

    def _settle(self):
        import mujoco
        # The native free hand follows its mocap weld; finish initialization
        # before defining the keyboard home or collecting an initial frame.
        for _ in range(50):
            mujoco.mj_step(self.model, self.data)

    def load(self, task):
        import mujoco
        from .scene import build_model, reset_data, model_identity
        from .control import Bridge
        from .rendering import Renderer
        from .recording import RecordingSession
        from .replay import ReplayJobs
        task_info(task)
        # Parse and validate the new task before releasing the working scene.
        main_size = (self.render_config.get('main_width',1280), self.render_config.get('main_height',720))
        main_camera = self.render_config.get('main_camera', 'oblique_v1')
        next_model = build_model(task, timestep=self.settings['physics_dt'], main_size=main_size,
                                 main_camera=main_camera)
        self.ui.update(ready=False, phase='loading', workspace_ready=False, error=None)
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        if self.ui.replays is not None:
            self.ui.replays.close()
        if self.recorder is not None:
            self.recorder.close()
            self.recorder = None
        if self.renderer is not None:
            self.renderer.close()
            self.renderer = None
        self.model = next_model
        self.data = mujoco.MjData(self.model)
        reset_data(self.model, self.data)
        self._settle()
        self.step = 0
        self.renderer = Renderer(self.model, self.render_config)
        self.bridge = Bridge(self.model, self.data, self.config)
        self.bridge.apply_workspace(self.ui.snapshot()['workspace'])
        if self.args.mode == 'teleop': self.bridge.start()
        self.bridge.set_enabled(not self.args.debug and self.args.mode == 'teleop')
        launch = dict(task=task, physics_dt=self.settings['physics_dt'], render=self.render_config,
                      retargeting=copy.deepcopy(self.config.get('retargeting', {})),
                      model_identity=model_identity(task, timestep=self.settings['physics_dt'], main_size=main_size,
                                                    main_camera=main_camera))
        self.recorder = RecordingSession(self.ui, self.model, task, launch,
            sample_hz=self.settings['sample_hz'], max_frames=self.settings['max_episode_frames'],
            max_buffer_mb=self.settings['max_buffer_mb'])
        self.ui.replays = ReplayJobs(self.ui.store, self.args.output_dir/'replay-cache')
        self.ui._pose_receiver = self.bridge.receiver
        self.ui.previews.configure(['cam_task','cam_wrist_right'])
        self.ui.update(task=task, ready=True, phase='debug' if self.args.debug else 'idle',
                       recording=False, frames=0, sim_seconds=0., success=False, outcome_request=None,
                       workspace_ready=not self.args.debug, workspace_pending=False,
                       workspace_effective=self.bridge.workspace_status(), action_spec=self.bridge.action_spec)
        self.ui.persist()
        self.publisher.reset()
        self.capture()
        self.reset_schedule()

    def reset_schedule(self):
        self.next_control = float(self.data.time)
        self.last_control = float(self.data.time)-1/self.settings['control_hz']
        self.next_preview = float(self.data.time)+1/self.settings['preview_hz']
        self.timing.reset(time.monotonic())
        self.deadline = time.monotonic()

    def capture(self):
        cameras = self.renderer.capture(self.data)
        self.publisher.offer(cameras, self.step, simulator='pomdar', task=self.ui.state['task'],
                             debug_mode=self.args.debug, execution_mode='serial')
        return cameras

    def reset(self):
        from .scene import reset_data
        self.ui.update(ready=False, phase='resetting')
        self.bridge.set_enabled(False)
        reset_data(self.model, self.data)
        self._settle()
        self.step = 0
        self.bridge.reset()
        self.bridge.set_enabled(self.args.mode == 'teleop')
        self.publisher.reset()
        self.ui.update(ready=True, phase='saving' if self.recorder.saving else 'idle',
                       outcome_request=None, workspace_effective=self.bridge.workspace_status())
        self.reset_schedule()
        return self.capture()

    def request_outcome(self):
        """Freeze the episode at the F7 boundary, before disarming changes targets."""
        cameras = self.capture()
        stopped_at = time.monotonic()
        self.recorder.append(self.step, self.data, cameras)
        token = uuid.uuid4().hex
        self.pending_outcome = dict(token=token, step=self.step, cameras=cameras,
                                    stopped_at=stopped_at)
        self.bridge.set_enabled(False)
        self.ui.update(phase='awaiting_outcome', recording=False, error=None,
                       wall_seconds=stopped_at-self.recorder.started,
                       outcome_request=dict(token=token))

    def commands(self):
        while True:
            try: command = self.ui.commands.get_nowait()
            except queue.Empty: break
            try:
                action = command['action']
                if self.pending_outcome is not None:
                    # A second F7 may already be queued before the first is handled.
                    if action == 'toggle':
                        continue
                    pending = self.pending_outcome
                    if (action != 'outcome' or command.get('token') != pending['token']
                            or type(command.get('success')) is not bool):
                        raise ValueError('Choose success or failure for the stopped episode first')
                    self.recorder.finish(pending['step'], self.data, pending['cameras'],
                                         command['success'], 'manual_stop', stopped_at=pending['stopped_at'])
                    self.pending_outcome = None
                    self.ui.update(outcome_request=None)
                    self.reset()
                    continue
                if action == 'outcome':
                    raise ValueError('This episode is no longer waiting for an outcome')
                if self.recorder.saving:
                    if action == 'toggle' and self.recorder.error:
                        self.recorder.retry()
                        continue
                    raise ValueError('Wait until the previous episode has finished saving')
                if action == 'toggle':
                    if self.recorder.active:
                        self.request_outcome()
                    else:
                        cameras = self.reset()
                        self.recorder.start(self.step, self.data, cameras)
                elif action == 'reset':
                    if self.recorder.active:
                        self.recorder.finish(self.step, self.data, self.capture(), False, 'environment_reset')
                    self.reset()
                elif action == 'task':
                    previous = self.ui.snapshot()
                    self.ui.update(phase='switching', ready=False, folder=command['task'])
                    try:
                        self.load(command['task'])
                    except Exception:
                        self.ui.update(folder=previous['folder'])
                        self.load(previous['task'])
                        raise
                elif action == 'workspace':
                    self.bridge.apply_workspace(command['settings'])
                    self.ui.update(workspace=command['settings'], workspace_pending=False,
                                   workspace_effective=self.bridge.workspace_status())
                    self.ui.persist()
            except Exception as exc:
                if self.pending_outcome is not None and action == 'outcome':
                    self.ui.update(outcome_request=dict(token=self.pending_outcome['token']))
                self.ui.update(error=str(exc), workspace_pending=False)
                print(f'[POMDAR] Command failed: {exc}', flush=True)

    def service_replay(self):
        jobs = self.ui.replays
        if jobs.busy:
            if not self.replay_paused:
                self.bridge.set_enabled(False)
                self.ui.update(phase='replay')
                self.replay_paused = True
            if jobs.needs_service: jobs.start_pending()
            # Polling completion is required even with the browser disconnected.
            jobs.snapshot(jobs.job['id'])
            return True
        if self.replay_paused:
            self.replay_paused = False
            self.bridge.set_enabled(self.args.mode == 'teleop' and not self.args.debug)
            self.ui.update(phase='idle')
            self.reset_schedule()
        return False

    def run(self):
        import mujoco
        stride = round(1/(self.settings['sample_hz']*self.settings['physics_dt']))
        last_status = 0.
        while not self.stopping and (not self.args.steps or self.completed_steps < self.args.steps):
            self.recorder.poll()
            self.commands()
            if self.pending_outcome is not None or self.args.debug or self.service_replay():
                time.sleep(.02)
                continue
            # Haptic UI is shared; force extraction belongs to the backend.
            if time.monotonic()-last_status >= .1:
                settings = self.ui.snapshot()
                self.bridge.haptic_enabled = settings['haptic_enabled']
                self.bridge.haptic_encoder.set_force_range(settings['haptic_force_min_n'], settings['haptic_force_max_n'])
                self.bridge.haptic_encoder.set_max_power(settings['haptic_max_power'])
                self.ui.update(workspace_effective=self.bridge.workspace_status())
                last_status = time.monotonic()
            sim_time = float(self.data.time)
            if sim_time+1e-10 >= self.next_control:
                action = self.bridge.update(max(self.settings['physics_dt'], sim_time-self.last_control))
                self.action_source = action['source']
                self.last_control = sim_time
                self.next_control += 1/self.settings['control_hz']
                if self.bridge.consume_environment_reset():
                    if self.recorder.active:
                        self.recorder.finish(self.step, self.data, self.capture(), False, 'environment_reset')
                    self.reset()
                    continue
            self.recorder.note_control(self.step, self.data, self.action_source)
            mujoco.mj_step(self.model, self.data)
            self.step += 1
            self.completed_steps += 1
            self.bridge.after_physics_step(self.settings['physics_dt'])
            sample = self.recorder.active and self.step % stride == 0
            preview = self.data.time+1e-10 >= self.next_preview
            if sample or preview:
                cameras = self.capture()
                self.next_preview = float(self.data.time)+1/self.settings['preview_hz']
                if sample: self.recorder.append(self.step, self.data, cameras)
                if self.recorder.limit_reached:
                    self.recorder.finish(self.step, self.data, cameras, False, 'frame_limit')
                    self.reset()
            self.deadline += self.settings['physics_dt']
            remaining = self.deadline-time.monotonic()
            if remaining > 0: time.sleep(remaining)
            elif remaining < -.25: self.deadline = time.monotonic()
            self.timing.record(self.settings['physics_dt'], time.monotonic())
            self.frames.publish_timing(self.timing.snapshot())

    def close(self):
        errors = []
        if self.bridge is not None: self.bridge.set_enabled(False)
        if self.recorder is not None:
            try:
                if self.recorder.active:
                    if self.pending_outcome is not None:
                        pending = self.pending_outcome
                        self.recorder.finish(pending['step'], self.data, pending['cameras'], False,
                                             'shutdown', stopped_at=pending['stopped_at'])
                    else:
                        self.recorder.finish(self.step, self.data, self.capture(), False, 'shutdown')
            except Exception as exc: errors.append(str(exc))
            try: self.recorder.close()
            except Exception as exc: errors.append(str(exc))
        for resource in (self.ui.replays, self.bridge, self.publisher, self.ui.previews, self.renderer):
            if resource is not None:
                try:
                    method = resource.stop if resource is self.bridge else resource.close
                    method()
                except Exception as exc: errors.append(str(exc))
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if errors: raise RuntimeError('; '.join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='pomdar.yaml')
    parser.add_argument('--task')
    parser.add_argument('--mode', choices=('teleop','viewer'), default='teleop')
    parser.add_argument('--port', type=int, default=int(os.environ.get('DEX_TELEOP_BROWSER_PORT','8080')))
    parser.add_argument('--input-port', type=int)
    parser.add_argument('--steps', type=int, default=0)
    parser.add_argument('--timing-window', type=int, default=int(os.environ.get('TIMING_WINDOW','120')))
    parser.add_argument('--output-dir', type=Path, default=WORKSPACE/'logs/pomdar-live')
    parser.add_argument('--data-root', type=Path, default=DATA_ROOT,
                        help='Server-side override; never editable in the browser')
    args = parser.parse_args()
    if args.steps < 0 or not 1024 <= args.port <= 65535 or (args.input_port and not 1024 <= args.input_port <= 65535):
        parser.error('Invalid steps or port')
    debug = os.environ.get('DEBUG_MODE','false').lower()
    if debug not in ('0','1','false','true'): parser.error('DEBUG_MODE must be true or false')
    args.debug = debug in ('1','true')
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    config_path = resolve_server_config(args.config)
    os.environ['DEX_TELEOP_SERVER_CONFIG'] = str(config_path)
    config = load_server_config(config_path)
    if args.input_port: config['network']['bind'] = f'tcp://127.0.0.1:{args.input_port}'
    config['control']['status_path'] = str(args.output_dir/'teleop-status.json')
    runtime = Runtime(args, config)
    def stop(signum, frame):
        runtime.stopping = True
        print('[POMDAR] Stop requested; finishing episode and closing resources.', flush=True)
    previous = {number:signal.signal(number, stop) for number in (signal.SIGINT,signal.SIGTERM)}
    try:
        runtime.start()
        runtime.run()
    finally:
        try: runtime.close()
        finally:
            for number, handler in previous.items(): signal.signal(number, handler)


if __name__ == '__main__':
    main()
