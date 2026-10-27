"""MANUS SDK output adapter."""
from dex_teleop.core.haptics import ZERO


class ManusHapticSink:
    """Route only fresh, unambiguous left/right glove IDs; zero displaced IDs."""
    def __init__(self,provider):self.provider=provider;self.known=set()
    def apply(self,powers):
        tracking=self.provider.get_tracking()
        current={}
        for side,data in tracking.items():
            if data['valid']:current[int(data['glove_id'])]=powers[side]
        errors=[]
        failed=set()
        for glove in self.known|set(current):
            try:self.provider.set_haptics(glove,current.get(glove,ZERO))
            except Exception as exc:
                errors.append(str(exc));failed.add(glove)
        self.known=set(current)|failed
        if errors:raise RuntimeError('; '.join(errors))
    def stop(self):
        errors=[]
        for glove in self.known:
            try:self.provider.set_haptics(glove,ZERO)
            except Exception as exc:errors.append(str(exc))
        if errors:raise RuntimeError('; '.join(errors))
