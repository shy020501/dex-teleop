"""Main-thread camera scheduling and bounded wall-clock pacing.

All USD, Physics and camera APIs stay on the simulation thread. Dashboard main
and wrist previews share the recording cameras. JPEG encoding happens elsewhere.
"""
import time


class StepPacer:
    def __init__(self, dt, clock=time.monotonic, sleep=time.sleep):
        self.dt, self.clock, self.sleep = dt, clock, sleep
        self.reset()

    def reset(self):
        self.deadline = self.clock()

    def wait(self):
        self.deadline += self.dt
        now = self.clock()
        # Catch up across the render/non-render steps of one control interval.
        # Never accumulate a long catch-up burst after a reset, stall or pause.
        if now - self.deadline > 6 * self.dt:
            self.deadline = now
        delay = max(0., self.deadline - now)
        if delay > .0005:
            self.sleep(delay)
        return delay


class DashboardRenderer:
    def __init__(self, collector, sim, probe, previews):
        self.collector, self.sim, self.probe, self.previews = collector, sim, probe, previews
        self.stride = collector._step_stride
        self.token, self.frames = None, {}
        self.errors = []
        collector._camera_frame_provider = self.capture
        probe.task_snapshot = lambda: self.capture(int(sim.current_time_step_index), force=True)

    def invalidate(self):
        self.token, self.frames, self.errors = None, {}, []

    def capture(self, step, force=False):
        # The local episode step alone is insufficient: reset warmup advances
        # Physics without changing main.py's step counter.
        token = (int(self.sim.current_time_step_index), step)
        rig = self.collector._camera_rig
        if force or token != self.token:
            self.collector.sync_mounted_camera_poses()
            self.sim.render()
            elapsed_steps = token[0] - self.token[0] if self.token is not None else self.stride
            self.frames = rig.capture(dt=self.sim.get_physics_dt() * max(1, elapsed_steps))
            self.errors = list(rig.last_capture_errors)
            self.token = token
            if getattr(self.probe, 'use_task_camera', False):
                self.probe.offer_task_frame(self.frames.get('cam_task'), step, self.sim)
            self.previews.offer(self.frames, step)
        # Collection and preview share the same immutable CPU frame arrays.
        rig._last_capture_errors = list(self.errors)
        return self.frames

    def after_physics(self, step):
        if step % self.stride == 0:
            self.capture(step)
