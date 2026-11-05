"""The hostapp half of the phone's Settings tab: ``/api/admin/site`` and the scope sync.

The settings files are written by THIS process (emori-admin holds no credential), so the
routes are guarded like the robot's token: the admin token AND this robot only. Pinned:

* ``GET`` lists every setting with where its value comes from -- and never the value of
  a secret;
* ``POST`` saves hostapp-owned, editable settings only, validates all of them before
  writing any, and saves a new ``POKAMIND_BASE`` only when whoami signed with the token
  in force answers from it;
* a save or a scope sync only RECORDS why a restart is pending: nothing
  here restarts anything;
* "Sync now" records this robot's scope (and the menus built from it) and reports what
  changed; the scope then feeds the language offer and the web-app origin.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from emori_adapter import robot_token, site_settings
from emori_adapter.hostapp import admin_api, pokamind_scope, server
from emori_adapter.hostapp.admin_api import PokamindAdmin
from emori_adapter.hostapp.pokamind_whoami import WhoamiResult, parse_whoami
from tests.test_admin_api import HEADER, TOKEN, _FakeMicProfile, _IdleRuntime, _RecordingPlayer
from tests.test_pokamind_whoami import GOLDEN_TOKEN, body

ON_THE_ROBOT = ("127.0.0.1", 50000)
SECRET_VALUE = "correct-horse-battery-staple"


def whoami_result(token, **overrides) -> WhoamiResult:
    raw = body(**overrides)
    raw["robot_id"] = token.robot_id
    raw["credential"] = {"kind": "fleet", "key_fingerprint": token.fingerprint,
                         "key_version": 1}
    who = parse_whoami(raw, robot_id=token.robot_id, fingerprint=token.fingerprint)
    return WhoamiResult(ok=True, code="ok", http_status=200, whoami=who, checked_at=1.0)


class FakeWhoami:
    """Answers for the token that asks; records (base_url, robot_id)."""

    def __init__(self, result=None, **overrides):
        self.result = result
        self.overrides = overrides
        self.asked: list = []

    def __call__(self, base_url, token):
        self.asked.append((base_url, token.robot_id))
        if self.result is not None:
            return self.result
        return whoami_result(token, **self.overrides)


# A record with a module in each kiosk language (the shared fixture's are Catalan only).
BOTH_LANGUAGES = [
    {"module_id": "example-ca-module-1", "title": "Atenció", "category": "Example pathway",
     "language": "ca", "available": True, "problem": None},
    {"module_id": "example-en-module-1", "title": "Front desk", "category": "Example pathway",
     "language": "en", "available": True, "problem": None},
]


def write_scope(root: Path, token, *, fetched_at: float | None = None, provisioned=False,
                **overrides) -> dict:
    """THIS robot's scope snapshot, as a verified sync would have written it."""

    who = whoami_result(token, **overrides).whoami
    snapshot = site_settings.merge_scope_snapshot(
        None, who.to_dict(), contract=who.contract, key_fingerprint=token.fingerprint,
        key_version=1, now=time.time() if fetched_at is None else fetched_at)
    site_settings.write_scope_snapshot(snapshot, root=root)
    return snapshot


class Rig:
    pass


def build(monkeypatch, tmp_path, *, token=None, whoami=None, scope_fetch=None,
          runtime=None, speak=None, demo=False, admin=True, overrides=None,
          env=None, lang="en") -> Rig:
    """A hostapp whose install root is ``tmp_path`` and whose environment holds no
    setting but ``env``."""

    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root=None: Path(root) if root is not None else tmp_path)
    monkeypatch.setattr(admin_api, "_dotenv_file", lambda: None)
    for key in (*site_settings.REGISTRY, site_settings.SOURCES_ENV,
                site_settings.PROFILE_ENV, site_settings.AUTO_ENV, "EMORI_VOICE_TOPICS",
                "EMORI_VOICE_TOPICS_EN", "EMORI_VOICE_TOPICS_CA"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "kiosk_lang.json"))
    monkeypatch.setattr(server, "EMORI_LANG", lang)
    if token is not None:
        # The configured view loads the token the way a restarted process would.
        monkeypatch.setenv("POKAMIND_ROBOT_TOKEN", token.reveal())
    monkeypatch.setattr(server, "DEMO", demo)
    monkeypatch.setattr(server, "POKAMIND_BASE", "https://api.pokamind.com")
    load = (robot_token.TokenLoad(token=token, source="file") if token is not None
            else robot_token.TokenLoad())
    monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", load)
    monkeypatch.setattr(server, "ROBOT_TOKEN", token)
    rig = Rig()
    rig.whoami = whoami if whoami is not None else FakeWhoami()
    base = {
        "robot_client": DryRunRobotClient(), "player": _RecordingPlayer(),
        "fiware_factory": None, "idle": False, "watchdog": False, "gesture": False,
        "extension": runtime if runtime is not None else _IdleRuntime(),
        "mic_profile": _FakeMicProfile(), "tts": None, "stt": None,
        "speak": speak if speak is not None else (lambda text, code: None),
        "scope_fetch": scope_fetch if scope_fetch is not None else rig.whoami,
    }
    if admin:
        base["pokamind_admin"] = PokamindAdmin(
            load=load, base_url="https://api.pokamind.com", whoami=rig.whoami,
            read_file=lambda: load)
    base.update(overrides or {})
    rig.app = server._build_app(overrides=base)
    rig.root = tmp_path
    rig.client = TestClient(rig.app, client=ON_THE_ROBOT)
    rig.lan = TestClient(rig.app, client=("192.168.1.40", 50000))
    return rig


