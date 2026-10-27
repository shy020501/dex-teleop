"""Contact-force encoding and a clock-independent Windows vibration watchdog."""
import copy
import math
import re
import threading
import time

FINGERS=('thumb','index','middle','ring','pinky')
LINK=re.compile(r'^(right|left)_finger([1-5])_(?:link[1-4]|tip_link)$')
ZERO=[0.]*5


def validate_power(value):
    if isinstance(value, bool):
        raise ValueError('Vibration power must be between 0.0 and 1.0')
    try:
        power = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Vibration power must be between 0.0 and 1.0') from None
    if not math.isfinite(power) or not 0 <= power <= 1:
        raise ValueError('Vibration power must be between 0.0 and 1.0')
    return power


def finger_link(name):
    match=LINK.fullmatch(name.rsplit('/',1)[-1])
    return (match[1],int(match[2])-1) if match else None


def aggregate_forces(names,forces):
    result={s:[0.]*5 for s in ('right','left')}
    if len(names)!=len(forces):raise ValueError('Contact body count mismatch')
    for name,force in zip(names,forces):
        key=finger_link(name)
        if not key:raise ValueError('Unknown contact body: '+name)
        if len(force)!=3 or not all(math.isfinite(float(x)) for x in force):
            raise ValueError('Invalid contact force')
        side,index=key
        result[side][index]+=math.sqrt(sum(float(x)**2 for x in force))
    return result


def scale_powers(powers, gain, ceiling):
    """Runtime UI gain can attenuate contact feedback but cannot raise its configured limit."""
    if not math.isfinite(gain) or not 0 <= gain <= 1:
        raise ValueError('Invalid haptic gain')
    return {side: [min(ceiling, max(0., value * gain)) for value in values]
            for side, values in powers.items()}


class ContactHapticEncoder:
    def __init__(self,config):
        self.cfg=dict(config)
        self.reset()
        if not 0<=config['max_power']<=1:raise ValueError('Invalid haptic power limit')
        if not 0<=config['force_off_n']<=config['force_on_n']<config['full_force_n']:
            raise ValueError('Invalid haptic force thresholds')
        self.hysteresis_n=config['force_on_n']-config['force_off_n']

    def set_force_range(self, minimum, maximum):
        """Server-side mapping range; changing it clears the previous force filter."""
        if (isinstance(minimum, bool) or isinstance(maximum, bool) or
                not math.isfinite(minimum) or not math.isfinite(maximum) or
                not 0 <= minimum < maximum):
            raise ValueError('Invalid haptic force range')
        if (minimum, maximum) == (self.cfg['force_on_n'], self.cfg['full_force_n']):
            return
        self.cfg.update(force_on_n=float(minimum), full_force_n=float(maximum),
                        force_off_n=max(0., minimum-self.hysteresis_n))
        self.reset()

    def set_max_power(self, maximum):
        maximum = validate_power(maximum)
        if maximum == self.cfg['max_power']:
            return
        self.cfg['max_power'] = maximum
        # Lowering the ceiling takes effect immediately, including pulse tails.
        # Raising it still follows the existing attack filter on the next update.
        if maximum == 0:
            self.reset()
        else:
            for side in self.power:
                self.power[side] = [min(maximum, value) for value in self.power[side]]

    def reset(self):
        self.power={s:[0.]*5 for s in ('right','left')}
        self.until={s:[0.]*5 for s in ('right','left')}

    def update(self,forces,active,dt,now=None):
        now=time.monotonic() if now is None else now
        c=self.cfg
        alpha=1-math.exp(-max(0.,min(dt,.1))/max(c['attack_tau_s'],1e-6))
        for side in self.power:
            for i,force in enumerate(forces[side]):
                if not math.isfinite(force) or force<0:raise ValueError('Invalid force magnitude')
                if not active.get(side,False):
                    self.power[side][i]=0.
                    self.until[side][i]=0.
                    continue
                threshold=c['force_off_n'] if self.power[side][i]>0 else c['force_on_n']
                if force>=threshold:
                    # Zero at the lower handle, full configured output at the upper.
                    # Hysteresis and the short pulse tail affect transitions only.
                    desired=c['max_power']*min(1.,max(0.,(force-c['force_on_n'])/(c['full_force_n']-c['force_on_n'])))
                    self.power[side][i]+=alpha*(desired-self.power[side][i])
                    if desired>0:
                        self.until[side][i]=now+c['minimum_pulse_s']
                    elif now>=self.until[side][i]:
                        self.power[side][i]=0.
                elif now>=self.until[side][i]:
                    self.power[side][i]=0.
        return copy.deepcopy(self.power)


