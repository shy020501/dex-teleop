"""Bench2Dex collection lifecycle and publication of its HDF5 episodes."""
import copy
import json
import os
import queue
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dex_teleop.paths import WORKSPACE
from dex_teleop.core.workspace_limits import validate_workspace


class DashboardWriter:
    """Use upstream HDF5 schema, stage privately, publish a complete uniquely named file atomically."""
    def __init__(self, dashboard, session, submit=None):
        self.dashboard, self.session = dashboard, session
        self.submit = submit
        self.output_dir = str(WORKSPACE/'logs/dashboard-staging')

    def next_episode_id(self): return 0

    def write_episode(self, buffer, episode_id, meta):
        job = self.prepare(buffer, meta)
        if self.submit is not None:
            self.submit(job)
            return None
        return job.write()

    def prepare(self, buffer, meta):
        from dex_teleop.backends.bench2dex.episode_save import PreparedEpisode
        s = self.session
        status = 'success' if s.save_success else 'fail'
        uid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:12]
        folder = self.dashboard.store.folder(s.folder)
        if s.number is None:
            s.number = self.dashboard.store.next_number(s.folder)
        number = s.number
        saved_at = datetime.now(ZoneInfo('Asia/Seoul'))
        frames = buffer.frame_count
        settings = self.dashboard.snapshot()
        meta = dict(copy.deepcopy(meta), success=s.save_success, collection_mode='teleop',
                    demo_eligible=(buffer.error_count == 0 and any(f.action_commanded is not None for f in buffer.frames)),
                    task_name=self.dashboard.state['task'], episode_length_s=s.duration,
                    wall_duration_s=time.monotonic()-s.wall_start, termination_reason=s.reason,
                    unique_id=uid, data_subfolder=s.folder, haptic_strength=settings['haptic_strength'],
                    haptic_max_power=settings['haptic_max_power'],
                    haptic_enabled=settings['haptic_enabled'], haptic_force_min_n=settings['haptic_force_min_n'],
                    haptic_force_max_n=settings['haptic_force_max_n'],
                    workspace_min_offset_xyz=settings['workspace']['min_offset_xyz'],
                    workspace_max_offset_xyz=settings['workspace']['max_offset_xyz'])
        meta.update(episode_index=number, episode_length_steps=frames,
                    filename_timezone='Asia/Seoul', saved_at=saved_at.isoformat())
        if getattr(s.collector, '_replay_launch', None) is not None:
            meta.update(replay_launch=copy.deepcopy(s.collector._replay_launch),
                        scene_signature=s.state_recorder.signature,
                        execution_mode=self.dashboard.state.get('execution_mode', 'serial'))
        filename = f'{number:04d}_{status}_{frames}_{saved_at:%Y%m%d_%H%M%S}.hdf5'
        return PreparedEpisode(buffer, meta, folder, filename, self.dashboard.store.lock)


