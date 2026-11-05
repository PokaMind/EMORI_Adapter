"""The hostapp half of the phone's Pokamind tab: ``/api/admin/pokamind*``.

emori-admin deliberately holds no robot credential, so reading, verifying, replacing
and revealing THIS robot's token happens here, behind the same ``EMORI_ADMIN_TOKEN``
gate as the language switch. Pinned:

* a replacement is parsed, verified LIVE with the NEW token, refused while anything is
  running (before and after the network call), and only then written atomically;
* nothing but :meth:`PokamindAdmin.reveal` ever answers with the token, and the reveal
  is logged without its value;
* the status says which credential is in force and whether a saved one is waiting for
  the restart that makes it so.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from emori_adapter import robot_token
from emori_adapter.hostapp import admin_api, server
from emori_adapter.hostapp.admin_api import PokamindAdmin
from emori_adapter.hostapp.pokamind_whoami import Whoami, WhoamiResult
from tests.test_admin_api import (
    HEADER,
    TOKEN,
    _FakeMicProfile,
    _IdleRuntime,
    _RecordingPlayer,
)

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
SEED = GOLDEN_TOKEN.rsplit(".", 1)[1]


def _golden():
    return robot_token.parse_token(GOLDEN_TOKEN)


def _answer(robot_id: str = "test-robot-01", *, status: str = "active",
            ok: bool = True, code: str = "ok") -> WhoamiResult:
    if not ok:
        return WhoamiResult(code=code, http_status=401, checked_at=1.0)
    who = Whoami(robot_id=robot_id, display_name="Example Site", status=status,
                 credential_kind="fleet", key_fingerprint=None, key_version=1,
                 org="example-org", partner="emori-robot", course_id="example-course",
                 languages=("ca",), pathways=(), identity_domains=(), modules=(),
                 features={"feature_c": True, "feature_b": False, "feature_a": False})
    return WhoamiResult(ok=True, code="ok", http_status=200, whoami=who, checked_at=1.0)


class _Whoami:
    """A fake whoami: records which token asked, answers what the test says."""

    def __init__(self, result=None):
        self.result = result
        self.asked: list = []

    def __call__(self, base_url, token):
        self.asked.append((base_url, token))
        return self.result if self.result is not None else _answer(token.robot_id)


class _Saver:
    def __init__(self, fail: bool = False):
        self.saved: list = []
        self.fail = fail

    def __call__(self, token):
        if self.fail:
            raise PermissionError("read-only card")
        self.saved.append(token)
        return "/opt/emori/secrets/pokamind_robot_token"


def _admin(*, token=None, env_robot_id="", whoami=None, save=None,
           on_disk=None) -> PokamindAdmin:
    load = (robot_token.TokenLoad(token=token, source="file", path="/x")
            if token is not None else robot_token.TokenLoad())
    return PokamindAdmin(
        load=load, base_url="https://api.pokamind.com",
        env_robot_id=env_robot_id, whoami=whoami or _Whoami(), save=save or _Saver(),
        read_file=lambda: on_disk if on_disk is not None else load)


# --- status ---------------------------------------------------------------------------------


def test_the_status_of_a_token_robot_is_masked_and_complete():
    status, body = _admin(token=_golden()).status()

    assert status == 200
    assert body["configured"] is True and body["credential"] == "robot_token"
    assert body["robot_id"] == "test-robot-01"
    assert body["masked"] == _golden().masked
    assert body["fingerprint"] == "Vkdap1RjR0wChd9d"
    assert body["mismatch"] is False and body["restart_pending"] is False
    assert body["last_whoami"] is None
    assert SEED not in repr(body)


def test_the_status_of_a_robot_without_a_token_says_so():
    bare = _admin().status()[1]
    assert (bare["credential"], bare["configured"], bare["robot_id"]) == ("none", False, None)


def test_the_status_reports_a_conflicting_emori_robot_id_and_a_pending_restart():
    newer = robot_token.TokenLoad(token=robot_token.generate("test-robot-01"), source="file")

    body = _admin(token=_golden(), env_robot_id="Ward 3", on_disk=newer).status()[1]

    assert body["env_robot_id"] == "ward-3"
    assert body["mismatch"] is True
    assert body["restart_pending"] is True


def test_an_unusable_installed_token_is_reported_by_code():
    load = robot_token.TokenLoad(source="file", path="/x", error="seed_invalid")
    admin = PokamindAdmin(load=load, read_file=lambda: load)

    body = admin.status()[1]

    assert (body["configured"], body["token_error"], body["credential"]) == (
        False, "seed_invalid", "none")


# --- verify ---------------------------------------------------------------------------------


def test_verify_asks_pokamind_with_the_token_in_force_and_caches_the_answer():
    whoami = _Whoami()
    admin = _admin(token=_golden(), whoami=whoami)

    status, body = admin.verify()

    assert status == 200 and body["ok"] is True
    assert whoami.asked == [("https://api.pokamind.com", _golden())]
    assert admin.status()[1]["last_whoami"]["whoami"]["org"] == "example-org"


def test_verify_without_a_token_is_a_conflict_not_a_call():
    whoami = _Whoami()

    assert _admin(whoami=whoami).verify() == (409, {"error": "no_robot_token"})
    assert whoami.asked == []


def test_a_failed_verify_is_still_an_answer_the_page_can_render():
    admin = _admin(token=_golden(), whoami=_Whoami(_answer(ok=False, code="robot_revoked")))

    status, body = admin.verify()

    assert status == 200
    assert (body["ok"], body["last_whoami"]["code"]) == (False, "robot_revoked")


# --- replace --------------------------------------------------------------------------------


def _idle():
    return True


def test_a_replacement_is_verified_with_the_new_token_then_saved():
    whoami, saver = _Whoami(), _Saver()
    new = robot_token.generate("reception-01")
    admin = _admin(token=_golden(), whoami=whoami, save=saver)

    status, body = admin.replace(new.reveal(), _idle)

    assert status == 200
    assert whoami.asked == [("https://api.pokamind.com", new)]  # the NEW one asked
    assert saver.saved == [new]
    assert body["saved"] is True and body["restart_required"] is True
    assert body["robot_id"] == "reception-01" and body["robot_id_changed"] is True
    assert body["org"] == "example-org"
    assert new.reveal().rsplit(".", 1)[1] not in repr(body)


@pytest.mark.parametrize("raw, code", [
    ("", "token_empty"),
    ("pkr1.test-robot-01", "token_malformed"),
    (GOLDEN_TOKEN[:-1], "seed_invalid"),
    (None, "token_malformed"),
    (["list"], "token_malformed"),
    ("x" * 600, "token_malformed"),
])
def test_a_bad_paste_is_refused_before_anything_else_happens(raw, code):
    whoami, saver = _Whoami(), _Saver()

    status, body = _admin(whoami=whoami, save=saver).replace(raw, _idle)

    assert (status, body["error"], body["code"]) == (400, "token_invalid", code)
    assert whoami.asked == [] and saver.saved == []


def test_a_busy_robot_is_refused_before_the_network_call():
    whoami, saver = _Whoami(), _Saver()

    status, body = _admin(whoami=whoami, save=saver).replace(GOLDEN_TOKEN, lambda: False)

    assert (status, body) == (409, {"error": "busy"})
    assert whoami.asked == [] and saver.saved == []


def test_a_session_that_starts_during_the_check_still_blocks_the_write():
    answers = iter([True, False])
    saver = _Saver()

    status, _ = _admin(save=saver).replace(GOLDEN_TOKEN, lambda: next(answers))

    assert status == 409 and saver.saved == []


@pytest.mark.parametrize("result, code", [
    (_answer(ok=False, code="robot_revoked"), "robot_revoked"),
    (_answer(ok=False, code="whoami_unreachable"), "whoami_unreachable"),
    (_answer(status="revoked"), "robot_not_active"),
])
def test_a_token_pokamind_does_not_accept_is_never_saved(result, code):
    saver = _Saver()

    status, body = _admin(whoami=_Whoami(result), save=saver).replace(GOLDEN_TOKEN, _idle)

    assert (status, body["error"], body["code"]) == (422, "token_not_verified", code)
    assert saver.saved == []


def test_a_failed_write_is_a_500_with_a_code():
    status, body = _admin(save=_Saver(fail=True)).replace(GOLDEN_TOKEN, _idle)

    assert (status, body) == (500, {"error": "token_save_failed"})


def test_a_replacement_for_another_robot_than_emori_robot_id_says_so():
    status, body = _admin(env_robot_id="ward-3").replace(GOLDEN_TOKEN, _idle)

    assert status == 200 and body["env_robot_id_mismatch"] is True


def test_the_replacement_reaches_the_real_token_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets" / "pokamind_robot_token"
    monkeypatch.setenv("EMORI_POKAMIND_TOKEN_FILE", str(path))
    admin = PokamindAdmin(base_url="https://api.pokamind.com", whoami=_Whoami())

    assert admin.replace(GOLDEN_TOKEN, _idle)[0] == 200
    assert path.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    # ...and the status now says a restart is pending (nothing was in force before).
    assert admin.status()[1]["restart_pending"] is True


# --- reveal ---------------------------------------------------------------------------------


def test_reveal_returns_the_token_and_logs_only_that_it_happened(caplog):
    with caplog.at_level(logging.INFO, logger="emori.hostapp.admin_api"):
        status, body = _admin(token=_golden()).reveal()

    assert status == 200 and body == {"robot_id": "test-robot-01", "token": GOLDEN_TOKEN}
    lines = [r.getMessage() for r in caplog.records]
    assert any("REVEALED" in line for line in lines)
    assert all(SEED not in line for line in lines)


def test_nothing_to_reveal_is_a_conflict():
    assert _admin().reveal() == (409, {"error": "no_robot_token"})


# --- the routes -----------------------------------------------------------------------------

# emori-admin always calls the hostapp at 127.0.0.1 (admin/probes.hostapp_base): the
# Pokamind routes answer nobody else.
_ON_THE_ROBOT = ("127.0.0.1", 50000)
_POKAMIND_ROUTES = [("get", "/api/admin/pokamind"), ("post", "/api/admin/pokamind/verify"),
                    ("post", "/api/admin/pokamind/token"),
                    ("post", "/api/admin/pokamind/reveal")]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "kiosk_lang.json"))
    monkeypatch.setattr(server, "DEMO", False)
    admin = _admin(token=_golden())
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "player": _RecordingPlayer(),
        "fiware_factory": None, "idle": False, "watchdog": False, "gesture": False,
        "extension": _IdleRuntime(), "mic_profile": _FakeMicProfile(),
        "tts": None, "stt": None, "speak": lambda text, code: None,
        "pokamind_admin": admin,
    })
    return TestClient(app, client=_ON_THE_ROBOT)


def test_every_pokamind_route_is_behind_the_admin_token(client, monkeypatch):
    routes = _POKAMIND_ROUTES
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 401, path
    monkeypatch.delenv("EMORI_ADMIN_TOKEN")
    for method, path in routes:
        assert getattr(client, method)(path, headers=HEADER).status_code == 503, path


@pytest.mark.parametrize("peer", ["192.168.1.40", "10.42.0.17", "fe80::1", "testclient"])
def test_the_pokamind_routes_refuse_anyone_not_on_the_robot_even_with_the_token(client, peer):
    # The hostapp listens on 0.0.0.0 for the kiosk; the PIN re-entry and the wrong-PIN
    # limiter live in emori-admin, so the static admin token alone must not reach the
    # robot's credential from the site Wi-Fi.
    lan = TestClient(client.app, client=(peer, 50000))

    for method, path in _POKAMIND_ROUTES:
        kwargs = {"json": {"token": GOLDEN_TOKEN}} if path.endswith("/token") else {}
        reply = getattr(lan, method)(path, headers=HEADER, **kwargs)
        assert (reply.status_code, reply.json()) == (403, {"error": "local_only"}), path
        assert SEED not in reply.text
    # The language routes keep their existing contract (token only).
    assert lan.get("/api/admin/summary", headers=HEADER).status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_every_loopback_spelling_is_on_the_robot(host):
    assert admin_api._is_loopback(host) is True


@pytest.mark.parametrize("host", ["", None, "localhost", "192.168.1.40", "::ffff:10.0.0.1",
                                  "0.0.0.0", "testclient"])
def test_anything_else_is_not(host):
    assert admin_api._is_loopback(host) is False


def test_the_routes_answer_through_the_admin_object(client):
    status = client.get("/api/admin/pokamind", headers=HEADER)
    verify = client.post("/api/admin/pokamind/verify", headers=HEADER)
    reveal = client.post("/api/admin/pokamind/reveal", headers=HEADER)
    bad = client.post("/api/admin/pokamind/token", headers=HEADER, json={"token": "nope"})

    assert status.json()["robot_id"] == "test-robot-01" and SEED not in status.text
    assert verify.json()["ok"] is True
    assert reveal.json()["token"] == GOLDEN_TOKEN
    assert reveal.headers["cache-control"] == "no-store"
    assert (bad.status_code, bad.json()["code"]) == (400, "token_malformed")


def test_a_replacement_is_refused_while_a_session_is_running(monkeypatch, tmp_path):
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "kiosk_lang.json"))
    saver = _Saver()
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "player": _RecordingPlayer(),
        "fiware_factory": None, "idle": False, "watchdog": False, "gesture": False,
        "extension": _IdleRuntime(mode="running"), "mic_profile": _FakeMicProfile(),
        "tts": None, "stt": None, "speak": lambda text, code: None,
        "pokamind_admin": _admin(save=saver),
    })

    reply = TestClient(app, client=_ON_THE_ROBOT).post("/api/admin/pokamind/token",
                                                       headers=HEADER,
                                 json={"token": GOLDEN_TOKEN})

    assert (reply.status_code, reply.json()) == (409, {"error": "busy"})
    assert saver.saved == []


def test_a_mount_without_an_admin_object_still_answers_honestly(monkeypatch):
    from fastapi import FastAPI

    app = FastAPI()
    admin_api.mount(app, lang_state=None, driver=None, mic_profile=None,
                    robot_idle=lambda: True, set_lang=lambda code: code)
    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)

    body = TestClient(app, client=_ON_THE_ROBOT).get("/api/admin/pokamind",
                                                     headers=HEADER).json()

    assert (body["credential"], body["configured"]) == ("none", False)


# --- the scope: recorded on Verify, and for a NEW token before it is saved -------------


class _Scope:
    """A stand-in ScopeSync recording what the admin asked of it, in order."""

    def __init__(self, events=None, *, record_ok=True, sync_ok=True):
        self.events = events if events is not None else []
        self.record_ok = record_ok
        self.sync_ok = sync_ok

    def record_verified(self, whoami, token):
        self.events.append(("record", whoami.robot_id, token.robot_id))
        return self.record_ok

    def sync_now(self, reason="manual", *, result=None):
        from emori_adapter.hostapp.pokamind_scope import SyncOutcome

        self.events.append(("sync", reason, result))
        return SyncOutcome(self.sync_ok, "ok" if self.sync_ok else "scope_write_failed",
                           reason)


class _OrderedSaver(_Saver):
    def __init__(self, events, fail=False):
        super().__init__(fail=fail)
        self.events = events

    def __call__(self, token):
        self.events.append(("save", token.robot_id))
        return super().__call__(token)


def test_a_replacement_records_the_new_tokens_scope_before_the_save():
    events: list = []
    new = robot_token.generate("reception-01")
    admin = PokamindAdmin(load=robot_token.TokenLoad(token=_golden(), source="file"),
                          base_url="https://api.pokamind.com", whoami=_Whoami(),
                          save=_OrderedSaver(events), scope=_Scope(events),
                          read_file=lambda: robot_token.TokenLoad())

    status, body = admin.replace(new.reveal(), _idle)

    assert status == 200 and body["scope_recorded"] is True
    # The NEW robot's answer, recorded with the NEW token, then the token file.
    assert events == [("record", "reception-01", "reception-01"), ("save", "reception-01")]


@pytest.mark.parametrize("case", ["busy", "refused", "inactive", "malformed"])
def test_a_replacement_that_is_not_saved_records_no_scope(case):
    events: list = []
    result = {"refused": _answer(ok=False, code="robot_revoked"),
              "inactive": _answer(status="revoked")}.get(case)
    admin = _admin(whoami=_Whoami(result), save=_OrderedSaver(events))
    admin._scope = _Scope(events)

    raw = "pkr1.nope" if case == "malformed" else GOLDEN_TOKEN
    status, _ = admin.replace(raw, (lambda: False) if case == "busy" else _idle)

    assert status != 200
    assert events == []


def test_a_scope_that_cannot_be_recorded_does_not_block_the_replacement():
    events: list = []
    admin = _admin(whoami=_Whoami(), save=_OrderedSaver(events))
    admin._scope = _Scope(events, record_ok=False)

    status, body = admin.replace(GOLDEN_TOKEN, _idle)

    assert status == 200 and body["saved"] is True and body["scope_recorded"] is False
    assert [event[0] for event in events] == ["record", "save"]


def _join_restore(timeout_s: float = 5.0) -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name == "pokamind-scope-restore":
            thread.join(timeout=timeout_s)


def test_a_failed_save_leaves_a_snapshot_the_token_in_force_ignores_then_restores_it(
        tmp_path):
    """The new robot's snapshot is on disk before the save; when the save fails, the
    settings layer ignores it (another robot_id) and one sync puts this robot's back."""

    from emori_adapter import site_settings
    from emori_adapter.hostapp.pokamind_scope import ScopeSync
    from emori_adapter.hostapp.pokamind_whoami import parse_whoami
    from tests.test_pokamind_whoami import body

    current = _golden()
    new = robot_token.generate("reception-01")

    def answer(base_url, token):
        raw = body(robot_id=token.robot_id,
                   credential={"kind": "fleet", "key_fingerprint": None, "key_version": 1})
        who = parse_whoami(raw, robot_id=token.robot_id)
        return WhoamiResult(ok=True, code="ok", http_status=200, whoami=who, checked_at=1.0)

    scope = ScopeSync("https://api.pokamind.com", current, fetch=answer, root=tmp_path,
                      rebuild=lambda snapshot, out_dir: {})
    admin = PokamindAdmin(load=robot_token.TokenLoad(token=current, source="file"),
                          base_url="https://api.pokamind.com", whoami=answer,
                          save=_Saver(fail=True), scope=scope,
                          read_file=lambda: robot_token.TokenLoad())

    status, body_ = admin.replace(new.reveal(), _idle)

    assert (status, body_) == (500, {"error": "token_save_failed"})
    _join_restore()
    # This robot's own snapshot is back: the token in force sees its scope again...
    assert site_settings.scope_snapshot_status(
        current.robot_id, current.fingerprint, root=tmp_path) == "ok"
    assert scope.last_outcome().reason == "token_restore"


