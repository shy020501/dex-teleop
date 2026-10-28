#!/usr/bin/env python3
"""Fetch pinned DexJoCo sources and place assets under the shared storage root."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

def main():
    import argparse
    from dex_teleop.backends.dexjoco.assets import prepare_sources
    parser=argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(prepare_sources())

if __name__=='__main__':main()
