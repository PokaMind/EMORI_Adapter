"""Readycheck on a robot whose gateway is the hub container.

The hub flag adds three things to the self-check: the ``ros2`` gate asks the hub's
container too, ``hub_identity`` says which gateway answers and whether the hub is wired the
way a container needs, and ``hub_audio`` covers the container's path to PipeWire. Every
remedy that says "restart" names the unit that is running. Without the flag the robot is
judged exactly as before and shells out for none of it.
"""

from __future__ import annotations

import os
import sys

import pytest

from emori_adapter.hostapp import readycheck
from emori_adapter.hostapp.readycheck import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    CommandResult,
    HttpResult,
    collect,
    evaluate,
)
from test_readycheck import _by_name, _FakeProbe, _healthy, _NODE_LIST, _ros2_v2

HUB_NODE_LIST = _NODE_LIST + "/reachy_mini\n/emori/robot_link\n"
HEALTHY_HUB_HEALTHZ = {"status": "ok", "demo": False, "partner": "emori-robot",
                       "pokamind_credential": "robot_token", "roster_staff": 2,
                       "robot": True, "runtime": "ros"}
ROBOT_STATUS = {"state": "running", "runtime": "ros", "motion_owner": "ros",
                "skill_transport": "ros_native"}
APP_STATUS = {"mic_profile": "idle", "voice_link": {"enabled": True, "available": True}}
SINK_LINE = '\t\tnode.name = "emori_voice"\n'


def _hub(**overrides):
    """A v2 robot whose gateway is the hub, with the whole second container healthy."""

    base = dict(
        hub_mode=True,
        v2_fence=True,
        healthz=HttpResult(True, 200, dict(HEALTHY_HUB_HEALTHZ)),
        ros2_nodes=CommandResult(True, 0, HUB_NODE_LIST),
        hub_container=CommandResult(True, 0, "true\n"),
        hub_fiware=CommandResult(True, 0, "0\n"),
        hub_cache=CommandResult(True, 0, ""),
        hub_status=HttpResult(True, 200, dict(ROBOT_STATUS)),
        app_status=HttpResult(True, 200, {**APP_STATUS, "voice_link": dict(
            APP_STATUS["voice_link"])}),
        hub_addon_status=HttpResult(True, 503, {"error_code": "addon_unavailable"}),
        host_has_addon=False,
    )
    base.update(overrides)
    return _ros2_v2(**base)


def test_a_hub_on_the_deploy_on_disk_passes():
    gate = _gates(deployed_commit="abc1234 (x)",
                  hub_status=HttpResult(True, 200, {**ROBOT_STATUS,
                                                    "hub_deployed": "abc1234 (x)"}))
    assert gate["hub_identity"].status == PASS


def test_a_hub_left_running_across_a_deploy_is_named():
    """A deploy leaves a hub with a session running, and nothing else would say so afterwards."""

    gate = _gates(deployed_commit="def5678901234 (x)",
                  hub_status=HttpResult(True, 200, {**ROBOT_STATUS,
                                                    "hub_deployed": "abc1234567890 (x)"})
                  )["hub_identity"]
    assert (gate.status, gate.code) == (WARN, "hub_restart_pending")
    assert "abc123456789" in gate.hint and "def567890123" in gate.hint
    assert "systemctl restart emori-hub" in gate.hint


def test_a_hub_that_does_not_say_what_it_started_on_is_named_too():
    gate = _gates(deployed_commit="def5678901234 (x)")["hub_identity"]
    assert (gate.status, gate.code) == (WARN, "hub_restart_pending")
    assert "does not say" in gate.hint


@pytest.mark.parametrize("marker,status", [("", HttpResult(True, 200, dict(ROBOT_STATUS))),
                                           ("abc", HttpResult(False))])
def test_with_nothing_to_compare_the_gate_does_not_guess(marker, status):
    gate = _gates(deployed_commit=marker, hub_status=status)["hub_identity"]
    assert gate.code != "hub_restart_pending"


