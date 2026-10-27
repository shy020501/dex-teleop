#!/usr/bin/env python3
"""Summarize bounded runtime timers without adding nested or parallel work."""
import argparse
from collections import defaultdict, deque
import json
from pathlib import Path
import statistics


def stats(values):
    v = sorted(values)
    if not v:
        return None
    def percentile(p):
        i = (len(v)-1)*p
        lo = int(i)
        return v[lo]+(v[min(lo+1, len(v)-1)]-v[lo])*(i-lo)
    return dict(n=len(v), mean=statistics.fmean(v), median=statistics.median(v),
                p95=percentile(.95), p99=percentile(.99), min=v[0], max=v[-1])


def group(rows, kind):
    if not rows:
        return None
    periods = [r['period_ms'] for r in rows if r.get('period_ms', 0) > 0]
    wall = [r['wall_ms'] for r in rows]
    seconds = sum(periods)/1000 if periods else sum(wall)/1000
    timings = defaultdict(list)
    for row in rows:
        for name, samples in row['timings'].items():
            timings[name].extend(samples)
    result = dict(samples=len(rows), measured_seconds=seconds, wall_ms=stats(wall),
                  cpu_ms=stats([r['cpu_ms'] for r in rows]),
                  timers={name: dict(calls=len(samples),
                    inclusive_ms_per_call=stats([s[0] for s in samples]),
                    thread_cpu_ms_per_call=stats([s[1] for s in samples]),
                    inclusive_ms_per_sample=sum(s[0] for s in samples)/len(rows),
                    exclusive_ms_per_sample=sum(s[2] for s in samples)/len(rows))
                    for name, samples in sorted(timings.items())})
    if kind == 'step' and periods:
        result.update(physics_hz=len(periods)/seconds,
                      control_updates=len(timings.get('control.total', [])),
                      control_hz=len(timings.get('control.total', []))/seconds,
                      period_ms=stats(periods),
                      periods_above_16_667ms=sum(x > 1000/60 for x in periods),
                      periods_above_50ms=sum(x > 50 for x in periods),
                      periods_above_100ms=sum(x > 100 for x in periods),
                      outside_loop_gap_ms=stats([max(0., r['period_ms']-r['wall_ms'])
                                                for r in rows if 'period_ms' in r]),
                      unattributed_ms_per_step=stats([max(0., r['wall_ms']-
                          sum(s[2] for a in r['timings'].values() for s in a)) for r in rows]))
        rolling, window, duration = [], deque(), 0.
        for period in periods:
            window.append(period); duration += period
            if len(window) > 120:
                duration -= window.popleft()
            if len(window) == 120:
                rolling.append(120000/duration)
        result['rolling_120_step_hz'] = stats(rolling)
    return result


def analyze(directory):
    window = json.loads((directory/'window.json').read_text())
    by_role = defaultdict(list)
    malformed = []
    for path in sorted(directory.glob('*.jsonl')):
        role = path.name.split('-', 1)[0]
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            try:
                row = json.loads(line)
                if window['start'] <= row.get('t', 0) <= window['end']:
                    by_role[role].append(row)
            except ValueError:
                malformed.append(f'{path.name}:{line_no}')
    main_rows = sorted([r for r in by_role['main'] if r['kind'] == 'step'], key=lambda r: r['t'])
    frames = sorted([r for r in by_role['renderer'] if r['kind'] == 'frame'], key=lambda r: r['t'])
    background = {}
    for role, rows in by_role.items():
        grouped = defaultdict(list)
        for row in rows:
            if row['kind'] == 'background':
                grouped[(row['thread'], row['name'])].append(row['sample'])
        background[role] = {thread+'/'+name: dict(calls=len(samples),
            wall_ms=stats([s[0] for s in samples]), cpu_ms=stats([s[1] for s in samples]))
            for (thread, name), samples in sorted(grouped.items())}
    bins = []
    for index in range(int(window['seconds']/10)+1):
        start = window['start']+index*10
        rows = [r for r in main_rows if start <= r['t'] < start+10]
        if not rows:
            continue
        part = group(rows, 'step')
        bins.append(dict(from_s=index*10, steps=len(rows), physics_hz=part.get('physics_hz'),
            control_hz=part.get('control_hz'), period_ms=part.get('period_ms'),
            recording_fraction=sum(r['recording'] for r in rows)/len(rows),
            active_fraction=sum(bool(r['arm_active'] or r['finger_active']) for r in rows)/len(rows)))
    slow = []
    for row in sorted(main_rows, key=lambda r: r.get('period_ms', 0), reverse=True)[:20]:
        slow.append(dict(at_s=row['t']-window['start'], step=row['step'],
            period_ms=row.get('period_ms'), wall_ms=row['wall_ms'], cpu_ms=row['cpu_ms'],
            outside_loop_gap_ms=max(0., row.get('period_ms', row['wall_ms'])-row['wall_ms']),
            recording=row['recording'], arm_active=row['arm_active'], finger_active=row['finger_active'],
            exclusive_ms={name:sum(s[2] for s in samples) for name, samples in row['timings'].items()}))
    gc_events = [dict(role=role, **r) for role, rows in by_role.items() for r in rows if r['kind'] == 'gc']
    return dict(window=window, main=group(main_rows, 'step'), renderer=group(frames, 'frame'),
        main_active=group([r for r in main_rows if r['arm_active'] or r['finger_active']], 'step'),
        main_inactive=group([r for r in main_rows if not (r['arm_active'] or r['finger_active'])], 'step'),
        main_recording=group([r for r in main_rows if r['recording']], 'step'),
        main_not_recording=group([r for r in main_rows if not r['recording']], 'step'),
        background=background, ten_second_bins=bins, slowest_steps=slow, gc_events=gc_events,
        malformed_lines=malformed,
        completion=[json.loads(p.read_text()) for p in sorted(directory.glob('*-done.json'))],
        notes=['All times are wall times or calling-thread CPU times, not GPU kernel timings.',
               'Nested inclusive timings overlap. Only exclusive values within a step are additive.',
               'Renderer and background times overlap the main physics loop; never add them to its period.',
               'period_ms includes work and pacing between completed steps; individual step times can exceed 16.667ms while later steps catch up.',
               'No new synchronization or network waits were inserted. Timer and logger overhead is present.',
               'Reset/pause work outside a completed loop may appear in outside_loop_gap_ms; do not attribute that gap to physics or rendering.',
               'Active/inactive 120-step values may span gaps; use the full-session rolling metric for the UI comparison.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    report = analyze(args.directory)
    output = args.directory/'summary.json'
    output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(dict(output=str(output), seconds=report['window']['seconds'],
        physics_hz=(report['main'] or {}).get('physics_hz'),
        control_hz=(report['main'] or {}).get('control_hz'),
        rolling_120_step_hz=(report['main'] or {}).get('rolling_120_step_hz'),
        renderer_ms=(report['renderer'] or {}).get('wall_ms'),
        main_steps=(report['main'] or {}).get('samples'), render_frames=(report['renderer'] or {}).get('samples')),
        indent=2))


if __name__ == '__main__':
    main()
