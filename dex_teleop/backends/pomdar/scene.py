"""Compose the pinned ORCA/task MJCF for native, headless MuJoCo.

Original body, joint, actuator and collision definitions remain unchanged.
Only asset paths, explicit timestep and viewer cameras are adapted.
"""
import ast
import copy
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

from .assets import AssetDownloads, asset_path, destination
from .paths import SOURCE_ROOT, ASSET_ROOT, SOURCE_REVISION
from .task_info import task_info

CAMERA_NAMES = ('cam_task', 'cam_wrist_right')
MAIN_CAMERAS = ('oblique_v1', 'table_top_v1', 'table_front_v1', 'table_front_v2')


def _camera_axes(position, target, up=(0., 0., 1.)):
    import numpy as np
    z = np.asarray(position, dtype=float)-np.asarray(target, dtype=float)
    z /= np.linalg.norm(z)
    x = np.cross(np.asarray(up, dtype=float), z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return ' '.join(f'{value:.12g}' for value in [*x, *y])


def _template(source_root):
    # Read the literal; importing the example would start torch/GUI dependencies.
    tree = ast.parse(destination(source_root, 'teleop/webcam_teleop.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '_SCENE_TEMPLATE'
                                               for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Pinned POMDAR scene template is missing')


def scene_xml(task_id, *, timestep=.002, main_size=(1280, 720), main_camera='oblique_v1',
              source_root=SOURCE_ROOT, asset_root=ASSET_ROOT):
    spec = task_info(task_id)
    if not 0 < timestep <= .01:
        raise ValueError('POMDAR physics timestep must be in (0, 0.01] seconds')
    width, height = (int(value) for value in main_size)
    if not (64 <= width <= 4096 and 64 <= height <= 4096):
        raise ValueError('Camera dimensions must be in 64..4096')
    if main_camera not in MAIN_CAMERAS:
        raise ValueError(f'Unknown POMDAR main camera: {main_camera!r}; choose {MAIN_CAMERAS}')
    root = ET.fromstring(_template(source_root).format(task_include=f'<include file="tasks/{spec["fragment"]}.xml"/>'))
    for include in list(root.findall('include')):
        relative = 'sim/'+include.attrib['file']
        fragment = ET.parse(destination(source_root, relative)).getroot()
        root.remove(include)
        for element in fragment:
            root.append(copy.deepcopy(element))
    root.find('option').set('timestep', f'{float(timestep):.12g}')
    root.find('visual/quality').set('shadowsize', '2048')
    settings = root.find('visual/global')
    settings.set('offwidth', str(max(320, width)))
    settings.set('offheight', str(max(240, height)))
    for element in root.iter():
        reference = element.get('file')
        if reference:
            if element.tag not in ('mesh', 'texture', 'hfield', 'skin'):
                raise ValueError('Unsupported scene file reference: '+element.tag)
            reference = reference.removeprefix('assets/')
            element.set('file', str(destination(asset_root, asset_path(reference)).resolve()))
    # Versioned cameras preserve old recordings' model identity and replay view.
    world = root.find('worldbody')
    if main_camera == 'table_top_v1':
        # The native 1.2 x 0.8 m tabletop is centered at world (0, 0, 0).
        # Explicit screen-up avoids the singular Z-up look-at for a vertical view.
        position, target, up, fovy = (0., 0., 1.05), (0., 0., 0.), (0., 1., 0.), '50'
    elif main_camera == 'table_front_v1':
        # Elevated in front of the table, looking down about 49 degrees toward
        # the central task area. +X stays screen-right and +Y moves farther away.
        position, target, up, fovy = (0., -.85, 1.05), (0., 0., .08), (0., 0., 1.), '48'
    elif main_camera == 'table_front_v2':
        # Lower and slightly closer than v1, with a gentler 46-degree downward view.
        position, target, up, fovy = (0., -.80, .90), (0., 0., .08), (0., 0., 1.), '48'
    else:
        position, target, up, fovy = (.52, -.66, .70), (.12, .06, .19), (0., 0., 1.), '48'
    ET.SubElement(world, 'camera', name='cam_task', pos=' '.join(map(str, position)),
                  xyaxes=_camera_axes(position, target, up=up), fovy=fovy)
    palm = next((body for body in root.iter('body') if body.get('name') == 'palm'), None)
    if palm is None:
        raise ValueError('ORCA palm body is missing')
    wrist_position, wrist_target = (0., -.16, -.03), (0., 0., .08)
    ET.SubElement(palm, 'camera', name='cam_wrist_right', pos=' '.join(map(str, wrist_position)),
                  xyaxes=_camera_axes(wrist_position, wrist_target, up=(0., -1., 0.)), fovy='75')
    return ET.tostring(root, encoding='unicode')


def build_model(task_id, *, timestep=.002, main_size=(1280, 720), main_camera='oblique_v1',
                source_root=SOURCE_ROOT, asset_root=ASSET_ROOT):
    import mujoco
    available, _, error = AssetDownloads(root=asset_root, source_root=source_root).readiness(task_id)
    if not available:
        raise FileNotFoundError(f'POMDAR assets are not ready for {task_id}: {error or "download task assets first"}')
    xml = scene_xml(task_id, timestep=timestep, main_size=main_size, main_camera=main_camera,
                    source_root=source_root, asset_root=asset_root)
    model = mujoco.MjModel.from_xml_string(xml)
    root_id = model.body('root').id
    hand_actuators = []
    for index in range(model.nu):
        if model.actuator_trntype[index] != mujoco.mjtTrn.mjTRN_JOINT:
            continue
        joint = int(model.actuator_trnid[index, 0])
        body = int(model.jnt_bodyid[joint])
        while body > 0 and body != root_id:
            body = int(model.body_parentid[body])
        if body == root_id:
            hand_actuators.append(index)
    # C1 includes a task motor in addition to the ORCA position actuators.
    # It must remain part of the native scene and recorded control vector.
    if len(hand_actuators) != 16 or model.nmocap != 1:
        raise ValueError('Unexpected pinned ORCA actuator/mocap layout')
    return model


def reset_data(model, data):
    import mujoco
    mujoco.mj_resetData(model, data)
    mujoco.mj_forward(model, data)


def model_identity(task_id, *, timestep=.002, main_size=(1280, 720), main_camera='oblique_v1',
                   source_root=SOURCE_ROOT, asset_root=ASSET_ROOT):
    xml = scene_xml(task_id, timestep=timestep, main_size=main_size, main_camera=main_camera,
                    source_root=source_root, asset_root=asset_root)
    portable = xml.replace(str(Path(asset_root).resolve()), '${POMDAR_ASSETS}')
    return dict(task=task_id, source_revision=SOURCE_REVISION,
                model_sha256=hashlib.sha256(portable.encode()).hexdigest(), physics_dt=float(timestep),
                cameras=list(CAMERA_NAMES), main_size=list(main_size), robot='orca_v1_nowrist')
