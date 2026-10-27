"""Console-only MANUS client; keyboard input comes from the focused browser view."""
import argparse
import math
import threading
import time

from dex_teleop.core.browser_keys import BrowserKeyboardPoller
from dex_teleop.core.config import load_client_config
from dex_teleop.core.protocol import ProtocolError
from dex_teleop.core.session import session_settings
from dex_teleop.core.transport import Client
from dex_teleop.devices.keyboard import KeyboardPoseProvider
from dex_teleop.devices.mock import mock_finger


class ConsoleReporter:
    """Print state changes, plus optional 1 Hz RTT. Never run in the input sender."""
    def __init__(self, show_rtt=False):
        self.show_rtt = show_rtt
        self.previous = {}
        self.next_rtt = 0.

    def update(self, status, now=None):
        now = time.monotonic() if now is None else now
        connection = (status['connection'], status.get('reason'))
        if connection != self.previous.get('connection'):
            detail = ': ' + connection[1] if connection[1] else ''
            print('[Teleop] ' + connection[0] + detail, flush=True)
            self.previous['connection'] = connection
        for key, label in [('browser_error', 'Browser input'), ('sdk_error', 'MANUS'),
                           ('haptic_error', 'Haptics')]:
            error = status.get(key)
            if error != self.previous.get(key):
                print(f'[{label}] {error or "Recovered"}', flush=True)
                self.previous[key] = error
        if self.show_rtt and now >= self.next_rtt:
            self.next_rtt = now + 1.
            fresh = (status.get('received_at') is not None and
                     now-status['received_at'] <= 1. and status.get('rtt_ms') is not None)
            rtt = f'{status["rtt_ms"]:.1f} ms' if fresh else '--'
            state = status['connection'] if fresh or status['connection'] != 'Connected' else 'Stale'
            keyboard = 'active' if status.get('keyboard_active') and fresh else 'inactive'
            print(f'[RTT] {rtt} | {state} | browser {keyboard} | {status.get("hand", "right")}', flush=True)


