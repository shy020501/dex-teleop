#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
python - <<'PY'
import os,sys
from pathlib import Path
if os.environ.get('CONDA_DEFAULT_ENV')!='dexbench' or sys.version_info[:2]!=(3,11):
    raise SystemExit('Create environments/dexbench.yml and activate dexbench (Python 3.11) first.')
if Path(sys.prefix).resolve()!=Path(os.environ.get('CONDA_PREFIX','/nonexistent')).resolve():
    raise SystemExit('The current Python must belong to the active dexbench Conda environment.')
PY
BUILD_IMAGE=true
SETUP_ARGS=()
for argument in "$@"; do
  case "$argument" in
    --skip-image) BUILD_IMAGE=false ;;
    --build-image) BUILD_IMAGE=true ;;
    *) SETUP_ARGS+=("$argument") ;;
  esac
done
python -m pip install -r requirements/dexbench.txt
python -m pip check
python scripts/dexbench/setup.py "${SETUP_ARGS[@]}"
if [[ "$BUILD_IMAGE" == true ]]; then
  python scripts/dexbench/build_image.py
  echo 'Start: ./scripts/run_teleop.sh --config dexbench.yaml'
else
  echo 'Host tools/assets prepared; simulation is not installed until python scripts/dexbench/build_image.py succeeds.'
fi
