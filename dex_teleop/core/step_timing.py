"""Bounded, constant-time averages of simulation dt and completed loop periods.

Only the simulation thread samples this accumulator. HTTP readers receive a
separate snapshot; no camera, tensor, GPU synchronization or file I/O is needed.
"""
from collections import deque


class RollingStepTiming:
    def __init__(self, window=120):
        if type(window) is not int or window < 1:
            raise ValueError('Timing window must be a positive integer')
        self.window = window
        self.reset()

    def reset(self, now=None):
        """Start a new window after initialization, reset or replay generation."""
        self.samples = deque()
        self.sim_seconds = self.wall_seconds = 0.
        self.last_completed_at = now

    def record(self, physics_dt, now):
        """Complete one main-loop step, including control, rendering and pacing.

        Consecutive completion timestamps also include work between iterations.
        After a reset without a timestamp, the first completion sets the origin.
        """
        if self.last_completed_at is None:
            self.last_completed_at = now
            return
        elapsed = now - self.last_completed_at
        if elapsed <= 0:
            return
        self.last_completed_at = now
        if len(self.samples) == self.window:
            old_sim, old_wall = self.samples.popleft()
            self.sim_seconds -= old_sim
            self.wall_seconds -= old_wall
        self.samples.append((physics_dt, elapsed))
        self.sim_seconds += physics_dt
        self.wall_seconds += elapsed

    def snapshot(self):
        count = len(self.samples)
        return dict(
            window=self.window, samples=count,
            physics_period_ms=1000*self.sim_seconds/count if count else None,
            wall_period_ms=1000*self.wall_seconds/count if count else None,
            physics_hz=count/self.sim_seconds if count else None,
            wall_hz=count/self.wall_seconds if count else None,
            real_time_factor=self.sim_seconds/self.wall_seconds if count else None,
            sampled_at=self.last_completed_at if count else None,
        )
