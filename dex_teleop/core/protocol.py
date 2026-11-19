"""Versioned finite JSON schema. Pose: world metres/wxyz; fingers: global metres/xyzw."""
import json
import math
from dex_teleop.core.config import load_server_config, validate_keys

VERSION = 1
MAX_BYTES = 65536
SIDES = ('right', 'left')
COORDINATE_FRAME = 'simulation_world_z_up_m'
FINGER_REPRESENTATION = 'manus25_global_m_xyzw'


class ProtocolError(ValueError):
    """A peer cannot be controlled using the supported teleoperation contract."""


def load_config(path=None):
    """Compatibility name for server-side callers and existing Bench2Dex patches."""
    return load_server_config(path)


def vector(value, length, limit=10.):
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f'Expected vector of length {length}')
    result = [float(x) for x in value]
    if not all(math.isfinite(x) and abs(x) <= limit for x in result):
        raise ValueError('Non-finite/out-of-range vector')
    return result


def quaternion(value):
    value = vector(value, 4, 2.)
    norm = math.sqrt(sum(x*x for x in value))
    if not .8 <= norm <= 1.2:
        raise ValueError('Quaternion must have unit norm')
    return [x/norm for x in value]


def validate_packet(data):
    if not isinstance(data, dict) or data.get('version') != VERSION:
        raise ValueError('Unknown protocol version')
    if not isinstance(data.get('session'), str) or not 8 <= len(data['session']) <= 64:
        raise ValueError('Invalid session')
    if type(data.get('seq')) is not int or not 0 <= data['seq'] < 2**63:
        raise ValueError('Invalid sequence')
    if data.get('kind') not in ('hello', 'input'):
        raise ValueError('Invalid kind')
    if data['kind'] == 'hello':
        if 'keys' in data:
            validate_keys(data['keys'])
        return data
    if type(data.get('epoch')) is not int:
        raise ValueError('Missing calibration epoch')
    stamp = data.get('timestamp_unix_ns', 0)
    if type(stamp) is not int or not 0 <= stamp < 2**127:
        raise ValueError('Invalid diagnostic timestamp')
    if data.get('coordinate_frame') != COORDINATE_FRAME:
        raise ValueError('Unknown pose frame')
    hands = data.get('hands')
    if not isinstance(hands, dict) or not hands or not set(hands).issubset(SIDES):
        raise ValueError('Invalid hands')
    if 'pose_input' in data:
        data['pose_input'] = validate_pose_input(data['pose_input'])
    for side, hand in hands.items():
        if not isinstance(hand, dict) or not isinstance(hand.get('pose'), dict):
            raise ValueError('Invalid hand/pose object')
        if type(hand.get('active')) is not bool:
            raise ValueError('active must be boolean')
        pose = hand['pose']
        if 'position_active' in pose and type(pose['position_active']) is not bool:
            raise ValueError('Invalid position activity flag')
        pose['position_xyz'] = vector(pose['position_xyz'], 3)
        pose['orientation_wxyz'] = quaternion(pose['orientation_wxyz'])
        if pose.get('source') not in ('keyboard', 'mock', 'vive'):
            raise ValueError('Unknown pose source')
        if pose['source'] == 'vive':
            if type(pose.get('valid')) is not bool:
                raise ValueError('Vive pose requires tracking validity')
            age = pose.get('age_ms')
            if isinstance(age, bool) or not isinstance(age, (float, int)) or not math.isfinite(age) or not 0 <= age <= 10000:
                raise ValueError('Vive pose requires a finite sample age')
        for event in ('home', 'reset'):
            if type(hand.get(event)) is not int or not 0 <= hand[event] < 2**63:
                raise ValueError('Invalid event counter')
        finger = hand.get('finger', {'valid':False})
        if not isinstance(finger, dict):
            raise ValueError('Invalid finger object')
        if type(finger.get('valid')) is not bool:
            raise ValueError('Invalid tracking validity')
        if finger['valid']:
            if finger.get('representation') != 'manus25_global_m_xyzw':
                raise ValueError('Unknown finger representation')
            if len(finger['positions']) != 25 or len(finger['quaternions']) != 25:
                raise ValueError('Expected complete 25-node skeleton')
            finger['positions'] = [vector(v, 3, 5.) for v in finger['positions']]
            finger['quaternions'] = [quaternion(v) for v in finger['quaternions']]
            age = float(finger['age_ms'])
            if not math.isfinite(age) or age < 0 or age > 10000:
                raise ValueError('Invalid sample age')
        hand['finger'] = finger
    return data


def validate_pose_input(value):
    if not isinstance(value, dict) or value.get('source') != 'vive':
        raise ValueError('Invalid pose input telemetry')
    if value.get('mode') not in ('waiting', 'paused', 'active', 'tracking_lost'):
        raise ValueError('Invalid tracker mode')
    if any(type(value.get(k)) is not bool for k in ('aligned', 'armed')):
        raise ValueError('Invalid tracker alignment/arming status')
    message = value.get('message', '')
    yaw = value.get('world_yaw_deg', 0.)
    if (not isinstance(message, str) or len(message) > 240 or isinstance(yaw, bool)
            or not isinstance(yaw, (int, float)) or not math.isfinite(yaw) or abs(yaw) > 360):
        raise ValueError('Invalid tracker status')
    hands = value.get('hands')
    if not isinstance(hands, dict) or set(hands)-set(SIDES):
        raise ValueError('Invalid tracker hand assignments')
    result = dict(source='vive', mode=value['mode'], aligned=value['aligned'], armed=value['armed'],
                  message=message, world_yaw_deg=yaw, hands={})
    for side, hand in hands.items():
        if not isinstance(hand, dict): raise ValueError('Invalid tracker hand')
        serial, model, age = hand.get('serial'), hand.get('model', ''), hand.get('age_ms')
        if (serial is not None and (not isinstance(serial, str) or not 1 <= len(serial) <= 128)
                or not isinstance(model, str) or len(model) > 160 or type(hand.get('valid')) is not bool
                or isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age <= 10000):
            raise ValueError('Invalid tracker hand status')
        result['hands'][side] = dict(serial=serial, model=model, age_ms=age, valid=hand['valid'])
    return result


def encode(data):
    raw = json.dumps(data, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ValueError('Message too large')
    return raw


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('Message too large')
    return validate_packet(json.loads(raw))
