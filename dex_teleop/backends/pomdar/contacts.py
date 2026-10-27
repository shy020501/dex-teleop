"""MuJoCo external contact forces mapped to ORCA human-finger channels."""
import numpy as np

from dex_teleop.core.haptics import FINGERS


class FingerContacts:
    def __init__(self, model, data, *, root_body='root', force_function=None):
        self.model, self.data = model, data
        self.force_function = force_function
        self.root_id = model.body(root_body).id
        self.robot_bodies = set()
        self.finger_by_geom = {}
        for body_id in range(model.nbody):
            chain, current = [], body_id
            while current:
                chain.append(current)
                if current == self.root_id:
                    self.robot_bodies.add(body_id)
                    break
                current = int(model.body_parentid[current])
            if body_id not in self.robot_bodies:
                continue
            finger = next((index for index, name in enumerate(FINGERS)
                           if any((model.body(part).name or '').startswith(name+'_') for part in chain)), None)
            if finger is not None:
                for geom_id in np.flatnonzero(model.geom_bodyid == body_id):
                    self.finger_by_geom[int(geom_id)] = finger
        if not self.finger_by_geom:
            raise ValueError('ORCA finger collision geometries were not found')
        self.robot_geoms = {int(i) for i, body in enumerate(model.geom_bodyid)
                            if int(body) in self.robot_bodies}

    def read(self, dt=None):
        force_function = self.force_function
        if force_function is None:
            from mujoco import mj_contactForce
            force_function = mj_contactForce
        forces = dict(right=[0.]*5, left=[0.]*5)
        wrench = np.zeros(6)
        for index in range(self.data.ncon):
            contact = self.data.contact[index]
            pair = (int(contact.geom1), int(contact.geom2))
            # Robot self-contact must not masquerade as object grip force.
            if all(geom in self.robot_geoms for geom in pair):
                continue
            finger = next((self.finger_by_geom[geom] for geom in pair if geom in self.finger_by_geom), None)
            if finger is None:
                continue
            wrench.fill(0.)
            force_function(self.model, self.data, index, wrench)
            # Local-frame norm is frame-invariant; torque is deliberately ignored.
            magnitude = float(np.linalg.norm(wrench[:3]))
            if not np.isfinite(magnitude):
                raise ValueError('Non-finite MuJoCo contact force')
            forces['right'][finger] += magnitude
        return forces
