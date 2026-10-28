"""External Allegro contact forces; the absent human pinky channel stays zero."""
import numpy as np

class FingerContacts:
    def __init__(self,scene):
        self.model,self.data=scene.model,scene.data
        roots=[]
        for side in scene.sides:
            suffix='_'+side if len(scene.sides)==2 else ''
            body=int(self.model.jnt_bodyid[self.model.joint('joint1'+suffix).id])
            while self.model.body_parentid[body]!=0:body=int(self.model.body_parentid[body])
            roots.append(body)
        self.robot_geoms=set();self.fingers={}
        for geom,body in enumerate(self.model.geom_bodyid):
            chain=[];cursor=int(body)
            while cursor:chain.append(cursor);cursor=int(self.model.body_parentid[cursor])
            if not any(root in chain for root in roots):continue
            self.robot_geoms.add(geom)
            for link in chain:
                name=self.model.body(link).name or ''
                prefix=next((p for p in ('th','ff','mf','rf') if name.startswith(p+'_')),None)
                if prefix:
                    side='left' if name.endswith('_left') else 'right'
                    self.fingers[geom]=(side,{'th':0,'ff':1,'mf':2,'rf':3}[prefix]);break
        if not self.fingers:raise ValueError('No Allegro contact geometry found')
    def read(self):
        import mujoco
        forces=dict(right=[0.]*5,left=[0.]*5);wrench=np.zeros(6)
        for index in range(self.data.ncon):
            c=self.data.contact[index];pair=(int(c.geom1),int(c.geom2))
            if all(g in self.robot_geoms for g in pair):continue
            mapped=next((self.fingers[g] for g in pair if g in self.fingers),None)
            if mapped is None:continue
            mujoco.mj_contactForce(self.model,self.data,index,wrench)
            magnitude=float(np.linalg.norm(wrench[:3]))
            if not np.isfinite(magnitude):raise ValueError('Non-finite Allegro contact force')
            forces[mapped[0]][mapped[1]]+=magnitude
        return forces
