#!/usr/bin/env python3
"""Fetch only the pinned DexBench source tree and a selected task's assets."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from dex_teleop.backends.dexbench.paths import SOURCE_ROOT,SOURCE_REPOSITORY,SOURCE_REVISION,ARENA_REVISION,ISAACLAB_REVISION
from dex_teleop.backends.dexbench.task_info import DEFAULT_TASK,TASKS


def prepare_sources():
    env=dict(os.environ,GIT_LFS_SKIP_SMUDGE='1')
    if not SOURCE_ROOT.exists():
        SOURCE_ROOT.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['git','clone','--filter=blob:none','--no-checkout',SOURCE_REPOSITORY,str(SOURCE_ROOT)],check=True,env=env)
        subprocess.run(['git','-C',str(SOURCE_ROOT),'checkout','--detach',SOURCE_REVISION],check=True,env=env)
    head=subprocess.check_output(['git','-C',str(SOURCE_ROOT),'rev-parse','HEAD'],text=True).strip()
    if head!=SOURCE_REVISION:raise ValueError('Existing DexBench source has a different revision; keep that checkout separate')
    dirty=subprocess.check_output(['git','-C',str(SOURCE_ROOT),'status','--porcelain','--untracked-files=no'],text=True)
    if dirty.strip():raise ValueError('DexBench sources have local modifications; preserve them before running the pinned setup')
    subprocess.run(['git','-C',str(SOURCE_ROOT),'submodule','update','--init','--depth','1','submodule/isaaclab_arena'],check=True,env=env)
    arena=SOURCE_ROOT/'submodule/isaaclab_arena'
    subprocess.run(['git','-C',str(arena),'submodule','update','--init','--depth','1','submodules/IsaacLab'],check=True,env=env)
    for path,pin in [(arena,ARENA_REVISION),(arena/'submodules/IsaacLab',ISAACLAB_REVISION)]:
        actual=subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()
        if actual!=pin:raise ValueError('DexBench submodule revision mismatch: '+str(path))
    return SOURCE_ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',choices=list(TASKS),default=DEFAULT_TASK)
    parser.add_argument('--skip-assets',action='store_true')
    parser.add_argument('--skip-sources',action='store_true')
    args=parser.parse_args()
    from scripts.common.setup_storage import prepare_simulator_storage
    prepare_simulator_storage(ROOT,'dexbench')
    if not args.skip_sources:prepare_sources()
    if not args.skip_assets:
        from dex_teleop.backends.dexbench.assets import AssetDownloads
        result=AssetDownloads().install(args.task)
        print(json.dumps(result,indent=2))
    print('Pinned source and selected assets prepared. Runtime still requires the DexBench Docker image.')

if __name__=='__main__':main()
