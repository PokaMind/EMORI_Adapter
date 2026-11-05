"""This robot's Pokamind scope: a verified whoami, recorded as the last-good snapshot.

Pinned here, with no network (an injected fetch) and no hostapp:

* only a whoami about THIS robot, active, holding THIS key is recorded -- a revoked
  robot, another robot or another key leaves the disk untouched;
* a failed or offline sync keeps the last-good snapshot;
* ``on_change`` hears every sync, with the changed key NAMES and the restart reasons;
* the menus are rebuilt from the snapshot (injected builder) and their numbers carried;
* the background loop's timing (10 s, then 6 h; 10 and 30 min retries) is bounded;
* nothing of the record's content reaches a log line.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from emori_adapter import robot_token, site_settings
from emori_adapter.hostapp import pokamind_scope
from emori_adapter.hostapp.pokamind_scope import ScopeSync, SyncOutcome, next_delay
from emori_adapter.hostapp.pokamind_whoami import WhoamiResult, parse_whoami
from tests.test_pokamind_whoami import GOLDEN_TOKEN, body, body_v2

TOKEN = robot_token.parse_token(GOLDEN_TOKEN)
FINGERPRINT = "Vkdap1RjR0wChd9d"
# Built, not written out: the golden vector is the one full token the repository holds.
OTHER_TOKEN = robot_token.parse_token("pkr1.test-robot-01." + base64.urlsafe_b64encode(
    bytes(range(32))[::-1]).rstrip(b"=").decode("ascii"))
ROBOT_02 = robot_token.RobotToken("test-robot-02", bytes(range(32)))


@pytest.fixture(autouse=True)
def _auto_on_and_no_local_values(monkeypatch):
    """Derivation on, and no setting of this test process counted as set locally."""

    monkeypatch.delenv("EMORI_SITE_AUTO", raising=False)
    monkeypatch.setattr(site_settings, "effective_sources", lambda environ=None: {})


def _who(raw=None, **overrides):
    value = copy.deepcopy(raw if raw is not None else body())
    value.update(overrides)
    return parse_whoami(value, robot_id="test-robot-01", fingerprint=FINGERPRINT)


def _ok(raw=None, **overrides) -> WhoamiResult:
    return WhoamiResult(ok=True, code="ok", http_status=200, whoami=_who(raw, **overrides),
                        checked_at=1.0)


class _Fetch:
    """Answers queued in order; records every call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, base_url, token):
        self.calls.append((base_url, token))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Menus:
    """A stand-in for menu_builder.rebuild_auto: writes one file per language listed."""

    def __init__(self):
        self.snapshots = []

    def __call__(self, snapshot, out_dir):
        self.snapshots.append(copy.deepcopy(snapshot))
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        order = {}
        for lang in ("ca", "en"):
            ids = [m["module_id"] for m in snapshot["whoami"]["modules"]
                   if m["language"] == lang and m["available"] and m["problem"] is None]
            path = out_dir / f"voice_topics.{lang}.yaml"
            if ids:
                path.write_text("ids: %s\n" % ",".join(ids), encoding="utf-8")
                order[lang] = ids
            elif path.exists():
                path.unlink()
        return order


def _sync(tmp_path, fetch=None, *, token=TOKEN, clock=lambda: 1_785_000_000.0, **kwargs):
    seen = []
    sync = ScopeSync("https://api.example.test", token, fetch=fetch or _Fetch(_ok()),
                     on_change=seen.append, clock=clock, root=tmp_path,
                     rebuild=kwargs.pop("rebuild", _Menus()), **kwargs)
    return sync, seen


