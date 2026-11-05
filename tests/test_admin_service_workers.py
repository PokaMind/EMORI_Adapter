"""The parts that run with nobody watching: probes, watchdog, rebrand, boot audio.

Everything in this file is about a robot in a cupboard at a client site.  There
is no operator to retry, no screen to read, and the failure mode of each of
these workers is silent: an AP that never gets its name, a speaker that stays at
zero after a power cut, a Wi-Fi outage that lasts until somebody drives out.

Two properties get the most attention because a robot without them fails
silently in the field:

* the AP rebrand must NEVER touch the Hotspot profile while it is the live
  connection (that drops the phone of the person doing the deploy), and
* every subprocess/HTTP wrapper must answer instead of raising, on a machine
  that has none of the tools it asks for.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import pytest

from emori_adapter.admin import audio as audio_mod
from emori_adapter.admin import network, probes, server
from emori_adapter.admin import system as system_mod
from emori_adapter.admin.state import AdminState
from test_admin_service_fakes import base_env, http_ok, install


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, state_file=str(tmp_path / "state.json"))
    return install(monkeypatch)


# --------------------------------------------------------------------------
# probes.run — the never-raise subprocess wrapper
# --------------------------------------------------------------------------
class _Runner:
    def __init__(self, *, returncode=0, stdout="", stderr="", raises=None):
        self.calls = []
        self.kwargs = []
        self.returncode, self.stdout, self.stderr, self.raises = (
            returncode, stdout, stderr, raises)

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout,
                                           self.stderr)


def test_run_captures_output_with_an_argv_list_and_a_timeout():
    runner = _Runner(stdout="hello\nworld\n")

    result = probes.run(["nmcli", "-t", "general"], runner=runner)

    assert result.ok is True and result.lines == ["hello", "world"]
    assert runner.calls == [["nmcli", "-t", "general"]]
    kwargs = runner.kwargs[0]
    assert kwargs["timeout"] == probes.CMD_TIMEOUT_S
    assert kwargs["capture_output"] is True and kwargs["check"] is False
    assert "shell" not in kwargs  # never shell=True


@pytest.mark.parametrize("failure,slug", [
    (FileNotFoundError("nmcli"), "missing"),
    (subprocess.TimeoutExpired(cmd="nmcli", timeout=5.0), "timeout"),
    (OSError("no such device"), "os_error"),
    (subprocess.SubprocessError("boom"), "os_error"),
])
def test_every_subprocess_failure_is_a_record_not_an_exception(failure, slug):
    """A dev box has no nmcli/systemctl/journalctl/wpctl at all, and the robot can
    lose any of them; not one may reach a route handler."""
    result = probes.run(["nmcli"], runner=_Runner(raises=failure))

    assert result.ok is False and result.error == slug


def test_a_nonzero_exit_is_reported_with_its_code():
    result = probes.run(["systemctl"], runner=_Runner(returncode=5, stderr="nope"))

    assert result.ok is False and result.returncode == 5
    assert result.error == "exit_5" and result.stderr == "nope"


# --------------------------------------------------------------------------
# probes.request — no exception text ever escapes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("exc,slug", [
    (TimeoutError("timed out"), "timeout"),
    # The message here is the danger: httpx quotes the URL it failed on, and
    # POST /wifi/connect carries the site's Wi-Fi password as a URL parameter.
    (RuntimeError("ConnectTimeout: .../wifi/connect?password=hunter2hunter2"),
     "transport_error"),
])
def test_error_slugs_come_from_the_type_never_the_message(exc, slug):
    assert probes._error_slug(exc) == slug
    assert "hunter2" not in probes._error_slug(exc)


def test_a_connect_error_and_a_timeout_are_distinguishable():
    class ConnectError(Exception):
        pass

    class ReadTimeout(Exception):
        pass

    assert probes._error_slug(ConnectError("x")) == "unreachable"
    assert probes._error_slug(ReadTimeout("x")) == "timeout"


def test_the_failure_body_names_which_peer_was_silent():
    assert probes.fail(probes.Http(error="unreachable"),
                       when_unreachable="daemon_down") == {
        "ok": False, "error": "daemon_down", "status": 0}
    # ...but a peer that ANSWERED badly keeps its own status.
    assert probes.fail(probes.Http(reached=True, status=409, error="http_409"),
                       when_unreachable="daemon_down") == {
        "ok": False, "error": "http_409", "status": 409}


# --------------------------------------------------------------------------
# where the peers live
# --------------------------------------------------------------------------
def test_the_hostapp_url_follows_the_port_the_hostapp_itself_reads(monkeypatch):
    """Never a hardcoded 8090: the robot sets HOSTAPP_PORT in .env and both
    processes have to agree without a second key to keep in sync."""
    monkeypatch.setenv("HOSTAPP_PORT", "8090")
    assert probes.hostapp_base() == "http://127.0.0.1:8090"
    monkeypatch.delenv("HOSTAPP_PORT")
    assert probes.hostapp_base() == "http://127.0.0.1:8080"


def test_the_daemon_url_is_the_shared_reachy_base(monkeypatch):
    monkeypatch.setenv("REACHY_BASE_URL", "http://10.0.0.5:8000/")
    assert probes.daemon_base() == "http://10.0.0.5:8000"


def test_daemon_ok_asserts_the_running_state_not_just_a_200(env):
    _, http = env
    http.add("GET", "/api/daemon/status", http_ok({"state": "starting"}))
    assert probes.daemon_ok() is False
    http.add("GET", "/api/daemon/status", http_ok({"state": "running"}))
    assert probes.daemon_ok() is True


# --------------------------------------------------------------------------
# internet probe
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [("full", True), ("none", False),
                                            ("portal", False), ("limited", False)])
def test_internet_uses_network_managers_own_answer(env, value, expected):
    run, _ = env
    run.add(["nmcli", "-t", "-f", "CONNECTIVITY"], stdout=value + "\n")

    assert probes.internet_ok() is expected


def test_without_nmcli_it_falls_back_to_a_tcp_probe(env, monkeypatch):
    seen = []
    monkeypatch.setattr(probes, "_tcp_reachable",
                        lambda url: seen.append(url) or True)
    monkeypatch.setenv("POKAMIND_BASE", "https://app.pokamind.com")

    assert probes.internet_ok() is True
    assert seen == ["https://app.pokamind.com"]


def test_an_unknown_connectivity_state_also_falls_through(env, monkeypatch):
    run, _ = env
    run.add(["nmcli", "-t", "-f", "CONNECTIVITY"], stdout="unknown\n")
    monkeypatch.setattr(probes, "_tcp_reachable", lambda url: True)

    assert probes.internet_ok() is True


# --------------------------------------------------------------------------
# TtlCache
# --------------------------------------------------------------------------
def test_the_cache_holds_for_its_ttl_and_then_refreshes():
    cache = probes.TtlCache(3.0)
    calls = []

    def produce():
        calls.append(1)
        return len(calls)

    assert cache.get("k", produce, now=100.0) == 1
    assert cache.get("k", produce, now=102.9) == 1
    assert cache.get("k", produce, now=103.1) == 2


def test_a_producer_that_raises_becomes_a_cached_none_not_a_500():
    cache = probes.TtlCache(3.0)

    assert cache.get("k", lambda: 1 / 0, now=1.0) is None


# --------------------------------------------------------------------------
# CPU sampler
# --------------------------------------------------------------------------
def test_the_cpu_sampler_needs_two_samples_before_it_answers(monkeypatch):
    """A single /proc/stat read is a since-boot average: on a robot that has been
    up a week it reads ~4% forever and tells an operator nothing."""
    samples = iter([(1000, 900), (1100, 950)])
    monkeypatch.setattr(probes.CpuSampler, "_read", staticmethod(lambda: next(samples)))
    sampler = probes.CpuSampler()

    assert sampler.sample() is None
    assert sampler.sample() == 50.0


def test_the_cpu_sampler_is_none_where_there_is_no_proc(monkeypatch):
    monkeypatch.setattr(probes.CpuSampler, "_read", staticmethod(lambda: None))

    assert probes.CpuSampler().sample() is None


# --------------------------------------------------------------------------
# the Wi-Fi watchdog decision
# --------------------------------------------------------------------------
def test_the_watchdog_waits_out_the_whole_grace_period():
    watchdog = network.WifiWatchdog(grace_s=180.0)

    assert watchdog.observe("disconnected", now=0.0) is False
    assert watchdog.observe("disconnected", now=179.0) is False
    assert watchdog.observe("disconnected", now=181.0) is True


@pytest.mark.parametrize("mode", ["wlan", "hotspot", "busy", None])
def test_anything_that_is_not_disconnected_resets_the_clock(mode):
    """`busy` is a mutation in flight and `hotspot` is the goal state; treating
    either as an outage would make the robot flap its own AP."""
    watchdog = network.WifiWatchdog(grace_s=100.0)
    watchdog.observe("disconnected", now=0.0)

    assert watchdog.observe(mode, now=50.0) is False
    assert watchdog.down_since is None
    assert watchdog.observe("disconnected", now=51.0) is False
    assert watchdog.observe("disconnected", now=140.0) is False  # clock restarted


def test_the_watchdog_re_arms_instead_of_latching():
    """If setup_hotspot silently failed, a stranded robot must get another try."""
    watchdog = network.WifiWatchdog(grace_s=100.0)
    watchdog.observe("disconnected", now=0.0)

    assert watchdog.observe("disconnected", now=101.0) is True
    assert watchdog.observe("disconnected", now=150.0) is False
    assert watchdog.observe("disconnected", now=202.0) is True


def test_the_grace_period_is_env_tunable_and_floored(monkeypatch):
    monkeypatch.setenv("EMORI_ADMIN_WIFI_GRACE_S", "600")
    assert network.watchdog_grace_s() == 600.0
    # A grace under one poll interval would fire on the first sample.
    monkeypatch.setenv("EMORI_ADMIN_WIFI_GRACE_S", "1")
    assert network.watchdog_grace_s() == network.WATCHDOG_POLL_S * 2
    monkeypatch.setenv("EMORI_ADMIN_WIFI_GRACE_S", "soon")
    assert network.watchdog_grace_s() == network.WATCHDOG_DEFAULT_GRACE_S
    monkeypatch.delenv("EMORI_ADMIN_WIFI_GRACE_S")
    assert network.watchdog_grace_s() == network.WATCHDOG_DEFAULT_GRACE_S


def test_the_watchdog_is_on_by_default_and_switched_off_with_zero(monkeypatch):
    monkeypatch.delenv("EMORI_ADMIN_WIFI_WATCHDOG", raising=False)
    assert network.watchdog_enabled() is True
    monkeypatch.setenv("EMORI_ADMIN_WIFI_WATCHDOG", "0")
    assert network.watchdog_enabled() is False
    monkeypatch.setenv("EMORI_ADMIN_WIFI_WATCHDOG", "1")
    assert network.watchdog_enabled() is True


def test_the_watchdog_refuses_to_arm_on_the_vendors_published_ap(monkeypatch):
    """Its only action is "broadcast an AP"; whose AP is therefore a precondition.

    Unset EMORI_AP_SSID/PASSWORD means the daemon falls back to
    ``reachy-mini-ap`` / ``reachy-mini`` -- constants in a pip-installable
    package -- so an unattended, attacker-triggerable transition would put an
    open-to-anyone-in-range door on the robot's own network.
    """
    monkeypatch.delenv("EMORI_AP_SSID", raising=False)
    monkeypatch.delenv("EMORI_AP_PASSWORD", raising=False)
    assert network.watchdog_ap_ready() == (False, "ap_not_configured")

    monkeypatch.setenv("EMORI_AP_SSID", "emori-setup-3")
    assert network.watchdog_ap_ready() == (False, "ap_not_configured")

    monkeypatch.setenv("EMORI_AP_PASSWORD", "short")          # < 8 chars: not a psk
    assert network.watchdog_ap_ready() == (False, "ap_invalid")

    monkeypatch.setenv("EMORI_AP_PASSWORD", "a-real-passphrase")
    assert network.watchdog_ap_ready() == (True, "ok")


def test_the_watchdog_is_only_started_with_an_ap_identity_of_ours(env, monkeypatch):
    from starlette.testclient import TestClient

    names = []
    real_create_task = asyncio.create_task

    def spy(coro, *, name=None):
        names.append(name)
        return real_create_task(coro, name=name)

    monkeypatch.setattr(asyncio, "create_task", spy)
    monkeypatch.setenv("EMORI_ADMIN_WIFI_WATCHDOG", "1")
    with TestClient(server.create_app()):
        pass
    assert "admin-wifi-watchdog" not in names        # env pair unset by base_env

    names.clear()
    monkeypatch.setenv("EMORI_AP_SSID", "emori-setup-3")
    monkeypatch.setenv("EMORI_AP_PASSWORD", "a-real-passphrase")
    with TestClient(server.create_app()):
        pass
    assert "admin-wifi-watchdog" in names


@pytest.mark.parametrize("raw,expected", [
    ("disconnected", "disconnected"),
    ({"value": "disconnected"}, "disconnected"),   # some daemon builds do this
    ({"value": None}, None), ({}, None), (None, None), ("", None),
])
def test_the_mode_is_normalised_the_same_way_for_every_reader(raw, expected):
    """Two readers of one field disagreeing is the defect: the header would show
    'no Wi-Fi' while the watchdog sat in the healthy branch for ever."""
    assert network.normalize_mode(raw) == expected


def test_the_watchdog_fires_on_an_objectified_mode(env, monkeypatch):
    run, http = env
    http.add("GET", "/wifi/status", http_ok({"mode": {"value": "disconnected"},
                                             "known_networks": [],
                                             "connected_network": None}))
    http.add("POST", "/wifi/setup_hotspot", http_ok(None))
    monkeypatch.setattr(network, "WATCHDOG_POLL_S", 0.001)
    monkeypatch.setenv("EMORI_ADMIN_WIFI_GRACE_S", "0.002")

    async def drive():
        task = asyncio.create_task(server.wifi_watchdog_task())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if http.called("POST", "/wifi/setup_hotspot"):
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert http.called("POST", "/wifi/setup_hotspot")
    assert run.argv_for("sudo", "nmcli") == []


def test_the_watchdog_task_only_ever_calls_the_daemons_own_hotspot(env, monkeypatch):
    """No direct nmcli mutation: the daemon's busy lock has to stay the one
    place that serialises Wi-Fi changes on this robot."""
    run, http = env
    http.add("GET", "/wifi/status", http_ok({"mode": "disconnected",
                                             "known_networks": [],
                                             "connected_network": None}))
    http.add("POST", "/wifi/setup_hotspot", http_ok(None))
    monkeypatch.setattr(network, "WATCHDOG_POLL_S", 0.001)
    monkeypatch.setenv("EMORI_ADMIN_WIFI_GRACE_S", "0.002")

    async def drive():
        task = asyncio.create_task(server.wifi_watchdog_task())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if http.called("POST", "/wifi/setup_hotspot"):
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert http.called("POST", "/wifi/setup_hotspot")
    assert run.argv_for("sudo", "nmcli") == []


def test_the_watchdog_does_not_act_on_a_daemon_it_cannot_reach(env, monkeypatch):
    _, http = env  # no /wifi/status rule -> unreachable
    monkeypatch.setattr(network, "WATCHDOG_POLL_S", 0.001)
    monkeypatch.setenv("EMORI_ADMIN_WIFI_GRACE_S", "0.002")

    async def drive():
        task = asyncio.create_task(server.wifi_watchdog_task())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert http.called("POST", "/wifi/setup_hotspot") == []


# --------------------------------------------------------------------------
# AP rebrand
# --------------------------------------------------------------------------
def _configure_ap(monkeypatch, ssid="emori-setup", password="test-ap-passphrase"):
    monkeypatch.setenv("EMORI_AP_SSID", ssid)
    monkeypatch.setenv("EMORI_AP_PASSWORD", password)


def test_no_ap_configured_leaves_the_stock_hotspot_alone(env):
    run, _ = env

    assert network.rebrand_ap() == {"applied": False, "reason": "not_configured"}
    assert run.calls == []


@pytest.mark.parametrize("ssid,password", [
    ("emori-setup", "short"), ("x" * 40, "test-ap-passphrase"), ("emori", "x" * 70)])
def test_an_unusable_wpa2_pair_is_refused_loudly(env, monkeypatch, ssid, password):
    run, _ = env
    _configure_ap(monkeypatch, ssid, password)

    assert network.rebrand_ap()["reason"] == "invalid_config"
    assert run.argv_for("sudo") == []


def test_the_rebrand_is_idempotent(env, monkeypatch):
    run, _ = env
    _configure_ap(monkeypatch)
    run.add(["nmcli", "-t", "-g", "802-11-wireless.ssid"], stdout="emori-setup\n")
    run.add(["sudo", "nmcli", "-s", "-g"], stdout="test-ap-passphrase\n")

    assert network.rebrand_ap() == {"applied": False, "reason": "already_current"}
    assert run.argv_for("sudo", "nmcli", "connection", "modify") == []


def test_an_unreadable_psk_still_counts_as_done_when_the_ssid_matches(env, monkeypatch):
    """Otherwise every boot would rewrite the profile chasing a value it cannot
    observe -- the opposite of idempotent."""
    run, _ = env
    _configure_ap(monkeypatch)
    run.add(["nmcli", "-t", "-g", "802-11-wireless.ssid"], stdout="emori-setup\n")

    assert network.rebrand_ap()["reason"] == "already_current"
    assert run.argv_for("sudo", "nmcli", "connection", "modify") == []


def test_the_rebrand_refuses_while_the_hotspot_is_the_live_connection(env, monkeypatch):
    """This is the operator's own phone. Rewriting the AP under them leaves them
    on a robot they can no longer reach."""
    run, _ = env
    _configure_ap(monkeypatch)
    run.add(["nmcli", "-t", "-g", "802-11-wireless.ssid"], stdout="reachy-mini-ap\n")
    run.add(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"],
            stdout="Hotspot\n")

    assert network.rebrand_ap() == {"applied": False, "reason": "active_hotspot"}
    assert run.argv_for("sudo", "nmcli", "connection", "modify") == []


def test_the_rebrand_writes_exactly_the_sudoers_allowlisted_invocation(env, monkeypatch):
    run, _ = env
    _configure_ap(monkeypatch)
    run.add(["nmcli", "-t", "-g", "802-11-wireless.ssid"], stdout="reachy-mini-ap\n")
    run.add(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"],
            stdout="example-wifi\n")
    run.add(["sudo", "nmcli", "connection", "modify"])

    assert network.rebrand_ap() == {"applied": True, "reason": "modified"}
    assert run.argv_for("sudo", "nmcli", "connection", "modify") == [[
        "sudo", "nmcli", "connection", "modify", "Hotspot",
        "802-11-wireless.ssid", "emori-setup",
        "802-11-wireless-security.psk", "test-ap-passphrase"]]


def test_a_profile_that_cannot_be_read_is_left_to_the_daemon(env, monkeypatch):
    _configure_ap(monkeypatch)  # no nmcli at all on this box

    assert network.rebrand_ap()["reason"] == "profile_unreadable"


def test_the_psk_read_matches_the_sudoers_entry_exactly(env, monkeypatch):
    run, _ = env
    _configure_ap(monkeypatch)
    run.add(["nmcli", "-t", "-g", "802-11-wireless.ssid"], stdout="emori-setup\n")
    run.add(["sudo", "nmcli", "-s", "-g"], stdout="other\n")
    run.add(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"],
            stdout="example\n")
    run.add(["sudo", "nmcli", "connection", "modify"])

    network.rebrand_ap()

    assert ["sudo", "nmcli", "-s", "-g", "802-11-wireless-security.psk",
            "connection", "show", "Hotspot"] in run.calls


# --------------------------------------------------------------------------
# boot audio re-apply
# --------------------------------------------------------------------------
def test_nothing_persisted_means_the_mixer_is_left_alone(env, tmp_path):
    _, http = env
    state = AdminState(str(tmp_path / "s.json"))

    assert audio_mod.reapply_persisted(state) == {"pending": False, "speaker": None,
                                                  "mic": None}
    assert http.calls == []


def test_persisted_levels_are_pushed_back_into_the_daemon(env, tmp_path):
    _, http = env
    http.add("POST", "/api/volume/set", http_ok({"volume": 85}))
    http.add("POST", "/api/volume/microphone/set", http_ok({"volume": 35}))
    state = AdminState(str(tmp_path / "s.json"))
    state.update(speaker_volume=85, mic_volume=35)

    outcome = audio_mod.reapply_persisted(state)

    assert outcome == {"pending": False, "speaker": 85, "mic": 35}
    assert http.called("POST", "/api/volume/set")[0]["json"] == {"volume": 85}


def test_a_daemon_that_is_not_up_yet_leaves_the_work_pending(env, tmp_path):
    state = AdminState(str(tmp_path / "s.json"))
    state.update(speaker_volume=85)

    assert audio_mod.reapply_persisted(state)["pending"] is True


def test_the_boot_task_retries_until_the_daemon_accepts(env, monkeypatch, tmp_path):
    """The daemon's audio backend can take tens of seconds to bind its device on
    a cold boot; giving up on the first refusal is how the speaker stays at 0."""
    attempts = []

    def flaky(_state):
        attempts.append(1)
        return {"pending": len(attempts) < 3}

    monkeypatch.setattr(audio_mod, "reapply_persisted", flaky)
    monkeypatch.setattr(audio_mod, "BOOT_RETRY_DELAYS_S", (0.0, 0.0, 0.0, 0.0))

    asyncio.run(server.boot_audio_task(AdminState(str(tmp_path / "s.json"))))

    assert len(attempts) == 3  # stopped as soon as it took


def test_the_boot_task_gives_up_after_the_ladder(env, monkeypatch, tmp_path):
    attempts = []
    monkeypatch.setattr(audio_mod, "reapply_persisted",
                        lambda _s: attempts.append(1) or {"pending": True})
    monkeypatch.setattr(audio_mod, "BOOT_RETRY_DELAYS_S", (0.0, 0.0))

    asyncio.run(server.boot_audio_task(AdminState(str(tmp_path / "s.json"))))

    assert len(attempts) == 2  # bounded: it never becomes a permanent poll loop


# --------------------------------------------------------------------------
# the readycheck runner
# --------------------------------------------------------------------------
def test_the_readycheck_argv_uses_this_interpreter():
    """`/opt/emori/.venv/bin/python` IS sys.executable on the robot, and does not
    exist on a dev box."""
    assert system_mod.default_runner_argv() == [
        sys.executable, "-m", "emori_adapter.hostapp.readycheck", "--json", "--no-speak"]


def test_the_runner_is_single_flight(monkeypatch):
    import threading

    release = threading.Event()

    def blocking(argv, *, timeout_s=None, runner=None):
        release.wait(timeout=5.0)
        return probes.Cmd(ok=True, ran=True, stdout="[]")

    monkeypatch.setattr(probes, "run", blocking)
    runner = system_mod.ReadycheckRunner()

    assert runner.start()["ok"] is True
    second = runner.start()
    assert second == {"ok": False, "error": "already_running",
                      "started_at": pytest.approx(second["started_at"])}
    assert runner.status()["running"] is True
    release.set()
    for _ in range(200):
        if not runner.status()["running"]:
            break
        time.sleep(0.01)
    assert runner.status()["running"] is False
    # ...and it can be run again once it has finished.
    release.set()
    assert runner.start()["ok"] is True


def test_a_failing_gate_is_a_RESULT_not_an_error(monkeypatch):
    """readycheck exits 1 precisely when a gate FAILs -- that is the answer the
    operator asked for, not a broken run."""
    gates = [{"name": "daemon", "status": "PASS", "code": "reachable", "hint": ""},
             {"name": "audio", "status": "FAIL", "code": "x", "hint": "check the sink"}]
    monkeypatch.setattr(probes, "run", lambda *a, **k: probes.Cmd(
        ok=False, ran=True, returncode=1, stdout=json.dumps(gates), error="exit_1"))
    runner = system_mod.ReadycheckRunner()
    runner.start()
    _wait_idle(runner)

    status = runner.status()
    assert status["error"] is None
    assert status["result"]["exit_code"] == 1
    assert status["result"]["counts"] == {"PASS": 1, "FAIL": 1}
    assert status["result"]["gates"][1]["hint"] == "check the sink"


@pytest.mark.parametrize("cmd", [
    probes.Cmd(error="missing"),
    probes.Cmd(ran=True, error="timeout"),
])
def test_a_run_that_never_produced_json_is_an_error(monkeypatch, cmd):
    monkeypatch.setattr(probes, "run", lambda *a, **k: cmd)
    runner = system_mod.ReadycheckRunner()
    runner.start()
    _wait_idle(runner)

    status = runner.status()
    assert status["result"] is None and status["error"]


def test_unparseable_output_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(probes, "run", lambda *a, **k: probes.Cmd(
        ok=True, ran=True, stdout="Traceback (most recent call last):"))
    runner = system_mod.ReadycheckRunner()
    runner.start()
    _wait_idle(runner)

    assert runner.status()["error"] == "readycheck_output_unparseable"


def _wait_idle(runner, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while runner.status()["running"] and time.time() < deadline:
        time.sleep(0.01)
    assert runner.status()["running"] is False


# --------------------------------------------------------------------------
# systemctl show parsing
# --------------------------------------------------------------------------
def test_show_blocks_split_on_blank_lines_and_keep_values_with_equals():
    parsed = system_mod._show_blocks(
        "Id=a.service\nActiveEnterTimestamp=Mon 2026-08-04 09:00:00 UTC\n"
        "\n"
        "Id=b.service\nActiveState=failed\n")

    assert parsed[0]["ActiveEnterTimestamp"] == "Mon 2026-08-04 09:00:00 UTC"
    assert parsed[1] == {"Id": "b.service", "ActiveState": "failed"}


def test_services_survives_a_box_with_no_systemd(env):
    body = system_mod.services()

    assert body == {"ok": False, "error": "missing", "services": []}


# --------------------------------------------------------------------------
# port + host resolution
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [("", 8070), ("8070", 8070), ("9000", 9000),
                                          ("nope", 8070), ("0", 8070), ("99999", 8070)])
def test_the_admin_port_never_crashes_the_service(monkeypatch, raw, expected):
    monkeypatch.setenv("EMORI_ADMIN_PORT", raw)

    assert server.admin_port() == expected


# --------------------------------------------------------------------------
# the whole lifespan, started and stopped
# --------------------------------------------------------------------------
def test_the_service_starts_and_stops_its_workers_cleanly(env):
    from starlette.testclient import TestClient

    with TestClient(server.create_app()) as client:
        assert client.get("/healthz").json()["status"] == "ok"


def test_the_watchdog_task_is_not_created_when_switched_off(env, monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv("EMORI_ADMIN_WIFI_WATCHDOG", "0")
    names = []

    real_create_task = asyncio.create_task

    def spy(coro, *, name=None):
        names.append(name)
        return real_create_task(coro, name=name)

    monkeypatch.setattr(asyncio, "create_task", spy)
    with TestClient(server.create_app()):
        pass

    assert "admin-wifi-watchdog" not in names
    assert "admin-boot-audio" in names