def test_the_new_robots_snapshot_is_ignored_by_the_token_still_in_force(tmp_path):
    from emori_adapter import site_settings
    from emori_adapter.hostapp.pokamind_scope import ScopeSync
    from emori_adapter.hostapp.pokamind_whoami import parse_whoami
    from tests.test_pokamind_whoami import body

    current = _golden()
    new = robot_token.generate("reception-01")
    raw = body(robot_id="reception-01",
               credential={"kind": "fleet", "key_fingerprint": None, "key_version": 1})
    who = parse_whoami(raw, robot_id="reception-01")
    scope = ScopeSync("https://api.pokamind.com", None, root=tmp_path,
                      rebuild=lambda snapshot, out_dir: {})

    assert scope.record_verified(who, new) is True
    assert site_settings.load_scope_snapshot(
        current.robot_id, current.fingerprint, root=tmp_path) is None
    assert site_settings.scope_snapshot_status(
        current.robot_id, current.fingerprint, root=tmp_path) == "scope_robot_mismatch"
    assert site_settings.load_scope_snapshot(
        "reception-01", new.fingerprint, root=tmp_path) is not None


def test_verify_records_the_answer_as_this_robots_scope():
    events: list = []
    admin = _admin(token=_golden())
    admin._scope = _Scope(events)

    status, body = admin.verify()

    assert status == 200 and body["ok"] is True and body["scope_recorded"] is True
    ((kind, reason, result),) = events
    # The answer it already has (no second whoami), through the notifying path.
    assert (kind, reason, result.ok) == ("sync", "admin_verify", True)


