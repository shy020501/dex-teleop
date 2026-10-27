"""Run AFTER the official MANUS minimal example succeeds in Local mode."""
import argparse
import json
import time
from pathlib import Path
from dex_teleop.devices.manus.provider import ManusFingerProvider


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=15)
    parser.add_argument('--output',default='manus-probe.json')
    args=parser.parse_args()
    provider=ManusFingerProvider()
    provider.start()
    seen=set()
    try:
        start=time.monotonic()
        while time.monotonic()-start<args.seconds:
            tracking=provider.get_tracking()
            for side,data in tracking.items():
                if data['valid']:
                    seen.add(side)
                    print(side,'glove',data['glove_id'],'25 selected nodes',data['node_ids'],'age_ms',round(data['age_ms'],1),flush=True)
            if provider.error: print('SDK/topology status:',provider.error,flush=True)
            time.sleep(.2)
        report=provider.diagnostics()
        report['canonical_sides_seen']=sorted(seen)
        Path(args.output).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    finally:
        provider.stop()
    if not seen: raise SystemExit('No usable MANUS skeleton received; inspect probe report and SDK license before enabling gloves')


if __name__=='__main__':main()
