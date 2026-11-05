"""The stride arithmetic between a captured buffer and a published frame.

`reachy_mini_ros2/video.py` is three lines of numpy guarding one failure that does not
announce itself: GStreamer pads each row of an RGB frame up to a 4-byte boundary, and
reshaping on `width * 3` instead of the real stride shears the image progressively down
the frame instead of raising anything. On the robot that is a camera topic whose pictures
are diagonally smeared, published at a healthy rate, by a node reporting no errors.

Loaded by PATH: `ros2_ws` is not on `sys.path` and putting it there would need ROS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

_MODULE = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
           / "reachy_mini_ros2" / "video.py")


def _load():
    spec = importlib.util.spec_from_file_location("reachy_mini_video_under_test", _MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


video = _load()


def _padded(frame: np.ndarray, stride: int) -> bytes:
    """The frame as GStreamer would hand it over: each row padded out to `stride`."""

    height, width, _ = frame.shape
    rows = [frame[y].tobytes().ljust(stride, b"\x00") for y in range(height)]
    return b"".join(rows)


def test_an_unpadded_buffer_round_trips():
    frame = np.arange(4 * 3 * 3, dtype=np.uint8).reshape(3, 4, 3)   # 4*3 = 12, divides by 4
    out = video.rgb_frame_from_buffer(frame.tobytes(), 4, 3)
    assert np.array_equal(out, frame)


def test_a_padded_buffer_is_read_on_its_own_stride():
    """5 pixels wide is 15 bytes a row, which GStreamer pads to 16.

    Read as 15, every row after the first is shifted one byte further left than the last
    -- the image shears, and nothing anywhere raises.
    """

    frame = np.arange(5 * 4 * 3, dtype=np.uint8).reshape(4, 5, 3)
    out = video.rgb_frame_from_buffer(_padded(frame, 16), 5, 4)
    assert np.array_equal(out, frame)

    naive = np.frombuffer(_padded(frame, 16)[: 5 * 4 * 3], dtype=np.uint8).reshape(4, 5, 3)
    assert not np.array_equal(naive, frame), "the padding has to be doing something"


def test_a_short_buffer_is_a_missed_frame_not_an_exception():
    """A truncated buffer arrives when a device is unplugged mid-frame. One miss, counted
    by the caller, is the correct outcome; a traceback out of the capture thread is not."""

    assert video.rgb_frame_from_buffer(b"\x00" * 10, 320, 240) is None


@pytest.mark.parametrize("width, height", [(0, 240), (320, 0), (-1, 240)])
def test_nonsense_dimensions_are_refused(width, height):
    assert video.rgb_frame_from_buffer(b"\x00" * 4096, width, height) is None


def test_the_result_owns_its_memory():
    """The buffer is unmapped the moment this returns, so a view into it would be a frame
    whose pixels are freed under the publisher."""

    frame = np.arange(4 * 3 * 3, dtype=np.uint8).reshape(3, 4, 3)
    out = video.rgb_frame_from_buffer(frame.tobytes(), 4, 3)
    assert out.flags["OWNDATA"]
