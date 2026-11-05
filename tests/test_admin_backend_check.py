"""emori-admin's deep check (``GET /healthz/deep``) and what readycheck makes of it.

When emori-admin on a v2 robot sends Wi-Fi and volume to routes its ``:8000`` does not
serve, nothing says so until the Network tab answers "HTTP 404" on site. readycheck
therefore asks the running service whether a Wi-Fi status and a volume read work
through the backend it would use for the phone, and FAILs ``admin_wifi_backend_missing``
when the answer is a missing route or a missing tool. These tests run the REAL route
(through emori-admin's own doubles of nmcli, ALSA and ``:8000``) and hand its answer to
the REAL gate, so the two halves cannot drift apart.
"""

from __future__ import annotations

import pytest

from emori_adapter.admin import backend_check, probes, server
from emori_adapter.hostapp import readycheck
from emori_adapter.hostapp.readycheck import FAIL, PASS, WARN, HttpResult
from test_admin_service_fakes import (
    NOT_FOUND,
    V2_STATUS,
    FakeAlsa,
    FakeNM,
    backend_error_words,
    base_env,
    http_ok,
    http_status,
    install,
)
from test_readycheck import _healthy

TOKEN = "deep-check-test-token"
HEADER = {"X-EMORI-Admin-Token": TOKEN}
DAEMON_STATUS = {"state": "running", "no_media": True}
# What readycheck's own GET :8000/api/daemon/status saw: the v2 shim, or Pollen's daemon.
SHIM_ON_8000 = HttpResult(True, 200, {"state": "running", "no_media": True,
                                      "backend": "reachy_mini_ros2"})
DAEMON_ON_8000 = HttpResult(True, 200, {"state": "running", "no_media": True})
WIFI_STATUS = {"mode": "wlan", "known_networks": ["Example Wi-Fi"],
               "connected_network": "Example Wi-Fi"}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, token=TOKEN, state_file=str(tmp_path / "state.json"))
    return install(monkeypatch)


@pytest.fixture()
def v2(env, monkeypatch):
    """A v2 robot: the shim on :8000 (no /wifi, no /api/volume), nmcli, ALSA."""

    _, http = env
    http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))
    for method, path in (("GET", "/wifi/status"), ("GET", "/api/volume/current")):
        http.add(method, path, http_status(404, NOT_FOUND))
    nm, alsa = FakeNM(saved=("Example Wi-Fi",), active="Example Wi-Fi"), FakeAlsa()

    def run(argv, **kwargs):
        return (alsa if argv[0] in ("aplay", "amixer", "pw-play") else nm)(argv, **kwargs)

    monkeypatch.setattr(probes, "run", run)
    return nm, alsa, http


def _client():
    from starlette.testclient import TestClient

    return TestClient(server.create_app(background=False))


def _deep(headers=HEADER, **kwargs):
    return _client().get("/healthz/deep", headers=headers, **kwargs)


def _gate(response, **evidence) -> readycheck.GateResult:
    """The readycheck admin gate, fed exactly what this route answered."""

    try:
        payload = response.json()
    except ValueError:
        payload = None
    deep = HttpResult(True, response.status_code,
                      payload if isinstance(payload, dict) else None)
    results = readycheck.evaluate(_healthy(admin_deep=deep, **evidence))
    return {result.name: result for result in results}["admin"]


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------
def test_a_v2_robot_answers_wifi_and_volume_through_the_host(v2):
    nm, alsa, http = v2

    response = _deep()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "ok": True, "service": "emori-admin", "commit": "", "backend": "host",
        "reason": "v2_status",
        "wifi": {"ok": True, "route_missing": False, "error": ""},
        "volume": {"ok": True, "route_missing": False, "error": ""},
        "wifi_privileges": {"ok": True, "error": ""},
    }
    assert [c for c in http.calls if c["path"].startswith(("/wifi", "/api/volume"))] == []
    assert (_gate(response).status, _gate(response).code) == (PASS, "ok")


def test_the_deep_check_never_rescans_and_runs_nothing_as_root(v2):
    """A rescan disturbs the link it asks about, and readycheck is read-only: the only
    sudo it touches is `sudo -n -l`, which asks and executes nothing."""

    nm, _, _ = v2

    _deep()

    ran = [call for call in nm.calls if call not in nm.listed()]
    assert ran and not any("rescan" in call for call in ran)
    assert nm.root_calls() == []
    assert nm.listed() and all(call[:4] == ["sudo", "-n", "-l", "nmcli"]
                               for call in nm.listed())


