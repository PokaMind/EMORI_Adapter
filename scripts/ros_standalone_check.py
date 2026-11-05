"""Build the standalone ROS 2 image and prove the three publishable nodes start in it.

The claim under test is the one `LICENSING.md` makes: `emori_msgs`,
`hri_context_analyzer`, `hri_engagement_behaviors` and `emori_skill_facade` are usable by
someone who does not have the EMORI application. A grep for `emori_adapter` imports
(`tests/test_ros_standalone.py`) is the cheap half of that; this is the expensive half —
an image with no EMORI core installed at all (`deploy/ros2/Dockerfile.standalone`), in
which all three nodes are actually started and the graph is asked what it can see.

The distinction matters because an `ament_python` package builds perfectly while its node
imports something that is not there. `colcon build` stays green and the failure surfaces
at `ros2 run`, i.e. on the robot. Here it surfaces as a node missing from `ros2 node list`,
with the node's own log printed next to it.

    python scripts/ros_standalone_check.py
    python scripts/ros_standalone_check.py --no-build          # reuse the built image
    python scripts/ros_standalone_check.py --image emori/ros-standalone:arm64 \
        --platform linux/arm64

Exit codes: 0 PASS, 1 the smoke failed (or the build did), 2 Docker is not usable.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = "deploy/ros2/Dockerfile.standalone"
DEFAULT_IMAGE = "emori/ros-standalone:amd64"
# The host here is amd64 and the robot is a CM4. Stating the platform is not optional:
# an accidental arm64 image runs under emulation and takes minutes per colcon package,
# and an accidental amd64 image on the robot dies with a bare `exec format error`.
DEFAULT_PLATFORM = "linux/amd64"
# The build compiles emori_msgs with rosidl for three languages from a cold apt cache.
# Twenty minutes is generous on this box and still short of "hung".
BUILD_TIMEOUT_S = 1800
# The smoke settles for 8 s and then polls for up to 60. Three minutes is the wall.
RUN_TIMEOUT_S = 240

# Exactly what the smoke must find. Kept here as well as in the shell script so this
# script can say what was expected even when the container produced nothing at all.
EXPECTED_NODES = ("/hri_context_analyzer", "/hri_engagement_behaviors", "/skill_facade")


def _run(command: list[str], *, timeout: int) -> int:
    """Run a command with its output streamed straight through, and return its status."""

    print(f"$ {' '.join(command)}", flush=True)
    try:
        return subprocess.call(command, cwd=REPO_ROOT, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT after {timeout}s: {' '.join(command)}", file=sys.stderr)
        return 124


def build_command(image: str, platform: str, *, buildx: bool) -> list[str]:
    """The build invocation, buildx when it exists because `--platform` needs it.

    Plain `docker build --platform` works on a modern daemon with BuildKit, but buildx is
    the form that is guaranteed to honour the flag and to `--load` the result into the
    local image store, which is what `docker run` reads.
    """

    if buildx:
        return ["docker", "buildx", "build", "--platform", platform, "--load",
                "-t", image, "-f", DOCKERFILE, "."]
    return ["docker", "build", "--platform", platform, "-t", image, "-f", DOCKERFILE, "."]


def has_buildx() -> bool:
    try:
        completed = subprocess.run(["docker", "buildx", "version"], cwd=REPO_ROOT,
                                   capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def node_lines(output: str) -> list[str]:
    """The `ros2 node list` block out of the smoke's output, for the summary line.

    Parsed rather than re-derived: the authoritative list is what the container saw, and
    re-running `ros2 node list` from here would need a second container.
    """

    lines = output.splitlines()
    try:
        start = lines.index("== ros2 node list")
    except ValueError:
        return []
    found: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("== "):
            break
        stripped = line.strip()
        if stripped.startswith("/"):
            found.append(stripped)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    parser.add_argument("--no-build", dest="build", action="store_false",
                        help="reuse the image already in the local store")
    parser.add_argument("--build-timeout", type=int, default=BUILD_TIMEOUT_S)
    parser.add_argument("--run-timeout", type=int, default=RUN_TIMEOUT_S)
    args = parser.parse_args(argv)

    if shutil.which("docker") is None:
        print("docker is not on PATH: this check needs a running Docker daemon",
              file=sys.stderr)
        return 2

    if args.build:
        status = _run(build_command(args.image, args.platform, buildx=has_buildx()),
                      timeout=args.build_timeout)
        if status != 0:
            print(f"FAIL: building {args.image} exited {status}", file=sys.stderr)
            return 1

    command = ["docker", "run", "--rm", args.image]
    print(f"$ {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                                   timeout=args.run_timeout, check=False)
    except subprocess.TimeoutExpired:
        print(f"FAIL: the smoke did not finish within {args.run_timeout}s", file=sys.stderr)
        return 1
    except OSError as error:  # the daemon went away between the build and the run
        print(f"FAIL: could not run {args.image}: {error}", file=sys.stderr)
        return 2

    output = completed.stdout or ""
    print(output, end="" if output.endswith("\n") else "\n")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")

    found = node_lines(output)
    print(f"nodes seen in {args.image}: {found or '(none)'}")
    missing = [name for name in EXPECTED_NODES if name not in found]
    if missing:
        print(f"missing: {missing}", file=sys.stderr)

    verdict = "PASS" if completed.returncode == 0 else "FAIL"
    print(f"{verdict}: {args.image} smoke exited {completed.returncode}")
    return 0 if completed.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
