#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python "$ROOT/scripts/common/build_windows_teleop_bundle.py" "$@"