def _key(listing: dict, name: str) -> dict:
    return next(row for row in listing["keys"] if row["key"] == name)


@pytest.fixture()
def rig(monkeypatch, tmp_path) -> Rig:
    return build(monkeypatch, tmp_path, token=robot_token.parse_token(GOLDEN_TOKEN))


# --- the guard ----------------------------------------------------------------------

_ROUTES = [("get", "/api/admin/site", None), ("post", "/api/admin/site", {"EMORI_FIWARE": "0"}),
           ("post", "/api/admin/scope/sync", None), ("get", "/api/admin/menu", None),
           ("post", "/api/admin/menu/save", {"lang": "en"}),
           ("post", "/api/admin/menu/reset", {"lang": "en"}),
           ("post", "/api/admin/menu/preview", {"lang": "en"}),
           ("post", "/api/admin/results-email/test", {"to": "a@example.org"})]


@pytest.mark.parametrize("method, path, payload", _ROUTES)
def test_every_new_route_answers_nobody_off_the_robot(rig, method, path, payload):
    kwargs = {"json": payload} if payload is not None else {}

    reply = getattr(rig.lan, method)(path, headers=HEADER, **kwargs)

    assert (reply.status_code, reply.json()) == (403, {"error": "local_only"})
    assert not (rig.root / site_settings.SETTINGS_FILE).exists()


@pytest.mark.parametrize("method, path, payload", _ROUTES)
def test_every_new_route_needs_the_admin_token(rig, monkeypatch, method, path, payload):
    kwargs = {"json": payload} if payload is not None else {}

    assert getattr(rig.client, method)(path, **kwargs).status_code == 401
    monkeypatch.delenv("EMORI_ADMIN_TOKEN")
    assert getattr(rig.client, method)(path, headers=HEADER, **kwargs).status_code == 503


# --- GET ----------------------------------------------------------------------------


def test_the_listing_names_every_setting_and_where_it_comes_from(rig, monkeypatch):
    monkeypatch.setenv("EMORI_COURSE_ID", "course-from-env")

    listing = rig.client.get("/api/admin/site", headers=HEADER)

    assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
    body_ = listing.json()
    assert {row["key"] for row in body_["keys"]} == set(site_settings.REGISTRY)
    course = _key(body_, "EMORI_COURSE_ID")
    assert course == {"key": "EMORI_COURSE_ID", "owner": "hostapp",
                      "value": "course-from-env", "set": True, "source": "env",
                      "secret": False, "editable": True, "restart": "hostapp",
                      "running_differs": False, "problem": None}
    assert _key(body_, "EMORI_SMTP_HOST")["set"] is False
    assert _key(body_, "EMORI_SMTP_HOST")["source"] is None
    assert _key(body_, "EMORI_ADMIN_PIN")["editable"] is False
    assert body_["restart_pending"] is False and body_["restart_reasons"] == []


def test_the_listing_never_returns_a_secret_value(rig, monkeypatch):
    monkeypatch.setenv("EMORI_ADMIN_PIN", "123456")
    monkeypatch.setenv("EMORI_DASH_PASSWORD", SECRET_VALUE + "-dash")
    saved = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"EMORI_SMTP_PASS": SECRET_VALUE})
    assert saved.status_code == 200

    listing = rig.client.get("/api/admin/site", headers=HEADER)

    assert SECRET_VALUE not in listing.text and "123456" not in listing.text
    password = _key(listing.json(), "EMORI_SMTP_PASS")
    assert password["value"] is None and password["set"] is True
    assert password["source"] == "admin" and password["secret"] is True
    # Saved, not yet in force: the hostapp runs with what it started with.
    assert password["running_differs"] is True
    pin = _key(listing.json(), "EMORI_ADMIN_PIN")
    assert pin["value"] is None and pin["set"] is True and pin["owner"] == "admin"


def test_a_value_derived_from_the_record_shows_its_source_and_the_scope(rig):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(rig.root, token, course_id="course-from-the-record",
                display_name="Reception desk")

    body_ = rig.client.get("/api/admin/site", headers=HEADER).json()

    course = _key(body_, "EMORI_COURSE_ID")
    assert (course["value"], course["source"]) == ("course-from-the-record", "pokamind")
    # Derived after this process started: a restart puts it in force.
    assert course["running_differs"] is True
    scope = body_["scope"]
    assert scope["status"] == "ok" and scope["robot_id"] == "test-robot-01"
    assert scope["digest"].startswith("sha256:") and scope["stale"] is False
    assert scope["derived"]["EMORI_ROBOT_NAME"] == "Reception desk"
    assert scope["auto"] is True and scope["last_sync_code"] is None


def test_a_snapshot_older_than_a_week_is_stale(rig):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(rig.root, token, fetched_at=time.time() - site_settings.SCOPE_STALE_S - 60)

    assert rig.client.get("/api/admin/site", headers=HEADER).json()["scope"]["stale"] is True


def test_another_robots_snapshot_is_reported_and_never_used(rig):
    other = robot_token.generate("another-robot-02")
    write_scope(rig.root, other, course_id="not-ours")

    body_ = rig.client.get("/api/admin/site", headers=HEADER).json()

    assert body_["scope"]["status"] == "scope_robot_mismatch"
    assert body_["scope"]["robot_id"] is None
    assert _key(body_, "EMORI_COURSE_ID")["set"] is False


def test_a_refused_saved_value_is_reported_by_code(rig):
    path = rig.root / site_settings.SETTINGS_FILE
    path.parent.mkdir(parents=True)
    path.write_text("EMORI_SMTP_PORT=seventy\n", encoding="utf-8")

    row = _key(rig.client.get("/api/admin/site", headers=HEADER).json(), "EMORI_SMTP_PORT")

    assert row["problem"] == "value_not_port" and row["set"] is False


