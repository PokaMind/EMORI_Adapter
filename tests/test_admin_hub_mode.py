"""emori-admin on a robot whose gateway is the hub container.

With the flag file up, the hub unit replaces the host application's: it is the one listed,
restarted and rolled back, and its restart is sent without blocking and then waited for,
because the hub's unit holds "started" until its gateway answers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from emori_adapter.admin import page, server
from emori_adapter.admin import system as system_mod
from test_admin_service_fakes import PIN, base_env, http_ok, install

HUB = "emori-hub.service"
HOSTAPP = "emori-hostapp.service"
NO_BLOCK_RESTART = ["sudo", "systemctl", "--no-block", "restart", HUB]
IDLE = {"lang": "ca", "allowed_langs": ["ca", "en"], "mic_profile": None,
        "session_active": False, "addon_mode": "idle", "idle": True}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, token="tok-123", state_file=str(tmp_path / "admin_state.json"))
    run, http = install(monkeypatch)
    monkeypatch.setenv("EMORI_HUB_FLAG", str(tmp_path / "HUB_IS_ROS"))
    monkeypatch.chdir(tmp_path)
    return run, http, tmp_path


def _raise_flag(tmp_path: Path) -> None:
    (tmp_path / "HUB_IS_ROS").write_text("", encoding="utf-8")


def _client():
    from starlette.testclient import TestClient

    client = TestClient(server.create_app(background=False))
    assert client.post("/api/login", json={"pin": PIN}).status_code == 204
    return client


def _script() -> str:
    return os.path.join(os.getcwd(), system_mod.HUB_STACK_SCRIPT)


class _Clock:
    """A monotonic clock that only moves when the code sleeps."""

    def __init__(self, monkeypatch) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        monkeypatch.setattr(system_mod, "_monotonic", lambda: self.now)
        monkeypatch.setattr(system_mod, "_sleep", self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


# --------------------------------------------------------------------------
# which unit is the gateway
# --------------------------------------------------------------------------
def test_without_the_flag_the_host_app_is_the_gateway(env):
    assert system_mod.hub_mode() is False
    assert system_mod.host_unit() == HOSTAPP
    assert HUB not in system_mod.listed_units()
    assert HOSTAPP in system_mod.listed_units()


def test_with_the_flag_the_hub_is_the_gateway_and_the_host_app_is_not_listed(env):
    _raise_flag(env[2])

    assert system_mod.hub_mode() is True
    assert system_mod.host_unit() == HUB
    assert HOSTAPP not in system_mod.listed_units()
    assert HUB in system_mod.listed_units()


def test_the_services_list_asks_systemd_about_the_units_that_can_run_here(env):
    run, _, tmp_path = env
    _raise_flag(tmp_path)
    run.add(["systemctl", "show"],
            stdout="Id=%s\nActiveState=active\nSubState=running\n" % HUB)

    body = _client().get("/api/services").json()

    asked = run.argv_for("systemctl", "show")[0]
    assert HUB in asked and HOSTAPP not in asked
    assert [row["unit"] for row in body["services"]][0] == HUB
    assert body["hub_mode"] is True and body["hub_graph"] is False


def test_the_services_list_says_the_graph_is_in_the_hub_from_the_motor_env_file(env):
    run, _, tmp_path = env
    _raise_flag(tmp_path)
    (tmp_path / "deploy" / "ros2").mkdir(parents=True)
    (tmp_path / "deploy" / "ros2" / ".env").write_text(
        "EMORI_ROS_MOTION_OWNER=hostapp\nEMORI_MOTOR_LAUNCH=robot.launch.py\n", encoding="utf-8")
    run.add(["systemctl", "show"], stdout="Id=%s\nActiveState=active\n" % HUB)

    assert _client().get("/api/services").json()["hub_graph"] is True


def test_the_graph_setting_in_the_host_env_says_nothing_about_the_motor(env):
    run, _, tmp_path = env
    _raise_flag(tmp_path)
    (tmp_path / "deploy" / "ros2").mkdir(parents=True)
    (tmp_path / "deploy" / "ros2" / ".env").write_text(
        "EMORI_MOTOR_LAUNCH=v2.launch.py\n", encoding="utf-8")
    run.add(["systemctl", "show"], stdout="Id=%s\nActiveState=active\n" % HUB)

    assert _client().get("/api/services").json()["hub_graph"] is False


def test_off_the_hub_the_services_list_is_what_it_was(env):
    run, _, _ = env
    run.add(["systemctl", "show"], stdout="Id=%s\nActiveState=active\n" % HOSTAPP)

    body = _client().get("/api/services").json()

    assert [row["unit"] for row in body["services"]] == list(
        unit for unit in system_mod.ALLOWED_UNITS if unit != HUB)
    assert body["hub_mode"] is False and body["hub_graph"] is False


# --------------------------------------------------------------------------
# the hub's restart: --no-block, then wait for /healthz
# --------------------------------------------------------------------------
def test_a_hub_restart_is_sent_without_blocking_and_waited_for(env, monkeypatch):
    run, _, _ = env
    clock = _Clock(monkeypatch)
    facts = iter([("active", "T0"), ("deactivating", "T0"), ("activating", ""),
                  ("active", "T1")])
    monkeypatch.setattr(system_mod, "_unit_facts", lambda unit: next(facts))
    monkeypatch.setattr(system_mod, "_gateway_answers", lambda: True)
    run.add(NO_BLOCK_RESTART)

    outcome = system_mod.service_action(HUB, "restart")

    assert outcome == {"ok": True, "unit": HUB, "action": "restart", "deferred": False}
    assert run.argv_for("sudo") == [NO_BLOCK_RESTART]
    assert len(clock.slept) == 2


def test_the_old_process_answering_is_not_a_finished_restart(env, monkeypatch):
    """/healthz answers until the stop lands; only a new start time counts."""

    run, _, _ = env
    _Clock(monkeypatch)
    facts = iter([("active", "T0"), ("active", "T0"), ("active", "T0"), ("active", "T1")])
    monkeypatch.setattr(system_mod, "_unit_facts", lambda unit: next(facts))
    monkeypatch.setattr(system_mod, "_gateway_answers", lambda: True)
    run.add(NO_BLOCK_RESTART)

    assert system_mod.service_action(HUB, "restart")["ok"] is True


def test_a_hub_that_never_comes_back_is_reported_after_the_wait(env, monkeypatch):
    run, _, _ = env
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(system_mod, "_unit_facts", lambda unit: ("activating", ""))
    monkeypatch.setattr(system_mod, "_gateway_answers", lambda: False)
    run.add(NO_BLOCK_RESTART)

    outcome = system_mod.service_action(HUB, "restart")

    assert outcome["ok"] is False and outcome["error"] == "hub_not_ready"
    assert clock.now >= system_mod.HUB_START_WAIT_S


def test_a_stop_is_not_waited_for(env, monkeypatch):
    run, _, _ = env
    _Clock(monkeypatch)
    monkeypatch.setattr(system_mod, "_unit_facts", lambda unit: ("active", "T0"))
    run.add(["sudo", "systemctl", "--no-block", "stop", HUB])

    outcome = system_mod.service_action(HUB, "stop")

    assert outcome["ok"] is True
    assert run.argv_for("sudo") == [["sudo", "systemctl", "--no-block", "stop", HUB]]


def test_starting_a_hub_that_is_already_running_returns_at_once(env, monkeypatch):
    run, _, _ = env
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(system_mod, "_unit_facts", lambda unit: ("active", "T0"))
    run.add(["sudo", "systemctl", "--no-block", "start", HUB])

    assert system_mod.service_action(HUB, "start")["ok"] is True
    assert clock.slept == []


def test_a_refused_systemctl_is_a_failure_and_nothing_is_waited_for(env, monkeypatch):
    run, _, _ = env
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(system_mod, "_unit_facts", lambda unit: ("failed", ""))
    run.add(NO_BLOCK_RESTART, returncode=1, stderr="Unit not found")

    outcome = system_mod.service_action(HUB, "restart")

    assert outcome["ok"] is False and outcome["detail"] == "Unit not found"
    assert clock.slept == []


def test_other_units_are_still_restarted_the_way_they_were(env):
    run, _, _ = env
    run.add(["sudo", "systemctl", "restart", "emori-kiosk.service"])

    assert system_mod.service_action("emori-kiosk.service", "restart")["ok"] is True
    assert run.argv_for("sudo") == [["sudo", "systemctl", "restart", "emori-kiosk.service"]]


# --------------------------------------------------------------------------
# the admin's own restarts go to the unit that runs the gateway
# --------------------------------------------------------------------------
def test_applying_settings_restarts_the_hub_when_the_hub_runs_the_gateway(env, monkeypatch):
    run, http, tmp_path = env
    _raise_flag(tmp_path)
    seen: list[tuple] = []
    monkeypatch.setattr(system_mod, "service_action",
                        lambda unit, action: seen.append((unit, action)) or {"ok": True})
    http.add("GET", "/api/admin/summary", http_ok(IDLE))

    reply = _client().post("/api/settings/apply")

    assert reply.status_code == 200
    assert seen == [(HUB, "restart")]


def test_applying_settings_restarts_the_host_app_otherwise(env, monkeypatch):
    _, http, _ = env
    seen: list[tuple] = []
    monkeypatch.setattr(system_mod, "service_action",
                        lambda unit, action: seen.append((unit, action)) or {"ok": True})
    http.add("GET", "/api/admin/summary", http_ok(IDLE))

    assert _client().post("/api/settings/apply").status_code == 200
    assert seen == [(HOSTAPP, "restart")]


def test_the_hub_unit_is_allowed_through_the_action_route(env, monkeypatch):
    run, _, tmp_path = env
    _raise_flag(tmp_path)
    monkeypatch.setattr(system_mod, "_unit_facts", lambda unit: ("inactive", "T0"))
    run.add(["sudo", "systemctl", "--no-block", "stop", HUB])

    reply = _client().post("/api/services/action", json={"unit": HUB, "action": "stop"})

    assert reply.status_code == 200 and reply.json()["unit"] == HUB


# --------------------------------------------------------------------------
# the way back to the host app
# --------------------------------------------------------------------------
def test_the_way_back_is_refused_when_the_hub_is_not_the_gateway(env):
    run, _, _ = env

    reply = _client().post("/api/hub/rollback")

    assert reply.status_code == 409 and reply.json()["error"] == "not_hub_mode"
    assert not run.ran("bash")


def test_the_way_back_is_refused_while_the_hub_answers(env):
    run, http, tmp_path = env
    _raise_flag(tmp_path)
    http.add("GET", "/healthz", http_ok({"status": "ok", "runtime": "ros"}))

    reply = _client().post("/api/hub/rollback")

    assert reply.status_code == 409 and reply.json()["error"] == "hub_healthy"
    assert not run.ran("bash")


def test_the_way_back_runs_the_script_without_the_motor_when_the_graph_is_in_the_motor(env):
    run, _, tmp_path = env
    _raise_flag(tmp_path)
    script = _script()
    run.add(["bash", script, "apply", "hostapp", "--no-wait"])

    reply = _client().post("/api/hub/rollback")

    assert reply.status_code == 200 and reply.json() == {"ok": True, "with_motor": False}
    assert run.argv_for("bash") == [["bash", script, "apply", "hostapp", "--no-wait"]]


def test_the_way_back_recreates_the_motor_when_the_graph_is_in_the_hub(env):
    run, _, tmp_path = env
    _raise_flag(tmp_path)
    (tmp_path / "deploy" / "ros2").mkdir(parents=True)
    (tmp_path / "deploy" / "ros2" / ".env").write_text(
        "EMORI_MOTOR_LAUNCH=robot.launch.py\n", encoding="utf-8")
    script = _script()
    run.add(["bash", script, "apply", "hostapp", "--with-motor", "--no-wait"])

    reply = _client().post("/api/hub/rollback")

    assert reply.json() == {"ok": True, "with_motor": True}


def test_a_script_that_refuses_is_reported(env):
    run, _, tmp_path = env
    _raise_flag(tmp_path)
    script = _script()
    run.add(["bash", script, "apply", "hostapp", "--no-wait"], returncode=3,
            stdout="busy: a session\n")

    reply = _client().post("/api/hub/rollback")

    assert reply.status_code == 502
    assert reply.json()["error"] == "exit_3" and "busy" in reply.json()["detail"]


def test_how_the_way_back_went_comes_with_the_services(env):
    from emori_adapter.admin import system

    run, _, tmp_path = env
    _raise_flag(tmp_path)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    (tmp_path / "artifacts" / "last-hub-stack.env").write_text(
        "reason=hostapp_not_ready\nexit=7\n", encoding="utf-8")

    switch = system.hub_state()["hub_switch"]

    assert switch == {"running": False, "to_hub": False, "stuck": False,
                      "reason": "hostapp_not_ready", "exit": "7"}


def test_the_way_back_can_be_followed_after_it_has_lowered_the_flag(env):
    """It lowers the flag first and with the motor takes minutes after that. With the flag down
    the phone must still see that it runs, and how it ended."""
    from emori_adapter.admin import system

    _, _, tmp_path = env
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    (tmp_path / "artifacts" / "last-hub-stack.env").write_text(
        "reason=hostapp_not_up\nexit=7\n", encoding="utf-8")

    state = system.hub_state()

    assert state["hub_mode"] is False
    assert state["hub_switch"]["reason"] == "hostapp_not_up" and state["hub_switch"]["exit"] == "7"


def _result(tmp_path: Path, text: str, target: str = "") -> None:
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    (tmp_path / "artifacts" / "last-hub-stack.env").write_text(text, encoding="utf-8")
    if target:
        (tmp_path / "artifacts" / "hub-stack.target").write_text(target + "\n", encoding="utf-8")


@pytest.mark.parametrize("target,running", [("ros", False), ("hostapp", True), ("", False)])
def test_only_a_run_going_back_counts_as_the_way_back_running(env, monkeypatch, target, running):
    from emori_adapter.admin import system

    _, _, tmp_path = env
    _result(tmp_path, "reason=switched\nexit=0\n", target)
    monkeypatch.setattr(system, "_unit_facts", lambda unit: ("active", ""))

    assert system.hub_state()["hub_switch"]["running"] is running
    assert system.hub_state()["hub_switch"]["to_hub"] is (target == "ros")


def test_a_way_back_that_stopped_short_can_be_run_again_from_the_phone(env):
    """The flag is down and the hub's container may still answer on the port: only a second
    run puts the host app back, and a site with no ssh has only this page."""

    run, http, tmp_path = env
    _result(tmp_path, "target=hostapp\nreason=hostapp_not_up\nexit=7\n")
    http.add("GET", "/healthz", http_ok({"status": "ok", "runtime": "ros"}))
    run.add(["bash", _script(), "apply", "hostapp", "--with-motor", "--no-wait"])

    reply = _client().post("/api/hub/rollback")

    assert reply.status_code == 200, reply.text
    assert run.ran("bash")


@pytest.mark.parametrize("result", ["target=hostapp\nreason=session_running\nexit=3\n",
                                    "target=hostapp\nreason=needs_motor_recreate\nexit=5\n"])
def test_a_retry_that_was_refused_does_not_close_the_way_back(env, result):
    """The hub's container is still on the port, so a session can begin there and refuse the
    retry. Reading only exit 7 would hide the way back for good while the robot stays like this."""
    from emori_adapter.admin import system

    run, http, tmp_path = env
    _result(tmp_path, result)
    http.add("GET", "/healthz", http_ok({"status": "ok", "runtime": "ros"}))
    run.add(["bash", _script(), "apply", "hostapp", "--with-motor", "--no-wait"])

    assert system.hub_state()["hub_switch"]["stuck"] is True
    assert _client().post("/api/hub/rollback").status_code == 200


def test_a_way_back_that_stopped_short_asks_for_the_motor_whatever_the_settings_say(env):
    """The run that stopped short wrote the settings back before compose failed, and a retry
    without the motor is refused outright. The script ignores it for a motor that needs nothing."""

    run, http, tmp_path = env
    _result(tmp_path, "target=hostapp\nreason=hostapp_not_up\nexit=7\n")
    argv = ["bash", _script(), "apply", "hostapp", "--with-motor", "--no-wait"]
    run.add(argv)

    reply = _client().post("/api/hub/rollback")

    assert reply.status_code == 200 and reply.json()["with_motor"] is True
    assert run.argv_for("bash") == [argv]


def test_a_failed_switch_to_the_hub_whose_way_back_stopped_short_can_be_run_again(env):
    """Docker hung while a switch to the hub was going back: the run set out for the hub, and the
    flag is down, the host app does not answer and the hub's container is still on the port."""

    run, _, tmp_path = env
    _result(tmp_path, "target=ros\nreason=hub_not_answering_and_hostapp_not_up\nexit=7\n")
    run.add(["bash", _script(), "apply", "hostapp", "--with-motor", "--no-wait"])

    reply = _client().post("/api/hub/rollback")

    assert reply.status_code == 200, reply.text
    assert run.ran("bash")


