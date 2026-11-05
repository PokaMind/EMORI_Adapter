"""The hub's robot client and link over rclpy's own transport, against a stand-in robot node."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import ros_support

SCENARIO = ros_support.ROOT / "tests_ros" / "link_scenario.py"
CHILD_S = 120


def test_the_client_and_link_work_against_a_robot_node(rclpy: Any, reachy_srv: Any) -> None:
    ros_support.need_module("emori_adapter.models")
    done = subprocess.run([sys.executable, str(SCENARIO)], capture_output=True, text=True,
                          timeout=CHILD_S, env=ros_support.child_environment(), check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "link scenario ok" in done.stdout