# --- POST ---------------------------------------------------------------------------


def test_a_save_writes_each_key_to_its_own_file_and_asks_for_a_restart(rig):
    reply = rig.client.post("/api/admin/site", headers=HEADER, json={
        "EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_PASS": SECRET_VALUE,
        "EMORI_FIWARE": "0"})

    assert reply.status_code == 200
    assert reply.json() == {
        "saved": ["EMORI_FIWARE", "EMORI_SMTP_HOST", "EMORI_SMTP_PASS"],
        "restart_required": ["EMORI_FIWARE", "EMORI_SMTP_HOST", "EMORI_SMTP_PASS"]}
    assert SECRET_VALUE not in reply.text
    settings_file = (rig.root / site_settings.SETTINGS_FILE).read_text(encoding="utf-8")
    secrets_file = (rig.root / site_settings.SITE_SECRETS_FILE).read_text(encoding="utf-8")
    assert "EMORI_SMTP_HOST=smtp.example.org" in settings_file
    assert SECRET_VALUE not in settings_file
    assert f"EMORI_SMTP_PASS={SECRET_VALUE}" in secrets_file
    if os.name == "posix":
        assert (rig.root / site_settings.SITE_SECRETS_FILE).stat().st_mode & 0o777 == 0o600
    status = rig.client.get("/api/admin/site", headers=HEADER).json()
    assert status["restart_pending"] is True
    assert status["restart_reasons"] == ["settings_saved"]
    # Nothing in the running process moved: the next start applies it.
    assert os.environ.get("EMORI_SMTP_HOST") is None


def test_null_or_empty_removes_a_saved_value(rig):
    rig.client.post("/api/admin/site", headers=HEADER,
                    json={"EMORI_SMTP_HOST": "smtp.example.org", "EMORI_RECORD": "0"})

    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"EMORI_SMTP_HOST": None, "EMORI_RECORD": ""})

    assert reply.status_code == 200
    assert site_settings.read_env_file(rig.root / site_settings.SETTINGS_FILE) == {}


@pytest.mark.parametrize("key, value, code", [
    ("EMORI_SMTP_PORT", "70000", "value_not_port"),
    ("EMORI_SMTP_FROM", "not-an-address", "value_not_email"),
    ("EMORI_LANG", "fr", "value_not_choice"),
    ("EMORI_FIWARE", "yes", "value_not_bool"),
    ("EMORI_DASH_RETENTION_DAYS", "99999", "value_out_of_range"),
    ("EMORI_ROBOT_NAME", "two\nlines", "value_has_newline"),
    ("POKAMIND_BASE", "http://pokamind.example", "value_not_https"),
    ("ORION_URL", "ftp://broker", "value_not_url"),
    ("EMORI_SMTP_HOST", 587, "value_not_text"),
])
def test_a_refused_value_is_a_400_with_its_slug_and_nothing_is_written(rig, key, value,
                                                                      code):
    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"EMORI_RECORD": "0", key: value})

    assert reply.status_code == 400
    assert reply.json() == {"error": "invalid_value", "key": key, "code": code}
    assert not (rig.root / site_settings.SETTINGS_FILE).exists()
    assert rig.whoami.asked == []


@pytest.mark.parametrize("key, code", [
    ("EMORI_ADMIN_PIN", "key_wrong_owner"),
    ("EMORI_AP_PASSWORD", "key_wrong_owner"),
    ("EMORI_DASH_PASSWORD", "key_wrong_owner"),
    ("EMORI_ROBOT_ID", "key_unknown"),
    ("PATH", "key_unknown"),
])
def test_a_key_this_route_does_not_own_is_refused(rig, key, code):
    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"EMORI_RECORD": "0", key: "600"})

    assert reply.status_code == 400
    assert reply.json() == {"error": "key_not_editable", "key": key, "code": code}
    assert not (rig.root / site_settings.SETTINGS_FILE).exists()
    assert not (rig.root / site_settings.ADMIN_SECRETS_FILE).exists()


@pytest.mark.parametrize("payload", [{}, [], "EMORI_FIWARE=0", None])
def test_a_body_that_is_not_a_mapping_of_settings_is_a_400(rig, payload):
    reply = rig.client.post("/api/admin/site", headers=HEADER, json=payload)

    assert reply.status_code in (400, 422)


def test_a_client_supplied_key_is_never_echoed_unless_it_is_a_name(rig):
    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"<script>alert(1)</script>": "x"})

    assert reply.status_code == 400 and reply.json()["key"] is None
    assert "<script>" not in reply.text


# --- POKAMIND_BASE: verified before it is saved --------------------------------------


def test_a_new_origin_is_saved_only_after_whoami_answers_from_it(rig):
    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"POKAMIND_BASE": " https://api.example.test/ "})

    assert reply.status_code == 200 and reply.json()["saved"] == ["POKAMIND_BASE"]
    # Asked from the NEW origin, signed with the token in force.
    assert rig.whoami.asked == [("https://api.example.test", "test-robot-01")]
    saved = site_settings.read_env_file(rig.root / site_settings.SETTINGS_FILE)
    assert saved == {"POKAMIND_BASE": "https://api.example.test"}


@pytest.mark.parametrize("value", ["/", "   ", " // "])
def test_an_origin_that_normalises_to_nothing_is_refused_not_a_removal(rig, value):
    site_settings.save_settings({"POKAMIND_BASE": "https://api.example.test"}, root=rig.root)

    reply = rig.client.post("/api/admin/site", headers=HEADER, json={"POKAMIND_BASE": value})

    assert reply.status_code == 400
    assert reply.json()["code"] == "value_not_origin"
    assert rig.whoami.asked == []
    saved = site_settings.read_env_file(rig.root / site_settings.SETTINGS_FILE)
    assert saved == {"POKAMIND_BASE": "https://api.example.test"}


