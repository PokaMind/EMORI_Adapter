"""Read the robot's motion from its ROS topics and print one JSON line per control tick.

This file is piped into the running motor container (``docker exec -i``) by
``scripts/motion_sync_run.py``, so nothing has to be installed there and the container is
left as it was found. It only subscribes: no parameter service, no ``/rosout``, no ``ros2``
command and so no ros2 daemon. ``ROS_LOG_DIR`` points at a temporary directory, removed on
exit, because rclpy writes a log file even for a node this quiet.

Every record is stamped with ``time.monotonic()`` in the subscription callback. Docker adds
no time namespace, so that is the same clock the host and the hostapp read.

``/reachy_mini/head_pose`` and ``/joint_states`` both come from the 50 Hz control loop, so
one record is printed once both have arrived, carrying the head's rotation from the pose
message and the antennas and body yaw from the joint message. If one of them stops, the
other still prints, because a stopped topic is not a stopped robot.

The topics are read best-effort, so a slow reader here can only lose samples and can never
push back on the node that publishes them. It exits on the first of: stdin closing (the
host went away), stdout closing, or ``--max-s``.

Standard library and rclpy only: the container runs its own Python, not the host's.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time

JOINT_TOPIC = "/joint_states"
POSE_TOPIC = "/reachy_mini/head_pose"
STATUS_TOPIC = "/reachy_mini/motion_status"
# Names ``backend_host.JOINT_NAMES`` publishes on the joint topic.
JOINT_BODY_YAW = "body_yaw"
JOINT_LEFT_ANTENNA = "left_antenna"
JOINT_RIGHT_ANTENNA = "right_antenna"
# How the host finds this process again if ``docker exec`` loses it.
MARKER = "motion_sync_joints_probe"


def rpy_from_quaternion(x: float, y: float, z: float, w: float) -> list[float]:
    """``[roll, pitch, yaw]`` in radians, in the convention ``/api/state/full`` reports."""

    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    sin_pitch = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sin_pitch)
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return [roll, pitch, yaw]


def joints_from_message(names, positions) -> tuple[list[float], float] | None:
    """``([left, right], body yaw)`` off a joint message, or None if a name is missing."""

    by_name = dict(zip(names, positions))
    try:
        return ([float(by_name[JOINT_LEFT_ANTENNA]), float(by_name[JOINT_RIGHT_ANTENNA])],
                float(by_name[JOINT_BODY_YAW]))
    except KeyError:
        return None


class Merger:
    """Pair a pose and a joint message into one record per tick.

    ``on_pose`` and ``on_joints`` return the record to print, or None. A second message of
    the same kind before the other one arrives means that other topic has gone quiet, so
    the newest values go out anyway.
    """

    def __init__(self) -> None:
        self._rpy: list[float] | None = None
        self._ant: list[float] | None = None
        self._yaw: float | None = None
        self._pose_fresh = False
        self._joints_fresh = False

    def _record(self, stamp: float) -> dict | None:
        self._pose_fresh = self._joints_fresh = False
        if self._rpy is None or self._ant is None:
            return None
        return {"t": round(stamp, 4), "rpy": [round(v, 5) for v in self._rpy],
                "ant": [round(v, 5) for v in self._ant], "yaw": round(float(self._yaw), 5)}

    def on_pose(self, stamp: float, rpy: list[float]) -> dict | None:
        out = self._record(stamp) if self._pose_fresh else None
        self._rpy = rpy
        self._pose_fresh = True
        return self._record(stamp) if self._joints_fresh else out

    def on_joints(self, stamp: float, ant: list[float], yaw: float) -> dict | None:
        out = self._record(stamp) if self._joints_fresh else None
        self._ant, self._yaw = ant, yaw
        self._joints_fresh = True
        return self._record(stamp) if self._pose_fresh else out


def _status_record(stamp: float, message) -> dict:
    return {"t": round(stamp, 4), "status": {
        "busy": bool(message.busy), "queue_depth": int(message.queue_depth),
        "running_move_id": str(message.running_move_id)}}


def run(*, rclpy, qos, pose_type, joint_type, status_type=None, max_s: float = 3600.0,
        clock=time.monotonic, out=None, stop: threading.Event | None = None) -> None:
    """Subscribe and print until told to stop. Every ROS module comes in as an argument."""

    stream = sys.stdout if out is None else out
    stop = threading.Event() if stop is None else stop
    merger = Merger()

    def emit(record: dict | None) -> None:
        if record is None:
            return
        try:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            stream.flush()
        except (BrokenPipeError, ValueError, OSError):
            stop.set()

    def on_pose(message) -> None:
        stamp = clock()
        q = message.pose.orientation
        emit(merger.on_pose(stamp, rpy_from_quaternion(q.x, q.y, q.z, q.w)))

    def on_joints(message) -> None:
        stamp = clock()
        joints = joints_from_message(message.name, message.position)
        if joints is not None:
            emit(merger.on_joints(stamp, joints[0], joints[1]))

    def on_status(message) -> None:
        emit(_status_record(clock(), message))

    rclpy.init(args=["--ros-args", "--disable-rosout-logs"])
    node = rclpy.create_node(MARKER, enable_rosout=False, start_parameter_services=False)
    try:
        node.create_subscription(pose_type, POSE_TOPIC, on_pose, qos)
        node.create_subscription(joint_type, JOINT_TOPIC, on_joints, qos)
        if status_type is not None:
            node.create_subscription(status_type, STATUS_TOPIC, on_status, qos)
        deadline = clock() + float(max_s)
        while not stop.is_set() and clock() < deadline:
            rclpy.spin_once(node, timeout_sec=0.2)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _watch_stdin(stop: threading.Event) -> None:
    """Stop when the host closes our stdin, which is how it says the run is over."""

    try:
        while sys.stdin.buffer.read(1):
            pass
    except (OSError, ValueError):
        pass
    stop.set()


def _max_s(argv: list[str]) -> float:
    try:
        return float(argv[argv.index("--max-s") + 1])
    except (ValueError, IndexError):
        return 3600.0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    log_dir = tempfile.mkdtemp(prefix=MARKER + "-log-")
    os.environ["ROS_LOG_DIR"] = log_dir
    stop = threading.Event()
    threading.Thread(target=_watch_stdin, args=(stop,), daemon=True).start()
    try:
        import rclpy
        from geometry_msgs.msg import PoseStamped
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import JointState

        try:
            from reachy_mini_msgs.msg import MotionStatus
        except ImportError:
            MotionStatus = None  # the queue depth is an extra, the motion is the point

        run(rclpy=rclpy, qos=qos_profile_sensor_data, pose_type=PoseStamped,
            joint_type=JointState, status_type=MotionStatus, max_s=_max_s(args), stop=stop)
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
