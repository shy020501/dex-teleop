#!/usr/bin/env python3
"""Select a simulator from a Linux server profile and launch its integration."""
import argparse
import importlib
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dex_teleop.core.config import resolve_server_config

# Register a simulator's launch module here when its integration is implemented.
RUNNERS = {'bench2dex': 'scripts.bench2dex.launch', 'pomdar': 'scripts.pomdar.launch'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
        epilog='Simulator options such as --task and --steps are forwarded to the selected integration.')
    parser.add_argument('--config', '--teleop-config', type=Path, default=ROOT/'configs/bench2dex.yaml',
                        help='Server profile path or filename in configs/ (default: bench2dex.yaml)')
    parser.add_argument('--mode', choices=('teleop', 'viewer'), default='teleop', help=argparse.SUPPRESS)
    args, forwarded = parser.parse_known_args(argv)
    try:
        config_path = resolve_server_config(args.config)
        config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            raise ValueError('The teleoperation config must be a YAML mapping')
        simulator = config.get('simulator', 'bench2dex')
        if not isinstance(simulator, str) or simulator not in RUNNERS:
            raise ValueError(f'Unsupported simulator: {simulator!r}. Available: {", ".join(RUNNERS)}')
        runner = importlib.import_module(RUNNERS[simulator])
        runner.launch(config_path, args.mode, forwarded)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    except subprocess.CalledProcessError as exc:
        print(f'Simulator launch failed: {exc}', file=sys.stderr)
        return exc.returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())