@pytest.mark.parametrize("answer, code", [
    (WhoamiResult(code="whoami_unreachable"), "whoami_unreachable"),
    (WhoamiResult(code="whoami_auth_failed", http_status=401), "whoami_auth_failed"),
    ("revoked", "robot_not_active"),
])
def test_an_origin_that_does_not_answer_for_this_robot_is_refused(monkeypatch, tmp_path,
                                                                  answer, code):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    result = whoami_result(token, status="revoked") if answer == "revoked" else answer
    rig = build(monkeypatch, tmp_path, token=token, whoami=FakeWhoami(result))

    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"POKAMIND_BASE": "https://api.example.test"})

    assert reply.status_code == 422
    assert reply.json() == {"error": "base_not_verified", "code": code}
    assert not (rig.root / site_settings.SETTINGS_FILE).exists()


def test_a_robot_without_a_token_cannot_move_its_origin(monkeypatch, tmp_path):
    rig = build(monkeypatch, tmp_path, token=None)

    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"POKAMIND_BASE": "https://api.example.test"})

    assert (reply.status_code, reply.json()) == (
        422, {"error": "base_not_verified", "code": "no_robot_token"})
    assert rig.whoami.asked == []


def test_removing_the_saved_origin_needs_no_whoami(rig):
    rig.client.post("/api/admin/site", headers=HEADER,
                    json={"POKAMIND_BASE": "https://api.example.test"})
    rig.whoami.asked.clear()

    reply = rig.client.post("/api/admin/site", headers=HEADER, json={"POKAMIND_BASE": None})

    assert reply.status_code == 200 and rig.whoami.asked == []


# --- the session-start deadlines keep their order -------------------------------------
# The page's own deadline speaks its line and goes home first, the adapter's backstop
# covers a page that cannot. A save that reorders them makes the backstop end a session the
# page was still waiting for.

_REACHY = {"EMORI_HARDWARE_PROFILE": "reachy-mini-wireless"}   # 120000 / 130


@pytest.mark.parametrize("payload, blamed", [
    ({"EMORI_SESSION_START_S": "60"}, "EMORI_SESSION_START_S"),        # below the page's 120
    ({"EMORI_SESSION_START_MS": "600000"}, "EMORI_SESSION_START_MS"),  # above the robot's 130
])
def test_a_save_that_reorders_the_deadlines_is_refused(monkeypatch, tmp_path, payload,
                                                        blamed):
    rig = build(monkeypatch, tmp_path, token=robot_token.parse_token(GOLDEN_TOKEN),
                env=_REACHY)

    reply = rig.client.post("/api/admin/site", headers=HEADER, json=payload)

    assert reply.status_code == 400
    assert reply.json() == {"error": "invalid_value", "key": blamed,
                            "code": "value_breaks_deadline_order"}
    assert not (rig.root / site_settings.SETTINGS_FILE).exists()


@pytest.mark.parametrize("payload", [
    {"EMORI_SESSION_START_S": "150"},
    {"EMORI_SESSION_START_MS": "130000"},                 # equal to the robot's 130: allowed
    {"EMORI_SESSION_START_S": "300"},                     # nothing behind it with no add-on
    {"EMORI_SESSION_START_MS": "300000", "EMORI_SESSION_START_S": "310"},
    {"EMORI_SESSION_START_S": "0"},                       # the backstop off races nothing
])
def test_a_save_that_keeps_the_order_is_saved(monkeypatch, tmp_path, payload):
    rig = build(monkeypatch, tmp_path, token=robot_token.parse_token(GOLDEN_TOKEN),
                env=_REACHY)

    reply = rig.client.post("/api/admin/site", headers=HEADER, json=payload)

    assert reply.status_code == 200, reply.json()
    assert reply.json()["saved"] == sorted(payload)


def test_clearing_a_saved_deadline_is_judged_on_the_value_it_falls_back_to(monkeypatch,
                                                                           tmp_path):
    """Empty = the layer below takes over: here .env's 100 s backstop, which the saved
    300 s page deadline would then outrun."""

    rig = build(monkeypatch, tmp_path, token=robot_token.parse_token(GOLDEN_TOKEN),
                env={**_REACHY, "EMORI_SESSION_START_S": "100"})
    first = rig.client.post("/api/admin/site", headers=HEADER, json={
        "EMORI_SESSION_START_MS": "90000", "EMORI_SESSION_START_S": "310"})
    assert first.status_code == 200
    raised = rig.client.post("/api/admin/site", headers=HEADER,
                             json={"EMORI_SESSION_START_MS": "300000"})
    assert raised.status_code == 200

    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"EMORI_SESSION_START_S": None})

    assert reply.status_code == 400 and reply.json()["key"] == "EMORI_SESSION_START_S"


def test_an_older_conflict_does_not_block_an_unrelated_save(monkeypatch, tmp_path):
    rig = build(monkeypatch, tmp_path, token=robot_token.parse_token(GOLDEN_TOKEN),
                env={**_REACHY, "EMORI_SESSION_START_S": "60"})   # .env, below 120

    assert rig.client.post("/api/admin/site", headers=HEADER,
                           json={"EMORI_CUE_AMP": "1.2"}).status_code == 200


# --- what an add-on registers ---------------------------------------------------------


