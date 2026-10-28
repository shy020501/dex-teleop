#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
if [[ "${CONDA_DEFAULT_ENV:-}" != dexjoco ]]; then
  echo 'Create environments/dexjoco.yml and activate dexjoco first.' >&2
  exit 1
fi
python - <<'PY'
import os, sys
from pathlib import Path
assert sys.version_info[:2] == (3, 11), 'DexJoCo requires Python 3.11'
assert Path(sys.prefix).resolve() == Path(os.environ['CONDA_PREFIX']).resolve()
PY
python scripts/dexjoco/setup.py "$@"
python -m pip install 'torch==2.7.0+cpu' --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements/dexjoco.txt
python -m pip install --no-deps --editable third_party/DexJoCo/dexjoco
python -m pip check
echo 'Run: ./scripts/run_teleop.sh --config dexjoco.yaml'
