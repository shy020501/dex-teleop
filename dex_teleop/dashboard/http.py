"""Localhost JPEG viewer with a separate latest-key state endpoint. One latest raw frame and JPEG, no frame FIFO."""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from dex_teleop.paths import WORKSPACE


class LatestFrame:
    def __init__(self):
        self.condition = threading.Condition()
        self.jpeg = None
        self.sequence = 0
        self.stream = uuid.uuid4().hex
        self.captured_at = 0.0
        self.last_request = 0.0
        self.metadata = {}
        self.timing = None
        self.error = None

    def publish(self, jpeg, captured_at, metadata):
        with self.condition:
            self.jpeg, self.captured_at = jpeg, captured_at
            self.sequence += 1
            self.metadata = metadata
            self.condition.notify_all()

    def publish_timing(self, snapshot):
        # Independent of JPEG metadata: a delayed encoder/render worker cannot
        # overwrite live Physics telemetry with an older frame's measurements.
        with self.condition:
            self.timing = snapshot

    def health(self):
        with self.condition:
            now = time.monotonic()
            timing = None
            if self.timing is not None:
                timing = dict(self.timing)
                stamp = timing.pop('sampled_at')
                age = max(0., now-stamp)*1000 if stamp is not None else None
                timing.update(age_ms=round(age, 1) if age is not None else None,
                              stale=age is None or age > 2000)
            return dict(self.metadata, stream=self.stream, sequence=self.sequence, ready=self.jpeg is not None,
                        frame_age_ms=round((now-self.captured_at)*1000, 1) if self.jpeg else None,
                        jpeg_bytes=len(self.jpeg) if self.jpeg else 0, error=self.error, timing=timing)