# --------------------------------------------------------------------------
# the Wi-Fi sudo grant (v2): reads pass without it, every change fails
# --------------------------------------------------------------------------
def test_a_v2_robot_without_its_wifi_sudo_grant_fails(v2):
    """A missing /etc/sudoers.d/emori-admin breaks joins, the hotspot
    and the boot hotspot fallback, while the status read the deep check makes passes."""

    nm, _, _ = v2
    nm.sudo_denied = True

    response = _deep()
    body = response.json()
    gate = _gate(response, daemon=SHIM_ON_8000)

    assert body["wifi"]["ok"] is True                 # the read alone would say "fine"
    assert body["wifi_privileges"] == {"ok": False, "error": "sudo_denied"}
    assert body["ok"] is False
    assert (gate.status, gate.code) == (FAIL, "admin_wifi_privileges_missing")
    assert "sudoers-emori-admin" in gate.hint and "visudo -c" in gate.hint
    assert "full install" in gate.hint
    assert nm.root_calls() == []                      # asked, never run


def test_the_privilege_check_asks_about_placeholders_never_a_real_password(
        v2, monkeypatch):
    monkeypatch.setenv("EMORI_AP_SSID", "EMORI-Setup")
    monkeypatch.setenv("EMORI_AP_PASSWORD", "real-ap-password-123")
    nm, _, _ = v2

    _deep()

    asked = " ".join(" ".join(call) for call in nm.listed())
    assert "real-ap-password-123" not in asked and "EMORI-Setup" not in asked
    assert "emori-privilege-check" in asked


def test_a_privilege_check_that_could_not_run_is_only_a_warning(v2, monkeypatch):
    from emori_adapter.admin import netman

    monkeypatch.setattr(netman, "privileges", lambda: (False, "privilege_check_failed"))

    gate = _gate(_deep(), daemon=SHIM_ON_8000)

    assert (gate.status, gate.code) == (WARN, "admin_backend_unavailable")
    assert "privilege_check_failed" in gate.hint


def test_a_v1_robot_is_not_asked_about_sudo(env):
    run, http = env
    http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
    http.add("GET", "/wifi/status", http_ok(WIFI_STATUS))
    http.add("GET", "/api/volume/current", http_ok({"volume": 70}))

    body = _deep().json()

    assert body["backend"] == "daemon" and body["wifi_privileges"] is None
    assert body["ok"] is True
    assert not run.ran("sudo")


def test_a_v1_robot_answers_through_the_daemon(env):
    _, http = env
    http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
    http.add("GET", "/wifi/status", http_ok(WIFI_STATUS))
    http.add("GET", "/api/volume/current", http_ok({"volume": 70}))

    body = _deep().json()

    assert body["ok"] is True and body["backend"] == "daemon"
    assert body["reason"] == "daemon_status"


def test_the_answer_is_content_free(v2):
    """Booleans and slugs: no SSID, no level, no card name."""

    text = _deep().text

    for leak in ("Example Wi-Fi", "Reachy Mini Audio", "82", "known_networks", "volume\": 8"):
        assert leak not in text


@pytest.mark.parametrize("headers, status, error", [
    ({}, 403, "token_rejected"),
    ({"X-EMORI-Admin-Token": "not-the-token"}, 403, "token_rejected"),
])
def test_the_deep_check_needs_the_admin_token(v2, headers, status, error):
    nm, _, http = v2

    response = _deep(headers=headers)

    assert response.status_code == status and response.json()["error"] == error
    assert nm.calls == [] and http.calls == []  # refused before anything ran
    assert (_gate(response).status, _gate(response).code) == (WARN, "admin_token_rejected")


def test_the_deep_check_needs_no_pin(v2, monkeypatch):
    """readycheck has no PIN; the token is its credential, the PIN the phone's."""

    monkeypatch.delenv("EMORI_ADMIN_PIN")

    assert _deep().status_code == 200


def test_without_an_admin_token_configured_the_deep_check_is_closed(v2, monkeypatch):
    monkeypatch.delenv("EMORI_ADMIN_TOKEN")

    response = _deep()

    assert response.status_code == 503 and response.json()["error"] == "admin_token_unset"
    assert (_gate(response).status, _gate(response).code) == (WARN, "admin_token_unset")


