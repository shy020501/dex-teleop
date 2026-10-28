"""Anatomical MANUS joint angles to the six coupled Inspire/Fourier channels.

This is a geometric mapping, not the original DexMimicGen data-generation
algorithm. Human bend angles are measured from consecutive phalanges, then
clamped to the real hand's coupled actuator/joint limits. Device calibration
and fidelity must be evaluated with real gloves.
"""
import numpy as np


def angle(a,b):
    a,b=np.asarray(a),np.asarray(b)
    norm=np.linalg.norm(a)*np.linalg.norm(b)
    if norm<1e-9:raise ValueError('Degenerate finger bone geometry')
    return float(np.arccos(np.clip(np.dot(a,b)/norm,-1.,1.)))


def retarget(finger,dof,lower=None,upper=None):
    if not finger.get('valid') or finger.get('representation')!='manus25_global_m_xyzw':
        raise ValueError('Expected valid canonical MANUS25 tracking')
    points=np.asarray(finger['positions'],dtype=float)
    if points.shape!=(25,3) or not np.isfinite(points).all():raise ValueError('Invalid MANUS points')
    if dof==1:
        # Genuine parallel-gripper tasks use thumb/index pinch aperture only.
        aperture=np.linalg.norm(points[4]-points[9])
        return np.array([np.clip(1.-2.*aperture/.08,-1.,1.)])
    if dof!=6:raise ValueError('Unsupported hand synergy count')
    bends=[]
    for start in (21,16,11,6):  # little, ring, middle, index, as upstream format_action
        p=points[start:start+4]
        bends.append(.5*(angle(p[1]-p[0],p[2]-p[1])+angle(p[2]-p[1],p[3]-p[2])))
    p=points[1:5]
    thumb=.5*(angle(p[1]-p[0],p[2]-p[1])+angle(p[2]-p[1],p[3]-p[2]))
    # Opposition closes the thumb toward the index ray, invariant to global
    # wrist rotation, scale and handedness; no hard-coded world-axis flips.
    spread=angle(points[2]-points[0],points[6]-points[0])
    opposition=np.clip(np.pi/2-spread,0,np.pi/2)
    value=np.r_[bends,thumb,opposition]
    return np.clip(value,lower,upper)
