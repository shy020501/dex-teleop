"""Small NumPy-only rigid-pose helpers shared with the Windows tracker reader."""
import numpy as np


def rotation(q):
    q = np.asarray(q, dtype=float)
    if q.shape != (4,) or not np.isfinite(q).all() or np.linalg.norm(q) < 1e-8:
        raise ValueError('Invalid wxyz quaternion')
    w, x, y, z = q/np.linalg.norm(q)
    return np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])


def quaternion(r):
    r = np.asarray(r, dtype=float)
    if r.shape != (3, 3) or not np.isfinite(r).all():
        raise ValueError('Invalid rotation matrix')
    # Symmetric eigenproblem remains stable at 180 degrees.
    xx, xy, xz, yx, yy, yz, zx, zy, zz = r.flat
    k = np.array([[xx-yy-zz, xy+yx, xz+zx, zy-yz],
                  [xy+yx, yy-xx-zz, yz+zy, xz-zx],
                  [xz+zx, yz+zy, zz-xx-yy, yx-xy],
                  [zy-yz, xz-zx, yx-xy, xx+yy+zz]])/3
    _, vectors = np.linalg.eigh(k)
    q = vectors[:, -1][[3, 0, 1, 2]]
    return q if q[0] >= 0 else -q


def tracking_matrix(value):
    value = np.asarray(value, dtype=float)
    if value.shape != (3, 4) or not np.isfinite(value).all():
        raise ValueError('Invalid tracker matrix')
    r = value[:, :3]
    if not np.allclose(r.T@r, np.eye(3), atol=.005) or abs(np.linalg.det(r)-1) > .005:
        raise ValueError('Tracker matrix is not a proper rotation')
    return value.copy()


def default_tracking_basis(yaw_deg=0.):
    # OpenVR: +X right, +Y up, -Z forward. Protocol: +X right, +Y forward, +Z up.
    angle = np.deg2rad(yaw_deg)
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c,-s,0.], [s,c,0.], [0.,0.,1.]])@np.array([[1.,0.,0.],[0.,0.,-1.],[0.,1.,0.]])
