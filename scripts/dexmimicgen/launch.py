"""Start the independent native MuJoCo runtime; no Isaac imports or environment changes."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def launch(config_path, mode, argv):
    env = os.environ.copy()
    python = env.get('DEX_TELEOP_DEXMIMICGEN_PYTHON') or sys.executable
    if not Path(python).is_file():
        raise ValueError('DEXMIMICGEN Python executable missing. Activate dexmimicgen or check DEX_TELEOP_DEXMIMICGEN_PYTHON.')
    gpu = env.get('DEX_TELEOP_GPU_INDEX', '0')
    if not gpu.isdigit():
        raise ValueError('DEX_TELEOP_GPU_INDEX must be a non-negative integer')
    subprocess.run(['nvidia-smi', '-i', gpu, '--query-gpu=index,uuid,memory.used,utilization.gpu',
                    '--format=csv'], check=True)
    env.update(MUJOCO_GL='egl', PYOPENGL_PLATFORM='egl', MUJOCO_EGL_DEVICE_ID=gpu,
               DEX_TELEOP_SERVER_CONFIG=str(config_path), PYTHONUNBUFFERED='1',
               OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
    # Native MuJoCo physics and retargeting run on CPU; EGL selects rendering.
    # An inherited CUDA_VISIBLE_DEVICES must not redefine the EGL device index.
    env.pop('CUDA_VISIBLE_DEVICES', None)
    env['PYTHONPATH'] = str(ROOT)+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    os.execve(python, [python, '-m', 'dex_teleop.backends.dexmimicgen.runtime',
                       '--config', str(config_path), '--mode', mode, *argv], env)
