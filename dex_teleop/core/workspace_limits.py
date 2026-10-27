"""CPU-only validation for Home-relative palm workspace settings (world axes, metres)."""
import math

OFFSET_LIMIT_M = 2.


def validate_workspace(value):
    if not isinstance(value, dict):
        raise ValueError('이동 범위 설정을 확인하세요.')
    def number(raw):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise ValueError('이동 범위는 유한한 숫자(m)여야 합니다.')
        return float(raw)
    def vector(name, low, high):
        raw = value.get(name)
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise ValueError('X·Y·Z 세 축의 이동 범위를 입력하세요.')
        result = [number(item) for item in raw]
        if any(not low <= item <= high for item in result):
            raise ValueError('Home 기준 최소값은 -2~0 m, 최대값은 0~2 m입니다.')
        return result
    minimum = vector('min_offset_xyz', -OFFSET_LIMIT_M, 0.)
    maximum = vector('max_offset_xyz', 0., OFFSET_LIMIT_M)
    # Discard legacy absolute-Z fields when reading old saved settings or requests.
    return dict(min_offset_xyz=minimum, max_offset_xyz=maximum)


def workspace_from_control(control):
    upper = control['workspace_radius_xyz']
    lower = control.get('workspace_lower_radius_xyz', upper)
    return validate_workspace(dict(min_offset_xyz=[-float(x) for x in lower],
                                   max_offset_xyz=list(upper)))


def workspace_bounds(home, settings):
    """Convert validated Home offsets to world bounds, identically for all axes."""
    if len(home) != 3 or not all(math.isfinite(float(v)) for v in home):
        raise ValueError('손의 Home 위치가 유효하지 않습니다.')
    lower = [float(p)+d for p,d in zip(home, settings['min_offset_xyz'])]
    upper = [float(p)+d for p,d in zip(home, settings['max_offset_xyz'])]
    return lower, upper