def _addon_origin(monkeypatch) -> str:
    key = "EMORI_EXAMPLE_ORIGIN"
    monkeypatch.setitem(site_settings.REGISTRY, key, site_settings.make_spec(
        key, site_settings.origin_check, verified_origin="example_not_verified"))
    return key


def test_a_registered_origin_is_saved_only_after_whoami_answers_from_it(monkeypatch, tmp_path):
    key = _addon_origin(monkeypatch)
    token = robot_token.parse_token(GOLDEN_TOKEN)
    rig = build(monkeypatch, tmp_path, token=token, whoami=FakeWhoami())

    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={key: " https://other.example.test/ "})

    assert reply.status_code == 200 and reply.json()["saved"] == [key]
    assert site_settings.read_env_file(
        tmp_path / site_settings.SETTINGS_FILE)[key] == "https://other.example.test"


def test_a_registered_origin_that_does_not_answer_gets_its_own_error(monkeypatch, tmp_path):
    key = _addon_origin(monkeypatch)
    rig = build(monkeypatch, tmp_path, token=robot_token.parse_token(GOLDEN_TOKEN),
                whoami=FakeWhoami(WhoamiResult(code="whoami_unreachable")))

    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={key: "https://other.example.test"})

    assert (reply.status_code, reply.json()) == (
        422, {"error": "example_not_verified", "code": "whoami_unreachable"})
    assert not (rig.root / site_settings.SETTINGS_FILE).exists()


def test_a_line_no_setting_owns_is_never_served_and_survives_a_save(rig):
    secrets = rig.root / site_settings.SITE_SECRETS_FILE
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text("EMORI_EXAMPLE_ADDON_KEYS=do-not-echo\n", encoding="utf-8")

    listing = rig.client.get("/api/admin/site", headers=HEADER)
    saved = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"EMORI_SMTP_PASS": "s3cret-value"})
    again = rig.client.get("/api/admin/site", headers=HEADER)

    assert saved.status_code == 200
    for reply in (listing, saved, again):
        assert "do-not-echo" not in reply.text
        assert "EMORI_EXAMPLE_ADDON_KEYS" not in reply.text
    assert "EMORI_EXAMPLE_ADDON_KEYS=do-not-echo\n" in secrets.read_text(encoding="utf-8")


# --- the scope sync -----------------------------------------------------------------


def test_sync_now_records_the_scope_and_builds_the_menus(rig):
    # The development menu is what the kiosk serves (and caches) before the first sync.
    assert server.menu_source("en") == "dev"
    assert rig.client.get("/api/voice-menu").json()["options"]
    modules = [{"module_id": "AAAAAAAAAAAAAAAAAAAA", "title": "Customer greetings",
                "category": "Example pathway", "language": "en", "available": True,
                "problem": None}]
    rig.whoami.overrides = {"modules": modules, "languages": ["ca", "en"]}

    reply = rig.client.post("/api/admin/scope/sync", headers=HEADER)

    assert reply.status_code == 200
    outcome = reply.json()
    assert outcome["ok"] is True and outcome["code"] == "ok"
    assert outcome["reason"] == "admin_sync" and outcome["menus_changed"] is True
    assert set(outcome) == {"ok", "code", "reason", "changed_keys", "restart_reasons",
                            "menus_changed", "checked_at"}
    assert (rig.root / site_settings.SCOPE_FILE).exists()
    assert (rig.root / "configs/site/auto/voice_topics.en.yaml").exists()
    # The built menu is live at once (the cache was dropped), no restart.
    assert server.menu_source("en") == "auto"
    menu = rig.client.get("/api/voice-menu").json()
    assert [option["name"] for option in menu["options"]] == ["Customer greetings"]
    status = rig.client.get("/api/admin/site", headers=HEADER).json()["scope"]
    assert status["last_sync_code"] == "ok" and status["last_sync_reason"] == "admin_sync"


def test_a_failed_sync_keeps_the_last_good_snapshot(rig):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    before = write_scope(rig.root, token)
    rig.whoami.result = WhoamiResult(code="whoami_unreachable")

    outcome = rig.client.post("/api/admin/scope/sync", headers=HEADER).json()

    assert (outcome["ok"], outcome["code"]) == (False, "whoami_unreachable")
    after, _ = site_settings.read_scope_file(root=rig.root)
    assert after["digest"] == before["digest"]
    status = rig.client.get("/api/admin/site", headers=HEADER).json()
    assert status["scope"]["last_sync_code"] == "whoami_unreachable"
    assert status["restart_pending"] is False


def test_sync_now_without_a_token_answers_with_a_code(monkeypatch, tmp_path):
    rig = build(monkeypatch, tmp_path, token=None)

    outcome = rig.client.post("/api/admin/scope/sync", headers=HEADER).json()

    assert (outcome["ok"], outcome["code"]) == (False, "no_robot_token")
    assert rig.whoami.asked == []


def test_the_background_sync_runs_only_on_a_live_robot_with_a_token(monkeypatch, tmp_path):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    live = build(monkeypatch, tmp_path, token=token)
    with TestClient(live.app):
        assert live.app.state.scope_sync.running is True
    assert live.app.state.scope_sync.running is False
    # The first sync waits 10 s: nothing was asked during this short life.
    assert live.whoami.asked == []

    demo = build(monkeypatch, tmp_path, token=token, demo=True)
    with TestClient(demo.app):
        assert demo.app.state.scope_sync.running is False

    bare = build(monkeypatch, tmp_path, token=None)
    with TestClient(bare.app):
        assert bare.app.state.scope_sync.running is False


# --- what the scope feeds -----------------------------------------------------------


