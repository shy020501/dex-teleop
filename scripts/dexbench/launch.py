"""Launch the isolated DexBench image with one GPU and the shared localhost UI."""
import argparse
import grp
import os
from pathlib import Path
import pwd
import shlex
import subprocess
from scripts.dexbench.docker_access import docker_prefix

ROOT = Path(__file__).resolve().parents[2]
CONTAINER_ROOT = '/workspace/dex-teleop'
DEFAULT_IMAGE = 'dex-teleop-dexbench:local'


def gpu_index(value):
    if not str(value).isdigit():
        raise ValueError('DEX_TELEOP_GPU_INDEX must be a non-negative physical GPU index')
    return str(int(value))


def docker_command(config_path, mode, argv, *, gpu, image, asset_root, data_root,
                   output_dir, cache_dir, uid=None, gid=None, user=None, group=None):
    """Only this selected physical GPU is visible as cuda:0 inside the container."""
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    user = pwd.getpwuid(uid).pw_name if user is None else user
    group = grp.getgrgid(gid).gr_name if group is None else group
    # Keep the dataset path identical on host and container so Explorer and
    # saved metadata report the real storage location, like the other backends.
    data_path = str(Path(data_root).resolve())
    command = [*docker_prefix(), 'run', '--rm', '--init', '--stop-timeout', '60',
               '--gpus', 'device='+gpu_index(gpu), '--network', 'host', '--shm-size', '2g']
    environment = dict(ACCEPT_EULA='Y', DOCKER_RUN_USER_NAME=user, DOCKER_RUN_USER_ID=str(uid),
        DOCKER_RUN_GROUP_NAME=group, DOCKER_RUN_GROUP_ID=str(gid),
        PYTHONPATH=CONTAINER_ROOT, PYTHONUNBUFFERED='1',
        DEX_TELEOP_DEXBENCH_SOURCE='/workspaces/dexbench_arena',
        DEX_TELEOP_DEXBENCH_ASSETS='/dex-assets', DEX_TELEOP_DEXBENCH_DATA=data_path,
        DEX_TELEOP_SERVER_CONFIG='/dex-config/server.yaml',
        NVIDIA_DRIVER_CAPABILITIES='compute,utility,graphics,display',
        DEBUG_MODE=os.environ.get('DEBUG_MODE', 'false'),
        DEX_TELEOP_BROWSER_PORT=os.environ.get('DEX_TELEOP_BROWSER_PORT', '8080'),
        TIMING_WINDOW=os.environ.get('TIMING_WINDOW', '120'))
    for name, value in environment.items():
        command.extend(['--env', name+'='+value])
    mounts = [(ROOT, CONTAINER_ROOT, True), (config_path, '/dex-config/server.yaml', True),
              (asset_root, '/workspaces/dexbench_arena/assets', True),
              (asset_root, '/dex-assets', False), (data_root, data_path, False),
              (output_dir, '/dex-output', False), (cache_dir, '/home/'+user, False)]
    for source, target, readonly in mounts:
        # Docker --mount uses commas as its delimiter; do not silently split a path.
        if ',' in str(source) or ',' in target:
            raise ValueError('Docker bind paths cannot contain a comma')
        spec = f'type=bind,src={Path(source).resolve()},dst={target}'
        command.extend(['--mount', spec+(',readonly' if readonly else '')])
    runtime = ['/isaac-sim/python.sh', '-m', 'dex_teleop.backends.dexbench.runtime',
               '--config', '/dex-config/server.yaml', '--mode', mode,
               '--device', 'cuda:0', '--output-dir', '/dex-output', '--data-root', data_path, *argv]
    # The pinned image's entrypoint invokes bash -ic "$@": pass ONE quoted
    # command, and exec it so Ctrl+C reaches Python and its recording cleanup.
    command.extend(['--workdir', CONTAINER_ROOT, image, 'exec '+shlex.join(runtime)])
    return command


def launch(config_path, mode, argv):
    from dex_teleop.backends.dexbench.paths import ASSET_ROOT, DATA_ROOT
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--output-dir', type=Path, default=ROOT/'logs/dexbench-live')
    parser.add_argument('--data-root', type=Path, default=DATA_ROOT)
    paths, forwarded = parser.parse_known_args(argv)
    docker = docker_prefix()
    gpu = gpu_index(os.environ.get('DEX_TELEOP_GPU_INDEX', '0'))
    config_path = Path(config_path).resolve()
    if not config_path.is_file():
        raise ValueError(f'Missing server profile: {config_path}')
    if '--help' not in forwarded and '-h' not in forwarded:
        subprocess.run(['nvidia-smi', '-i', gpu, '--query-gpu=index,name,memory.used,utilization.gpu',
                        '--format=csv'], check=True)
    try:
        result = subprocess.run([*docker, 'info', '--format', '{{.ServerVersion}}'],
                                capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError('DexBench needs Docker with NVIDIA container support. For sudo access, run sudo -v first and set DEX_TELEOP_DOCKER_SUDO=true.') from exc
    if result.returncode:
        raise ValueError('Docker is unavailable to this account: '+result.stderr.strip()+
                         '\nIf sudo docker works, set DEX_TELEOP_DOCKER_SUDO=true; keep this Python launcher running as your normal user.')
    image = os.environ.get('DEX_TELEOP_DEXBENCH_IMAGE', DEFAULT_IMAGE)
    if subprocess.run([*docker, 'image', 'inspect', image], capture_output=True).returncode:
        raise ValueError(f'Missing image {image}. Run python scripts/dexbench/build_image.py first.')
    cache = ROOT/'logs/dexbench-cache/home'
    for directory in (paths.data_root, paths.output_dir, cache):
        directory.mkdir(parents=True, exist_ok=True)
    if not ASSET_ROOT.is_dir():
        raise ValueError('Missing DexBench assets. Run scripts/setup_dexbench.sh first.')
    command = docker_command(config_path, mode, forwarded, gpu=gpu, image=image,
        asset_root=ASSET_ROOT, data_root=paths.data_root, output_dir=paths.output_dir, cache_dir=cache)
    print(f'[DexBench] Physical GPU {gpu} -> container CUDA/RTX 0; localhost HTTP and input ports.', flush=True)
    os.execvp(command[0], command)
