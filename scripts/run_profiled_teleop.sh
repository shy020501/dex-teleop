#!/usr/bin/env bash
# The same manual session, with bounded timers. No automatic inputs or recording.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export DEX_TELEOP_GPU_INDEX="${DEX_TELEOP_GPU_INDEX:-1}"
export DEX_TELEOP_PROFILE_SECONDS="${DEX_TELEOP_PROFILE_SECONDS:-300}"
export DEX_TELEOP_PROFILE_DIR="${DEX_TELEOP_PROFILE_DIR:-$ROOT/logs/performance/manual-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$DEX_TELEOP_PROFILE_DIR"
if [[ -e "$DEX_TELEOP_PROFILE_DIR/window.json" ]]; then
    echo 'Use a new DEX_TELEOP_PROFILE_DIR for each measurement.' >&2
    exit 2
fi
echo "[PROFILE] Output: $DEX_TELEOP_PROFILE_DIR"
exec bash "$ROOT/scripts/run_teleop.sh" "$@"
