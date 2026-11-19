"""Pinned sources and simulator-specific storage, shared by host and container."""
import os
from pathlib import Path
from dex_teleop.paths import WORKSPACE

SOURCE_REVISION = 'b0be6b1302e895eedf673b78061d3a3c59486eaf'
SOURCE_REPOSITORY = 'https://github.com/RLWRLD/DexBench.git'
ARENA_REVISION = '59d95cc9b9adbd8560661c1fd3e0487aac4273af'
ISAACLAB_REVISION = '2d3ae03ede6be38d71af5698509fe8116b6b7216'
SOURCE_ROOT = Path(os.environ.get('DEX_TELEOP_DEXBENCH_SOURCE', WORKSPACE/'third_party/DexBench')).resolve()
ASSET_ROOT = Path(os.environ.get('DEX_TELEOP_DEXBENCH_ASSETS', WORKSPACE/'assets/dexbench')).resolve()
DATA_ROOT = Path(os.environ.get('DEX_TELEOP_DEXBENCH_DATA', WORKSPACE/'data/dexbench')).resolve()
LFS_ENDPOINT = 'https://huggingface.co/datasets/RLWRLD/DexBench-assets.git/info/lfs'
INPUT_REPOSITORY = 'RLWRLD/DexBench-eval'
INPUT_REVISION = 'c54c9c123e93541dc2e6e6128eeaa748db40a027'
BASE_IMAGE = 'nvcr.io/nvidia/isaac-sim@sha256:af1d2b4e75d553bfa27beb5a401198654aa8d607f3b7a6749196e9ce253def20'
IMAGE = 'dex-teleop-dexbench:local'
UPSTREAM_IMAGE = 'dexbench-arena:b0be6b1'
