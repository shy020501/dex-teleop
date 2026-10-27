#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${DEX_TELEOP_PYTHON:-${BENCH2DEX_PYTHON:-python}}
exec "$PYTHON" "$ROOT/scripts/common/launch.py" --mode teleop "$@"
