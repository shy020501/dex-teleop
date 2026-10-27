"""MANUS Python SDK 3.2.1 Local Mode. Proprietary SDK is installed by the operator."""
import copy
import threading
import time
from dex_teleop.devices.manus.skeleton import canonicalize_raw


class ManusFingerProvider:
    def __init__(self, tracking_timeout_s=.2):
        # Import on demand so mocks never require proprietary vendor binaries.
        from manus_sdk import ManusSDK, lib, ffi
        from manus_sdk.generated._enums import SDKReturnCode, AxisView, AxisPolarity, Side, HandMotion
        self.sdk, self.lib, self.ffi = ManusSDK(), lib, ffi
        self.enums = (SDKReturnCode, AxisView, AxisPolarity, Side, HandMotion)
        self.lock = threading.Lock()
        self.raw = {}
        self.timeout = tracking_timeout_s
        self.error = None
        self.running = False
        self.last_connect_attempt = 0.
        self.connected = False

    def _check(self, code, operation):
        if int(code) != int(self.enums[0].Success):
            raise RuntimeError(f'MANUS {operation} failed: SDKReturnCode={int(code)}; check Core 3.2.1 and SDK license')

    def start(self):
        ret, view, up, side, motion = self.enums
        self._check(self.lib.CoreSdk_InitializeCore(), 'InitializeCore (Local Mode)')
        self._check(self.sdk.register_raw_skeleton_callback(self._callback), 'raw skeleton callback')
        coord = self.ffi.new('CoordinateSystemVUH*')
        coord.view, coord.up, coord.handedness, coord.unitScale = view.XFromViewer, up.PositiveZ, side.Right, 1.
        self._check(self.lib.CoreSdk_InitializeCoordinateSystemWithVUH(coord[0], True), 'world coordinate initialization')
        self.running = True
        self.connect_thread = threading.Thread(target=self._connect_loop, name='manus-local-connect', daemon=True)
        self.connect_thread.start()

    def _connect_loop(self):
        while self.running:
            with self.lock:
                age = time.monotonic()-max((v[1] for v in self.raw.values()), default=0.)
            if self.connected:
                session = self.ffi.new('uint32_t*')
                self.connected = int(self.lib.CoreSdk_GetSessionId(session)) == int(self.enums[0].Success)
            if not self.connected:
                try:
                    self._check(self.lib.CoreSdk_LookForHosts(1, True), 'loopback host discovery')
                    count = self.ffi.new('uint32_t*')
                    self._check(self.lib.CoreSdk_GetNumberOfAvailableHostsFound(count), 'host count')
                    if count[0] > 0:
                        hosts = self.ffi.new('ManusHost[]', count[0])
                        self._check(self.lib.CoreSdk_GetAvailableHostsFound(hosts, count[0]), 'host list')
                        self._check(self.lib.CoreSdk_ConnectToHost(hosts[0]), 'connect local Core')
                        self._check(self.lib.CoreSdk_SetRawSkeletonHandMotion(self.enums[4].IMU), 'raw hand motion IMU')
                        self.connected = True
                        print('[MANUS] Connected to local Core; waiting for raw skeleton.', flush=True)
                except Exception as exc:
                    self.connected = False
                    self.error = str(exc)
            time.sleep(1)

    def _callback(self, stream):
        # SDK thread: copy CFFI-owned memory before returning. No network, IK or disk IO.
        try:
            if not 0 <= stream.skeletonsCount <= 32:
                return
            copied = {}
            for index in range(stream.skeletonsCount):
                info = self.ffi.new('RawSkeletonInfo*')
                self._check(self.lib.CoreSdk_GetRawSkeletonInfo(index, info), 'raw info')
                if not 1 <= info.nodesCount <= 256:
                    raise ValueError('Unexpected MANUS raw node count')
                nodes = self.ffi.new('SkeletonNode[]', info.nodesCount)
                meta = self.ffi.new('NodeInfo[]', info.nodesCount)
                self._check(self.lib.CoreSdk_GetRawSkeletonData(index, nodes, info.nodesCount), 'raw data')
                self._check(self.lib.CoreSdk_GetRawSkeletonNodeInfoArray(info.gloveId, meta, info.nodesCount), 'raw node metadata')
                entries = []
                # Join node and semantic metadata by ID, not by an assumed 25-node order.
                metadata = {int(meta[j].nodeId):meta[j] for j in range(info.nodesCount)}
                for j in range(info.nodesCount):
                    n = nodes[j]
                    m = metadata[int(n.id)]
                    p, q = n.transform.position, n.transform.rotation
                    entries.append({'id':int(n.id), 'parent_id':int(m.parentId), 'chain_type':int(m.chainType),
                                    'finger_joint_type':int(m.fingerJointType), 'side':int(m.side),
                                    'position':[float(p.x),float(p.y),float(p.z)],
                                    'quaternion_xyzw':[float(q.x),float(q.y),float(q.z),float(q.w)]})
                copied[int(info.gloveId)] = (entries, time.monotonic())
            with self.lock:
                self.raw.update(copied)  # callbacks may report only one glove
        except Exception as exc:
            self.error = str(exc)

    def get_tracking(self):
        with self.lock:
            raw = copy.deepcopy(self.raw)
        result, ambiguous = {}, set()
        for glove, (nodes, stamp) in raw.items():
            age = time.monotonic()-stamp
            try:
                side, data = canonicalize_raw(nodes)
                data.update(age_ms=age*1000, valid=age <= self.timeout, glove_id=glove)
                if side in result:
                    ambiguous.add(side)
                    raise ValueError(f'Multiple {side} gloves; disconnect unused glove or add explicit glove selection')
                result[side] = data
            except ValueError as exc:
                self.error = f'Glove {glove}: {exc}'
        for side in ambiguous:
            result.pop(side, None)
        return result

    def set_haptics(self,glove_id,powers):
        import math
        values=[float(v) for v in powers]
        if len(values)!=5 or not all(math.isfinite(v) and 0<=v<=1 for v in values):
            raise ValueError('Haptics requires five finite powers in [0,1]')
        if not hasattr(self.lib,'CoreSdk_VibrateFingersForGlove'):
            raise RuntimeError('MANUS SDK lacks CoreSdk_VibrateFingersForGlove; use matching SDK 3.2.1')
        array=self.ffi.new('float[5]',values)
        self._check(self.lib.CoreSdk_VibrateFingersForGlove(int(glove_id),array),'VibrateFingersForGlove')

    def diagnostics(self):
        with self.lock:
            return {'connected':self.connected, 'error':self.error,
                    'gloves':{str(g):{'raw_node_count':len(v[0]), 'age_ms':(time.monotonic()-v[1])*1000,
                                      'nodes':v[0]} for g,v in self.raw.items()}}

    def stop(self):
        self.running = False
        if hasattr(self, 'connect_thread'):
            self.connect_thread.join(timeout=3)
        self.lib.CoreSdk_ShutDown()
