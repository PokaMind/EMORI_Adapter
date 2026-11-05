"""v2 graph: the robot is a node in it, and the skill facade reaches it without a wire.

    reachy_mini_ros2 (Pollen's backend in-process, arbiter, REST shim on :8000)
        ^ /reachy_mini/{goto,play_recorded_move,set_target,wake_up}
        |
    emori_skill_facade (transport `ros_native`)
        ^ /skill/{set_expression,replay_motion,look_at}
        |
    hri_engagement_behaviors <- hri_context_analyzer <- emori_adapter (publisher)
                             \\-- fiware_bridge --NGSI-LD--> Orion-LD

`slice2.launch.py` is v1 and is untouched: there the robot was the Reachy daemon and the
only writer to it was the host application, which the facade reached over HTTP. Here the
robot node's arbiter is the single writer, the host application (if one is running) is
just another of its clients through the REST shim on :8000, and the facade talks to the
arbiter natively. The five EMORI nodes are exactly the ones `slice2.launch.py` starts -
the graph above the facade does not know which transport is underneath it, and that is
the whole point of the plug point.

Args (env-var defaults so docker compose configures without overriding CMD):
    backend          mujoco | mockup | robot        (env REACHY_BACKEND)
    serialport       "" = find it                   (env REACHY_SERIALPORT)
    wireless_version the CM4 wireless robot          (env REACHY_WIRELESS)
    rest_port        the compatibility shim's port   (env REACHY_REST_PORT, default 8000)
    with_camera_v4l2 publish the v4l2 camera too     (env WITH_CAMERA_V4L2)
    expression_owner which client owns the expression lane on the robot node
                                                     (env EMORI_ROS_MOTION_OWNER)
    source / ws_url / person_id / skill_url / with_fiware  as in `slice2.launch.py`
    skill_transport  which wire the facade speaks; `ros_native` in v2
                                                     (env EMORI_SKILL_TRANSPORT)

Two knobs decide who moves the robot and they are read on both sides:
`EMORI_ROS_MOTION_OWNER` gates the facade (only `ros` sends) and names the arbiter's
expression owner, so a request that the facade would send is also one the robot node
will accept. Setting it to `hostapp` leaves the graph identical and inert.
"""

from __future__ import annotations

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# What the robot node's launch arguments mean, spelled the way ROS 2 parameters need
# them: compose files write `REACHY_WIRELESS=1` and a bool parameter will not take "1".
_TRUE = ("1", "true", "yes", "on")


def _flag(name: str, default: bool) -> str:
    """An env var read as a launch boolean, tolerant of 1/0 as well as true/false."""

    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return "true" if default else "false"
    return "true" if str(raw).strip().lower() in _TRUE else "false"


def generate_launch_description() -> LaunchDescription:
    params = os.path.join(get_package_share_directory("emori_adapter_bringup"),
                          "config", "topics.yaml")
    source = LaunchConfiguration("source")
    ws_url = LaunchConfiguration("ws_url")
    person_id = LaunchConfiguration("person_id")
    skill_url = LaunchConfiguration("skill_url")
    skill_transport = LaunchConfiguration("skill_transport")
    with_fiware = LaunchConfiguration("with_fiware")
    backend = LaunchConfiguration("backend")
    serialport = LaunchConfiguration("serialport")
    wireless_version = LaunchConfiguration("wireless_version")
    rest_port = LaunchConfiguration("rest_port")
    with_camera_v4l2 = LaunchConfiguration("with_camera_v4l2")
    expression_owner = LaunchConfiguration("expression_owner")

    common = [params, {"person_id": person_id}]

    return LaunchDescription([
        # --- the robot ---------------------------------------------------------
        DeclareLaunchArgument("backend", default_value=os.environ.get(
            "REACHY_BACKEND", "mujoco")),
        DeclareLaunchArgument("serialport", default_value=os.environ.get(
            "REACHY_SERIALPORT", "")),
        DeclareLaunchArgument("wireless_version",
                              default_value=_flag("REACHY_WIRELESS", False)),
        DeclareLaunchArgument("rest_port", default_value=os.environ.get(
            "REACHY_REST_PORT", "8000")),
        DeclareLaunchArgument("with_camera_v4l2",
                              default_value=_flag("WITH_CAMERA_V4L2", False)),
        # The robot node's arbiter refuses an expression request from any client that is
        # not this one. Same env var the facade reads, so "who may move the robot" has
        # one answer on both sides of the action.
        DeclareLaunchArgument("expression_owner", default_value=os.environ.get(
            "EMORI_ROS_MOTION_OWNER", "hostapp")),

        # --- the EMORI graph ---------------------------------------------------
        DeclareLaunchArgument("source", default_value=os.environ.get("EMORI_SOURCE", "mock")),
        DeclareLaunchArgument("ws_url", default_value=os.environ.get(
            "EMORI_WS_URL", "ws://host.docker.internal:8080/ws/relay")),
        DeclareLaunchArgument("person_id", default_value="learner_01"),
        DeclareLaunchArgument("skill_url", default_value=os.environ.get(
            "EMORI_SKILL_URL", "http://host.docker.internal:8080/api/skill")),
        # v2's default is the native transport: the robot is in this graph, so the one
        # component allowed to turn a decision into motion no longer needs a wire at all.
        DeclareLaunchArgument("skill_transport", default_value=os.environ.get(
            "EMORI_SKILL_TRANSPORT", "ros_native")),
        DeclareLaunchArgument("with_fiware", default_value=os.environ.get(
            "EMORI_WITH_FIWARE", "true")),

        # Pollen's backend, the motion arbiter, the state publishers and the REST
        # compatibility shim, all in one node. Included rather than copied so the robot
        # package stays the single source of truth for how the robot comes up - and so
        # `ros2 launch reachy_mini_ros2 robot.launch.py` alone still brings up a robot
        # with no EMORI graph attached, which is how it gets debugged.
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(
                get_package_share_directory("reachy_mini_ros2"), "launch",
                "robot.launch.py")),
            launch_arguments={
                "backend": backend,
                "serialport": serialport,
                "wireless_version": wireless_version,
                "rest_port": rest_port,
                "with_camera_v4l2": with_camera_v4l2,
                "expression_owner": expression_owner,
            }.items()),

        Node(package="emori_adapter_ros", executable="publisher_node",
             name="emori_adapter", output="screen",
             parameters=common + [{"source": source, "ws_url": ws_url}]),

        Node(package="hri_context_analyzer", executable="context_analyzer",
             name="hri_context_analyzer", output="screen", parameters=common),

        Node(package="hri_engagement_behaviors", executable="behavior_node",
             name="hri_engagement_behaviors", output="screen", parameters=common),

        # The one component allowed to turn a skill request into robot motion. In v2 its
        # transport is `ros_native` (emori_skill_native), which sends GoTo goals to the
        # robot node above instead of POSTing a host application - the arbiter, not a
        # queue in another process, is what keeps the single-writer rule.
        Node(package="emori_skill_facade", executable="skill_facade",
             name="skill_facade", output="screen",
             parameters=common + [{"skill_url": skill_url,
                                   "transport": skill_transport}]),

        Node(package="fiware_bridge", executable="bridge_node",
             name="fiware_bridge", output="screen", parameters=common,
             condition=IfCondition(with_fiware)),
    ])
