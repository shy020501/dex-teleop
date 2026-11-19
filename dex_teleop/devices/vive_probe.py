"""Inspect real SteamVR tracker poses on Windows without Linux or a simulator."""
import argparse
import json
import time

from .steamvr import SteamVRTrackerReader
from .pose_math import quaternion


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=15.)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 300:
        parser.error('--seconds must be within 1..300')
    reader = SteamVRTrackerReader()
    valid_seen = False
    print('Keep SteamVR running. Move each tracker separately. No simulation or MANUS SDK required.', flush=True)
    reader.start()
    try:
        deadline = time.monotonic()+args.seconds
        while time.monotonic() < deadline:
            state = reader.snapshot()
            if state['error']:
                print(state['error'], flush=True)
            elif not state['devices']:
                print('No GenericTracker devices found (the null HMD is ignored).', flush=True)
            for serial, device in state['devices'].items():
                record = dict(serial=serial, model=device['model'], valid=device['valid'],
                              tracking_result=device['tracking_result'])
                if device['valid']:
                    import numpy as np
                    matrix = np.asarray(device['matrix'])
                    record.update(xyz_m=matrix[:,3].round(4).tolist(),
                                  orientation_wxyz=quaternion(matrix[:,:3]).round(4).tolist())
                    valid_seen = True
                print(json.dumps(record, ensure_ascii=True), flush=True)
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
    print('Valid 6DoF observed.' if valid_seen else 'No valid 6DoF observed; device recognition alone is insufficient.')
    return 0 if valid_seen else 2


if __name__ == '__main__':
    raise SystemExit(main())
