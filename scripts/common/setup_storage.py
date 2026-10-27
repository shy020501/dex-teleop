#!/usr/bin/env python3
"""Configure shared storage roots, optionally linking DATASET_DIR and ASSET_DIR."""
import argparse
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
STORAGE_NAMES = ('data', 'assets')
STORAGE_CONTAINERS = ('data', 'assets', 'third_party')


def _plan(workspace, name, variable, configured):
    local = workspace / name
    current = local.resolve()
    if configured:
        requested = Path(configured).expanduser()
        if not requested.is_absolute():
            raise ValueError(f'{variable} must be an absolute Linux path: {configured}')
        target = requested.resolve()
    else:
        target = current

    if workspace.is_relative_to(target):
        raise ValueError(f'{variable}: storage cannot contain the workspace itself: {target}')
    if target != local and any(target.is_relative_to(workspace / entry) for entry in STORAGE_CONTAINERS):
        raise ValueError(f'{variable}: target cannot be inside a workspace storage alias: {target}')
    if target.exists() and not target.is_dir():
        raise ValueError(f'{variable}: target is not a directory: {target}')
    if local.is_symlink():
        if current != target:
            raise ValueError(
                f'{local} already points to {current}; leave it unchanged or '
                'stop the server and reconnect it manually after arranging your data'
            )
    elif local.exists():
        if not local.is_dir():
            raise ValueError(f'{local} exists and is not a directory')
        if target != local and any(local.iterdir()):
            raise ValueError(
                f'{local} is not empty; existing data was not moved or overwritten. '
                'Move the directory aside or migrate its contents before linking external storage'
            )
    return local, target


def setup_storage(workspace, dataset_dir=None, asset_dir=None):
    """Persist common roots without creating any simulator-specific directories."""
    workspace = Path(workspace).resolve()
    # Validate both requests before changing either workspace entry.
    plans = [
        _plan(workspace, 'data', 'DATASET_DIR', dataset_dir),
        _plan(workspace, 'assets', 'ASSET_DIR', asset_dir),
    ]
    if plans[0][1] == plans[1][1]:
        raise ValueError('DATASET_DIR and ASSET_DIR must use different storage roots')
    for _, target in plans:
        target.mkdir(parents=True, exist_ok=True)
        if not os.access(target, os.W_OK | os.X_OK):
            raise PermissionError(f'Storage directory is not writable: {target}')
    (workspace / 'logs').mkdir(exist_ok=True)

    for local, target in plans:
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.is_symlink():
            if local.resolve() != target:
                raise RuntimeError(f'Storage link changed during setup: {local}')
            continue
        if local == target:
            continue  # The target directory was already created above.
        removed_empty = local.exists()
        if removed_empty:
            local.rmdir()  # Refuses removal if another process added a file.
        try:
            local.symlink_to(target, target_is_directory=True)
        except OSError:
            if removed_empty and not local.exists() and not local.is_symlink():
                local.mkdir()
            raise
    return {local.relative_to(workspace).as_posix(): target for local, target in plans}


def prepare_simulator_storage(workspace, simulator):
    """Create one simulator's directories under the persisted roots.

    Existing simulator-level symlinks are preserved for older workspaces.
    Environment variables are only read by the common storage setup CLI.
    """
    if not re.fullmatch(r'[a-z][a-z0-9_-]*', simulator):
        raise ValueError(f'Invalid simulator directory name: {simulator!r}')
    workspace = Path(workspace).resolve()
    plans = []
    for name in STORAGE_NAMES:
        local = workspace / name / simulator
        target = local.resolve()
        if workspace.is_relative_to(target) or target == (workspace / name).resolve():
            raise ValueError(f'{local}: simulator storage must have its own directory')
        if target.exists() and not target.is_dir():
            raise ValueError(f'{local}: target is not a directory: {target}')
        plans.append((local, target))
    setup_storage(workspace)  # Also creates local defaults if the optional step was skipped.
    for _, target in plans:
        target.mkdir(parents=True, exist_ok=True)
        if not os.access(target, os.W_OK | os.X_OK):
            raise PermissionError(f'Storage directory is not writable: {target}')
    return {local.relative_to(workspace).as_posix(): target for local, target in plans}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        paths = setup_storage(ROOT, os.environ.get('DATASET_DIR'), os.environ.get('ASSET_DIR'))
        for name, target in paths.items():
            kind = 'symlink' if (ROOT / name).is_symlink() else 'directory'
            print(f'{name} -> {target} ({kind})')
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Storage setup failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
