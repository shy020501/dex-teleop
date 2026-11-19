"""Calibrated relative 6DoF input, independent of simulator and robot joints."""
import copy
import math
import time
import numpy as np
from dex_teleop.core.input import HandPoseProvider
from .pose_math import rotation, quaternion, tracking_matrix, default_tracking_basis


class VivePoseProvider(HandPoseProvider):
    def __init__(self, config, reader, clock=time.monotonic):
        self.reader, self.clock = reader, clock
        cfg = config.get('vive', {})
        self.timeout = float(cfg.get('tracking_timeout_s', .15))
        self.scale = float(cfg.get('position_scale', 1.))
        yaw = float(cfg.get('world_yaw_deg', 0.))
        if not (.02 <= self.timeout <= .5 and .05 <= self.scale <= 2 and math.isfinite(yaw)):
            raise ValueError('Invalid Vive tracking timeout, scale or heading')
        self.basis = default_tracking_basis(yaw)
        self.mapping = dict(cfg.get('tracker_serials', {}))
        if (set(self.mapping)-{'right','left'} or any(value is not None and
                (not isinstance(value, str) or not 1 <= len(value) <= 128) for value in self.mapping.values())):
            raise ValueError('Tracker serials must map right/left to a serial string or null')
        serials = [value for value in self.mapping.values() if value]
        if len(set(serials)) != len(serials):
            raise ValueError('Each hand must use a different Tracker serial')
        offsets = cfg.get('tracker_to_hand_m', {})
        self.offsets = {s:np.asarray(offsets.get(s, [0.,0.,0.]), dtype=float) for s in ('right','left')}
        if any(v.shape != (3,) or not np.isfinite(v).all() or np.linalg.norm(v) > .5 for v in self.offsets.values()):
            raise ValueError('Tracker-to-hand offset must be a finite 3-vector within 0.5 m')
        self.states, self.references, self.last_samples = {}, {}, {}
        self.identity = self.generation = self.last_command = None
        self.reset_count = 0
        self.armed = self.supported = self.server_ready = False
        self.pending_alignment = None
        self.mode, self.message = 'waiting', 'Waiting for simulation'
        self.command_error = None
        self.selected = 'both'
        self.raw = dict(devices={}, captured_at=0., generation=0, error=None)

    def synchronize(self, status, force=False):
        identity = (status.get('server_id'), status.get('epoch'))
        changed = identity != self.identity
        self.supported = 'vive' in status.get('teleop', {}).get('pose_sources', [])
        self.server_ready = bool(status.get('ready'))
        reset_count = status.get('environment_reset_count', 0)
        if changed:
            same_server = self.identity is not None and identity[0] == self.identity[0]
            expected_reset = same_server and reset_count > self.reset_count
            if not expected_reset:
                self.armed = False
            self.references.clear()
            self.last_samples.clear()
            self.states.clear()
            self.pending_alignment = 'home' if expected_reset or self.identity is None else 'current'
            self.identity = identity
        self.reset_count = reset_count
        for side in set(self.states)-set(status.get('hands', {})):
            del self.states[side]
        for side, remote in status.get('hands', {}).items():
            if side not in self.states:
                self.states[side] = dict(active=False, home=remote.get('home_event_ack', 0),
                    reset=remote.get('reset_event_ack', 0), pose=dict(source='vive', valid=False,
                    age_ms=10000., position_active=False, position_xyz=list(remote['current_position_xyz']),
                    orientation_wxyz=list(remote['current_orientation_wxyz'])))
            self.states[side]['remote'] = copy.deepcopy(remote)
        if force:
            self.connection_lost()
        return changed

    def server_paused(self):
        # Expected reset is distinct from losing the network connection.
        self.server_ready = False
        for state in self.states.values():
            state['active'] = False

    def connection_lost(self):
        self.armed = False
        self.references.clear()
        self.pending_alignment = None
        self.last_samples.clear()
        self.mode, self.message = 'paused', 'Connection lost; align and start again'
        for state in self.states.values():
            state['active'] = False
            state['pose']['position_active'] = False

    def _samples(self):
        self.raw = self.reader.snapshot()
        age = max(0., self.clock()-self.raw['captured_at'])
        if self.raw.get('error'):
            raise ValueError(self.raw['error'])
        if age > self.timeout:
            raise ValueError('Tracker pose is stale')
        if self.generation is not None and self.raw['generation'] != self.generation:
            self.generation = self.raw['generation']
            self.references.clear()
            self.pending_alignment = None
            raise ValueError('SteamVR origin changed; align again')
        self.generation = self.raw['generation']
        devices = self.raw['devices']
        if not any(self.mapping.values()) and len(devices) == 1 and len(self.states) == 1:
            self.mapping[next(iter(self.states))] = next(iter(devices))
        samples = {}
        for side in self.states:
            serial = self.mapping.get(side)
            if not serial:
                continue
            device = devices.get(serial)
            if not device or not device['valid']:
                raise ValueError(f'{side}: Tracker tracking unavailable ({serial})')
            matrix = tracking_matrix(device['matrix'])
            r = matrix[:,:3]
            p = matrix[:,3]+r@self.offsets[side]
            samples[side] = (p, r, age)
        if not samples:
            raise ValueError('Assign Tracker serials to the enabled hand(s)')
        return samples

    def _anchor(self, samples, reference='current'):
        self.references.clear()
        for side, (p, r, _) in samples.items():
            remote = self.states[side]['remote']
            prefix = 'home' if reference == 'home' else 'current'
            self.references[side] = dict(p=p.copy(), r=r.copy(),
                robot_p=np.asarray(remote[prefix+'_position_xyz'], dtype=float),
                robot_r=rotation(remote[prefix+'_orientation_wxyz']))
        self.last_samples = {s:(p.copy(),r.copy()) for s,(p,r,_) in samples.items()}
        self.pending_alignment = None

    def command(self, command):
        if not command or command.get('id') == self.last_command:
            return
        self.last_command = command.get('id')
        # Stop is safe across epochs, including a reset/labeling pause. Never
        # discard an operator's stop just because the server already reset.
        if command.get('action') == 'stop':
            self.armed = False
            self.command_error = None
            self.mode, self.message = 'paused', 'Tracker control paused'
            for state in self.states.values():
                state['active'] = False
                state['pose']['position_active'] = False
            return
        if self.identity != (command.get('server_id'), command.get('epoch')):
            return
        action = command.get('action')
        try:
            self.command_error = None
            if not self.supported or not self.server_ready:
                raise ValueError('Server does not support Tracker control or is not ready')
            samples = self._samples()
            if action == 'forward':
                side = 'right' if 'right' in samples else next(iter(samples))
                if side not in self.references:
                    raise ValueError('Align first, then move the hand forward at least 10 cm')
                delta = samples[side][0]-self.references[side]['p']
                delta[1] = 0.
                if np.linalg.norm(delta) < .1:
                    raise ValueError('Move the hand forward at least 10 cm from its aligned position')
                forward = delta/np.linalg.norm(delta)
                up = np.array([0.,1.,0.])
                self.basis = np.stack([np.cross(forward, up), forward, up])
                self._anchor(samples)
                self.armed = False
            elif action in ('align', 'start'):
                self._anchor(samples)
                self.armed = action == 'start'
            else:
                raise ValueError('Unknown Tracker command')
            self.mode = 'active' if self.armed else 'paused'
            self.message = 'Tracker control active' if self.armed else 'Aligned; ready to start'
        except ValueError as exc:
            self.armed = False
            self.command_error = str(exc)
            self.mode, self.message = 'waiting', str(exc)

    def update(self, held_keys=(), dt=0., enabled=True):
        # Explicit Start/Stop replaces keyboard's browser-focus activation policy.
        for state in self.states.values():
            state['active'] = False
            state['pose'].update(position_active=False, valid=False)
        if not self.supported or not self.server_ready:
            self.mode = 'waiting'
            self.message = 'Waiting for simulation' if not self.server_ready else 'This server has not enabled Vive control'
            return
        try:
            samples = self._samples()
            if self.pending_alignment:
                self._anchor(samples, self.pending_alignment)
            for side, (p, r, age) in samples.items():
                previous = self.last_samples.get(side)
                if self.armed and previous is not None:
                    angle = np.arccos(np.clip((np.trace(r@previous[1].T)-1)/2, -1., 1.))
                    if np.linalg.norm(p-previous[0]) > .25 or angle > np.deg2rad(120):
                        raise ValueError('Tracker pose jumped; align and start again')
                self.last_samples[side] = (p.copy(), r.copy())
                reference = self.references.get(side)
                if reference is None:
                    continue
                state = self.states[side]
                position = reference['robot_p']+self.scale*self.basis@(p-reference['p'])
                attitude = self.basis@r@reference['r'].T@self.basis.T@reference['robot_r']
                remote = state['remote']
                position = np.clip(position, remote['workspace_min_xyz'], remote['workspace_max_xyz'])
                state['pose'].update(position_xyz=position.tolist(), orientation_wxyz=quaternion(attitude).tolist(),
                    valid=True, age_ms=min(10000., age*1000), position_active=self.armed)
                state['active'] = self.armed
            self.mode = 'active' if self.armed else 'paused'
            if self.references:
                self.message = 'Tracker control active' if self.armed else 'Aligned; ready to start'
        except ValueError as exc:
            self.armed = False
            self.mode, self.message = 'tracking_lost', str(exc)
            for state in self.states.values():
                state['active'] = False
                state['pose'].update(valid=False, position_active=False)

    def get_pose(self, side):
        return copy.deepcopy(self.states[side]['pose'])

    def packet_hands(self, fingers):
        return {side:dict(active=s['active'], home=s['home'], reset=s['reset'],
            pose=self.get_pose(side), finger=fingers.get(side, {'valid':False})) for side,s in self.states.items()}

    def telemetry(self):
        return dict(source='vive', mode=self.mode, message=(self.command_error or self.message)[:240],
            aligned=bool(self.references) and self.pending_alignment is None,
            armed=self.armed, generation=self.generation,
            world_yaw_deg=float(np.rad2deg(np.arctan2(self.basis[1,0], self.basis[0,0]))),
            hands={side:dict(serial=self.mapping.get(side), valid=state['pose']['valid'],
                model=self.raw['devices'].get(self.mapping.get(side), {}).get('model', ''),
                age_ms=state['pose'].get('age_ms', 10000.)) for side,state in self.states.items()})

