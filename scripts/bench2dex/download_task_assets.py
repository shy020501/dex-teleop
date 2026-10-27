#!/usr/bin/env python3
"""Prepare one task, its robot/URDFs and recursive USD dependencies without starting Isaac."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dex_teleop.backends.bench2dex.assets import ASSET_ROOT, AssetDownloads, RemoteRepository


def download(manager, task):
    manager.start(task)
    last = None
    try:
        while True:
            job = next(job for job in manager.snapshot()['jobs'] if job['task'] == task)
            progress = (job['state'], job['completed_files'], job['current_file'])
            if progress != last:
                print(f'{task}: {job["state"]}, {job["completed_files"]}/{job["discovered_files"]} files '
                      f'{job["current_file"]}', flush=True)
                last = progress
            if job['state'] == 'complete':
                ready, _, error = manager.readiness(task)
                if not ready:
                    raise RuntimeError(error or 'Asset readiness check failed')
                print(f'Ready: {task} (asset revision {job["revision"]})')
                return 0
            if job['state'] in ('error', 'cancelled'):
                raise RuntimeError(job.get('error') or 'Download cancelled')
            time.sleep(0.25)
    except KeyboardInterrupt:
        print('Cancelling download; waiting for the current transfer to stop...', file=sys.stderr)
        manager.cancel(task)
        while manager.snapshot()['busy']:
            time.sleep(0.1)
        return 130


def main(argv=None):
    lock = json.loads((ROOT / 'configs/sources.lock.json').read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', default='86_short_jigsaw_puzzle', help='Scene ID without .yaml')
    parser.add_argument('--asset-root', type=Path, default=ASSET_ROOT,
                        help='Download destination; default: workspace assets/bench2dex/')
    parser.add_argument('--revision', default=lock['assets']['revision'],
                        help='Asset repository revision; default: configs/sources.lock.json')
    args = parser.parse_args(argv)
    try:
        args.asset_root.mkdir(parents=True, exist_ok=True)
        manager = AssetDownloads(root=args.asset_root.resolve(),
                                 remote_factory=lambda: RemoteRepository(revision=args.revision))
        return download(manager, args.task)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Asset setup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
