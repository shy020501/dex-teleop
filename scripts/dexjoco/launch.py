"""Launch DexJoCo in its own interpreter and EGL device namespace."""
import os
from pathlib import Path
import sys
import subprocess
ROOT=Path(__file__).resolve().parents[2]

def launch(config_path, mode, argv):
    env=os.environ.copy()
    python=env.get('DEX_TELEOP_DEXJOCO_PYTHON') or sys.executable
    gpu=env.get('DEX_TELEOP_GPU_INDEX','0')
    if not gpu.isdigit():raise ValueError('DEX_TELEOP_GPU_INDEX must be a non-negative integer')
    subprocess.run(['nvidia-smi','-i',gpu,'--query-gpu=index,name,memory.used','--format=csv'],check=True)
    env.update(MUJOCO_GL='egl',PYOPENGL_PLATFORM='egl',MUJOCO_EGL_DEVICE_ID=gpu,
               DEX_TELEOP_SERVER_CONFIG=str(config_path),PYTHONUNBUFFERED='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1')
    env.pop('CUDA_VISIBLE_DEVICES',None)
    env['PYTHONPATH']=str(ROOT)+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    os.execve(python,[python,'-m','dex_teleop.backends.dexjoco.runtime','--config',str(config_path),'--mode',mode,*argv],env)
