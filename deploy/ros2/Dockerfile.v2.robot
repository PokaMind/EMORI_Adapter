# EMORI ROS 2 Jazzy image for the ROBOT: the native robot node + the EMORI graph, slim.
#
# The sim image (Dockerfile.v2) carries MuJoCo, Mesa/EGL and the dev headers it needs to
# build them; none of that runs on the CM4, and the robot's SD card has ~2.5 GB free. This
# file keeps only what the real backend needs: Pollen's driver (serial, kinematics, recorded
# moves), the ROS4HRI messages, GStreamer for the node's own camera capture, and our
# workspace (no v4l2_camera, no audio_common; see below). The dev headers PyGObject needs at
# build time are purged in the same layer that used them.
#
#   docker buildx build --platform linux/arm64 -t emori/ros2-jazzy-v2:robot \
#       -f deploy/ros2/Dockerfile.v2.robot --load .
#
# The one-click deploy (scripts/deploy_to_robot.py) builds it from the checkout it deploys
# and ships it the same way when a robot has none.
# Ship it through a local registry over an SSH reverse tunnel (deploy/robot/ros2_native/
# README.md, Route A), not `docker save | docker load`: a load needs about twice the image
# size free on the robot's card.
ARG TARGETPLATFORM
FROM --platform=$TARGETPLATFORM ros:jazzy-ros-base
ARG TARGETPLATFORM
RUN echo "building for TARGETPLATFORM=${TARGETPLATFORM:-(unset)} uname -m=$(uname -m)"

ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-c"]

