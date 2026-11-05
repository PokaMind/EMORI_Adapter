"""Turning a captured video buffer into an array, and nothing else.

The robot node publishes frames from two sources: MuJoCo's offscreen renderer in the
simulator, and a V4L2 device through GStreamer on a robot (`/dev/video41`, the
v4l2loopback node EMORI's camera bridge tees for ROS). The renderer hands back an array. The
device hands back bytes with a *stride*, and that is the whole reason this file exists:
GStreamer pads every row up to a 4-byte boundary, so a buffer is `stride * height` bytes
and `stride` is only `width * 3` when the row length happens to divide by 4.

Reshaping on `width * 3` instead of the real stride does not fail, which is the problem:
it shears the image progressively down the frame and every consumer sees a picture that
is merely *wrong*. So the arithmetic lives here, pure, importable on a machine with no
ROS, no GStreamer and no camera, and tested in `tests/test_reachy_video.py`.
"""

from __future__ import annotations

from typing import Optional

import numpy as np


def rgb_frame_from_buffer(data: bytes, width: int, height: int) -> Optional[np.ndarray]:
    """A mapped RGB buffer -> `height x width x 3` uint8, or `None` if it cannot be one.

    `None` rather than an exception: a short or malformed buffer is one bad frame on a
    stream that should keep going, and the caller already treats a missing frame as a
    miss to be counted.
    """

    if width <= 0 or height <= 0:
        return None
    if len(data) < width * height * 3:
        return None
    stride = len(data) // height
    if stride < width * 3:
        return None
    rows = np.frombuffer(data[: stride * height], dtype=np.uint8).reshape(height, stride)
    return rows[:, : width * 3].reshape(height, width, 3).copy()