@pytest.mark.parametrize("hub", [True, False])
def test_a_voice_tap_delivering_in_blocks_is_named_on_either_gateway(hub):
    make = _hub if hub else _healthy
    status = {"voice_link": {"enabled": True, "available": True, "frame_gap_ms": 0.1}}
    gate = _by_name(evaluate(make(app_status=HttpResult(True, 200, status))))["voice_tap"]
    assert (gate.status, gate.code) == (WARN, "voice_tap_in_blocks")
    assert "128 ms" in gate.hint and "stdbuf" in gate.hint


def test_a_dead_voice_tap_is_named_though_its_last_gaps_look_live():
    """The gaps stay behind when pw-record dies, and on a host app no other gate looks at the tap."""
    status = {**APP_STATUS, "voice_link": {"enabled": True, "available": False,
                                           "frame_gap_ms": 21.3}}
    gate = _by_name(evaluate(_healthy(app_status=HttpResult(True, 200, status))))["voice_tap"]
    assert (gate.status, gate.code) == (WARN, "voice_tap_down")
    assert "fixed pacing" in gate.hint and "emori-hostapp" in gate.hint


@pytest.mark.parametrize("gap,code", [(21.3, "voice_tap_live"), (None, "voice_tap_not_reporting")])
def test_a_live_tap_or_one_that_says_nothing_passes(gap, code):
    status = {"voice_link": {"enabled": True, "available": True, "frame_gap_ms": gap}}
    gate = _by_name(evaluate(_healthy(app_status=HttpResult(True, 200, status))))["voice_tap"]
    assert (gate.status, gate.code) == (PASS, code)
    assert _by_name(evaluate(_healthy()))["voice_tap"].status == PASS


def _gates(**overrides):
    return _by_name(evaluate(_hub(**overrides)))


# --------------------------------------------------------------------------
# without the flag: nothing changes
# --------------------------------------------------------------------------
def test_off_the_hub_both_new_gates_pass_without_asking_anything():
    gates = _by_name(evaluate(_healthy()))

    assert (gates["hub_identity"].status, gates["hub_identity"].code) == (PASS, "hub_off")
    assert (gates["hub_audio"].status, gates["hub_audio"].code) == (PASS, "hub_audio_off")


def test_a_hub_answering_behind_a_lowered_flag_is_a_failure():
    gate = _by_name(evaluate(_healthy(healthz=HttpResult(
        True, 200, dict(HEALTHY_HUB_HEALTHZ)))))["hub_identity"]

    assert (gate.status, gate.code) == (FAIL, "hub_answering_without_flag")
    assert "emori-hub" in gate.hint


def test_collect_asks_nothing_about_the_hub_without_the_flag():
    probe = _FakeProbe({})

    evidence = collect(probe)

    assert evidence.hub_mode is False
    assert probe.argv == [["v4l2-ctl", "-d", "/dev/video40", "--list-formats-ext"]]
    assert not any(url.endswith(("/api/robot/status", "/api/addon/status")) for url in probe.urls)
    # The voice tap's status is asked of either gateway.
    assert any(url.endswith("/api/status") for url in probe.urls)


# --------------------------------------------------------------------------
# collect: the flag arms the second container
# --------------------------------------------------------------------------
class _HubProbe(_FakeProbe):
    def __init__(self, environ, flag: bool = True, **kw):
        super().__init__(environ, **kw)
        self._flag = flag

    def path_exists(self, path: str) -> bool:
        return self._flag and path == readycheck._HUB_FLAG


def test_collect_with_the_flag_asks_the_hub_container_and_its_gateway(monkeypatch):
    monkeypatch.setattr(readycheck._extensions, "extension_class", lambda: None)
    inspect = ("docker", "inspect", "-f", "{{.State.Running}}", "emori-hub")
    probe = _HubProbe(
        {"HOSTAPP_PORT": "8090"},
        commands={inspect: CommandResult(True, 0, "true\n"),
                  ("docker", "exec", "emori-hub", "printenv", "FIWARE_BRIDGE_ENABLED"):
                      CommandResult(True, 0, "0\n")})

    evidence = collect(probe)

    assert evidence.hub_mode is True and evidence.ros2_enabled is True
    assert list(inspect) in probe.argv
    assert ["docker", "exec", "emori-hub", "printenv", "FIWARE_BRIDGE_ENABLED"] in probe.argv
    assert ["docker", "exec", "emori-hub", "test", "-w", "/home/pollen/.cache/emori"] \
        in probe.argv
    for path in ("/api/robot/status", "/api/status", "/api/addon/status"):
        assert f"http://127.0.0.1:8090{path}" in probe.urls
    assert evidence.host_has_addon is False


