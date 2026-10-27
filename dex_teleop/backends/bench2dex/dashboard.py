"""Bind the shared dashboard to Bench2Dex's task catalog and storage."""
import yaml

from dex_teleop.dashboard import state
from .assets import AssetDownloads
from .debug_preview import debug_enabled
from .paths import DATA_ROOT, SOURCE_ROOT
from .task_info import success_description


class Bench2DexBackend:
    name = 'bench2dex'
    data_root = DATA_ROOT
    default_task = '86_short_jigsaw_puzzle'

    def __init__(self):
        self.assets = AssetDownloads()

    def tasks(self):
        result = []
        for file in sorted((SOURCE_ROOT / 'scenes').glob('*.yaml')):
            spec = yaml.safe_load(file.read_text())
            available, missing, error = self.assets.readiness(file.stem)
            result.append(dict(id=file.stem, description=spec.get('description', ''), available=available,
                               missing_assets=missing, asset_error=error,
                               success_criteria=success_description(file.stem, spec),
                               dwell_time_s=spec.get('metrics', {}).get('terminal', {}).get('dwell_time_s')))
        return result


class Dashboard(state.Dashboard):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, backend=Bench2DexBackend(), **kwargs)


def install_dashboard(task=None):
    return state.install_dashboard(Dashboard(debug_mode=debug_enabled()), task)
