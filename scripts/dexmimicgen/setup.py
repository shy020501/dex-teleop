#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.common.setup_storage import prepare_simulator_storage
from dex_teleop.backends.dexmimicgen.assets import prepare_sources
if __name__=='__main__':
 p=argparse.ArgumentParser(description='Prepare pinned DexMimicGen/robosuite sources and bundled assets.')
 p.parse_args()
 prepare_simulator_storage(ROOT,'dexmimicgen')
 print(prepare_sources())
