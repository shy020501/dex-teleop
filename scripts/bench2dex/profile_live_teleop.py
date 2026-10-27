#!/usr/bin/env python3
"""Observe a running split session; never send input, reset, or change settings.

Run with the bench2dex Python. Stack sampling requires a prior `sudo -v` on
machines using Yama ptrace_scope=1. Only py-spy runs through sudo; this observer
and the simulation stay under the current user. --observe-only needs no sudo.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
from urllib.request import urlopen

import psutil

ROOT = Path(__file__).resolve().parents[2]


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return dict(mean=statistics.mean(values), median=statistics.median(values),
                p95=values[min(len(values)-1, int(len(values)*.95))],
                min=values[0], max=values[-1])


def find_main(port, pid=None):
    matches = []
    for proc in psutil.process_iter(['pid', 'cmdline', 'uids']):
        if pid is not None and proc.pid != pid:
            continue
        args = proc.info['cmdline'] or []
        if not any(Path(a).name == 'browser_bench2dex.py' for a in args[:3]):
            continue
        if proc.info['uids'].real != os.getuid():
            continue
        actual_port = int(args[args.index('--port')+1]) if '--port' in args else 8080
        if actual_port == port:
            matches.append(proc)
    if len(matches) != 1:
        raise RuntimeError(f'Expected one owned browser session on port {port}; found {len(matches)}')
    return matches[0]


def read_json(url):
    with urlopen(url, timeout=1) as response:
        return json.load(response)


def thread_counters(pid):
    directory = Path(f'/proc/{pid}/task/{pid}')
    stat = (directory/'stat').read_text().rsplit(')', 1)[1].split()
    result = dict(cpu_s=(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK'))
    try:
        run, wait, slices = map(int, (directory/'schedstat').read_text().split())
        result.update(scheduled_s=run/1e9, runqueue_wait_s=wait/1e9, timeslices=slices)
    except (OSError, ValueError):
        pass
    return result


def summarize(rows):
    intervals = []
    for a, b in zip(rows, rows[1:]):
        dt = b['t']-a['t']
        sa, sb = a.get('status', {}), b.get('status', {})
        ha, hb = a.get('health', {}), b.get('health', {})
        if not sa or not sb or sa.get('epoch') != sb.get('epoch'):
            continue
        steps = sb['physics_seq']-sa['physics_seq']
        if steps < 0 or dt <= 0:
            continue
        item = dict(seconds=dt, steps=steps,
                    active=sa['active'] or sb['active'],
                    recording=a.get('recording', False) or b.get('recording', False))
        if ha.get('stream') == hb.get('stream'):
            item['images'] = max(0, hb.get('sequence', 0)-ha.get('sequence', 0))
        for role in ('main', 'renderer'):
            ca, cb = a.get('processes', {}).get(role, {}), b.get('processes', {}).get(role, {})
            if ca.get('pid') != cb.get('pid'):
                continue
            for key in ('cpu_s', 'scheduled_s', 'runqueue_wait_s'):
                if key in ca.get('main_thread', {}) and key in cb.get('main_thread', {}):
                    item[role+'_'+key] = max(0., cb['main_thread'][key]-ca['main_thread'][key])
        intervals.append(item)
    groups = {}
    for name, selected in [('all', intervals),
                           ('input_active', [r for r in intervals if r['active']]),
                           ('input_inactive', [r for r in intervals if not r['active']]),
                           ('recording', [r for r in intervals if r['recording']])]:
        duration = sum(r['seconds'] for r in selected)
        steps = sum(r['steps'] for r in selected)
        if not duration:
            continue
        group = dict(seconds=duration, physics_steps=steps,
                     physics_hz=steps/duration, control_hz_inferred=steps/duration/3,
                     published_image_hz=sum(r.get('images', 0) for r in selected)/duration)
        if steps:
            for role in ('main', 'renderer'):
                for key in ('cpu_s', 'scheduled_s', 'runqueue_wait_s'):
                    field = role+'_'+key
                    if any(field in r for r in selected):
                        group[field[:-2]+'_ms_per_physics_step'] = sum(r.get(field, 0) for r in selected)*1000/steps
        groups[name] = group
    return dict(interval_groups=groups,
                rolling_wall_hz=distribution([r.get('health', {}).get('timing', {}).get('wall_hz') for r in rows]),
                jpeg_encode_ms=distribution([r.get('health', {}).get('encode_ms') for r in rows]),
                network_period_ms=distribution([r.get('status', {}).get('network', {}).get('period_ms') for r in rows]),
                network_receive_age_ms=distribution([r.get('status', {}).get('network', {}).get('receive_age_ms') for r in rows]),
                errors=[r['errors'] for r in rows if r.get('errors')])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--seconds', type=int, default=90)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--observe-only', action='store_true')
    args = parser.parse_args()
    if not 3 <= args.seconds <= 600:
        parser.error('--seconds must be 3..600')
    process = find_main(args.port, args.pid)
    children = []
    for child in process.children(recursive=True):
        try:
            if any(Path(a).name == 'scene_worker.py' for a in child.cmdline()[:3]):
                children.append(child)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(children) != 1:
        raise RuntimeError(f'Expected one split renderer; found {len(children)}. No session changes made.')
    renderer = children[0]
    env = process.environ()
    gpu = env.get('CUDA_VISIBLE_DEVICES') or env.get('BENCH2DEX_GPU_INDEX')
    if not gpu or ',' in gpu:
        raise RuntimeError('A single selected GPU is required for scoped observation.')
    command = process.cmdline()
    status_path = ROOT/'logs/teleop/status.json'
    if '--teleop-config' in command:
        import yaml
        config = yaml.safe_load(Path(command[command.index('--teleop-config')+1]).read_text())
        configured = config.get('control', {}).get('status_path')
        if configured:
            status_path = Path(configured)
            if not status_path.is_absolute():
                status_path = ROOT/status_path
    output = args.output or ROOT/'logs/performance'/time.strftime('live-%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=False)
    roles = dict(main=process, renderer=renderer)
    for proc in roles.values():
        proc.cpu_percent(None)
    metadata = dict(started_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                    duration_s=args.seconds, main_pid=process.pid, renderer_pid=renderer.pid,
                    gpu=gpu, command=command, status_path=str(status_path),
                    settings_changed=False, control_commands_sent=False,
                    stack_sampling=not args.observe_only,
                    notes=['Stack samples estimate wall-time shares, including native waits; not CUDA kernel timings.',
                           'Main and renderer run concurrently; their times must not be added together.',
                           'Active input is read from server arm_active/finger_active flags at 1 Hz.',
                           'Control Hz is inferred from the existing stride of three physics steps.',
                           'HTTP timing is a rolling average; this observer sends no image or control requests.'])
    (output/'manifest.json').write_text(json.dumps(metadata, indent=2)+'\n')
    spy = None
    spy_log = None
    if not args.observe_only:
        binary = ROOT/'.tools/py_spy/bin/py-spy'
        if not binary.is_file():
            raise RuntimeError(f'Profiler missing: {binary}')
        spy_log = (output/'py-spy.log').open('w')
        # Only the bounded, read-only profiler runs elevated. Never sudo this script.
        spy = subprocess.Popen(['sudo', '-n', str(binary), 'record', '--pid', str(process.pid),
                                '--subprocesses', '--idle', '--nonblocking', '--threads', '--full-filenames',
                                '--rate', '99', '--duration', str(args.seconds), '--format', 'speedscope',
                                '--output', str(output/'stacks.json')], stdout=spy_log, stderr=subprocess.STDOUT)
        time.sleep(.5)
        if spy.poll() is not None:
            spy_log.close()
            raise RuntimeError((output/'py-spy.log').read_text()+'\nRun sudo -v in this terminal first, or use --observe-only.')
    print(f'MEASUREMENT START: {args.seconds}s; main={process.pid}, renderer={renderer.pid}, GPU={gpu}', flush=True)
    print(f'Output: {output}\nUse your normal keyboard/glove controls during this window.', flush=True)
    rows = []
    started = time.monotonic()
    complete = False
    try:
        with (output/'observations.jsonl').open('w') as log:
            while time.monotonic()-started <= args.seconds:
                row = dict(t=time.monotonic(), unix_s=time.time(), processes={}, errors=[])
                if not process.is_running():
                    raise RuntimeError('The observed session ended; stopping observation.')
                for endpoint, key in [('health', 'health'), ('api/state', 'dashboard'), ('keyboard', 'keyboard')]:
                    try:
                        value = read_json(f'http://127.0.0.1:{args.port}/{endpoint}')
                        if key == 'dashboard':
                            row.update(recording=value.get('recording', False), task=value.get('task'),
                                       phase=value.get('phase'), execution_mode=value.get('execution_mode'))
                        else:
                            row[key] = value
                    except Exception as exc:
                        row['errors'].append(f'{key}: {exc}')
                try:
                    status = json.loads(status_path.read_text())
                    hands = status.get('hands', {})
                    row['status'] = dict(epoch=status.get('epoch'), physics_seq=status['haptics']['seq'],
                        active=any(h.get('arm_active') or h.get('finger_active') for h in hands.values()),
                        network=status.get('network'), status_file_age_s=time.time()-status_path.stat().st_mtime,
                        hands={side: {key: hand.get(key) for key in ('arm_active', 'finger_active',
                            'current_position_xyz', 'target_position_xyz', 'ik_failures')}
                            for side, hand in hands.items()})
                except Exception as exc:
                    row['errors'].append(f'status: {exc}')
                for role, proc in roles.items():
                    try:
                        row['processes'][role] = dict(pid=proc.pid, cpu_percent=proc.cpu_percent(None),
                            rss_mib=proc.memory_info().rss/2**20, main_thread=thread_counters(proc.pid))
                    except Exception as exc:
                        row['errors'].append(f'{role}: {exc}')
                try:
                    raw = subprocess.check_output(['nvidia-smi', '-i', gpu,
                        '--query-gpu=index,utilization.gpu,utilization.memory,memory.used,power.draw',
                        '--format=csv,noheader,nounits'], text=True, timeout=2).strip()
                    row['gpu'] = dict(zip(('index', 'utilization_pct', 'memory_utilization_pct', 'memory_mib', 'power_w'),
                                         [float(v.strip()) for v in raw.split(',')]))
                except Exception as exc:
                    row['errors'].append(f'gpu: {exc}')
                rows.append(row)
                log.write(json.dumps(row)+'\n'); log.flush()
                next_tick = started+len(rows)
                time.sleep(max(0., min(started+args.seconds, next_tick)-time.monotonic()))
        complete = True
    except KeyboardInterrupt:
        print('Observer interrupted. A profiler still running has its own bounded duration.', flush=True)
    finally:
        result = dict(complete=complete, samples=len(rows), **summarize(rows))
        if spy is not None:
            try:
                result['profiler_exit_code'] = spy.wait(timeout=max(1., started+args.seconds+10-time.monotonic()))
            except subprocess.TimeoutExpired:
                result['profiler_error'] = 'Profiler did not exit within its expected duration; inspect py-spy.log.'
            spy_log.close()
        (output/'summary.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result, indent=2), flush=True)
        print(f'MEASUREMENT END: {output}', flush=True)


if __name__ == '__main__':
    main()
