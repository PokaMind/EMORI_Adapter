"""Capture the motor container's filesystem as a local build environment.

Streams ``docker export`` of the motor container over ssh into a local tar,
then imports it on this machine as a ``linux/arm64`` image tagged by the
robot's own image id (``emori/robot-buildenv:<id>``). Anything compiled
inside that image matches the ABI of the files the robot runs, hand patches
included.

Why export and not save: ``docker save`` on the robot's overlay2 store writes
every layer to a temporary directory on the robot's disk before it streams
them, which needs more space than the card has free. ``docker export`` streams
the merged filesystem with no temporary copy. The result is flat, so it is a
build environment and a backup of that filesystem, and can never be pushed
back as a layered image. This script refuses ``save`` by construction: every
docker call goes through ``docker_argv``, which rejects it.

Importing drops the image's ENV (``PATH``, ``LANG``, ``ROS_DISTRO``); whatever
builds on the image has to set them again.

When this is needed: only a change to the motor container's interfaces
(messages and services) has to be compiled against the robot's ABI and so
needs this environment. A Python-only motor change does not; it ships as a
small overlay layer on the robot.

The export reads the whole filesystem (about 1.7 GB) over the network, so run
it while the robot is idle. The tar is kept in the output directory, so an
import problem never costs a second transfer. Use ``--remove-tar`` once the
imported image has been used successfully.

    python scripts/capture_robot_base.py --ssh pollen@reachy-mini.local
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_CONTAINER = "emori-ros2-v2"
IMAGE_REPO = "emori/robot-buildenv"
PLATFORM = "linux/arm64"
EXPECTED_ARCH = "arm64"
DEFAULT_OUT = Path("build") / "robot-base"

_FORBIDDEN_VERBS = frozenset({"save"})
_IMAGE_ID = re.compile(r"^(?:sha256:)?([0-9a-f]{12,64})$")

# A runner executes argv. With ``stdout_to`` set it streams stdout into that
# file instead of capturing it. It returns (returncode, captured stdout text).
Runner = Callable[[Sequence[str], "Path | None"], "tuple[int, str]"]


class CaptureError(RuntimeError):
    """The capture stopped; nothing was left half-tagged."""


def docker_argv(*args: str) -> list[str]:
    """Build a docker command, refusing any form of ``save``."""
    words = [a for a in args if not a.startswith("-")]
    if any(w in _FORBIDDEN_VERBS for w in words[:2]):
        raise CaptureError("docker save is refused: it needs temporary disk the robot lacks")
    return ["docker", *args]


# What --ssh-option added: a key, a known-hosts file or a route the caller reaches the robot by.
_SSH_OPTIONS: list[str] = []


def ssh_argv(target: str, remote: Sequence[str]) -> list[str]:
    """Run a docker command on the robot over ssh as one remote command string."""
    if not remote or remote[0] != "docker":
        raise CaptureError("only docker commands run on the robot")
    return ["ssh", *_SSH_OPTIONS, target, shlex.join(remote)]


def run_subprocess(argv: Sequence[str], stdout_to: Path | None) -> tuple[int, str]:
    if stdout_to is not None:
        with open(stdout_to, "wb") as fh:
            return subprocess.run(list(argv), stdout=fh).returncode, ""
    done = subprocess.run(list(argv), capture_output=True, text=True)
    return done.returncode, done.stdout


def _checked(run: Runner, argv: Sequence[str], what: str,
             stdout_to: Path | None = None) -> str:
    code, out = run(argv, stdout_to)
    if code != 0:
        raise CaptureError(f"{what} failed (exit {code})")
    return out


def robot_image_id(run: Runner, target: str, container: str) -> str:
    """The short image id the container runs, used to tag the capture."""
    argv = ssh_argv(target, docker_argv(
        "inspect", "--format", "{{.Image}}", container))
    out = _checked(run, argv, "reading the robot's image id").strip()
    match = _IMAGE_ID.match(out)
    if not match:
        raise CaptureError(f"unexpected image id from the robot: {out!r}")
    return match.group(1)[:12]


def capture(run: Runner, target: str, out_dir: Path,
            container: str = DEFAULT_CONTAINER) -> str:
    """Export, import and verify. Returns the imported image tag."""
    image_id = robot_image_id(run, target, container)
    tag = f"{IMAGE_REPO}:{image_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / f"base-{image_id}.tar"

    _checked(run, ssh_argv(target, docker_argv("export", container)),
             "docker export", stdout_to=tar_path)
    if not tar_path.exists() or tar_path.stat().st_size == 0:
        raise CaptureError(f"the export wrote nothing to {tar_path}")

    _checked(run, docker_argv("import", "--platform", PLATFORM, str(tar_path), tag),
             "docker import")

    arch = _checked(run, docker_argv(
        "image", "inspect", "--format", "{{.Architecture}}", tag),
        "reading the imported architecture").strip()
    if arch != EXPECTED_ARCH:
        raise CaptureError(
            f"{tag} has architecture {arch!r}, expected {EXPECTED_ARCH!r}; "
            f"the tar is kept at {tar_path}")
    return tag


def main(argv: Sequence[str] | None = None, run: Runner = run_subprocess) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ssh", required=True, metavar="USER@HOST",
                    help="ssh target of the robot")
    ap.add_argument("--container", default=DEFAULT_CONTAINER)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--remove-tar", action="store_true",
                    help="delete the tar after a successful import")
    ap.add_argument("--ssh-option", action="append", default=[], metavar="ARG",
                    help="one more argument for ssh; repeat for each (=-i, =key)")
    args = ap.parse_args(argv)
    _SSH_OPTIONS[:] = args.ssh_option
    try:
        tag = capture(run, args.ssh, args.out_dir, args.container)
    except CaptureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.remove_tar:
        image_id = tag.rsplit(":", 1)[1]
        (args.out_dir / f"base-{image_id}.tar").unlink(missing_ok=True)
    print(f"imported {tag} ({PLATFORM})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
