"""DexBench's native Newton tasks behind the shared teleoperation dashboard."""
import argparse
import math
import os
from pathlib import Path
import queue
import signal
import time

from dex_teleop.paths import WORKSPACE
from dex_teleop.core.config import load_server_config, resolve_server_config
from dex_teleop.core.step_timing import RollingStepTiming
from dex_teleop.dashboard import state
from dex_teleop.dashboard.http import LatestFrame, serve_viewer
from .assets import AssetDownloads
from .paths import DATA_ROOT, SOURCE_REVISION
from .task_info import DEFAULT_TASK, task_info, task_catalog


def simulation_settings(config):
    values = dict(physics_dt=1/60, control_hz=30, sample_hz=30, preview_hz=30,
                  max_episode_frames=1800, max_buffer_mb=6144, seed=42)
    values.update(config.get('simulation', {}))
    for name in ('physics_dt', 'control_hz', 'sample_hz', 'preview_hz'):
        number = values[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise ValueError(f'Invalid simulation setting: {name}')
    if not math.isclose(values['physics_dt'], 1/60, rel_tol=0, abs_tol=1e-12):
        raise ValueError('DexBench native physics runs at 60 Hz')
    if not float(values['control_hz']).is_integer():
        raise ValueError('control_hz must be an integer divisor of 60 Hz')
    for numerator, denominator in ((60, values['control_hz']),
            (values['control_hz'], values['sample_hz']), (values['control_hz'], values['preview_hz'])):
        ratio = numerator/denominator
        if ratio < 1 or not math.isclose(ratio, round(ratio), abs_tol=1e-9):
            raise ValueError('Control must divide 60 Hz; sampling/preview must divide control_hz')
    for key, lo, hi in (('max_episode_frames', 2, 10000), ('max_buffer_mb', 16, 16384)):
        if type(values[key]) is not int or not lo <= values[key] <= hi:
            raise ValueError(f'{key} must be an integer in {lo}..{hi}')
    return values


class Backend:
    name = 'dexbench'
    default_task = DEFAULT_TASK

    def __init__(self, data_root=DATA_ROOT):
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.assets = AssetDownloads()

    def tasks(self):
        return task_catalog(self.assets)


class Runtime:
    def __init__(self, args, config):
        self.args, self.config = args, config
        self.settings = simulation_settings(config)
        self.decimation = round(60/self.settings['control_hz'])
        if args.steps and args.steps % self.decimation:
            raise ValueError(f'--steps must align with a control boundary ({self.decimation} physics steps)')
        self.sample_stride = round(self.settings['control_hz']/self.settings['sample_hz'])
        self.preview_stride = round(self.settings['control_hz']/self.settings['preview_hz'])
        self.stopping = False
        self.app_launcher = self.server = self.publisher = None
        self.env = self.embodiment = self.boundary = self.bridge = self.renderer = self.recorder = None
        self.step = self.completed_steps = self.control_steps = 0
        self.ui = state.install_dashboard(state.Dashboard(backend=Backend(args.data_root),
            state_file=args.output_dir/'settings.json', debug_mode=args.debug), args.task)
        self.ui.update(simulator='dexbench', success_mode='automatic', execution_mode='serial',
            enabled_sides=['right', 'left'], pose_reference='end_effector',
            max_episode_frames=self.settings['max_episode_frames'], sample_hz=self.settings['sample_hz'])
        self.frames = LatestFrame()
        # Newton can integrate a whole decimation block in one native call.
        # Report its measured wall time amortized over its actual Physics steps.
        self.timing = RollingStepTiming(max(1, math.ceil(args.timing_window/self.decimation)))

    def start(self):
        from .scene import launch_application
        from .rendering import FramePublisher
        self.server = serve_viewer(self.frames, self.args.port)
        self.publisher = FramePublisher(self.frames, self.ui.previews,
                                        self.config.get('render', {}).get('jpeg_quality', 75))
        self.app_launcher = launch_application(self.args.device)
        # Kit installs its own SIGINT handler during startup; replace it only
        # after launch so a user stop first flushes the active episode.
        self.install_signals()
        self.load(self.ui.snapshot()['task'])
        print(f'[DexBench] Ready: http://127.0.0.1:{self.args.port}; '
              f'input {self.config["network"]["bind"]}; source {SOURCE_REVISION}', flush=True)

    def install_signals(self):
        def stop(*_):
            self.stopping = True
            print('[DexBench] Stopping; saving the current episode and closing the environment.', flush=True)
        self.previous_signals = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}

    def _close_scene(self):
        from .scene import close_environment
        errors = []
        for name in ('bridge', 'recorder', 'renderer', 'boundary'):
            resource = getattr(self, name)
            if resource is not None:
                try:
                    (resource.stop if name == 'bridge' else resource.close)()
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    setattr(self, name, None)
        if self.env is not None:
            old, self.env = self.env, None
            try:
                close_environment(old)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('; '.join(errors))

    def load(self, task):
        from .scene import create_environment
        from .control import Bridge
        from .recording import RecordingSession
        from .rendering import Renderer
        task_info(task)
        ready, missing, error = self.ui.assets.readiness(task)
        if not ready:
            raise ValueError(f'Missing DexBench assets for {task}: {missing}; {error or "download the task assets first"}')
        self.ui.update(ready=False, phase='loading', workspace_ready=False, error=None)
        self._close_scene()
        self.env, self.embodiment, self.boundary = create_environment(task, self.config, self.args.device)
        if not math.isclose(float(self.env.physics_dt), self.settings['physics_dt'], abs_tol=1e-12):
            raise RuntimeError('Native DexBench physics rate differs from the server profile')
        if not math.isclose(float(self.env.step_dt), 1/self.settings['control_hz'], abs_tol=1e-12):
            raise RuntimeError('Native DexBench control rate differs from the server profile')
        self.step = self.control_steps = 0
        self.renderer = Renderer(self.env)
        self.bridge = Bridge(self.env, self.config, embodiment=self.embodiment)
        self.bridge.apply_workspace(self.ui.snapshot()['workspace'])
        if self.args.mode == 'teleop':
            self.bridge.start()
        self.bridge.set_enabled(self.args.mode == 'teleop' and not self.args.debug)
        launch = dict(task=task, simulator='dexbench', source_revision=SOURCE_REVISION,
            robot_key='dexbench_rby1_wuji2_joint', render=self.config.get('render', {}),
            environment=task_info(task)['environment'], physics_backend='newton',
            control_hz=self.settings['control_hz'])
        self.recorder = RecordingSession(self.ui, task, launch,
            action_names=self.bridge.action_names, joint_names=self.env.scene['robot'].joint_names,
            physics_dt=self.settings['physics_dt'], control_hz=self.settings['control_hz'],
            sample_hz=self.settings['sample_hz'], max_frames=self.settings['max_episode_frames'],
            max_buffer_mb=self.settings['max_buffer_mb'])
        self.ui._pose_receiver = self.bridge.receiver
        self.ui.previews.configure(['cam_wrist_left', 'cam_wrist_right'])
        self.publisher.reset()
        images = self.renderer.warmup()
        self.publish(images)
        self.ui.update(task=task, ready=True, phase='debug' if self.args.debug else 'idle',
            recording=False, frames=0, sim_seconds=0., success=False,
            workspace_ready=not self.args.debug, workspace_pending=False,
            workspace_effective=self.bridge.workspace_status(), action_spec=self.bridge.action_spec)
        self.ui.persist()
        self.reset_schedule()

    def reset_schedule(self):
        self.deadline = time.monotonic()
        self.timing.reset(self.deadline)

    def publish(self, images):
        self.publisher.offer(images, self.step, simulator='dexbench', task=self.ui.state['task'],
                             debug_mode=self.args.debug, execution_mode='serial')

    def capture(self):
        images = self.renderer.capture()
        self.publish(images)
        return images

    def snapshot(self):
        from .recording import capture_snapshot
        return capture_snapshot(self.env, sim_time_s=self.step*self.settings['physics_dt'])

    def reset(self):
        self.ui.update(ready=False, phase='resetting')
        self.bridge.set_enabled(False)
        self.env.reset()
        self.boundary.clear()
        self.step = self.control_steps = 0
        self.bridge.reset()
        self.bridge.set_enabled(self.args.mode == 'teleop' and not self.args.debug)
        self.publisher.reset()
        images = self.renderer.warmup()
        self.publish(images)
        self.ui.update(ready=True, phase='saving' if self.recorder.saving else 'idle',
                       workspace_effective=self.bridge.workspace_status())
        self.reset_schedule()
        return images

    def finish(self, success, reason, images=None):
        self.recorder.finish(self.step, self.snapshot(), self.capture() if images is None else images,
                             success, reason)

    def commands(self):
        while True:
            try:
                command = self.ui.commands.get_nowait()
            except queue.Empty:
                return
            try:
                action = command['action']
                if self.recorder.saving:
                    if action == 'toggle' and self.recorder.error:
                        self.recorder.retry()
                        continue
                    raise ValueError('Wait until the episode has finished saving')
                if action == 'toggle':
                    if self.recorder.active:
                        self.finish(False, 'manual_stop')
                        self.reset()
                    else:
                        images = self.reset()
                        self.recorder.start(self.step, self.snapshot(), images)
                elif action == 'reset':
                    if self.recorder.active:
                        self.finish(False, 'environment_reset')
                    self.reset()
                elif action == 'task':
                    previous = self.ui.snapshot()
                    self.ui.update(ready=False, phase='switching', folder=command['task'])
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
                self.ui.update(error=str(exc), workspace_pending=False)
                print(f'[DexBench] Command failed: {exc}', flush=True)
                if self.env is None or self.recorder is None:
                    raise  # A failed rollback cannot leave a runnable half-scene.

    def publish_timing(self):
        snapshot = self.timing.snapshot()
        snapshot.update(window=self.timing.window*self.decimation,
                        samples=snapshot['samples']*self.decimation)
        for key in ('physics_period_ms', 'wall_period_ms'):
            if snapshot[key] is not None:
                snapshot[key] /= self.decimation
        for key in ('physics_hz', 'wall_hz'):
            if snapshot[key] is not None:
                snapshot[key] *= self.decimation
        self.frames.publish_timing(snapshot)

    def run(self):
        import torch
        last_settings = 0.
        while not self.stopping and (not self.args.steps or self.completed_steps < self.args.steps):
            if not self.app_launcher.app.is_running():
                break
            self.recorder.poll()
            self.commands()
            if self.args.debug:
                time.sleep(.02)
                continue
            if time.monotonic()-last_settings >= .1:
                settings = self.ui.snapshot()
                self.bridge.haptic_enabled = settings['haptic_enabled']
                self.bridge.haptic_encoder.set_force_range(settings['haptic_force_min_n'], settings['haptic_force_max_n'])
                self.bridge.haptic_encoder.set_max_power(settings['haptic_max_power'])
                self.ui.update(workspace_effective=self.bridge.workspace_status())
                last_settings = time.monotonic()
            with torch.inference_mode():
                action = self.bridge.update(float(self.env.step_dt))
                if self.bridge.consume_environment_reset():
                    if self.recorder.active:
                        self.finish(False, 'environment_reset')
                    self.reset()
                    continue
                self.recorder.note_action(self.step, action, self.bridge.source)
                self.env.step(action)
                self.step += self.decimation
                self.completed_steps += self.decimation
                self.control_steps += 1
                self.bridge.after_physics_step(float(self.env.step_dt))
                sample = self.recorder.active and self.control_steps % self.sample_stride == 0
                done = self.recorder.active and self.boundary.done
                preview = self.control_steps % self.preview_stride == 0
                images = self.capture() if sample or preview or done else None
                if sample:
                    self.recorder.append(self.step, self.snapshot(), images)
                if self.recorder.active and (done or self.recorder.limit_reached):
                    success = self.boundary.success if done else False
                    reason = ('native_success' if success else
                              'time_limit' if self.boundary.timeout else 'native_failure') if done else 'frame_limit'
                    self.finish(success, reason, images)
                    self.reset()
                    continue
            self.deadline += float(self.env.step_dt)
            delay = self.deadline-time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -.25:
                self.deadline = time.monotonic()
            self.timing.record(float(self.env.step_dt), time.monotonic())
            self.publish_timing()

    def close(self, failed=False):
        errors = []
        if self.recorder is not None and self.recorder.active:
            try:
                if failed:
                    self.recorder.finish_cached('simulation_error')
                else:
                    self.finish(False, 'shutdown')
            except Exception as exc:
                errors.append(str(exc))
                # Kit/solver failure can leave the live world unusable. Save
                # only the last complete camera/state pair, never a partial step.
                try:
                    self.recorder.finish_cached('simulation_error')
                except Exception as recovery_error:
                    errors.append(str(recovery_error))
        if self.bridge is not None:
            try:
                self.bridge.set_enabled(False)
            except Exception as exc:
                errors.append(str(exc))
        try:
            self._close_scene()
        except Exception as exc:
            errors.append(str(exc))
        for resource in (self.publisher, self.ui.previews):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    errors.append(str(exc))
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        for sig, handler in getattr(self, 'previous_signals', {}).items():
            signal.signal(sig, handler)
        if self.app_launcher is not None:
            self.app_launcher.app.close(exit_code=1 if errors or failed else 0)
        if errors:
            raise RuntimeError('; '.join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='dexbench.yaml')
    parser.add_argument('--task')
    parser.add_argument('--mode', choices=('teleop', 'viewer'), default='teleop')
    parser.add_argument('--device', default='cuda:0', choices=('cuda:0', 'cpu'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('DEX_TELEOP_BROWSER_PORT', '8080')))
    parser.add_argument('--input-port', type=int)
    parser.add_argument('--steps', type=int, default=0)
    parser.add_argument('--timing-window', type=int, default=int(os.environ.get('TIMING_WINDOW', '120')))
    parser.add_argument('--output-dir', type=Path, default=WORKSPACE/'logs/dexbench-live')
    parser.add_argument('--data-root', type=Path, default=DATA_ROOT)
    args = parser.parse_args()
    if args.steps < 0 or args.timing_window < 1 or not 1024 <= args.port <= 65535 or (args.input_port and not 1024 <= args.input_port <= 65535):
        parser.error('Invalid step limit, timing window or localhost port')
    debug = os.environ.get('DEBUG_MODE', 'false').lower()
    if debug not in ('true', 'false', '0', '1'):
        parser.error('DEBUG_MODE must be true or false')
    args.debug = debug in ('true', '1')
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = resolve_server_config(args.config)
    os.environ['DEX_TELEOP_SERVER_CONFIG'] = str(path)
    config = load_server_config(path)
    config['control']['status_path'] = str(args.output_dir/'teleop-status.json')
    if args.input_port:
        config['network']['bind'] = f'tcp://127.0.0.1:{args.input_port}'
    runtime = Runtime(args, config)
    failed = True
    try:
        runtime.start()
        runtime.run()
        failed = False
    finally:
        runtime.close(failed=failed)


if __name__ == '__main__':
    main()
