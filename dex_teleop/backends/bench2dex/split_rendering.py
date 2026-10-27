"""Snapshot rendering with step-stamped, lossless recorded-frame matching."""
from concurrent.futures import CancelledError
from types import SimpleNamespace
import threading
import time

from dex_teleop.backends.bench2dex.render_process import RenderProcess
from dex_teleop.backends.bench2dex.scene_state import StateRecorder, capture_scene


def resolve_recorded_frames(frames, timeout=120.):
    """Called only by the save worker. Failed captures remain visibly invalid."""
    deadline = time.monotonic()+timeout
    for frame in frames:
        future = getattr(frame, 'render_future', None)
        if future is None:
            continue
        try:
            result = future.result(timeout=max(0., deadline-time.monotonic()))
            if result['sim_step'] != frame.sim_step or result['physics_step'] != frame.physics_step:
                raise ValueError('Image/state step mismatch')
            if result.get('errors'):
                raise ValueError('; '.join(result['errors']))
            frame.camera = result['frames']
            frame.render_sim_step = result['sim_step']
            frame.render_completed_ns = result['completed_ns']
        except Exception as exc:
            frame.valid = False
            frame.errors.append('async camera: '+str(exc))
            frame.render_sim_step = -1
        finally:
            frame.render_future = None


class SplitRenderer(StateRecorder):
    def __init__(self, collector, sim, probe, previews, hold_targets, process_factory=RenderProcess):
        super().__init__(collector, sim, hold_targets)
        if collector._cfg.enabled('box2d') or collector._cfg.enabled('tactile'):
            raise ValueError('Split 모드는 RGB/depth/state 수집을 지원합니다. box2d/tactile은 serial로 실행하세요.')
        self.probe, self.previews = probe, previews
        self.stride = collector._step_stride
        self.factory = process_factory
        self.worker = None
        self.epoch = 0
        self.cache = None
        self.error = None
        self.closed = False
        self.published = -1
        self.render_count = 0
        self.lock = threading.RLock()
        self.start_worker()
        # The parent keeps camera definitions but releases render products.
        collector._camera_rig.close(keep_camera_prims=True)
        collector._split_rendering = True
        collector._camera_frame_provider = lambda step: {}
        collector._frame_observer = self.observe

    def start_worker(self):
        if self.worker is None:
            self.worker = self.factory(self.collector._replay_launch, self.signature)
            try:
                self.worker.ready.result(timeout=self.worker.startup_timeout+5)
            except BaseException:
                self.worker.close(); self.worker = None; raise

    def suspend(self):
        self.invalidate()
        if self.worker is not None:
            self.worker.close()
            self.worker = None

    def invalidate(self):
        with self.lock:
            self.epoch += 1
            self.cache = None
            self.published = -1

    def _request(self, step, required):
        token = (self.epoch, int(self.sim.current_time_step_index), int(step))
        if self.cache is not None and self.cache[0] == token and (self.cache[1] or not required):
            return self.cache[2], self.cache[3]
        states = capture_scene(self.collector._interactive_objects, self.hold_targets)
        request = dict(type='render', token=token, sim_step=step, physics_step=token[1],
                       sim_time=float(self.sim.current_time), state=states, snapshot_monotonic=time.monotonic())
        future = self.worker.submit(request, required=required)
        self.cache = (token, required, states, future)
        future.add_done_callback(lambda result: self._publish(result, token))
        return states, future

    def _publish(self, future, token):
        try:
            result = future.result()
            with self.lock:
                if self.closed or token[0] != self.epoch:
                    return
                if token[1] < self.published:
                    return
                if result.get('errors'):
                    raise RuntimeError('; '.join(result['errors']))
                # Never read the now-advanced simulation clock from this thread.
                self.render_count = getattr(self, 'render_count', 0)+1
                frozen_sim = SimpleNamespace(current_time_step_index=result['physics_step'],
                    current_time=result['sim_time'], render_calls=self.render_count,
                    snapshot_monotonic=result.get('snapshot_monotonic', time.monotonic()))
                self.probe.offer_task_frame(result['frames'].get('cam_task'), result['sim_step'], frozen_sim)
                self.previews.offer(result['frames'], result['sim_step'])
                self.published = token[1]
        except CancelledError:
            pass  # Only preview requests may be replaced.
        except Exception as exc:
            with self.lock:
                if not self.closed and token[0] == self.epoch:
                    self.error = str(exc)

    def observe(self, frame):
        states, future = self._request(frame.sim_step, required=True)
        frame.scene_state = states
        frame.physics_step = int(self.sim.current_time_step_index)
        frame.render_future = future

    def after_physics(self, step):
        if step % self.stride == 0 and self.worker is not None:
            self._request(step, required=self.collector.episode_active)

    def close(self):
        with self.lock:
            self.closed = True
        self.suspend()
