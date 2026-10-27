"""POMDAR protocol catalog, distinct from its 17 MJCF asset fragments.

The public source ships no automatic success evaluator. These descriptions do
not invent thresholds or claim official benchmark scores; outcome is manual.
"""

_ROWS = (
    ('V1_Wheel', 'Wheel', '바퀴', 'vertical'),
    ('V2_Stick', 'Stick', '막대', 'vertical'),
    ('V3_Sphere', 'Sphere', '구', 'vertical'),
    ('H1_Scissors', 'Scissors', '가위', 'horizontal'),
    ('H2_Chopsticks', 'Chopsticks', '젓가락', 'horizontal'),
    ('H3_Squeeze', 'Squeeze', '쥐기', 'horizontal'),
    ('H4_Palmar', 'Palmar', '손바닥 파지', 'horizontal'),
    ('H5_Pinch', 'Pinch', '집게 파지', 'horizontal'),
    ('C1_Thread', 'Thread', '나사', 'continuous'),
    ('C2_Stick', 'Stick', '막대', 'continuous'),
    ('C3_Wheel', 'Wheel', '바퀴', 'continuous'),
    ('C4_Fidget', 'Fidget', '피젯', 'continuous'),
    ('G1_Wheel', 'Wheel', '바퀴', 'grasping'),
    ('G2_Sphere', 'Sphere', '구', 'grasping'),
    ('G3_Disk', 'Disk', '원판', 'grasping'),
    ('G4_Cylinder_Small', 'Small cylinder', '작은 원통', 'grasping'),
    ('G5_Cylinder', 'Cylinder', '원통', 'grasping'),
    ('G6_Cylinder_Large', 'Large cylinder', '큰 원통', 'grasping'),
)
TASKS = {identity: dict(id=identity, title_en=en, title_ko=ko, category=category,
                      fragment='H4_Palmar_H5_Pinch' if identity in ('H4_Palmar', 'H5_Pinch') else identity)
         for identity, en, ko, category in _ROWS}


def task_info(task):
    if not isinstance(task, str) or task not in TASKS:
        raise ValueError('Unknown POMDAR task: '+str(task))
    return dict(TASKS[task])


def success_description(task):
    spec = task_info(task)
    descriptions = {
        'vertical': ('수직 기구의 걸림턱을 통과하도록 물체를 손안에서 조작합니다.',
                     'Manipulate the object in-hand through the notches of the vertical apparatus.'),
        'horizontal': ('해당 파지 방식으로 물체를 조작하여 수평 기구의 경로를 따라 이동합니다.',
                       'Use the specified grasp to manipulate the object along the horizontal apparatus.'),
        'continuous': ('해당 회전 기구를 손가락으로 연속 조작합니다.',
                       'Use the fingers to sustain rotation of the task mechanism.'),
        'grasping': ('물체를 안정적으로 잡아 들어 올리고 원하는 위치에 옮깁니다.',
                     'Grasp, lift and relocate the object while maintaining control.'),
    }
    ko, en = descriptions[spec['category']]
    if task == 'H4_Palmar': ko += ' 손바닥 파지를 사용합니다.'; en += ' Use a palmar grasp.'
    if task == 'H5_Pinch': ko += ' 집게 파지를 사용합니다.'; en += ' Use a pinch grasp.'
    translations = {
        'ko': dict(summary=ko, details=['완료 여부는 사용자가 판정합니다. 자동 성공 판정과 공식 POMDAR 점수 계산은 제공하지 않습니다.']),
        'en': dict(summary=en, details=['Outcome is judged by the operator. Automatic success and official POMDAR scoring are not implemented.']),
    }
    return dict(**translations['ko'], translations=translations, dwell_time_s=None,
                evaluation_mode='manual', automatic_success=False)


def task_catalog(assets):
    rows = []
    for task in TASKS:
        spec = task_info(task)
        available, missing, error = assets.readiness(task)
        rows.append(dict(id=task, description=spec['title_en'], available=available,
                         missing_assets=missing, asset_error=error, dwell_time_s=None,
                         success_criteria=success_description(task), evaluation_mode='manual'))
    return rows
