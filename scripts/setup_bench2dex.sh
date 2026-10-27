#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

# Use the activated Conda interpreter, never a separate launcher override.
python - <<'PY'
import os
from pathlib import Path
import platform
import sys

if platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('This environment supports Linux x86_64. See windows/README.md for the input client.')
if sys.version_info[:2] != (3, 11):
    raise SystemExit('Python 3.11 required. Create environments/bench2dex.yml and activate the environment first.')
prefix = os.environ.get('CONDA_PREFIX')
if not prefix or Path(prefix).resolve() != Path(sys.prefix).resolve() or os.environ.get('CONDA_DEFAULT_ENV') == 'base':
    raise SystemExit('Activate the project Conda environment first; installation into base is not supported.')
libc, version = platform.libc_ver()
if libc != 'glibc' or tuple(map(int, version.split('.')[:2])) < (2, 35):
    raise SystemExit('Isaac Sim 5.1 pip wheels require glibc >= 2.35 (Ubuntu 22.04/24.04).')
print('Installing into:', sys.prefix)
PY

# Create Bench2Dex directories under the configured storage roots before downloads.
python scripts/bench2dex/setup_storage.py
python scripts/common/setup_sources.py
CONSTRAINTS="$ROOT/requirements/constraints-bench2dex.txt"

# flatdict 4.0.1 needs the bootstrap setuptools/wheel environment to build.
python -m pip install --no-build-isolation -c "$CONSTRAINTS" flatdict==4.0.1 toml==0.10.2

# Explicit local version suffixes prevent choosing CPU or another CUDA build.
python -m pip install -c "$CONSTRAINTS" \
    --index-url https://download.pytorch.org/whl/cu128 \
    --extra-index-url https://pypi.org/simple \
    'torch==2.7.0+cu128' 'torchvision==0.22.0+cu128' 'torchaudio==2.7.0+cu128'

# Install Isaac Lab core and the Bench2Dex requirements, including shared teleop packages.
python -m pip install --no-build-isolation -c "$CONSTRAINTS" \
    -r "$ROOT/requirements/bench2dex.txt" --editable "$ROOT/third_party/IsaacLab/source/isaaclab"
python -m pip check
python - <<'PY'
import importlib.metadata as metadata
import h5py
import numpy
import pinocchio
import torch
import zmq
from dex_retargeting.retargeting_config import RetargetingConfig

for name in ('isaacsim', 'isaaclab', 'torch', 'numpy', 'pin', 'dex-retargeting', 'h5py', 'pyzmq'):
    print(f'{name}=={metadata.version(name)}')
assert torch.version.cuda == '12.8', 'Expected the CUDA 12.8 PyTorch build'
print('CPU dependency imports passed. GPU simulation has not been started.')
PY
# Prepare the default scene only after dependency installation and import checks pass.
python scripts/bench2dex/download_task_assets.py --task 86_short_jigsaw_puzzle
echo 'Environment ready. Start: ./scripts/run_teleop.sh'

