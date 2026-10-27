"""Interface placeholder; tracker reception and calibration are not implemented."""
from dex_teleop.core.input import HandPoseProvider


class VivePoseProvider(HandPoseProvider):
    """Extension point: supply calibrated world poses with the same interface."""
    def get_pose(self, side):
        raise NotImplementedError('Vive connection/calibration is outside this implementation')

