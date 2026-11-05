"""The kiosk language is RUNTIME state, not a constant frozen at import.

A standalone robot boots to idle in Catalan and is switched to English by a hidden
physical gesture, with no shell, no keyboard and no restart. That makes the language
a small piece of persisted state: the switch must outlive the process (an appliance
reboots on power loss, and coming back up in the wrong language is indistinguishable
from a broken toggle) while the deployment's ``EMORI_LANG`` stays the value a robot
that has never been toggled starts from.
"""

from __future__ import annotations

import json

import pytest

from emori_adapter.hostapp.lang_state import (
    DEFAULT_STATE_FILE,
    LangState,
    allowed_languages,
    normalize_lang,
    other_lang,
    scope_languages,
)


def _state(tmp_path, **kwargs) -> LangState:
    kwargs.setdefault("path", str(tmp_path / "kiosk_lang.json"))
    return LangState(**kwargs)


# --- pure helpers -------------------------------------------------------------

def test_normalize_accepts_only_the_two_shipped_languages():
    assert normalize_lang(" CA ") == "ca"
    assert normalize_lang("en") == "en"
    assert normalize_lang("es") == ""        # no voice, no strings, no course
    assert normalize_lang(None) == ""
    assert normalize_lang(3) == ""


def test_other_lang_is_total_so_a_gesture_can_never_raise():
    assert other_lang("ca") == "en"
    assert other_lang("en") == "ca"
    # A robot configured with something we do not ship still has to toggle somewhere,
    # and Catalan is the fallback.
    assert other_lang("es") == "ca"
    assert other_lang(None) == "ca"


# --- initial value ------------------------------------------------------------

def test_initial_value_is_the_deployment_default_without_a_state_file(tmp_path):
    assert _state(tmp_path, default="ca").current() == "ca"


def test_a_deployment_language_we_do_not_ship_is_still_served_untouched(tmp_path):
    """``EMORI_LANG`` is the deployment's word, and normalising it here would
    silently re-language a robot that was never toggled at all."""

    assert _state(tmp_path, default="es").current() == "es"


def test_the_default_falls_back_to_EMORI_LANG_then_english(tmp_path, monkeypatch):
    monkeypatch.setenv("EMORI_LANG", "ca")
    assert _state(tmp_path).current() == "ca"
    monkeypatch.delenv("EMORI_LANG", raising=False)
    assert _state(tmp_path).current() == "en"


def test_the_state_file_path_comes_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "elsewhere" / "lang.json"
    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(path))
    state = LangState(default="ca")
    assert state.path == str(path)
    state.set("en")
    assert path.exists()


def test_the_default_state_file_sits_with_the_other_runtime_artifacts():
    assert DEFAULT_STATE_FILE.startswith("artifacts/")


# --- persistence --------------------------------------------------------------

def test_a_switch_survives_a_restart(tmp_path):
    path = str(tmp_path / "kiosk_lang.json")
    assert LangState(default="ca", path=path).set("en") == "en"

    # A brand-new process, same deployment default: the file wins.
    assert LangState(default="ca", path=path).current() == "en"


def test_the_file_holds_a_readable_language(tmp_path):
    path = tmp_path / "kiosk_lang.json"
    _state(tmp_path).set(" EN ")
    assert json.loads(path.read_text(encoding="utf-8")) == {"lang": "en"}


def test_an_unreadable_state_file_falls_back_to_the_deployment_default(tmp_path):
    """A half-written file (power cut mid-write) must not brick the kiosk."""

    path = tmp_path / "kiosk_lang.json"
    path.write_text('{"lang": "e', encoding="utf-8")
    assert LangState(default="ca", path=str(path)).current() == "ca"


def test_a_state_file_holding_an_unsupported_language_is_ignored(tmp_path):
    path = tmp_path / "kiosk_lang.json"
    path.write_text(json.dumps({"lang": "de"}), encoding="utf-8")
    assert LangState(default="ca", path=str(path)).current() == "ca"


def test_an_unsupported_language_is_refused_and_changes_nothing(tmp_path):
    state = _state(tmp_path, default="ca")
    with pytest.raises(ValueError):
        state.set("de")
    assert state.current() == "ca"