def test_collect_does_not_exec_into_a_hub_that_is_not_running(monkeypatch):
    monkeypatch.setattr(readycheck._extensions, "extension_class", lambda: None)
    inspect = ("docker", "inspect", "-f", "{{.State.Running}}", "emori-hub")
    probe = _HubProbe({}, commands={inspect: CommandResult(True, 1, "")})

    collect(probe)

    assert not any(argv[:3] == ["docker", "exec", "emori-hub"] for argv in probe.argv)


def test_collect_reads_the_transport_the_host_asks_for(monkeypatch):
    monkeypatch.setattr(readycheck._extensions, "extension_class", lambda: None)

    evidence = collect(_HubProbe({"EMORI_ROBOT_TRANSPORT": " HTTP "}))

    assert evidence.robot_transport == "http"


# --------------------------------------------------------------------------
# a healthy hub
# --------------------------------------------------------------------------
def test_a_healthy_hub_passes_all_three_gates():
    gates = _gates()

    assert (gates["ros2"].status, gates["ros2"].code) == (PASS, "ros2_up_v2")
    assert (gates["hub_identity"].status, gates["hub_identity"].code) == (PASS, "hub_ros")
    assert (gates["hub_audio"].status, gates["hub_audio"].code) == (PASS, "hub_audio_ok")


# --------------------------------------------------------------------------
# hub_identity
# --------------------------------------------------------------------------
def test_the_host_app_answering_behind_a_raised_flag_is_a_failure():
    gate = _gates(healthz=HttpResult(True, 200, {**HEALTHY_HUB_HEALTHZ, "runtime": "http"})
                  )["hub_identity"]

    assert (gate.status, gate.code) == (FAIL, "hub_wrong_runtime")
    assert "emori-hub" in gate.hint


def test_a_gateway_that_does_not_report_a_runtime_is_the_wrong_one():
    healthz = {k: v for k, v in HEALTHY_HUB_HEALTHZ.items() if k != "runtime"}

    gate = _gates(healthz=HttpResult(True, 200, healthz))["hub_identity"]

    assert (gate.status, gate.code) == (FAIL, "hub_wrong_runtime")


def test_the_runtime_has_to_match_the_transport_the_host_asks_for():
    http_runtime = HttpResult(True, 200, {**HEALTHY_HUB_HEALTHZ, "runtime": "http"})

    assert _gates(healthz=http_runtime, robot_transport="http")["hub_identity"].code == "hub_http"
    gate = _gates(robot_transport="http")["hub_identity"]
    assert (gate.status, gate.code) == (FAIL, "hub_wrong_runtime")


def test_a_transport_the_hub_would_refuse_to_start_on_is_named():
    gate = _gates(robot_transport="rosz")["hub_identity"]

    assert (gate.status, gate.code) == (FAIL, "hub_transport_invalid")


def test_an_unreachable_gateway_is_unknown_not_wrong():
    gate = _gates(healthz=HttpResult(False))["hub_identity"]

    assert (gate.status, gate.code) == (SKIP, "hostapp_unreachable")


def test_a_cache_directory_the_container_cannot_write_is_a_failure():
    gate = _gates(hub_cache=CommandResult(True, 1, ""))["hub_identity"]

    assert (gate.status, gate.code) == (FAIL, "hub_cache_not_writable")
    assert "/home/pollen/.cache/emori" in gate.hint


def test_an_unreadable_cache_check_is_not_a_failure():
    gate = _gates(hub_cache=CommandResult(False))["hub_identity"]

    assert gate.status == PASS


