"""Bounded, CPU-only wrist previews from the collector's existing camera frames."""
import io
import threading
import time
import uuid

import numpy as np
from PIL import Image

WRIST_CAMERAS = {'left': 'cam_wrist_left', 'right': 'cam_wrist_right'}


class WristPreviews:
    def __init__(self, fps=15, size=(320, 240), quality=75):
        self.fps, self.size, self.quality = fps, size, quality
        self.condition = threading.Condition()
        self.enabled = set()
        self.stream = uuid.uuid4().hex
        self.frames = {side: self._empty() for side in WRIST_CAMERAS}
        self.last_request = 0.
        self.next_capture = 0.
        self.pending = None
        self.worker = None
        self.closed = False

    @staticmethod
    def _empty():
        return dict(jpeg=None, sequence=0, captured_at=0., sim_step=None, error=None)

    def configure(self, camera_ids):
        with self.condition:
            self.enabled = {side for side, name in WRIST_CAMERAS.items() if name in camera_ids}
            self.condition.notify_all()

    def needs_capture(self):
        with self.condition:
            return bool(self.enabled) and not self.closed and time.monotonic() >= self.next_capture

    def offer(self, camera_frames, sim_step):
        """Copy just the latest RGB pair. Never encode or wait for HTTP on the sim thread."""
        now = time.monotonic()
        with self.condition:
            if not self.enabled or self.closed or now < self.next_capture:
                return False
            self.next_capture = now + (1/self.fps if now-self.last_request < 3 else 1.)
            rgb, errors = {}, {}
            for side in self.enabled:
                frame = camera_frames.get(WRIST_CAMERAS[side])
                value = getattr(frame, 'rgb', None)
                if (not isinstance(value, np.ndarray) or value.dtype != np.uint8 or
                        value.ndim != 3 or value.shape[2] != 3 or not value.size):
                    errors[side] = 'RGB frame unavailable'
                else:
                    rgb[side] = value.copy(order='C')
            # One pending pair replaces the previous pair; frames never accumulate.
            self.pending = (self.stream, rgb, errors, int(sim_step), now)
            if self.worker is None:
                self.worker = threading.Thread(target=self._encode, name='wrist-jpeg', daemon=True)
                self.worker.start()
            self.condition.notify_all()
            return True

    def _encode(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.closed)
                if self.closed:
                    return
                stream, rgb, errors, sim_step, stamp = self.pending
                self.pending = None
            encoded = {}
            for side, value in rgb.items():
                try:
                    image = Image.fromarray(value)
                    image.thumbnail(self.size, Image.Resampling.BILINEAR)
                    buf = io.BytesIO()
                    image.save(buf, format='JPEG', quality=self.quality)
                    encoded[side] = buf.getvalue()
                except Exception as exc:
                    errors[side] = str(exc)
            with self.condition:
                if stream != self.stream or self.closed:
                    continue  # A reset happened while JPEG encoding was in flight.
                for side in self.enabled:
                    old = self.frames[side]
                    self.frames[side] = dict(jpeg=encoded.get(side), sequence=old['sequence']+1,
                                             captured_at=stamp, sim_step=sim_step, error=errors.get(side))
                self.condition.notify_all()

    def read(self, side, after=-1, stream=None, timeout=.35):
        if side not in WRIST_CAMERAS:
            raise ValueError('Unknown wrist camera')
        with self.condition:
            self.last_request = time.monotonic()
            if side not in self.enabled:
                return None
            self.condition.wait_for(
                lambda: self.closed or (self.frames[side]['jpeg'] is not None and
                                        (stream != self.stream or self.frames[side]['sequence'] > after)),
                timeout=timeout,
            )
            frame = self.frames[side]
            if self.closed or frame['jpeg'] is None or (stream == self.stream and frame['sequence'] <= after):
                return None
            return dict(frame, stream=self.stream)

    def snapshot(self):
        now = time.monotonic()
        with self.condition:
            return dict(enabled=bool(self.enabled), stream=self.stream, fps_limit=self.fps,
                        streams={side: dict(camera_id=WRIST_CAMERAS[side], enabled=side in self.enabled,
                                           ready=frame['jpeg'] is not None, sequence=frame['sequence'],
                                           age_ms=round((now-frame['captured_at'])*1000) if frame['captured_at'] else None,
                                           sim_step=frame['sim_step'], error=frame['error'])
                                 for side, frame in self.frames.items()})

    def reset(self):
        with self.condition:
            self.stream = uuid.uuid4().hex
            self.frames = {side: self._empty() for side in WRIST_CAMERAS}
            self.pending = None
            self.next_capture = 0.
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending = None
            self.condition.notify_all()
        if self.worker:
            self.worker.join(timeout=1)
