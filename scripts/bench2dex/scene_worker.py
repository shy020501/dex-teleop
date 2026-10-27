"""CLI entry point for the Bench2Dex backend."""
from pathlib import Path
import os
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if __name__ == "__main__":
    if os.environ.get("DEX_TELEOP_PARENT_PID"):
        from dex_teleop.backends.bench2dex.process_lifecycle import arm_parent_death_guard
        arm_parent_death_guard(int(os.environ["DEX_TELEOP_PARENT_PID"]))
    if os.environ.get("DEX_TELEOP_PROFILE_DIR"):
        from dex_teleop.diagnostics.runtime_profile import install
        install()
    runpy.run_module("dex_teleop.backends.bench2dex.worker_entry", run_name="__main__")
