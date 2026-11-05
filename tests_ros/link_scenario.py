"""The hub's robot link against a stand-in robot node, in one process.

    python tests_ros/link_scenario.py

Run by ``test_robot_link.py`` as a child, because the link builds its node on rclpy's default
context and that is a property of a process. The stand-in answers every service the link
reads with the real message types and publishes the heartbeat, so what is exercised is the
link and the client over rclpy's own transport and nothing in between. It prints
``link scenario ok`` and exits 0, or says what differed and exits 1.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"))
sys.path.insert(0, str(ROOT / "src"))

import rclpy  # noqa: E402
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.signals import SignalHandlerOptions  # noqa: E402
from std_msgs.msg import Header  # noqa: E402
from std_srvs.srv import SetBool  # noqa: E402

from reachy_mini_msgs.srv import (  # noqa: E402
    GetFullState,
    GetRobotStatus,
    PlaySound,
    SetMotorMode,
    StopMove,
    SubmitMove,
)

from emori_adapter.models import MotionIntent  # noqa: E402
from emori_hub import robot_link  # noqa: E402

FAILED: list[str] = []


def check(condition: bool, what: str) -> None:
    if not condition:
        FAILED.append(what)
        print(f"FAIL: {what}", flush=True)


def wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class StandInRobot:
    """Every service the link names, answering as a healthy robot does."""

    def __init__(self) -> None:
        self.node = Node("standin_robot")
        group = ReentrantCallbackGroup()
        self.moves: list[SubmitMove.Request] = []
        self.gaze: list[bool] = []
        self.beating = True
        node = self.node
        node.create_service(SubmitMove, "/reachy_mini/submit_move", self._submit,
                            callback_group=group)
        node.create_service(GetFullState, "/reachy_mini/get_full_state", self._full_state,
                            callback_group=group)
        node.create_service(GetRobotStatus, "/reachy_mini/get_status", self._status,
                            callback_group=group)
        node.create_service(SetMotorMode, "/reachy_mini/set_motor_mode", self._mode,
                            callback_group=group)
        node.create_service(SetBool, "/reachy_mini/set_gaze", self._set_gaze,
                            callback_group=group)
        node.create_service(SetBool, "/reachy_mini/set_head_reserved", self._ok,
                            callback_group=group)
        node.create_service(SetBool, "/reachy_mini/set_wobbling", self._ok,
                            callback_group=group)
        node.create_service(StopMove, "/reachy_mini/stop", self._stop, callback_group=group)
        node.create_service(PlaySound, "/reachy_mini/play_sound", self._sound,
                            callback_group=group)
        self.beat = node.create_publisher(Header, robot_link.HEARTBEAT_TOPIC, 1)
        node.create_timer(0.5, self._heartbeat, callback_group=group)
        self.executor = MultiThreadedExecutor(num_threads=4)
        self.executor.add_node(node)
        self.thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self) -> None:
        while rclpy.ok():
            try:
                self.executor.spin_once(timeout_sec=0.05)
            except Exception:  # noqa: BLE001 - the executor outlives a callback's fault
                time.sleep(0.05)

    def start(self) -> None:
        self.thread.start()

    def _heartbeat(self) -> None:
        if self.beating:
            self.beat.publish(Header())

    def _submit(self, request, response):
        self.moves.append(request)
        response.accepted = True
        response.move_id = "move-1" if request.kind == "play_recorded" else ""
        return response

    def _full_state(self, _request, response):
        response.ok = True
        response.state.control_mode = "enabled"
        response.state.head_joints = [0.0] * 7
        response.state.antennas_position = [0.1, -0.1]
        return response

    def _status(self, _request, response):
        response.state = "running"
        response.ready = True
        response.backend = "reachy_mini_ros2"
        return response

    def _mode(self, request, response):
        response.ok = True
        response.mode = request.mode
        return response

    def _set_gaze(self, request, response):
        self.gaze.append(bool(request.data))
        response.success = True
        return response

    def _ok(self, _request, response):
        response.success = True
        return response

    def _stop(self, _request, response):
        response.stopped = 1
        return response

    def _sound(self, _request, response):
        response.ok = True
        return response

    def stop(self) -> None:
        self.executor.shutdown()
        self.node.destroy_node()


def main() -> int:
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    robot = StandInRobot()
    robot.start()
    node = robot_link.start_robot_link()
    client = robot_link.RosRobotClient(node.link, timeout=5.0)
    try:
        check(wait_for(client.health), "health() never became true against a healthy robot")
        check(node.link.is_up(), "the link is not up")
        check(node.link.first_unmatched() == "", "a required service is unmatched")

        state = client.read_state()
        check(state is not None and state["control_mode"] == "enabled",
              f"read_state returned {state!r}")
        check(state is not None and state["antennas_position"] == [0.1, -0.1],
              "the antenna positions were not carried")

        check(client.set_gaze(True) is True, "the gaze gate was not acknowledged")
        check(robot.gaze == [True], f"the robot heard {robot.gaze!r}")

        client.apply(MotionIntent(kind="antennas", antennas=(0.3, -0.1), duration=0.4))
        check(len(robot.moves) == 1, f"the robot got {len(robot.moves)} moves")
        if robot.moves:
            move = robot.moves[0]
            check(move.kind == "goto" and move.lane == "interaction"
                  and move.client == "hostapp", f"identity: {move.kind} {move.lane} {move.client}")
            check(list(move.antennas) == [-0.1, 0.3],
                  f"antennas on the wire: {list(move.antennas)!r}")
            check(abs(move.duration - 0.4) < 1e-9, f"duration {move.duration!r}")

        check(client.play_recorded_move("happy1") == "move-1", "a recorded move got no id")

        # A robot that stops speaking is a link that is down within the heartbeat window.
        robot.beating = False
        check(wait_for(lambda: not client.health(), timeout=10.0),
              "health() stayed true after the heartbeat stopped")
        started = time.monotonic()
        client.apply(MotionIntent(kind="antennas", antennas=(0.0, 0.0), duration=0.2))
        check(time.monotonic() - started < 1.0, "a move on a dead link blocked the caller")
        check(client.read_state() is None, "read_state on a dead link was not None")
    finally:
        client.close()
        node.stop()
        robot.stop()
        rclpy.try_shutdown()
    if FAILED:
        return 1
    print("link scenario ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