def test_a_cross_origin_page_cannot_drive_it(v2):
    response = _deep(headers={**HEADER, "Origin": "http://reachy-mini.local:8000"})

    assert response.status_code == 403 and response.json()["error"] == "bad_origin"


# --------------------------------------------------------------------------
# what readycheck makes of it
# --------------------------------------------------------------------------
def test_forced_daemon_on_a_v2_robot_is_a_missing_route_and_fails(v2, monkeypatch):
    """EMORI_ADMIN_HOST_BACKEND=daemon on a shim robot: both reads 404 on a missing route."""

    monkeypatch.setenv("EMORI_ADMIN_HOST_BACKEND", "daemon")

    response = _deep()
    body = response.json()
    gate = _gate(response)

    assert body["backend"] == "daemon" and body["reason"] == "forced"
    assert body["wifi"] == {"ok": False, "route_missing": True, "error": "route_missing"}
    assert body["volume"]["route_missing"] is True
    assert (gate.status, gate.code) == (FAIL, "admin_wifi_backend_missing")
    assert "Wi-Fi status and volume read" in gate.hint
    assert "EMORI_ADMIN_HOST_BACKEND=daemon" in gate.hint and "set it to auto" in gate.hint
    assert "sudo systemctl restart emori-admin" in gate.hint


def _from_before_the_deep_check():
    """A process older than the checkout (not restarted after a deploy) answers 404."""

    from starlette.testclient import TestClient

    from fastapi import FastAPI

    old = FastAPI()

    @old.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "emori-admin"}

    return TestClient(old).get("/healthz/deep", headers=HEADER)


@pytest.mark.parametrize("evidence", [
    {"daemon": SHIM_ON_8000},                                 # v2: the shim answers
    {"daemon": HttpResult(False), "v2_fence": True},          # v2: node restarting
    {"daemon": HttpResult(False)},                            # nothing says which
], ids=["shim", "fence", "unknown"])
def test_an_emori_admin_from_before_the_deep_check_fails_unless_the_daemon_serves_wifi(
        env, evidence):
    gate = _gate(_from_before_the_deep_check(), **evidence)

    assert (gate.status, gate.code) == (FAIL, "admin_wifi_backend_missing")
    assert "older than this checkout" in gate.hint


def test_an_emori_admin_from_before_the_deep_check_is_a_warning_on_pollens_daemon(env):
    """On v1 the daemon serves /wifi and /api/volume, so the phone's
    tabs work; an old process there is a WARN to restart it, not a dead Network tab."""

    gate = _gate(_from_before_the_deep_check(), daemon=DAEMON_ON_8000)

    assert (gate.status, gate.code) == (WARN, "admin_deep_missing")
    assert "older than this checkout" in gate.hint
    assert "sudo systemctl restart emori-admin" in gate.hint


# --------------------------------------------------------------------------
# admin_stale: an emori-admin that kept running across a deploy
# --------------------------------------------------------------------------
def _write_marker(directory, text: str) -> None:
    (directory / "DEPLOYED_COMMIT.txt").write_text(text, encoding="utf-8")