def test_the_kiosk_offers_only_the_languages_of_the_scope(monkeypatch, tmp_path):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, languages=["en"])
    rig = build(monkeypatch, tmp_path, token=token)

    summary = rig.client.get("/api/admin/summary", headers=HEADER).json()
    refused = rig.client.post("/api/admin/lang", headers=HEADER, json={"lang": "ca"})

    assert rig.app.state.lang_state.allowed == ("en",)
    assert summary["allowed_langs"] == ["en"]
    assert (refused.status_code, refused.json()) == (400, {"error": "unsupported_lang"})


def test_the_language_offer_ignores_the_scope_with_the_kill_switch(monkeypatch, tmp_path):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, languages=["en"])
    rig = build(monkeypatch, tmp_path, token=token, env={"EMORI_SITE_AUTO": "0"})

    assert set(rig.app.state.lang_state.allowed) == {"ca", "en"}


def test_a_sync_that_changes_the_language_offer_applies_it_live(monkeypatch, tmp_path):
    """The derived settings do not change here (the first language stays English):
    only the offer does, and the running language state takes it with no restart."""

    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, languages=["en", "ca"], modules=BOTH_LANGUAGES)
    rig = build(monkeypatch, tmp_path, token=token)
    assert rig.app.state.lang_state.allowed == ("en", "ca")
    assert rig.client.post("/api/admin/lang", headers=HEADER,
                           json={"lang": "ca"}).status_code == 200

    rig.whoami.overrides = {"languages": ["en", "ca"], "modules": BOTH_LANGUAGES}
    same = rig.client.post("/api/admin/scope/sync", headers=HEADER).json()
    assert same["ok"] is True and same["changed_keys"] == []
    assert rig.client.get("/api/admin/site", headers=HEADER).json()["restart_pending"] is False

    rig.whoami.overrides = {"languages": ["en"], "modules": BOTH_LANGUAGES}
    monkeypatch.setattr(pokamind_scope, "ADMIN_MIN_INTERVAL_S", 0.0)
    changed = rig.client.post("/api/admin/scope/sync", headers=HEADER).json()
    assert changed["ok"] is True and changed["changed_keys"] == []

    # Live: the offer is the record's, the kiosk left the language it no longer has
    # (nothing runs, and EMORI_LANG is not set on this robot)...
    assert rig.app.state.lang_state.allowed == ("en",)
    assert rig.app.state.lang_state.current() == "en"
    summary = rig.client.get("/api/admin/summary", headers=HEADER).json()
    assert summary["allowed_langs"] == ["en"]
    refused = rig.client.post("/api/admin/lang", headers=HEADER, json={"lang": "ca"})
    assert (refused.status_code, refused.json()) == (400, {"error": "unsupported_lang"})
    # ...and nothing waits for a restart.
    status = rig.client.get("/api/admin/site", headers=HEADER).json()
    assert status["restart_pending"] is False and status["restart_reasons"] == []


def test_a_language_state_without_a_live_setter_gets_the_offer_at_the_restart(
        monkeypatch, tmp_path):
    """An injected language state that cannot take a new offer while running records
    the restart reason instead (the next start reads the new offer)."""

    from emori_adapter.hostapp.lang_state import LangState

    class _Fixed(LangState):
        set_allowed = None

    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, languages=["en", "ca"])
    fixed = _Fixed(default="en", path=str(tmp_path / "kiosk_lang.json"),
                   allowed=("en", "ca"))
    rig = build(monkeypatch, tmp_path, token=token, overrides={"lang_state": fixed})

    rig.whoami.overrides = {"languages": ["en"]}
    assert rig.client.post("/api/admin/scope/sync", headers=HEADER).json()["ok"] is True

    assert fixed.allowed == ("en", "ca")
    status = rig.client.get("/api/admin/site", headers=HEADER).json()
    assert status["restart_reasons"] == ["scope_changed"]


def test_the_session_page_posts_to_the_app_origin_the_record_names(monkeypatch, tmp_path):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, app_origin="https://learn.example.test")
    rig = build(monkeypatch, tmp_path, token=token)
    monkeypatch.delenv("EMORI_TT_TARGET_ORIGINS", raising=False)

    page = rig.client.get("/session?sid=walkin").text
    landing = rig.client.get("/").text

    assert "https://learn.example.test" in page
    assert "https://app.pokamind.com" not in page  # the hostname guess is not needed
    assert 'href="https://learn.example.test"' in landing


def test_without_an_app_origin_the_guess_from_the_api_origin_stays(monkeypatch, tmp_path):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token)
    rig = build(monkeypatch, tmp_path, token=token)
    monkeypatch.delenv("EMORI_TT_TARGET_ORIGINS", raising=False)

    page = rig.client.get("/session?sid=walkin").text

    assert "https://app.pokamind.com" in page and "https://api.pokamind.com" in page


def test_the_kill_switch_drops_the_app_origin_too(monkeypatch, tmp_path):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, app_origin="https://learn.example.test")
    rig = build(monkeypatch, tmp_path, token=token)
    monkeypatch.setenv("EMORI_SITE_AUTO", "0")
    monkeypatch.delenv("EMORI_TT_TARGET_ORIGINS", raising=False)

    page = rig.client.get("/session?sid=walkin").text

    assert "https://learn.example.test" not in page


def test_the_site_runtime_reads_the_snapshot_again_when_the_file_changes(tmp_path):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    site = admin_api.SiteRuntime(token=token, root=tmp_path)
    assert site.snapshot() is None and site.scope_status() == "scope_missing"

    write_scope(tmp_path, token, app_origin="https://learn.example.test")
    assert site.app_origin() == "https://learn.example.test"

    time.sleep(0.01)
    write_scope(tmp_path, token, app_origin="https://other.example.test",
                course_id="changed")
    assert site.app_origin() == "https://other.example.test"
    assert admin_api.SiteRuntime(token=None).scope_status() == "no_token"


