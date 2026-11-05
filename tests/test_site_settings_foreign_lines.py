"""What the settings layer does with what it does not own.

A settings file or snapshot may hold what an optional add-on wrote while that add-on is not
loaded now: never reported, never warned about, and still there when the add-on is back.
"""

from __future__ import annotations

import json
import logging

import pytest

from emori_adapter import site_settings as ss

_FOREIGN = 'EMORI_EXAMPLE_ADDON_VALUE="kept  verbatim" '
_SECRET = "EMORI_EXAMPLE_ADDON_KEYS=do-not-echo"


@pytest.fixture
def root(tmp_path):
    for relative, line in ((ss.SETTINGS_FILE, _FOREIGN), (ss.SITE_SECRETS_FILE, _SECRET)):
        path = ss.site_path(relative, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(line + "\nEMORI_CUE_AMP=1.2\n# a comment\nnot a setting\n"
                        if relative == ss.SETTINGS_FILE else line + "\n", encoding="utf-8")
    return tmp_path


def _text(root, relative) -> str:
    return ss.site_path(relative, root).read_text(encoding="utf-8")


def test_a_save_keeps_a_line_no_setting_owns_verbatim_in_its_own_file(root):
    ss.save_settings({"EMORI_CUE_Z_MM": "12"}, root=root)
    ss.save_settings({"EMORI_SMTP_PASS": "s3cret-value"}, root=root)

    assert _text(root, ss.SETTINGS_FILE).splitlines() == [
        _FOREIGN, "EMORI_CUE_AMP=1.2", "EMORI_CUE_Z_MM=12"]
    secrets = _text(root, ss.SITE_SECRETS_FILE).splitlines()
    assert secrets == [_SECRET, "EMORI_SMTP_PASS=s3cret-value"]
    assert _SECRET not in _text(root, ss.SETTINGS_FILE)


def test_removing_the_last_setting_leaves_the_kept_line(root):
    ss.save_settings({"EMORI_CUE_AMP": None}, root=root)

    assert _text(root, ss.SETTINGS_FILE).splitlines() == [_FOREIGN]


def test_no_reader_surfaces_such_a_line_and_nothing_warns(root, caplog):
    env: dict = {}

    with caplog.at_level(logging.DEBUG, logger="emori.site_settings"):
        stored = ss.stored_values(root)
        problems = ss.check_stored_settings(root)
        sources = ss.apply(env, root=root)

    for seen in (stored, problems, sources, env):
        assert "EMORI_EXAMPLE_ADDON_VALUE" not in seen
        assert "EMORI_EXAMPLE_ADDON_KEYS" not in seen
    assert "do-not-echo" not in caplog.text
    unknown = [r for r in caplog.records if "not a site setting" in r.getMessage()]
    assert unknown and {r.levelno for r in unknown} == {logging.DEBUG}


def test_the_sources_sidecar_keeps_an_entry_no_setting_owns(tmp_path):
    path = ss.site_path(ss.SOURCES_SIDECAR, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"EMORI_EXAMPLE_ADDON_VALUE": "calibration"}), encoding="utf-8")

    ss.write_sources_sidecar({"EMORI_CUE_AMP": "calibration"}, tmp_path)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "EMORI_CUE_AMP": "calibration", "EMORI_EXAMPLE_ADDON_VALUE": "calibration"}
    assert ss.read_sources_sidecar(tmp_path) == {"EMORI_CUE_AMP": "calibration"}


def _merge(previous, *, fingerprint="sha256:same"):
    return ss.merge_scope_snapshot(previous, {"robot_id": "test-robot-01", "features": {}},
                                   contract=1, key_fingerprint=fingerprint, key_version=1,
                                   now=1.0)


_PREVIOUS = {"robot_id": "test-robot-01", "key_version": 1, "key_fingerprint": "sha256:same",
             "addon_provisioned": True, "example_flag": True}


def test_with_no_add_on_a_sync_carries_what_the_same_credential_held():
    merged = _merge(_PREVIOUS)

    assert merged["addon_provisioned"] is True and merged["example_flag"] is True
    assert ss.derive_from_scope(merged) == ss.derive_from_scope({**merged, "example_flag": False})


def test_a_new_credential_starts_over():
    merged = _merge(_PREVIOUS, fingerprint="sha256:other")

    assert merged["addon_provisioned"] is False and "example_flag" not in merged


def test_an_add_on_answer_replaces_what_was_carried(monkeypatch):
    monkeypatch.setattr(ss, "contributed", lambda hook, *a, **k: False)

    merged = _merge(_PREVIOUS)

    assert merged["addon_provisioned"] is False and "example_flag" not in merged


def _merged_hooks(monkeypatch, tmp_path, dotenv_text):
    calls = []
    dotenv = tmp_path / ".env"
    dotenv.write_text(dotenv_text, encoding="utf-8")
    monkeypatch.setattr(ss, "find_install_dotenv", lambda start: str(dotenv))
    monkeypatch.setattr(ss, "contributed", lambda hook, *a, **k: calls.append(hook))
    ss._merge_extension_registry()
    return calls


def test_a_switch_off_set_only_in_the_dotenv_keeps_the_registry_public(monkeypatch, tmp_path):
    monkeypatch.delenv("EMORI_ADDONS", raising=False)

    assert _merged_hooks(monkeypatch, tmp_path, "EMORI_ADDONS=off\n") == []


def test_the_process_environment_wins_over_the_dotenv_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_ADDONS", "on")

    assert _merged_hooks(monkeypatch, tmp_path, "EMORI_ADDONS=off\n") == ["settings_registry"]


@pytest.mark.parametrize("entry", [
    (1.0,), (1.0, 2.0, 3.0), "ab", 5, None, ("60", 5.0), (float("nan"), 5.0),
    (float("inf"), 5.0), (True, 5.0),
])
def test_a_malformed_added_deadline_takes_no_part_and_breaks_no_save(monkeypatch, entry):
    key = "EMORI_EXAMPLE_LIMIT_S"
    monkeypatch.setitem(ss.REGISTRY, key, ss.make_spec(key, ss.number_check(30.0, 1800.0, off=True)))
    monkeypatch.setattr(ss, "contributed",
                        lambda hook, *a, **k: {key: entry} if hook == "session_deadlines" else None)

    assert key not in ss.deadline_keys()
    assert ss.deadline_order_problems({key: "31"}) == []


def test_an_added_deadline_stays_behind_the_session_start_ones(monkeypatch):
    key = "EMORI_EXAMPLE_LIMIT_S"
    monkeypatch.setitem(ss.REGISTRY, key, ss.make_spec(key, ss.number_check(30.0, 1800.0, off=True)))
    monkeypatch.setattr(ss, "contributed",
                        lambda hook, *a, **k: {key: (120.0, 30.0)}
                        if hook == "session_deadlines" else None)

    assert key in ss.deadline_keys()
    start = {"EMORI_SESSION_START_S": "100"}
    assert ss.deadline_order_problems({**start, key: "130"}) == []
    assert ss.deadline_order_problems({**start, key: "0"}) == []
    assert ss.deadline_order_problems({**start, key: "129"}) == [
        (key, "EMORI_SESSION_START_S", "EMORI_SESSION_START_MS")]


def test_with_no_add_on_only_the_two_start_deadlines_are_ordered():
    assert ss.deadline_keys() == {"EMORI_SESSION_START_MS", "EMORI_SESSION_START_S"}
    assert ss.deadline_order_problems({"EMORI_SESSION_START_S": "600"}) == []
