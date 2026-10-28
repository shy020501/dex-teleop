"""Official GeoRT networks/checkpoints with MANUS canonical skeleton input.

GeoRT's runtime convenience API forces CUDA. We construct its same IKModel and
load only tensor weights on CPU; no retraining, joint heuristic or SDK is used.
"""
import importlib.util
import json
import numpy as np
from .paths import SOURCE_ROOT,ASSET_ROOT

MANUS_TO_HAND21=np.array([0,1,2,3,4,6,7,8,9,11,12,13,14,16,17,18,19,21,22,23,24])

def canonical_hand(finger,side):
    if not finger.get('valid') or finger.get('representation')!='manus25_global_m_xyzw':
        raise ValueError('Expected valid canonical MANUS25 tracking')
    points=np.asarray(finger['positions'],dtype=np.float64)
    if points.shape!=(25,3) or not np.isfinite(points).all():raise ValueError('Invalid MANUS hand geometry')
    p=points[MANUS_TO_HAND21]
    z=p[9]-p[0]
    y=p[13]-p[5] if side=='left' else p[5]-p[13]
    if np.linalg.norm(z)<.005:raise ValueError('Degenerate MANUS palm')
    z/=np.linalg.norm(z);x=np.cross(y,z)
    if np.linalg.norm(x)<1e-5:raise ValueError('Degenerate MANUS palm basis')
    x/=np.linalg.norm(x);y=np.cross(z,x)
    return ((p-p[0])@np.stack([x,y,z],axis=1)).astype(np.float32)

class GeoRTRetargeter:
    def __init__(self,side,lower,upper):
        import torch
        torch.set_num_threads(1)
        self.side=side
        spec=importlib.util.spec_from_file_location('_dexjoco_geort_model',SOURCE_ROOT/'teleoperation/GeoRT/geort/model.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        folders=list((ASSET_ROOT/'geort-checkpoints').glob('allegro_'+side+'_*_dexjoco_'+side+'_default'))
        if len(folders)!=1:raise ValueError('Missing or ambiguous pinned GeoRT checkpoint')
        config=json.loads((folders[0]/'config.json').read_text())
        order=config['joint_order']
        joints=[[order.index(name) for name in item['joint']] for item in config['fingertip_link']]
        self.ids=[item['human_hand_id'] for item in config['fingertip_link']]
        self.model=module.IKModel(joints).cpu().eval()
        self.model.load_state_dict(torch.load(folders[0]/'last.pth',map_location='cpu',weights_only=True))
        self.model_lower=np.asarray(config['joint']['lower']);self.model_upper=np.asarray(config['joint']['upper'])
        self.lower=np.maximum(lower,self.model_lower);self.upper=np.minimum(upper,self.model_upper)
    def retarget(self,finger):
        import torch
        points=canonical_hand(finger,self.side)[self.ids]
        with torch.inference_mode():normalized=self.model(torch.from_numpy(points).unsqueeze(0))[0].numpy()
        result=(normalized/2+.5)*(self.model_upper-self.model_lower)+self.model_lower
        if not np.isfinite(result).all():raise ValueError('Non-finite GeoRT output')
        return np.clip(result,self.lower,self.upper)
