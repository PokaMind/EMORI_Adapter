"""The v2 deployment artifacts, checked as text.

v1's equivalent (`tests/test_ros2_deploy.py`) exists because none of it can be exercised
here: no Docker in the test run, no ROS on the robot, and the graph only ever exists inside
a container built off the robot. v2 inherits every one of those reasons and adds three of its
own, because in v2 the container is not an observer any more -- it drives the motors:

* the Reachy daemon and the robot node want the SAME serial bus and the SAME :8000. A
  profile that does not say so produces a robot where the motors belong to whichever
  process won the race, and it looks completely healthy from the outside;
* the hostapp is NOT changed in v2. `REACHY_BASE_URL=http://127.0.0.1:8000` still has to
  be true, which means the shim has to bind 0.0.0.0 inside the container and the port has
  to be published (sim) or shared (robot). Three settings, one promise;
* `reachy-mini` 1.10.0 needs Python >= 3.11, so v2 cannot be Humble, and v1 must not be
  disturbed by v2 existing. The two images, compose files and container names are checked
  to be disjoint here rather than discovered by a `docker compose up` that replaced the
  default v1 container.

It also pins the two places the same fact is written twice and could drift: the nine
joint names (the outside consumer's expectations vs the compose/harness world) and the
`ros2 topic hz` parser that `scripts/ros2_v2_smoke.py` deliberately keeps its own copy of.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_V2_SIM = _ROOT / "deploy" / "ros2" / "docker-compose.v2.yml"
_V2_ROBOT = _ROOT / "deploy" / "ros2" / "docker-compose.v2.robot.yml"
_V2_FENCE = "/opt/emori/V2_OWNS_THE_BUS"
_V1_SIM = _ROOT / "deploy" / "ros2" / "docker-compose.yml"
_V1_ROBOT = _ROOT / "deploy" / "ros2" / "docker-compose.robot.yml"
_DOCKERFILE = _ROOT / "deploy" / "ros2" / "Dockerfile.v2"
_ROBOT_DOCKERFILE = _ROOT / "deploy" / "ros2" / "Dockerfile.v2.robot"
_V1_DOCKERFILE = _ROOT / "deploy" / "ros2" / "Dockerfile"
_NATIVE = _ROOT / "deploy" / "robot" / "ros2_native"
_PIXI = _NATIVE / "pixi.toml"
_UNIT = _NATIVE / "reachy-mini-ros2.service"
_INSTALL_NATIVE = _NATIVE / "install-native.sh"
_CAMERA_UNIT = _ROOT / "deploy" / "robot" / "camera-bridge.service"
_SETUP_CAMERA = _ROOT / "deploy" / "robot" / "setup-camera-bridge.sh"
_INSTALL = _ROOT / "deploy" / "robot" / "install.sh"
_SMOKE = _ROOT / "scripts" / "ros2_v2_smoke.py"
_HARNESS = _ROOT / "scripts" / "rehearse_motion_owner.py"

_CONFIG_IN_CONTAINER = "/emori/configs/behavior.yaml"
_V2_SERVICE = "ros2v2"
_V2_IMAGE = "emori/ros2-jazzy-v2:latest"
# The robot gets a DIFFERENT tag, not a differently-built `:latest`: the two images
# have different contents and both exist on this network.
_V2_ROBOT_IMAGE = "emori/ros2-jazzy-v2:robot"
_V2_CONTAINER = "emori-ros2-v2"
# The value GET /api/daemon/status reports when the answer is our shim rather than
# Pollen's daemon. The whole v1/v2 interlock hangs off this one string.
_SHIM_BACKEND = "reachy_mini_ros2"


def _load(name: str, path: Path):
    """Import a script by path. `scripts/` is not a package and never will be."""

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def smoke():
    return _load("ros2_v2_smoke", _SMOKE)


@pytest.fixture(scope="module")
def harness():
    return _load("rehearse_motion_owner_v2deploy", _HARNESS)


def test_the_motor_container_cannot_start_with_the_fence_down():
    """It restarts with Docker. Binding the fence, which compose must not create, is what keeps
    it off a bus Pollen's daemon has been given."""
    service = yaml.safe_load(_V2_ROBOT.read_text(encoding="utf-8"))["services"]["ros2v2"]
    bind = next(volume for volume in service["volumes"]
                if isinstance(volume, dict) and _V2_FENCE in str(volume.get("source")))
    assert bind["type"] == "bind" and bind["read_only"] is True
    assert bind["bind"]["create_host_path"] is False
    assert service["restart"] == "unless-stopped"
    script = (_ROOT / "deploy/robot/ros2_native/v2-stack.sh").read_text(encoding="utf-8")
    assert f'FENCE="${{EMORI_V2_FENCE:-{_V2_FENCE}}}"' in script
    dropin = (_ROOT / "deploy/robot/ros2_native/reachy-mini-daemon-v2-fence.conf").read_text(
        encoding="utf-8")
    assert _V2_FENCE in dropin


def _service(compose_file: Path) -> dict:
    compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    services = compose["services"]
    assert list(services) == [_V2_SERVICE], \
        f"{compose_file.name} must define exactly one service, {_V2_SERVICE}"
    return services[_V2_SERVICE]