def test_the_deep_check_reports_the_deploy_it_started_on(v2, monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    _write_marker(tmp_path, "abc1234def (master)\n")
    client = TestClient(server.create_app(background=False))   # the process starts
    _write_marker(tmp_path, "9876fedcba (master)\n")             # ...then a deploy lands

    body = client.get("/healthz/deep", headers=HEADER).json()

    assert body["commit"] == "abc1234def (master)"


def test_an_emori_admin_that_kept_running_across_a_deploy_fails_stale(v2, monkeypatch,
                                                                      tmp_path):
    from starlette.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    _write_marker(tmp_path, "abc1234def (master)\n")
    response = TestClient(server.create_app(background=False)).get(
        "/healthz/deep", headers=HEADER)

    stale = _gate(response, daemon=SHIM_ON_8000, deployed_commit="9876fedcba (master)")
    current = _gate(response, daemon=SHIM_ON_8000, deployed_commit="abc1234def (master)")

    assert (stale.status, stale.code) == (FAIL, "admin_stale")
    assert "abc1234def" in stale.hint and "9876fedcba" in stale.hint
    assert "master" not in stale.hint            # hex only reaches a spoken hint
    assert "sudo systemctl restart emori-admin" in stale.hint
    assert (current.status, current.code) == (PASS, "ok")


@pytest.mark.parametrize("payload_commit", [None, "", "not a commit"])
def test_an_emori_admin_that_cannot_say_its_deploy_is_stale_when_there_is_one(
        payload_commit):
    """A build from before the field (no `commit`), or one started before the first
    marker was written, is older than the marker on disk."""

    payload = {"service": "emori-admin", "backend": "host", "reason": "v2_status",
               "wifi": {"ok": True, "route_missing": False, "error": ""},
               "volume": {"ok": True, "route_missing": False, "error": ""}}
    if payload_commit is not None:
        payload["commit"] = payload_commit
    deep = HttpResult(True, 200, payload)

    gate = {r.name: r for r in readycheck.evaluate(_healthy(
        admin_deep=deep, deployed_commit="9876fedcba (master)"))}["admin"]
    without_marker = {r.name: r for r in readycheck.evaluate(_healthy(
        admin_deep=deep))}["admin"]

    assert (gate.status, gate.code) == (FAIL, "admin_stale")
    assert (without_marker.status, without_marker.code) == (PASS, "ok")


def test_readycheck_and_emori_admin_read_the_deploy_marker_the_same_way(tmp_path):
    from emori_adapter.admin import system

    for text in ("abc1234 (master)\n", "  abc1234 (fix/x)\r\nsecond line\n", "",
                 "\n\n", "x" * 500):
        _write_marker(tmp_path, text)
        assert system.deployed_marker(str(tmp_path)) == readycheck._marker_line(text)


def test_collect_reads_the_deploy_marker_and_the_v2_fence(monkeypatch, tmp_path):
    from emori_adapter import robot_token

    _write_marker(tmp_path, "abc1234 (master)\n")
    monkeypatch.setattr(robot_token, "install_root", lambda: tmp_path)
    probe = readycheck.SystemProbe()

    assert probe.deployed_commit() == "abc1234 (master)"
    assert probe.path_exists(str(tmp_path / "DEPLOYED_COMMIT.txt")) is True
    assert probe.path_exists(str(tmp_path / "V2_OWNS_THE_BUS")) is False


@pytest.mark.parametrize("missing, words", [
    ("nmcli", "sudo apt install network-manager"),
    ("alsa", "sudo apt install alsa-utils"),
])
def test_a_host_robot_without_its_tools_fails(v2, monkeypatch, missing, words):
    nm, alsa, _ = v2
    if missing == "nmcli":
        nm.missing = True
    else:
        alsa.missing.update({"aplay", "amixer"})

    gate = _gate(_deep())

    assert (gate.status, gate.code) == (FAIL, "admin_wifi_backend_missing")
    assert words in gate.hint


def test_a_daemon_that_is_restarting_is_only_a_warning(env):
    """v1, daemon down: not a deploy defect, so the matrix stays green."""

    _, http = env  # nothing answers on :8000 at all

    response = _deep()
    gate = _gate(response)

    assert response.json()["wifi"] == {"ok": False, "route_missing": False,
                                       "error": "daemon_down"}
    assert (gate.status, gate.code) == (WARN, "admin_backend_unavailable")
    assert "daemon_down" in gate.hint
    results = readycheck.evaluate(_healthy(admin_deep=HttpResult(
        True, response.status_code, response.json())))
    assert readycheck.exit_code(results) == 0


def test_a_sound_card_still_enumerating_is_only_a_warning(v2):
    _, alsa, _ = v2
    alsa.failing_controls.update({"PCM", "Mic"})

    gate = _gate(_deep())

    assert (gate.status, gate.code) == (WARN, "admin_backend_unavailable")
    assert "volume read (mixer_failed)" in gate.hint


@pytest.mark.parametrize("payload", [
    None,
    {"service": "emori-admin"},
    {"service": "emori-admin", "wifi": "ok", "volume": {}},
])
def test_an_answer_in_another_shape_is_a_warning_never_a_pass(payload):
    gate = {r.name: r for r in readycheck.evaluate(_healthy(
        admin_deep=HttpResult(True, 200, payload)))}["admin"]

    assert (gate.status, gate.code) == (WARN, "admin_backend_unavailable")


def test_words_outside_the_vocabulary_never_reach_the_hint():
    """The hint is printed and spoken; only slugs from the answer may appear in it."""

    deep = HttpResult(True, 200, {
        "service": "emori-admin", "backend": "Example Wi-Fi", "reason": "x y",
        "wifi": {"ok": False, "route_missing": True, "error": "psk hunter2"},
        "volume": {"ok": False, "route_missing": False, "error": "Reachy Mini Audio"}})

    gate = {r.name: r for r in readycheck.evaluate(_healthy(admin_deep=deep))}["admin"]

    assert (gate.status, gate.code) == (FAIL, "admin_wifi_backend_missing")
    for leak in ("Example Wi-Fi", "hunter2", "Reachy Mini Audio", "x y"):
        assert leak not in gate.hint


def test_readycheck_and_emori_admin_agree_on_what_cannot_be_fixed_by_waiting():
    assert readycheck._ADMIN_BACKEND_MISSING == backend_check.BACKEND_MISSING


def test_every_word_the_host_backend_can_answer_is_a_slug():
    """The deep answer carries error words verbatim; every one must be a safe slug."""

    words = backend_error_words()

    assert words >= {"nmcli_missing", "nmcli_timeout", "worker_failed", "host_error",
                     "alsa_missing", "mixer_failed", "route_missing", "daemon_down"}
    for word in words:
        assert readycheck.SAFE_CODE_RE.fullmatch(word), word
    assert backend_check.BACKEND_MISSING <= words


# --------------------------------------------------------------------------
# collect(): when and how readycheck asks
# --------------------------------------------------------------------------
class _Probe:
    def __init__(self, environ, healthz: HttpResult, deep: HttpResult) -> None:
        self.environ, self.healthz, self.deep = environ, healthz, deep
        self.asked: list[tuple[str, str]] = []

    def env(self, name, default=""):
        return self.environ.get(name, default)

    def http(self, url, *, bearer=""):
        return self.healthz if url.endswith(":8070/healthz") else HttpResult(False)

    def admin_deep(self, url, token):
        self.asked.append((url, token))
        return self.deep


def _collect(probe):
    from test_readycheck import _FakeProbe

    # Everything except the two admin calls comes from readycheck's own fake.
    fake = _FakeProbe(probe.environ)
    fake.http = probe.http
    fake.admin_deep = probe.admin_deep
    return readycheck.collect(fake)


def test_collect_asks_the_deep_check_with_the_token_in_a_header_only():
    deep = HttpResult(True, 200, {"service": "emori-admin"})
    probe = _Probe({"EMORI_ADMIN_TOKEN": TOKEN},
                   HttpResult(True, 200, {"status": "ok", "service": "emori-admin"}), deep)

    evidence = _collect(probe)

    assert probe.asked == [("http://127.0.0.1:8070/healthz/deep", TOKEN)]
    assert evidence.admin_deep == deep and evidence.admin_token_set is True
    assert TOKEN not in repr(evidence)


@pytest.mark.parametrize("environ, healthz", [
    ({}, HttpResult(True, 200, {"status": "ok", "service": "emori-admin"})),
    ({"EMORI_ADMIN_TOKEN": TOKEN}, HttpResult(False)),
    # Something else on EMORI_ADMIN_PORT never sees the token:
    # the hostapp's own /healthz, a stray server, an emori-admin that is not healthy.
    ({"EMORI_ADMIN_TOKEN": TOKEN}, HttpResult(True, 200, {"status": "ok"})),
    ({"EMORI_ADMIN_TOKEN": TOKEN}, HttpResult(True, 200, {"service": "emori-hostapp"})),
    ({"EMORI_ADMIN_TOKEN": TOKEN}, HttpResult(True, 404, None)),
    ({"EMORI_ADMIN_TOKEN": TOKEN}, HttpResult(True, 503, {"service": "emori-admin"})),
])
def test_collect_does_not_ask_without_a_token_or_a_live_service(environ, healthz):
    probe = _Probe(environ, healthz, HttpResult(True, 200, {}))

    evidence = _collect(probe)

    assert probe.asked == []
    assert evidence.admin_deep == HttpResult()


def test_collect_hands_the_deploy_marker_and_the_fence_to_the_gates():
    from test_readycheck import _FakeProbe

    fake = _FakeProbe({})
    fake.deployed_commit = lambda: "abc1234 (master)"
    fake.path_exists = lambda path: path == "/opt/emori/V2_OWNS_THE_BUS"

    evidence = readycheck.collect(fake)

    assert evidence.deployed_commit == "abc1234 (master)" and evidence.v2_fence is True
