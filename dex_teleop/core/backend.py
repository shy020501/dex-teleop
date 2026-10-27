"""The dashboard only needs a task catalog, asset service and storage location."""
from pathlib import Path
from typing import Protocol


class AssetService(Protocol):
    def snapshot(self) -> dict: ...
    def start(self, task: str): ...
    def cancel(self, task: str): ...


class DashboardBackend(Protocol):
    name: str
    data_root: Path
    default_task: str
    assets: AssetService

    def tasks(self) -> list[dict]: ...
