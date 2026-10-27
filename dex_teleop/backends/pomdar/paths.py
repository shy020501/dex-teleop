"""Separate POMDAR sources, assets and recorded episodes."""
from dex_teleop.paths import WORKSPACE

SOURCE_REPOSITORY = 'srl-ethz/pomdar_benchmark'
SOURCE_REVISION = '6eb4131b78e7ccf478f3bfb2a2cb3e25babeedbb'
SOURCE_ROOT = WORKSPACE/'third_party/POMDAR'
ASSET_ROOT = (WORKSPACE/'assets/pomdar').resolve()
DATA_ROOT = (WORKSPACE/'data/pomdar').resolve()
