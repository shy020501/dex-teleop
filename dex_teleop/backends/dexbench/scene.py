"""Compose the pinned, unmodified DexBench environments after Kit has launched."""
from pathlib import Path


CAMERAS = {'cam_task': 'robot_pov_left_cam', 'cam_wrist_left': 'wrist_left_cam',
           'cam_wrist_right': 'wrist_right_cam'}


def configure_asset_root():
    # Must run before modules import the package's path constants. In Docker the
    # same directory is also mounted at the upstream package's assets location.
    import dexbench_arena
    from .paths import ASSET_ROOT
    dexbench_arena.ASSETS_DIR = Path(ASSET_ROOT)
    dexbench_arena.RBY1_WUJI2_BETA2_ASSETS_DIR = (
        Path(ASSET_ROOT)/'embodiments/simready/rby1m_wujihand2_beta2/source')


def launch_application(device='cuda:0'):
    configure_asset_root()
    from isaaclab_arena.cli.isaaclab_arena_cli import get_isaaclab_arena_cli_parser
    from dexbench_arena.app_launch import launch_isaac_app
    parser = get_isaaclab_arena_cli_parser()
    # This Isaac Lab revision selects headless mode with --viz none. Its old
    # --headless CLI flag no longer exists. RTX cameras remain enabled.
    args = parser.parse_args(['--viz', 'none', '--enable_cameras', '--device', device,
        '--kit_args=--/renderer/multiGpu/enabled=false --/renderer/activeGpu=0'])
    return launch_isaac_app(args)


def environment_arguments(task, config, device='cuda:0'):
    from .task_info import task_info
    info = task_info(task)
    settings = config.get('simulation', {})
    argv = ['--viz', 'none', '--enable_cameras', '--device', device, '--num_envs', '1',
            '--seed', str(settings.get('seed', 42)), info['environment_name']]
    options = dict(info['environment'], embodiment='dexbench_rby1_wuji2_joint',
                   arm_mode='dual_arm', rby1_control_hz=int(settings.get('control_hz', 30)),
                   wrist_cameras=True)
    for name, value in options.items():
        if value is None:
            continue
        flag = '--'+name
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        elif isinstance(value, (tuple, list)):
            argv.extend([flag, *map(str, value)])
        else:
            argv.extend([flag, str(value)])
    return argv


def configure_cameras(embodiment, config):
    """Render exactly one native head view and the two native palm views."""
    rig = embodiment.camera_config
    rig.robot_pov_right_cam = None
    settings = config.get('render', {})
    for name, source in CAMERAS.items():
        camera = getattr(rig, source)
        if camera is None:
            raise RuntimeError(f'DexBench camera is missing: {source}')
        main = name == 'cam_task'
        camera.width = int(settings.get('main_width' if main else 'wrist_width', 1280 if main else 320))
        camera.height = int(settings.get('main_height' if main else 'wrist_height', 720 if main else 240))
        if not (16 <= camera.width <= 1920 and 16 <= camera.height <= 1080):
            raise ValueError('Camera resolution must be within 16x16 .. 1920x1080')
        camera.data_types = ['rgb']
    rig.set_use_tiled_camera(False)


class ManualEpisodeBoundary:
    """Keep the terminal world intact until the dashboard has copied its frame.

    Native termination terms still run once in env.step, and their returned
    success/failure flags are untouched. Only the automatic-reset selection is
    suppressed. Explicit env.reset() retains upstream Newton reset handling.
    """
    def __init__(self, env):
        self.manager = env.termination_manager
        self.original = self.manager.compute
        self.done = self.success = self.timeout = False
        if 'success' not in self.manager.active_terms:
            raise RuntimeError('Selected DexBench task has no native success term')
        self.manager.compute = self.compute

    def compute(self):
        result = self.original()
        self.done = bool(result[0].item())
        self.success = bool(self.manager.get_term('success')[0].item())
        self.timeout = bool(self.manager.time_outs[0].item())
        # Never zero the original buffer in place: the manager owns its flags.
        return result.clone().fill_(False)

    def clear(self):
        self.done = self.success = self.timeout = False

    def close(self):
        self.manager.compute = self.original


def create_environment(task, config, device='cuda:0'):
    from dexbench_arena.cli import get_dexbench_arena_cli_parser, get_arena_builder_from_cli
    parser = get_dexbench_arena_cli_parser()
    args = parser.parse_args(environment_arguments(task, config, device))
    builder = get_arena_builder_from_cli(args)
    embodiment = builder.arena_env.embodiment
    configure_cameras(embodiment, config)
    cfg, kwargs = builder.compose_manager_cfg()
    # Shared HDF5 collection owns the episode buffer. Arena reinstalls its
    # progress-tracker hook (which writes no data) when loading the managers.
    cfg.recorders = None
    cfg.episode_recorders = {}
    env = builder.make_registered(cfg, kwargs).unwrapped
    try:
        boundary = ManualEpisodeBoundary(env)
        env.reset()
    except BaseException:
        env.close()
        raise
    return env, embodiment, boundary


def close_environment(env):
    if env is None:
        return
    env.close()
    # Isaac Sim is process-global, so release the scene before loading another.
    from isaaclab_arena.utils.isaaclab_utils.simulation_app import teardown_simulation_app
    teardown_simulation_app(make_new_stage=True)
