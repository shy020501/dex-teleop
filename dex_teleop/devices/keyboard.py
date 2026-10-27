"""Keyboard pose input; independent of the simulator."""
import copy
import numpy as np
from dex_teleop.core.input import HandPoseProvider
from dex_teleop.core.protocol import SIDES
from dex_teleop.core.config import load_client_config
from dex_teleop.core.session import session_settings


class KeyboardPoseProvider(HandPoseProvider):
    def __init__(self, config):
        self.config = config
        self.keys = config.get('keys') or load_client_config()['keys']
        # Explicit old server configs remain usable by diagnostic clients.
        self.speed = config.get('control', {}).get('keyboard_speed_m_s', 0.)
        self.home_key_enabled = config.get('control', {}).get('home_key_enabled', False)
        self.selected = 'right'
        self.states = {}
        self.previous_keys = set()
        self.blocked = True
        self.identity = None

    def synchronize(self, status, force=False):
        if 'teleop' in status:
            settings = session_settings(status)
            self.speed = settings['keyboard_speed_m_s']
            self.home_key_enabled = settings['home_key_enabled']
        identity = (status.get('server_id'), status.get('epoch'))
        changed = identity != self.identity
        if changed:
            self.identity = identity
            self.states.clear()
            self.previous_keys.clear()
            self.blocked = True
        hands = status.get('hands', {})
        for side in set(self.states)-set(hands):
            del self.states[side]
        if hands and self.selected not in hands:
            self.selected = next(iter(hands))
        for side, remote in hands.items():
            old = self.states.get(side)
            if force or old is None or not old['active'] or not old['pose'].get('position_active',False):
                position = remote['target_position_xyz']
            else:
                position = old['pose']['position_xyz']
            self.states[side] = {'active':False if old is None else old['active'],
                'pose':{'position_xyz':list(position), 'orientation_wxyz':list(remote['home_orientation_wxyz']), 'source':'keyboard',
                        'position_active':old['pose'].get('position_active',False) if old else False},
                '_home_running':old.get('_home_running',False) if old else False,
                'home':old['home'] if old else remote.get('home_event_ack', 0),
                'reset':old['reset'] if old else remote.get('reset_event_ack', 0),
                '_remote':remote}
        return changed

    def connection_lost(self):
        self.blocked = True
        for state in self.states.values():
            state['active'] = False

    def request_home(self,side):
        """Retained for the future collection UI; independent of the F6 binding."""
        state=self.states[side]
        state['home']+=1
        state['_home_running']=True
        state['pose']['position_xyz']=list(state['_remote']['home_position_xyz'])

    def update(self, held_keys, dt, enabled=True):
        held = {k.lower() for k in held_keys}
        deadman = self.keys.get('deadman')
        command_keys = {self.keys[k] for k in ('x_plus','x_minus','y_plus','y_minus','z_plus','z_minus','home','reset')}
        if not self.home_key_enabled:command_keys.discard(self.keys['home'])
        if (deadman and deadman not in held) or (not deadman and not held.intersection(command_keys)):
            self.blocked = False
        for side in SIDES:
            if side in self.states and self.keys['select_'+side] in held:
                self.selected = side
        for side, state in self.states.items():
            active = enabled and side == self.selected and (not deadman or deadman in held) and not self.blocked
            state['active'] = active
            if not active:
                state['pose']['position_active']=False
                state['_home_running']=False
                continue
            remote = state['_remote']
            for event in ('home', 'reset'):
                key = self.keys[event]
                if event=='home' and not self.home_key_enabled:continue
                if key in held and key not in self.previous_keys:
                    if event=='home':self.request_home(side)
                    else:
                        state[event]+=1
                        state['_home_running']=False
                        state['pose']['position_xyz']=list(remote['current_position_xyz'])
            direction = np.array([int(self.keys[axis+'_plus'] in held)-int(self.keys[axis+'_minus'] in held) for axis in ('x','y','z')], dtype=float)
            if self.keys['reset'] in held:direction[:]=0.
            norm = np.linalg.norm(direction)
            if norm > 0:state['_home_running']=False
            if state['_home_running'] and np.linalg.norm(np.array(remote['current_position_xyz'])-remote['home_position_xyz'])<.003:
                state['_home_running']=False
            state['pose']['position_active'] = bool(norm>0 or state['_home_running'])
            if norm > 1:
                direction /= norm
            position = np.array(state['pose']['position_xyz']) + direction*self.speed*max(0., min(dt, .05))
            position = np.clip(position, remote['workspace_min_xyz'], remote['workspace_max_xyz'])
            state['pose']['position_xyz'] = position.tolist()
        self.previous_keys = held

    def get_pose(self, side):
        return copy.deepcopy(self.states[side]['pose'])

    def packet_hands(self, fingers):
        return {side:{'active':s['active'], 'pose':self.get_pose(side), 'home':s['home'], 'reset':s['reset'],
                      'finger':fingers.get(side, {'valid':False})} for side,s in self.states.items()}

