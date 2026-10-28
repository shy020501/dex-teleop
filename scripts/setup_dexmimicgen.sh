#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
if [[ "${CONDA_DEFAULT_ENV:-}" != dexmimicgen ]]; then
  echo 'Create environments/dexmimicgen.yml and conda activate dexmimicgen first.' >&2
  exit 1
fi
python - <<'PY'
import os,sys
from pathlib import Path
assert sys.version_info[:2]==(3,10), 'DexMimicGen requires Python 3.10'
assert Path(sys.prefix).resolve()==Path(os.environ['CONDA_PREFIX']).resolve()
PY
python -m pip install 'pip==25.2' 'setuptools==69.5.1' 'wheel==0.45.1'
python -m pip install -r requirements/dexmimicgen.txt
python scripts/dexmimicgen/setup.py "$@"
python -m pip install --no-deps --no-build-isolation -e third_party/DexMimicGen/dependencies/robosuite -e third_party/DexMimicGen
python -m pip check
echo 'Run: ./scripts/run_teleop.sh --config dexmimicgen.yaml'
