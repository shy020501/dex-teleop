"""Human-readable descriptions of the installed terminal evaluators."""
from functools import lru_cache
from pathlib import Path

import yaml

from .paths import CONFIG_ROOT
CATALOG = CONFIG_ROOT/'task_success_ko.yaml'
EN_CATALOG = CONFIG_ROOT/'task_success_en.yaml'


@lru_cache(maxsize=2)
def _read_catalog(path, modified_ns):
    return yaml.safe_load(path.read_text(encoding='utf-8'))['tasks']


def success_description(task, scene):
    """Read dwell from the actual task, rather than duplicating it in translated prose."""
    translations = {}
    registered = False
    for language, path, fallback in (
        ('ko', CATALOG, '성공 조건 안내가 아직 등록되지 않았습니다.'),
        ('en', EN_CATALOG, 'Success criteria are not available for this task yet.'),
    ):
        entry = _read_catalog(path, path.stat().st_mtime_ns).get(task)
        registered = registered or entry is not None
        translations[language] = dict(summary=entry['summary'] if entry else fallback,
                                     details=list(entry.get('details', [])) if entry else [])
    terminal = scene.get('metrics', {}).get('terminal', {})
    # Keep the original fields for older dashboards; localization never changes evaluation.
    return dict(**translations['ko'], translations=translations,
                dwell_time_s=terminal.get('dwell_time_s', .5 if registered else None))
