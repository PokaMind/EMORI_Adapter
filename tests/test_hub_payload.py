"""How the deploy decides what the robot's hub image needs, and the sim overlay.

The computer half's planning is pure and runs here with the checkout's hashes replaced by
fixed values; the shipping runs against a fake robot. Nothing needs Docker or ROS.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "deploy_to_robot.py"
_HUB_DIR = _ROOT / "deploy" / "hub"
_NAME = "deploy_to_robot_hub_payload"
_WANTED = "aaaaaaaaaaaaaaaa"
_BASE_WANTED = "bbbbbbbbbbbbbbbb"
_MOTOR = "sha256:01|0123456789abcdef|2026-10-30T10:00:00Z"


@pytest.fixture(scope="module")
def deploy():
    spec = importlib.util.spec_from_file_location(_NAME, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(_NAME, None)


@pytest.fixture
def hashes(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "hub_source_hash", lambda: _WANTED)
    monkeypatch.setattr(deploy, "hub_base_source_hash", lambda: _BASE_WANTED)


def _info(**extra):
    info = {"v2_image": _MOTOR, "docker": "yes", "free_mb": "1286", "hub_flag": "yes"}
    info.update(extra)
    return info


def _label(source: str) -> str:
    return f"sha256:02|{source}|2026-11-01T10:00:00Z"


_KEEP = {"action": "keep", "why": ""}


# --- the hashes ------------------------------------------------------------------------


def test_the_hub_hash_covers_the_core_files_the_image_imports(deploy):
    assert set(deploy.HUB_CORE_INPUTS) == {
        "src/emori_adapter/hostapp/server.py", "src/emori_adapter/hostapp/extensions.py",
        "src/emori_adapter/robot/client.py", "src/emori_adapter/sources/websocket.py"}
    assert "ros2_ws/src/emori/emori_adapter_ros" in deploy.HUB_IMAGE_INPUTS
    for path in deploy.HUB_IMAGE_INPUTS + deploy.HUB_CORE_INPUTS + deploy.HUB_BASE_INPUTS:
        assert (_ROOT / path).exists(), path


def test_the_hub_hash_covers_everything_the_hub_dockerfile_copies(deploy):
    sources = []
    for line in (_HUB_DIR / "Dockerfile.hub").read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            sources += [word for word in line.split()[1:-1] if not word.startswith("--")]
    uncovered = [source for source in sources if not any(
        "deploy/" + source == path for path in deploy.HUB_IMAGE_INPUTS)
        and not any(path.startswith("ros2_ws/") for path in deploy.HUB_IMAGE_INPUTS
                    if source.startswith("hub/install-delta"))]
    assert sources and uncovered == []


def test_the_image_hashes_are_short_hex_and_the_base_one_is_separate(deploy):
    assert re.fullmatch(r"[0-9a-f]{16}", deploy.hub_source_hash())
    assert re.fullmatch(r"[0-9a-f]{16}", deploy.hub_base_source_hash())
    assert deploy.hub_source_hash() != deploy.hub_base_source_hash()
    assert not set(deploy.HUB_IMAGE_INPUTS) & set(deploy.HUB_BASE_INPUTS)


def test_each_image_ends_with_its_source_label():
    top = [line for line in (_HUB_DIR / "Dockerfile.hub").read_text(
        encoding="utf-8").splitlines() if line and not line.startswith("#")]
    base = [line for line in (_HUB_DIR / "Dockerfile.hub-base").read_text(
        encoding="utf-8").splitlines() if line and not line.startswith("#")]
    assert top[-1] == "LABEL org.pokamind.emori.hub-source=$EMORI_HUB_SOURCE"
    assert base[-1] == "LABEL org.pokamind.emori.hub-base-source=$EMORI_HUB_BASE_SOURCE"


# --- the plan --------------------------------------------------------------------------


def test_a_hub_image_built_from_this_checkout_is_kept(deploy, hashes):
    plan = deploy.plan_hub_payload(_info(hub_image=_label(_WANTED)), _KEEP, builder=True)
    assert plan["action"] == "keep"


def test_a_robot_on_the_host_app_gets_a_hub_image_by_default(deploy, hashes):
    assert deploy.plan_hub_payload(_info(hub_flag="no"), _KEEP, builder=True)["action"] == "base"
    current = _info(hub_flag="no", hub_image=_label(_WANTED))
    assert deploy.plan_hub_payload(current, _KEEP, builder=True)["action"] == "keep"


def _motor_hashes(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "v2_source_hash", lambda: "a" * 16)
    monkeypatch.setattr(deploy, "motor_source_hash", lambda: "e" * 16)


def test_a_motor_image_from_other_sources_gets_a_layer(deploy, monkeypatch):
    _motor_hashes(deploy, monkeypatch)
    stale = _info(v2_image=_label("b" * 16), free_mb="2000")
    assert deploy.plan_motor_layer(stale, _KEEP, builder=True)["action"] == "build"
    # A label the deploy cannot read says nothing about what it runs.
    unreadable = _info(v2_image=_label("d2893122" * 5), free_mb="2000")
    assert deploy.plan_motor_layer(unreadable, _KEEP, builder=True)["action"] == "build"


def test_a_motor_image_from_this_checkout_is_left_alone(deploy, monkeypatch):
    # A new layer parks and wakes the motors, so it must not happen for nothing.
    _motor_hashes(deploy, monkeypatch)
    current = _info(v2_image=_label("a" * 16), free_mb="2000")
    assert deploy.plan_motor_layer(current, _KEEP, builder=True)["action"] == "keep"


def test_a_motor_layer_from_this_checkout_is_left_alone_whatever_else_changed(deploy, monkeypatch):
    _motor_hashes(deploy, monkeypatch)
    layered = _info(v2_image=_label("b" * 16), motor_source="e" * 16, free_mb="2000")
    assert deploy.plan_motor_layer(layered, _KEEP, builder=True)["action"] == "keep"


def test_the_motor_hash_ignores_code_the_motor_does_not_run(deploy):
    inputs = " ".join(deploy.MOTOR_LAYER_INPUTS)
    assert "ros2_ws/src/open/reachy_mini_ros2" in inputs
    assert "src/emori_adapter" not in inputs and "emori_hub" not in inputs


def test_a_motor_image_just_shipped_needs_no_layer(deploy, monkeypatch):
    _motor_hashes(deploy, monkeypatch)
    arriving = {"action": "ship", "why": ""}
    assert deploy.plan_motor_layer(_info(v2_image=""), arriving, builder=True)["action"] == "keep"


def test_a_motor_layer_the_disk_cannot_hold_is_not_planned(deploy, monkeypatch):
    _motor_hashes(deploy, monkeypatch)
    low = _info(v2_image=_label("b" * 16), free_mb="300")
    plan = deploy.plan_motor_layer(low, _KEEP, builder=True)
    assert plan["action"] == "none" and "300 MB" in plan["why"]


def test_no_motor_image_means_no_layer(deploy, monkeypatch):
    _motor_hashes(deploy, monkeypatch)
    assert deploy.plan_motor_layer(_info(v2_image=""), _KEEP, builder=True)["action"] == "none"


class _Ran:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


class _SwitchRobot:
    user = "pollen"
    host = "robot"

    def __init__(self, returncode=0):
        self.commands = []
        self.returncode = returncode

    def run(self, command, timeout=None):
        self.commands.append(command)
        return _Ran(self.returncode if "hub-stack.sh" in command else 0)


def _on_v2(**overrides):
    fields = dict(v2_image=_label("a" * 16), v2_fence="yes", hub_flag="no",
                  hub_image=_label(_WANTED), free_mb="2000")
    fields.update(overrides)
    return _info(**fields)


def _finish(deploy, monkeypatch, info, robot):
    monkeypatch.setattr(deploy, "preflight", lambda _robot: info)
    monkeypatch.setattr(deploy, "local_docker", lambda: True)
    _motor_hashes(deploy, monkeypatch)
    return deploy.finish_on_hub(robot)


def test_by_default_a_robot_on_the_host_app_ends_up_on_the_hub(deploy, hashes, monkeypatch):
    robot = _SwitchRobot()
    line, on_hub, changed = _finish(deploy, monkeypatch, _on_v2(), robot)
    assert any("hub-stack.sh apply ros" in c for c in robot.commands)
    assert "now runs on it" in line
    assert on_hub is True and changed is True


def test_a_robot_already_on_the_hub_is_not_switched_again(deploy, hashes, monkeypatch):
    robot = _SwitchRobot()
    line, on_hub, changed = _finish(deploy, monkeypatch, _on_v2(hub_flag="yes"), robot)
    assert not any("hub-stack.sh apply" in c for c in robot.commands)
    assert line == "ROS 2 hub: the robot runs on it."
    assert on_hub is True and changed is False


def test_the_voice_delay_default_goes_in_before_the_switch(deploy, hashes, monkeypatch):
    robot = _SwitchRobot()
    _finish(deploy, monkeypatch, _on_v2(), robot)
    delay = next(i for i, c in enumerate(robot.commands) if "EMORI_VOICE_DELAY_MS" in c)
    switch = next(i for i, c in enumerate(robot.commands) if "hub-stack.sh" in c)
    assert delay < switch
    # Only when the robot names no value of its own.
    assert "grep -qE '^EMORI_VOICE_DELAY_MS=' .env ||" in robot.commands[delay]


@pytest.mark.parametrize("code,word", [(3, "session is running"), (5, "not possible yet"),
                                       (6, "back to the host app"), (7, "neither program"),
                                       (9, "exit 9")])
def test_every_way_the_switch_can_end_is_said_on_the_last_screen(deploy, hashes, monkeypatch,
                                                                 code, word):
    line, on_hub, _changed = _finish(deploy, monkeypatch, _on_v2(), _SwitchRobot(code))
    assert word in line
    assert on_hub is False


def test_a_robot_not_on_v2_is_not_switched(deploy, hashes, monkeypatch):
    robot = _SwitchRobot()
    line, on_hub, _changed = _finish(deploy, monkeypatch, _on_v2(v2_fence="no"), robot)
    assert robot.commands == [] and "not on v2" in line and on_hub is False


def test_no_docker_here_says_so_and_changes_nothing(deploy, hashes, monkeypatch):
    robot = _SwitchRobot()
    monkeypatch.setattr(deploy, "preflight", lambda _robot: _on_v2(hub_image=""))
    monkeypatch.setattr(deploy, "local_docker", lambda: False)
    _motor_hashes(deploy, monkeypatch)
    line, on_hub, _changed = deploy.finish_on_hub(robot)
    assert "no Docker" in line and robot.commands == [] and on_hub is False


def test_a_failed_motor_layer_stops_before_anything_switches(deploy, hashes, monkeypatch):
    robot = _SwitchRobot()
    monkeypatch.setattr(deploy, "give_motor_layer", lambda *_a: False)
    stale = _on_v2(v2_image=_label("b" * 16))
    line, on_hub, _changed = _finish(deploy, monkeypatch, stale, robot)
    assert "motor layer" in line and on_hub is False
    assert not any("hub-stack.sh" in c for c in robot.commands)


def test_an_image_built_late_on_a_hub_robot_is_restarted_onto(deploy, hashes, monkeypatch):
    robot = _SwitchRobot()
    monkeypatch.setattr(deploy, "give_hub_payload", lambda *_a, **_k: True)
    monkeypatch.setattr(deploy, "ensure_buildenv", lambda _r: True)
    stale = _on_v2(hub_flag="yes", hub_image=_label("c" * 16), hub_base=_label(_BASE_WANTED))
    _line, on_hub, changed = _finish(deploy, monkeypatch, stale, robot)
    assert on_hub is True and changed is True
    assert any("hub-refresh.sh restart" in c for c in robot.commands)


def test_the_build_environment_is_not_tied_to_the_motor_layer(deploy, hashes, monkeypatch):
    # A robot whose motor is current can still need a hub image built.
    robot = _SwitchRobot()
    asked = []
    monkeypatch.setattr(deploy, "ensure_buildenv", lambda _r: asked.append(1) or False)
    line, on_hub, _changed = _finish(deploy, monkeypatch,
                                     _on_v2(hub_image=_label("c" * 16),
                                            hub_base=_label(_BASE_WANTED)), robot)
    assert asked == [1] and on_hub is False and "no hub image" in line


def test_the_capture_uses_the_route_the_deploy_uses(deploy, monkeypatch):
    seen = {}

    class _Route:
        user, host = "pollen", "robot"

        def ssh(self):
            return ["ssh", "-i", "key", "-J", "me@bastion", "pollen@robot"]

    def run(command, check=False):
        seen["command"] = command
        return _Ran(0)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    assert deploy.capture_buildenv(_Route()) is True
    assert "--ssh-option=-J" in seen["command"] and "--ssh-option=me@bastion" in seen["command"]


def test_a_layer_that_does_not_run_goes_back_to_what_it_replaced():
    text = (REPO / "deploy/robot/ros2_native/motor-overlay.sh").read_text(encoding="utf-8")
    assert 'sudo -n docker tag "$displaced" "$IMAGE"' in text
    # The image it replaced is removed only once the new one runs.
    assert text.index('apply v2; then') < text.index('docker image rm "$displaced"')


def test_a_hub_build_is_known_to_fail_without_docker_before_anything_is_sent(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "local_docker", lambda: False)
    plan = deploy.hold_for_docker({"action": "top", "why": ""})
    assert plan["action"] == "none" and "Docker" in plan["why"]
    assert deploy.hold_for_docker({"action": "keep", "why": "x"})["action"] == "keep"


REPO = Path(__file__).resolve().parents[1]


def test_the_motor_layer_records_a_source_the_deploy_can_read():
    text = (REPO / "deploy/robot/ros2_native/motor-overlay.sh").read_text(encoding="utf-8")
    assert '--source) [ "$#" -ge 2 ] || usage; SOURCE_HASH="$2"; shift ;;' in text
    assert 'EMORI_MOTOR_SOURCE=$SOURCE_HASH' in text


def _main_on_a_robot(deploy, monkeypatch, *, finish, ready=0):
    info = {"docker": "yes", "v2_fence": "yes", "hub_flag": "no", "sudo": "yes", "env": "yes",
            "token": "yes", "token_robot": "robot-01", "internet": "yes"}
    monkeypatch.setattr(deploy, "connect", lambda robot: None)
    monkeypatch.setattr(deploy, "preflight", lambda robot: info)
    monkeypatch.setattr(deploy, "choose_token", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "prepare_env", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "ship", lambda robot, **k: None)
    monkeypatch.setattr(deploy, "upload_env", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "plan_v2_image", lambda *a, **k: {"action": "none", "why": ""})
    monkeypatch.setattr(deploy, "plan_hub_payload", lambda *a, **k: {"action": "none", "why": ""})
    monkeypatch.setattr(deploy, "install", lambda *a: 0)
    monkeypatch.setattr(deploy, "stack_outcome", lambda robot: "")
    monkeypatch.setattr(deploy, "readycheck_warnings", lambda robot: [])
    monkeypatch.setattr(deploy, "finish_on_hub", lambda robot: finish)
    monkeypatch.setattr(deploy, "recheck_readiness", lambda robot: ready)
    monkeypatch.setattr(deploy.webbrowser, "open", lambda url: None)
    return deploy.main(["--host", "robot", "--no-token", "--no-browser", "--yes"])


def test_a_deploy_that_reached_the_hub_says_ready(deploy, monkeypatch, capsys):
    code = _main_on_a_robot(deploy, monkeypatch, finish=("ROS 2 hub: it runs.", True, True))
    assert code == 0
    assert "ROBOT READY" in capsys.readouterr().out


def test_a_deploy_that_did_not_reach_the_hub_does_not_say_ready(deploy, monkeypatch, capsys):
    code = _main_on_a_robot(deploy, monkeypatch, finish=("ROS 2 hub: !! no.", False, True))
    out = capsys.readouterr().out
    assert code == 1
    assert "ROBOT READY" not in out
    assert "NOT ON THE ROS 2 HUB" in out and "ROS 2 hub: !! no." in out


def test_readiness_is_judged_again_after_the_programs_changed(deploy, monkeypatch, capsys):
    code = _main_on_a_robot(deploy, monkeypatch, finish=("ROS 2 hub: it runs.", True, True),
                            ready=1)
    assert code == 1
    assert "ROBOT READY" not in capsys.readouterr().out


def test_no_hub_is_refused_on_a_robot_already_on_it(deploy, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "connect", lambda robot: None)
    monkeypatch.setattr(deploy, "preflight", lambda robot: {"hub_flag": "yes", "v2_fence": "yes"})
    code = deploy.main(["--host", "robot", "--no-token", "--no-hub", "--yes"])
    assert code == 1
    assert "--no-hub on a robot on the hub" in capsys.readouterr().out


def test_a_new_robot_ships_with_the_voice_delay_on():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "\nEMORI_VOICE_DELAY_MS=100\n" in text


def test_changed_core_files_need_a_top_layer_over_the_base_already_there(deploy, hashes):
    info = _info(hub_image=_label("cccccccccccccccc"), hub_base=_label(_BASE_WANTED))
    assert deploy.plan_hub_payload(info, _KEEP, builder=True)["action"] == "top"


def test_a_missing_or_stale_base_is_rebuilt_not_shipped(deploy, hashes):
    assert deploy.plan_hub_payload(_info(), _KEEP, builder=True)["action"] == "base"
    stale = _info(hub_base=_label("dddddddddddddddd"))
    assert deploy.plan_hub_payload(stale, _KEEP, builder=True)["action"] == "base"


def test_a_build_the_disk_cannot_hold_is_not_planned(deploy, hashes):
    low = _info(free_mb="900")
    plan = deploy.plan_hub_payload(low, _KEEP, builder=True)
    assert plan["action"] == "none" and "900 MB free" in plan["why"]
    enough = _info(free_mb="900", hub_base=_label(_BASE_WANTED))
    assert deploy.plan_hub_payload(enough, _KEEP, builder=True)["action"] == "top"


def test_without_a_payload_builder_the_running_hub_stays(deploy, hashes):
    plan = deploy.plan_hub_payload(_info(), _KEEP, builder=False)
    assert plan["action"] == "none" and plan["why"].startswith("!!")


def test_no_motor_image_or_no_docker_means_no_hub_image(deploy, hashes):
    assert deploy.plan_hub_payload(_info(v2_image=""), _KEEP, builder=True)["action"] == "none"
    assert deploy.plan_hub_payload(_info(docker="no"), _KEEP, builder=True)["action"] == "none"
    arriving = {"action": "ship", "why": ""}
    assert deploy.plan_hub_payload(_info(), arriving, builder=True)["action"] == "none"


@pytest.mark.parametrize("info", [
    _info(hub_image=_label("cccccccccccccccc")),
    _info(hub_base=_label("dddddddddddddddd"), free_mb="5000"),
    _info(),
])
def test_no_hub_plan_is_ever_a_full_image_ship(deploy, hashes, info):
    plan = deploy.plan_hub_payload(info, _KEEP, builder=True)
    assert plan["action"] in ("keep", "top", "base", "none")


# --- the motor image is never replaced whole on an overlay or a full card ---------------


class _Args:
    def __init__(self, **values):
        self.rebuild_v2_image = values.get("rebuild_v2_image", False)
        self.yes = True


def test_a_rebuild_is_refused_on_an_overlay_robot(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "v2_source_hash", lambda: "0123456789abcdef")
    monkeypatch.setattr(deploy, "local_docker", lambda: pytest.fail("planned a build"))
    plan = deploy.plan_v2_image(_info(v2_overlay="yes"), _Args(rebuild_v2_image=True),
                                interactive=False)
    assert plan["action"] == "keep" and "overlay" in plan["why"]


def test_a_rebuild_is_refused_on_a_low_disk_robot(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "v2_source_hash", lambda: "0123456789abcdef")
    monkeypatch.setattr(deploy, "local_docker", lambda: pytest.fail("planned a build"))
    plan = deploy.plan_v2_image(_info(free_mb="1286"), _Args(rebuild_v2_image=True),
                                interactive=False)
    assert plan["action"] == "keep" and "1286 MB free" in plan["why"]


def test_a_rebuild_with_room_and_no_overlay_is_still_possible(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "v2_source_hash", lambda: "0123456789abcdef")
    monkeypatch.setattr(deploy, "local_docker", lambda: True)
    monkeypatch.setattr(deploy, "local_v2_image", lambda: {
        "id": "sha256:cd", "source": "0123456789abcdef", "created": "", "size_mb": "1600"})
    plan = deploy.plan_v2_image(_info(free_mb="4000", v2_overlay="no"),
                                _Args(rebuild_v2_image=True), interactive=False)
    assert plan["action"] == "build"


# --- add-on wheels, found without naming any add-on ------------------------------------


def test_add_on_wheels_are_found_from_whatever_the_folder_holds(deploy, monkeypatch, tmp_path):
    (tmp_path / "addons" / "first" / "deploy" / "hub-wheels").mkdir(parents=True)
    (tmp_path / "addons" / "first" / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "addons" / "first" / "deploy" / "hub-constraints.txt").write_text(
        "", encoding="utf-8")
    (tmp_path / "addons" / "first" / "deploy" / "hub-wheels" / "x-1-py3-none-any.whl"
     ).write_bytes(b"")
    (tmp_path / "addons" / "second").mkdir()
    (tmp_path / "addons" / "second" / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(deploy, "REPO", tmp_path)
    assert deploy.find_addon_hub_wheels() == [
        {"name": "first", "wheels": True, "constraints": True},
        {"name": "second", "wheels": False, "constraints": False}]


def test_no_add_ons_means_nothing_to_find(deploy, monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "REPO", tmp_path)
    assert deploy.find_addon_hub_wheels() == []


# --- giving the payload ----------------------------------------------------------------


class _Robot:
    host = "robot.invalid"

    def __init__(self, hub_build="ok"):
        self.commands = []
        self.hub_build = hub_build

    def ssh(self):
        return ["ssh", "robot.invalid"]

    def run(self, command, timeout=0):
        self.commands.append(command)
        out = f"hub_build={self.hub_build}\n" if "hub-build.sh" in command else ""
        return subprocess.CompletedProcess(command, 0, out.encode(), b"")


def _stub_shipping(deploy, monkeypatch, *, built=True):
    sent = []
    monkeypatch.setattr(deploy, "hub_source_hash", lambda: _WANTED)
    monkeypatch.setattr(deploy, "hub_base_source_hash", lambda: _BASE_WANTED)
    monkeypatch.setattr(deploy, "find_addon_hub_wheels", lambda: [])

    def build(action, destination):
        if built:
            destination.write_bytes(b"tar")
        return built

    monkeypatch.setattr(deploy, "build_hub_payload", build)
    monkeypatch.setattr(deploy.subprocess, "run", lambda command, **kw: sent.append(
        (command, kw.get("input"))) or subprocess.CompletedProcess(command, 0, b"", b""))
    return sent


def test_a_top_build_sends_the_payload_then_builds_with_the_hash(deploy, monkeypatch):
    sent = _stub_shipping(deploy, monkeypatch)
    robot = _Robot()
    assert deploy.give_hub_payload(robot, {"action": "top", "why": ""}) is True
    assert sent[0][1] == b"tar" and "hub-build" in sent[0][0][-1]
    assert robot.commands[-1].endswith(f"hub-build.sh top {_WANTED}")


def test_a_base_build_passes_the_base_hash_too(deploy, monkeypatch):
    _stub_shipping(deploy, monkeypatch)
    robot = _Robot()
    assert deploy.give_hub_payload(robot, {"action": "base", "why": ""}) is True
    assert robot.commands[-1].endswith(f"hub-build.sh base {_WANTED} {_BASE_WANTED}")


def test_a_failed_build_reports_false_and_the_hub_stays(deploy, monkeypatch):
    _stub_shipping(deploy, monkeypatch)
    assert deploy.give_hub_payload(_Robot("failed"), {"action": "top", "why": ""}) is False
    _stub_shipping(deploy, monkeypatch, built=False)
    robot = _Robot()
    assert deploy.give_hub_payload(robot, {"action": "top", "why": ""}) is False
    assert not any("hub-build.sh" in command for command in robot.commands)


def test_keep_and_none_touch_nothing(deploy, monkeypatch):
    sent = _stub_shipping(deploy, monkeypatch)
    robot = _Robot()
    assert deploy.give_hub_payload(robot, {"action": "keep", "why": ""}) is True
    assert robot.commands == [] and sent == []
    idle = _Robot()
    assert deploy.give_hub_payload(idle, {"action": "none", "why": ""}) is False
    assert idle.commands == []


def test_an_add_on_with_pins_and_no_wheels_is_called_out(deploy, monkeypatch, capsys):
    _stub_shipping(deploy, monkeypatch)
    monkeypatch.setattr(deploy, "find_addon_hub_wheels", lambda: [
        {"name": "thing", "wheels": False, "constraints": True}])
    deploy.give_hub_payload(_Robot(), {"action": "keep", "why": ""})
    assert "add-on thing pins hub dependencies" in capsys.readouterr().out
    deploy.give_hub_payload(_Robot(), {"action": "keep", "why": ""}, addons=False)
    assert capsys.readouterr().out == ""


# --- the robot-side build script and the sim overlay -----------------------------------


def test_the_build_script_cleans_up_from_a_trap_and_never_prunes_images():
    text = (_HUB_DIR / "hub-build.sh").read_text(encoding="utf-8")
    assert "trap clean EXIT" in text
    assert "builder prune" in text
    assert "image prune" not in text and "docker save" not in text
    assert "--pull=false" in text


def test_a_signal_ends_the_build_along_with_the_script():
    """``timeout`` puts the build in a process group of its own, so a signal to the script leaves
    it running: it retags what it built, and its cache outlives the prune."""
    text = (_HUB_DIR / "hub-build.sh").read_text(encoding="utf-8")
    clean = text[text.index("clean() {"):text.index("trap clean EXIT")]
    assert clean.index('pkill -TERM -P "$$"') < clean.index('rm -rf "$PAYLOAD"')
    assert clean.index('pkill -TERM -P "$$"') < clean.index("builder prune")


def test_a_rebuild_that_made_the_same_image_keeps_the_real_previous_one():
    """Otherwise :prev becomes :robot, and the image to go back to is gone with the next rmi."""
    text = (_HUB_DIR / "hub-build.sh").read_text(encoding="utf-8")
    restore = text.index('docker tag "$old_prev" "$HUB_PREV"')
    assert text.index('--build-arg "EMORI_HUB_SOURCE') < restore
    assert restore < text.index('docker rmi "$old_prev"')


def test_a_rebuilt_base_does_not_leave_the_old_one_on_the_card():
    """Nothing tags the old base, so no later build removes it, and the hub images keep only the
    layers they use. The rest would stay on a card that has little to spare."""
    text = (_HUB_DIR / "hub-build.sh").read_text(encoding="utf-8")
    assert 'old_base="$(docker image inspect' in text
    assert 'docker rmi "$old_base"' in text


def test_the_build_script_thresholds_match_the_deploy(deploy):
    text = (_HUB_DIR / "hub-build.sh").read_text(encoding="utf-8")
    for name in ("FLOOR_MB", "NEED_TOP_MB", "NEED_BASE_MB"):
        value = re.search(rf"^{name}=(\d+)$", text, re.MULTILINE)
        assert value is not None
        assert int(value.group(1)) == getattr(deploy, "HUB_" + name)


def _sim():
    return yaml.safe_load((_HUB_DIR / "docker-compose.hub.sim.yml").read_text(
        encoding="utf-8"))["services"]


def test_the_sim_hub_shares_the_node_service_network():
    sim = _sim()
    assert sim["hub"]["network_mode"] == "service:ros2v2"
    assert "ports" not in sim["hub"]
    assert any("8095" in port for port in sim["ros2v2"]["ports"])
    assert sim["hub"]["ipc"] == "private"


def test_the_sim_hub_and_node_read_the_same_dds_profile():
    sim = _sim()
    profile = "FASTRTPS_DEFAULT_PROFILES_FILE=/etc/emori/fastdds_peers.xml"
    assert profile in sim["hub"]["environment"]
    assert profile in sim["ros2v2"]["environment"]
    assert "./fastdds_peers.xml:/etc/emori/fastdds_peers.xml:ro" in sim["ros2v2"]["volumes"]


def test_the_sim_overlay_adds_services_to_the_node_file_and_changes_none_of_its_image():
    node = yaml.safe_load((_ROOT / "deploy" / "ros2" / "docker-compose.v2.yml").read_text(
        encoding="utf-8"))["services"]
    sim = _sim()
    assert set(sim) == {"ros2v2", "hub"} and "ros2v2" in node
    assert "image" not in sim["ros2v2"] and "build" not in sim["ros2v2"]
    assert "hub" not in node


def test_sim_full_up_has_a_hub_option_that_uses_the_overlay():
    text = (_ROOT / "scripts" / "sim_full_up.ps1").read_text(encoding="utf-8")
    assert "[switch]$Hub" in text
    assert "deploy/hub/docker-compose.hub.sim.yml" in text


def test_the_deploy_gives_the_robot_half_the_hash_it_compares(deploy, monkeypatch):
    seen = []
    monkeypatch.setattr(deploy.subprocess, "run", lambda command, **kw: seen.append(command)
                        or subprocess.CompletedProcess(command, 0, b"", b""))
    monkeypatch.setattr(deploy, "_has_keyboard", lambda: False)

    class Robot:
        def ssh(self, tty=False):
            return ["ssh", "robot"]

    assert deploy.install(Robot(), {"commit": "abc", "branch": "b"}, False,
                          hub_source=_WANTED) == 0
    assert f"--hub-source {_WANTED}" in seen[0][-1]
    assert deploy.install(Robot(), {"commit": "abc", "branch": "b"}, False) == 0
    assert "--hub-source" not in seen[1][-1]


def test_nothing_new_names_a_plan_identifier():
    banned = re.compile(r"\b(T\d\d|S-\d\d|SP-\d\d|SG-\d)\b|2026-\d\d-\d\d", re.I)
    for path in (_HUB_DIR / "hub-build.sh", _HUB_DIR / "docker-compose.hub.sim.yml"):
        assert banned.search(path.read_text(encoding="utf-8")) is None, path
