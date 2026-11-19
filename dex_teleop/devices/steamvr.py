"""Latest-only OpenVR poses; no compositor, HMD rendering, or SteamVR file edits."""
import copy
import threading
import time

from .pose_math import tracking_matrix


class SteamVRTrackerReader:
    def __init__(self, hz=90., vr_module=None):
        self.hz, self.vr = float(hz), vr_module
        if not 10 <= self.hz <= 240:
            raise ValueError('Tracker polling frequency must be within 10..240 Hz')
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state = dict(generation=0, captured_at=0., devices={}, error='Waiting for SteamVR')

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='steamvr-tracker', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if hasattr(self, 'thread'):
            self.thread.join(timeout=2)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def read_once(self, system):
        vr = self.vr
        stamp = time.monotonic()
        poses = system.getDeviceToAbsoluteTrackingPose(vr.TrackingUniverseStanding, 0., vr.k_unMaxTrackedDeviceCount)
        devices = {}
        for index, pose in enumerate(poses):
            if system.getTrackedDeviceClass(index) != vr.TrackedDeviceClass_GenericTracker:
                continue  # A null HMD at device 0 is never treated as a glove tracker.
            if not pose.bDeviceIsConnected:
                continue
            serial = system.getStringTrackedDeviceProperty(index, vr.Prop_SerialNumber_String)
            if not isinstance(serial, str) or not serial or len(serial) > 128:
                continue
            try:
                model = system.getStringTrackedDeviceProperty(index, vr.Prop_ModelNumber_String)
            except Exception:
                model = 'Unknown model'
            matrix = None
            valid = bool(pose.bPoseIsValid and pose.eTrackingResult == vr.TrackingResult_Running_OK)
            if valid:
                try:
                    matrix = tracking_matrix([[pose.mDeviceToAbsoluteTracking.m[r][c]
                                               for c in range(4)] for r in range(3)]).tolist()
                except ValueError:
                    valid = False
            devices[serial] = dict(serial=serial, model=str(model)[:160], index=index,
                valid=valid, tracking_result=int(pose.eTrackingResult), matrix=matrix)
        return dict(captured_at=stamp, devices=devices, error=None)

    def _run(self):
        generation = 0
        while not self.stop_event.is_set():
            initialized = False
            try:
                if self.vr is None:
                    import openvr
                    self.vr = openvr
                vr = self.vr
                system = vr.init(vr.VRApplication_Background)
                initialized = True
                generation += 1
                events = {getattr(vr, name, -1) for name in (
                    'VREvent_ChaperoneUniverseHasChanged', 'VREvent_ChaperoneDataHasChanged',
                    'VREvent_ChaperoneRoomSetupCommitted', 'VREvent_StandingZeroPoseReset')}
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    event = vr.VREvent_t()
                    for _ in range(64):
                        if not system.pollNextEvent(event):
                            break
                        if event.eventType in events:
                            generation += 1
                    state = self.read_once(system)
                    with self.lock:
                        self.state = dict(state, generation=generation)
                    self.stop_event.wait(max(0., 1/self.hz-(time.monotonic()-started)))
            except Exception as exc:
                with self.lock:
                    self.state = dict(generation=generation, captured_at=0., devices={},
                                      error=f'SteamVR: {type(exc).__name__}: {exc}')
                self.stop_event.wait(1.)
            finally:
                if initialized:
                    try:
                        self.vr.shutdown()
                    except Exception as exc:
                        with self.lock:
                            self.state = dict(generation=generation, captured_at=0., devices={},
                                              error=f'SteamVR shutdown: {type(exc).__name__}: {exc}')