class CollectionSession:
    def __init__(self, dashboard, collector, sim, probe=None, async_save=True, hold_targets=None):
        from dex_teleop.backends.bench2dex.episode_save import EpisodeSaver
        self.ui, self.collector, self.sim = dashboard, collector, sim
        self.saver = EpisodeSaver() if async_save else None
        self.renderer = None
        self.state_recorder = None
        self.replay_paused = False
        self.gc_policy = None  # Owned by main.py, after initialization completes.
        self.folder = dashboard.state['folder']
        self.start_step = 0
        self.wall_start = 0.
        self.duration = 0.
        self.reason = ''
        self.number = None
        self.save_success = False
        self.save_error = False
        self.boundary_step = 0
        self.terminal_captured = False
        self.start_after_reset = False
        self.workspace_bridge = None
        self.workspace_epoch = None
        collector._attach_initial_action = True
        collector._camera_frame_listener = dashboard.previews.offer
        rig = getattr(collector, '_camera_rig', None)
        dashboard.previews.configure(rig.camera_ids if rig is not None else [])
        collector._writer = DashboardWriter(dashboard, self, self.saver.submit if self.saver else None)
        if probe is not None and rig is not None and 'cam_task' in rig.camera_ids:
            mode = os.environ.get('BENCH2DEX_EXECUTION_MODE', 'serial')
            if mode == 'split' and not dashboard.debug_mode:
                from dex_teleop.backends.bench2dex.split_rendering import SplitRenderer
                self.renderer = SplitRenderer(collector, sim, probe, dashboard.previews, hold_targets)
                self.state_recorder = self.renderer
            else:
                from dex_teleop.backends.bench2dex.rendering import DashboardRenderer
                self.renderer = DashboardRenderer(collector, sim, probe, dashboard.previews)
            dashboard.update(execution_mode='serial' if dashboard.debug_mode else mode)
            # The renderer publishes once; recording reuses exactly those arrays.
            collector._camera_frame_listener = None
        elif probe is not None:
            probe.use_task_camera = False  # Legacy state-only / profiling configuration.
        if self.state_recorder is None and getattr(collector, '_replay_launch', None) is not None:
            from dex_teleop.backends.bench2dex.scene_state import StateRecorder
            try:
                self.state_recorder = StateRecorder(collector, sim, hold_targets)
                collector._frame_observer = self.state_recorder.observe
            except ValueError as exc:
                # Preserve legacy serial collection for unsupported deformables.
                del collector._replay_launch
                dashboard.update(error='이 scene은 replay 검증을 지원하지 않습니다: '+str(exc))
        if getattr(collector, '_replay_launch', None) is not None:
            from dex_teleop.backends.bench2dex.replay_jobs import ReplayJobs
            dashboard.replays = ReplayJobs(dashboard.store, dashboard.state_file.parent/'replay-cache')
        # Bound state-only/RGB episodes independently of machine wall-time.
        collector._buffer.max_frames = 1800
        dashboard.update(ready=not dashboard.debug_mode, phase='loading' if dashboard.debug_mode else 'idle')

    def start(self, step):
        """Capture the initial observation; callers must finish the scene reset first."""
        if self.ui.debug_mode:
            raise ValueError('DEBUG 모드에서는 수집을 시작할 수 없습니다.')
        if getattr(self.renderer, 'error', None):
            raise ValueError('렌더링 오류: '+self.renderer.error)
        if self.saver is not None and self.saver.pending:
            raise ValueError('이전 episode 저장을 먼저 완료하세요.')
        if self.collector.episode_active: return
        if self.ui.assets.snapshot()['busy']: raise ValueError('다운로드 완료 또는 취소 후 수집을 시작하세요.')
        self.folder = self.ui.snapshot()['folder']
        self.ui.store.folder(self.folder)  # Fail before starting if the root is not writable.
        self.number = self.ui.store.next_number(self.folder)
        self.start_step, self.wall_start = step, time.monotonic()
        self.duration, self.save_error = 0., False
        self.boundary_step, self.terminal_captured = step, False
        self.collector._episode_termination_reason_override = None
        self.collector.start_episode(sim_step=step)
        self.collector._buffer.max_frames = 1800
        self.ui.update(recording=True, phase='recording', error=None, sim_seconds=0., frames=self.collector.frame_count,
                       wall_seconds=0., success=False)

    def after_reset(self, step):
        """Called by the simulation only after restore, input rearm and camera refresh."""
        start = self.start_after_reset
        self.start_after_reset = False
        self.ui.previews.reset()
        if self.renderer is not None:
            self.renderer.invalidate()
        if self.gc_policy is not None:
            self.gc_policy.collect('environment_reset')
        phase = 'saving' if self.saver is not None and self.saver.pending else 'idle'
        self.ui.update(ready=True, phase=phase, frames=0, sim_seconds=0., wall_seconds=0., success=False)
        if start:
            try:
                self.start(step)
            except Exception as exc:
                self.ui.update(error=str(exc))

    def finish(self, success, reason):
        if not self.collector.episode_active: return
        self.save_success, self.reason = bool(success), reason
        self.collector._episode_termination_reason_override = reason
        self.ui.update(phase='saving')
        try:
            if not self.terminal_captured:
                # Terminal observation is after the final executed action, including off-stride success.
                self.collector._buffer.max_frames = 1801
                self.collector._capture_observation(self.boundary_step, None, 'none', None, force_capture=True)
                self.terminal_captured = True
            saved_frames = self.collector.frame_count
            path = self.collector.finish_episode()
        except Exception:
            self.collector._flushed = False
            self.save_error = True
            self.ui.update(phase='save_error')
            raise
        if self.saver is not None and self.saver.pending:
            self.ui.update(recording=False, phase='saving', success=bool(success), error=None, frames=saved_frames)
        else:
            self.ui.update(recording=False, phase='idle', last_saved=path, success=bool(success), error=None, frames=saved_frames)

    def poll_saves(self, wait=False):
        if self.saver is None:
            return
        result = self.saver.poll(wait=wait)
        if result is None:
            return
        if 'error' in result:
            self.save_error = True
            self.ui.update(recording=False, phase='save_error', error=result['error'])
            return
        self.save_error = False
        phase = self.ui.state['phase']
        self.ui.update(last_saved=result['path'], save_seconds=result['seconds'], error=None,
                       phase='idle' if phase in ('saving', 'save_error') else phase)
        print(f"[DASHBOARD] Background save completed in {result['seconds']:.2f}s: {result['path']}", flush=True)

    def close(self):
        """Drain saves before Kit's native exit or task exec replaces this process."""
        try:
            self._drain_saves()
        finally:
            # Renderer cleanup must also run when replay/save/recovery fails.
            if self.renderer is not None and hasattr(self.renderer, 'close'):
                self.renderer.close()

    def _drain_saves(self):
        if getattr(self.ui, 'replays', None) is not None:
            self.ui.replays.close()
        if self.saver is None:
            return
        self.poll_saves(wait=True)
        self.saver.close()
        if self.saver.pending:
            # Preserve a completed episode even when the data mount is unavailable
            # at shutdown. During normal operation F7 retries the original target.
            from dataclasses import replace
            recovery = self.ui.state_file.parent / 'unsaved-episodes' / uuid.uuid4().hex
            recovery.mkdir(parents=True)
            job = self.saver.job
            (recovery/'recovery.json').write_text(json.dumps(dict(
                intended_path=str(job.folder/job.filename), error=self.ui.state['error']), indent=2))
            path = replace(job, folder=recovery).write()
            print(f'[DASHBOARD] SAVE RECOVERY (original destination failed): {path}', flush=True)
            raise RuntimeError(f'원래 저장 경로에 쓰지 못했습니다. 복구 파일: {path}')

    def before_step(self, step, bridge):
        self.poll_saves()
        render_error = getattr(self.renderer, 'error', None)
        if render_error:
            self.ui.update(error='렌더링 오류: '+render_error)
            if self.collector.episode_active and not self.save_error:
                self.finish(False, 'render_error')
        if bridge is not None and hasattr(bridge, 'apply_workspace'):
            if self.workspace_bridge is not bridge:
                with self.ui.lock:
                    settings = copy.deepcopy(self.ui.state['workspace'])
                try:
                    bridge.apply_workspace(settings)
                except ValueError as exc:
                    # Never keep invalid saved limits when a new bridge is initialized.
                    settings = copy.deepcopy(self.ui.state['workspace_defaults'])
                    bridge.apply_workspace(settings)
                    self.ui.update(workspace=settings, error=str(exc)+' 기본 이동 범위를 사용합니다.')
                    self.ui.persist()
                self.workspace_bridge = bridge
                with self.ui.lock:
                    self.ui._pose_receiver = getattr(bridge, 'receiver', None)
                self.ui.update(workspace_ready=True, workspace_effective=bridge.workspace_status())
            if self.workspace_epoch != bridge.epoch:
                self.ui.update(workspace_effective=bridge.workspace_status())
                self.workspace_epoch = bridge.epoch
        if not self.save_error:
            self.boundary_step = step
        reset = False
        while True:
            try: command = self.ui.commands.get_nowait()
            except queue.Empty: break
            try:
                action = command['action']
                if self.saver is not None and self.saver.pending:
                    if action == 'toggle' and self.save_error:
                        self.saver.retry()
                        self.save_error = False
                        self.ui.update(phase='saving', error=None)
                        break
                    raise ValueError('이전 episode 저장을 먼저 완료하세요.')
                if action == 'toggle':
                    if self.collector.episode_active:
                        self.finish(bool(self.collector._metric_tracker and self.collector._metric_tracker.stable_success), 'manual_stop')
                        # Freeze the terminal observation and transfer the owned
                        # episode to the writer before restoring any scene state.
                        # This reset ends in idle; it must not start another episode.
                        self.start_after_reset = False
                        reset = True
                        self.ui.update(ready=False, phase='resetting', error=None)
                    else:
                        # Do not capture the pre-reset scene, even for the first observation.
                        if self.ui.assets.snapshot()['busy']:
                            raise ValueError('다운로드 완료 또는 취소 후 수집을 시작하세요.')
                        self.ui.store.folder(self.ui.snapshot()['folder'])
                        self.start_after_reset = True
                        reset = True
                        self.ui.update(ready=False, phase='resetting', error=None)
                elif action == 'reset':
                    # Preserve unfinished work as fail, then restore all robot/task state.
                    if self.collector.episode_active: self.finish(False, 'environment_reset')
                    self.start_after_reset = False
                    reset = True
                    self.ui.update(ready=False, phase='resetting', error=None)
                elif action == 'task':
                    if self.ui.assets.snapshot()['busy']: raise ValueError('다운로드 완료 또는 취소 후 task를 전환하세요.')
                    if self.collector.episode_active: raise ValueError('먼저 수집을 종료하세요.')
                    self.ui.restart_task = command['task']
                    self.ui.update(ready=False, phase='switching')
                    break
                elif action == 'workspace':
                    try:
                        if self.collector.episode_active or self.ui.state['phase'] != 'idle':
                            raise ValueError('수집을 종료한 뒤 이동 범위를 변경하세요.')
                        if bridge is None or not hasattr(bridge, 'apply_workspace'):
                            raise ValueError('로봇 이동 범위 준비 중입니다.')
                        settings = validate_workspace(command['settings'])
                        with self.ui.lock:
                            previous = copy.deepcopy(self.ui.state['workspace'])
                        bridge.apply_workspace(settings)
                        with self.ui.lock:
                            self.ui.state['workspace'] = settings
                            try:
                                self.ui.persist()
                            except OSError:
                                self.ui.state['workspace'] = previous
                                self.ui.persisted['workspace'] = copy.deepcopy(previous)
                                bridge.apply_workspace(previous)
                                raise
                        self.workspace_epoch = bridge.epoch
                        self.ui.update(workspace_effective=bridge.workspace_status(), error=None)
                    finally:
                        self.ui.update(workspace_pending=False)
            except Exception as exc: self.ui.update(error=str(exc))
            # Complete each transition before applying the next queued hotkey.
            break
        if bridge is not None and bridge.haptic_encoder is not None:
            # No filesystem/storage inspection on the Physics control path.
            with self.ui.lock:
                settings = {key:self.ui.state[key] for key in (
                    'haptic_force_min_n', 'haptic_force_max_n', 'haptic_enabled', 'haptic_max_power')}
            bridge.haptic_encoder.set_force_range(settings['haptic_force_min_n'], settings['haptic_force_max_n'])
            bridge.haptic_encoder.set_max_power(settings['haptic_max_power'])
            bridge.haptic_enabled = settings['haptic_enabled']
            bridge.haptic_gain = 1. if bridge.haptic_enabled else 0.
            if not bridge.haptic_enabled:
                bridge.haptic_encoder.reset()
        return reset

    def service_replay(self, bridge):
        """Pause simulation only for generation, not for precomputed playback."""
        jobs = getattr(self.ui, 'replays', None)
        if jobs is None:
            return False
        if jobs.needs_service and not self.replay_paused:
            if self.collector.episode_active or (self.saver and self.saver.pending):
                jobs.reject('수집·저장 종료 후 replay를 생성하세요.')
                return False
            self.replay_paused = True
            self.ui.update(phase='replay', ready=False)
            if bridge is not None:
                bridge.reset_input_state()
            if hasattr(self.renderer, 'suspend'):
                self.renderer.suspend()
            if self.gc_policy is not None:
                self.gc_policy.collect('replay_pause')
            jobs.start_pending()
        if self.replay_paused:
            if jobs.busy:
                return True
            if hasattr(self.renderer, 'start_worker'):
                self.renderer.start_worker()
            if self.renderer is not None:
                self.renderer.invalidate()
            if self.gc_policy is not None:
                self.gc_policy.collect('replay_resume')
            if bridge is not None:
                bridge.reset_input_state()
            self.replay_paused = False
            self.ui.update(phase='idle', ready=True)
        return False

    def after_step(self, step):
        if not self.collector.episode_active or self.save_error: return
        self.boundary_step = step + 1
        self.duration = max(0., (step + 1 - self.start_step)*self.sim.get_physics_dt())
        tracker = self.collector._metric_tracker
        success = bool(tracker and tracker.stable_success)
        self.ui.update(sim_seconds=self.duration, wall_seconds=time.monotonic()-self.wall_start,
                       frames=self.collector.frame_count, success=success)
        try:
            if success: self.finish(True, 'stable_success')
            elif self.collector.frame_count >= 1800: self.finish(False, 'frame_limit')
        except Exception as exc: self.ui.update(error=str(exc))

    def after_render(self, step):
        if self.renderer is not None:
            self.renderer.after_physics(step)
            return
        # Active collection already forwards the exact recorded RGB frames.
        # While idle (or frozen on save error), acquire only the wrist cameras.
        if (self.collector.episode_active and not self.save_error) or not self.ui.previews.needs_capture():
            return
        from dex_teleop.dashboard.camera_preview import WRIST_CAMERAS
        rig = getattr(self.collector, '_camera_rig', None)
        if rig is None:
            return
        try:
            frames = rig.capture(dt=self.sim.get_physics_dt(), camera_ids=set(WRIST_CAMERAS.values()))
        except Exception:
            frames = {}
        self.ui.previews.offer(frames, step)
