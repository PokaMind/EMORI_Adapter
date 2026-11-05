"""The robot node's second node keeps its own name on a real rclpy.

A launch file names a node with a remap on the command line, and that reaches every node the
process creates. Run in a child interpreter, because the arguments belong to a context and a
real launch passes them at ``rclpy.init``.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import ros_support

_PROBE = """
import sys
sys.path.insert(0, sys.argv[1])  # the checkout's: an image may hold an older one
import rclpy
from rclpy.node import Node
rclpy.init(args=["--ros-args", "-r", "__node:=reachy_mini"])
from reachy_mini_ros2.ros_surface import load_ros_bindings
named_by_the_remap = Node("robot_node")
link = load_ros_bindings().create_node("reachy_mini_link")
print(named_by_the_remap.get_name(), link.get_name())
print(sorted(name for name, _ in link.get_service_names_and_types_by_node(
    link.get_name(), link.get_namespace())))
rclpy.shutdown()
"""


def test_the_launch_files_remap_does_not_rename_the_link_node(rclpy: Any) -> None:
    package = ros_support.ROOT / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
    done = subprocess.run([sys.executable, "-c", _PROBE, str(package)], capture_output=True,
                          text=True, timeout=120, check=False, cwd=ros_support.ROOT)
    assert done.returncode == 0, done.stderr
    names, services = done.stdout.splitlines()[:2]
    assert names == "reachy_mini reachy_mini_link"
    assert "parameter" not in services