def serve_viewer(store, port):
    from dex_teleop.core.browser_keys import BrowserKeys
    keyboard=BrowserKeys()
    from dex_teleop.dashboard.state import get_dashboard
    dashboard=get_dashboard()
    hotkey_lock=threading.Lock()
    hotkey_state={"owner":None,"keys":set(),"stamp":0.}
    page = (WORKSPACE / 'browser/index.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, mime, body, extra=None):
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, str(value))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.connection.settimeout(2)
            # Custom header blocks cross-origin form/no-cors control submissions.
            origin=self.headers.get('Origin')
            path=urlsplit(self.path).path
            if (path not in ('/keyboard','/api/command','/api/delete','/api/replay','/api/replay/remove') or
                self.headers.get('X-Dex-Teleop-Control', self.headers.get('X-Bench2Dex-Control'))!='1' or
                (origin and origin != 'http://'+self.headers.get('Host',''))):
                self.reply(403,'text/plain',b'Forbidden')
                return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=4096:raise ValueError('Invalid length')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError('Expected object')
                if path=='/keyboard':
                    # F6/F7 are handled on the server, independent of the Windows SDK/input client.
                    accepted=keyboard.submit(data) if not (dashboard and dashboard.debug_mode) else False
                    if accepted and dashboard is not None:
                        with hotkey_lock:
                            held=set(data['keys']) if data['focused'] else set()
                            previous=hotkey_state['keys'] if hotkey_state['owner']==data['session'] else set()
                            hotkey_state.update(owner=data['session'],keys=held,stamp=time.monotonic())
                            for key,action in (('f6','reset'),('f7','toggle')):
                                if key in held and key not in previous:
                                    try:dashboard.submit({'action':action})
                                    except ValueError as exc:dashboard.update(error=str(exc))
                    result={'accepted':accepted}
                elif dashboard is None:
                    raise ValueError('Dashboard is not enabled')
                elif path=='/api/command':result=dashboard.submit(data)
                elif path.startswith('/api/replay'):
                    jobs = getattr(dashboard, 'replays', None)
                    if jobs is None:
                        raise ValueError('일반 시뮬레이션 모드에서 replay를 사용할 수 있습니다.')
                    if path == '/api/replay/remove':
                        result = jobs.remove(data.get('id'))
                    else:
                        with dashboard.lock:
                            if dashboard.state['phase'] != 'idle' or not dashboard.state['ready'] or not dashboard.commands.empty():
                                raise ValueError('수집·저장·환경 변경 완료 후 replay를 생성하세요.')
                            if dashboard.assets.snapshot()['busy']:
                                raise ValueError('Asset 다운로드 완료 후 replay를 생성하세요.')
                            result = jobs.request(data.get('path'))
                else:
                    if data.get('confirm') is not True:raise ValueError('삭제 확인이 필요합니다.')
                    jobs = getattr(dashboard, 'replays', None)
                    if jobs is not None and jobs.busy and jobs.job['source'] == data.get('path'):
                        raise ValueError('Replay 생성을 취소한 후 삭제하세요.')
                    result=dashboard.store.delete(data.get('path'))
                self.reply(200,'application/json',json.dumps(result).encode())
            except (ValueError,TypeError,TimeoutError,OSError) as exc:
                self.reply(400,'application/json',json.dumps({'error':str(exc)}).encode())
            except (BrokenPipeError,ConnectionResetError):pass

        def do_GET(self):
            self.connection.settimeout(5)
            url = urlsplit(self.path)
            try:
                if url.path == '/':
                    self.reply(200, 'text/html; charset=utf-8', page)
                elif url.path in ('/replay.js', '/i18n.js'):
                    self.reply(200, 'text/javascript; charset=utf-8',
                               (WORKSPACE/'browser'/url.path[1:]).read_bytes())
                elif url.path == '/api/state':
                    self.reply(200,'application/json',json.dumps(dict(dashboard.snapshot(),enabled=True) if dashboard is not None else {'enabled':False}).encode())
                elif url.path == '/api/eef':
                    poses = dashboard.eef_snapshot() if dashboard is not None else {'ready':False,'mode':'waiting','hands':{}}
                    self.reply(200,'application/json',json.dumps(poses).encode())
                elif url.path in ('/api/replay', '/api/replay/image') and dashboard is not None:
                    jobs = getattr(dashboard, 'replays', None)
                    if jobs is None:
                        raise ValueError('Replay is not available')
                    query = parse_qs(url.query)
                    job_id = query.get('id', [''])[0]
                    if url.path.endswith('/image'):
                        payload = jobs.image(job_id, query.get('side', [''])[0],
                                             query.get('camera', [''])[0], int(query.get('frame', ['-1'])[0]))
                        self.reply(200, 'image/jpeg', payload)
                    else:
                        self.reply(200, 'application/json', json.dumps(jobs.snapshot(job_id)).encode())
                elif url.path in ('/camera/left.jpg', '/camera/right.jpg') and dashboard is not None:
                    side = url.path.split('/')[-1].split('.')[0]
                    query = parse_qs(url.query)
                    frame = dashboard.previews.read(side, int(query.get('after', ['-1'])[0]),
                                                    query.get('stream', [None])[0])
                    if frame is None:
                        self.reply(204, 'image/jpeg', b'')
                    else:
                        self.reply(200, 'image/jpeg', frame['jpeg'], {
                            'X-Frame-Sequence':frame['sequence'], 'X-Frame-Stream':frame['stream'],
                            'X-Sim-Step':frame['sim_step'],
                            'X-Frame-Age-Ms':round((time.monotonic()-frame['captured_at'])*1000)})
                elif url.path == '/api/tasks' and dashboard is not None:
                    self.reply(200,'application/json',json.dumps(dashboard.tasks()).encode())
                elif url.path == '/api/episodes' and dashboard is not None:
                    folder=parse_qs(url.query,keep_blank_values=True).get('folder',[dashboard.snapshot()['folder']])[0]
                    self.reply(200,'application/json',json.dumps(dashboard.store.list(folder)).encode())
                elif url.path == '/api/tree' and dashboard is not None:
                    self.reply(200,'application/json',json.dumps(dashboard.store.inventory()).encode())
                elif url.path == '/api/storage' and dashboard is not None:
                    folder=parse_qs(url.query).get('folder',[''])[0]
                    self.reply(200,'application/json',json.dumps(dashboard.store.summary(folder)).encode())
                elif url.path == '/api/metadata' and dashboard is not None:
                    value=parse_qs(url.query).get('path',[''])[0]
                    self.reply(200,'application/json',json.dumps(dashboard.store.metadata(value)).encode())
                elif url.path == '/keyboard/config':
                    from dex_teleop.core.config import load_client_config
                    keys = dashboard.keyboard_settings() if dashboard else load_client_config()['keys']
                    self.reply(200,'application/json',json.dumps(keys).encode())
                elif url.path == '/keyboard':
                    state=keyboard.snapshot()
                    if dashboard and (dashboard.debug_mode or dashboard.state['phase'] == 'replay'):
                        state.update(keys=[], focused=False)
                    if dashboard:state['keys']=[k for k in state['keys'] if k not in ('f6','f7')]
                    self.reply(200,'application/json',json.dumps(state).encode())
                elif url.path == '/health':
                    self.reply(200, 'application/json', json.dumps(store.health()).encode())
                elif url.path == '/frame.jpg':
                    try:
                        after = int(parse_qs(url.query).get('after', ['-1'])[0])
                    except ValueError:
                        self.reply(400, 'text/plain', b'Invalid sequence')
                        return
                    client_stream = parse_qs(url.query, keep_blank_values=True).get('stream', [None])[0]
                    with store.condition:
                        store.last_request = time.monotonic()
                        changed = lambda: client_stream is not None and client_stream != store.stream
                        store.condition.wait_for(lambda: store.jpeg is not None and (changed() or store.sequence > after), timeout=1)
                        jpeg, seq, stamp = store.jpeg, store.sequence, store.captured_at
                    if jpeg is None or (not changed() and seq <= after):
                        self.reply(204, 'image/jpeg', b'')
                    else:
                        self.reply(200, 'image/jpeg', jpeg, {'X-Frame-Sequence':seq, 'X-Frame-Stream':store.stream,
                                   'X-Frame-Age-Ms':round((time.monotonic()-stamp)*1000, 1)})
                else:
                    self.reply(404, 'text/plain', b'Not found')
            except (ValueError,OSError,TypeError) as exc:
                try:self.reply(400,'application/json',json.dumps({'error':str(exc)}).encode())
                except (BrokenPipeError,ConnectionResetError):pass
            except TimeoutError:pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='browser-http', daemon=True).start()
    return server

