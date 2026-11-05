"""Slice-2 graph:

    emori_adapter (publisher) --/humans/persons/<id>/{engagement_status,context,speech_cue}-->
        hri_context_analyzer  --/humans/persons/<id>/context/<key>-->
            hri_engagement_behaviors --/skill/{set_expression,replay_motion,look_at}-->
                emori_skill_facade   --HTTP POST /api/skill--> hostapp -> RobotPlayer -> robot
            fiware_bridge            --NGSI-LD--> Orion-LD (-> QuantumLeap -> Timescale)

No node holds a daemon URL: the hostapp is the single robot writer, and skill_facade is
the only component in this graph that turns a decision into motion.

Args (env-var defaults so docker compose can configure without overriding CMD):
    source           mock | pokamind (env EMORI_SOURCE; pokamind = the live robot hub relay)
    ws_url           hostapp relay URL (env EMORI_WS_URL)
    person_id        stable ROS topic namespace (default learner_01)
    skill_url        hostapp skill intake (env EMORI_SKILL_URL)
    skill_transport  which wire the facade speaks: hostapp_http | log
                      (env EMORI_SKILL_TRANSPORT; `log` moves nothing and records
                      every request, which is what the standalone smoke test uses)
    with_fiware      (default true; set false on the live stack if the hostapp
                      already historizes — or use FIWARE_BRIDGE_ENABLED=0)

Whether skill_facade actually sends is EMORI_ROS_MOTION_OWNER (hostapp | ros | none);
it is launched in every mode so the graph is identical and only the one knob differs.
"""

from __future__ import annotations

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    params = os.path.join(get_package_share_directory("emori_adapter_bringup"), "config", "topics.yaml")
    source = LaunchConfiguration("source")
    ws_url = LaunchConfiguration("ws_url")
    person_id = LaunchConfiguration("person_id")
    skill_url = LaunchConfiguration("skill_url")
    skill_transport = LaunchConfiguration("skill_transport")
    with_fiware = LaunchConfiguration("with_fiware")

    common = [params, {"person_id": person_id}]

    return LaunchDescription([
        DeclareLaunchArgument("source", default_value=os.environ.get("EMORI_SOURCE", "mock")),
        DeclareLaunchArgument("ws_url", default_value=os.environ.get(
            "EMORI_WS_URL", "ws://host.docker.internal:8080/ws/relay")),
        DeclareLaunchArgument("person_id", default_value="learner_01"),
        DeclareLaunchArgument("skill_url", default_value=os.environ.get(
            "EMORI_SKILL_URL", "http://host.docker.internal:8080/api/skill")),
        DeclareLaunchArgument("skill_transport", default_value=os.environ.get(
            "EMORI_SKILL_TRANSPORT", "hostapp_http")),
        DeclareLaunchArgument("with_fiware", default_value=os.environ.get("EMORI_WITH_FIWARE", "true")),

        Node(package="emori_adapter_ros", executable="publisher_node",
             name="emori_adapter", output="screen",
             parameters=common + [{"source": source, "ws_url": ws_url}]),

        Node(package="hri_context_analyzer", executable="context_analyzer",
             name="hri_context_analyzer", output="screen", parameters=common),

        Node(package="hri_engagement_behaviors", executable="behavior_node",
             name="hri_engagement_behaviors", output="screen", parameters=common),

        # The one component allowed to turn a skill request into robot motion, and it
        # does it through a transport - the hostapp's single-writer intake by default,
        # never the daemon.  Its own package so that boundary can be swapped for another
        # skill transport without touching the node that decides.
        Node(package="emori_skill_facade", executable="skill_facade",
             name="skill_facade", output="screen",
             parameters=common + [{"skill_url": skill_url,
                                   "transport": skill_transport}]),

        Node(package="fiware_bridge", executable="bridge_node",
             name="fiware_bridge", output="screen", parameters=common,
             condition=IfCondition(with_fiware)),
    ])
