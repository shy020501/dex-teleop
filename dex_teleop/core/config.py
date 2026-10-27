"""Separate server profiles from the portable input client's settings."""
import os
from pathlib import Path
import re

import yaml

from dex_teleop.paths import WORKSPACE

SERVER_CONFIG_ENV = 'DEX_TELEOP_SERVER_CONFIG'
KEY_ACTIONS = ('x_plus', 'x_minus', 'y_plus', 'y_minus', 'z_plus', 'z_minus',
               'deadman', 'home', 'reset', 'select_right', 'select_left')


def resolve_server_config(path=None):
    value = path or os.environ.get(SERVER_CONFIG_ENV) or WORKSPACE/'configs/bench2dex.yaml'
    candidate = Path(value).expanduser()
    # Explicit relative paths belong to the caller; bare filenames can name a bundled profile.
    if not candidate.is_absolute() and candidate.parent == Path('.') and not candidate.exists():
        bundled = WORKSPACE/'configs'/candidate
        if bundled.is_file():
            candidate = bundled
    return candidate.resolve()


def _read(path):
    config = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError(f'Configuration must be a YAML mapping: {path}')
    return config


def load_server_config(path=None):
    return _read(resolve_server_config(path))


def validate_keys(keys):
    if not isinstance(keys, dict) or set(keys) != set(KEY_ACTIONS):
        raise ValueError('Keyboard settings must specify all supported key actions')
    for action, key in keys.items():
        if action == 'deadman' and key is None:
            continue
        if not isinstance(key, str) or not re.fullmatch(
                r'[a-z0-9]|f(?:[1-9]|1[0-2])|space|shift|control|alt|arrow(?:up|down|left|right)', key):
            raise ValueError(f'Unsupported key for {action}: {key!r}')
    return dict(keys)


def load_client_config(path=None):
    config = _read(Path(path).expanduser() if path else WORKSPACE/'configs/client.yaml')
    if 'simulator' in config:
        raise ValueError('Use configs/client.yaml for the Windows client; simulator profiles belong on Linux')
    config['keys'] = validate_keys(config.get('keys'))
    return config
