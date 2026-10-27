#!/usr/bin/env python3
"""Fetch pinned upstream sources and apply workspace patches without discarding edits."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args, check=True):
    return subprocess.run(
        ['git', '-C', str(repo), '-c', 'core.fileMode=false', *args],
        check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def prepare_source(root, name, spec, *, check_only=False):
    root = Path(root).resolve()
    destination = Path(spec.get('path', name))
    if destination.is_absolute() or '..' in destination.parts or destination == Path('.'):
        raise ValueError(f'{name}: invalid source path: {destination}')
    target = root / destination
    if not target.resolve().is_relative_to(root):
        raise ValueError(f'{name}: source path escapes the workspace')
    patch = root / spec['patch']
    if not patch.is_file():
        raise RuntimeError(f'Missing workspace patch: {patch}')
    if target.is_symlink():
        raise RuntimeError(f'{name}: expected a local checkout, found a symbolic link')
    empty_directory = target.is_dir() and not any(target.iterdir())
    if not target.exists() or empty_directory:
        if check_only:
            raise RuntimeError(f'{name}: missing; run python scripts/common/setup_sources.py')
        # A failed fetch/patch never leaves a partially prepared final checkout.
        with tempfile.TemporaryDirectory(prefix=f'.{name}-setup-', dir=root) as temp:
            checkout = Path(temp) / name
            checkout.mkdir()
            git(checkout, 'init', '--quiet')
            git(checkout, 'remote', 'add', 'origin', spec['url'])
            print(f'{name}: fetching {spec["commit"]}', flush=True)
            git(checkout, 'fetch', '--depth', '1', 'origin', spec['commit'])
            git(checkout, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
            _prepare_checkout(checkout, spec, patch, check_only=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            if empty_directory:
                target.rmdir()  # Fails safely if another process added anything.
            checkout.rename(target)
        return 'created'
    _prepare_checkout(target, spec, patch, check_only=check_only)
    return 'ready'


def _prepare_checkout(checkout, spec, patch, *, check_only):
    top = git(checkout, 'rev-parse', '--show-toplevel', check=False)
    if top.returncode or Path(top.stdout.strip()).resolve() != checkout.resolve():
        raise RuntimeError(f'{checkout.name}: directory exists but is not its own Git checkout')
    revision = git(checkout, 'rev-parse', 'HEAD').stdout.strip()
    if revision != spec['commit']:
        raise RuntimeError(
            f'{checkout.name}: expected {spec["commit"]}, found {revision}; '
            'keep your existing work and move the directory aside before retrying'
        )
    if git(checkout, 'apply', '--reverse', '--check', str(patch), check=False).returncode == 0:
        return  # Already patched; preserve any additional local work.
    if check_only:
        raise RuntimeError(f'{checkout.name}: workspace patch is missing or differs')
    if git(checkout, 'diff', '--quiet', 'HEAD', '--', check=False).returncode:
        raise RuntimeError(
            f'{checkout.name}: local changes differ from the workspace patch; '
            'nothing was overwritten; reconcile or move the checkout aside'
        )
    git(checkout, 'apply', '--check', str(patch))
    git(checkout, 'apply', str(patch))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check existing sources without changing them')
    args = parser.parse_args(argv)
    try:
        lock = json.loads((ROOT / 'configs/sources.lock.json').read_text())
        for name, spec in lock['sources'].items():
            status = prepare_source(ROOT, name, spec, check_only=args.check)
            print(f'{name}: {status} ({spec["commit"][:12]})')
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f'Source setup failed: {detail}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

