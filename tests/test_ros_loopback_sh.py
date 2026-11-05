"""`deploy/robot/ros-loopback.sh` and the unit line that calls it.

This script exists for one reason and the tests are about that reason: systemd does NOT
do `${VAR:-default}` expansion. It expands `${VAR}` in an Exec line itself and has no
`:-` form at all, so an inline

    ExecStartPre=+/bin/sh -c 'test -e /dev/video${EMORI_CAM_ROS_VIDEO_NR:-41} || ...'

is a line whose behaviour depends on the systemd version -- the same trap that produced
camera-bridge.sh in the first place (camera-bridge.sh:5-8). The default belongs in a
shell script; the unit calls it bare.

The other two properties are safety properties on a LIVE robot: the script must be a no-op
when nothing is configured (every v1 robot), and it must never reload the module, because
`modprobe -r v4l2loopback` destroys /dev/video40 under Chromium's open handle.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "robot" / "ros-loopback.sh"
_UNIT = _ROOT / "deploy" / "robot" / "camera-bridge.service"


def _text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def _exec_lines() -> list[str]:
    """Every `Exec*=` line of the unit, continuations joined."""

    return [line for line in _UNIT.read_text(encoding="utf-8").splitlines()
            if re.match(r"^Exec(Start|StartPre|StartPost|Stop|StopPost|Reload)=", line)]


# --- the script -----------------------------------------------------------------------


def test_the_script_is_lf_only_with_a_posix_sh_shebang():
    """/bin/sh, not bash: this runs as an ExecStartPre on a robot where the unit must come
    up before anything else, and a CRLF shebang is an exec failure 203."""

    raw = _SCRIPT.read_bytes()
    assert b"\r\n" not in raw
    assert raw.startswith(b"#!/bin/sh")


def test_an_unconfigured_rig_is_a_no_op():
    """No argument and no env var means exit 0 having done nothing.

    Every v1 robot is this robot. The unit calls the script unconditionally, so "not
    configured" has to be success, not a failure that puts emori-camera-bridge into
    restart backoff and takes the kiosk camera with it.
    """

    text = _text()

    assert 'NR="${1:-${EMORI_CAM_ROS_VIDEO_NR:-}}"' in text
    assert '[ -n "$NR" ] || exit 0' in text


def test_the_script_is_idempotent():
    """systemd runs ExecStartPre on every start, and `v4l2loopback-ctl add` on an
    existing number fails."""

    text = _text()

    assert '[ -e "/dev/video${NR}" ] && exit 0' in text
    # ...and the exit-0 check comes BEFORE the add.
    assert text.index('[ -e "/dev/video${NR}" ]') < text.index("v4l2loopback-ctl add")


def test_the_script_never_reloads_the_module():
    """`modprobe -r v4l2loopback` destroys /dev/video40 out from under Chromium's open
    handle: the kiosk camera dies and only a kiosk restart brings it back. A live
    `v4l2loopback-ctl add` leaves the module -- and video40 -- alone."""

    # Directives only: the header EXPLAINS the modprobe trap at length, and prose about
    # a command is not a use of it.
    directives = "\n".join(line for line in _text().splitlines()
                           if line.strip() and not line.lstrip().startswith("#"))

    assert "v4l2loopback-ctl add" in directives
    assert "modprobe" not in directives
    assert "/dev/video40" not in directives


def test_the_new_node_is_created_with_the_same_caps_as_the_kiosk_node():
    """exclusive_caps and max_buffers=2, matching the persisted modprobe options: a node
    created by hand must not behave differently from one created at boot."""

    text = _text()

    assert "-n ReachyCamROS" in text
    assert "-x 1" in text
    assert "-b 2" in text


def test_the_script_says_why_it_is_a_script_and_not_an_exec_line():
    """The reason is the whole point of the file and is the thing a future editor will
    try to undo."""

    text = _text()

    assert "systemd does NOT do" in text


# --- the unit line that calls it -------------------------------------------------------


def test_the_unit_calls_the_script_bare():
    """No argument, so the default lives in /bin/sh where a default can exist."""

    lines = _exec_lines()

    assert "ExecStartPre=+-/opt/emori/deploy/robot/ros-loopback.sh" in lines


def test_no_exec_line_carries_a_shell_default_expansion():
    """`${VAR:-default}` in a systemd Exec line is the bug this whole file exists to
    avoid, and it would be invisible until a robot on a different systemd."""

    for line in _exec_lines():
        assert ":-" not in line, line


def _prefix_of(line: str) -> str:
    """The `@-+!` prefix characters systemd allows, in any order, before the path."""

    return line.split("=", 1)[1].split("/", 1)[0]


def test_the_loopback_pre_step_runs_as_root():
    """`+` = run as root. v4l2loopback-ctl needs it, and the unit's User is pollen.

    The prefix characters may be combined in any order, so this asks for the character
    rather than for a literal `ExecStartPre=+`.
    """

    line = next(line for line in _exec_lines() if "ros-loopback.sh" in line)

    assert "+" in _prefix_of(line), line


def test_the_loopback_pre_step_is_non_fatal_and_the_kiosk_one_is_not():
    """`-` on the ROS loopback, and deliberately NOT on the kiosk modprobe.

    systemd treats a non-zero ExecStartPre without `-` as a unit failure, so a fail-hard
    ROS pre-step would let the OPTIONAL face look-at take down the SHIPPED badge scanner:
    no bridge means no /dev/video40 means no kiosk camera. Every failure mode here is
    reachable -- `v4l2loopback-ctl add` refused, v4l2loopback-utils absent, or 203/EXEC
    from a lost exec bit -- and camera-bridge.sh already applies this exact rule one file
    over, warning rather than failing when the ROS device is missing. The kiosk modprobe
    keeps no `-`: without video40 the bridge has nothing to serve and failing is correct.
    """

    lines = _exec_lines()
    ros = next(line for line in lines if "ros-loopback.sh" in line)
    kiosk = next(line for line in lines if "modprobe v4l2loopback" in line)

    assert "-" in _prefix_of(ros), ros
    assert "-" not in _prefix_of(kiosk), kiosk


def test_the_script_is_executable_in_git():
    """Mode 100755 in the index, checked because this box cannot check it on disk.

    `git config core.filemode` is false on the Windows dev box, so a new file lands in
    the index 100644 while every sibling deploy/robot/*.sh is 100755. install.sh step 0
    chmods them, but a `git archive | tar -x` deploy that skips install.sh does not --
    and systemd answers a non-executable ExecStartPre with 203/EXEC.
    """

    git = shutil.which("git")
    if git is None:
        pytest.skip("shell test skipped: git is not on PATH, so the index mode cannot be read")

    result = subprocess.run(
        [git, "ls-files", "-s", "--", "deploy/robot/ros-loopback.sh"],
        cwd=str(_ROOT), capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("shell test skipped: ros-loopback.sh is not tracked yet "
                    "(`git add --chmod=+x deploy/robot/ros-loopback.sh`)")

    assert result.stdout.split(" ", 1)[0] == "100755", result.stdout.strip()


def test_the_environment_file_precedes_both_pre_steps():
    """ros-loopback.sh reads EMORI_CAM_ROS_VIDEO_NR from /etc/default/emori-camera.

    systemd applies EnvironmentFile to every Exec line regardless of order, so this is a
    READABILITY contract rather than a functional one -- and it is asserted because the
    functional version of the same mistake (splitting the variable from its reader) is
    what an editor reaching for "tidy the unit" would do.
    """

    body = _UNIT.read_text(encoding="utf-8")
    env_at = body.index("EnvironmentFile=-/etc/default/emori-camera")

    for line in _exec_lines():
        if line.startswith("ExecStartPre="):
            assert env_at < body.index(line), line


def test_the_kiosk_pre_step_is_unchanged():
    """video40 is still created by the same modprobe fallback it always was."""

    lines = _exec_lines()

    assert any("modprobe v4l2loopback video_nr=40 exclusive_caps=1" in line
               for line in lines)


# --- the script actually parses --------------------------------------------------------


def _sh() -> str | None:
    """A non-WSL sh/bash that can read a Windows path (Git Bash), else None."""

    for candidate in (
        r"C:\Program Files\Git\usr\bin\sh.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        "/bin/sh",
        "/bin/bash",
    ):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("sh") or shutil.which("bash")
    return found if found and "system32" not in found.lower() else None


def test_the_script_passes_a_shell_syntax_check():
    shell = _sh()
    if shell is None:
        pytest.skip("shell test skipped: no non-WSL sh/bash available to syntax-check the script")

    result = subprocess.run([shell, "-n", str(_SCRIPT)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_an_unconfigured_run_really_exits_zero():
    """The no-op path, executed rather than read. `v4l2loopback-ctl` is not on this box,
    so a run that reached the `add` would fail 127 -- which is the assertion."""

    shell = _sh()
    if shell is None or not os.environ.get("EMORI_TEST_SHELL"):
        pytest.skip(
            "shell test skipped: set EMORI_TEST_SHELL=1 and have a non-WSL sh on PATH. "
            "The text assertions above cover the same no-op contract statically."
        )

    env = dict(os.environ)
    env.pop("EMORI_CAM_ROS_VIDEO_NR", None)
    result = subprocess.run([shell, str(_SCRIPT)], capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
