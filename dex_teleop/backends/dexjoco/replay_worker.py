"""Replay native policy/controller steps, including DexJoCo task side effects."""
import os


def parent_guard():
    expected=int(os.environ.get('DEX_TELEOP_PARENT_PID','0'))
    if not expected:return
    import ctypes,signal
    if expected<=1 or os.getppid()!=expected:raise SystemExit('Replay parent exited')
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGKILL,0,0,0):raise OSError(ctypes.get_errno(), 'prctl failed')
    if os.getppid()!=expected:raise SystemExit('Replay parent exited')


if __name__=='__main__':parent_guard()

import json
from pathlib import Path
import time
import h5py
import numpy as np
from PIL import Image
from .recording import model_signature,restore_state
from .scene import Scene,MODEL_FIELDS,copy_data
from .paths import SOURCE_REVISION


def atomic_json(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,allow_nan=False));temporary.replace(path)


def generate(config, *, renderer_factory=None):
    import mujoco
    if config['mujoco_version']!=mujoco.__version__:raise ValueError('MuJoCo version differs from recording')
    launch=config['launch']
    if launch['source_revision']!=SOURCE_REVISION:raise ValueError('DexJoCo source revision differs from recording')
    source,directory=Path(config['source']),Path(config['directory'])
    stat=source.stat()
    if [stat.st_ino,stat.st_size,stat.st_mtime_ns]!=config['source_stat']:raise ValueError('Episode changed during replay request')
    scene=Scene(launch['task'],launch['simulation']);renderer=None
    try:
        with h5py.File(source,'r') as h:
            binary=directory/'model.mjb';binary.write_bytes(h['replay/model_binary'][:].tobytes())
            scene.model=mujoco.MjModel.from_binary_path(str(binary));binary.unlink()
            scene.data=mujoco.MjData(scene.model);scene.env._model=scene.model;scene.env._data=scene.data
            scene._pose_data=mujoco.MjData(scene.model)
            model,data=scene.model,scene.data
            if model_signature(model)!=config['signature']:raise ValueError('Saved model/actuator layout does not match recording')
            if renderer_factory is None:
                from .rendering import Renderer
                renderer_factory=Renderer
            renderer=renderer_factory(scene,launch['render'])
            if launch.get('camera_mapping'):
                scene.cameras={name:model.camera(camera).id for name,camera in launch['camera_mapping'].items()}
            steps,cameras,n=config['steps'],config['cameras'],config['frames']
            snapshots=h['replay/state/integration'];spec=int(config['state_spec'])
            if spec!=int(mujoco.mjtState.mjSTATE_INTEGRATION):raise ValueError('Incomplete integration state')
            if snapshots.shape!=(n,mujoco.mj_stateSize(model,spec)):raise ValueError('Invalid integration state dimensions')
            restore_state(model,data,snapshots[0],spec,forward=True)
            restore_state(model,data,snapshots[0],spec)
            scene.restore_native(json.loads(h['replay/native_initial'][()]))
            scene.physics_step=steps[0]
            policy=h['replay/policy'];starts=policy['sim_step'][:];actions=policy['action'][:];substeps=policy['substeps'][:]
            event=0;metrics=[];cache_bytes=0;started=time.monotonic()
            actual,reference=mujoco.MjData(model),mujoco.MjData(model)
            from .contacts import FingerContacts
            contacts=FingerContacts(scene)
            robot_mask=np.zeros(model.nbody,bool)
            robot_mask[model.geom_bodyid[list(contacts.robot_geoms)]]=True
            object_mask=~robot_mask;object_mask[0]=False
            addresses=np.concatenate([scene.parts[s]['qadr'] for s in scene.sides])
            def save(view,camera,index,rgb):
                nonlocal cache_bytes
                rgb=np.asarray(rgb)
                if rgb.dtype!=np.uint8 or rgb.ndim!=3 or rgb.shape[-1]!=3:raise ValueError('Invalid RGB frame')
                target=directory/view/camera/f'{index:06d}.jpg';target.parent.mkdir(parents=True,exist_ok=True)
                Image.fromarray(rgb).save(target,quality=90,subsampling=0);cache_bytes+=target.stat().st_size
                if cache_bytes>config['max_cache_bytes']:raise RuntimeError('Replay cache limit exceeded')
            for index,target in enumerate(steps):
                while scene.physics_step<target:
                    if event>=len(starts) or starts[event]!=scene.physics_step:raise ValueError('Native action history has a gap')
                    if scene.physics_step+int(substeps[event])>target:raise ValueError('Observation is not at a native control boundary')
                    scene.step(actions[event],substeps=int(substeps[event]));event+=1
                copy_data(model,data,actual);mujoco.mj_forward(model,actual)
                restore_state(model,reference,snapshots[index],spec,forward=True)
                body_error=np.linalg.norm(actual.xpos-reference.xpos,axis=1)
                metrics.append(dict(frame=index,sim_step=target,
                    joint_max_rad=float(np.abs(data.qpos[addresses]-reference.qpos[addresses]).max(initial=0)),
                    object_position_max_m=float(body_error[object_mask].max(initial=0)),
                    link_position_max_m=float(body_error[robot_mask].max(initial=0)),
                    qpos_max_abs=float(np.abs(data.qpos-reference.qpos).max(initial=0)),
                    qvel_max_abs=float(np.abs(data.qvel-reference.qvel).max(initial=0))))
                images=renderer.capture(data)
                for camera in cameras:
                    save('action',camera,index,images[camera]);save('original',camera,index,h[f'cameras/{camera}/rgb'][index])
                atomic_json(directory/'progress.json',dict(progress=.5*(index+1)/n,message=f'Action replay {index+1} / {n}'))
            for index in range(n):
                for key in MODEL_FIELDS:getattr(model,key)[:]=h[f'replay/model/{key}'][index]
                mujoco.mj_setConst(model,reference);restore_state(model,reference,snapshots[index],spec)
                images=renderer.capture(reference)
                for camera in cameras:save('state',camera,index,images[camera])
                atomic_json(directory/'progress.json',dict(progress=.5+.5*(index+1)/n,message=f'State replay {index+1} / {n}'))
            maxima={key:max(row[key] for row in metrics) for key in metrics[0] if key not in ('frame','sim_step')}
            manifest=dict(frames=n,cameras=cameras,steps=steps,physics_dt=float(model.opt.timestep),metrics=metrics,maxima=maxima,
                cache_bytes=cache_bytes,generation_seconds=time.monotonic()-started,interpolation=False,
                full_control_timeline=True,physics_warmup_steps=0,
                note='Native absolute TCP/Allegro actions are replayed through the pinned task and operational-space controller. Full applied torque/target history is retained separately.')
            atomic_json(directory/'manifest.json',manifest);return manifest
    finally:
        if renderer is not None:renderer.close()
        scene.close()


if __name__=='__main__':
    import sys
    if len(sys.argv)!=2:raise SystemExit('Usage: python -m dex_teleop.backends.dexjoco.replay_worker CONFIG.json')
    generate(json.loads(Path(sys.argv[1]).read_text()))