@pytest.mark.parametrize("answer,stuck", [
    ({"status": "ok", "runtime": "ros"}, True),
    ({"status": "ok", "runtime": "http"}, False),
    (None, True),
])
def test_a_way_back_that_stopped_short_stays_offered_until_the_host_app_answers(
        env, answer, stuck):
    """The host app's unit loses the port to a hub's container, restarts every few seconds and
    reads as running between the restarts, so the port is what says it is back."""
    from emori_adapter.admin import system

    _, http, tmp_path = env
    _result(tmp_path, "target=hostapp\nreason=hostapp_not_up\nexit=7\n")
    if answer is not None:
        http.add("GET", "/healthz", http_ok(answer))

    assert system.hub_state()["hub_switch"]["stuck"] is stuck


def test_a_motor_that_did_not_come_back_can_be_retried_with_the_host_app_up(env):
    """The host app answers, so nothing else says the way back has not finished."""
    from emori_adapter.admin import system

    _, http, tmp_path = env
    _result(tmp_path, "target=hostapp\nreason=nodes_missing\nexit=7\n")
    http.add("GET", "/healthz", http_ok({"status": "ok", "runtime": "http"}))

    assert system.hub_state()["hub_switch"]["stuck"] is True


@pytest.mark.parametrize("result", ["target=ros\nreason=low_disk\nexit=5\n",
                                    "target=ros\nreason=hub_not_answering\nexit=6\n"])