# Runtime apt: the ROS packages the graph needs, GStreamer introspection for Pollen's
# media imports, and the shared libraries PyGObject links against.
#
# The two gstreamer1.0-plugins-* ARE the camera: v4l2src, videoconvert, videoscale and
# appsink, which is what `reachy_mini_ros2.robot_node` captures the camera with. 63 MB,
# measured. The gir1.2-* packages are only typelibs -- PyGObject imports happily without
# a single plugin installed and then finds no elements at runtime, which is a camera that
# fails at the pipeline instead of at the build.
#
# NOT HERE, and measured: ros-jazzy-v4l2-camera and
# ros-jazzy-audio-common cost 640 MB between them. v4l2_camera depends on
# cv_bridge, cv_bridge depends on libopencv-dev, and that drags VTK, GDAL, LLVM and
# Mesa onto a robot that renders nothing. `docker load` needs roughly TWICE an
# image's size while it unpacks (layer tar in /var/lib/docker/tmp + the unpacked
# tree), and the robot's card has 2.8 GB free -- the 2.28 GB image with both of these
# in it ran the card out of space mid-load and rolled back. So the robot profile runs
# with WITH_CAMERA_V4L2=0 and WITH_AUDIO=0; the SIM image (Dockerfile.v2) keeps
# both. The camera came back the other way instead: the node captures the device
# itself through GStreamer, 63 MB against 640, so WITH_CAMERA_V4L2=0 on the robot means
# "this node publishes the camera" and not "there is no camera". audio_capturer_node
# was off by default and untested on hardware anyway.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        python3-colcon-common-extensions \
        ros-jazzy-hri-msgs \
        ros-jazzy-diagnostic-msgs \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        python3-gi \
        libcairo2 \
        libgirepository-1.0-1 \
    && rm -rf /var/lib/apt/lists/*

# Pollen's driver. reachy-mini pins pip>=26.1, numpy>=2.2.5 and PyGObject<=3.46.0, all of
# which Ubuntu 24.04 ships as apt packages that pip cannot uninstall, so they are
# pre-installed into /usr/local with --ignore-installed (same as Dockerfile.v2). The dev
# headers exist only inside this layer: installed, used to compile PyGObject, purged.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libcairo2-dev libgirepository1.0-dev pkg-config \
    && pip3 install --break-system-packages --no-cache-dir --ignore-installed \
        "pip>=26.1" "numpy>=2.2.5" "PyGObject>=3.42.2,<=3.46.0" \
    && pip3 install --break-system-packages --no-cache-dir "reachy-mini==1.10.0" \
    && apt-get purge -y build-essential libcairo2-dev libgirepository1.0-dev pkg-config \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Pollen's emotion library, cached in the image so the robot needs no network to play one.
ENV HF_HOME=/opt/hf
RUN python3 -c "from reachy_mini.motion.recorded_move import RecordedMoves; \
print('emotions library cached:', \
len(RecordedMoves('pollen-robotics/reachy-mini-emotions-library').list_moves()), 'moves')"

WORKDIR /emori
COPY pyproject.toml README.md ./
COPY src ./src
COPY configs/behavior.yaml ./configs/behavior.yaml
RUN pip3 install --break-system-packages --no-cache-dir ".[pokamind]"

COPY ros2_ws ./ros2_ws
# ONE compile at a time, deliberately. Building this image for arm64 on an x86 box runs
# every gcc under qemu-user, and parallel cc1plus processes there die of
# `internal compiler error: Segmentation fault` partway through the fastrtps type
# support (measured: both msg packages, ~15 files in). Sequential is slower
# on native hardware too, but this image is built rarely and shipped, and a build that
# finishes beats one that is fast on a machine nobody has.
# emori_msgs and reachy_mini_msgs are rosidl interface packages: colcon generates C,
# C++ and Python for every .msg and compiles it, so this step needs a C++ compiler --
# and the pip layer above purged the one it borrowed. Same discipline here: brought
# in, used, and gone inside the layer that used it, so the image carries the generated
# interfaces and no toolchain. (Purging in a LATER layer would not help: an image is
# the sum of its layers, and a file deleted downstream is still shipped upstream.)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && source /opt/ros/jazzy/setup.bash \
    && cd ros2_ws \
    && MAKEFLAGS=-j1 colcon build --executor sequential --parallel-workers 1 \
        --event-handlers console_direct+ \
    && rm -rf build log \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# The YuNet face detector, baked. reachy_mini's own FaceDetector downloads this from
# HuggingFace in its CONSTRUCTOR; this image runs HF_HUB_OFFLINE=1 with the repo not in
# the cache, so the vendored detector takes a PATH and this is the path. 229,738 bytes.
#
# AFTER the colcon layer on purpose. A COPY beneath it would sit under the 45-minute
# arm64 build layer, so swapping a 229 KB model would re-ship the whole build; here the
# pulled delta for a model-only change is 229 KB.
#
# No new apt or pip: onnxruntime 1.27, numpy 2.5.3 and scipy 1.18 are already in the
# image as reachy-mini==1.10.0 dependencies. The image grows by 229 KB, plus the
# model's MIT licence text, which travels with it.
COPY deploy/ros2/models/face_detection_yunet_2026may.onnx /opt/emori/models/
COPY deploy/ros2/models/LICENSE.face_detection_yunet.txt /opt/emori/models/
# Verified in the build, not at runtime: a truncated or substituted model would otherwise
# surface on the robot as a detector that loads and finds nobody.
RUN test "$(sha256sum /opt/emori/models/face_detection_yunet_2026may.onnx | cut -d' ' -f1)" \
      = "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0" \
    && echo "yunet baked: 229738 bytes, sha256 verified"

RUN printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -e' \
    'source /opt/ros/jazzy/setup.bash' \
    'source /emori/ros2_ws/install/setup.bash' \
    'exec "$@"' > /entrypoint.sh \
    && chmod +x /entrypoint.sh

ENV HF_HUB_OFFLINE=1 \
    HF_HOME=/opt/hf
ENTRYPOINT ["/entrypoint.sh"]
CMD ["ros2", "launch", "emori_adapter_bringup", "v2.launch.py"]

# Which sources this image was built from: the one-click deploy computes the same hash for
# the checkout it deploys (scripts/deploy_to_robot.py, v2_source_hash) and says when a
# robot's image is older than its code. Last, so it never invalidates a cached layer.
ARG EMORI_V2_SOURCE=unknown
LABEL org.pokamind.emori.v2-source=$EMORI_V2_SOURCE