def test_restart_reasons_are_names_only_and_deduplicated():
    site = admin_api.SiteRuntime()

    site.add_reasons(["scope_changed", "scope_changed", "Not A Reason", None, "x" * 40])

    assert site.reasons() == ["scope_changed"] and site.restart_pending is True


def test_the_configured_view_never_touches_the_process_environment(rig, monkeypatch):
    rig.client.post("/api/admin/site", headers=HEADER, json={"EMORI_RECORD": "0"})
    before = dict(os.environ)

    env, sources = admin_api.configured_environment()

    assert dict(os.environ) == before
    assert env["EMORI_RECORD"] == "0" and sources["EMORI_RECORD"] == "admin"


# --- the results e-mail test --------------------------------------------------------


class _Smtp:
    sent: list = []

    def __init__(self, host, port, timeout=15):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        # The certificate is checked unless EMORI_SMTP_TLS_VERIFY=0.
        assert context is not None and context.check_hostname is True

    def login(self, user, password):
        _Smtp.sent.append(("login", user, password))

    def send_message(self, msg):
        _Smtp.sent.append(("send", self.host, self.port, msg["From"], msg["To"]))


def test_the_test_email_uses_the_settings_saved_on_the_page(rig, monkeypatch):
    import smtplib

    _Smtp.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _Smtp)
    rig.client.post("/api/admin/site", headers=HEADER, json={
        "EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_PORT": "2525",
        "EMORI_SMTP_USER": "robot@example.org", "EMORI_SMTP_PASS": SECRET_VALUE})

    reply = rig.client.post("/api/admin/results-email/test", headers=HEADER,
                            json={"to": "trainer@example.org"})

    assert reply.json() == {"sent": True, "code": "ok"}
    # Saved but not applied yet: the test checks what the restart will use. The sender
    # falls back to the e-mail-shaped user.
    assert _Smtp.sent == [("login", "robot@example.org", SECRET_VALUE),
                          ("send", "smtp.example.org", 2525, "robot@example.org",
                           "trainer@example.org")]
    assert SECRET_VALUE not in reply.text


def test_the_test_email_reports_a_missing_setting_by_code(rig, monkeypatch):
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **kw: pytest.fail("no connection"))

    unset = rig.client.post("/api/admin/results-email/test", headers=HEADER,
                            json={"to": "trainer@example.org"})
    rig.client.post("/api/admin/site", headers=HEADER,
                    json={"EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_USER": "key"})
    no_sender = rig.client.post("/api/admin/results-email/test", headers=HEADER,
                                json={"to": "trainer@example.org"})
    bad = rig.client.post("/api/admin/results-email/test", headers=HEADER,
                          json={"to": "not an address"})

    assert unset.json() == {"sent": False, "code": "smtp_host_unset"}
    assert no_sender.json() == {"sent": False, "code": "smtp_from_unset"}
    assert (bad.status_code, bad.json()) == (400, {"sent": False,
                                                   "code": "recipient_invalid"})


# --- review hardening: smuggled lines, the control origin, SMTP, throttles ------------


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x0b", "\x0c", "\x85", "\x1e"])
def test_a_value_cannot_carry_a_setting_past_its_checks(rig, separator):
    """Neither POKAMIND_BASE (whoami-checked) nor the admin PIN can ride inside an
    ordinary setting."""

    smuggled = (f"course{separator}POKAMIND_BASE=https://elsewhere.example"
                f"{separator}EMORI_ADMIN_PIN=000000")

    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"EMORI_COURSE_ID": smuggled})

    assert (reply.status_code, reply.json()) == (
        400, {"error": "invalid_value", "key": "EMORI_COURSE_ID", "code": "value_has_newline"})
    assert not (rig.root / site_settings.SETTINGS_FILE).exists()
    assert rig.whoami.asked == []


def test_an_origin_with_a_path_is_refused_before_anyone_is_asked(rig):
    reply = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"POKAMIND_BASE": "https://api.example.test/relay"})

    assert (reply.status_code, reply.json()["code"]) == (400, "value_not_origin")
    assert rig.whoami.asked == []


def _saved(rig) -> dict:
    return site_settings.stored_values(rig.root)


def test_a_new_mail_server_without_a_password_drops_the_saved_one(rig, monkeypatch):
    """The SMTP password is write-only: pointing the mail server elsewhere and pressing
    "Send test e-mail" must never log in to the new server with it."""

    import smtplib

    rig.client.post("/api/admin/site", headers=HEADER, json={
        "EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_USER": "robot@example.org",
        "EMORI_SMTP_PASS": SECRET_VALUE})
    assert _saved(rig)["EMORI_SMTP_PASS"] == SECRET_VALUE

    moved = rig.client.post("/api/admin/site", headers=HEADER,
                            json={"EMORI_SMTP_HOST": "smtp.elsewhere.example"})

    assert moved.status_code == 200
    assert moved.json()["cleared"] == ["EMORI_SMTP_PASS"]
    assert "EMORI_SMTP_PASS" not in _saved(rig)
    assert SECRET_VALUE not in moved.text
    _Smtp.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _Smtp)
    rig.client.post("/api/admin/results-email/test", headers=HEADER,
                    json={"to": "trainer@example.org"})
    assert all(SECRET_VALUE not in str(entry) for entry in _Smtp.sent)


