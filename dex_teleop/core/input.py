"""Shared interface for calibrated hand pose sources."""
from abc import ABC, abstractmethod


class HandPoseProvider(ABC):
    @abstractmethod
    def get_pose(self, side):
        """Return world position_xyz (metres), orientation_wxyz and source."""

