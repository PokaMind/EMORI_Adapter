"""Bring up the Reachy Mini as a native ROS 2 node, in the sim or on the robot.

    ros2 launch reachy_mini_ros2 robot.launch.py                       # MuJoCo, headless
    ros2 launch reachy_mini_ros2 robot.launch.py backend:=robot \\
        wireless_version:=true serialport:=/dev/ttyAMA3 \\
        with_camera_v4l2:=true

Every argument defaults from an environment variable so ``docker compose`` can configure
the graph without overriding the image's ``CMD``; ``emori_adapter_bringup``'s
``v2.launch.py`` includes this file and adds the EMORI nodes on top.

On the robot, Pollen's ``reachy-mini-daemon`` service must be stopped and disabled
first: it holds the serial port, and two things driving nine servos over one 1 Mbaud bus
is not a situation that degrades gracefully.  The REST shim inside the node then answers
on :8000 in the daemon's place, so the host application needs no change at all.
"""

from __future__ import annotations

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, Shutdown
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, NotSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default) or default


def generate_launch_description() -> LaunchDescription:
    backend = LaunchConfiguration("backend")
    headless = LaunchConfiguration("headless")
    serialport = LaunchConfiguration("serialport")
    wireless_version = LaunchConfiguration("wireless_version")
    expression_owner = LaunchConfiguration("expression_owner")
    rest_port = LaunchConfiguration("rest_port")
    rest_shim_enabled = LaunchConfiguration("rest_shim_enabled")
    with_camera_v4l2 = LaunchConfiguration("with_camera_v4l2")
    with_audio = LaunchConfiguration("with_audio")
    video_device = LaunchConfiguration("video_device")
    sleep_on_shutdown = LaunchConfiguration("sleep_on_shutdown")
    gaze_enabled = LaunchConfiguration("gaze_enabled")
    gaze_model = LaunchConfiguration("gaze_model")
    gaze_detect_hz = LaunchConfiguration("gaze_detect_hz")
    gaze_select_score = LaunchConfiguration("gaze_select_score")
    gaze_invert_yaw = LaunchConfiguration("gaze_invert_yaw")
    gaze_fx_scale = LaunchConfiguration("gaze_fx_scale")
    gaze_fy_scale = LaunchConfiguration("gaze_fy_scale")

    return LaunchDescription([
        DeclareLaunchArgument("backend", default_value=_env("REACHY_BACKEND", "mujoco"),
                              description="mujoco | mockup | robot"),
        DeclareLaunchArgument("headless", default_value="true",
                              description="MuJoCo only: no viewer window"),
        DeclareLaunchArgument("serialport", default_value=_env("REACHY_SERIALPORT", ""),
                              description="empty = find it (wireless: /dev/ttyAMA3)"),
        DeclareLaunchArgument("wireless_version",
                              default_value=_env("REACHY_WIRELESS", "false")),
        DeclareLaunchArgument("expression_owner",
                              default_value=_env("EMORI_ROS_MOTION_OWNER", "hostapp"),
                              description="hostapp | ros | none - who may use the "
                                          "expression lane"),
        DeclareLaunchArgument("rest_port", default_value=_env("REACHY_REST_PORT", "8000"),
                              description="the daemon-compatible REST shim's port"),
        # Off means nothing listens on the daemon's port and every robot call has to
        # arrive over ROS. The shim stays on by default while the host application still
        # speaks HTTP to :8000.
        DeclareLaunchArgument("rest_shim_enabled",
                              default_value=_env("REACHY_REST_SHIM_ENABLED", "true"),
                              description="serve the daemon-compatible REST shim"),
        # Off by default: the real camera is a v4l2 device that only exists on the
        # robot, and on an EMORI robot it is already shared through v4l2loopback
        # (/dev/video40). NOT several readers: v4l2loopback with exclusive_caps=Y
        # gives its buffers to ONE capture consumer, and on a kiosk robot that
        # consumer is Chromium.
        # A second one gets VIDIOC_REQBUFS: Device or resource busy.
        DeclareLaunchArgument("with_camera_v4l2", default_value="false"),
        DeclareLaunchArgument("video_device", default_value=_env("REACHY_VIDEO_DEVICE",
                                                                 "/dev/video40")),
        # Off by default and UNTESTED on hardware: audio_common's capturer has jazzy
        # binaries, but on the robot PipeWire already owns the microphone for EMORI and
        # two readers of one capture device is not a thing to switch on by default.
        DeclareLaunchArgument("with_audio", default_value="false"),
        # auto = park and de-torque on the REAL robot and nowhere else. A simulator has
        # nothing to drop; a robot whose process exits with the motors still holding a
        # pose snaps down whenever the torque eventually goes.
        DeclareLaunchArgument("sleep_on_shutdown",
                              default_value=_env("REACHY_SLEEP_ON_SHUTDOWN", "auto"),
                              description="auto (robot only) | true | false"),

        # --- face look-at ---------------------------------------------------------
        # One argument per knob a RIG would ever set. The rest of the `gaze.*`
        # parameters exist on the node with the same defaults and are reachable with
        # `--ros-args -p gaze.<name>:=...`; putting all twenty-five here would be a
        # launch file nobody can read.
        #
        # `gaze_enabled` is a CAPABILITY switch: false means the detector is never
        # built. Whether gaze is RUNNING is a separate, fail-closed question answered by
        # `POST /api/emori/gaze`, so an idle kiosk runs no face detector.
        DeclareLaunchArgument("gaze_enabled", default_value=_env("EMORI_GAZE", "true"),
                              description="build the face look-at component at all"),
        DeclareLaunchArgument(
            "gaze_model",
            default_value=_env("EMORI_GAZE_MODEL",
                               "/opt/emori/models/face_detection_yunet_2026may.onnx"),
            description="the YuNet weights, baked into the image and bind-mounted"),
        DeclareLaunchArgument("gaze_detect_hz", default_value=_env("EMORI_GAZE_DETECT_HZ", "5.0"),
                              description="detections per second; 20 max_misses at 5 Hz "
                                          "is the 4.0 s relax window"),
        DeclareLaunchArgument("gaze_select_score", default_value="0.75",
                              description="confidence to AIM at a face (the detector's "
                                          "own threshold is 0.6)"),
        DeclareLaunchArgument("gaze_invert_yaw", default_value="false",
                              description="only the robot can settle a mechanism sign"),
        DeclareLaunchArgument("gaze_fx_scale", default_value="1.0",
                              description="horizontal loop-gain trim"),
        DeclareLaunchArgument("gaze_fy_scale", default_value="1.0",
                              description="vertical loop-gain trim (SEPARATE from fx: "
                                          "one knob cannot fix an aspect error)"),

        # on_exit=Shutdown(): this node IS the robot. Without it, a robot_node that
        # dies (a control loop that faulted, an OOM kill, the non-zero exit the node
        # itself takes when its backend dies) leaves the rest of the launch running --
        # the analyzer, the behaviours, the facade, all still publishing, all still
        # deciding what the robot should do, with nothing left to do it. The container
        # stays "up", the unit stays "active", and the only symptom is a robot that
        # stopped moving. Taking the whole launch down turns that into a restart.
        # `v2.launch.py` includes this file, so it inherits this without repeating it.
        Node(package="reachy_mini_ros2", executable="robot_node", name="reachy_mini",
             output="screen", on_exit=Shutdown(),
             # How long launch waits after SIGINT before escalating to SIGTERM, and then
             # to SIGKILL. The defaults are 5 s and 5 s, and the node needs more than
             # that on the way out: `backend.reset_to_sleep()` alone is bounded at 8 s
             # (lift to init, sleep trajectory, de-torque) before the shim, the arbiter
             # loop and the backend thread are joined. Escalating at 5 s would SIGKILL
             # the robot mid-trajectory and leave nine servos torqued at an arbitrary
             # pose -- which is the exact failure the park exists to prevent, arriving
             # from the layer above instead.
             sigterm_timeout="20", sigkill_timeout="25",
             parameters=[{
                 "backend": backend,
                 "headless": ParameterValue(headless, value_type=bool),
                 "serialport": serialport,
                 "wireless_version": ParameterValue(wireless_version, value_type=bool),
                 "expression_owner": expression_owner,
                 # value_type=str, or launch_ros infers BOOL from the string "true"
                 # and the node - whose parameter is a three-valued STRING - refuses to
                 # declare it and dies before it has built anything.
                 "sleep_on_shutdown": ParameterValue(sleep_on_shutdown, value_type=str),
                 "rest_shim.enabled": ParameterValue(rest_shim_enabled, value_type=bool),
                 "rest_shim.port": ParameterValue(rest_port, value_type=int),
                 # One publisher per topic: when the separate v4l2_camera node is
                 # publishing the real camera, this node must not publish over it.
                 # Otherwise the node publishes the camera itself -- from MuJoCo on a
                 # simulator, and from `camera.device` through GStreamer on a robot.
                 "camera.enabled": ParameterValue(NotSubstitution(with_camera_v4l2),
                                                  value_type=bool),
                 # The same device the v4l2_camera node would have read, handed to
                 # whichever of the two is doing the reading. Ignored by the MuJoCo
                 # backend, which has a renderer instead of a device.
                 "camera.device": video_device,
                 # value_type on every one of these: launch_ros infers the type from
                 # the STRING a launch argument carries, and a `gaze.detect_hz` that
                 # arrived as the string "5.0" would be declared as a string parameter
                 # and refused by the node before it had built anything.
                 "gaze.enabled": ParameterValue(gaze_enabled, value_type=bool),
                 "gaze.model_path": ParameterValue(gaze_model, value_type=str),
                 "gaze.detect_hz": ParameterValue(gaze_detect_hz, value_type=float),
                 "gaze.select_score": ParameterValue(gaze_select_score,
                                                     value_type=float),
                 "gaze.invert_yaw": ParameterValue(gaze_invert_yaw, value_type=bool),
                 "gaze.fx_scale": ParameterValue(gaze_fx_scale, value_type=float),
                 "gaze.fy_scale": ParameterValue(gaze_fy_scale, value_type=float),
             }]),

        Node(package="v4l2_camera", executable="v4l2_camera_node", name="reachy_camera",
             output="screen", condition=IfCondition(with_camera_v4l2),
             parameters=[{
                 "video_device": video_device,
                 "image_size": [1280, 720],
                 "pixel_format": "YUYV",
             }],
             remappings=[("/image_raw", "/reachy_mini/camera/image_raw"),
                         ("/camera_info", "/reachy_mini/camera/camera_info")]),

        Node(package="audio_common", executable="audio_capturer_node",
             name="reachy_audio", output="screen", condition=IfCondition(with_audio)),
    ])
