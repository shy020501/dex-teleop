#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
# Install only into the dedicated, explicitly activated Conda environment.
if [[ "${CONDA_DEFAULT_ENV:-}" != pomdar ]]; then
  echo 'Create and activate the POMDAR environment first:' >&2
  echo '  conda env create -f environments/pomdar.yml' >&2
  echo '  conda activate pomdar' >&2
  exit 1
fi
python - <<'PY'
import os
from pathlib import Path
import sys
assert sys.version_info[:2] == (3, 11), 'POMDAR requires Python 3.11'
assert Path(sys.prefix).resolve() == Path(os.environ['CONDA_PREFIX']).resolve(), 'Python must belong to the activated pomdar Conda environment'
PY
python -m pip install 'pip==25.2'
python -m pip install 'torch==2.7.0+cpu' --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements/pomdar.txt
python -m pip check
python scripts/pomdar/setup.py "$@"
echo "POMDAR environment: $CONDA_PREFIX"
echo 'Run: bash scripts/run_teleop.sh --config pomdar.yaml'