def test_a_host_app_stopped_later_is_not_a_way_back_that_stopped_short(env, result):
    """A refused or rolled back switch finished, whatever the host app does after it."""
    from emori_adapter.admin import system

    _, _, tmp_path = env
    _result(tmp_path, result)

    assert system.hub_state()["hub_switch"]["stuck"] is False


@pytest.mark.parametrize("result", ["target=hostapp\nreason=switched\nexit=0\n",
                                    "target=ros\nreason=low_disk\nexit=5\n",
                                    "target=ros\nreason=hub_not_answering\nexit=6\n"])
def test_with_the_flag_down_nothing_else_opens_the_way_back(env, result):
    run, http, tmp_path = env
    _result(tmp_path, result)
    http.add("GET", "/healthz", http_ok({"status": "ok", "runtime": "http"}))

    reply = _client().post("/api/hub/rollback")

    assert reply.status_code == 409 and reply.json()["error"] == "not_hub_mode"
    assert not run.ran("bash")


_RENDER_HARNESS = """
var nodes={};
function $(id){return nodes[id]||(nodes[id]={hidden:true,textContent:''});}
function setMsg(el,text){el.text=text;}
function svcKind(service){return service.kind;}
var OK={kind:'ok'}, BAD={kind:'bad'};
function render(hub,back,hubUnit){
 nodes={};
 S={hub:{mode:hub,graph:!!back.graph,sw:back.sw}};
 renderHubBack(hubUnit);
 return {shown:!$('hubBack').hidden,button:!$('hubBackGo').hidden,
         note:$('hubBackNote').textContent,msg:$('hubBackMsg').text||''};
}
var S;
var out={};
out.going_to_hub=render(false,{sw:{running:false,stuck:false,exit:'0'}},OK);
out.hub_coming_up_in_a_run=render(true,{sw:{running:false,to_hub:true,stuck:false,exit:'0'}},BAD);
out.going_back_flag_up=render(true,{sw:{running:true,stuck:false,exit:'0'}},OK);
out.going_back_flag_down=render(false,{sw:{running:true,stuck:false,exit:'0'}},BAD);
out.healthy_hub=render(true,{sw:{running:false,stuck:false,exit:'0'}},OK);
out.failing_hub=render(true,{sw:{running:false,stuck:false,exit:'0'}},BAD);
out.failing_hub_graph=render(true,{graph:true,sw:{running:false,stuck:false,exit:'0'}},BAD);
out.stuck=render(false,{sw:{running:false,stuck:true,reason:'hostapp_not_up',exit:'7'}},BAD);
out.message_left_behind=(function(){
 nodes={};
 S={hub:{mode:true,graph:false,sw:{running:false,stuck:false,exit:'0'}}};
 $('hubBackMsg').text='Switching back';
 renderHubBack(OK);
 return {msg:$('hubBackMsg').text};
})();
console.log(JSON.stringify(out));
"""