def test_a_write_failure_still_changes_the_LIVE_language(tmp_path):
    """Persistence is best-effort; the gesture must take effect regardless.

    A read-only or full SD card is a reason to come back up in the old language
    after a reboot -- never a reason for the toggle the human just performed to
    do nothing at all.
    """

    directory = tmp_path / "occupied"
    directory.mkdir()                       # the state "file" path is a directory
    state = LangState(default="ca", path=str(directory))
    assert state.set("en") == "en"
    assert state.current() == "en"


def test_a_state_file_that_outranks_EMORI_LANG_says_so_out_loud(tmp_path, caplog):
    """An appliance whose language was toggled earlier comes up in the "wrong" language while
    the only visible configuration says otherwise. journalctl is the only window."""

    path = tmp_path / "kiosk_lang.json"
    path.write_text(json.dumps({"lang": "en"}), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert LangState(default="ca", path=str(path)).current() == "en"
    assert str(path) in caplog.text and "EMORI_LANG=ca" in caplog.text


def test_a_state_file_that_agrees_with_the_default_is_quiet(tmp_path, caplog):
    path = tmp_path / "kiosk_lang.json"
    path.write_text(json.dumps({"lang": "ca"}), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert LangState(default="ca", path=str(path)).current() == "ca"
    # THIS module's channel only. caplog is process-wide, and other tests leave real
    # RobotPlayer threads retrying `daemon status check failed` against a daemon no dev
    # box runs -- one landing inside this block would fail an assertion about a
    # message it has nothing to do with.
    assert [r for r in caplog.records if r.name == "emori.hostapp.lang"] == []


def test_reload_picks_up_an_externally_edited_file(tmp_path):
    path = tmp_path / "kiosk_lang.json"
    state = LangState(default="ca", path=str(path))
    assert state.current() == "ca"
    path.write_text(json.dumps({"lang": "en"}), encoding="utf-8")

    # Reading is cached (it sits on the page-render path), so an outside edit
    # needs an explicit reload -- which is also what makes the cache testable.
    assert state.current() == "ca"
    assert state.reload() == "en"
    assert state.current() == "en"


# --- the languages on offer: this robot's Pokamind scope --------------------------------

def test_by_default_every_shipped_language_is_on_offer(tmp_path):
    state = _state(tmp_path, default="ca")

    assert state.allowed == ("ca", "en")
    assert state.set("en") == "en" and state.set("ca") == "ca"


def test_an_empty_scope_allows_ca_and_en(tmp_path):
    assert scope_languages(None) == ("ca", "en")
    assert scope_languages({"whoami": {"languages": []}}) == ("ca", "en")
    assert _state(tmp_path, allowed=()).allowed == ("ca", "en")
    assert _state(tmp_path, allowed=["de", "", None]).allowed == ("ca", "en")


def test_set_refuses_a_language_outside_the_scope(tmp_path):
    state = _state(tmp_path, default="ca", allowed=("ca",))

    with pytest.raises(ValueError) as refused:
        state.set("en")

    assert str(refused.value) == "unsupported_lang"
    assert state.current() == "ca"
    assert not (tmp_path / "kiosk_lang.json").exists()
    assert state.is_allowed("CA") and not state.is_allowed("en")


def test_an_unshipped_language_is_refused_with_the_same_code(tmp_path):
    with pytest.raises(ValueError, match="unsupported_lang"):
        _state(tmp_path).set("de")


def test_a_persisted_language_outside_the_scope_is_ignored_at_start(tmp_path, caplog):
    path = tmp_path / "kiosk_lang.json"
    path.write_text(json.dumps({"lang": "en"}), encoding="utf-8")

    with caplog.at_level("WARNING", logger="emori.hostapp.lang"):
        state = LangState(default="ca", path=str(path), allowed=("ca",))
        assert state.current() == "ca"

    assert "scope does not offer" in caplog.text and str(path) in caplog.text
    # The file is left alone: a scope that lists the language again brings it back.
    assert LangState(default="ca", path=str(path)).current() == "en"


def test_the_offer_changes_live_but_never_the_language_in_force(tmp_path):
    """A scope sync narrows the OFFER while the kiosk is in a language it drops. Moving
    the kiosk is a sequence (driver stamp, page reload, idle gate) that belongs to the
    hostapp (server._follow_scope_languages), and an explicit EMORI_LANG is never
    overridden from here."""

    path = tmp_path / "kiosk_lang.json"
    state = LangState(default="ca", path=str(path))
    assert state.set("en") == "en"

    assert state.set_allowed(["ca"]) == ("ca",)

    # The language in force is unchanged; the switch back to it is refused...
    assert state.current() == "en" and state.allowed == ("ca",)
    with pytest.raises(ValueError, match="unsupported_lang"):
        state.set("en")
    # ...the fallback the hostapp moves to is the deployment default...
    assert state.fallback() == "ca"
    # ...and the state file is not rewritten: a later offer listing en brings it back.
    assert json.loads(path.read_text(encoding="utf-8")) == {"lang": "en"}
    assert state.set_allowed(None) == ("ca", "en")
    assert state.set("en") == "en"


def test_the_fallback_of_an_offer_without_the_default_is_its_first_language(tmp_path):
    state = _state(tmp_path, default="ca")

    assert state.set_allowed(["en", "de"]) == ("en",)
    assert state.current() == "ca"
    assert state.fallback() == "en"


def test_an_offer_that_still_holds_the_live_language_keeps_it(tmp_path):
    state = _state(tmp_path, default="ca")
    state.set("en")

    assert state.set_allowed(["en", "ca"]) == ("en", "ca")
    assert state.current() == "en"


def test_the_scope_order_is_the_records_preference_order():
    snapshot = {"whoami": {"languages": ["en-GB", "ca_ES", "es", "en"]}}

    assert scope_languages(snapshot) == ("en", "ca")
    assert LangState(default="ca", path="unused.json",
                     allowed=scope_languages(snapshot)).allowed == ("en", "ca")


def test_a_scope_listing_no_shipped_language_restricts_nothing(caplog):
    with caplog.at_level("WARNING", logger="emori.hostapp.lang"):
        assert scope_languages({"whoami": {"languages": ["es", "fr"]}}) == ("ca", "en")
    assert "lists no kiosk language this robot ships" in caplog.text


@pytest.mark.parametrize("snapshot", [{}, {"whoami": None}, {"whoami": {"languages": "ca"}},
                                      {"whoami": {"languages": [3, None]}}, "junk"])
def test_a_malformed_snapshot_restricts_nothing(snapshot):
    assert scope_languages(snapshot) == ("ca", "en")


_GOLDEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def _with_scope(monkeypatch, tmp_path, languages, *, robot_id="test-robot-01"):
    from emori_adapter import robot_token, site_settings

    monkeypatch.setenv("POKAMIND_ROBOT_TOKEN", _GOLDEN)
    robot_token.reset_configured()
    token = robot_token.parse_token(_GOLDEN)
    whoami = {"robot_id": robot_id, "languages": languages, "features": {}}
    snapshot = site_settings.merge_scope_snapshot(
        None, whoami, contract=1, key_fingerprint=token.fingerprint, key_version=1,
        now=1.0)
    site_settings.write_scope_snapshot(snapshot, root=tmp_path)


def test_allowed_languages_reads_this_robots_scope(monkeypatch, tmp_path):
    monkeypatch.delenv("EMORI_SITE_AUTO", raising=False)
    _with_scope(monkeypatch, tmp_path, ["en"])

    assert allowed_languages(root=tmp_path) == ("en",)


def test_allowed_languages_ignores_another_robots_scope(monkeypatch, tmp_path):
    monkeypatch.delenv("EMORI_SITE_AUTO", raising=False)
    _with_scope(monkeypatch, tmp_path, ["en"], robot_id="another-robot")

    assert allowed_languages(root=tmp_path) == ("ca", "en")


def test_allowed_languages_is_everything_with_derivation_off(monkeypatch, tmp_path):
    _with_scope(monkeypatch, tmp_path, ["en"])
    monkeypatch.setenv("EMORI_SITE_AUTO", "0")

    assert allowed_languages(root=tmp_path) == ("ca", "en")


def test_allowed_languages_without_a_token_or_a_scope_is_everything(tmp_path):
    # conftest: no token in any test unless it installs one.
    assert allowed_languages(root=tmp_path) == ("ca", "en")
    assert allowed_languages(snapshot={"whoami": {"languages": ["ca"]}}) == ("ca",)