def test_a_failed_verify_records_nothing():
    events: list = []
    admin = _admin(token=_golden(), whoami=_Whoami(_answer(ok=False, code="robot_revoked")))
    admin._scope = _Scope(events)

    status, body = admin.verify()

    assert status == 200 and body["ok"] is False and body["scope_recorded"] is False
    assert events == []


def test_verify_base_asks_the_new_origin_with_the_token_in_force():
    whoami = _Whoami()
    admin = _admin(token=_golden(), whoami=whoami)

    assert admin.verify_base("https://api.example.test") == (True, "ok")
    assert whoami.asked == [("https://api.example.test", _golden())]
    assert _admin().verify_base("https://api.example.test") == (False, "no_robot_token")
    assert _admin(token=_golden(), whoami=_Whoami(_answer(status="revoked"))).verify_base(
        "https://api.example.test") == (False, "robot_not_active")
    assert _admin(token=_golden(), whoami=_Whoami(
        _answer(ok=False, code="whoami_unreachable"))).verify_base(
        "https://api.example.test") == (False, "whoami_unreachable")


def test_the_hostapp_builds_its_admin_with_the_scope_sync(monkeypatch, tmp_path):
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "kiosk_lang.json"))
    monkeypatch.setattr(server, "DEMO", False)
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "player": _RecordingPlayer(),
        "fiware_factory": None, "idle": False, "watchdog": False, "gesture": False,
        "extension": _IdleRuntime(), "mic_profile": _FakeMicProfile(),
        "tts": None, "stt": None, "speak": lambda text, code: None,
    })

    from emori_adapter.hostapp.pokamind_scope import ScopeSync

    assert isinstance(app.state.scope_sync, ScopeSync)
    # Built even without a token: the first token pasted on a fresh robot still
    # records its scope before the restart.
    assert app.state.scope_sync.token is None
