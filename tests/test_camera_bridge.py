"""`deploy/robot/camera-bridge.sh`: the kiosk pipeline, and the optional ROS tee.

Two layers, deliberately, because the two questions are different.

Layer 1 is a pure-Python read of the script's text and RUNS EVERYWHERE, including the
Windows dev box where this repo is developed. It is what guarantees that the properties
the design hangs on -- both queues leaky, `add-borders=false`, the kiosk sink untouched --
cannot be deleted by an edit that nobody executes.

Layer 2 actually RUNS the script under bash with `EMORI_CAM_PRINT_PIPELINE=1` and a stub
`gst-inspect`, which is the only way to prove the three-way branch (no tee / configured
but absent / configured and present) really produces those strings. It is skipped unless
`EMORI_TEST_SHELL=1` and a non-WSL bash exists, and the skip REASON says so out loud: a
shell test that silently vanishes on the machine the shell script is edited from is worse
than no shell test, because it reads as a pass.

The one property with no room for interpretation: with `EMORI_CAM_ROS_VIDEO_NR` unset the
pipeline must be CHARACTER-FOR-CHARACTER the kiosk-only pipeline every v1 robot runs, and
this feature is not allowed to touch them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "robot" / "camera-bridge.sh"

# The pipeline as it stood before the tee landed, with the script's own defaults
# (EMORI_CAM_SRC_CONTROLS stubbed empty, so `keep` is empty). Written out rather than
# derived: a regression test whose expectation is computed by the code under test
# cannot fail.
_PRE_CHANGE_PIPELINE = (
    "libcamerasrc ! video/x-raw,width=1280,height=720,framerate=15/1,format=NV12 ! "
    "videoconvert ! video/x-raw,format=YUY2 ! "
    "videobalance brightness=0.05 contrast=1.30 ! "
    "v4l2sink device=/dev/video40 sync=false"
)


def _text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def _pipeline_assignment() -> str:
    """The single `PIPELINE="..."` line, as written in the script."""

    match = re.search(r'^PIPELINE="(.*)"$', _text(), re.MULTILINE)
    assert match is not None, "camera-bridge.sh no longer has a single PIPELINE= line"
    return match.group(1)


# --- layer 1: the text, on every platform ---------------------------------------------


def test_the_script_is_lf_only_with_a_bash_shebang():
    """A CRLF shebang makes systemd exec fail 203 on the CM4."""

    raw = _SCRIPT.read_bytes()
    assert b"\r\n" not in raw
    assert raw.startswith(b"#!/usr/bin/env bash")


def test_the_pipeline_interpolates_the_tee_at_three_points_and_nowhere_else():
    """`${ros_tee}` before videoconvert, `${ros_branch}` after the kiosk sink.

    The tee has to sit immediately after the NV12 caps so the ROS branch scales the
    sensor-native format, and the branch has to be appended AFTER the kiosk sink so the
    kiosk's own chain (videoconvert -> YUY2 -> videobalance -> v4l2sink) is the same
    sequence of elements it has always been.
    """

    pipeline = _pipeline_assignment()

    assert "format=NV12 ! ${ros_tee}videoconvert" in pipeline
    assert pipeline.endswith("device=/dev/video${VIDEO_NR} sync=false${ros_branch}")
    # Exactly one of each: a second interpolation would fork the graph twice.
    assert pipeline.count("${ros_tee}") == 1
    assert pipeline.count("${ros_branch}") == 1


def test_the_kiosk_sink_is_untouched():
    """The badge scanner's device, format and sync flag are the shipped ones."""

    pipeline = _pipeline_assignment()

    assert "${balance}v4l2sink device=/dev/video${VIDEO_NR} sync=false" in pipeline
    assert "video/x-raw,width=${W},height=${H},framerate=${FPS}/1,format=NV12" in pipeline


def test_both_queues_are_leaky_downstream_and_bounded():
    """The one way this feature could damage a shipped capability.

    Without `leaky=downstream` a stalled ROS consumer back-pressures the tee and stalls
    the KIOSK branch -- face look-at would be able to break the badge scanner. Both
    branches, not just the ROS one: a `tee` blocks on its slowest pad whichever pad that
    is.
    """

    text = _text()

    assert text.count("queue leaky=downstream max-size-buffers=2") == 2
    # ...and NOT a blocking queue anywhere in the pipeline construction.
    assert not re.search(r"!\s*queue\s*!", text)


def test_the_kiosk_branch_says_out_loud_that_it_gained_a_queue():
    """It never had one. A leaky queue can drop a frame the scanner would have
    decoded, and that trade belongs in the file, not in a review comment."""

    text = _text()

    assert "has never had a queue before this change" in text
    assert "badge scanner" in text


def test_the_ros_branch_squeezes_rather_than_letterboxes():
    """`add-borders` has flipped its default across GStreamer releases.

    1280x720 -> 320x240 is an aspect change. A letterbox moves the principal point and is
    not modelled by the intrinsics the gaze component computes; an anamorphic squeeze is
    (x by 320/1280, y by 240/720). Pin it.
    """

    text = _text()

    assert "videoscale add-borders=false" in text
    assert "videorate ! videoscale" in text, "videorate must precede videoscale: scale 10 fps, not 30"