def _env_of(compose_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in _service(compose_file).get("environment", []):
        name, _, value = str(entry).partition("=")
        out[name] = value
    return out


# --- the image ------------------------------------------------------------------------


def test_v2_is_jazzy_because_the_robot_sdk_cannot_be_installed_on_humble():
    """reachy-mini 1.10.0 requires Python >= 3.11; Humble is 22.04 / 3.10.

    This is not a preference and not a "newer is better": it is the single constraint
    that decided the whole v2 distro, so it is pinned where a future upgrade would read
    it.
    """

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert "ros:jazzy-ros-base" in dockerfile
    assert '"reachy-mini[mujoco]==1.10.0"' in dockerfile
    # Directives only: the header EXPLAINS the Humble comparison at length, and prose
    # about a distro is not a use of it.
    directives = "\n".join(line for line in dockerfile.splitlines()
                           if line.strip() and not line.lstrip().startswith("#"))
    assert "humble" not in directives.lower()


def test_the_v2_image_build_states_its_architecture():
    """Same trap as v1: an accidental amd64 image dies on the CM4 with `exec format
    error` and nothing else. The arch is echoed into the build log."""

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG TARGETPLATFORM" in dockerfile
    assert "--platform=$TARGETPLATFORM" in dockerfile
    assert "uname -m" in dockerfile


@pytest.mark.parametrize("package", [
    # The ROS4HRI interface the two hri_ packages publish, and the ledger carrier.
    "ros-jazzy-hri-msgs", "ros-jazzy-diagnostic-msgs",
    # The robot's real camera on /dev/video40.
    "ros-jazzy-v4l2-camera",
    # reachy-mini pulls PyGObject, which COMPILES against cairo and girepository. Without
    # these the pip install dies halfway through a C build naming none of them.
    "libcairo2-dev", "libgirepository1.0-dev", "pkg-config",
    # MUJOCO_GL=egl with no GPU: a libglvnd EGL/GLES stack answered by Mesa's software
    # path. libosmesa6 is the documented fallback, so MUJOCO_GL=osmesa is one env var
    # rather than a rebuild.
    "libegl1", "libgles2", "libglvnd0", "libglx0", "libosmesa6",
])
def test_the_v2_image_installs_what_the_backend_and_the_renderer_need(package):
    assert package in _DOCKERFILE.read_text(encoding="utf-8")


def test_the_emotions_library_is_baked_into_the_image():
    """85 moves, pulled from Hugging Face at BUILD time.

    Otherwise the first expression request on a venue's wifi stalls for the length of a
    dataset download, and on an air-gapped robot it fails outright. The daemon pre-loads
    the same dataset at startup for the same reason.
    """

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert "pollen-robotics/reachy-mini-emotions-library" in dockerfile
    assert "RecordedMoves" in dockerfile
    # Stated rather than inherited, so the cache written at build time is provably the
    # one read at run time.
    assert "HF_HOME=/opt/hf" in dockerfile


def test_the_image_ships_the_headless_renderer_default():
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert "MUJOCO_GL=egl" in dockerfile


def test_the_apt_owned_distributions_are_stepped_around_rather_than_uninstalled():
    """pip cannot uninstall a dpkg (no RECORD file) and reachy-mini wants all three newer.

    Observed three times in a row: pip 24.0, numpy 1.26.4, PyGObject 3.48.2. Each failure arrives a minute into the build, after every wheel has been
    compiled, and names only one package -- so the fix is pinned here with its reason
    rather than left to be rediscovered.
    """

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    preinstall = [line for line in dockerfile.splitlines() if "--ignore-installed" in line]
    assert preinstall, "the ignore-installed pre-install step is gone"
    block = dockerfile.split("--ignore-installed", 1)[1].split("&&", 1)[0]
    for requirement in ("pip>=26.1", "numpy>=2.2.5", "PyGObject"):
        assert requirement in block, requirement


def test_v2_leaves_the_v1_image_completely_alone():
    """Two Dockerfiles, two distros, two tags, no shared state."""

    v1 = _V1_DOCKERFILE.read_text(encoding="utf-8")
    assert "ros:humble-ros-base" in v1 and "jazzy" not in v1.lower()
    assert "reachy-mini" not in v1
    # ...and v2 does not reuse v1's entrypoint file, which sources Humble.
    assert "COPY deploy/ros2/entrypoint.sh" not in _DOCKERFILE.read_text(encoding="utf-8")


# --- the robot image ------------------------------------------------------------------


def test_the_robot_profile_and_the_sim_profile_do_not_share_a_tag():
    """The simulator image is not shippable to the robot, and tagging both `:latest` is
    how a MuJoCo image ends up on a 2.7 GB SD card -- or, worse, how `docker load`
    quietly replaces the image the robot is running."""

    assert _service(_V2_ROBOT)["image"] == _V2_ROBOT_IMAGE
    assert _service(_V2_SIM)["image"] == _V2_IMAGE
    assert _V2_ROBOT_IMAGE != _V2_IMAGE
    v1 = yaml.safe_load(_V1_ROBOT.read_text(encoding="utf-8"))["services"]["ros2"]
    assert v1["image"] != _V2_ROBOT_IMAGE


def test_the_robot_image_carries_no_simulator():
    """`REACHY_BACKEND=robot` never imports MuJoCo -- no model, no meshes, no offscreen
    GL -- so none of that belongs on the robot's card. This is the whole reason a second
    Dockerfile exists, and the assertion that keeps it true."""

    dockerfile = _ROBOT_DOCKERFILE.read_text(encoding="utf-8")
    directives = " ".join(line for line in dockerfile.splitlines()
                          if line.strip() and not line.lstrip().startswith("#"))
    assert "mujoco" not in directives.lower()
    for renderer in ("MUJOCO_GL", "libegl1", "libgles2", "libosmesa6", "libglvnd0"):
        assert renderer not in directives, renderer
    assert '"reachy-mini==1.10.0"' in directives


def test_the_robot_image_leaves_the_camera_and_audio_stacks_out():
    """640 MB, measured, for one node that is off and one that was never
    tested on hardware.

    `ros-jazzy-v4l2-camera` depends on `cv_bridge`, `cv_bridge` depends on
    `libopencv-dev`, and that pulls VTK, GDAL, LLVM and Mesa onto a robot that renders
    nothing. It is not just weight: `docker load` needs roughly twice an image's size
    while it unpacks, and the 2.28 GB image carrying both ran the robot's card out of space
    mid-load. So the robot publishes no camera topic until the node grows its own capture
    path or the card grows -- and the SIM image keeps both packages, so nothing is lost
    off the robot.
    """

    dockerfile = _ROBOT_DOCKERFILE.read_text(encoding="utf-8")
    directives = " ".join(line for line in dockerfile.splitlines()
                          if line.strip() and not line.lstrip().startswith("#"))
    for package in ("ros-jazzy-v4l2-camera", "ros-jazzy-audio-common"):
        assert package not in directives, package
        # ...and the sim image, which has room, still has it.
        assert package in _DOCKERFILE.read_text(encoding="utf-8"), package
    # The reason is written where the next person to "fix" this will be standing.
    assert "640 MB" in dockerfile


def test_the_robot_image_carries_the_capture_plugins_instead():
    """The camera is not gone from the robot, it is captured differently.

    `robot_node` reads the device through GStreamer -- v4l2src, videoconvert, videoscale,
    appsink -- which is 63 MB measured against the 640 MB of the ROS camera stack. The
    gir1.2-* typelibs are NOT enough on their own: PyGObject imports fine with no plugins
    installed and then finds no elements, which turns a build-time dependency into a
    camera that fails on the robot at pipeline start.
    """

    dockerfile = _ROBOT_DOCKERFILE.read_text(encoding="utf-8")
    for package in ("gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good"):
        assert package in dockerfile, package
    assert "63 MB" in dockerfile


def test_the_robot_image_states_its_architecture():
    """An accidental amd64 image dies on the CM4 with `exec format error` and nothing
    else -- and this one is only ever built for a platform this machine is not."""

    dockerfile = _ROBOT_DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG TARGETPLATFORM" in dockerfile
    assert "--platform=$TARGETPLATFORM" in dockerfile
    assert "uname -m" in dockerfile


@pytest.mark.parametrize("package", [
    # The ROS4HRI interface the two hri_ packages publish, and the ledger carrier.
    "ros-jazzy-hri-msgs", "ros-jazzy-diagnostic-msgs",
    # GStreamer introspection: Pollen's media module imports gi.repository at import time.
    "gir1.2-gstreamer-1.0",
    # PyGObject links against these at RUN time; the -dev half is build-time only (below).
    "libcairo2", "libgirepository-1.0-1", "python3-gi",
])
def test_the_robot_image_installs_what_the_real_backend_needs(package):
    assert package in _ROBOT_DOCKERFILE.read_text(encoding="utf-8")


def test_the_robot_image_purges_the_build_headers_in_the_layer_that_used_them():
    """PyGObject has no aarch64 wheel, so the CM4 image compiles it -- and a compiler
    plus headers left behind is ~300 MB of an SD card that has 2.7 GB."""

    dockerfile = _ROBOT_DOCKERFILE.read_text(encoding="utf-8")
    layers = [layer for layer in dockerfile.split("\nRUN ")
              if "libcairo2-dev" in layer]
    assert len(layers) == 1, "the dev headers are installed in more than one layer"
    assert "apt-get purge" in layers[0]
    for header in ("build-essential", "libcairo2-dev", "libgirepository1.0-dev"):
        assert layers[0].count(header) == 2, header  # installed and purged, same layer


def test_the_emotions_library_is_baked_into_the_robot_image():
    """A venue's wifi is not a dependency of the first expression request, and an
    air-gapped robot is not a broken one."""

    dockerfile = _ROBOT_DOCKERFILE.read_text(encoding="utf-8")
    assert "pollen-robotics/reachy-mini-emotions-library" in dockerfile
    assert "HF_HOME=/opt/hf" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile


# --- the sim profile ------------------------------------------------------------------


def test_the_sim_profile_publishes_the_daemons_own_port():
    """The hostapp is NOT changed in v2: REACHY_BASE_URL=http://127.0.0.1:8000 still
    has to be true, so the shim has to be reachable exactly where the daemon was."""

    assert _service(_V2_SIM).get("ports") == ["8000:8000"]


def test_the_sim_profile_names_its_own_image_and_container():
    """Sharing either with v1 would mean `docker compose up` replacing the default v1 stack."""

    service = _service(_V2_SIM)
    assert service["image"] == _V2_IMAGE
    assert service["container_name"] == _V2_CONTAINER
    assert service["build"]["dockerfile"] == "deploy/ros2/Dockerfile.v2"

    v1 = yaml.safe_load(_V1_SIM.read_text(encoding="utf-8"))["services"]["ros2"]
    assert v1["image"] != service["image"]
    assert v1["container_name"] != service["container_name"]


@pytest.mark.parametrize("name, expected", [
    ("REACHY_BACKEND", "${REACHY_BACKEND:-mujoco}"),   # the container IS the robot
    ("EMORI_WITH_FIWARE", "false"),        # the hostapp is the writer: no double rows
    ("FIWARE_BRIDGE_ENABLED", "0"),        # ...and the launch file must not start it
    ("RMW_IMPLEMENTATION", "rmw_fastrtps_cpp"),
    ("MUJOCO_GL", "egl"),                  # headless, no GPU
    ("EMORI_BEHAVIOR_CONFIG", _CONFIG_IN_CONTAINER),
])
def test_the_sim_profile_hard_sets_the_traps(name, expected):
    assert _env_of(_V2_SIM).get(name) == expected


def test_both_v2_profiles_default_to_the_native_transport():
    """v2's whole point: the facade sends skills to the robot node's actions, no HTTP.

    The sim profile keeps it overridable so a v1-vs-v2 comparison can be run through
    `hostapp_http`; the robot profile does not, because on a robot there is no second
    intake worth pointing at.
    """

    assert _env_of(_V2_SIM)["EMORI_SKILL_TRANSPORT"] == \
        "${EMORI_SKILL_TRANSPORT:-ros_native}"
    assert _env_of(_V2_ROBOT)["EMORI_SKILL_TRANSPORT"] == "ros_native"


@pytest.mark.parametrize("compose_file", [_V2_SIM, _V2_ROBOT])
@pytest.mark.parametrize("name, default", [
    # Pseudonymous ids and NO speech text by default: ROS_LOCALHOST_ONLY is the only
    # access control on the wire, so anything published is readable by any node there.
    ("EMORI_ROS_DEIDENTIFY", "1"),
    ("EMORI_ROS_SPEECH_TEXT", "0"),
    # Empty = a per-process random salt. A fixed salt is a re-identification key.
    ("EMORI_ROS_ID_SALT", ""),
])
def test_both_v2_profiles_keep_the_v1_privacy_defaults(compose_file, name, default):
    assert _env_of(compose_file).get(name) == "${%s:-%s}" % (name, default)


@pytest.mark.parametrize("compose_file", [_V2_SIM, _V2_ROBOT])
def test_both_v2_profiles_default_to_the_hostapp_owning_motion(compose_file):
    """First run of v2 must be a non-event, exactly as v1's first run was: the robot has
    moved into the container, and the lane that authors expression has not moved."""

    assert _env_of(compose_file).get("EMORI_ROS_MOTION_OWNER", "").endswith(":-hostapp}")


@pytest.mark.parametrize("compose_file, default", [
    (_V2_SIM, "../../configs/behavior.yaml"),
    (_V2_ROBOT, "/opt/emori/configs/behavior.yaml"),
])
def test_both_v2_profiles_mount_the_tuning_file_read_only(compose_file, default):
    """The same defect v1 guards against: a node silently running on code defaults.

    Relative on the dev box (compose resolves it against deploy/ros2/), ABSOLUTE on the
    rig (systemd's working directory is not a promise anyone should rely on).
    """

    mounts = [str(v) for v in _service(compose_file).get("volumes", [])]
    expected = "${EMORI_ROS_BEHAVIOR_CONFIG_HOST:-%s}:%s:ro" % (default,
                                                                _CONFIG_IN_CONTAINER)
    assert expected in mounts


def test_the_sim_profile_warns_that_the_windows_daemon_must_be_down():
    """Both want :8000, and the failure when the daemon wins is SILENT: the hostapp
    talks to the daemon and the robot looks like a healthy v2 that is actually v1."""

    header = _V2_SIM.read_text(encoding="utf-8")
    assert "reachy-mini-daemon" in header.lower()
    assert _SHIM_BACKEND in header


# --- the robot profile ----------------------------------------------------------------


def test_the_robot_profile_reaches_the_hostapp_over_loopback():
    """Host networking is what makes the shim literally 127.0.0.1:8000 for the hostapp,
    and what lets /api/skill keep refusing everything that is not loopback."""

    service = _service(_V2_ROBOT)
    assert service["network_mode"] == "host"
    # `ports:` under host networking is meaningless and compose warns about it.
    assert "ports" not in service
    assert "127.0.0.1" in _env_of(_V2_ROBOT).get("EMORI_WS_URL", "")


def test_the_robot_profile_passes_through_the_bus_and_the_camera():
    """The 9-servo 1 Mbaud bus and the v4l2loopback node the camera bridge produces.

    v1 needed neither: it never touched hardware. This is the line where v2 stops being
    an observer.
    """

    service = _service(_V2_ROBOT)
    devices = [str(d) for d in service.get("devices", [])]
    assert "/dev/ttyAMA3:/dev/ttyAMA3" in devices
    assert "/dev/video40:/dev/video40" in devices
    groups = [str(g) for g in service.get("group_add", [])]
    assert "dialout" in groups and "video" in groups


@pytest.mark.parametrize("name, expected", [
    ("REACHY_BACKEND", "robot"),            # the real serial bus, not MuJoCo
    ("REACHY_WIRELESS", "1"),               # find_serial_port(wireless_version=True)
    ("REACHY_SERIALPORT", "/dev/ttyAMA3"),  # named, so a second adapter cannot win
    # 0, and the image is why: the camera stack costs 640 MB the robot cannot load.
    ("WITH_CAMERA_V4L2", "0"),
    ("EMORI_SOURCE", "pokamind"),           # the live relay, not the mock arc
    ("EMORI_WITH_FIWARE", "false"),
    ("FIWARE_BRIDGE_ENABLED", "0"),
    ("ROS_LOCALHOST_ONLY", "1"),            # no DDS multicast onto the venue wifi
])
def test_the_robot_profile_hard_sets_the_traps(name, expected):
    assert _env_of(_V2_ROBOT).get(name) == expected


def test_the_robot_profile_is_bounded_higher_than_v1_and_says_why():
    """This container carries the 50 Hz control loop the daemon carries on v1.

    v1's 512m/1.0 was sized for an observer graph. Leaving it there would OOM-kill the
    robot, and the comment is what stops someone "tidying" it back.

    1536m replaces the original 768m estimate: the graph measured 1156 MB PSS on a
    robot, and the only reason 768m would not kill it is that the Pi kernel
    discards the limit unless `cgroup_enable=memory` is on the boot cmdline. A ceiling
    that is both wrong and unenforced is the worst of the three states.
    """

    service = _service(_V2_ROBOT)
    assert service["mem_limit"] == "1536m"
    assert float(service["cpus"]) >= 2.0
    assert service["restart"] == "unless-stopped"
    assert service["logging"]["options"]["max-size"]

    v1 = yaml.safe_load(_V1_ROBOT.read_text(encoding="utf-8"))["services"]["ros2"]
    assert service["mem_limit"] != v1["mem_limit"]

    text = _V2_ROBOT.read_text(encoding="utf-8")
    assert "50 Hz" in text and "daemon" in text


def test_the_robot_profile_says_the_memory_limit_is_for_the_robot_backend_only():
    """The ceiling has no room for MuJoCo, and "debug on the robot with the simulator" is
    the obvious thing to try. The header has to say so, or the OOM kill reads as a crash
    in our code."""

    header = _V2_ROBOT.read_text(encoding="utf-8")
    assert "REACHY_BACKEND=robot" in header
    assert "MuJoCo" in header
    assert "docker-compose.v2.yml" in header, "the header must name the sim compose"


def test_the_measured_memory_number_is_written_next_to_the_limit():
    """A limit with no measurement beside it is the one that gets "tidied" back down."""

    text = _V2_ROBOT.read_text(encoding="utf-8")
    assert "1156 MB PSS" in text
    assert "cgroup_enable=memory" in text, "say how the limit becomes real, or it reads as dead code"


def test_the_robot_profile_gives_the_node_time_to_park_the_robot():
    """The node runs `backend.reset_to_sleep()` on the way out -- lift, sleep
    trajectory, de-torque AT the sleep pose -- bounded at 8 s inside the node.

    Compose's default grace is 10 s. A SIGKILL landing mid-trajectory leaves nine servos
    torqued at whatever pose the head was in, until the power goes and it drops. This is
    the number that makes the park survive a `docker compose down`.
    """

    assert _service(_V2_ROBOT)["stop_grace_period"] == "20s"


@pytest.mark.parametrize("compose_file", [_V2_SIM, _V2_ROBOT])
def test_both_v2_profiles_stop_with_the_only_signal_ros2_launch_answers(compose_file):
    """Measured against emori/ros2-jazzy-v2: `docker stop -t 30` on a
    container running `ros2 launch` ran the FULL grace period and ended in a SIGKILL
    (exit 137) with the node's `shutdown()` never entered. The same container stopped
    with SIGINT exited cleanly in 0.7 s.

    `ros2 launch` installs a SIGINT handler and does not act on SIGTERM. Without this
    line the park added for the robot profile is code that never runs.
    """

    assert _service(compose_file)["stop_signal"] == "SIGINT"


def test_the_robot_profile_tells_you_to_stop_the_daemon_first():
    """Two processes on one serial bus is not a degraded rig, it is a coin toss."""

    header = _V2_ROBOT.read_text(encoding="utf-8")
    assert "systemctl disable --now reachy-mini-daemon" in header
    assert "/dev/ttyAMA3" in header


def test_v2_never_touches_the_v1_compose_files():
    """v1 must keep working unchanged: it is the robot that has actually run a session."""

    for compose_file in (_V1_SIM, _V1_ROBOT):
        text = compose_file.read_text(encoding="utf-8")
        assert "jazzy" not in text.lower()
        assert _V2_CONTAINER not in text
        assert "REACHY_BACKEND" not in text


# --- the gaze camera path: the tee, the second loopback, the model --------------------
#
# Face look-at needs a camera the node can actually open, and on a kiosk rig video40 is
# not it: v4l2loopback runs exclusive_caps=Y and hands its buffers to ONE consumer, which
# is Chromium. The whole path is therefore four artifacts that have to agree -- the tee
# in camera-bridge.sh, the node created at module load, the device passed into the
# container, and the env var naming it -- and any one of them disagreeing produces a robot
# where the topic is silently empty rather than one that fails.


def test_the_robot_profile_passes_through_the_ros_camera_tee():
    """/dev/video41, the bridge's SECOND loopback, at 320x240 @ 10 fps."""

    devices = [str(d) for d in _service(_V2_ROBOT).get("devices", [])]

    assert "/dev/video41:/dev/video41" in devices
    # video40 is still passed through -- a non-kiosk robot whose device is free may use it.
    assert "/dev/video40:/dev/video40" in devices


def test_the_robot_profile_points_the_node_at_the_tee_and_not_at_the_kiosk_device():
    """A device in `devices:` that nothing names is a device nothing reads.

    REACHY_VIDEO_DEVICE is what robot.launch.py threads into the node's `camera.device`
    parameter. Defaulting it to video40 would put the node in a REQBUFS fight with
    Chromium and lose, which on this robot looks exactly like "the camera topic is empty".
    """

    env = _env_of(_V2_ROBOT)

    assert env.get("REACHY_VIDEO_DEVICE") == "${REACHY_VIDEO_DEVICE:-/dev/video41}"
    assert "video40" not in env.get("REACHY_VIDEO_DEVICE", "")


def test_the_gaze_capability_switch_is_separate_from_the_run_switch():
    """EMORI_GAZE builds the detector; POST /api/emori/gaze runs it.

    Defaulting the capability ON and the run flag OFF is what makes an idle kiosk in a
    public corridor spend no CPU and run no face detector, while still letting a session
    turn look-at on without a container restart. The compose file has to SAY which of the
    two it is, because "gaze is enabled" reads like a behaviour and is not one.
    """

    text = _V2_ROBOT.read_text(encoding="utf-8")
    env = _env_of(_V2_ROBOT)

    assert env.get("EMORI_GAZE") == "${EMORI_GAZE:-1}"
    assert "CAPABILITY switch, not a run switch" in text
    assert "/api/emori/gaze" in text


_GAZE_MOTION_KNOBS = [
    ("EMORI_GAZE_SERVO_HZ", "${EMORI_GAZE_SERVO_HZ:-25.0}"),
    ("EMORI_GAZE_ANCHOR", "${EMORI_GAZE_ANCHOR:-1}"),
    ("EMORI_GAZE_LANE", "${EMORI_GAZE_LANE:-}"),
    ("EMORI_GAZE_ANCHOR_RECORDED", "${EMORI_GAZE_ANCHOR_RECORDED:-yaw}"),
    ("EMORI_RECORDED_BLEND_S", "${EMORI_RECORDED_BLEND_S:-0.3}"),
]


@pytest.mark.parametrize("name, value", _GAZE_MOTION_KNOBS)
def test_the_robot_profile_passes_the_gaze_motion_knobs_into_the_container(name, value):
    """The node reads these from ITS environment, and a container only has what this
    list gives it. A container env with neither EMORI_GAZE_ANCHOR nor EMORI_GAZE_LANE
    makes the documented off switch for the face anchor do nothing, and the follower's
    rate cannot be turned from deploy/ros2/.env at all."""

    assert _env_of(_V2_ROBOT).get(name) == value
    # Hardware-only, like the rest of face look-at: the sim profile has no camera.
    assert name not in _env_of(_V2_SIM)


def test_the_robot_profile_detects_faces_at_the_measured_cm4_rate():
    """The one compose default that is NOT the code's, on purpose. On the CM4 with the kiosk
    running YuNet takes 71 ms a frame: 5 Hz held /joint_states at 45.4 Hz, 3 Hz at 46.6 Hz.
    A robot whose deploy/ros2/.env does not set it must still get 3.0, and the launch
    default (what the simulator and a bench run) stays 5.0."""

    assert _env_of(_V2_ROBOT).get("EMORI_GAZE_DETECT_HZ") == "${EMORI_GAZE_DETECT_HZ:-5.0}"
    launch = (_ROOT / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "launch"
              / "robot.launch.py").read_text(encoding="utf-8")
    assert '_env("EMORI_GAZE_DETECT_HZ", "5.0")' in launch
    assert "EMORI_GAZE_DETECT_HZ" not in _env_of(_V2_SIM)


def test_the_compose_defaults_are_the_codes_own_defaults():
    """`${VAR:-default}` always SETS the variable, so a compose default that drifts from
    the code's silently becomes the robot's behaviour."""

    node = (_ROOT / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "reachy_mini_ros2")
    controller = (node / "gaze" / "controller.py").read_text(encoding="utf-8")
    arbiter = (node / "arbiter.py").read_text(encoding="utf-8")

    assert "servo_hz: float = 25.0" in controller
    assert 'anchor_recorded: str = "yaw"' in controller
    assert "anchor_moves: bool = True" in controller
    assert "DEFAULT_RECORDED_BLEND_S = 0.3" in arbiter
    assert 'DEFAULT_RECORDED_ANCHOR = "yaw"' in arbiter


# --- which launch file the motor container runs -----------------------------------------


_LAUNCH_DEFAULT = "${EMORI_MOTOR_LAUNCH:-v2.launch.py}"


def _bash() -> str | None:
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


def _command_as_compose_gives_it(compose_file: Path, launch: str | None) -> list[str]:
    command = _service(compose_file)["command"]
    assert isinstance(command, list)
    return [part.replace(_LAUNCH_DEFAULT, launch or "v2.launch.py").replace("$$", "$")
            for part in command]


@pytest.mark.parametrize("compose_file", [_V2_ROBOT, _V2_SIM])
def test_the_launch_file_is_the_one_setting_and_defaults_to_the_graph(compose_file):
    text = "\n".join(_service(compose_file)["command"])
    assert text.count(_LAUNCH_DEFAULT) == 1
    assert text.count("EMORI_MOTOR_LAUNCH") == 1


@pytest.mark.parametrize("compose_file", [_V2_ROBOT, _V2_SIM])
@pytest.mark.parametrize("launch", ["v2.launch.py", "robot.launch.py"])
def test_hub_stack_can_read_the_launch_file_back_from_the_running_container(compose_file, launch):
    """It greps the first launch file name out of `docker inspect`, so it must be that one."""

    script = (_ROOT / "deploy" / "robot" / "hub-stack.sh").read_text(encoding="utf-8")
    pattern = re.search(r"grep -oE '([^']+)' \| head -n1", script).group(1)
    inspected = json.dumps(_command_as_compose_gives_it(compose_file, launch))
    assert re.findall(pattern, inspected) == [launch]


@pytest.mark.parametrize("compose_file, launch, expected", [
    (_V2_ROBOT, None, "launch emori_adapter_bringup v2.launch.py"),
    (_V2_ROBOT, "v2.launch.py", "launch emori_adapter_bringup v2.launch.py"),
    (_V2_ROBOT, "robot.launch.py",
     "launch reachy_mini_ros2 robot.launch.py wireless_version:=true with_camera_v4l2:=false"),
    (_V2_SIM, None, "launch emori_adapter_bringup v2.launch.py"),
    (_V2_SIM, "robot.launch.py", "launch reachy_mini_ros2 robot.launch.py"),
])
def test_the_command_runs_the_launch_file_it_names_as_the_containers_main_process(
        tmp_path, compose_file, launch, expected):
    """`exec`: a `ros2 launch` under a shell never gets the SIGINT that parks the robot."""

    bash = _bash()
    if bash is None:
        pytest.skip("no POSIX shell here")
    ros2 = tmp_path / "ros2"
    ros2.write_text('#!/bin/sh\nprintf "%s" "$*"\n', encoding="ascii", newline="\n")
    ros2.chmod(0o755)
    command = _command_as_compose_gives_it(compose_file, launch)
    assert command[:2] == ["bash", "-c"]
    done = subprocess.run(
        [bash, "-c", command[2]], capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"})
    assert done.returncode == 0, done.stderr
    assert done.stdout == expected
    assert command[2].count("exec ros2 launch") == 2


def test_the_robot_launch_files_booleans_are_what_the_robot_profile_says():
    """v2.launch.py turns these two into booleans; robot.launch.py has to be handed them."""

    env = _env_of(_V2_ROBOT)
    command = _command_as_compose_gives_it(_V2_ROBOT, "robot.launch.py")[2]
    assert env["REACHY_WIRELESS"] == "1" and "wireless_version:=true" in command
    assert env["WITH_CAMERA_V4L2"] == "0" and "with_camera_v4l2:=false" in command


def test_the_robot_node_alone_keeps_the_settings_v2_launch_forwarded():
    """Every argument v2.launch.py forwards reaches robot.launch.py some other way here."""

    launch = (_ROOT / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "launch"
              / "robot.launch.py").read_text(encoding="utf-8")
    env = _env_of(_V2_ROBOT)
    for name in ("REACHY_BACKEND", "REACHY_SERIALPORT", "REACHY_REST_PORT",
                 "EMORI_ROS_MOTION_OWNER"):
        assert f'_env("{name}"' in launch
        assert name in env


@pytest.mark.parametrize("launch, expected", [
    ("v2.launch.py", ("reachy_mini", "emori_adapter", "hri_context_analyzer",
                      "hri_engagement_behaviors", "skill_facade")),
    ("robot.launch.py", ("reachy_mini",)),
])
def test_the_smoke_expects_the_nodes_its_launch_file_starts(smoke, launch, expected):
    assert smoke.nodes_for_launch(launch) == expected


def test_the_smoke_fails_a_robot_only_run_that_still_has_a_graph_node(smoke):
    assert smoke.stray_nodes(["/reachy_mini"], "robot.launch.py") == []
    assert smoke.stray_nodes(["/reachy_mini", "/skill_facade"], "robot.launch.py") == [
        "skill_facade"]
    assert smoke.stray_nodes(["/reachy_mini", "/skill_facade"], "v2.launch.py") == []


def test_the_detector_thread_pool_is_bounded_on_a_two_cpu_container():
    """`cpus: 2.0` carries the 50 Hz control loop. An unbounded ORT/OpenMP pool is the
    one thing in this image that can starve it."""

    assert _env_of(_V2_ROBOT).get("OMP_NUM_THREADS") == "1"
    assert _service(_V2_ROBOT)["cpus"] == 2.0


def _mount_target(volume: str) -> str:
    """The container-side path of a `source:target[:mode]` volume string.

    `source` is a `${VAR:-/default}` on every mount in this file and that default
    CONTAINS a colon, so a naive `split(":")[1]` reads the middle of the substitution.
    Blanking the `${...}` spans first is what makes the split mean what it looks like.
    """

    import re

    # The target side is always a literal path here, so masking is lossless for it.
    return re.sub(r"\$\{[^}]*\}", "$", volume).split(":")[1]


def test_the_model_is_bind_mounted_read_only_BESIDE_the_bake_not_over_it():
    """Two copies on purpose, and the mount must not shadow the bake.

    The bake is what makes a fresh container work with no host state, so the path the
    node actually reads (EMORI_GAZE_MODEL) is the BAKED one. The host copy is mounted at
    a different path, /opt/emori/models-host, for the days someone wants to try a model
    or detector change without a 45-minute arm64 rebuild -- and unlike `docker cp` that
    survives `compose up -d`, which recreates the container and discards the writable
    layer. Read-only because nothing in this graph has any business writing a model.

    Mounting the host directory ON the baked path is the failure this pins: it shadows
    the bake on every normal run, and on a robot where install.sh step 5b has not run
    Docker creates the host directory EMPTY and mounts that. The detector then finds no
    model, and a detector that cannot load deliberately never raises the node's
    DiagnosticStatus -- a robot that looks healthy and never looks at anybody.
    """

    volumes = [str(v) for v in _service(_V2_ROBOT).get("volumes", [])]
    mount = next((v for v in volumes if "EMORI_MODELS_HOST" in v), None)
    baked = "/opt/emori/models/face_detection_yunet_2026may.onnx"

    assert mount is not None, volumes
    assert mount.endswith(":ro")
    assert mount.startswith("${EMORI_MODELS_HOST:-/opt/emori/models}:")
    assert _mount_target(mount) == "/opt/emori/models-host", mount
    assert _env_of(_V2_ROBOT).get("EMORI_GAZE_MODEL") == baked
    # No volume may target the baked directory, however its host side is spelled.
    for volume in volumes:
        assert _mount_target(volume) != "/opt/emori/models", volume


def test_the_model_is_baked_after_the_colcon_layer():
    """Layer ORDER, not just presence.

    A COPY beneath the colcon RUN sits under the 45-minute arm64 build layer, so a 229 KB
    model swap would re-ship the whole build. Above it, the pulled delta is 229 KB -- and
    on a robot with 937 MB free that difference is the difference between a shippable fix
    and a rebuild.
    """

    text = _ROBOT_DOCKERFILE.read_text(encoding="utf-8")
    colcon_at = text.index("colcon build")
    copy_at = text.index("COPY deploy/ros2/models/face_detection_yunet_2026may.onnx")

    assert colcon_at < copy_at
    # ...and still inside the image, i.e. before the entrypoint/CMD block.
    assert copy_at < text.index('CMD ["ros2", "launch"')


def test_the_baked_model_is_pinned_by_hash():
    """A truncated or substituted model would otherwise surface on the robot as a detector
    that loads cleanly and finds nobody."""

    text = _ROBOT_DOCKERFILE.read_text(encoding="utf-8")

    assert "sha256sum /opt/emori/models/face_detection_yunet_2026may.onnx" in text
    assert "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0" in text


def test_the_committed_model_is_the_file_the_dockerfile_pins():
    """The hash in the Dockerfile and the bytes in the repo, checked against each other
    rather than both taken on trust."""

    import hashlib

    model = _ROOT / "deploy" / "ros2" / "models" / "face_detection_yunet_2026may.onnx"
    data = model.read_bytes()

    assert len(data) == 229738
    assert hashlib.sha256(data).hexdigest() == \
        "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0"


def test_the_model_survives_the_repositorys_own_models_ignore_rule():
    """`.gitignore` carries a blanket `models/` for the offline speech models. Without a
    negation the one model we deliberately commit is invisible to a clean checkout, and
    the Dockerfile's COPY fails in a build nobody ran locally."""

    ignore = (_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "!deploy/ros2/models/*.onnx" in ignore


def test_the_camera_bridge_unit_creates_the_ros_loopback_as_root():
    """A second ExecStartPre, called bare: the default lives in the script because
    systemd has no `${VAR:-default}` form (deploy/robot/ros-loopback.sh explains it).

    `+-`, not `+`: root, and NON-FATAL. The optional face look-at must never be able to
    fail the bridge and take the kiosk badge scanner's /dev/video40 down with it.
    """

    unit = _CAMERA_UNIT.read_text(encoding="utf-8")
    pre = [line for line in unit.splitlines() if line.startswith("ExecStartPre=")]

    assert len(pre) == 2
    assert "ExecStartPre=+-/opt/emori/deploy/robot/ros-loopback.sh" in pre
    # The EnvironmentFile the script reads its number from comes first.
    assert unit.index("EnvironmentFile=-/etc/default/emori-camera") < unit.index(pre[0])


def test_the_module_options_create_both_nodes_with_matching_arrays():
    """`video_nr=40,41` at module load is what wins the race with docker.service.

    video_nr, exclusive_caps and card_label are ARRAYS and must be the same length: a
    short `exclusive_caps` silently leaves the extra node non-exclusive, which is a node
    Chromium will list and a second consumer can steal. max_buffers is NOT an array --
    it is one int, and `max_buffers=2,2` is EINVAL on v0.15.3: nothing loads at boot.
    """

    text = _SETUP_CAMERA.read_text(encoding="utf-8")

    assert "video_nr=${VIDEO_NR},${ROS_VIDEO_NR}" in text
    assert "exclusive_caps=1,1" in text
    assert 'card_label=\\"ReachyCam\\",\\"ReachyCamROS\\"' in text
    assert 'max_buffers=2"' in text
    assert 'ROS_VIDEO_NR="${EMORI_CAM_ROS_VIDEO_NR:-41}"' in text


def _modprobe_options_line(text: str) -> str:
    """The `options v4l2loopback …` line a script writes to /etc/modprobe.d/."""

    line = next(line for line in text.splitlines() if "options v4l2loopback" in line)
    # Both scripts write it through `echo "…" | tee` / `echo "…" >`, and one interpolates
    # its kiosk number through ${VIDEO_NR} while the other has it literal. Normalise both
    # so the comparison is about the ARRAYS, which is where a drift would hurt.
    return (line.strip().strip("\\").strip()
            .split("options v4l2loopback", 1)[1]
            .replace("${VIDEO_NR}", "40")
            .replace('\\"', '"')
            .strip().strip('"').strip())


def test_the_repeatable_deploy_writes_the_same_module_options_as_first_install():
    """install.sh and setup-camera-bridge.sh must write the SAME options string.

    setup-camera-bridge.sh is the first-install path; install.sh is the REPEATABLE one --
    it runs again on every skill deploy. A one-node line in install.sh therefore silently
    reverts the two-node line on every deploy, and the revert only bites at the NEXT
    reboot: the module loads with video40 alone, and `devices:` in
    docker-compose.v2.robot.yml naming a missing /dev/video41 fails container CREATION.
    The container that fails to be created is emori-ros2-v2 -- the one that owns
    /dev/ttyAMA3 and the 50 Hz control loop -- so the symptom is a robot with no motor
    control at all, one reboot after a deploy that looked clean.
    """

    install = _modprobe_options_line(_INSTALL.read_text(encoding="utf-8"))
    setup = _modprobe_options_line(_SETUP_CAMERA.read_text(encoding="utf-8"))

    assert install == setup, f"install.sh: {install!r}\nsetup-camera-bridge.sh: {setup!r}"
    assert install == ('video_nr=40,${ROS_VIDEO_NR} exclusive_caps=1,1 '
                       'card_label="ReachyCam","ReachyCamROS" max_buffers=2')


def test_the_repeatable_deploy_reloads_the_module_with_both_nodes_too():
    """Writing the file is half of it: step 5 also `modprobe -r` + reloads, and a reload
    with `video_nr=40` alone destroys /dev/video41 until the next reboot."""

    text = _INSTALL.read_text(encoding="utf-8")

    assert 'ROS_VIDEO_NR="${EMORI_CAM_ROS_VIDEO_NR:-41}"' in text
    assert 'sudo modprobe v4l2loopback video_nr="40,${ROS_VIDEO_NR}" exclusive_caps=1,1' in text
    assert 'card_label="ReachyCam","ReachyCamROS" max_buffers=2' in text
    # ...and the single-node line is gone, in both the write and the reload.
    assert "video_nr=40 exclusive_caps=1 " not in text


def test_the_sim_profile_knows_nothing_about_any_of_this():
    """The simulator has no CSI camera, no loopback, no kiosk and no face to look at.

    Every assertion above is about hardware, and the sim profile is where a well-meaning
    "keep the two in sync" edit would land first.
    """

    text = _V2_SIM.read_text(encoding="utf-8")
    env = _env_of(_V2_SIM)

    for name in ("REACHY_VIDEO_DEVICE", "EMORI_GAZE", "EMORI_GAZE_MODEL",
                 "OMP_NUM_THREADS"):
        assert name not in env, name
    assert "video41" not in text
    assert "/opt/emori/models" not in text
    # ...and the sim image does not bake a detector it will never run.
    assert "face_detection_yunet" not in _DOCKERFILE.read_text(encoding="utf-8")


# --- the CM4 native recipe ------------------------------------------------------------


def test_the_pixi_environment_targets_the_robot_and_a_dev_box():
    """linux-aarch64 is the CM4. linux-64 is so the env can be resolved before anyone
    touches the robot -- a lock that can only be made on the hardware it de-risks is not
    much of a de-risking."""

    manifest = yaml.safe_load(_PIXI.read_text(encoding="utf-8").replace("\t", "  ")) \
        if False else _PIXI.read_text(encoding="utf-8")
    assert "linux-aarch64" in manifest and "linux-64" in manifest
    assert "https://prefix.dev/robostack-jazzy" in manifest
    assert "conda-forge" in manifest
    assert 'python = "3.12.*"' in manifest


def test_the_pixi_manifest_records_why_hri_msgs_is_not_a_conda_dependency():
    """RoboStack's jazzy channel does not build ros-jazzy-hri-msgs for ANY platform
    (`pixi lock`). It is a pure rosidl package, so on this route it is a
    source package -- and the absence has to be explained or someone will "fix" it back
    in and get an unsolvable environment."""

    manifest = _PIXI.read_text(encoding="utf-8")
    assert "ros-jazzy-hri-msgs" in manifest, "the explanation is gone"
    dependencies = manifest.split("[dependencies]", 1)[1]
    declared = [line.split("=", 1)[0].strip() for line in dependencies.splitlines()
                if "=" in line and not line.lstrip().startswith("#")]
    assert "ros-jazzy-hri-msgs" not in declared
    for required in ("ros-jazzy-ros-base", "ros-jazzy-tf2-ros",
                     "ros-jazzy-diagnostic-msgs", "ros-jazzy-v4l2-camera",
                     "colcon-common-extensions"):
        assert required in declared, required
    # install-native.sh is where it comes from instead.
    assert "hri_msgs" in _INSTALL_NATIVE.read_text(encoding="utf-8")


def test_the_native_unit_conflicts_with_the_daemon_it_replaces():
    """Directives only: the unit EXPLAINS at length why it has no PartOf=, and prose
    about a directive is not a use of it."""

    unit = "\n".join(line for line in _UNIT.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    assert "Conflicts=reachy-mini-daemon.service" in unit
    assert "After=network.target" in unit
    assert "PartOf=" not in unit and "BindsTo=" not in unit
    assert "Restart=always" in unit


@pytest.mark.parametrize("setting", [
    "REACHY_BACKEND=robot",
    "REACHY_WIRELESS=1",
    "ROS_LOCALHOST_ONLY=1",
    "ROS_DOMAIN_ID=42",
    "ros2 launch emori_adapter_bringup v2.launch.py",
])
def test_the_native_unit_launches_the_v2_graph_against_real_hardware(setting):
    assert setting in _UNIT.read_text(encoding="utf-8")


# The two setup files `reachy-mini-ros2.service` sources, in order. `setup.bash` at the
# environment prefix root is what RoboStack's `ros-<distro>-ros-workspace` conda package
# installs (and what install-native.sh already sources for its own colcon build); the
# second is the colcon overlay that carries emori_adapter_bringup.
_UNIT_SETUP_FILES = ("/opt/emori/ros2/.pixi/envs/default/setup.bash",
                     "/opt/emori/ros2_ws/install/setup.bash")


def test_the_native_unit_sources_the_environment_before_it_launches():
    """`ros2 launch` resolves packages through AMENT_PREFIX_PATH / PYTHONPATH, and
    systemd provides neither: there is no login shell, so nothing sources anything.

    Calling the environment's `ros2` binary directly therefore dies with
    `Package 'emori_adapter_bringup' not found` -- or, once the distro's own packages
    happen to resolve, starts a graph WITHOUT the workspace overlay, which looks healthy
    and runs the wrong code. `exec` is checked too: without it systemd's SIGTERM goes to
    bash, the launch never hears it, and the robot is killed without parking.
    """

    unit = _UNIT.read_text(encoding="utf-8")
    exec_start = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert len(exec_start) == 1, exec_start
    (line,) = exec_start
    assert line.startswith("ExecStart=/bin/bash -lc "), line
    for setup_file in _UNIT_SETUP_FILES:
        assert f"source {setup_file}" in line, setup_file
    assert line.index(_UNIT_SETUP_FILES[0]) < line.index(_UNIT_SETUP_FILES[1]), \
        "the environment must be sourced before the overlay that sits on top of it"
    assert "exec ros2 launch emori_adapter_bringup v2.launch.py" in line
    # ...and the binary path is gone: sourcing is what puts `ros2` on PATH.
    assert "/opt/emori/ros2/.pixi/envs/default/bin/ros2" not in unit


def test_the_native_installer_produces_exactly_those_two_setup_files():
    """A unit that sources a file the installer never creates fails at the first start,
    in a journal nobody is tailing yet. The installer asserts both before it installs
    the unit, and this pins the pair to the one the unit names."""

    install = _INSTALL_NATIVE.read_text(encoding="utf-8")
    assert '"${ENV_PREFIX}/setup.bash"' in install
    assert '"${EMORI_ROOT}/ros2_ws/install/setup.bash"' in install
    # ENV_PREFIX and EMORI_ROOT are what make those two expand to the unit's paths.
    assert 'ENV_PREFIX="${ENV_ROOT}/.pixi/envs/default"' in install
    assert 'ENV_ROOT="${ENV_ROOT:-${EMORI_ROOT}/ros2}"' in install
    assert 'EMORI_ROOT="${EMORI_ROOT:-/opt/emori}"' in install
    assert "colcon" in install, "the overlay setup.bash comes from colcon build"


def test_the_native_unit_stops_with_the_only_signal_ros2_launch_answers():
    """Same measurement as the compose profiles: `ros2 launch` handles SIGINT and
    ignores SIGTERM, so systemd's default stop signal would run the whole
    TimeoutStopSec and then SIGKILL a robot that never parked."""

    unit = "\n".join(line for line in _UNIT.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    assert "KillSignal=SIGINT" in unit


def test_the_native_unit_gives_the_node_time_to_park_the_robot():
    """Same 8 s park as the container profile, plus the shim, the arbiter loop and the
    backend join. systemd's default is high but is lowered distro-wide often enough that
    leaving it unstated is a real way to lose the park."""

    unit = "\n".join(line for line in _UNIT.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    assert "TimeoutStopSec=25" in unit


def test_the_native_installer_disables_the_daemon_and_removes_emoris_override():
    """Leaving the drop-in behind means a later `systemctl enable reachy-mini-daemon`
    silently restores a second owner of the serial bus."""

    install = _INSTALL_NATIVE.read_text(encoding="utf-8")
    assert "systemctl disable --now reachy-mini-daemon" in install
    assert "reachy-mini-daemon.service.d/override.conf" in install
    assert "usermod -aG dialout,video pollen" in install
    # ...and a way back to v1, because a changeover you cannot reverse is not a trial.
    assert "reachy-mini-daemon-override.conf" in install


def test_the_native_installer_installs_the_compilers_pygobject_needs():
    """PyGObject publishes no aarch64 wheel, so it and pycairo are COMPILED on the CM4.

    Without these the failure is a wall of meson output whose actual message is
    `Unknown compiler(s)` -- measured while locking the PyPI half.
    """

    install = _INSTALL_NATIVE.read_text(encoding="utf-8")
    for package in ("build-essential", "libcairo2-dev", "libgirepository1.0-dev",
                    "pkg-config"):
        assert package in install, package


def test_the_native_recipe_is_honest_about_never_having_run():
    """It has not been run on hardware. A recipe that does not say so is a liability."""

    for path in (_INSTALL_NATIVE, _NATIVE / "README.md"):
        text = path.read_text(encoding="utf-8").lower()
        assert "hardware" in text
        assert "not" in text


# --- the smoke script -----------------------------------------------------------------


def test_the_smoke_needs_nothing_installed(smoke):
    """It is meant to be runnable from a bare checkout by someone verifying the claim."""

    import ast

    roots: set[str] = set()
    for node in ast.walk(ast.parse(_SMOKE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add((node.module or "").split(".", 1)[0])
    assert sorted(roots - set(sys.stdlib_module_names)) == []


def test_the_smoke_expects_the_robot_node_in_the_graph(smoke):
    assert "reachy_mini" in smoke.EXPECTED_NODES
    assert set(smoke.EXPECTED_NODES) == {
        "reachy_mini", "emori_adapter", "hri_context_analyzer",
        "hri_engagement_behaviors", "skill_facade"}
    # A leading slash is how `ros2 node list` prints them, and must not read as missing.
    assert smoke.missing_nodes([f"/{name}" for name in smoke.EXPECTED_NODES]) == []
    assert smoke.missing_nodes(["/emori_adapter"]) == [
        n for n in smoke.EXPECTED_NODES if n != "emori_adapter"]


def test_a_refused_goal_is_not_a_successful_one(smoke):
    """A refusal is deliberately a SUCCEEDED goal with completed: false -- refusing is a
    normal arbitration outcome, not a transport error. Checking the status alone would
    therefore pass on a robot that never moved, which is the one thing this asks."""

    completed = ("Goal accepted with ID: 8f3a\nResult:\n    completed: true\n"
                 "    error_code: ''\n    move_id: 'm-7'\n\n"
                 "Goal finished with status: SUCCEEDED\n")
    refused = ("Goal accepted with ID: 8f3a\nResult:\n    completed: false\n"
               "    error_code: 'dropped'\n    move_id: ''\n\n"
               "Goal finished with status: SUCCEEDED\n")
    aborted = ("Result:\n    completed: true\n\nGoal finished with status: ABORTED\n")

    assert smoke.goal_succeeded(completed)
    assert not smoke.goal_succeeded(refused)
    assert not smoke.goal_succeeded(aborted)
    assert not smoke.goal_succeeded("")


def test_the_smoke_refuses_pollens_daemon_on_the_shims_port(smoke):
    """Pollen's daemon has no `backend` field at all, which is what makes it detectable."""

    assert smoke.shim_verdict({"state": "running", "backend": "reachy_mini_ros2"}) == []
    pollen = smoke.shim_verdict({"state": "running", "no_media": True})
    assert len(pollen) == 1 and "reachy_mini_ros2" in pollen[0]
    # Ours, but the backend behind it never came up.
    assert smoke.shim_verdict({"state": "starting", "backend": "reachy_mini_ros2"})
    assert smoke.shim_verdict(None)


def test_the_smoke_asks_the_shim_from_the_host_not_from_inside(smoke):
    """A shim bound to 127.0.0.1 INSIDE the container passes every in-container check
    and is unreachable by the one process that needs it."""

    assert smoke.SHIM_URL.startswith("http://127.0.0.1:8000")
    assert smoke.SHIM_BACKEND == _SHIM_BACKEND


def test_the_two_hz_parsers_agree(smoke, harness):
    """`scripts/ros2_v2_smoke.py` keeps its own copy on purpose (stdlib only), so the
    duplication is pinned to an identical answer rather than left to drift."""

    echo = ("average rate: 31.402\n\tmin: 0.004s max: 0.061s window: 33\n"
            "average rate: 49.973\n\tmin: 0.018s max: 0.022s window: 133\n")

    assert smoke.parse_topic_hz(echo) == harness.parse_topic_hz(echo)
    assert smoke.parse_topic_hz("") is harness.parse_topic_hz("") is None
    assert smoke.MIN_JOINT_STATE_HZ == harness.MIN_JOINT_STATE_HZ
    assert smoke.MIN_CAMERA_HZ == harness.MIN_CAMERA_HZ
    assert smoke.JOINT_STATES_TOPIC == harness.JOINT_STATES_TOPIC
    assert smoke.CAMERA_TOPIC == harness.CAMERA_TOPIC


def test_the_goal_the_smoke_sends_is_small_and_lands_on_the_interaction_lane(smoke):
    """1 cm over 0.5 s: safe on any backend including a real robot on a desk, and large
    enough that MuJoCo's solver has to do something."""

    assert "use_head: true" in smoke.GOAL_YAML
    assert "z: 0.01" in smoke.GOAL_YAML
    assert "duration: 0.5" in smoke.GOAL_YAML
    assert "lane: interaction" in smoke.GOAL_YAML
    assert "client: smoke" in smoke.GOAL_YAML


def test_the_fault_injection_check_needs_the_lie_and_the_exit(smoke):
    """`--kill-backend-check` makes two claims and needs both.

    That the shim SAID `error` is what a hostapp, an installer wait-loop and this very
    script read; that the process then EXITED non-zero is what makes the supervisor
    rebuild the node instead of leaving a live husk in the graph. A run that only ever
    showed `running` and then exited 0 is indistinguishable from a clean shutdown --
    which is precisely the bug being guarded.
    """

    assert smoke.fault_verdict(["running", "running", "error"],
                               smoke.BACKEND_DEAD_EXIT_CODE) == []
    # Healthy the whole way and then gone: the silent failure, in one line.
    assert smoke.fault_verdict(["running", "running"], 0)
    assert len(smoke.fault_verdict(["running"], 137)) == 1      # said nothing, but died
    assert len(smoke.fault_verdict(["error"], 0)) == 2          # never proved it was up
    assert smoke.fault_verdict([], None)
    # It never exits on its own: a node still answering is a node that did not notice.
    assert smoke.fault_verdict(["running", "error"], None)


def test_the_fault_injection_run_cannot_collide_with_the_graph_it_follows(smoke):
    """It is a SECOND container on a SECOND port, so `--keep-up` still means what it
    says and the numbers measured above are not disturbed by it."""

    assert smoke.FAULT_CONTAINER != _V2_CONTAINER
    assert smoke.FAULT_PORT != 8000
    assert str(smoke.FAULT_PORT) in smoke.FAULT_STATUS_URL
    # The fault must fire between two diagnostics ticks, never on one: the window in
    # which the shim is up AND the control loop is dead is what the check observes.
    assert 0 < smoke.FAULT_AFTER_S
    assert smoke.FAULT_AFTER_S % smoke.FAULT_DIAGNOSTICS_PERIOD_S != 0
    assert smoke.FAULT_POLL_S * 4 < smoke.FAULT_DIAGNOSTICS_PERIOD_S


def test_the_fault_switch_is_test_only_and_says_so(smoke):
    """A switch that makes a robot fail its own control loop has to be impossible to
    set by accident and impossible to miss when it IS set."""

    backend_host = (_ROOT / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "reachy_mini_ros2"
                    / "backend_host.py").read_text(encoding="utf-8")
    assert "REACHY_FAULT_INJECT_S" in backend_host
    assert "TEST ONLY" in backend_host
    # Never in a deployable profile or unit: those are the files a robot actually reads.
    for path in (_V2_SIM, _V2_ROBOT, _UNIT, _INSTALL_NATIVE):
        assert "REACHY_FAULT_INJECT_S" not in path.read_text(encoding="utf-8"), path.name


# --- the one fact written in three places ---------------------------------------------


def test_every_file_that_names_the_joints_names_the_same_nine():
    """`body_yaw, stewart_1..6, right_antenna, left_antenna` -- and [right, left] is the
    daemon's wire order, which is the pair a reader would "fix" on sight."""

    checker = _load("outside_consumer_check_v2deploy",
                    _ROOT / "scripts" / "outside_consumer_check.py")
    expected = ("body_yaw", "stewart_1", "stewart_2", "stewart_3", "stewart_4",
                "stewart_5", "stewart_6", "right_antenna", "left_antenna")

    assert checker.EXPECTED_JOINT_NAMES == expected
    assert len(set(expected)) == 9


def test_the_robot_boots_with_pollens_motor_gains_unless_told_otherwise(tmp_path, monkeypatch):
    """v2 passed no hardware config, so no PID gains were written and every servo held on
    its firmware default after a power cycle - the daemon it replaced loads Pollen's
    bundled hardware_config.yaml by default (P 300 on the Stewart servos)."""

    import types

    path = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
            / "reachy_mini_ros2" / "backend_host.py")
    source = path.read_text(encoding="utf-8")
    namespace: dict = {}
    helper = source[source.index("def _hardware_config_path"):]
    exec("from pathlib import Path\n" + helper, namespace)  # noqa: S102 - the helper alone
    resolve = namespace["_hardware_config_path"]

    bundled = tmp_path / "reachy_mini" / "assets" / "config" / "hardware_config.yaml"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("motors: []\n", encoding="utf-8")
    fake = types.ModuleType("reachy_mini")
    fake.__file__ = str(tmp_path / "reachy_mini" / "__init__.py")
    monkeypatch.setitem(sys.modules, "reachy_mini", fake)

    assert resolve("") == str(bundled.resolve())
    assert resolve("  ") == str(bundled.resolve())
    assert resolve("/etc/custom.yaml") == "/etc/custom.yaml"
    assert resolve("none") is None and resolve("NONE") is None
    bundled.unlink()
    assert resolve("") is None
    assert "hardware_config_filepath=hardware_config" in source


# --- the REST shim switch, the motor's OOM score, and ROS CLI calls that must not
# --- leave a daemon behind -------------------------------------------------------------

_LAUNCH = _ROOT / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "launch" / "robot.launch.py"
_READYCHECK = _ROOT / "src" / "emori_adapter" / "hostapp" / "readycheck.py"
# The files that run `ros2` inside the container and would start a ROS 2 daemon there.
_DAEMON_CALLERS = (
    _SMOKE,
    _HARNESS,
    _ROOT / "scripts" / "rehearse_on_robot.py",
    _READYCHECK,
)


def test_the_robot_profile_passes_the_rest_shim_switch_into_the_container():
    """Default on: the host application still reaches the daemon port over HTTP."""

    assert _env_of(_V2_ROBOT)["REACHY_REST_SHIM_ENABLED"] == "${REACHY_REST_SHIM_ENABLED:-true}"


def test_the_launch_file_threads_the_rest_shim_switch_to_the_node():
    """The argument is a bool parameter, declared with its type, or the node would read
    the launch string "true" as a string and refuse it."""

    text = _LAUNCH.read_text(encoding="utf-8")
    assert 'DeclareLaunchArgument("rest_shim_enabled"' in text
    assert 'default_value=_env("REACHY_REST_SHIM_ENABLED", "true")' in text
    assert '"rest_shim.enabled": ParameterValue(rest_shim_enabled, value_type=bool)' in text


def test_the_motor_container_is_the_last_thing_the_oom_killer_picks():
    assert _service(_V2_ROBOT)["oom_score_adj"] == -500


def test_the_ros_cli_calls_that_start_a_daemon_all_pass_no_daemon():
    """`ros2 node list`, `service list` and `topic echo` start a resident daemon in the
    container unless told not to. Every such call in these files must say so."""

    import re

    node_list_bare = re.compile(r'"node",\s*"list"(?!,\s*"--no-daemon")')
    echo_bare = re.compile(r'"topic",\s*"echo"(?!,\s*"--no-daemon")')
    # Followed by a space, so prose such as "`ros2 node list`" in a message is not a call.
    shell_node_bare = re.compile(r"ros2 node list(?! --no-daemon)(?=\s)")
    # The shell form is only a command where it is handed to a shell as a `timeout N` line.
    shell_echo_bare = re.compile(r"timeout \d+ ros2 topic echo(?! --no-daemon)")
    for path in _DAEMON_CALLERS:
        text = path.read_text(encoding="utf-8")
        assert not node_list_bare.search(text), path.name
        assert not echo_bare.search(text), path.name
        assert not shell_node_bare.search(text), path.name
        assert not shell_echo_bare.search(text), path.name


def test_the_readycheck_node_list_is_no_daemon_with_the_spin_time():
    text = _READYCHECK.read_text(encoding="utf-8")
    assert '"&& ros2 node list --no-daemon --spin-time 5"' in text