def console_loop(worker, snapshot, show_rtt):
    reporter = ConsoleReporter(show_rtt)
    while worker.is_alive():
        reporter.update(snapshot())
        # Keep Ctrl+C out of Thread.join; join only during orderly shutdown below.
        time.sleep(.1)
    reporter.update(snapshot())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='Windows client settings (default: configs/client.yaml)')
    parser.add_argument('--endpoint')
    parser.add_argument('--finger-source', choices=['manus', 'mock', 'none'])
    # Accept the browser argument used by older launchers; native-window control is removed.
    parser.add_argument('--keyboard-source', choices=['browser'], default='browser', help=argparse.SUPPRESS)
    parser.add_argument('--browser-url')
    parser.add_argument('--haptics', choices=['on', 'off', 'mock'])
    parser.add_argument('--show-rtt', action='store_true', help='Print input request/reply RTT once per second')
    args = parser.parse_args(argv)
    config = load_client_config(args.config)
    args.finger_source = args.finger_source or config['devices']['finger_source']
    if args.finger_source not in ('manus', 'mock', 'none'):
        parser.error('devices.finger_source must be manus, mock or none')
    args.haptics = args.haptics or ('on' if config['haptics'].get('enabled', True) else 'off')
    endpoint = args.endpoint or config['network']['client_endpoint']
    browser_url = args.browser_url or config['network'].get('browser_url', 'http://127.0.0.1:18080')
    provider = KeyboardPoseProvider(config)
    stop = threading.Event()
    failed = threading.Event()
    lock = threading.Lock()
    status = dict(connection='Connecting', reason=None, rtt_ms=None, received_at=None)
    manus = feedback = browser_keys = worker = None

    def publish(**fields):
        with lock:
            status.update(fields)

    def snapshot():
        with lock:
            result = dict(status)
        result.update(browser_error=browser_keys.error if browser_keys else None,
                      sdk_error=manus.error if manus else None,
                      haptic_error=(feedback.error or feedback.remote_error) if feedback else None)
        return result

    def sender():
        client = None
        last = time.monotonic()
        try:
            client = Client(endpoint, config['network']['request_timeout_ms'], config['keys'])
            while not stop.is_set():
                start = time.monotonic()
                try:
                    if client.challenge is None or not client.status.get('ready'):
                        client.request()
                        if not client.status.get('ready'):
                            provider.connection_lost()
                            if feedback:
                                feedback.clear()
                            publish(connection='Waiting for simulation', reason=None,
                                    rtt_ms=None, received_at=None)
                            stop.wait(.1)
                            continue
                        provider.connection_lost()
                    settings = session_settings(client.status)
                    if provider.synchronize(client.status) and feedback:
                        feedback.clear()
                    state = browser_keys.snapshot()
                    keys, enabled = set(state['keys']), state['focused']
                    provider.update(keys, time.monotonic()-last, enabled=enabled)
                    last = time.monotonic()
                    if manus:
                        tracking = manus.get_tracking()
                    elif args.finger_source == 'mock':
                        tracking = {side: mock_finger(.4+.3*math.sin(time.monotonic()*2), side)
                                    for side in provider.states}
                    else:
                        tracking = {}
                    for hand in tracking.values():
                        if hand.get('age_ms', 0) > settings['tracking_timeout_s']*1000:
                            hand['valid'] = False
                    reply = client.request(provider.packet_hands(tracking))
                    if not reply.get('accepted'):
                        provider.connection_lost()
                    if not client.status.get('ready'):
                        provider.connection_lost()
                        if feedback:
                            feedback.clear()
                        continue
                    settings = session_settings(client.status)
                    changed = provider.synchronize(client.status, force=not reply.get('accepted'))
                    if feedback:
                        if changed or not settings['haptics']:
                            feedback.clear()
                        else:
                            feedback.accept(reply, client.last_rtt_ms)
                    publish(connection='Connected' if reply.get('accepted') else 'Input rejected',
                            reason=None if reply.get('accepted') else reply.get('reason'),
                            rtt_ms=client.last_rtt_ms, received_at=time.monotonic(),
                            keyboard_active=enabled, hand=provider.selected)
                    stop.wait(max(0., 1/settings['client_hz']-(time.monotonic()-start)))
                except TimeoutError:
                    if feedback:
                        feedback.clear()
                    provider.connection_lost()
                    publish(connection='Disconnected', reason='Check server/SSH; release movement keys before reconnecting.',
                            rtt_ms=None, received_at=None, keyboard_active=False)
                    stop.wait(.1)
                except ProtocolError as exc:
                    if feedback:
                        feedback.clear()
                    provider.connection_lost()
                    client.disconnect()
                    publish(connection='Incompatible server', reason=str(exc),
                            rtt_ms=None, received_at=None, keyboard_active=False)
                    stop.wait(.5)
        except Exception as exc:
            failed.set()
            publish(connection='Client stopped', reason=str(exc), rtt_ms=None, received_at=None)
        finally:
            if feedback:
                feedback.clear()
            if client is not None:
                # Best-effort disarm on normal exit; server timeout still handles lost connections.
                if client.challenge and provider.states:
                    provider.connection_lost()
                    try:
                        client.request(provider.packet_hands({}))
                    except Exception:
                        pass
                client.close()

    try:
        print(f'[Teleop] Console client | fingers: {args.finger_source} | haptics: {args.haptics}', flush=True)
        print(f'[Teleop] Input: {endpoint} | Browser: {browser_url}', flush=True)
        print('[Teleop] Click the browser simulation to control. Keep this PowerShell open; Ctrl+C to stop.', flush=True)
        if args.finger_source == 'manus':
            from dex_teleop.devices.manus.provider import ManusFingerProvider
            manus = ManusFingerProvider(config['devices']['tracking_timeout_s'])
            manus.start()
        if args.haptics != 'off':
            from dex_teleop.core.haptics import HapticFeedback, MockHapticSink
            from dex_teleop.devices.manus.haptics import ManusHapticSink
            sink = ManusHapticSink(manus) if manus and args.haptics == 'on' else MockHapticSink()
            # Legacy direct callers retain their previous fallback behavior.
            feedback = HapticFeedback(sink, dict(config['haptics'], device_max_power=config['haptics']['max_power']))
            feedback.start()
        browser_keys = BrowserKeyboardPoller(browser_url)
        browser_keys.start()
        worker = threading.Thread(target=sender, name='teleop-sender', daemon=True)
        worker.start()
        console_loop(worker, snapshot, args.show_rtt)
        return 1 if failed.is_set() else 0
    except KeyboardInterrupt:
        print('\n[Teleop] Stopping...', flush=True)
        return 130
    finally:
        stop.set()
        if feedback:
            feedback.clear()
        if worker is not None:
            worker.join(timeout=2)
        for label, resource in [('Browser input', browser_keys), ('Haptics', feedback), ('MANUS', manus)]:
            if resource is not None:
                try:
                    resource.stop()
                except Exception as exc:
                    print(f'[{label}] Shutdown: {exc}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
