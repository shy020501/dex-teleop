"""Construct the published robosuite tasks, using their real robot controllers."""
import copy
import random
import numpy as np
from .task_info import task_info


def create_environment(task, config):
    import robosuite
    import dexmimicgen  # registers the official task classes
    from robosuite.controllers import load_composite_controller_config
    spec=task_info(task)
    controllers=[]
    for name in spec['robots']:
        controller=load_composite_controller_config(robot=name)
        if controller['type']=='BASIC':
            for part,arm in controller['body_parts'].items():
                if part not in ('right','left'):continue
                arm.update(input_type='absolute',input_ref_frame='world')
        else:
            options=controller['composite_controller_specific_configs']
            options.update(ik_input_type='absolute',ik_input_ref_frame='world')
        controllers.append(controller)
    seed=int(config.get('simulation',{}).get('seed',0))
    np.random.seed(seed)
    random.seed(seed)
    env=robosuite.make(env_name=task,robots=spec['robots'],controller_configs=controllers,
        has_renderer=False,has_offscreen_renderer=False,use_camera_obs=False,use_object_obs=True,
        control_freq=int(config.get('simulation',{}).get('control_hz',20)),
        ignore_done=True,hard_reset=False,initialization_noise=None)
    env.lite_physics=False
    env.rng=np.random.default_rng(seed)
    # Upstream task samplers default to independent, unseeded Generators.
    # Share the seeded environment generator throughout the placement tree.
    def seed_sampler(sampler):
        if hasattr(sampler,'rng'):sampler.rng=env.rng
        # Some objects (notably Transport's hammer) sample init_quat from
        # their own Generator when the placement sampler accesses it.
        for obj in getattr(sampler,'mujoco_objects',[]):
            if hasattr(obj,'rng'):obj.rng=env.rng
        for child in getattr(sampler,'samplers',{}).values():seed_sampler(child)
    seed_sampler(env.placement_initializer)
    env._teleop_reset_rng=(copy.deepcopy(env.rng.bit_generator.state),np.random.get_state(),random.getstate())
    reset_environment(env)
    return env


def reset_environment(env):
    """Replay the complete upstream reset, including Python task/controllers.

    Reusing the pre-reset RNG reproduces object placements and model fixtures;
    restoring only qpos would leave task state and IK/controller state stale.
    """
    rng,numpy_state,python_state=env._teleop_reset_rng
    env.rng.bit_generator.state=copy.deepcopy(rng)
    np.random.set_state(numpy_state)
    random.setstate(python_state)
    return env.reset()


def camera_mapping(env):
    names=list(env.sim.model.camera_names)
    mapping={'cam_task':'agentview'}
    for side,index,arm in arm_bindings(env):
        prefix=env.robots[index].robot_model.naming_prefix
        options=[n for n in names if n.startswith(prefix) and 'eye_in_hand' in n]
        choice=next((n for n in options if side in n),options[0] if options else None)
        if choice:mapping['cam_wrist_'+side]=choice
    if mapping['cam_task'] not in names:
        mapping['cam_task']='frontview' if 'frontview' in names else names[0]
    return mapping


def arm_bindings(env):
    if len(env.robots)==2:
        return [('right',0,env.robots[0].arms[0]),('left',1,env.robots[1].arms[0])]
    return [(side,0,side) for side in ('right','left') if side in env.robots[0].arms]
