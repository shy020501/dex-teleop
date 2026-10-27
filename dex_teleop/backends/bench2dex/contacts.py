"""PhysX normal-contact forces from all 50 Wuji finger segments/tips, no SDK imports."""
from dex_teleop.core.haptics import finger_link, aggregate_forces

ROBOT='/World/Objects/GlobalRobot'


def prepare_hand_contact_reports(config_path=None):
    from dex_teleop.core.protocol import load_config
    if not load_config(config_path).get('haptics',{}).get('enabled',False):return []
    import omni.usd
    from pxr import Usd,UsdPhysics,PhysxSchema
    stage=omni.usd.get_context().get_stage()
    paths=[]
    for prim in Usd.PrimRange(stage.GetPrimAtPath(ROBOT)):
        if finger_link(prim.GetName()) and prim.HasAPI(UsdPhysics.RigidBodyAPI):
            PhysxSchema.PhysxContactReportAPI.Apply(prim).CreateThresholdAttr(0.)
            paths.append(str(prim.GetPath()))
    if len(paths)!=50:raise RuntimeError(f'Haptics expected 50 Wuji rigid finger links, found {len(paths)}')
    print('[HAPTICS] Enabled contact reports for 50 Wuji finger bodies.',flush=True)
    return paths


class FingerContacts:
    def __init__(self):
        from isaacsim.core.simulation_manager import SimulationManager
        self.view=SimulationManager.get_physics_sim_view().create_rigid_contact_view(ROBOT+'/*_finger*_link*')
        self.names=list(self.view.sensor_paths)
        if len(self.names)!=50 or any(finger_link(n) is None for n in self.names):
            raise RuntimeError(f'Unexpected haptic contact body mapping: {self.names}')
        print('[HAPTICS] PhysX contact view ready:',len(self.names),'bodies',flush=True)

    def read(self,dt):
        # PhysX converts impulses to Newtons using the actual physics timestep.
        forces=self.view.get_net_contact_forces(dt=dt).cpu().tolist()
        return aggregate_forces(self.names,forces)