def _snapshot(tmp_path) -> dict:
    return json.loads((tmp_path / site_settings.SCOPE_FILE).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- recording
def test_a_verified_answer_is_recorded_with_its_menus(tmp_path):
    menus = _Menus()
    sync, _ = _sync(tmp_path, rebuild=menus)

    assert sync.record_verified(_who(body_v2()), TOKEN) is True

    snap = _snapshot(tmp_path)
    assert (snap["schema"], snap["robot_id"], snap["key_fingerprint"]) == (
        site_settings.SNAPSHOT_SCHEMA, "test-robot-01", FINGERPRINT)
    assert (snap["contract"], snap["key_version"], snap["fetched_at"]) == (
        2, 1, 1_785_000_000.0)
    assert snap["whoami"] == _who(body_v2()).to_dict()
    assert snap["digest"] == site_settings.snapshot_digest(snap["whoami"])
    assert snap["menu_order"] == {"ca": ["example-ca-module-1"]}
    assert (tmp_path / site_settings.AUTO_MENU_DIR / "voice_topics.ca.yaml").exists()
    # The builder saw the snapshot it builds from, at the auto-menu path.
    assert menus.snapshots[0]["robot_id"] == "test-robot-01"
    assert sync.menus_path() == tmp_path / site_settings.AUTO_MENU_DIR
    # What site_settings derives from it is what the record says.
    assert site_settings.load_scope_snapshot("test-robot-01", FINGERPRINT,
                                             root=tmp_path) == snap
    if os.name == "posix":
        mode = stat.S_IMODE((tmp_path / site_settings.SCOPE_FILE).stat().st_mode)
        assert mode == 0o600


@pytest.mark.parametrize("who, token, code", [
    (lambda: _who(status="revoked"), TOKEN, "robot_not_active"),
    (lambda: _who(), ROBOT_02, "whoami_robot_id_mismatch"),
    (lambda: _who(), OTHER_TOKEN, "whoami_key_mismatch"),
    (lambda: "not a whoami", TOKEN, "invalid_whoami"),
], ids=["revoked", "another-robot", "another-key", "not-a-whoami"])
def test_only_this_active_robot_with_this_key_is_recorded(tmp_path, who, token, code):
    sync, _ = _sync(tmp_path)

    assert sync.record(who(), token) == code
    assert sync.record_verified(who(), token) is False
    assert not (tmp_path / "configs").exists()


def test_no_token_records_and_fetches_nothing(tmp_path):
    fetch = _Fetch(_ok())
    sync, seen = _sync(tmp_path, fetch, token=None)

    assert sync.record(_who()) == "no_robot_token"
    outcome = sync.sync_now("admin_sync")

    assert (outcome.ok, outcome.code) == (False, "no_robot_token")
    assert fetch.calls == [] and seen == [outcome]
    assert sync.start_background() is False and not sync.running
    assert not (tmp_path / "configs").exists()


def test_a_registry_robot_without_a_fingerprint_is_recorded_with_the_tokens(tmp_path):
    raw = body(credential={"kind": "registry", "key_fingerprint": None, "key_version": None})
    sync, _ = _sync(tmp_path)

    assert sync.record_verified(_who(raw), TOKEN)
    assert _snapshot(tmp_path)["key_fingerprint"] == FINGERPRINT


# --------------------------------------------------------------------------- syncing
def test_a_failed_sync_keeps_the_last_good_snapshot(tmp_path):
    fetch = _Fetch(_ok(course_id="course-a"), WhoamiResult(code="whoami_unreachable"),
                   WhoamiResult(code="robot_revoked", http_status=401),
                   RuntimeError("boom"))
    sync, seen = _sync(tmp_path, fetch)
    assert sync.sync_now("boot").ok
    before = (tmp_path / site_settings.SCOPE_FILE).read_bytes()

    codes = [sync.sync_now("periodic").code for _ in range(3)]

    assert codes == ["whoami_unreachable", "robot_revoked", "whoami_unreachable"]
    assert (tmp_path / site_settings.SCOPE_FILE).read_bytes() == before
    assert [o.ok for o in seen] == [True, False, False, False]
    assert sync.last_outcome() == seen[-1]


def test_a_revoked_answer_never_replaces_the_snapshot(tmp_path):
    fetch = _Fetch(_ok(course_id="course-a"), _ok(course_id="course-b", status="revoked"))
    sync, _ = _sync(tmp_path, fetch)
    sync.sync_now("boot")

    outcome = sync.sync_now("periodic")

    assert (outcome.ok, outcome.code) == (False, "robot_not_active")
    assert _snapshot(tmp_path)["whoami"]["course_id"] == "course-a"


def test_a_write_failure_is_reported_and_the_old_snapshot_stays(tmp_path, monkeypatch):
    sync, _ = _sync(tmp_path, _Fetch(_ok(course_id="course-a")))
    sync.sync_now("boot")
    before = (tmp_path / site_settings.SCOPE_FILE).read_bytes()

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(site_settings, "write_scope_snapshot", refuse)
    sync2, seen = _sync(tmp_path, _Fetch(_ok(course_id="course-b")))
    outcome = sync2.sync_now("periodic")

    assert (outcome.ok, outcome.code) == (False, "scope_write_failed")
    assert seen == [outcome]
    assert (tmp_path / site_settings.SCOPE_FILE).read_bytes() == before


def test_a_key_set_locally_never_asks_for_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(site_settings, "effective_sources", lambda environ=None: {
        key: "env" for key in site_settings.DERIVED_KEYS})
    sync, _ = _sync(tmp_path, _Fetch(_ok(body_v2(course_id="course-a"))))

    outcome = sync.sync_now("boot")

    assert outcome.ok and "EMORI_COURSE_ID" in outcome.changed_keys
    assert outcome.restart_reasons == ()


def test_derivation_switched_off_never_asks_for_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("EMORI_SITE_AUTO", "0")
    sync, _ = _sync(tmp_path, _Fetch(_ok(body_v2(course_id="course-a"))))

    outcome = sync.sync_now("boot")

    assert outcome.ok and outcome.restart_reasons == ()
    assert (tmp_path / site_settings.SCOPE_FILE).exists()  # still the last-good record


def test_on_change_hears_the_changed_key_names_and_the_restart_reasons(tmp_path,
                                                                     monkeypatch):
    monkeypatch.setattr(pokamind_scope, "ADMIN_MIN_INTERVAL_S", 0.0)
    first = _ok(body_v2(course_id="course-a", display_name="Reception"))
    fetch = _Fetch(first, first, _ok(body_v2(course_id="course-b")))
    sync, seen = _sync(tmp_path, fetch)

    one = sync.sync_now("boot")
    two = sync.sync_now("periodic")
    three = sync.sync_now("admin_sync")

    assert seen == [one, two, three]
    assert one.ok and set(one.changed_keys) == {
        "EMORI_COURSE_ID", "EMORI_PATHWAYS", "EMORI_ROBOT_NAME", "EMORI_LANG"}
    assert one.restart_reasons == ("scope_changed",)
    assert one.menus_changed is True and one.reason == "boot"
    # The same record again changes nothing.
    assert (two.changed_keys, two.restart_reasons, two.menus_changed) == ((), (), False)
    # A new course and a new name (display_name back to the v2 default): named, never shown.
    assert set(three.changed_keys) == {"EMORI_COURSE_ID", "EMORI_ROBOT_NAME"}
    assert three.restart_reasons == ("scope_changed",)
    assert three.to_dict() == {
        "ok": True, "code": "ok", "reason": "admin_sync",
        "changed_keys": list(three.changed_keys), "restart_reasons": ["scope_changed"],
        "menus_changed": False, "checked_at": 1_785_000_000.0}


def test_a_snapshot_of_another_token_is_no_previous_record(tmp_path):
    other = ScopeSync("https://api.example.test", OTHER_TOKEN, root=tmp_path,
                      rebuild=_Menus(), clock=lambda: 1.0)
    raw = body(credential={"kind": "fleet", "key_fingerprint": OTHER_TOKEN.fingerprint,
                           "key_version": 1})
    assert other.record_verified(
        parse_whoami(raw, robot_id="test-robot-01", fingerprint=OTHER_TOKEN.fingerprint),
        OTHER_TOKEN)

    sync, _ = _sync(tmp_path, _Fetch(_ok()))
    outcome = sync.sync_now("boot")

    assert outcome.ok and _snapshot(tmp_path)["key_fingerprint"] == FINGERPRINT


def test_a_flag_only_an_add_on_reads_changes_no_restart_without_one(tmp_path):
    on = {"feature_a": True, "feature_b": True, "feature_c": True}
    sync, _ = _sync(tmp_path, _Fetch(_ok(features=on)))

    outcome = sync.sync_now("boot")

    assert outcome.ok and outcome.restart_reasons == ("scope_changed",)
    assert not _snapshot(tmp_path).get("addon_provisioned")
    assert set(site_settings.derive_from_scope(_snapshot(tmp_path))) <= set(
        site_settings.DERIVED_KEYS)


def test_sync_now_can_record_an_answer_it_already_has(tmp_path):
    fetch = _Fetch(WhoamiResult(code="whoami_unreachable"))
    sync, seen = _sync(tmp_path, fetch)

    outcome = sync.sync_now("admin_verify", result=_ok(course_id="course-v"))

    assert outcome.ok and fetch.calls == []
    assert _snapshot(tmp_path)["whoami"]["course_id"] == "course-v"
    assert seen == [outcome]


def test_an_odd_reason_is_reported_as_manual(tmp_path):
    sync, _ = _sync(tmp_path)

    assert sync.sync_now("Not A Slug!").reason == "manual"


def test_a_listener_that_raises_never_breaks_the_sync(tmp_path):
    def boom(outcome):
        raise RuntimeError("listener")

    sync = ScopeSync("https://api.example.test", TOKEN, fetch=_Fetch(_ok()),
                     on_change=boom, root=tmp_path, rebuild=_Menus())

    assert sync.sync_now("boot").ok


# --------------------------------------------------------------------------- menus
def test_the_menu_numbers_are_carried_and_merged_per_language(tmp_path):
    seen_orders = []

    def builder(snapshot, out_dir):
        seen_orders.append(copy.deepcopy(snapshot["menu_order"]))
        return {"en": ["en-1"]} if len(seen_orders) > 1 else {"ca": ["ca-2", "ca-1"]}

    sync, _ = _sync(tmp_path, _Fetch(_ok()), rebuild=builder)
    sync.sync_now("boot")
    sync.sync_now("periodic")

    # The builder is handed the numbers it gave last time ...
    assert seen_orders == [{}, {"ca": ["ca-2", "ca-1"]}]
    # ... and a language it did not answer for keeps them.
    assert _snapshot(tmp_path)["menu_order"] == {"ca": ["ca-2", "ca-1"], "en": ["en-1"]}


def test_a_menu_builder_failure_still_records_the_scope(tmp_path, caplog):
    def broken(snapshot, out_dir):
        raise ValueError("bad menu")

    sync, _ = _sync(tmp_path, _Fetch(_ok()), rebuild=broken)
    with caplog.at_level(logging.WARNING, logger="emori.hostapp.pokamind_scope"):
        outcome = sync.sync_now("boot")

    assert outcome.ok and outcome.menus_changed is False
    assert _snapshot(tmp_path)["menu_order"] == {}
    assert "menu_rebuild_failed" in caplog.text


def test_a_menu_removed_by_the_builder_counts_as_a_change(tmp_path):
    with_en = body()
    with_en["modules"][0]["language"] = "en"
    fetch = _Fetch(_ok(), _ok(with_en))
    sync, _ = _sync(tmp_path, fetch)

    sync.sync_now("boot")
    outcome = sync.sync_now("periodic")

    auto = tmp_path / site_settings.AUTO_MENU_DIR
    assert outcome.menus_changed is True
    assert sorted(p.name for p in auto.iterdir()) == ["voice_topics.en.yaml"]


def test_without_the_menu_builder_the_scope_is_still_recorded(tmp_path, monkeypatch, caplog):
    import builtins

    real_import = builtins.__import__

    def no_builder(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "emori_adapter.hostapp" and fromlist and "menu_builder" in fromlist:
            raise ImportError("menu_builder")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_builder)
    sync = ScopeSync("https://api.example.test", TOKEN, fetch=_Fetch(_ok()), root=tmp_path)
    with caplog.at_level(logging.WARNING, logger="emori.hostapp.pokamind_scope"):
        assert sync.sync_now("boot").ok
    assert "menu_builder_unavailable" in caplog.text


# --------------------------------------------------------------------------- background
@pytest.mark.parametrize("ok, attempt, expected", [
    (True, 0, (21600.0, 0)),
    (True, 2, (21600.0, 0)),
    (False, 0, (600.0, 1)),
    (False, 1, (1800.0, 2)),
    (False, 2, (21600.0, 0)),     # the ladder is used up: back to the 6-hour rhythm
])
def test_the_retry_ladder(ok, attempt, expected):
    assert next_delay(ok, attempt) == expected


def test_the_background_loop_is_bounded(tmp_path):
    clock = {"now": 1_785_000_000.0}
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock["now"] += seconds
        return len(waits) > 9          # stop after nine syncs

    down = WhoamiResult(code="whoami_unreachable")
    fetch = _Fetch(down, down, down, down, _ok(), down, _ok(), _ok(), down, _ok())
    sync, seen = _sync(tmp_path, fetch, clock=lambda: clock["now"], wait=wait)

    assert sync.start_background() is True
    sync._thread.join(5)

    assert not sync.running
    assert waits == [10.0, 600.0, 1800.0, 21600.0, 600.0, 21600.0, 600.0, 21600.0,
                     21600.0, 600.0]
    assert [o.reason for o in seen] == ["boot", "retry", "retry", "periodic", "retry",
                                        "periodic", "retry", "periodic", "periodic"]
    assert len(fetch.calls) == 9
    # Each outcome is stamped with the (fake) time it ran at.
    assert seen[1].checked_at == 1_785_000_000.0 + 10 + 600


def test_stop_ends_the_background_thread(tmp_path):
    sync, seen = _sync(tmp_path, _Fetch(_ok()))

    assert sync.start_background(initial_delay_s=30.0) is True
    assert sync.start_background() is False        # already running
    sync.stop(timeout=5)

    assert not sync.running and seen == []


def test_the_default_timing_is_the_documented_one():
    assert pokamind_scope.INITIAL_DELAY_S == 10.0
    assert pokamind_scope.INTERVAL_S == 6 * 3600.0
    assert pokamind_scope.RETRY_S == (600.0, 1800.0)


# --------------------------------------------------------------------------- privacy
def test_no_value_of_the_record_reaches_the_log(tmp_path, caplog):
    raw = body_v2(course_id="secret-course", display_name="Secret Reception",
                  pathways=["Secret pathway"], org="secret-org",
                  app_origin="https://secret.example.test")
    raw["modules"][0]["menu_name"] = "Secret menu"
    raw["modules"][1]["menu_keywords"] = ["bad,keyword"]
    fetch = _Fetch(_ok(raw), WhoamiResult(code="whoami_unreachable"))
    sync, _ = _sync(tmp_path, fetch)

    with caplog.at_level(logging.DEBUG):
        sync.sync_now("boot")
        sync.sync_now("periodic")
        sync.record(_who(raw, status="revoked"), TOKEN)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "pokamind scope" in text and "EMORI_COURSE_ID" in text  # names are fine
    assert "modules[1].menu_keywords" in text                       # ... and so are key names
    for value in ("secret", "Secret", "example-ca-module", "test-robot-01", "bad,keyword",
                  FINGERPRINT, GOLDEN_TOKEN.rsplit(".", 1)[1]):
        assert value not in text, value


def test_the_outcome_is_json_ready():
    outcome = SyncOutcome(False, "whoami_timeout", "retry", checked_at=2.0)

    assert json.loads(json.dumps(outcome.to_dict())) == {
        "ok": False, "code": "whoami_timeout", "reason": "retry", "changed_keys": [],
        "restart_reasons": [], "menus_changed": False, "checked_at": 2.0}
