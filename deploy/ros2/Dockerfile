# EMORI ROS 2 (Humble) image: ROS4HRI + the EMORI adapter/behaviour/skill/bridge nodes.
# Build context is the repo root:
#   docker build -t emori/ros2-humble:latest -f deploy/ros2/Dockerfile .
#
# FOR THE ROBOT (CM4, aarch64) the platform is not optional and must be stated:
#   docker buildx build --platform linux/arm64 -t emori/ros2-humble:latest \
#       -f deploy/ros2/Dockerfile --load .
# An accidental amd64 image installs and starts perfectly on a workstation and dies
# on the robot with a bare `exec format error`, so the arch is echoed into the build log
# below -- the cheapest place to notice is at build time, not in front of a person.
ARG TARGETPLATFORM
FROM --platform=$TARGETPLATFORM ros:humble-ros-base
ARG TARGETPLATFORM
RUN echo "building for TARGETPLATFORM=${TARGETPLATFORM:-(unset)} uname -m=$(uname -m)"

ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-c"]

# ROS4HRI message package (required) + std_srvs; pyhri/libhri are optional
# (the behaviour critical path uses plain rclpy, so don't fail the build if absent).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        python3-colcon-common-extensions \
        build-essential \
        ros-humble-rosidl-default-generators \
        ros-humble-hri-msgs \
        ros-humble-std-srvs \
    && (apt-get install -y --no-install-recommends ros-humble-pyhri ros-humble-libhri \
        || echo "WARN: ros-humble-pyhri/libhri not available via apt; pyhri demo disabled") \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /emori

# Install the pure-Python core (pydantic/httpx/etc.) into the container's Python 3.10.
# Ubuntu 22.04 ships setuptools <61 (no PEP 621 [project] support) and no PEP 660
# editable hook, so upgrade build tooling first and do a regular (non-editable) install.
COPY pyproject.toml README.md ./
COPY src ./src
# The behaviour tuning the ROS engagement node reads (EMORI_BEHAVIOR_CONFIG, set to
# /emori/configs/behavior.yaml by both compose files). Without it the file never reaches
# the image and the node silently falls back to its code defaults, which nobody notices
# while configs/behavior.yaml happens to equal them. This COPY is the image DEFAULT; a
# robot that tunes its own thresholds bind-mounts over it read-only.
COPY configs/behavior.yaml ./configs/behavior.yaml
# .[pokamind] brings the websockets client the LIVE source needs (source:=pokamind).
RUN pip3 install --no-cache-dir --upgrade pip "setuptools>=68" wheel \
    && pip3 install --no-cache-dir ".[pokamind]"

# Build the colcon workspace (emori_msgs + the four nodes).
COPY ros2_ws ./ros2_ws
RUN source /opt/ros/humble/setup.bash \
    && cd ros2_ws \
    && colcon build --event-handlers console_direct+

COPY deploy/ros2/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["ros2", "launch", "emori_adapter_bringup", "slice2.launch.py"]
