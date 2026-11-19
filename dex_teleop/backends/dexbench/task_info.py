"""The 15 upstream tasks, with teleoperation starts and native success criteria.

The catalog is pinned to sources.lock. It is independent of Kit imports so the
browser can show all tasks even before their payloads have been downloaded.
"""
import copy
from .paths import ASSET_ROOT

DEFAULT_TASK = 'db_h1'
TASKS = {'db_h1': {'id': 'db_h1',
           'description': 'DB-H1 · Plate stacking',
           'description_ko': 'DB-H1 · 접시 쌓기',
           'environment_name': 'dexbench_hotel_fb_e4_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'stack_plates',
                           'anchor': 'A_h1',
                           'rby1_control_hz': 30,
                           'hide_replay_prims': True,
                           'stack_alignment_point': 'rim',
                           'success_follows_dest_draw': True,
                           'episode_length_s': 55.0},
           'scene_group': 'e4',
           'episode_limit_s': 55.0,
           'success_criteria': {'summary': 'Move the source plate stack onto the destination stack; all '
                                           'plates must be seated, aligned and stable, with hands clear as '
                                           'required by the task.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Move the source plate stack onto the '
                                                                   'destination stack; all plates must be '
                                                                   'seated, aligned and stable, with hands '
                                                                   'clear as required by the task.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '접시 더미를 목표 접시 더미 위에 옮깁니다. 모든 접시가 정렬되어 '
                                                                   '안정적으로 안착하고 환경이 요구하는 손 이격 조건을 충족해야 합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_h2': {'id': 'db_h2',
           'description': 'DB-H2 · Lift and pour',
           'description_ko': 'DB-H2 · 주전자 들어 따르기',
           'environment_name': 'dexbench_hotel_fb_e4_pour_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'lift_and_pour',
                           'anchor': 'A_h2',
                           'rby1_control_hz': 30,
                           'idealisk_pour': True,
                           'base_offset_y': 0.15,
                           'pour_station_offset_y': 0.05,
                           'start_pose': 'teleop_ready',
                           'newton_nconmax': 16384,
                           'newton_njmax': 65536,
                           'newton_num_substeps': 8,
                           'pour_jug_jitter_xy': 0.02,
                           'pour_jug_jitter_yaw_deg': 10.0,
                           'pour_goblet_jitter_xy': 0.01,
                           'episode_length_s': 90.0},
           'scene_group': 'e4',
           'episode_limit_s': 90.0,
           'success_criteria': {'summary': 'Lift the jug and pour into the glass; the native evaluator '
                                           'checks the required retained volume, spill limit and stable '
                                           'hold.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Lift the jug and pour into the glass; '
                                                                   'the native evaluator checks the '
                                                                   'required retained volume, spill limit '
                                                                   'and stable hold.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '주전자를 들어 잔에 따릅니다. 환경이 잔에 담긴 양, 흘린 양의 '
                                                                   '한도와 안정 유지 조건을 판정합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_h3': {'id': 'db_h3',
           'description': 'DB-H3 · Utensil extraction',
           'description_ko': 'DB-H3 · 식기 꺼내기',
           'environment_name': 'dexbench_hotel_fb_e4_h3_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'extract_utensil',
                           'anchor': 'A_h3',
                           'rby1_control_hz': 30,
                           'wrist_cameras': True,
                           'camera_scale': 0.5,
                           'head_yaw_deg': 5.0,
                           'head_pitch_deg': 45.0,
                           'h3_drive_profile': 'collection_v1',
                           'episode_length_s': 25.0},
           'scene_group': 'e4',
           'episode_limit_s': 25.0,
           'success_criteria': {'summary': 'Place the specified utensil from the in-hand bundle into the '
                                           'bin and release it; keep the other two utensils near the '
                                           'support wrist above the table.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Place the specified utensil from the '
                                                                   'in-hand bundle into the bin and '
                                                                   'release it; keep the other two '
                                                                   'utensils near the support wrist above '
                                                                   'the table.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '손에 든 식기 묶음에서 지정 식기를 꺼내 통에 놓고 손을 뗍니다. '
                                                                   '나머지 두 식기는 지지 손목 부근에서 테이블 위 높이로 유지합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_l1': {'id': 'db_l1',
           'description': 'DB-L1 · Conveyor polybag sorting',
           'description_ko': 'DB-L1 · 컨베이어 포장 봉투 분류',
           'environment_name': 'dexbench_logistics_e2_l1_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'pick_polybag_from_conveyor',
                           'anchor': 'A_L1',
                           'rby1_control_hz': 30,
                           'belt_speed': 0.2,
                           'l1_stream_mode': 'episode',
                           'l1_stream_bags': 3,
                           'l1_stream_boxes': 2,
                           'l1_stream_lead': 'bag',
                           'l1_item_y': [0.8, 0.95],
                           'l1_spacing': [0.72, 0.88],
                           'polybag_coupling': 'two_way',
                           'polybag_contact_ke': 3000.0,
                           'polybag_contact_kd': 1.0,
                           'polybag_collider_ke': 3000.0,
                           'polybag_collider_kd': 1.0,
                           'polybag_contact_mu': 1.0,
                           'newton_solver': 'newton',
                           'polybag_stepping': 'a',
                           'polybag_vbd_iterations': 8,
                           'polybag_collider_mu': 3.0,
                           'l1_bag_wrist_link_collision': 'off',
                           'polybag_contact_barrier': 'off',
                           'polybag_vbd_init': 'contact',
                           'polybag_film_hessian': 'psd',
                           'polybag_tet_poisson': 0.45,
                           'polybag_tet_damping': 10.0,
                           'l1_bag_yaw_off_belt_deg': [0.0, 30.0],
                           'episode_length_s': 60.0},
           'scene_group': 'e2',
           'episode_limit_s': 60.0,
           'success_criteria': {'summary': 'Remove the required polybags from the conveyor into the '
                                           'collection area while allowing the boxes to pass; the native '
                                           'stream evaluator decides completion.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Remove the required polybags from the '
                                                                   'conveyor into the collection area '
                                                                   'while allowing the boxes to pass; the '
                                                                   'native stream evaluator decides '
                                                                   'completion.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '필요한 포장 봉투를 컨베이어에서 수집 영역으로 옮기고 상자는 '
                                                                   '통과시킵니다. 환경의 물체 흐름 판정으로 종료합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_l2': {'id': 'db_l2',
           'description': 'DB-L2 · Parcel lifting',
           'description_ko': 'DB-L2 · 소포 들어 올리기',
           'environment_name': 'dexbench_logistics_center_e2_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'lift_to_conveyor',
                           'anchor': 'A1_CAGE',
                           'rby1_control_hz': 30,
                           'cage_box_alt_scales': [0.85, 1.15],
                           'base_jitter_radius': 0.0,
                           'newton_num_substeps': 8,
                           'episode_length_s': 25.0},
           'scene_group': 'e2',
           'episode_limit_s': 25.0,
           'success_criteria': {'summary': 'Lift the parcel clear of its cage and leave it settled on the '
                                           'conveyor without dropping it.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Lift the parcel clear of its cage and '
                                                                   'leave it settled on the conveyor '
                                                                   'without dropping it.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '소포를 케이지에서 꺼내 떨어뜨리지 않고 컨베이어 위에 안정적으로 '
                                                                   '놓습니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_l3': {'id': 'db_l3',
           'description': 'DB-L3 · Invoice packing',
           'description_ko': 'DB-L3 · 송장 넣기',
           'environment_name': 'dexbench_logistics_center_e2_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'pack_invoice',
                           'anchor': 'A3_BENCH',
                           'rby1_control_hz': 30,
                           'paper_coupling': 'one_way_proxy',
                           'paper_hand_colliders': 'official_hull',
                           'paper_num_sheets': 4,
                           'paper_resolution': '4x6',
                           'paper_substeps': 16,
                           'paper_iterations': 10,
                           'paper_contact_mu': 0.2,
                           'paper_self_contact_radius': 0.002,
                           'base_jitter_low': [-0.01, -0.02, -2.5],
                           'base_jitter_high': [0.02, 0.015, 2.0],
                           'episode_length_s': 25.0},
           'scene_group': 'e2',
           'episode_limit_s': 25.0,
           'success_criteria': {'summary': 'Take a sheet from the paper stack and place it inside the open '
                                           'outbound box.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Take a sheet from the paper stack and '
                                                                   'place it inside the open outbound box.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '종이 더미에서 한 장을 집어 열린 배송 상자 안에 넣습니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_l4': {'id': 'db_l4',
           'description': 'DB-L4 · Barcode-up parcel delivery',
           'description_ko': 'DB-L4 · 바코드 위로 소포 전달',
           'environment_name': 'dexbench_logistics_e2_l4_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'deliver_parcels_barcode_up',
                           'anchor': 'A_L4',
                           'rby1_control_hz': 30,
                           'belt_speed': 0.45,
                           'end_on_parcel_failure': True,
                           'l4_rotation_draw': 'id',
                           'l4_mass_draw': 'id',
                           'episode_length_s': 95.0},
           'scene_group': 'e2',
           'episode_limit_s': 95.0,
           'success_criteria': {'summary': 'Deliver every required parcel through the output region with '
                                           'its barcode facing upward; parcel failures end the episode.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Deliver every required parcel through '
                                                                   'the output region with its barcode '
                                                                   'facing upward; parcel failures end the '
                                                                   'episode.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '필요한 소포를 모두 바코드가 위로 향한 상태로 출구에 전달합니다. '
                                                                   '소포 처리 실패 시 종료합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_m1': {'id': 'db_m1',
           'description': 'DB-M1 · Drawer insertion',
           'description_ko': 'DB-M1 · 서랍 삽입',
           'environment_name': 'dexbench_manufacturing_e3_m1_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'pick_insert_drawer',
                           'anchor': 'A_m1',
                           'start_pose': 'teleop_ready',
                           'm1_tabletop_base_xy': [0.15, -0.9757],
                           'm1_tabletop_base_yaw_deg': 180.0,
                           'm1_tabletop_crouch_deg': 55.0,
                           'm1_tabletop_drawer_mass_kg': 0.55,
                           'm1_physics_profile': 'damped_contacts',
                           'rby1_control_hz': 30,
                           'camera_hz': 30,
                           'episode_length_s': 100.0},
           'scene_group': 'e3',
           'episode_limit_s': 100.0,
           'success_criteria': {'summary': 'Pick up the drawer and seat it in the cabinet opening, meeting '
                                           'the native insertion depth, alignment and stability '
                                           'conditions.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Pick up the drawer and seat it in the '
                                                                   'cabinet opening, meeting the native '
                                                                   'insertion depth, alignment and '
                                                                   'stability conditions.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '서랍을 집어 수납장 입구에 넣고 환경의 삽입 깊이·정렬·안정 조건을 '
                                                                   '만족합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_m2': {'id': 'db_m2',
           'description': 'DB-M2 · Connector insertion',
           'description_ko': 'DB-M2 · 커넥터 삽입',
           'environment_name': 'dexbench_manufacturing_e3_m2_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'insert_connector',
                           'anchor': 'A_m2',
                           'rby1_control_hz': 30,
                           'arm_mode': 'dual_arm',
                           'm2_plug_condim': 3,
                           'm2_fingertip_condim': 4,
                           'm2_wire_collision': 'panel',
                           'm2_wire_rest_spot': [-0.008, 0.008, 0.0872665],
                           'episode_length_s': 70.0},
           'scene_group': 'e3',
           'episode_limit_s': 70.0,
           'success_criteria': {'summary': 'Insert the loose harness plug into its mate while keeping the '
                                           'already seated connector in place.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Insert the loose harness plug into its '
                                                                   'mate while keeping the already seated '
                                                                   'connector in place.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '분리된 하네스 플러그를 대응 소켓에 꽂고 이미 연결된 커넥터를 '
                                                                   '유지합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_m3': {'id': 'db_m3',
           'description': 'DB-M3 · Power-driver fastening',
           'description_ko': 'DB-M3 · 전동 드라이버 체결',
           'environment_name': 'dexbench_manufacturing_e3_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'power_driver_fastening',
                           'anchor': 'A_m3',
                           'rby1_control_hz': 30,
                           'base_jitter_radius': 0.01,
                           'episode_length_s': 70.0},
           'scene_group': 'e3',
           'episode_limit_s': 70.0,
           'success_criteria': {'summary': 'Use the driver to seat both task bolts; the native evaluator '
                                           'requires both to remain seated in two consecutive physics '
                                           'states.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Use the driver to seat both task '
                                                                   'bolts; the native evaluator requires '
                                                                   'both to remain seated in two '
                                                                   'consecutive physics states.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '드라이버로 두 볼트를 체결합니다. 두 볼트가 연속된 두 물리 상태에서 '
                                                                   '모두 안착해야 합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_m4': {'id': 'db_m4',
           'description': 'DB-M4 · Hand-start threaded leg',
           'description_ko': 'DB-M4 · 손으로 다리 나사 체결',
           'environment_name': 'dexbench_manufacturing_e3_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'hand_start_leg',
                           'anchor': 'A_m4',
                           'rby1_control_hz': 30,
                           'episode_length_s': 115.0},
           'scene_group': 'e3',
           'episode_limit_s': 115.0,
           'success_criteria': {'summary': 'Pick up the leveling leg and start its thread in the cabinet '
                                           'bracket to the required engagement depth.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Pick up the leveling leg and start its '
                                                                   'thread in the cabinet bracket to the '
                                                                   'required engagement depth.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '높이 조절 다리를 집어 캐비닛 브래킷 나사에 맞추고 필요한 깊이까지 '
                                                                   '손으로 체결합니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_m5': {'id': 'db_m5',
           'description': 'DB-M5 · Moving-box kitting',
           'description_ko': 'DB-M5 · 이동 상자 부품 담기',
           'environment_name': 'dexbench_manufacturing_m5_moving_conveyor_kitting_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'task': 'place_part_in_kit_box',
                           'anchor': 'A_m5',
                           'rby1_control_hz': 30,
                           'arm_mode': 'left',
                           'kit_box_empty_compartments': [0, 3],
                           'episode_length_s': 30.0},
           'scene_group': 'e3',
           'episode_limit_s': 30.0,
           'success_criteria': {'summary': 'Pick the target part from the bin and release it into an '
                                           'allowed empty compartment of the moving kit box.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Pick the target part from the bin and '
                                                                   'release it into an allowed empty '
                                                                   'compartment of the moving kit box.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '부품함에서 목표 부품을 집어 이동하는 키트 상자의 지정 빈 칸에 '
                                                                   '놓습니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_r1': {'id': 'db_r1',
           'description': 'DB-R1 · Fruit picking',
           'description_ko': 'DB-R1 · 과일 집기',
           'environment_name': 'dexbench_retail_v1_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'arm_mode': 'dual_arm',
                           'task': 'pick_fruit_from_display',
                           'scene_layout': 'fc_260821',
                           'anchor': 'Fruit',
                           'start_pose': 'teleop_home',
                           'rby1_control_hz': 30,
                           'with_full_items': True,
                           'episode_length_s': 50.0},
           'scene_group': 'e1',
           'episode_limit_s': 50.0,
           'success_criteria': {'summary': 'Place one fruit of the instructed kind in the basket and '
                                           'release it at rest; keep all other fruit on the tray.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Place one fruit of the instructed kind '
                                                                   'in the basket and release it at rest; '
                                                                   'keep all other fruit on the tray.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '지시된 종류의 과일 하나를 바구니에 넣고 놓아 안정시킵니다. 다른 '
                                                                   '과일은 모두 트레이에 남겨 둡니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_r2': {'id': 'db_r2',
           'description': 'DB-R2 · Beverage retrieval',
           'description_ko': 'DB-R2 · 음료 꺼내기',
           'environment_name': 'dexbench_retail_v1_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'arm_mode': 'dual_arm',
                           'task': 'retrieve_beverage_from_clutter',
                           'scene_layout': 'fc_260821',
                           'anchor': 'Fridge',
                           'start_pose': 'teleop_home',
                           'rby1_control_hz': 30,
                           'with_full_items': True,
                           'fridge_shelf3_raise_m': 0.065,
                           'r2_stock_rule': 'unique_target',
                           'r2_stock_fillers': 'TC2,TC1',
                           'success_tier': 'strict',
                           'episode_length_s': 35.0},
           'scene_group': 'e1',
           'episode_limit_s': 35.0,
           'success_criteria': {'summary': 'Place the requested drink in the basket at rest without '
                                           'disturbing or toppling its watched neighbours.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Place the requested drink in the '
                                                                   'basket at rest without disturbing or '
                                                                   'toppling its watched neighbours.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '지정 음료를 바구니 안에 안정적으로 놓고 주변 감시 대상 물체를 '
                                                                   '건드리거나 넘어뜨리지 않습니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}},
 'db_r3': {'id': 'db_r3',
           'description': 'DB-R3 · Shelf slot placement',
           'description_ko': 'DB-R3 · 선반 슬롯 배치',
           'environment_name': 'dexbench_retail_v1_environment',
           'environment': {'embodiment': 'dexbench_rby1_wuji2_joint',
                           'arm_mode': 'dual_arm',
                           'task': 'place_box_in_slot',
                           'scene_layout': 'fc_260821',
                           'anchor': 'Gondola',
                           'start_pose': 'r3_carry',
                           'base_offset_x': 0.05,
                           'base_offset_y': -0.1,
                           'rby1_control_hz': 30,
                           'with_full_items': True,
                           'facing_width_mm': None,
                           'box_jitter_xy': 0.02,
                           'episode_length_s': 75.0},
           'scene_group': 'e1',
           'episode_limit_s': 75.0,
           'success_criteria': {'summary': 'Place the carton upright in the empty shelf slot, aligned and '
                                           'at rest; release it, move the wrist clear, and leave its '
                                           'neighbours undisturbed.',
                                'details': ['Uses the original task success predicate; no extra hold time '
                                            'is added.'],
                                'automatic_success': True,
                                'translations': {'en': {'summary': 'Place the carton upright in the empty '
                                                                   'shelf slot, aligned and at rest; '
                                                                   'release it, move the wrist clear, and '
                                                                   'leave its neighbours undisturbed.',
                                                        'details': ['Uses the original task success '
                                                                    'predicate; no extra hold time is '
                                                                    'added.']},
                                                 'ko': {'summary': '상자를 빈 선반 슬롯에 똑바로 정렬하여 안정적으로 놓고 손을 뗍니다. '
                                                                   '손목을 이격하고 주변 물체를 건드리지 않습니다.',
                                                        'details': ['기존 환경의 성공 판정을 사용하며 추가 유지 시간은 넣지 '
                                                                    '않습니다.']}}}}}

def task_info(task):
    if task not in TASKS:
        raise ValueError(f'Unknown DexBench task: {task!r}')
    result = copy.deepcopy(TASKS[task])
    if task == 'db_r1':
        result['environment']['r1_layouts'] = str(ASSET_ROOT/'runtime_inputs/db_r1_layouts/eval')
    elif task == 'db_h3':
        result['environment']['h3_inputs_dir'] = str(ASSET_ROOT/'runtime_inputs/db_h3_inputs')
    return result


def task_catalog(assets):
    result = []
    for task in TASKS:
        row = task_info(task)
        available, missing, error = assets.readiness(task)
        row.update(available=available, missing_assets=missing, asset_error=error)
        result.append(row)
    return result
