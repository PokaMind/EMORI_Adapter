#!/usr/bin/env bash
set -e
# Source ROS 2 + the built EMORI workspace, then run the given command.
source /opt/ros/humble/setup.bash
source /emori/ros2_ws/install/setup.bash
exec "$@"
