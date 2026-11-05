"""Run the outside consumer against a live EMORI graph and judge what it saw.

The claim this checks is "our ROS 2 graph is readable by a standard ROS4HRI consumer",
and the only honest way to check it is with a consumer that cannot cheat. So the actual
reading is done by `tests/ros/outside_consumer.py` inside `emori/outside-consumer`, an
image that contains `hri_msgs` and nothing of ours (`deploy/ros2/Dockerfile.consumer`).
This script is the harness around it: build, run, read the report, assert, archive.

Networking: the consumer joins the adapter container's network namespace
(`--network container:emori-ros2`). That is what keeps `ROS_LOCALHOST_ONLY=1` true for
both sides — same namespace, same loopback — instead of weakening the setting to let a
second container in. `ROS_DOMAIN_ID`, `ROS_LOCALHOST_ONLY` and `RMW_IMPLEMENTATION` are
read off the running container with `docker inspect` and passed through, because a
domain mismatch produces an empty report that looks exactly like a broken publisher.
The consumer also runs with `FASTDDS_BUILTIN_TRANSPORTS=UDPv4`: a shared network
namespace makes Fast DDS believe both participants can talk over shared memory, but
`/dev/shm` is per-container, so the default SHM-first stack discovers every topic and
delivers no data at all. That is a property of the consumer's own container, not of the
graph under test, which is why it is set here and not in docker-compose.yml.

The second half of the check does need EMORI's own eyes: `ros2 topic echo --once` on
`/humans/persons/<id>/context` inside the adapter container, to confirm that the
identity the EMORI extension carries is a pseudonym when `EMORI_ROS_DEIDENTIFY` is on.
The outside consumer cannot decode that message — that is the point of it — so the
privacy assertion is made where the message can be read.

`--v2` points the whole thing at the v2 graph (`emori-ros2-v2`, Jazzy) and adds the
claim v1 could not make: the robot is on the graph. An `hri_msgs`/`tf2_msgs`-only
container must be able to read `/joint_states` at the control-loop rate, see all nine
joints by name, and find `base_link -> head` on `/tf` — with no EMORI code, no Pollen
SDK and no HTTP daemon anywhere. The consumer image is built from the same Dockerfile
with a different `--build-arg BASE`, because a Humble subscriber and a Jazzy publisher
discover each other and never match: the type hashes differ.

    python scripts/outside_consumer_check.py                # v1, Humble, emori-ros2
    python scripts/outside_consumer_check.py --v2           # v2, Jazzy, emori-ros2-v2
    python scripts/outside_consumer_check.py --seconds 45 --no-build

Exit codes: 0 PASS, 1 FAIL (assertions), 2 the graph is not up.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = "emori/outside-consumer:amd64"
DEFAULT_CONTAINER = "emori-ros2"
DOCKERFILE = "deploy/ros2/Dockerfile.consumer"
REPORT_DIR = REPO_ROOT / "artifacts" / "outside-consumer"

# --- v1 vs v2 ------------------------------------------------------------------------
# The distro is not a preference. DDS discovery works across distros but the message type
# hashes do not match, so a Humble consumer reading a Jazzy graph finds every topic and
# receives nothing -- indistinguishable from a dead publisher. One Dockerfile, two bases.
V1_BASE = "ros:humble-ros-base"
V1_DISTRO = "humble"
V2_BASE = "ros:jazzy-ros-base"
V2_DISTRO = "jazzy"
V2_IMAGE = "emori/outside-consumer:jazzy"
V2_CONTAINER = "emori-ros2-v2"

# The container environment that decides whether two nodes can see each other at all.
# Only the variables actually set on the adapter are forwarded: inventing a default
# here would be the checker quietly disagreeing with the graph it is measuring.
PASSTHROUGH = ("ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY", "RMW_IMPLEMENTATION")

# A 30 s window at the 2 s cycle cadence is ~15 engagement messages. Five is the
# "this lane is alive and it is not a single latched leftover" line, low enough that a
# slow emulated arm64 image under Docker Desktop does not fail an otherwise good graph.
MIN_ENGAGEMENT_SAMPLES = 5

# --- the v2 assertions ----------------------------------------------------------------
# The nine joints, in the order reachy_mini_ros2 publishes them: Pollen's body yaw, the
# six Stewart-platform legs of the head, then the antennas in the daemon's [right, left]
# wire order. Asserted as a SET -- the order is reachy_mini_msgs' business and a consumer
# that cared about it would be reading positionally, which is exactly the coupling
# /joint_states carrying names exists to avoid.
EXPECTED_JOINT_NAMES = ("body_yaw", "stewart_1", "stewart_2", "stewart_3", "stewart_4",
                        "stewart_5", "stewart_6", "right_antenna", "left_antenna")
# One TF edge is the whole claim: the head's pose is expressed in the robot's base frame,
# so any ROS tool can transform into it without knowing what a Reachy Mini is.
REQUIRED_TF_EDGE = "base_link->head"
# 30 s at 50 Hz is ~1500 messages. 100 is "the stream is real and sustained", i.e. two
# orders of magnitude above the single latched leftover that would pass a naive >0.
MIN_JOINT_STATE_COUNT = 100
# The control loop's own rate, measured by an outsider rather than claimed by the node.
# Same 40 Hz floor the rehearsal uses, for the same reason: 20% of headroom on a busy VM.
MIN_JOINT_STATE_HZ = 40.0

_START_HINT = """the adapter container is not running. Start the mock graph first:

  docker compose -f deploy/ros2/docker-compose.yml build
  EMORI_SOURCE=mock EMORI_WITH_FIWARE=false FIWARE_BRIDGE_ENABLED=0 \\
      docker compose -f deploy/ros2/docker-compose.yml up -d

