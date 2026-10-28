"""Publish the latest immutable camera frames without blocking a simulator loop."""
import io
import threading
import time
from types import SimpleNamespace

from PIL import Image


class FramePublisher:
    def __init__(self, latest, previews, quality=75):
        self.latest, self.previews, self.quality = latest, previews, quality
        self.condition = threading.Condition()
        self.pending = None
        self.closed = False
        self.generation = 0
        self.thread = threading.Thread(target=self._run, name='dex-teleop-jpeg', daemon=True)
        self.thread.start()

    def offer(self, cameras, step, **metadata):
        stamp = time.monotonic()
        # Arrays returned by Renderer are owned snapshots, never mutated again.
        self.previews.offer({key:SimpleNamespace(rgb=value) for key,value in cameras.items()}, step)
        with self.condition:
            self.pending = (cameras['cam_task'], stamp, dict(physics_step=int(step), **metadata), self.generation)
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed: return
                rgb, stamp, metadata, generation = self.pending
                self.pending = None
            output = io.BytesIO()
            Image.fromarray(rgb).save(output, format='JPEG', quality=self.quality)
            with self.condition:
                if not self.closed and generation == self.generation:
                    self.latest.publish(output.getvalue(), stamp, metadata)

    def reset(self):
        with self.condition:
            self.generation += 1
            self.pending = None
        self.previews.reset()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending = None
            self.condition.notify()
        self.thread.join(timeout=2)
