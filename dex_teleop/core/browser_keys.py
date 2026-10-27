"""Latest browser focus/key state, independent of image capture and glove tracking."""
import json
import threading
import time
from urllib.request import Request, urlopen


class BrowserKeys:
    def __init__(self, timeout=.25):
        self.timeout = timeout
        self.lock = threading.Lock()
        self.owner = None
        self.seq = -1
        self.stamp = 0.
        self.keys = []
        self.focused = False

    def submit(self, data, now=None):
        now = time.monotonic() if now is None else now
        if not isinstance(data,dict):raise ValueError('Expected key state')
        session,seq,keys,focused = (data.get(k) for k in ('session','seq','keys','focused'))
        if not isinstance(session,str) or not 8<=len(session)<=64:raise ValueError('Invalid page session')
        if type(seq) is not int or not 0<=seq<2**53:raise ValueError('Invalid key sequence')
        if type(focused) is not bool or not isinstance(keys,list) or len(keys)>32:raise ValueError('Invalid focus/keys')
        if any(not isinstance(k,str) or len(k)>32 for k in keys):raise ValueError('Invalid key')
        with self.lock:
            busy=self.owner!=session and self.focused and now-self.stamp<=self.timeout
            if busy:return False
            if self.owner==session and seq<=self.seq:return False
            self.owner,self.seq,self.stamp=session,seq,now
            self.keys=sorted(set(keys)) if focused else []
            self.focused=focused
            return True

    def snapshot(self, now=None):
        now=time.monotonic() if now is None else now
        with self.lock:
            live=self.focused and now-self.stamp<=self.timeout
            return {'focused':live,'keys':list(self.keys) if live else [],'seq':self.seq,
                    'age_ms':max(0.,(now-self.stamp)*1000) if self.owner else None}


class BrowserKeyboardPoller:
    """Windows reads key state via the existing authenticated SSH HTTP tunnel."""
    def __init__(self, url='http://127.0.0.1:18080', timeout=.2):
        self.url=url.rstrip('/')+'/keyboard'
        self.state=BrowserKeys(timeout)
        self.running=threading.Event()
        self.error=None

    def start(self):
        self.running.set()
        self.thread=threading.Thread(target=self._run,name='browser-key-reader',daemon=True)
        self.thread.start()

    def _run(self):
        seq=0
        while self.running.is_set():
            started=time.monotonic()
            try:
                request=Request(self.url,headers={'Cache-Control':'no-cache'})
                with urlopen(request,timeout=.15) as response:
                    data=json.loads(response.read(4096))
                seq+=1
                self.state.submit({'session':'windows-browser-reader','seq':seq,'keys':data['keys'],
                                   'focused':data['focused']})
                self.error=None
            except Exception as exc:
                self.error=str(exc)
                seq+=1
                self.state.submit({'session':'windows-browser-reader','seq':seq,'keys':[],'focused':False})
            delay=1/60-(time.monotonic()-started)
            if delay>0:time.sleep(delay)

    def snapshot(self):return self.state.snapshot()

    def stop(self):
        self.running.clear()
        if hasattr(self,'thread'):self.thread.join(timeout=1)
