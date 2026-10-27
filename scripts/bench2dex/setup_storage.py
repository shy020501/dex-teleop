#!/usr/bin/env python3
"""Create Bench2Dex storage and its upstream asset link under the configured roots."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.common.setup_storage import prepare_simulator_storage


def setup_bench2dex_storage(workspace):
    workspace = Path(workspace).resolve()
    source_root = workspace / 'third_party'
    if source_root.is_symlink() or (source_root.exists() and not source_root.is_dir()):
        raise ValueError(f'{source_root} must be a local directory')
    compat = source_root / 'dex2bench_dataset'
    expected = (workspace / 'assets/bench2dex').resolve()
    if compat.is_symlink():
        if compat.resolve() != expected:
            raise ValueError(f'{compat} points to different assets; nothing was overwritten')
    elif compat.exists():
        raise ValueError(f'{compat} must be a compatibility symlink; nothing was overwritten')

    paths = prepare_simulator_storage(workspace, 'bench2dex')
    source_root.mkdir(exist_ok=True)
    if not compat.is_symlink():
        compat.symlink_to('../assets/bench2dex', target_is_directory=True)
    return paths


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        for name, target in setup_bench2dex_storage(ROOT).items():
            print(f'{name} -> {target}')
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Bench2Dex storage setup failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