(PowerShell: set the three variables with $env:NAME="..." before `docker compose up -d`.)
"""

_START_HINT_V2 = """the v2 adapter container is not running. Start the mock graph first:

  docker compose -f deploy/ros2/docker-compose.v2.yml build
  EMORI_SOURCE=mock docker compose -f deploy/ros2/docker-compose.v2.yml up -d

(PowerShell: $env:EMORI_SOURCE="mock" before `docker compose ... up -d`.)
Note that v2 publishes :8000 — Pollen's Windows daemon must not be running.
"""


# --- the judgement, with no Docker anywhere near it ----------------------------------


def evaluate(report: Mapping, person_id: str,
             expression_vocabulary: Sequence[str]) -> list[str]:
    """Every way this report fails the outside-consumer contract, as sentences.

    A list rather than an exception: one run should tell you everything that is wrong,
    because each retry costs a container start and 30 s of listening. Pure and
    Docker-free so `tests/test_outside_consumer_script.py` can drive it on fixtures —
    the assertions are the part worth regression-testing, the `docker run` is not.

    `expression_vocabulary` is supplied by the caller (the consumer reads it off the
    installed `hri_msgs` at runtime and records it) rather than hardcoded here, so this
    check compares our labels against the interface instead of against themselves.
    """

    if not isinstance(report, Mapping):
        return ["the report is not a JSON object"]

    failures: list[str] = []

    tracked = report.get("tracked_ids")
    if tracked != [person_id]:
        failures.append(
            f"tracked_ids is {tracked!r}, expected exactly ['{person_id}'] — a standard "
            "consumer finds the person through this latched topic or not at all")

    counts = report.get("engagement_counts")
    if isinstance(counts, Mapping):
        total = sum(int(value) for value in counts.values())
    else:
        failures.append("engagement_counts is missing from the report")
        total = 0
    if total < MIN_ENGAGEMENT_SAMPLES:
        failures.append(
            f"only {total} engagement_status messages in the window, expected at least "
            f"{MIN_ENGAGEMENT_SAMPLES} — check the QoS match and the domain id")

    invalid = report.get("invalid_levels")
    if invalid != 0:
        failures.append(
            f"invalid_levels is {invalid!r}, expected 0 — a level outside the REP-155 "
            "constants is a message no standard consumer can interpret")

    vocabulary = set(expression_vocabulary or ())
    seen = report.get("expressions")
    if isinstance(seen, Mapping):
        outside = sorted(label for label in seen if label not in vocabulary)
        if outside:
            failures.append(
                f"expression labels outside the installed hri_msgs vocabulary: {outside}")
    else:
        failures.append("expressions is missing from the report")

    unknown = report.get("unknown_expression_labels")
    if unknown:
        failures.append(
            f"the consumer itself flagged unknown expression labels: {unknown}")

    return failures


def evaluate_v2(report: Mapping) -> list[str]:
    """The extra claim v2 makes: the ROBOT is on this graph, in standard messages.

    Separate from `evaluate` rather than a branch inside it so the v1 check keeps exactly
    the assertions it has always had, and so a v1 report run through `--v2` by mistake
    fails loudly on every row instead of half-passing. Pure, like its sibling, and
    sentences rather than an exception for the same reason: one 30 s run should tell you
    everything that is wrong.
    """

    if not isinstance(report, Mapping):
        return ["the report is not a JSON object"]

    failures: list[str] = []

    count = report.get("joint_state_count")
    count = count if isinstance(count, int) else 0
    if count < MIN_JOINT_STATE_COUNT:
        failures.append(
            f"only {count} /joint_states messages, expected at least "
            f"{MIN_JOINT_STATE_COUNT} — an outside node must be able to read the robot's "
            "state as plain sensor_msgs/JointState, with no EMORI code and no HTTP")

    names = report.get("joint_names")
    seen = set(names) if isinstance(names, (list, tuple)) else set()
    missing = [name for name in EXPECTED_JOINT_NAMES if name not in seen]
    if missing:
        failures.append(
            f"/joint_states is missing joints {missing} (saw {sorted(seen)}) — all nine "
            "motors have to be named on the wire or a consumer cannot address them")
    extra = sorted(seen - set(EXPECTED_JOINT_NAMES))
    if extra:
        failures.append(f"/joint_states carries unexpected joint names: {extra}")

    try:
        hz = float(report.get("joint_state_hz", 0.0))
    except (TypeError, ValueError):
        hz = 0.0
    if hz < MIN_JOINT_STATE_HZ:
        failures.append(
            f"/joint_states arrived at {hz:.1f} Hz, expected at least "
            f"{MIN_JOINT_STATE_HZ:.0f} — the 50 Hz control loop is the robot, and a "
            "consumer that sees it slow is seeing a loop that is not keeping up")

    frames = report.get("tf_frames")
    frames = list(frames) if isinstance(frames, (list, tuple)) else []
    if REQUIRED_TF_EDGE not in frames:
        failures.append(
            f"/tf does not carry {REQUIRED_TF_EDGE!r} (saw {frames}) — without it the "
            "head pose is a number, not a pose any ROS tool can transform")

    return failures


def evaluate_context_echo(echo: str, *, deidentify: bool) -> list[str]:
    """Does the EMORI extension topic carry a pseudonym or a roster id?

    `ros2 topic echo --once` prints YAML and the only line that matters is the first
    `person_id:`. Parsed by hand rather than with PyYAML so this script keeps running
    from a bare checkout with nothing installed — it is meant to be runnable by someone
    who does not have this repository's virtualenv.
    """

    value: str | None = None
    for line in (echo or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("person_id:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            break
    if value is None:
        return ["no person_id line in the /context echo — did the topic publish at all?"]

    if deidentify and not value.startswith("p-"):
        return [f"EMORI_ROS_DEIDENTIFY is on but Context.person_id is {value!r}: the "
                "publisher is putting a real identity on DDS"]
    if not deidentify and value.startswith("p-"):
        return [f"EMORI_ROS_DEIDENTIFY is off but Context.person_id is {value!r}: the "
                "graph is not publishing what this deployment asked for"]
    return []


# --- the Docker half -----------------------------------------------------------------


# Every docker command is issued from the repository root: the consumer Dockerfile
# is addressed as `deploy/ros2/Dockerfile.consumer` with the build context `.`, and a
# checker started from another directory must not pick up a different `.`.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(command: Sequence[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), capture_output=True, text=True, timeout=timeout,
                          check=False, cwd=str(_REPO_ROOT))


def container_environment(container: str) -> dict[str, str] | None:
    """The running container's env as a dict, or None when it is not running."""

    probe = _run(["docker", "inspect", "-f", "{{.State.Running}}|{{json .Config.Env}}",
                  container], timeout=60.0)
    if probe.returncode != 0:
        return None
    running, _, raw = probe.stdout.strip().partition("|")
    if running.lower() != "true":
        return None
    try:
        entries = json.loads(raw or "[]")
    except json.JSONDecodeError:
        entries = []
    environment: dict[str, str] = {}
    for entry in entries:
        name, _, value = str(entry).partition("=")
        environment[name] = value
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--person-id", default="learner_01")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--v2", action="store_true",
                        help="check the v2 (Jazzy) graph in emori-ros2-v2 and assert the "
                             "robot's own topics: /joint_states and /tf")
    parser.add_argument("--container", default="",
                        help=f"default: {DEFAULT_CONTAINER}, or {V2_CONTAINER} with --v2")
    parser.add_argument("--image", default="",
                        help=f"default: {DEFAULT_IMAGE}, or {V2_IMAGE} with --v2")
    parser.add_argument("--no-build", action="store_true",
                        help="reuse the consumer image instead of rebuilding it")
    args = parser.parse_args()

    # The distro of the consumer must follow the distro of the graph, not a flag someone
    # can forget: see V1_BASE/V2_BASE above for what a mismatch looks like (everything
    # discovered, nothing received).
    container = args.container or (V2_CONTAINER if args.v2 else DEFAULT_CONTAINER)
    image = args.image or (V2_IMAGE if args.v2 else DEFAULT_IMAGE)
    base = V2_BASE if args.v2 else V1_BASE
    distro = V2_DISTRO if args.v2 else V1_DISTRO

    environment = container_environment(container)
    if environment is None:
        print(f"[outside-consumer] {_START_HINT_V2 if args.v2 else _START_HINT}")
        return 2
    print(f"[outside-consumer] adapter container '{container}' is up "
          f"({'v2 / ' + distro if args.v2 else 'v1 / ' + distro})")

    if not args.no_build:
        print(f"[outside-consumer] building {image} from {DOCKERFILE} (BASE={base})")
        build = _run(["docker", "build", "-t", image, "-f", DOCKERFILE,
                      "--build-arg", f"BASE={base}",
                      "--build-arg", f"ROS_DISTRO_ARG={distro}", "."],
                     timeout=1800.0)
        if build.returncode != 0:
            print(build.stdout[-4000:])
            print(build.stderr[-4000:], file=sys.stderr)
            print("[outside-consumer] FAIL: the consumer image did not build")
            return 1

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / "report.json"
    if report_path.exists():
        report_path.unlink()  # never judge a previous run's file

    command = ["docker", "run", "--rm", "--network", f"container:{container}",
               # Fast DDS sees two participants behind the same network namespace as
               # "same host" and prefers its shared-memory transport -- but /dev/shm is
               # NOT shared (only the NET namespace is), so discovery succeeds and not
               # one sample is ever delivered: topics_seen full, engagement_counts {}.
               # Restricting the CONSUMER to UDPv4 leaves the graph under test exactly as
               # shipped; the writer simply uses the only locator its reader announced.
               # Sharing IPC instead would need the adapter container started with
               # --ipc=shareable, i.e. changing the thing being measured.
               "-e", "FASTDDS_BUILTIN_TRANSPORTS=UDPv4",
               "-v", f"{REPORT_DIR}:/out"]
    for name in PASSTHROUGH:
        if name in environment:
            command += ["-e", f"{name}={environment[name]}"]
    command += [image, "--person-id", args.person_id,
                "--seconds", str(args.seconds), "--out", "/out/report.json"]
    print(f"[outside-consumer] listening for {args.seconds:.0f}s with standard "
          "messages only")
    run = _run(command, timeout=args.seconds + 180.0)
    if run.returncode != 0:
        print(run.stdout[-4000:])
        print(run.stderr[-4000:], file=sys.stderr)
        print("[outside-consumer] FAIL: the consumer container exited non-zero")
        return 1

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"[outside-consumer] FAIL: no readable report at {report_path}: {error}")
        return 1

    # The privacy half, read where the message can actually be decoded. `--once` blocks
    # until a message arrives, so it is bounded by `timeout` inside the container.
    topic = f"/humans/persons/{args.person_id}/context"
    echo = _run(["docker", "exec", container, "/entrypoint.sh",
                 "timeout", "15", "ros2", "topic", "echo", "--once", topic], timeout=90.0)

    deidentify = str(environment.get("EMORI_ROS_DEIDENTIFY", "1")).strip().lower() \
        not in {"0", "false", "no", "off"}
    failures = evaluate(report, args.person_id,
                        report.get("expression_vocabulary", []))
    failures += evaluate_context_echo(echo.stdout, deidentify=deidentify)
    if args.v2:
        failures += evaluate_v2(report)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = REPORT_DIR / f"{stamp}.json"
    archive.write_text(json.dumps({
        "checked_at": stamp,
        "person_id": args.person_id,
        "container": container,
        "image": image,
        "profile": "v2" if args.v2 else "v1",
        "ros_distro": distro,
        "container_env": {name: environment.get(name, "") for name in PASSTHROUGH},
        "deidentify": deidentify,
        "context_echo": echo.stdout,
        "report": report,
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    counts = report.get("engagement_counts", {})
    print(f"[outside-consumer] tracked_ids={report.get('tracked_ids')} "
          f"anonymous={report.get('anonymous')} "
          f"engagement={sum(int(v) for v in counts.values()) if counts else 0} "
          f"expressions={report.get('expressions')}")
    if args.v2:
        print(f"[outside-consumer] joint_states={report.get('joint_state_count')} "
              f"@ {report.get('joint_state_hz')} Hz "
              f"joints={len(report.get('joint_names') or [])} "
              f"tf={report.get('tf_frames')}")
    print(f"[outside-consumer] report: {archive}")
    if failures:
        for failure in failures:
            print(f"[outside-consumer]   - {failure}")
        print("[outside-consumer] FAIL")
        return 1
    print("[outside-consumer] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
