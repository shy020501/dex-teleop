"""Simulator-independent control information published by every Linux integration."""
import math

from dex_teleop.core.protocol import (
    COORDINATE_FRAME, FINGER_REPRESENTATION, VERSION, SIDES, ProtocolError, quaternion, vector,
)


def describe_server(control, network, *, haptics=False):
    return dict(protocol_version=VERSION, coordinate_frame=COORDINATE_FRAME,
                pose_quaternion_order='wxyz', finger_representation=FINGER_REPRESENTATION,
                client_hz=network.get('client_hz', 60),
                keyboard_speed_m_s=control['keyboard_speed_m_s'],
                home_key_enabled=control.get('home_key_enabled', False),
                tracking_timeout_s=control['tracking_timeout_s'], haptics=bool(haptics))


def session_settings(status):
    """Validate only the public contract, never a simulator name or robot joint layout."""
    try:
        info = status['teleop']
        expected = dict(protocol_version=VERSION, coordinate_frame=COORDINATE_FRAME,
                        pose_quaternion_order='wxyz', finger_representation=FINGER_REPRESENTATION)
        if any(info.get(key) != value for key, value in expected.items()):
            raise ValueError('Unsupported protocol, coordinate system or hand representation')
        if not isinstance(status['server_id'], str) or not status['server_id']:
            raise ValueError('Missing server identity')
        if type(status['epoch']) is not int:
            raise ValueError('Invalid control epoch')
        for key in ('client_hz', 'keyboard_speed_m_s', 'tracking_timeout_s'):
            value = info[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'Invalid server control setting: {key}')
        for key in ('home_key_enabled', 'haptics'):
            if type(info[key]) is not bool:
                raise ValueError(f'Invalid server capability: {key}')
        hands = status['hands']
        if not isinstance(hands, dict) or not hands or not set(hands).issubset(SIDES):
            raise ValueError('Invalid available hands')
        for hand in hands.values():
            for key in ('home_position_xyz', 'target_position_xyz', 'current_position_xyz',
                        'workspace_min_xyz', 'workspace_max_xyz'):
                vector(hand[key], 3)
            quaternion(hand['home_orientation_wxyz'])
            if any(lo > hi for lo, hi in zip(hand['workspace_min_xyz'], hand['workspace_max_xyz'])):
                raise ValueError('Invalid workspace bounds')
        return info
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ProtocolError(f'Incompatible teleop server: {exc}. Update the server and Windows client together.') from exc
