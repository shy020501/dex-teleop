"""Restore the startup physical scene without rebuilding USD assets or stopping Kit."""


class SceneResetSnapshot:
    def __init__(self,objects,hold_targets):
        self.identities={name:id(obj) for name,obj in objects.items()}
        self.states={}
        for name,obj in objects.items():
            data=getattr(obj,'data',None)
            if data is None or not hasattr(data,'root_state_w'):continue
            root=data.root_state_w.clone()
            root[:,7:]=0.
            joint=data.joint_pos.clone() if hasattr(data,'joint_pos') else None
            hold=hold_targets.get(id(obj))
            self.states[name]=(obj,root,joint,hold.clone() if hold is not None else None)

    def matches(self,objects):
        return self.identities=={name:id(obj) for name,obj in objects.items()}

    def restore(self,hold_targets):
        for obj,root,joint,hold in self.states.values():
            obj.reset()
            obj.write_root_state_to_sim(root.clone())
            if joint is not None:
                velocity=joint.clone().zero_()
                obj.write_joint_state_to_sim(joint.clone(),velocity)
                obj.set_joint_velocity_target(velocity)
                target=hold.clone() if hold is not None else joint.clone()
                obj.set_joint_position_target(target)
                if hold is not None:hold_targets[id(obj)]=target
            obj.write_data_to_sim()
        return {'objects_reset':list(self.states),'object_count':len(self.states)}

    def audit(self):
        pose_error=joint_error=velocity=0.
        for obj,root,joint,_ in self.states.values():
            state=obj.data.root_state_w
            pose_error=max(pose_error,float((state[:,:7]-root[:,:7]).abs().max().item()))
            velocity=max(velocity,float(state[:,7:].abs().max().item()))
            if joint is not None:
                joint_error=max(joint_error,float((obj.data.joint_pos-joint).abs().max().item()))
                velocity=max(velocity,float(obj.data.joint_vel.abs().max().item()))
        return dict(max_root_state_error=pose_error,max_joint_error_rad=joint_error,max_velocity=velocity)
