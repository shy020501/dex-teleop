"""Prepare the Bench2Dex GPU environment and start its viewer or teleoperation entry point."""
import argparse
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def step_limit(value):
    if not re.fullmatch(r'[0-9]+', value):
        raise argparse.ArgumentTypeError('--steps requires a non-negative integer (0 = unlimited)')
    return value


def launch(config_path, mode, argv):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--steps', type=step_limit, default='0')
    args, forwarded = parser.parse_known_args(argv)
    env = os.environ.copy()

    def setting(name, default):
        return env.get('DEX_TELEOP_'+name) or env.get('BENCH2DEX_'+name) or default

    gpu = setting('GPU_INDEX', '0')
    if not re.fullmatch(r'[0-9]+', gpu):
        raise ValueError('DEX_TELEOP_GPU_INDEX must be a non-negative integer')
    env['BENCH2DEX_GPU_INDEX'] = gpu
    env['DEX_TELEOP_SERVER_CONFIG'] = str(config_path)
    env.setdefault('DEBUG_MODE', 'false')
    env['BENCH2DEX_EXECUTION_MODE'] = setting('EXECUTION_MODE', 'split')
    env['PYTHONPATH'] = str(ROOT) + (os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    subprocess.run(['nvidia-smi', '-i', gpu,
                    '--query-gpu=index,uuid,memory.used,utilization.gpu', '--format=csv'],
                   check=True, env=env)
    gpu_uuid = subprocess.check_output(['nvidia-smi', '-i', gpu, '--query-gpu=uuid',
                                       '--format=csv,noheader'], text=True, env=env).strip()
    env.update(CUDA_VISIBLE_DEVICES=gpu_uuid, OMNI_KIT_ACCEPT_EULA='YES', PYTHONUNBUFFERED='1',
               BENCH2DEX_SMOKE_FAST_EXIT='1', OMP_NUM_THREADS='8', MKL_NUM_THREADS='8', LIVESTREAM='0')
    output = Path(setting('BROWSER_OUTPUT', str(ROOT/'logs/browser-live'))).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    python = setting('PYTHON', 'python')
    command = [python, str(ROOT/'scripts/bench2dex/browser_bench2dex.py'),
               '--steps', args.steps, '--port', setting('BROWSER_PORT', '8080'),
               '--fps', setting('BROWSER_FPS', '24'), '--quality', setting('BROWSER_QUALITY', '75'),
               '--output-dir', str(output), '--teleop-config', str(config_path)]
    if mode == 'teleop':
        command += ['--dashboard', '--teleop', '--teleop-backend', 'network']
    command += [*forwarded, '--kit_args',
                f'--/renderer/activeGpu={gpu} --/renderer/multiGpu/enabled=false --/app/livestream/enabled=false']
    os.execvpe(python, command, env)
