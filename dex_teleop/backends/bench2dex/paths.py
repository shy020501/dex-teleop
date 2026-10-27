"""All Bench2Dex-specific filesystem locations."""
from pathlib import Path
from dex_teleop.paths import WORKSPACE

SOURCE_ROOT = WORKSPACE / 'third_party/Bench2Dex'
CONFIG_ROOT = WORKSPACE / 'configs/bench2dex'
DATA_ROOT = (WORKSPACE / 'data/bench2dex').resolve()
ASSET_ROOT = (WORKSPACE / 'assets/bench2dex').resolve()
# Upstream scene YAMLs/robot loaders retain their original relative paths.
LEGACY_ASSET_ROOT = WORKSPACE / 'third_party/dex2bench_dataset'


def resolve_task_path(recorded_path):
    """Replay existing episodes after moving the upstream checkout.

    Only known upstream scene paths can be relocated by name. Missing custom
    YAMLs must be restored explicitly; guessing could replay a different task.
    """
    path = Path(recorded_path)
    if path.is_file():
        return path
    if path.parent.name == 'scenes' and path.parent.parent.name == 'Bench2Dex':
        candidate = SOURCE_ROOT / 'scenes' / path.name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f'Recorded task YAML is unavailable: {path}')
