"""The on-robot ROS 2 deployment artifacts, checked as text.

None of this can be exercised here: there is no Docker on the dev machine's test run,
no ROS on the robot, and the graph only ever exists inside a container built off the robot. What
CAN be checked is the handful of settings whose absence is a quiet, expensive failure on
a robot in front of a person:

* a second FIWARE writer (the hostapp already historizes every cycle: every KPI doubles);
* DDS multicast onto a venue's wifi;
* a ``PartOf=`` that lets a ROS restart bounce the kiosk (the stale-kiosk trap) or a
  hostapp restart kill the graph;
* an amd64 image that installs perfectly on the dev machine and dies on the CM4 with a
  bare ``exec format error``;
* a tuning file that never reaches the container, so the node silently runs on code
  defaults while the robot's bisected thresholds sit unread on the host;
* an install step that runs on every robot instead of only where an operator opted in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_COMPOSE = _ROOT / "deploy" / "ros2" / "docker-compose.robot.yml"
_SIM_COMPOSE = _ROOT / "deploy" / "ros2" / "docker-compose.yml"
_UNIT = _ROOT / "deploy" / "robot" / "emori-ros2.service"
_DOCKERFILE = _ROOT / "deploy" / "ros2" / "Dockerfile"
_INSTALL = _ROOT / "deploy" / "robot" / "install.sh"
# The in-container path both profiles point EMORI_BEHAVIOR_CONFIG at, and the mount
# target the host file lands on. One constant so the two can never drift apart.
_CONFIG_IN_CONTAINER = "/emori/configs/behavior.yaml"


def _service(compose_file: Path) -> dict:
    compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    return compose["services"]["ros2"]


def _robot_service() -> dict:
    return _service(_ROBOT_COMPOSE)


def _env_of(compose_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in _service(compose_file).get("environment", []):
        name, _, value = str(entry).partition("=")
        out[name] = value
    return out


def _environment() -> dict[str, str]:
    return _env_of(_ROBOT_COMPOSE)


@pytest.mark.parametrize("name, expected", [
    ("EMORI_SOURCE", "pokamind"),          # the live relay, not the mock arc
    ("FIWARE_BRIDGE_ENABLED", "0"),        # the hostapp is the writer: no double rows
    ("EMORI_WITH_FIWARE", "false"),        # ...and the launch file must not start it
    ("ROS_LOCALHOST_ONLY", "1"),           # no DDS multicast onto the venue wifi
])
def test_the_robot_profile_hard_sets_the_traps(name, expected):
    assert _environment().get(name) == expected


def test_the_robot_profile_defaults_to_the_hostapp_owning_motion():
    """First run on a live robot must be a non-event: the graph computes, nothing moves."""

    assert _environment().get("EMORI_ROS_MOTION_OWNER", "").endswith(":-hostapp}")


def test_the_robot_profile_reaches_the_hostapp_over_loopback():
    """Host networking is what lets /api/skill refuse everything that is not loopback."""

    service = _robot_service()
    assert service["network_mode"] == "host"
    assert "127.0.0.1" in _environment().get("EMORI_SKILL_URL", "")
    assert "127.0.0.1" in _environment().get("EMORI_WS_URL", "")
    # No daemon URL anywhere: motion leaves the graph only as a skill request.
    assert "8000" not in _ROBOT_COMPOSE.read_text(encoding="utf-8")


def test_the_robot_profile_is_bounded_and_rotates_its_logs():
    """The camera bridge already costs ~104% of a core; an SD card fills up."""

    service = _robot_service()
    assert service.get("mem_limit") and service.get("cpus")
    assert service["logging"]["options"]["max-size"]
    assert service.get("restart") == "unless-stopped"


def test_the_unit_is_independent_of_the_hostapp_in_both_directions():
    # Directives only: the unit EXPLAINS at length why it has no PartOf=, and prose
    # about a directive is not a use of it.
    unit = "\n".join(line for line in _UNIT.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    assert "PartOf=" not in unit and "BindsTo=" not in unit
    assert "Restart=always" in unit
    assert "docker-compose.robot.yml" in unit


def test_the_image_build_states_its_architecture():
    """An accidental amd64 build must be obvious at build time, not `exec format error`."""

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG TARGETPLATFORM" in dockerfile
    assert "--platform=$TARGETPLATFORM" in dockerfile
    assert "uname -m" in dockerfile


# --- the tuning file actually reaching the node -------------------------------------
# If EMORI_BEHAVIOR_CONFIG is forwarded by NEITHER compose file, or the image does not
# copy configs/, hri_engagement_behaviors runs on code defaults. Nobody notices while
# configs/behavior.yaml happens to equal those defaults -- i.e. the first robot to bisect
# its escalation threshold would be the one to find out, in front of a person.

def test_the_image_carries_the_tuning_file_as_its_default():
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY configs/behavior.yaml ./configs/behavior.yaml" in dockerfile


@pytest.mark.parametrize("compose_file", [_SIM_COMPOSE, _ROBOT_COMPOSE])
def test_both_profiles_name_the_tuning_file_inside_the_container(compose_file):
    assert _env_of(compose_file).get("EMORI_BEHAVIOR_CONFIG") == _CONFIG_IN_CONTAINER


@pytest.mark.parametrize("compose_file, default", [
    # Compose resolves a RELATIVE bind path against the compose file's own directory
    # (deploy/ros2/), so this is the repo's configs/behavior.yaml.
    (_SIM_COMPOSE, "../../configs/behavior.yaml"),
    # ...and on the robot it must be absolute: systemd's working directory is not a
    # promise anyone should be relying on.
    (_ROBOT_COMPOSE, "/opt/emori/configs/behavior.yaml"),
])
def test_both_profiles_mount_the_tuning_file_read_only(compose_file, default):
    mounts = [str(v) for v in _service(compose_file).get("volumes", [])]
    expected = "${EMORI_ROS_BEHAVIOR_CONFIG_HOST:-%s}:%s:ro" % (default,
                                                                _CONFIG_IN_CONTAINER)
    assert expected in mounts


@pytest.mark.parametrize("compose_file", [_SIM_COMPOSE, _ROBOT_COMPOSE])
@pytest.mark.parametrize("name, default", [
    # Pseudonymous ids and NO speech text by default: ROS_LOCALHOST_ONLY is the only
    # access control on the wire, so anything published is readable by any node there.
    ("EMORI_ROS_DEIDENTIFY", "1"),
    ("EMORI_ROS_SPEECH_TEXT", "0"),
    # Empty = a per-process random salt, i.e. pseudonyms that do not link across
    # restarts. A fixed salt is a re-identification key and must be an explicit act.
    ("EMORI_ROS_ID_SALT", ""),
    # The facade's transport plug point; `log` is the bench, hostapp_http is the robot.
    ("EMORI_SKILL_TRANSPORT", "hostapp_http"),
])
def test_both_profiles_ship_the_privacy_and_transport_defaults(compose_file, name,
                                                               default):
    assert _env_of(compose_file).get(name) == "${%s:-%s}" % (name, default)


def test_the_sim_profile_still_defaults_to_the_hostapp_owning_motion():
    """Same first-run promise as the robot: the graph computes, nothing moves."""

    assert _env_of(_SIM_COMPOSE).get("EMORI_ROS_MOTION_OWNER", "").endswith(":-hostapp}")


def test_the_install_step_only_runs_where_an_operator_opted_in():
    """While EMORI_WITH_ROS2=0 the install is byte-identical to one without the step."""

    install = _INSTALL.read_text(encoding="utf-8")
    assert '[ "${EMORI_WITH_ROS2:-0}" = "1" ]' in install
    assert "emori-ros2.service" in install
    # ...and it comes after the hostapp unit exists, since the graph reads its relay.
    assert install.index("emori-hostapp.service") < install.index("emori-ros2.service")
