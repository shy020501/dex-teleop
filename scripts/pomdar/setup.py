#!/usr/bin/env python3
"""Fetch only POMDAR source and selected task assets; never CAD or datasets."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    from scripts.common.setup_storage import prepare_simulator_storage
    from dex_teleop.backends.pomdar.assets import prepare_sources, AssetDownloads, RemoteRepository
    from dex_teleop.backends.pomdar.task_info import TASKS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', action='append', choices=list(TASKS), help='Repeat to fetch more tasks; default V1_Wheel')
    parser.add_argument('--sources-only', action='store_true')
    args = parser.parse_args(argv)
    prepare_simulator_storage(ROOT, 'pomdar')
    remote = RemoteRepository()
    files = prepare_sources(remote=remote)
    print(f'POMDAR sources verified: {len(files)} files, revision {remote.revision}', flush=True)
    if not args.sources_only:
        assets = AssetDownloads(remote_factory=lambda: remote)
        for task in args.task or ['V1_Wheel']:
            job = assets.download_sync(task)
            print(f'{task}: {job["completed_files"]} assets verified ({job["bytes_total"] / 1024**2:.1f} MiB)', flush=True)


if __name__ == '__main__':
    main()
