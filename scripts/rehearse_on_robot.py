"""Rehearse the motion-owner flip on the REAL robot, from the dev box.

The local harness (``rehearse_motion_owner.py``) starts a simulator, a hostapp and a
container on this machine. On the robot all three already run as systemd units and a
container, so this script starts nothing: it replays a recorded session into the robot's
hostapp over HTTP, samples the robot's head pose while it plays, then reads the same three
ledgers the local harness reads (hostapp counters over HTTP, the two or three
``/diagnostics`` statuses through ``ssh … docker exec``) and judges them with the SAME
assertion tables, imported by path from the local harness so the two can never drift.

Modes:

* ``--mode daemon``  v1 on the robot: Pollen's daemon owns the serial bus, the ROS graph
  reaches the robot through the hostapp's ``/api/skill`` (container ``emori-ros2``).
* ``--mode native``  v2 on the robot: the robot node owns the serial bus and answers the
  daemon dialect itself (container ``emori-ros2-v2``); ``/api/daemon/status`` must carry
  ``backend: reachy_mini_ros2`` or the run refuses to start.

Nothing here changes the robot's configuration. Flipping ``EMORI_ROS_MOTION_OWNER`` and
restarting units is done by hand, on purpose, before the run.

    python scripts/rehearse_on_robot.py --mode daemon --owner ros
    python scripts/rehearse_on_robot.py --mode native --owner ros
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]
_HARNESS = ROOT / "scripts" / "rehearse_motion_owner.py"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "rehearsal_session.jsonl"
POSE_SAMPLE_HZ = 4.0
TAIL_S = 8.0


def _load_harness():
    spec = importlib.util.spec_from_file_location("emori_rehearse_harness", _HARNESS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ssh(user_host: str, command: str, *, timeout: float = 60.0) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", user_host, command],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def docker_exec(user_host: str, container: str, command: str, *, timeout: float = 60.0) -> str:
    # The entrypoint sources ROS + the overlay; `docker exec` skips ENTRYPOINT, so call it.
    return ssh(user_host, f"docker exec {container} /entrypoint.sh bash -lc {json.dumps(command)}",
               timeout=timeout)


def hz_of(text: str) -> Optional[float]:
    match = re.search(r"average rate:\s*([0-9.]+)", text)
    return float(match.group(1)) if match else None


class PoseSampler(threading.Thread):
    def __init__(self, url: str) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.samples: list[Any] = []
        self._stopping = threading.Event()

    def run(self) -> None:
        with httpx.Client(timeout=2.0) as client:
            while not self._stopping.is_set():
                try:
                    self.samples.append(client.get(self.url).json())
                except Exception:  # noqa: BLE001 - a missed sample is a missed sample
                    pass
                self._stopping.wait(1.0 / POSE_SAMPLE_HZ)

    def stop(self) -> None:
        self._stopping.set()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="reachy-mini.local")
    ap.add_argument("--ssh-user", default="pollen")
    ap.add_argument("--hostapp-port", type=int, default=8090)
    ap.add_argument("--daemon-port", type=int, default=8000)
    ap.add_argument("--mode", choices=("daemon", "native"), required=True)
    ap.add_argument("--owner", choices=("ros", "hostapp", "none"), required=True)
    ap.add_argument("--container", default=None,
                    help="ROS container name (default: emori-ros2 / emori-ros2-v2 by mode)")
    ap.add_argument("--camera", choices=("expect", "absent"), default="expect",
                    help="absent: this robot profile publishes NO camera topic "
                         "(WITH_CAMERA_V4L2=0). The row is then an assertion that the "
                         "topic really is gone, not a skipped check.")
    ap.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    harness = _load_harness()
    user_host = f"{args.ssh_user}@{args.host}"
    container = args.container or ("emori-ros2-v2" if args.mode == "native" else "emori-ros2")
    hostapp = f"http://{args.host}:{args.hostapp_port}"
    daemon = f"http://{args.host}:{args.daemon_port}"
    started = datetime.now(timezone.utc)
    ledger: dict[str, Any] = {"host": args.host, "mode": args.mode, "owner": args.owner,
                              "container": container, "started_utc": started.isoformat()}

    # 1. preflight
    log("1. preflight")
    with httpx.Client(timeout=5.0) as client:
        health = client.get(f"{hostapp}/healthz").json()
        status = client.get(f"{hostapp}/api/status").json()
        dstatus = client.get(f"{daemon}/api/daemon/status").json()
    if status.get("session"):
        log(f"   a session is active on the robot ({status['session']}); refusing to replay over it")
        return 2
    backend = dstatus.get("backend", "pollen-daemon")
    log(f"   hostapp ok={health.get('status')} robot={health.get('robot')}; :8000 backend={backend} "
        f"state={dstatus.get('state')}")
    if args.mode == "native" and backend != "reachy_mini_ros2":
        log("   --mode native but :8000 is not the robot node's shim; refusing")
        return 2
    if args.mode == "daemon" and backend == "reachy_mini_ros2":
        log("   --mode daemon but :8000 is the robot node's shim; refusing")
        return 2
    env = ssh(user_host, f"docker exec {container} printenv EMORI_ROS_MOTION_OWNER EMORI_SKILL_TRANSPORT 2>&1")
    log(f"   container {container} env: {' '.join(env.split())}")
    ledger["container_env"] = env.strip()
    ledger["preflight"] = {"health": health, "daemon_status": dstatus}
    before = ssh(user_host, "top -bn1 | head -12 | tail -6")
    ledger["top_before"] = before

    # 2. sample the head while the session plays
    log("2. sampling the head pose at %.0f Hz" % POSE_SAMPLE_HZ)
    sampler = PoseSampler(f"{daemon}/api/state/full")
    sampler.start()
    stats: list[str] = []

    def stats_loop() -> None:
        for _ in range(6):
            stats.append(ssh(user_host, f"docker stats --no-stream --format '{{{{.CPUPerc}}}} {{{{.MemUsage}}}}' {container}; "
                                        "top -bn1 | grep -E 'reachy_mini|robot_node|emori-h' | awk '{print $9, $12}' | head -3").strip())
            time.sleep(8)

    stats_thread = threading.Thread(target=stats_loop, daemon=True)
    stats_thread.start()

    # 3. replay
    log("3. replaying %s into %s" % (Path(args.fixture).name, hostapp))
    replay = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "replay_session.py"), args.fixture,
         "--base-url", hostapp],
        capture_output=True, text=True, timeout=600, check=False,
    )
    ledger["replay_returncode"] = replay.returncode
    ledger["replay_tail"] = (replay.stdout or "")[-1500:]
    log(f"   replay exit={replay.returncode}")
    time.sleep(TAIL_S)
    sampler.stop()
    stats_thread.join(timeout=20)

    # 4. collect
    log("4. collecting counters and ledgers")
    with httpx.Client(timeout=5.0) as client:
        counters = client.get(f"{hostapp}/api/status").json().get("counters", {})
    diag_text = docker_exec(user_host, container, "timeout 14 ros2 topic echo --no-daemon /diagnostics", timeout=40)
    statuses = harness.parse_diagnostics(diag_text)
    facade = statuses.get("emori_skill_facade", {})
    behaviors = statuses.get("hri_engagement_behaviors", {})
    robot = statuses.get("reachy_mini", {})
    joint_hz = camera_hz = None
    if args.mode == "native":
        joint_hz = hz_of(docker_exec(user_host, container, "timeout 7 ros2 topic hz /joint_states", timeout=30))
        camera_hz = hz_of(docker_exec(user_host, container,
                                      "timeout 7 ros2 topic hz /reachy_mini/camera/image_raw",
                                      timeout=30))
    logs = ssh(user_host, f"docker logs --tail 300 {container} 2>&1")
    pose_changes = harness.count_pose_changes([harness.pose_of_state(s) for s in sampler.samples])
    ledger.update({
        "hostapp_counters": counters,
        "diagnostics": statuses,
        "pose_samples": len(sampler.samples),
        "pose_changes": pose_changes,
        "joint_hz": joint_hz,
        "camera_hz": camera_hz,
        "stats": stats,
        "container_log": logs,
    })

    # 5. judge with the local harness's own tables
    if args.mode == "native":
        checks = harness.evaluate_native(args.owner, hostapp=counters, facade=facade,
                                         behaviors=behaviors, robot=robot,
                                         pose_changes=pose_changes, joint_hz=joint_hz,
                                         camera_hz=camera_hz)
    else:
        checks = harness.evaluate(args.owner, hostapp=counters, facade=facade,
                                  behaviors=behaviors, pose_changes=pose_changes)
    if args.mode == "native" and args.camera == "absent":
        # Not a skipped row, and not an absent TOPIC either: the node declares the camera
        # publishers before it discovers it has no camera to fill them, so
        # /reachy_mini/camera/image_raw exists on the robot and carries nothing. The honest
        # assertion for a robot image built without ros-jazzy-v4l2-camera (640 MB of
        # OpenCV/VTK the card cannot hold -- deploy/ros2/Dockerfile.v2.robot) is that no
        # FRAMES arrive. A rate here would mean this is not the image we think it is.
        checks = [harness.Check(
            "camera.no_frames_by_profile", camera_hz is None,
            "no frames (WITH_CAMERA_V4L2=0)" if camera_hz is None else
            "%.1f Hz of frames: this is not the robot image" % camera_hz)
            if c.name == "camera_hz" else c
            for c in checks]
        ledger["camera_profile"] = "absent"

    passed = all(c.ok for c in checks)
    print(f"\n=== on-robot rehearsal: mode={args.mode} EMORI_ROS_MOTION_OWNER={args.owner} ===")
    for c in checks:
        print(f"  {'PASS' if c.ok else 'FAIL'} {c.name:<44} {c.detail}")
    print(f"  --> {'PASS' if passed else 'FAIL'} ({sum(c.ok for c in checks)}/{len(checks)})")
    if joint_hz is not None or camera_hz is not None:
        print(f"  joint_states {joint_hz} Hz · camera {camera_hz} Hz")
    if stats:
        print("  container/process load samples:")
        for s in stats:
            print("    " + s.replace("\n", " | "))
    ledger["checks"] = [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]
    ledger["passed"] = passed

    out = Path(args.json) if args.json else (ROOT / "artifacts" / "rehearsals" /
                                             f"{started.strftime('%Y%m%dT%H%M%SZ')}-robot-{args.mode}-{args.owner}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ledger, indent=2, default=str), encoding="utf-8")
    log(f"ledger: {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