def test_an_add_on_the_host_finds_but_the_hub_does_not_is_a_failure():
    gate = _gates(host_has_addon=True)["hub_identity"]

    assert (gate.status, gate.code) == (FAIL, "hub_addon_missing")
    assert "hub-site" in gate.hint


def test_an_add_on_the_hub_reports_that_the_host_cannot_find_is_a_failure():
    gate = _gates(hub_addon_status=HttpResult(True, 200, {"mode": "idle"}),
                  host_has_addon=False)["hub_identity"]

    assert (gate.status, gate.code) == (FAIL, "hub_addon_unexpected")


def test_an_add_on_both_sides_agree_on_passes():
    both = _gates(hub_addon_status=HttpResult(True, 200, {"mode": "idle"}),
                  host_has_addon=True)["hub_identity"]
    neither = _gates()["hub_identity"]

    assert both.status == PASS and neither.status == PASS


def test_no_answer_about_the_add_on_is_not_judged():
    gate = _gates(hub_addon_status=HttpResult(False), host_has_addon=True)["hub_identity"]

    assert gate.status == PASS


# --------------------------------------------------------------------------
# hub_audio
# --------------------------------------------------------------------------
def test_a_voice_tap_that_delivers_nothing_is_a_failure():
    status = {**APP_STATUS, "voice_link": {"enabled": True, "available": False}}

    gate = _gates(app_status=HttpResult(True, 200, status))["hub_audio"]

    assert (gate.status, gate.code) == (FAIL, "hub_voice_link_down")
    assert "emori-hub" in gate.hint


def test_a_voice_tap_switched_off_on_purpose_is_not_a_failure():
    status = {**APP_STATUS, "voice_link": {"enabled": False, "available": False}}

    assert _gates(app_status=HttpResult(True, 200, status))["hub_audio"].status == PASS


@pytest.mark.parametrize("profile", [None, ""])
def test_a_capture_gain_that_never_took_is_a_failure(profile):
    status = {**APP_STATUS, "mic_profile": profile}

    gate = _gates(app_status=HttpResult(True, 200, status))["hub_audio"]

    assert (gate.status, gate.code) == (FAIL, "hub_mic_profile_unset")


def test_an_unreachable_gateway_leaves_the_audio_unknown():
    gate = _gates(app_status=HttpResult(False))["hub_audio"]

    assert (gate.status, gate.code) == (SKIP, "hostapp_unreachable")


def test_with_a_voice_delay_the_sink_the_tap_listens_on_must_exist():
    missing = _gates(voice_delay_ms="120",
                     voice_nodes=CommandResult(True, 0, "node.name = \"other\"\n"))["hub_audio"]
    present = _gates(voice_delay_ms="120",
                     voice_nodes=CommandResult(True, 0, SINK_LINE))["hub_audio"]
    unreadable = _gates(voice_delay_ms="120", voice_nodes=CommandResult(False))["hub_audio"]

    assert (missing.status, missing.code) == (FAIL, "hub_audio_sink_missing")
    assert present.status == PASS
    assert (unreadable.status, unreadable.code) == (SKIP, "voice_nodes_unreadable")


def test_without_a_voice_delay_the_sink_is_not_asked_for():
    assert _gates(voice_nodes=CommandResult(True, 0, ""))["hub_audio"].status == PASS


# --------------------------------------------------------------------------
# ros2: the second container
# --------------------------------------------------------------------------
@pytest.mark.parametrize("container, code", [
    (CommandResult(True, 1, ""), "ros2_hub_container_absent"),
    (CommandResult(True, 0, "false\n"), "ros2_hub_container_stopped"),
])
def test_a_hub_container_that_is_not_running_is_a_failure(container, code):
    gate = _gates(hub_container=container)["ros2"]

    assert (gate.status, gate.code) == (FAIL, code)
    assert "systemctl status emori-hub" in gate.hint


def test_the_fiware_bridge_must_be_off_in_the_hub_too():
    gate = _gates(hub_fiware=CommandResult(True, 0, "1\n"))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_hub_fiware_bridge_on")