def test_a_new_account_drops_it_too_but_the_same_server_or_a_new_password_does_not(rig):
    rig.client.post("/api/admin/site", headers=HEADER, json={
        "EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_USER": "robot@example.org",
        "EMORI_SMTP_PASS": SECRET_VALUE})

    same = rig.client.post("/api/admin/site", headers=HEADER,
                           json={"EMORI_SMTP_HOST": "smtp.example.org",
                                 "EMORI_SMTP_PORT": "2525"})
    assert "cleared" not in same.json() and _saved(rig)["EMORI_SMTP_PASS"] == SECRET_VALUE

    with_new = rig.client.post("/api/admin/site", headers=HEADER,
                               json={"EMORI_SMTP_HOST": "smtp.other.example",
                                     "EMORI_SMTP_PASS": "another-password"})
    assert "cleared" not in with_new.json()
    assert _saved(rig)["EMORI_SMTP_PASS"] == "another-password"

    account = rig.client.post("/api/admin/site", headers=HEADER,
                              json={"EMORI_SMTP_USER": "someone@example.org"})
    assert account.json()["cleared"] == ["EMORI_SMTP_PASS"]
    assert "EMORI_SMTP_PASS" not in _saved(rig)


def test_the_test_email_is_rate_limited(rig, monkeypatch):
    import smtplib

    _Smtp.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _Smtp)
    rig.client.post("/api/admin/site", headers=HEADER, json={
        "EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_USER": "robot@example.org"})

    answers = [rig.client.post("/api/admin/results-email/test", headers=HEADER,
                               json={"to": "trainer@example.org"})
               for _ in range(admin_api.RESULTS_TEST_MAX + 1)]

    assert [a.json()["code"] for a in answers[:-1]] == ["ok"] * admin_api.RESULTS_TEST_MAX
    assert (answers[-1].status_code, answers[-1].json()) == (
        429, {"sent": False, "code": "test_rate_limited"})
    assert len([e for e in _Smtp.sent if e[0] == "send"]) == admin_api.RESULTS_TEST_MAX


def test_sync_now_asks_pokamind_at_most_once_per_interval(rig, monkeypatch):
    from emori_adapter.hostapp import pokamind_scope

    first = rig.client.post("/api/admin/scope/sync", headers=HEADER).json()
    second = rig.client.post("/api/admin/scope/sync", headers=HEADER).json()

    assert first["ok"] is True
    assert (second["ok"], second["code"]) == (False, "sync_throttled")
    assert len(rig.whoami.asked) == 1
    # A Verify (an answer already in hand) is never throttled, and a sync after the
    # interval asks again.
    monkeypatch.setattr(pokamind_scope, "ADMIN_MIN_INTERVAL_S", 0.0)
    third = rig.client.post("/api/admin/scope/sync", headers=HEADER).json()
    assert third["ok"] is True and len(rig.whoami.asked) == 2


def test_a_language_set_on_this_robot_is_kept_when_the_record_drops_it(monkeypatch,
                                                                       tmp_path):
    """Explicit wins: EMORI_LANG=ca in .env stays the kiosk language and the page is not
    reloaded; only the OFFER follows the record. (Whether the derived EMORI_LANG change
    is a restart reason is site_settings.restart_reasons's call, made with the running
    process's own sources: pinned in test_site_settings.)"""

    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, languages=["ca", "en"], modules=BOTH_LANGUAGES)
    rig = build(monkeypatch, tmp_path, token=token, lang="ca", env={"EMORI_LANG": "ca"})
    assert rig.app.state.lang_state.current() == "ca"

    rig.whoami.overrides = {"languages": ["en"], "modules": BOTH_LANGUAGES}
    assert rig.client.post("/api/admin/scope/sync", headers=HEADER).json()["ok"] is True

    assert rig.app.state.lang_state.allowed == ("en",)
    assert rig.app.state.lang_state.current() == "ca"
    assert rig.client.get("/api/pending-cmd?verbs=reload").json()["cmd"] is None


def test_a_busy_robot_moves_language_only_at_the_next_restart(monkeypatch, tmp_path):
    """Never under a visitor: the kiosk keeps its language, the move is a pending
    restart (applied when the robot is idle)."""

    from tests.test_admin_api import _IdleRuntime

    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, languages=["ca", "en"], modules=BOTH_LANGUAGES)
    rig = build(monkeypatch, tmp_path, token=token, lang="en",
                runtime=_IdleRuntime(mode="running"))
    assert rig.client.post("/api/admin/lang", headers=HEADER,
                           json={"lang": "ca"}).status_code == 409   # busy: the setup
    rig.app.state.lang_state.set("ca")

    rig.whoami.overrides = {"languages": ["en"], "modules": BOTH_LANGUAGES}
    assert rig.client.post("/api/admin/scope/sync", headers=HEADER).json()["ok"] is True

    assert rig.app.state.lang_state.current() == "ca"
    assert rig.client.get("/api/pending-cmd?verbs=reload").json()["cmd"] is None
    assert rig.client.get("/api/admin/site", headers=HEADER).json()[
        "restart_reasons"] == ["scope_changed"]


def test_an_idle_robot_moves_through_the_whole_switch_sequence(monkeypatch, tmp_path):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    write_scope(tmp_path, token, languages=["ca", "en"], modules=BOTH_LANGUAGES)
    rig = build(monkeypatch, tmp_path, token=token, lang="en")
    rig.app.state.lang_state.set("ca")

    rig.whoami.overrides = {"languages": ["en"], "modules": BOTH_LANGUAGES}
    assert rig.client.post("/api/admin/scope/sync", headers=HEADER).json()["ok"] is True

    assert rig.app.state.lang_state.current() == "en"
    assert rig.app.state.driver.default_lang == "en"
    assert rig.client.get("/api/pending-cmd?verbs=reload").json()["cmd"] == "reload"
