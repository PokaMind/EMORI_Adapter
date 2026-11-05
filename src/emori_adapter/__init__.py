"""EMORI adapter core.

Pure-Python (ROS-free) package that turns Pokamind's live, per-cycle session
analysis into:

* normalised engagement / affect / context signals (the ROS4HRI semantics), and
* Reachy Mini behaviour commands (head, antennas, body yaw, backchannel audio).

The same core is reused on the Windows side (Slice 1: direct to the Reachy Mini
daemon) and inside the ROS 2 container (Slice 2: behind ROS4HRI publishers /
subscribers). Keeping it ROS-free makes it unit-testable without a ROS install.
"""

from __future__ import annotations

__version__ = "0.1.0"

from emori_adapter.models import (
    AnalysisCycle,
    DerivedSignals,
    EngagementLevel,
    HeadPose,
    MotionIntent,
)

__all__ = [
    "AnalysisCycle",
    "DerivedSignals",
    "EngagementLevel",
    "HeadPose",
    "MotionIntent",
    "__version__",
]
