"""Run Bench2Dex with a localhost JPEG viewer and optional collection dashboard."""
import argparse
import os
import runpy
import sys
from pathlib import Path
from dex_teleop.core.config import resolve_server_config, SERVER_CONFIG_ENV

parser = argparse.ArgumentParser()
parser.add_argument('--dashboard', action='store_true')
parser.add_argument('--execution-mode', choices=('split', 'serial'),
                    default=os.environ.get('DEX_TELEOP_EXECUTION_MODE', os.environ.get('BENCH2DEX_EXECUTION_MODE', 'split')))
parser.add_argument('--gc-policy', choices=('scheduled', 'automatic'),
                    default=os.environ.get('DEX_TELEOP_GC_POLICY', 'scheduled'),
                    help='Network teleop cyclic GC: scheduled safe points (default) or original automatic GC')
parser.add_argument('--task')
parser.add_argument('--teleop-config', type=Path)
parser.add_argument('--steps', type=int, default=0,
                    help='Simulation step limit (0 = unlimited; default: 0)')
parser.add_argument('--port', type=int, default=8080)
parser.add_argument('--fps', type=int, default=24)
parser.add_argument('--quality', type=int, default=75)
parser.add_argument('--timing-window', type=int,
                    default=os.environ.get('TIMING_WINDOW', os.environ.get('BENCH2DEX_TIMING_WINDOW', '120')),
                    help='Number of completed Physics loop steps in the live timing average (TIMING_WINDOW; default: 120)')
parser.add_argument('--orbit-camera', action='store_true')
parser.add_argument('--output-dir', required=True)
args, forwarded = parser.parse_known_args()
config_path = resolve_server_config(args.teleop_config)
os.environ[SERVER_CONFIG_ENV] = str(config_path)
forwarded = ['--teleop-config', str(config_path), *forwarded]
if args.gc_policy not in ('scheduled', 'automatic'):
    parser.error('DEX_TELEOP_GC_POLICY must be scheduled or automatic')
args.output_dir = str(Path(args.output_dir).resolve())
os.environ['BENCH2DEX_EXECUTION_MODE'] = args.execution_mode
os.environ['DEX_TELEOP_EXECUTION_MODE'] = args.execution_mode
os.environ['DEX_TELEOP_GC_POLICY'] = args.gc_policy
if args.dashboard and args.orbit_camera and args.execution_mode == 'split':
    parser.error('--orbit-camera requires --execution-mode serial')
if not (1 <= args.fps <= 30 and 30 <= args.quality <= 95 and 1024 <= args.port <= 65535):
    parser.error('Use fps 1..30, JPEG quality 30..95, port 1024..65535')
if args.timing_window < 1:
    parser.error('--timing-window must be a positive integer')
if args.steps < 0:
    parser.error('--steps must be a non-negative integer (0 = unlimited)')
from dex_teleop.paths import WORKSPACE as root
from .paths import SOURCE_ROOT, CONFIG_ROOT
os.chdir(SOURCE_ROOT)
sys.path.insert(0, str(root))
dashboard = None
if args.dashboard:
    from dex_teleop.backends.bench2dex.dashboard import install_dashboard
    dashboard = install_dashboard(Path(args.task).stem if args.task else None)
    task_id = args.task = dashboard.snapshot()['task']
    dashboard.restart_argv = [str(root/'scripts/bench2dex/browser_bench2dex.py'), '--dashboard', '--port', str(args.port),
                              '--fps', str(args.fps), '--quality', str(args.quality),
                              '--output-dir', args.output_dir, *forwarded]
    dashboard.restart_argv += ['--execution-mode', args.execution_mode]
    dashboard.restart_argv += ['--gc-policy', args.gc_policy]
    dashboard.restart_argv += ['--timing-window', str(args.timing_window)]
    if args.steps: dashboard.restart_argv += ['--steps', str(args.steps)]
    match = next((t for t in dashboard.tasks() if t['id'] == task_id), None)
    if not match or not match['available']:
        parser.error('Unknown task or missing task assets: ' + task_id)
    import os
    os.environ['BENCH2DEX_DASHBOARD'] = '1'
args.task = args.task or '86_short_jigsaw_puzzle'
from isaaclab.app import AppLauncher
from .process_lifecycle import ShutdownSignals, close_owned_workers
original_init = AppLauncher.__init__
shutdown_signals = None


def init_browser(launcher, namespace, **kwargs):
    global shutdown_signals
    # Rendering must remain active although the native livestream is disabled.
    namespace.video = True
    original_init(launcher, namespace, **kwargs)
    # SimulationApp installs SIGINT -> native Kit shutdown. Override only after
    # AppLauncher initialization so Ctrl+C reaches the episode/worker finally.
    shutdown_signals = ShutdownSignals()
    shutdown_signals.install()
    launcher._teleop_shutdown_signals = shutdown_signals
    from isaaclab.sim import SimulationContext
    from dex_teleop.backends.bench2dex.browser import BrowserProbe
    probe = BrowserProbe(args.output_dir, args.port, args.fps, args.quality, args.orbit_camera,
                         debug_mode=bool(dashboard and dashboard.debug_mode),
                         use_task_camera=bool(dashboard) and not args.orbit_camera,
                         timing_window=args.timing_window)
    original_render = SimulationContext.render

    def render_and_publish(sim, *a, **kw):
        result = original_render(sim, *a, **kw)
        probe.after_render(sim)
        return result

    SimulationContext.render = render_and_publish
    launcher._browser_probe = probe


AppLauncher.__init__ = init_browser
sys.path.insert(0, str(SOURCE_ROOT))
sys.argv = [str(SOURCE_ROOT/'main.py'), '--task', str(SOURCE_ROOT/'scenes'/(Path(args.task).stem+'.yaml')),
    '--headless', '--livestream', '0', '--enable_cameras', '--experience', 'isaaclab.python.rendering.kit',
    '--device', 'cuda:0', '--seed', '42', '--collect-config', str(CONFIG_ROOT/('dashboard_collect.yaml' if args.dashboard else 'smoke_collect.yaml')),
    '--generalization-config', str(CONFIG_ROOT/'smoke_generalization.yaml'), *forwarded]
if args.dashboard:
    sys.argv += ['--collect']
if args.steps:
    sys.argv += ['--episode-steps', str(args.steps)]
try:
    runpy.run_path(str(SOURCE_ROOT/'main.py'), run_name='__main__')
except KeyboardInterrupt:
    pass
finally:
    if shutdown_signals is not None:
        shutdown_signals.begin_cleanup()
    close_owned_workers()
    if shutdown_signals is not None:
        shutdown_signals.restore()

