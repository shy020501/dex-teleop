"""Start a fresh Isaac process for a private rendering/replay job."""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--config', required=True)
parser.add_argument('--fd', type=int)
parser.add_argument('--replay', action='store_true')
args = parser.parse_args()
config_path = Path(args.config).resolve()
from dex_teleop.paths import WORKSPACE as root
from .paths import SOURCE_ROOT, CONFIG_ROOT
os.chdir(SOURCE_ROOT)
sys.path[:0] = [str(root), str(SOURCE_ROOT)]
config = json.loads(config_path.read_text())
launch = config['launch']
from .paths import resolve_task_path
task_path = resolve_task_path(launch['task_path'])
os.environ.update(BENCH2DEX_SCENE_WORKER=str(config_path), BENCH2DEX_DASHBOARD='0',
                  BENCH2DEX_HAPTICS='0', BENCH2DEX_EXECUTION_MODE='serial', DEBUG_MODE='false')
if args.fd is not None:
    os.environ['BENCH2DEX_WORKER_FD'] = str(args.fd)
if args.replay:
    os.environ['BENCH2DEX_REPLAY_JOB'] = '1'
# The recorded resolved config uses dataclass names; the YAML loader expects id.
from dex_teleop.backends.bench2dex.scene_state import worker_collect_config
collect = worker_collect_config(launch, config_path.parent/'unused')
import yaml
collect_path = config_path.parent/'collect.yaml'
collect_path.write_text(yaml.safe_dump(collect))
gpu = os.environ.get('BENCH2DEX_GPU_INDEX', '0')
sys.argv = [str(SOURCE_ROOT/'main.py'), '--task', str(task_path), '--collect',
    '--collect-config', str(collect_path), '--seed', str(launch.get('base_seed') or 42),
    '--headless', '--enable_cameras', '--livestream', '0', '--device', 'cuda:0',
    '--experience', 'isaaclab.python.rendering.kit', '--kit_args',
    f'--/renderer/activeGpu={gpu} --/renderer/multiGpu/enabled=false --/app/livestream/enabled=false']
runpy.run_path(str(SOURCE_ROOT/'main.py'), run_name='__main__')