def _render_cases(tmp_path: Path) -> dict:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("no node available to run the page's inline JS")
    source = page.APP_HTML
    start = source.index("function renderHubBack(")
    body = source[start:source.index("async function hubBack(", start)]
    script = tmp_path / "render.js"
    script.write_text(body + _RENDER_HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_page_offers_the_way_back_only_when_it_can_be_taken(tmp_path):
    cases = _render_cases(tmp_path)

    # A run going to the hub, or a hub that is fine, is none of this block's business.
    assert not cases["going_to_hub"]["shown"]
    assert not cases["hub_coming_up_in_a_run"]["shown"]
    # A finished run's message does not wait for the next time the block is shown.
    assert cases["message_left_behind"]["msg"] == ""
    assert not cases["healthy_hub"]["shown"]
    # A run going back is followed, and a second cannot be asked for.
    for name in ("going_back_flag_up", "going_back_flag_down"):
        assert cases[name]["shown"] and not cases[name]["button"], name
        assert "Switching back" in cases[name]["msg"]
        assert cases[name]["note"] == ""
    # What is offered says what it does, so the confirmation is not empty.
    assert cases["failing_hub"]["button"] and "leaves the motors alone" in cases["failing_hub"]["note"]
    assert cases["failing_hub_graph"]["button"] and "parks" in cases["failing_hub_graph"]["note"]
    assert cases["stuck"]["button"] and "tries again" in cases["stuck"]["note"]
    assert "hostapp_not_up" in cases["stuck"]["msg"]
    assert "parks" in cases["stuck"]["note"]


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------
def test_the_page_carries_the_rollback_button_hidden_until_it_is_needed(env):
    from emori_adapter.admin import page

    html = page.APP_HTML

    assert 'id=hubBackGo' in html and 'id=hubBack hidden' in html
    assert "'emori-hub.service':'Hub'" in html
    assert "/api/hub/rollback" in html
    assert "var can=!sw.running&&!sw.to_hub&&(stuck||failing);" in html


def test_the_services_list_follows_a_way_back_until_it_is_done():
    """The block says the list updates when the run is done, and a run takes minutes with the
    motor. Nothing else reloads the tab while it stays open."""
    from emori_adapter.admin import page

    html = page.APP_HTML
    timer = html[html.index("One timer for the whole page"):]
    timer = timer[:timer.index("},5000);")]

    assert "if(active==='services'&&S.hub.sw&&S.hub.sw.running) loadServices();" in timer


def test_the_sudoers_grant_lists_exactly_the_no_block_form():
    text = (Path(__file__).resolve().parents[1] / "deploy" / "robot"
            / "sudoers-emori-admin").read_text(encoding="utf-8")

    for action in ("restart", "start", "stop"):
        assert f"/usr/bin/systemctl --no-block {action} emori-hub.service" in text
    assert f"/usr/bin/systemctl restart {HUB}" not in text
    grants = [line for line in text.splitlines() if not line.startswith("#")]
    assert not [line for line in grants if "hub-stack" in line]