class MockHapticSink:
    """No hardware output; useful for end-to-end protocol tests."""
    def __init__(self):self.last={s:list(ZERO) for s in ('right','left')};self.history=[]
    def apply(self,powers):
        self.last=copy.deepcopy(powers)
        self.history.append(copy.deepcopy(powers))
        self.history=self.history[-500:]
    def stop(self):self.apply({s:list(ZERO) for s in ('right','left')})


class HapticFeedback:
    """Separate thread keeps zeroing hardware even if network requests stall."""
    def __init__(self,sink,config):
        self.sink,self.cfg=sink,config
        self.lock=threading.Lock()
        self.packet=None
        self.stamp=0.
        self.seen=None
        self.last_seq=-1
        self.error=None
        self.remote_error=None
        self.last={s:list(ZERO) for s in ('right','left')}
        self.running=threading.Event()

    def accept(self,reply,rtt_ms=0.,now=None):
        now=time.monotonic() if now is None else now
        try:
            data=reply['haptics']
            if not isinstance(data,dict):raise ValueError('Invalid feedback object')
            self.remote_error=data.get('error')
            if not reply.get('accepted') or not data['ready']:raise ValueError('No accepted contact feedback')
            stream=(data['stream_id'],data['epoch'])
            seq=data['seq']
            if not isinstance(stream[0],str) or not stream[0] or type(seq) is not int or seq<0:raise ValueError('Invalid feedback identity')
            age=float(data['age_ms'])/1000+max(0.,float(rtt_ms))/1000
            if not math.isfinite(age) or not 0<=age<self.cfg['feedback_timeout_s']:raise ValueError('Stale contact feedback')
            # New servers send the UI-selected ceiling with this same sample.
            # Older servers keep the local default (0.35) as their fallback.
            ceiling=validate_power(data.get('max_power', self.cfg['max_power']))
            ceiling=min(ceiling, validate_power(self.cfg.get('device_max_power', 1.)))
            powers={}
            for side in ('right','left'):
                values=data['powers'][side]
                if len(values)!=5 or not all(math.isfinite(float(v)) and 0<=float(v)<=1 for v in values):raise ValueError('Invalid vibration powers')
                powers[side]=[min(ceiling,float(v)) for v in values]
            with self.lock:
                if stream==self.seen and seq<=self.last_seq:return False
                self.seen,self.last_seq=stream,seq
                self.packet,self.stamp=powers,now-age
            return True
        except (KeyError,TypeError,ValueError,OverflowError):
            self.clear()
            return False

    def clear(self):
        with self.lock:self.packet=None;self.stamp=0.

    def output(self,now=None):
        now=time.monotonic() if now is None else now
        with self.lock:
            if self.packet is None or now-self.stamp>=self.cfg['feedback_timeout_s']:
                return {s:list(ZERO) for s in ('right','left')}
            return copy.deepcopy(self.packet)

    def start(self):
        self.running.set()
        self.thread=threading.Thread(target=self._run,name='haptic-watchdog',daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while self.running.is_set():
                powers=self.output()
                try:
                    self.sink.apply(powers)
                    self.last=powers
                    self.error=None
                except Exception as exc:
                    self.error=str(exc)
                    self.clear()
                    try:self.sink.stop()
                    except Exception:pass
                time.sleep(1/self.cfg['output_hz'])
        finally:
            try:self.sink.stop()
            except Exception as exc:self.error=str(exc)

    def stop(self):
        self.clear()
        self.running.clear()
        if hasattr(self,'thread'):self.thread.join(timeout=2)
        self.sink.stop()