def test_the_ros_branch_is_env_gated_and_defaults_to_no_tee():
    text = _text()

    assert 'ROS_NR="${EMORI_CAM_ROS_VIDEO_NR:-}"' in text
    assert 'ROS_W="${EMORI_CAM_ROS_WIDTH:-320}"' in text
    assert 'ROS_H="${EMORI_CAM_ROS_HEIGHT:-240}"' in text
    assert 'ROS_FPS="${EMORI_CAM_ROS_FPS:-10}"' in text


def test_a_configured_but_absent_device_warns_instead_of_failing():
    """The kiosk camera is the shipped capability. It must come up even when the ROS
    loopback did not, and the operator must be told which command creates it."""

    text = _text()

    assert 'elif [ -n "$ROS_NR" ]; then' in text
    assert "ros-loopback.sh" in text
    assert ">&2" in text


# --- layer 2: the script, actually executed --------------------------------------------


def _bash() -> str | None:
    """A bash that can read a Windows path (Git Bash), else None.

    C:\\Windows\\System32\\bash.exe is the WSL launcher: it cannot open a Windows path
    and would report a bogus syntax failure.
    """

    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    return found if found and "system32" not in found.lower() else None


_SHELL_SKIP = pytest.mark.skipif(
    not os.environ.get("EMORI_TEST_SHELL") or _bash() is None,
    reason=(
        "shell test skipped: set EMORI_TEST_SHELL=1 and have a non-WSL bash on PATH. "
        "Layer 1 (the text assertions above) covered the same properties statically; "
        "this layer proves the three-way branch by RUNNING the script."
    ),
)


def _run_pipeline(tmp_path: Path, **env_overrides: str) -> str:
    """`EMORI_CAM_PRINT_PIPELINE=1 camera-bridge.sh`, with gst-inspect stubbed.

    The real gst-inspect is ~100 KiB of listing and is not on a dev box; the stub prints
    nothing, so every entry in EMORI_CAM_SRC_CONTROLS is dropped and `keep` is empty --
    which is exactly the deterministic baseline _PRE_CHANGE_PIPELINE records.
    """

    stub = tmp_path / "gst-inspect-stub.sh"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    stub.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "EMORI_CAM_PRINT_PIPELINE": "1",
        "EMORI_CAM_GST_INSPECT": str(stub).replace("\\", "/"),
        "EMORI_CAM_SRC_CONTROLS": "",
    })
    env.pop("EMORI_CAM_ROS_VIDEO_NR", None)
    env.update(env_overrides)

    bash = _bash()
    assert bash is not None
    result = subprocess.run(
        [bash, str(_SCRIPT)], capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@_SHELL_SKIP
def test_the_script_passes_a_bash_syntax_check():
    bash = _bash()
    assert bash is not None
    result = subprocess.run([bash, "-n", str(_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@_SHELL_SKIP
def test_with_no_ros_device_configured_the_pipeline_is_byte_identical_to_v1(tmp_path):
    """The whole compatibility story in one assertion."""

    assert _run_pipeline(tmp_path) == _PRE_CHANGE_PIPELINE


@_SHELL_SKIP
def test_a_configured_but_missing_device_still_produces_the_v1_pipeline(tmp_path):
    """A robot whose loopback was never created keeps its kiosk camera."""

    bash = _bash()
    assert bash is not None
    stub = tmp_path / "gst-inspect-stub.sh"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "EMORI_CAM_PRINT_PIPELINE": "1",
        "EMORI_CAM_GST_INSPECT": str(stub).replace("\\", "/"),
        "EMORI_CAM_SRC_CONTROLS": "",
        # 99 is not a device node on any box this test runs on, including the robot.
        "EMORI_CAM_ROS_VIDEO_NR": "99",
    })
    result = subprocess.run(
        [bash, str(_SCRIPT)], capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _PRE_CHANGE_PIPELINE
    assert "/dev/video99 is absent" in result.stderr
    assert "ros-loopback.sh 99" in result.stderr


@_SHELL_SKIP
def test_a_present_device_adds_the_tee_and_leaves_the_kiosk_chain_alone(tmp_path):
    """The existence check is `[ -e /dev/video<nr> ]`, so a plain file stands in for a
    device node on a box with no v4l2loopback."""

    fake_dev = tmp_path / "dev"
    fake_dev.mkdir()
    (fake_dev / "video41").write_text("", encoding="utf-8")

    # The script hard-codes /dev/video<nr>, so this branch can only be exercised where a
    # real /dev/video41 exists. Skip rather than lie about what was proven.
    if not os.path.exists("/dev/video41"):
        pytest.skip(
            "the present-device branch needs a real /dev/video41; layer 1 asserts the "
            "strings it produces and a run on the robot proves it live"
        )

    pipeline = _run_pipeline(tmp_path, EMORI_CAM_ROS_VIDEO_NR="41")

    assert "tee name=camsrc" in pipeline
    assert pipeline.count("queue leaky=downstream max-size-buffers=2") == 2
    assert "videoscale add-borders=false" in pipeline
    assert "width=320,height=240,framerate=10/1" in pipeline
    assert "v4l2sink device=/dev/video41 sync=false" in pipeline
    # The kiosk half of the graph, unchanged.
    assert "videobalance brightness=0.05 contrast=1.30 ! v4l2sink device=/dev/video40" in pipeline
