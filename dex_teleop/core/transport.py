"""One outstanding REQ/REP and one latest input slot. Simulation never waits on network."""
import copy
import json
import secrets
import threading
import time
import uuid
import zmq
from dex_teleop.core.protocol import VERSION, MAX_BYTES, COORDINATE_FRAME, ProtocolError, decode, encode
from dex_teleop.core.config import validate_keys


class Receiver:
    def __init__(self, config):
        self.config = config
        if not config['bind'].startswith('tcp://127.0.0.1:'):
            raise ValueError('Bind only to localhost; use an authenticated SSH tunnel')
        self.lock = threading.Lock()
        self.latest = None
        self.status = {}
        self.server_id = uuid.uuid4().hex
        self.client_keys = None
        self.haptics = {'ready':False}
        self.haptic_stamp = 0.
        self.owner = None
        self.last_receive = 0.
        self.last_seq = -1
        self.challenges = {}
        self.accepted = self.rejected = 0
        self.period_ms = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='teleop-receiver', daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            raise RuntimeError(f'Receiver failed: {self.error}')

    def stop(self):
        self.stop_event.set()
        if hasattr(self, 'thread'):
            self.thread.join(timeout=2)

    def set_status(self, status):
        with self.lock:
            self.status = copy.deepcopy(status)
            self.status['server_id'] = self.server_id

    def keyboard_settings(self):
        with self.lock:
            return copy.deepcopy(self.client_keys)

    def set_haptics(self,data):
        with self.lock:
            self.haptics=copy.deepcopy(data)
            self.haptic_stamp=time.monotonic()

    def snapshot(self):
        with self.lock:
            if self.latest is None or time.monotonic()-self.last_receive > self.config['timeout_s']:
                return None
            return self.latest

    def stats(self):
        with self.lock:
            return {'accepted':self.accepted, 'rejected':self.rejected, 'period_ms':self.period_ms,
                    'receive_age_ms':(time.monotonic()-self.last_receive)*1000 if self.last_receive else None,
                    'session':self.owner, 'seq':self.last_seq}

    def handle(self, raw, now=None):
        now = time.monotonic() if now is None else now
        try:
            msg = decode(raw)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            return {'version':VERSION, 'accepted':False, 'reason':f'invalid: {exc}'}
        session = msg['session']
        with self.lock:
            reason, accepted = 'hello', False
            busy = self.owner is not None and session != self.owner and now-self.last_receive <= self.config['timeout_s']
            if busy:
                reason = 'another_client_active'
            elif msg['kind'] == 'hello' and 'keys' in msg:
                self.client_keys = validate_keys(msg['keys'])
            elif msg['kind'] == 'input':
                challenge = self.challenges.get(session)
                if not challenge or msg.get('challenge') != challenge[0] or now-challenge[1] > self.config['challenge_ttl_s']:
                    reason = 'expired_challenge'
                elif msg.get('epoch') != self.status.get('epoch'):
                    reason = 'calibration_changed'
                elif session == self.owner and msg['seq'] <= self.last_seq:
                    reason = 'old_sequence'
                else:
                    if self.owner == session and self.last_receive:
                        dt = (now-self.last_receive)*1000
                        self.period_ms = dt if self.period_ms is None else .9*self.period_ms+.1*dt
                    self.owner, self.last_seq, self.last_receive = session, msg['seq'], now
                    self.latest = (msg, now)
                    self.accepted += 1
                    accepted, reason = True, 'ok'
                if not accepted:
                    self.rejected += 1
            nonce = secrets.token_hex(12)
            if not busy:
                self.challenges[session] = (nonce, now)
                # Bounded handshake state; stale sessions cannot accumulate indefinitely.
                self.challenges = {s:c for s,c in self.challenges.items() if now-c[1] <= self.config['challenge_ttl_s']}
                if len(self.challenges) > 64:
                    self.challenges = dict(sorted(self.challenges.items(), key=lambda item:item[1][1])[-64:])
            haptics=copy.deepcopy(self.haptics)
            haptics['age_ms']=max(0.,(now-self.haptic_stamp)*1000) if self.haptic_stamp else None
            return {'version':VERSION, 'haptics':haptics, 'accepted':accepted, 'reason':reason, 'challenge':nonce,
                    'server_monotonic_ns':int(now*1e9), 'echo_seq':msg['seq'], 'status':copy.deepcopy(self.status)}

    def _run(self):
        context = zmq.Context()
        socket = context.socket(zmq.REP)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVHWM, 1)
        socket.setsockopt(zmq.SNDHWM, 1)
        socket.setsockopt(zmq.MAXMSGSIZE, MAX_BYTES)
        try:
            socket.bind(self.config['bind'])
            self.ready.set()
            while not self.stop_event.is_set():
                if socket.poll(20, zmq.POLLIN):
                    socket.send(encode(self.handle(socket.recv())))
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
        finally:
            socket.close()
            context.term()


class Client:
    def __init__(self, endpoint, timeout_ms=150, keys=None):
        self.endpoint, self.timeout_ms = endpoint, timeout_ms
        self.context = zmq.Context()
        self.socket = None
        self.session = uuid.uuid4().hex
        self.seq = 0
        self.challenge = None
        self.last_rtt_ms = None
        self.status = {}
        self.keys = validate_keys(keys) if keys is not None else None

    def disconnect(self):
        if self.socket is not None:
            self.socket.close()
        self.socket, self.challenge, self.status = None, None, {}

    def _open(self):
        self.socket = self.context.socket(zmq.REQ)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.SNDHWM, 1)
        self.socket.setsockopt(zmq.RCVHWM, 1)
        self.socket.setsockopt(zmq.IMMEDIATE, 1)
        self.socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self.socket.connect(self.endpoint)

    def request(self, hands=None):
        if self.socket is None:
            self._open()
        self.seq += 1
        msg = {'version':VERSION, 'session':self.session, 'seq':self.seq, 'timestamp_unix_ns':time.time_ns(),
               'kind':'input' if hands is not None and self.challenge else 'hello'}
        if msg['kind'] == 'input':
            msg.update(challenge=self.challenge, hands=hands, epoch=self.status.get('epoch',0), coordinate_frame=COORDINATE_FRAME)
        elif self.keys is not None:
            msg['keys'] = self.keys
        start = time.monotonic()
        try:
            self.socket.send(encode(msg))
            if not self.socket.poll(self.timeout_ms, zmq.POLLIN):
                raise TimeoutError('Teleoperation reply timed out')
            # Task replacement can disconnect/reconnect between poll and recv.
            # Never enter an unbounded recv after a readiness notification: if
            # no complete reply remains, EAGAIN follows the reconnect path below.
            reply = json.loads(self.socket.recv(flags=zmq.NOBLOCK))
            if not isinstance(reply, dict) or reply.get('version') != VERSION:
                raise ProtocolError('Unsupported server protocol version; update the server and Windows client together')
            if not isinstance(reply.get('status', {}), dict):
                raise ProtocolError('Invalid server status')
            self.last_rtt_ms = (time.monotonic()-start)*1000
            self.challenge = reply.get('challenge')
            self.status = reply.get('status', {})
            return reply
        except (zmq.ZMQError, TimeoutError):
            self.disconnect()
            raise TimeoutError('Teleoperation connection unavailable; inputs must be re-armed')
        except ProtocolError:
            self.disconnect()
            raise
        except json.JSONDecodeError as exc:
            self.disconnect()
            raise ProtocolError('Invalid server JSON reply') from exc

    def close(self):
        if self.socket is not None:
            self.socket.close()
        self.context.term()
