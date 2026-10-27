"""Isaac viewport capture and simulation timing for the browser dashboard."""
import ctypes
import io
import json
import math
import threading
import time
from pathlib import Path
from PIL import Image
from dex_teleop.dashboard.http import LatestFrame, serve_viewer
from dex_teleop.paths import WORKSPACE


class BrowserProbe:
    def __init__(self, output_dir, port=8080, fps=24, quality=75, orbit=False, debug_mode=False,
                 use_task_camera=False, timing_window=120):
        from dex_teleop.core.step_timing import RollingStepTiming
        self.step_timing = RollingStepTiming(timing_window)
        self.next_timing_publish = 0.
        self.output = Path(output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.fps, self.quality, self.orbit = fps, quality, orbit and not debug_mode
        self.debug_mode = debug_mode
        self.use_task_camera = use_task_camera
        self.task_snapshot = None
        self.snapshot_requested = False
        self.force_camera = False
        import yaml
        view=yaml.safe_load((WORKSPACE/'configs/bench2dex/browser_view.yaml').read_text())['camera']
        self.camera_eye,self.camera_target=view['eye'],view['target']
        self.store = LatestFrame()
        self.store.metadata['debug_mode'] = debug_mode
        self.store.publish_timing(self.step_timing.snapshot())
        self.server = serve_viewer(self.store, port)
        self.raw_condition = threading.Condition()
        self.raw = None
        self.pending = False
        self.capture_handle = None
        self.render_calls = 0
        self.next_capture = 0.0
        self.deadline = time.monotonic()
        self.first_frame = True
        threading.Thread(target=self._encode, name='browser-jpeg', daemon=True).start()
        print(f'[BROWSER] Listening only on http://127.0.0.1:{port}', flush=True)

    def reset_timing(self, now=None):
        self.step_timing.reset(now)
        self.next_timing_publish = 0.
        self.store.publish_timing(self.step_timing.snapshot())

    def record_step_timing(self, physics_dt, now):
        self.step_timing.record(physics_dt, now)
        # Sampling is O(1) per step; allocate/share a report at most four times
        # per second. The browser reuses its existing 1 Hz /health poll.
        if now >= self.next_timing_publish:
            self.store.publish_timing(self.step_timing.snapshot())
            self.next_timing_publish = now + .25

    def capture_snapshot(self, sim, timeout=30.):
        """Render the initialized scene into exactly one JPEG before the debug wait.

        Viewport readback is asynchronous and needs a few Kit renders to complete.
        No Physics steps are performed here; capture remains bounded on failure.
        """
        if not self.debug_mode:
            raise RuntimeError('Static capture is only available in DEBUG_MODE')
        baseline = self.store.sequence
        self.snapshot_requested = True
        self.force_camera = True
        self.next_capture = 0.
        deadline = time.monotonic() + timeout
        renders = 0
        try:
            while time.monotonic() < deadline and renders < 120:
                if getattr(self, 'use_task_camera', False) and self.task_snapshot is not None:
                    self.task_snapshot()
                else:
                    sim.render()
                renders += 1
                with self.store.condition:
                    self.store.condition.wait_for(
                        lambda: self.store.sequence > baseline or self.store.error, timeout=.05)
                    if self.store.error:
                        raise RuntimeError('Debug snapshot failed: ' + self.store.error)
                    if self.store.sequence > baseline:
                        report = dict(self.store.health(), debug_capture_renders=renders)
                        (self.output/'debug-snapshot.json').write_text(json.dumps(report, indent=2)+'\n')
                        return report
            raise RuntimeError('Debug snapshot did not finish within the render/time budget')
        finally:
            self.snapshot_requested = False

    def _encode(self):
        while True:
            with self.raw_condition:
                self.raw_condition.wait_for(lambda: self.raw is not None)
                data, width, height, stamp, metadata = self.raw
                self.raw = None
            try:
                if metadata.get('source_camera') == 'cam_task':
                    image = Image.fromarray(data, 'RGB')
                else:
                    stride = len(data)//height
                    image = Image.frombytes('RGBA', (width, height), data, 'raw', 'RGBA', stride).convert('RGB')
                buf = io.BytesIO()
                start = time.monotonic()
                image.save(buf, format='JPEG', quality=self.quality)
                metadata['encode_ms'] = round((time.monotonic()-start)*1000, 2)
                metadata['resolution'] = [width, height]
                jpeg = buf.getvalue()
                self.store.publish(jpeg, stamp, metadata)
                if self.first_frame:
                    (self.output/'first-frame.jpg').write_bytes(jpeg)
                    self.first_frame = False
                    print(f'[BROWSER] First JPEG ready: {width}x{height}, {len(jpeg)} bytes', flush=True)
            except Exception as exc:
                self.store.error = str(exc)
                print(f'[BROWSER] JPEG_ERROR: {exc}', flush=True)

    def _captured(self, buffer, buffer_size, width, height, byte_format):
        try:
            if buffer_size < width*height*4:
                raise ValueError(f'Unexpected LDR capture format: {byte_format}, bytes={buffer_size}')
            if type(buffer).__name__ == 'PyCapsule':
                get_name = ctypes.pythonapi.PyCapsule_GetName
                get_name.argtypes, get_name.restype = [ctypes.py_object], ctypes.c_char_p
                get_ptr = ctypes.pythonapi.PyCapsule_GetPointer
                get_ptr.argtypes, get_ptr.restype = [ctypes.py_object, ctypes.c_char_p], ctypes.c_void_p
                data = ctypes.string_at(get_ptr(buffer, get_name(buffer)), buffer_size)
            else:
                data = bytes(memoryview(buffer))
            with self.raw_condition:
                self.raw = (data, width, height, time.monotonic(), dict(self.capture_metadata))
                self.raw_condition.notify()
        except Exception as exc:
            self.store.error = str(exc)
            print(f'[BROWSER] CAPTURE_ERROR: {exc}', flush=True)
        finally:
            self.pending = False

    def offer_task_frame(self, frame, step, sim):
        """Share the captured RGB array; only the worker encodes it as JPEG."""
        if frame is None or frame.rgb is None:
            self.store.error = 'cam_task RGB is unavailable'
            return
        if self.debug_mode and not self.snapshot_requested:
            return
        now = time.monotonic()
        # Every regular render is 20 Hz. Avoid a wall-time gate at the same
        # frequency: small scheduling jitter would otherwise drop alternate frames.
        metadata = dict(source_camera='cam_task', physics_step=int(sim.current_time_step_index),
                        sim_step=step, sim_time=float(sim.current_time), render_calls=getattr(sim, 'render_calls', self.render_calls),
                        capture_fps_limit=20, debug_mode=self.debug_mode, diagnostic_camera_orbit=False)
        with self.raw_condition:
            self.raw = (frame.rgb, frame.rgb.shape[1], frame.rgb.shape[0], getattr(sim, 'snapshot_monotonic', now), metadata)
            self.raw_condition.notify()
        self.store.error = None

    def _report_health(self):
        if self.render_calls % 120 == 0:
            report = self.store.health()
            temp = self.output/'metrics.tmp'
            temp.write_text(json.dumps(report, indent=2)+'\n')
            temp.replace(self.output/'metrics.json')
            print('[BROWSER] '+json.dumps(report), flush=True)

    def after_render(self, sim):
        from omni.kit.viewport.utility import get_active_viewport, capture_viewport_to_buffer
        from isaacsim.core.utils.viewports import set_camera_view
        self.render_calls += 1
        viewport = get_active_viewport()
        if getattr(self, 'use_task_camera', False):
            # Sensor render products continue updating. The unused native
            # viewport must not render a fourth image for the browser.
            if viewport is not None:
                viewport.updates_enabled = False
            self._report_health()
            return
        if viewport is None:
            raise RuntimeError('Browser viewer needs an active rendering viewport')
        viewport.updates_enabled = True
        if self.render_calls == 1:
            viewport.resolution = (1280, 720)
        if self.force_camera or self.orbit or self.render_calls in (1, 30):
            if self.orbit:
                angle=.6+.0015*self.render_calls
                eye=[2.8*math.cos(angle),2.8*math.sin(angle),2.5]
            else:eye=self.camera_eye
            set_camera_view(eye=eye,target=self.camera_target)
            self.force_camera = False
        now = time.monotonic()
        # No viewers: capture at 1 Hz for readiness; connected viewers: configured rate.
        period = 1/self.fps if now-self.store.last_request < 3 else 1.0
        allow_capture = not self.debug_mode or self.snapshot_requested
        if allow_capture and not self.pending and now >= self.next_capture:
            self.snapshot_requested = False
            self.next_capture = now + period
            self.pending = True
            self.capture_metadata = {'source_camera':'viewport',
                                     'physics_step':int(sim.current_time_step_index), 'sim_time':float(sim.current_time),
                                     'render_calls':self.render_calls, 'capture_fps_limit':self.fps,
                                     'diagnostic_camera_orbit':self.orbit,'camera_eye':self.camera_eye,'camera_target':self.camera_target,
                                     'debug_mode':self.debug_mode}
            self.capture_handle = capture_viewport_to_buffer(viewport, self._captured)
        self._report_health()
        # Physics pacing belongs to the main loop, never the render callback.
