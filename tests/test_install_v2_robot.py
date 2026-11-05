"""``deploy/robot/install.sh`` on a robot that already runs v2, RUN in bash.

A brand-new robot that followed the v2 route has the camera tee armed in
``/etc/default/emori-camera`` and the daemon fence up. The installer must neither ask
for a ``.env`` line the robot no longer needs (step 5b) nor install a second owner of the
motor bus (step 11), and it retires the v1 graph a robot kept from its v1 days. Each step
runs as written, against a scratch root, with stubs for ``sudo``, ``docker`` and
``systemctl`` that only record what they were asked.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

_INSTALL = Path(__file__).resolve().parents[1] / "deploy" / "robot" / "install.sh"


def _text() -> str:
    return _INSTALL.read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    text = _text()
    return text[text.index(start):text.index(end)]


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


def _run(tmp_path, body: str, **env: str) -> subprocess.CompletedProcess:
    bash = _bash()
    if bash is None:
        pytest.skip("no bash")
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    for name in ("sudo", "docker"):
        stub = stubs / name
        # Record the call; `docker image inspect` succeeds, as on a robot that HAS a
        # Humble image (the case step 11 must still refuse on a fenced robot).
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log.as_posix()}"\nexit 0\n',
                        encoding="ascii", newline="\n")
        stub.chmod(0o755)
    # systemctl's read-only questions answer what STUB_UNIT_STATE names ("enabled",
    # "active", or neither), as the v1 graph's unit is on this robot.
    stub = stubs / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "systemctl $*" >> "{log.as_posix()}"\n'
        'case "$1" in\n'
        '  is-enabled) [ "${STUB_UNIT_STATE:-}" = enabled ] ;;\n'
        '  is-active) [ "${STUB_UNIT_STATE:-}" = active ] ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n", encoding="ascii", newline="\n")
    stub.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text("set -euo pipefail\n" + body, encoding="utf-8", newline="\n")
    result = subprocess.run(
        [bash, script.as_posix()], capture_output=True, encoding="utf-8",
        errors="replace", timeout=60, check=False, cwd=tmp_path,
        env={**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", **env})
    result.calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result


# --- step 5b: the look-at hint ---------------------------------------------------------


def _hint(tmp_path, camera_file: str | None) -> subprocess.CompletedProcess:
    path = tmp_path / "emori-camera"
    if camera_file is not None:
        path.write_text(camera_file, encoding="utf-8", newline="\n")
    body = _between("# The tee is armed in ONE place", "# 6. User-session lingering")
    return _run(tmp_path, body, EMORI_CAMERA_DEFAULTS=path.as_posix())


def test_an_armed_tee_needs_no_env_line(tmp_path):
    result = _hint(tmp_path, "EMORI_CAM_FPS=30\nEMORI_CAM_ROS_VIDEO_NR=41\n")

    assert result.returncode == 0, result.stderr
    assert "tee ARMED: EMORI_CAM_ROS_VIDEO_NR=41" in result.stdout
    assert "/dev/video41" in result.stdout
    # readycheck reads the number from the camera file: nothing for .env.
    assert "EMORI_READYCHECK_ROS_VIDEO_DEVICE" not in result.stdout
    assert ".env" not in result.stdout


@pytest.mark.parametrize("camera_file", [None, "", "EMORI_CAM_FPS=30\n",
                                         "EMORI_CAM_ROS_VIDEO_NR=41\nEMORI_CAM_ROS_VIDEO_NR=\n"])
def test_an_unarmed_tee_is_told_the_one_line_that_arms_it(tmp_path, camera_file):
    result = _hint(tmp_path, camera_file)

    assert result.returncode == 0, result.stderr
    assert "to ARM look-at:  EMORI_CAM_ROS_VIDEO_NR=41 in /etc/default/emori-camera" \
        in result.stdout
    assert "EMORI_READYCHECK_ROS_VIDEO_DEVICE" not in result.stdout
    # A bridge restart alone leaves the kiosk camera black: the hint restarts both.
    assert "restart emori-camera-bridge emori-kiosk" in result.stdout


# --- step 11: the v1 Humble graph -----------------------------------------------------


def _step11(tmp_path, *, fenced: bool, with_ros2: str,
            unit_state: str = "") -> subprocess.CompletedProcess:
    fence = tmp_path / "V2_OWNS_THE_BUS"
    if fenced:
        fence.write_text("", encoding="ascii")
    body = _between("# 11. OPTIONAL", 'echo\necho "== waiting for the daemon --no-media')
    return _run(tmp_path, body, EMORI_V2_FENCE=fence.as_posix(), EMORI_WITH_ROS2=with_ros2,
                STUB_UNIT_STATE=unit_state)


@pytest.mark.parametrize("with_ros2", ["1", "0"])
def test_a_fenced_robot_never_gets_the_v1_graph(tmp_path, with_ros2):
    """The v2 container owns the bus on a fenced robot. `EMORI_WITH_ROS2=1 bash install.sh`
    (the v1 recipe) with a Humble image on the
    card would `enable --now emori-ros2` beside it: two graphs, two hands on the motors."""

    result = _step11(tmp_path, fenced=True, with_ros2=with_ros2)

    assert result.returncode == 0, result.stderr
    assert "v2 robot" in result.stdout and "NOT installed" in result.stdout
    assert len([line for line in result.stdout.splitlines() if line.strip()]) == 1
    assert "sudo" not in result.calls                # no unit copied, enabled or started
    assert "docker" not in result.calls              # no image inspected or loaded


@pytest.mark.parametrize("unit_state", ["enabled", "active"])
def test_a_fenced_robot_loses_the_v1_graph_it_ran_before(tmp_path, unit_state):
    """Enabled from the robot's v1 days, emori-ros2 would start at the next boot beside
    the v2 graph; merely running, it is beside it now. The fence allows neither."""

    result = _step11(tmp_path, fenced=True, with_ros2="0", unit_state=unit_state)

    assert result.returncode == 0, result.stderr
    assert "sudo systemctl disable --now emori-ros2" in result.calls
    assert "disabled and stopped emori-ros2" in result.stdout
    assert "systemctl enable" not in result.calls and "docker" not in result.calls


def test_without_the_fence_the_v1_opt_in_still_installs_the_graph(tmp_path):
    result = _step11(tmp_path, fenced=False, with_ros2="1")

    assert result.returncode == 0, result.stderr
    assert "sudo systemctl enable --now emori-ros2" in result.calls
    assert "emori-ros2 enabled" in result.stdout


def test_without_the_fence_or_the_opt_in_step_11_does_nothing(tmp_path):
    result = _step11(tmp_path, fenced=False, with_ros2="0", unit_state="enabled")

    assert result.returncode == 0, result.stderr
    assert "skipped (EMORI_WITH_ROS2=0)" in result.stdout
    # A v1 robot's own graph is its operator's business: nothing asked, nothing touched.
    assert result.calls == ""


def test_the_fence_is_the_same_file_the_deploy_checks():
    deploy = (_INSTALL.parent / "one-click-deploy.sh").read_text(encoding="utf-8")
    fence = 'FENCE="${EMORI_V2_FENCE:-/opt/emori/V2_OWNS_THE_BUS}"'

    assert fence in deploy and "V2_" + fence in _text()
    # ...checked BEFORE the EMORI_WITH_ROS2 branch can run.
    text = _text()
    assert text.index('if [ -e "$V2_FENCE" ]') < text.index('"${EMORI_WITH_ROS2:-0}" = "1"')
