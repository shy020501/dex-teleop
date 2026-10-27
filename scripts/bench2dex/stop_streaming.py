"""Stop only the workspace's recorded live streaming process using SIGINT."""
import argparse
from pathlib import Path
import signal
import psutil

root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('mode', nargs='?', choices=['browser'], default='browser')
mode = parser.parse_args().mode
marker = root / 'logs/browser-live.pid'
if not marker.exists():
    raise SystemExit('No recorded live streaming PID. Use Ctrl+C for a foreground launcher.')
pid = int(marker.read_text().strip())
try:
    process = psutil.Process(pid)
    # Sessions started before the script reorganization may still be running.
    expected = {str(root / 'scripts/bench2dex/browser_bench2dex.py'),
                str(root / 'scripts/browser_bench2dex.py')}
    if not expected.intersection(process.cmdline()):
        raise SystemExit('PID no longer matches this workspace streaming launcher; no signal sent.')
    process.send_signal(signal.SIGINT)
    print(f'Sent SIGINT to dex-teleop browser PID {pid}.')
    process.wait(timeout=20)
    marker.unlink(missing_ok=True)
    print('Streaming stopped.')
except psutil.NoSuchProcess:
    marker.unlink(missing_ok=True)
    print('Streaming was already stopped.')
