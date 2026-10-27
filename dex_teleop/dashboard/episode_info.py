"""Small episode summaries and dataset shapes, without loading image/state payloads."""
import json
import math

import h5py


def _number(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0 or (positive and value <= 0):
        return None
    return value


def _dataset(file, path):
    # Shape inspection must not follow links into another file.
    cursor = file
    for part in path.split('/'):
        if not isinstance(cursor, h5py.Group) or not isinstance(cursor.get(part, getlink=True), h5py.HardLink):
            return None
        cursor = cursor[part]
    return cursor if isinstance(cursor, h5py.Dataset) else None


GROUP_LABELS = {
    'cameras': '카메라 영상·보정 정보',
    'robot': '관측한 로봇 관절 상태',
    'action': '로봇에 전달한 관절 목표',
    'objects': '물체별 위치·자세·속도',
    'time': '프레임·시간·Physics step',
    'episode': '시작·종료·성공 표시',
    'metrics': 'Task 평가 결과',
    'meta': 'Task·로봇·수집 설정',
    'labels': '추가 라벨',
    'replay': '재생 검증용 전체 상태·step 대응',
}
FIELD_LABELS = {
    'rgb': 'RGB', 'depth_m': 'Depth (m)', 'intrinsic': '카메라 내부 파라미터',
    'extrinsic_world_from_cam': '카메라 → 월드 변환', 'camera_model': '카메라 모델',
    'fisheye_camera_matrix': 'Fisheye 보정 행렬', 'distortion_coefficients': '왜곡 계수',
    'fisheye_valid_mask': '유효 픽셀', 'distortion_model': '왜곡 모델',
    'raw_image_semantics': '영상 형식', 'clipping_range': '촬영 거리 범위',
    'joint_names': '관절 순서', 'qpos': '관절 위치', 'qvel': '관절 속도',
    'qeffort': '관절 effort', 'commanded': '목표 관절 위치', 'action_names': 'Action 관절 순서',
    'action_valid': 'Action 유효 여부', 'source': '입력 출처',
    'control_mode': '제어 모드', 'action_type': '목표 형식',
    'pose_world': '월드 위치·자세 (xyz + xyzw)', 'lin_vel_world': '월드 선속도',
    'ang_vel_world': '월드 각속도', 'frame_index': '저장 프레임 번호',
    'sim_step': 'Physics step', 'timestamp_ns': '서버 시간 (ns)',
    'is_first': '첫 프레임', 'is_last': '마지막 프레임', 'done': 'Episode 종료',
    'success': '성공 종료', 'frame_valid': '수집 정상 여부', 'frame_errors': '수집 오류',
}
CAMERA_LABELS = {'cam_task': '작업 공간', 'cam_wrist_left': '왼손 손목', 'cam_wrist_right': '오른손 손목'}


def describe_episode(file, meta, size_bytes):
    time = _dataset(file, 'time/frame_index')
    steps = _dataset(file, 'time/sim_step')
    commanded = _dataset(file, 'action/commanded')
    frames = int(time.shape[0]) if time is not None and time.ndim == 1 else None
    if frames is None and steps is not None and steps.ndim == 1:
        frames = int(steps.shape[0])
    if frames is None:
        value = _number(meta.get('frame_count'))
        frames = int(value) if value is not None else None
    dt = _number(meta.get('physics_dt'), positive=True)
    rate = _number(meta.get('effective_fps'), positive=True)
    stride = _number(meta.get('step_stride'), positive=True)
    if rate is None:
        rate = 1/(dt*stride) if dt and stride else _number(meta.get('fps'), positive=True)
    duration = _number(meta.get('episode_length_s'))
    if duration is None and dt and steps is not None and steps.ndim == 1 and steps.size > 0:
        duration = max(0., (int(steps[-1])-int(steps[0]))*dt)
    valid = _dataset(file, 'frame_valid')
    valid_count = None
    if valid is not None and valid.ndim == 1 and valid.dtype.kind == 'b':
        # Small bool masks only; never scan RGB/robot/object data to display a dialog.
        valid_count = sum(int(valid[i:i+65536].sum()) for i in range(0, valid.size, 65536))
    summary = dict(
        frames=frames, duration_s=duration, sample_hz=rate, physics_hz=1/dt if dt else None,
        bytes=size_bytes, task=meta.get('task_name'), robot=meta.get('robot_key'),
        success=meta.get('success') if isinstance(meta.get('success'), bool) else None,
        saved_at=meta.get('saved_at') or meta.get('created_at'),
        termination_reason=meta.get('termination_reason'), valid_frames=valid_count,
        action_dim=int(commanded.shape[1]) if commanded is not None and commanded.ndim == 2 else None,
    )
    modalities = meta.get('modalities')
    if isinstance(modalities, str):
        try:
            modalities = json.loads(modalities)
        except ValueError:
            modalities = None
    label_placeholder = isinstance(modalities, list) and not any(
        name in modalities for name in ('box2d', 'box3d', 'occupancy'))
    budget = [500]

    def node(parent, name, path, depth=0):
        if not isinstance(parent.get(name, getlink=True), h5py.HardLink):
            return dict(name=name, label='링크', path=path, kind='note')
        item = parent[name]
        budget[0] -= 1
        result = dict(name=name, path=path)
        if isinstance(item, h5py.Dataset):
            result.update(kind='dataset', label=FIELD_LABELS.get(name, ''), shape=list(item.shape),
                          dtype='string' if h5py.check_string_dtype(item.dtype) else str(item.dtype))
            return result
        result.update(kind='group', label=GROUP_LABELS.get(path, CAMERA_LABELS.get(name, '')))
        if path == 'labels' and label_placeholder:
            result.update(label='미수집 · placeholder', kind='note')
        elif path in ('meta', 'metrics'):
            # Keep the data schema readable; hundreds of raw metadata/metric fields are not the UI.
            result.update(kind='note')
        elif depth >= 4 or budget[0] <= 0:
            result.update(kind='note', label=result['label'] or f'{len(item)}개 항목')
        else:
            names = sorted(item, key=lambda k: (k != 'rgb', k))
            result['children'] = []
            for child in names:
                if budget[0] <= 0:
                    result['children'].append(dict(name='…', label='나머지 항목', kind='note'))
                    break
                result['children'].append(node(item, child, f'{path}/{child}', depth+1))
        return result

    order = ['cameras', 'robot', 'action', 'objects', 'time', 'episode',
             'frame_valid', 'frame_errors', 'metrics', 'meta', 'labels']
    names = [name for name in order if name in file] + sorted(set(file)-set(order))
    return dict(summary=summary, structure=[node(file, name, name) for name in names])