def test_a_hub_whose_link_is_not_ros_is_a_failure():
    status = {**ROBOT_STATUS, "runtime": "http"}

    gate = _gates(hub_status=HttpResult(True, 200, status))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_hub_not_ros")


def test_a_hub_status_that_is_refused_is_not_a_ros_link_either():
    gate = _gates(hub_status=HttpResult(True, 403, {"error": "local_only"}))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_hub_not_ros")


def test_the_owner_is_compared_across_all_three_sources():
    drift = _gates(hub_status=HttpResult(True, 200, {**ROBOT_STATUS, "motion_owner": "hostapp"})
                   )["ros2"]
    agree = _gates()["ros2"]

    assert (drift.status, drift.code) == (FAIL, "ros2_owner_drift")
    assert "hub reports 'hostapp'" in drift.hint
    assert agree.code == "ros2_up_v2"


def test_the_hubs_node_has_to_be_in_the_graph_the_motor_container_sees():
    gate = _gates(ros2_nodes=CommandResult(True, 0, _NODE_LIST + "/reachy_mini\n"))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_hub_node_missing")


def test_the_transport_comes_from_the_hub_process_not_from_docker_exec():
    """A hub whose process says it does not use the native transport needs the intake."""

    status = {**ROBOT_STATUS, "skill_transport": ""}

    gate = _gates(hub_status=HttpResult(True, 200, status),
                  skill_api=HttpResult(True, 404, {}))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "skill_api_not_mounted")
    assert "emori-hub" in gate.hint


def test_an_unreachable_hub_gateway_leaves_the_ros2_gate_unknown():
    gate = _gates(hub_status=HttpResult(False))["ros2"]

    assert (gate.status, gate.code) == (SKIP, "hostapp_unreachable")


# --------------------------------------------------------------------------
# remedies name the unit that runs
# --------------------------------------------------------------------------
def _restart_hints(ev):
    return [r.hint for r in evaluate(ev) if r.hint and "restart" in r.hint.lower()]


def test_off_the_hub_every_restart_remedy_names_the_host_app():
    for hint in _restart_hints(_healthy(healthz=HttpResult(False))):
        assert "emori-hub" not in hint


@pytest.mark.parametrize("unit, flag", [("emori-hostapp", False), ("emori-hub", True)])
def test_the_remedy_for_a_dead_gateway_names_the_active_unit(unit, flag):
    ev = _healthy(healthz=HttpResult(False), hub_mode=flag)

    gate = _by_name(evaluate(ev))["hostapp"]

    assert f"systemctl restart {unit}`" in gate.hint
    other = "emori-hostapp" if flag else "emori-hub"
    assert other not in gate.hint


def test_the_services_tab_label_follows_the_unit():
    assert readycheck._gateway_label(_healthy(hub_mode=True)) == "Hub"
    assert readycheck._gateway_label(_healthy()) == "Host app"


def test_no_remedy_in_the_module_hardcodes_the_host_apps_unit():
    """Every `restart ...` hint goes through the helper, or it sends an operator on the hub
    to a start that exits 0 and changes nothing."""

    source = open(readycheck.__file__, encoding="utf-8").read()
    lines = [n for n, line in enumerate(source.splitlines(), 1)
             if "emori-hostapp" in line and "_HOSTAPP_UNIT" not in line
             and not line.lstrip().startswith("#")]

    # The constant, the gate docstrings and the hub's own hints may name it; a hint
    # string literal may not.
    for number in lines:
        text = source.splitlines()[number - 1]
        assert not text.lstrip().startswith(('"', 'f"')) or "systemctl status" in text, text


def test_rig_smoke_names_the_active_unit(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "rig_smoke.py"
    spec = importlib.util.spec_from_file_location("rig_smoke_under_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    flag = tmp_path / "HUB_IS_ROS"
    monkeypatch.setenv("EMORI_HUB_FLAG", str(flag))

    assert module.gateway_unit() == "emori-hostapp"
    flag.write_text("", encoding="utf-8")
    assert module.gateway_unit() == "emori-hub"
    assert os.path.exists(str(flag))
