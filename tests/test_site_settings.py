"""The robot-local settings layer (``emori_adapter.site_settings``).

Precedence is admin-saved > explicit .env > Pokamind-derived > hardware profile > code
default; an empty value is unset; ``EMORI_SITE_AUTO=0`` switches the Pokamind layer off;
the hardware layer and the shipped defaults exist only on an installed robot
(``EMORI_HARDWARE_PROFILE``). Every test runs :func:`site_settings.apply` on a plain dict
in a scratch install root, so the real process environment is never touched.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from emori_adapter import robot_token, site_settings as ss

_REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- helpers
@pytest.fixture
def token():
    return robot_token.generate("test-robot-01")


def _whoami(**overrides) -> dict:
    body = {
        "robot_id": "test-robot-01", "display_name": "Reception", "status": "active",
        "credential": {"kind": "fleet", "key_fingerprint": None, "key_version": 1},
        "org": "example-org", "partner": "emori-robot", "course_id": "course-a",
        "languages": ["ca", "en"], "pathways": ["Example pathway"],
        "identity_domains": ["example.org"], "modules": [],
        "features": {"feature_a": False, "feature_b": False, "feature_c": True},
    }
    body.update(overrides)
    return body


def _snapshot(root, token, *, whoami=None, previous=None, key_version=1,
              fingerprint="token") -> dict:
    snap = ss.merge_scope_snapshot(
        previous, whoami or _whoami(), contract=1,
        key_fingerprint=token.fingerprint if fingerprint == "token" else fingerprint,
        key_version=key_version, now=1_000.0)
    ss.write_scope_snapshot(snap, root=root)
    return snap


def _env(token=None, **values) -> dict:
    env = dict(values)
    if token is not None:
        env["POKAMIND_ROBOT_TOKEN"] = token.reveal()
    return env


def _save(root, relative, **values) -> None:
    ss.write_env_file(ss.site_path(relative, root), values)


def _ed25519_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------- precedence
def test_admin_beats_env_beats_pokamind(tmp_path, token):
    _snapshot(tmp_path, token)
    env = _env(token, EMORI_COURSE_ID="course-env", EMORI_ROBOT_NAME="Front desk")
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_ROBOT_NAME="Saved name")

    sources = ss.apply(env, root=tmp_path)

    assert env["EMORI_ROBOT_NAME"] == "Saved name" and sources["EMORI_ROBOT_NAME"] == "admin"
    assert env["EMORI_COURSE_ID"] == "course-env" and sources["EMORI_COURSE_ID"] == "env"
    assert env["EMORI_PATHWAYS"] == "Example pathway"
    assert sources["EMORI_PATHWAYS"] == "pokamind"


def test_env_beats_hardware_beats_the_code_default(tmp_path):
    env = _env(EMORI_HARDWARE_PROFILE="reachy-mini-wireless", EMORI_CUE_Z_MM="12")

    sources = ss.apply(env, root=tmp_path)

    assert (env["EMORI_CUE_Z_MM"], sources["EMORI_CUE_Z_MM"]) == ("12", "env")
    assert (env["EMORI_CUE_AMP"], sources["EMORI_CUE_AMP"]) == ("1.6", "hardware")
    # Nothing supplies a course: the reader's code default applies.
    assert "EMORI_COURSE_ID" not in env and "EMORI_COURSE_ID" not in sources


def test_pokamind_beats_hardware_and_admin_beats_everything(tmp_path, token):
    _snapshot(tmp_path, token)
    env = _env(token, EMORI_HARDWARE_PROFILE="reachy-mini-wireless", EMORI_CUE_AMP="1.2")
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_CUE_AMP="1.4", EMORI_COURSE_ID="course-admin")

    sources = ss.apply(env, root=tmp_path)

    assert env["EMORI_CUE_AMP"] == "1.4"
    assert sources["EMORI_CUE_AMP"] == "admin"
    assert env["EMORI_COURSE_ID"] == "course-admin"


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_value_is_unset(tmp_path, token, empty):
    _snapshot(tmp_path, token)
    env = _env(token, EMORI_COURSE_ID=empty, EMORI_HARDWARE_PROFILE="reachy-mini-wireless",
               EMORI_CUE_Z_MM=empty)

    sources = ss.apply(env, root=tmp_path)

    assert (env["EMORI_COURSE_ID"], sources["EMORI_COURSE_ID"]) == ("course-a", "pokamind")
    assert (env["EMORI_CUE_Z_MM"], sources["EMORI_CUE_Z_MM"]) == ("18", "hardware")


def test_an_empty_value_nothing_fills_is_left_exactly_as_it_was(tmp_path):
    env = _env(EMORI_COURSE_ID="", NGSILD_TENANT="")

    assert ss.apply(env, root=tmp_path) == {}
    assert env == {"EMORI_COURSE_ID": "", "NGSILD_TENANT": ""}


def test_an_empty_saved_value_is_unset_too(tmp_path):
    env = _env(EMORI_SMTP_HOST="smtp.example.org")
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_SMTP_HOST="")

    ss.apply(env, root=tmp_path)

    assert env["EMORI_SMTP_HOST"] == "smtp.example.org"


def test_site_auto_0_switches_the_pokamind_layer_off(tmp_path, token):
    _snapshot(tmp_path, token)
    env = _env(token, EMORI_SITE_AUTO="0")

    sources = ss.apply(env, root=tmp_path)

    assert "EMORI_COURSE_ID" not in env and "EMORI_PATHWAYS" not in env
    assert "pokamind" not in sources.values()
    assert ss.auto_enabled(env) is False
    assert ss.auto_enabled({}) is True and ss.auto_enabled({"EMORI_SITE_AUTO": "1"}) is True


def test_no_token_means_no_derivation(tmp_path, token):
    _snapshot(tmp_path, token)
    env = _env()

    assert ss.apply(env, root=tmp_path) == {}
    assert env == {}


# --------------------------------------------------------------------------- whose snapshot
def test_a_snapshot_for_another_robot_is_ignored(tmp_path, token):
    other = robot_token.generate("other-robot-02")
    _snapshot(tmp_path, other, whoami=_whoami(robot_id="other-robot-02"))
    env = _env(token)

    ss.apply(env, root=tmp_path)

    assert "EMORI_COURSE_ID" not in env
    assert ss.load_scope_snapshot("test-robot-01", token.fingerprint, root=tmp_path) is None
    assert ss.scope_snapshot_status("test-robot-01", root=tmp_path) == "scope_robot_mismatch"


def test_a_snapshot_for_another_key_of_the_same_robot_is_ignored(tmp_path, token):
    _snapshot(tmp_path, token, fingerprint="AAAAAAAAAAAAAAAA")
    env = _env(token)

    ss.apply(env, root=tmp_path)

    assert "EMORI_COURSE_ID" not in env
    assert ss.scope_snapshot_status("test-robot-01", token.fingerprint, root=tmp_path) \
        == "scope_fingerprint_mismatch"
    # ...and when one side does not know the fingerprint, the ids decide.
    assert ss.load_scope_snapshot("test-robot-01", None, root=tmp_path) is not None


@pytest.mark.parametrize("content, code", [
    (None, "scope_missing"),
    ("{not json", "scope_unreadable"),
    (json.dumps({"schema": 99, "whoami": {}}), "scope_schema_unknown"),
    (json.dumps({"schema": 1, "whoami": "x"}), "scope_schema_unknown"),
])
def test_an_unusable_snapshot_is_none_with_a_code(tmp_path, content, code):
    if content is not None:
        path = ss.site_path(ss.SCOPE_FILE, tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")

    assert ss.load_scope_snapshot("test-robot-01", root=tmp_path) is None
    assert ss.scope_snapshot_status("test-robot-01", root=tmp_path) == code


# --------------------------------------------------------------------------- derive rules
def test_display_name_equal_to_the_robot_id_is_not_a_name():
    snap = {"whoami": _whoami(display_name="test-robot-01")}
    assert "EMORI_ROBOT_NAME" not in ss.derive_from_scope(snap)
    assert ss.derive_from_scope({"whoami": _whoami(display_name="  ")}).get(
        "EMORI_ROBOT_NAME") is None
    assert ss.derive_from_scope({"whoami": _whoami()})["EMORI_ROBOT_NAME"] == "Reception"


def test_a_pathway_holding_a_comma_means_no_allowlist_and_is_counted(tmp_path, token, caplog):
    """An allowlist without the comma pathway would drop the claim of every badge that
    carries it: fail open (no allowlist, as without a record) and say how many."""

    whoami = _whoami(pathways=["Listening, speaking and writing", "Front desk", "", "Ward 3"])
    _snapshot(tmp_path, token, whoami=whoami)
    env = _env(token)

    with caplog.at_level(logging.INFO, logger="emori.site_settings"):
        sources = ss.apply(env, root=tmp_path)

    assert "EMORI_PATHWAYS" not in env and "EMORI_PATHWAYS" not in sources
    assert "1 Pokamind pathway(s)" in caplog.text
    assert "Rights" not in caplog.text
    # Only commas: nothing left to list means no allowlist at all.
    only = {"whoami": _whoami(pathways=["A, B"])}
    assert "EMORI_PATHWAYS" not in ss.derive_from_scope(only)


def test_an_empty_course_is_not_derived():
    assert "EMORI_COURSE_ID" not in ss.derive_from_scope({"whoami": _whoami(course_id="")})


@pytest.mark.parametrize("languages, default, expected", [
    (["ca", "en"], None, "ca"),
    (["en", "ca"], None, "en"),
    (["ca-ES", "en"], None, "ca"),
    (["es", "EN_gb"], None, "en"),
    (["es", "fr"], None, ""),
    (["ca", "en"], "en", "en"),
    (["es", "en"], "es", "en"),          # an unsupported default falls to the list
    (["ca"], "en", "ca"),                # a default outside the list is ignored
    ([], "en", "en"),                    # an empty list scopes nothing
])
def test_the_boot_language_follows_the_record(languages, default, expected):
    whoami = _whoami(languages=languages)
    if default is not None:
        whoami["default_language"] = default
    derived = ss.derive_from_scope({"whoami": whoami})
    assert derived.get("EMORI_LANG", "") == expected


def test_merge_carries_the_menu_order_and_records_the_digest():
    first = ss.merge_scope_snapshot(None, _whoami(), contract=2, key_fingerprint="F1",
                                    key_version=1, now=1.0)
    first["menu_order"] = {"ca": ["AAAAAAAAAAAAAAAAAAAA"]}
    second = ss.merge_scope_snapshot(first, _whoami(course_id="course-b"), contract=2,
                                     key_fingerprint="F1", key_version=1, now=2.0)

    assert second["menu_order"] == {"ca": ["AAAAAAAAAAAAAAAAAAAA"]}
    assert second["schema"] == ss.SNAPSHOT_SCHEMA and second["contract"] == 2
    assert second["fetched_at"] == 2.0 and second["robot_id"] == "test-robot-01"
    assert second["digest"].startswith("sha256:") and second["digest"] != first["digest"]
    assert second["digest"] == ss.snapshot_digest(_whoami(course_id="course-b"))
    other = ss.merge_scope_snapshot(first, _whoami(robot_id="other-robot-02"), contract=2,
                                    key_fingerprint="F9", key_version=1, now=3.0)
    assert other["menu_order"] == {}


# --------------------------------------------------------------------------- installed robot only
def test_an_unknown_profile_supplies_no_hardware_values(tmp_path, caplog):
    env = _env(EMORI_HARDWARE_PROFILE="another-robot")

    with caplog.at_level(logging.WARNING, logger="emori.site_settings"):
        sources = ss.apply(env, root=tmp_path)

    assert "EMORI_CUE_AMP" not in env and "EMORI_SESSION_START_S" not in env
    assert "hardware" not in sources.values()
    assert "unknown hardware profile" in caplog.text


# --------------------------------------------------------------------------- the child process
def test_inherited_values_are_derived_again_in_the_same_process(tmp_path, token):
    _snapshot(tmp_path, token)
    parent = _env(token, EMORI_HARDWARE_PROFILE="reachy-mini-wireless")
    ss.apply(parent, root=tmp_path)
    assert parent["EMORI_COURSE_ID"] == "course-a"
    marker = json.loads(parent[ss.SOURCES_ENV])
    assert marker["EMORI_COURSE_ID"] == "pokamind" and "POKAMIND_ROBOT_TOKEN" not in marker

    # Later: Pokamind changed the course, and an admin saved a microphone.
    previous = ss.load_scope_snapshot("test-robot-01", token.fingerprint, root=tmp_path)
    _snapshot(tmp_path, token, whoami=_whoami(course_id="course-b"), previous=previous)
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_CUE_Z_MM="12")
    child = dict(parent)

    sources = ss.apply(child, root=tmp_path)

    assert (child["EMORI_COURSE_ID"], sources["EMORI_COURSE_ID"]) == ("course-b", "pokamind")
    assert (child["EMORI_CUE_Z_MM"], sources["EMORI_CUE_Z_MM"]) == ("12", "admin")
    # An inherited derived value is never mistaken for an explicit .env one.
    assert "env" not in {sources[k] for k in ("EMORI_COURSE_ID", "EMORI_CUE_AMP")}


def test_a_stripped_key_gets_the_dotenv_value_back(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("EMORI_COURSE_ID=course-dotenv\n", encoding="utf-8")
    child = {"EMORI_COURSE_ID": "course-admin",
             ss.SOURCES_ENV: json.dumps({"EMORI_COURSE_ID": "admin"})}

    sources = ss.apply(child, root=tmp_path, dotenv_file=str(dotenv))

    # The admin value is gone from the files now: .env's own value is back, as 'env'.
    assert (child["EMORI_COURSE_ID"], sources["EMORI_COURSE_ID"]) == ("course-dotenv", "env")
    assert ss.SOURCES_ENV not in child


def test_the_marker_is_stripped_in_a_real_child_process(tmp_path, token):
    _snapshot(tmp_path, token)
    parent = _env(token, EMORI_HARDWARE_PROFILE="reachy-mini-wireless")
    ss.apply(parent, root=tmp_path)
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_CUE_AMP="1.2")
    env = {key: value for key, value in os.environ.items()
           if key not in ss.REGISTRY and key not in (ss.SOURCES_ENV, ss.PROFILE_ENV,
                                                      "POKAMIND_ROBOT_TOKEN")}
    env.update(parent)
    env["PYTHONPATH"] = str(_REPO / "src")
    code = ("import json, os, sys\n"
            "from emori_adapter import site_settings as ss\n"
            "sources = ss.apply(os.environ, root=sys.argv[1])\n"
            "print(json.dumps({'sources': sources,"
            " 'amp': os.environ.get('EMORI_CUE_AMP')}))\n")

    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env,
                            capture_output=True, text=True, timeout=120, check=False)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["amp"] == "1.2"
    assert out["sources"]["EMORI_CUE_AMP"] == "admin"
    assert out["sources"]["EMORI_COURSE_ID"] == "pokamind"
    assert out["sources"]["EMORI_CUE_Z_MM"] == "hardware"


def test_effective_sources_reads_the_marker():
    env = {"EMORI_COURSE_ID": "c", "EMORI_LANG": "ca",
           ss.SOURCES_ENV: json.dumps({"EMORI_LANG": "pokamind"})}

    assert ss.effective_sources(env) == {"EMORI_COURSE_ID": "env", "EMORI_LANG": "pokamind"}


# --------------------------------------------------------------------------- files
def test_env_files_round_trip_and_ignore_what_is_not_a_setting(tmp_path, caplog):
    path = tmp_path / "configs" / "site" / "settings.env"
    path.parent.mkdir(parents=True)
    path.write_text('# comment\n; also a comment\n\nEMORI_LANG="ca"\nNOT_A_SETTING=x\n'
                    "garbage line\nEMORI_SMTP_HOST=smtp.example.org\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="emori.site_settings"):
        values = ss.read_env_file(path)

    assert values == {"EMORI_LANG": "ca", "EMORI_SMTP_HOST": "smtp.example.org"}
    assert "NOT_A_SETTING" in caplog.text
    assert ss.read_env_file(tmp_path / "missing.env") == {}

    quoted = {"EMORI_SMTP_USER": '"quoted"', "EMORI_SMTP_FROM": "a@example.org",
              "EMORI_RESULTS_TO": "a@example.org"}
    ss.write_env_file(path, quoted)
    assert ss.read_env_file(path) == quoted


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b"])
def test_newlines_are_refused_on_write(tmp_path, bad):
    with pytest.raises(ss.SettingsError) as refused:
        ss.write_env_file(tmp_path / "x.env", {"EMORI_SMTP_USER": bad})
    assert refused.value.code == "value_has_newline"
    assert refused.value.key == "EMORI_SMTP_USER"
    assert bad not in str(refused.value)
    assert not (tmp_path / "x.env").exists()
    with pytest.raises(ss.SettingsError):
        ss.save_settings({"EMORI_SMTP_USER": bad}, root=tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_files_are_0600_and_the_secrets_directory_0700(tmp_path):
    ss.save_settings({"EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_PASS": "pw-123"},
                     root=tmp_path)
    ss.write_scope_snapshot({"schema": 1, "whoami": {}}, root=tmp_path)

    assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700
    for relative in (ss.SETTINGS_FILE, ss.SITE_SECRETS_FILE, ss.SCOPE_FILE):
        assert ss.site_path(relative, tmp_path).stat().st_mode & 0o777 == 0o600, relative


def test_save_settings_routes_each_key_to_its_file(tmp_path):
    ss.save_settings({"EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_PASS": "pw-123",
                      "EMORI_DASH_RETENTION_DAYS": "30"}, root=tmp_path)

    assert ss.read_env_file(ss.site_path(ss.SETTINGS_FILE, tmp_path)) \
        == {"EMORI_SMTP_HOST": "smtp.example.org", "EMORI_DASH_RETENTION_DAYS": "30"}
    secrets = ss.read_env_file(ss.site_path(ss.SITE_SECRETS_FILE, tmp_path))
    assert set(secrets) == {"EMORI_SMTP_PASS"}
    # None removes a key.
    ss.save_settings({"EMORI_SMTP_PASS": None}, root=tmp_path)
    assert "EMORI_SMTP_PASS" not in ss.stored_values(tmp_path)


def test_save_settings_refuses_wrong_owners_unknown_keys_and_bad_values(tmp_path):
    for updates, code in (
        ({"EMORI_ADMIN_PIN": "123456"}, "key_wrong_owner"),
        ({"EMORI_SOMETHING": "1"}, "key_unknown"),
        ({"EMORI_SMTP_PORT": "99999"}, "value_not_port"),
        ({"POKAMIND_BASE": "http://api.example.org"}, "value_not_https"),
    ):
        with pytest.raises(ss.SettingsError) as refused:
            ss.save_settings(updates, root=tmp_path)
        assert refused.value.code == code
    assert ss.stored_values(tmp_path) == {}
    # The admin owner writes its own file.
    ss.save_settings({"EMORI_ADMIN_PIN": "123456"}, owner="admin", root=tmp_path)
    assert ss.read_env_file(ss.site_path(ss.ADMIN_SECRETS_FILE, tmp_path)) \
        == {"EMORI_ADMIN_PIN": "123456"}


def test_a_saved_value_that_fails_its_validator_is_not_applied(tmp_path, caplog):
    _save(tmp_path, ss.SETTINGS_FILE, POKAMIND_BASE="http://api.example.org",
          EMORI_LANG="ca")
    _save(tmp_path, ss.ADMIN_SECRETS_FILE, EMORI_SMTP_HOST="smtp.example.org")
    env = _env(POKAMIND_BASE="https://api.pokamind.com")

    with caplog.at_level(logging.WARNING, logger="emori.site_settings"):
        sources = ss.apply(env, root=tmp_path)

    assert env["POKAMIND_BASE"] == "https://api.pokamind.com"
    assert sources["POKAMIND_BASE"] == "env" and sources["EMORI_LANG"] == "admin"
    assert "EMORI_SMTP_HOST" not in env                      # not its file
    assert ss.check_stored_settings(tmp_path) == {"POKAMIND_BASE": "value_not_https",
                                                  "EMORI_SMTP_HOST": "key_wrong_file"}
    assert "api.example.org" not in caplog.text


def test_a_refused_value_systemd_already_applied_is_taken_back(tmp_path):
    """The units load settings.env AFTER .env, so under systemd a refused saved value
    is already in the environment, over .env's own: it must not stay in force."""

    dotenv = tmp_path / ".env"
    dotenv.write_text("POKAMIND_BASE=https://api.pokamind.com\nEMORI_SMTP_HOST=smtp.example.org\n",
                      encoding="utf-8")
    settings = ss.site_path(ss.SETTINGS_FILE, tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        "POKAMIND_BASE=http://api.example.org\nEMORI_RECORD=maybe\nEMORI_SMTP_HOST=\n",
        encoding="utf-8")
    # What systemd hands the process: .env, then settings.env over it.
    env = {"POKAMIND_BASE": "http://api.example.org", "EMORI_RECORD": "maybe",
           "EMORI_SMTP_HOST": ""}

    sources = ss.apply(env, root=tmp_path, dotenv_file=str(dotenv))

    assert (env["POKAMIND_BASE"], sources["POKAMIND_BASE"]) \
        == ("https://api.pokamind.com", "env")
    assert "EMORI_RECORD" not in env and "EMORI_RECORD" not in sources
    assert (env["EMORI_SMTP_HOST"], sources["EMORI_SMTP_HOST"]) == ("smtp.example.org", "env")


# --------------------------------------------------------------------------- validators
@pytest.mark.parametrize("key, value, code", [
    ("POKAMIND_BASE", "https://api.example.org", None),
    ("POKAMIND_BASE", "http://localhost:4000", None),
    ("POKAMIND_BASE", "http://api.example.org", "value_not_https"),
    ("POKAMIND_BASE", "https://user:pw@api.example.org", "value_not_https"),
    ("POKAMIND_BASE", "https://api.example.org/?q=1", "value_not_https"),
    ("EMORI_SMTP_FROM", "robot@example.org", None),
    ("EMORI_SMTP_FROM", "robot", "value_not_email"),
    ("EMORI_RESULTS_TO", "a@example.org, b@example.org", None),
    ("EMORI_RESULTS_TO", "a@example.org, nope", "value_not_email"),
    ("EMORI_SMTP_PORT", "587", None),
    ("EMORI_SMTP_PORT", "0", "value_not_port"),
    ("EMORI_FIWARE", "1", None),
    ("EMORI_FIWARE", "yes", "value_not_bool"),
    ("EMORI_DASH_RETENTION_DAYS", "-1", "value_out_of_range"),
    ("EMORI_ROBOT_NAME", "two\nlines", "value_has_newline"),
    ("EMORI_LANG", "es", "value_not_choice"),
    ("ORION_URL", "http://orion:1026", None),
    ("ORION_URL", "orion:1026", "value_not_url"),
    ("EMORI_ADMIN_PIN", "12345", "value_out_of_range"),
    ("EMORI_AP_PASSWORD", "short", "value_out_of_range"),
    ("NOT_A_SETTING", "x", "key_unknown"),
])
def test_validators_return_stable_slugs(key, value, code):
    assert ss.validate(key, value) == code


def test_the_registry_is_complete_and_owned():
    hostapp, admin = set(ss.hostapp_keys()), set(ss.admin_keys())
    assert admin == {"EMORI_ADMIN_PIN", "EMORI_AP_SSID", "EMORI_AP_PASSWORD",
                     "EMORI_DASH_PASSWORD"}
    assert {"POKAMIND_BASE", "EMORI_SMTP_PASS", "EMORI_DASH_RETENTION_DAYS"} <= hostapp
    assert not hostapp & admin
    assert all(ss.REGISTRY[key].secret for key in admin)
    assert ss.REGISTRY["EMORI_SMTP_PASS"].secret
    assert ss.REGISTRY["EMORI_SMTP_PASS"].file == ss.SITE_SECRETS_FILE
    assert [key for key, spec in ss.REGISTRY.items() if not spec.editable] == []
    assert {spec.restart for spec in ss.REGISTRY.values()} <= {"live", "hostapp", "admin"}


# --------------------------------------------------------------------------- restart reasons
def test_restart_reasons(token):
    plain = {"whoami": _whoami()}
    moved = {"whoami": _whoami(course_id="course-b")}
    flagged = {"whoami": _whoami(), "addon_provisioned": True}

    assert ss.restart_reasons(plain, plain, sources={}, environ={}) == []
    assert ss.restart_reasons(plain, moved, sources={}, environ={}) == ["scope_changed"]
    assert ss.restart_reasons(None, plain, sources={}, environ={}) == ["scope_changed"]
    # With no add-on installed a flag in the record names no restart.
    assert ss.restart_reasons(plain, flagged, sources={}, environ={}) == []
    # A local value outranks the derived one, so a change to it restarts nothing.
    assert ss.restart_reasons(plain, moved, sources={"EMORI_COURSE_ID": "env"},
                              environ={}) == []
    assert ss.restart_reasons(plain, moved, sources={}, environ={"EMORI_SITE_AUTO": "0"}) == []


# --------------------------------------------------------------------------- never raises, never leaks
def test_apply_never_raises_and_leaves_the_environment_as_loaded(tmp_path, monkeypatch,
                                                                 caplog):
    env = {"EMORI_COURSE_ID": "c", ss.SOURCES_ENV: json.dumps({"EMORI_LANG": "pokamind"}),
           "EMORI_LANG": "ca"}
    before = dict(env)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ss, "read_env_file", boom)
    with caplog.at_level(logging.WARNING, logger="emori.site_settings"):
        assert ss.apply(env, root=tmp_path) == {}

    assert env == before
    assert "site_settings_apply_failed" in caplog.text


def test_logs_carry_key_names_never_values(tmp_path, token, caplog):
    whoami = _whoami(course_id="course-very-private", display_name="Private Name",
                     pathways=["Private pathway"])
    _snapshot(tmp_path, token, whoami=whoami)
    ss.save_settings({"EMORI_SMTP_PASS": "pw-never-logged",
                      "EMORI_SMTP_HOST": "smtp.private.example"}, root=tmp_path)
    env = _env(token, EMORI_HARDWARE_PROFILE="reachy-mini-wireless")

    with caplog.at_level(logging.DEBUG, logger="emori.site_settings"):
        ss.save_settings({"EMORI_SMTP_USER": "user-never-logged"}, root=tmp_path)
        ss.apply(env, root=tmp_path)

    text = caplog.text
    for value in ("course-very-private", "Private Name", "Private pathway",
                  "pw-never-logged", "smtp.private.example", "user-never-logged",
                  token.reveal()):
        assert value not in text
    for name in ("EMORI_COURSE_ID=pokamind", "EMORI_SMTP_PASS=admin",
                 "EMORI_CUE_AMP=hardware", "EMORI_SMTP_USER"):
        assert name in text
    marker = env[ss.SOURCES_ENV]
    assert "pw-never-logged" not in marker and "course-very-private" not in marker


def test_apply_on_a_plain_dict_does_not_change_what_this_process_reports(tmp_path, token):
    before = (ss.effective_sources(), ss.invalid_settings(), ss.last_scope_status())
    _snapshot(tmp_path, token)

    ss.apply(_env(token), root=tmp_path)

    assert (ss.effective_sources(), ss.invalid_settings(), ss.last_scope_status()) == before


# --------------------------------------------------------------------------- pinned duplicates
def test_duplicated_constants_match_their_owners():
    from emori_adapter.hostapp import lang_state

    assert ss.SUPPORTED_LANGS == lang_state.SUPPORTED_LANGS


def test_paths_resolve_under_the_install_root(tmp_path):
    assert ss.site_path(ss.SCOPE_FILE, tmp_path) == tmp_path / "configs/site/pokamind_scope.json"
    assert ss.site_path(ss.AUTO_MENU_DIR, tmp_path) == tmp_path / "configs/site/auto"
    assert ss.is_installed_robot({"EMORI_HARDWARE_PROFILE": "reachy-mini-wireless"})
    assert not ss.is_installed_robot({"EMORI_HARDWARE_PROFILE": " "})


# --------------------------------------------------------------------------- the Reachy Mini profile
# What a Reachy Mini Wireless runs, beyond its audio devices: a robot deployed
# with only its token must come up with the same values, each named as `hardware`.
_REACHY_MINI_TUNING = {
    "EMORI_SESSION_START_MS": "120000",
    "EMORI_SESSION_START_S": "130",
    "EMORI_CUE_BODY_YAW": "1",
    "EMORI_CUE_Z_MM": "18",
    "EMORI_CUE_AMP": "1.6",
    "EMORI_TT_OVERRIDES": ('{"en":{"filler":{"enabled":false},"guard":{"maxFires":30,'
                           '"minAnswerMs":1000}},"ca":{"filler":{"enabled":false},'
                           '"guard":{"maxFires":30,"minAnswerMs":1000}}}'),
}


def test_the_reachy_mini_profile_carries_the_reference_robots_tuning():
    profile = ss.HARDWARE_PROFILES["reachy-mini-wireless"]

    for key, value in _REACHY_MINI_TUNING.items():
        assert profile.get(key) == value, key
    # Every key the profile supplies is a first-class setting: it has a source on the
    # admin page and in readycheck, is stripped and re-derived in a child process, and a
    # saved override is validated. And the profile's own values pass that validation.
    for key, value in profile.items():
        assert key in ss.REGISTRY, key
        assert ss.validate(key, value) is None, key
        spec = ss.REGISTRY[key]
        assert not spec.secret and spec.owner == "hostapp" and spec.restart == "hostapp"


def test_a_fresh_robot_gets_the_profile_and_names_it_hardware(tmp_path):
    """.env.example as the deploy copies it, plus the line install.sh adds. A non-empty
    shipped line would be an explicit .env value on every robot and outrank the profile:
    the 45 s session-start deadline did exactly that to a fresh robot."""

    from dotenv import dotenv_values

    shipped = {key: value for key, value in dotenv_values(_REPO / ".env.example").items()
               if value is not None}
    for key in ss.HARDWARE_PROFILES["reachy-mini-wireless"]:
        assert not (shipped.get(key) or "").strip(), f".env.example shadows {key}"
    env = dict(shipped, EMORI_HARDWARE_PROFILE="reachy-mini-wireless")

    sources = ss.apply(env, root=tmp_path)

    assert (env["EMORI_SESSION_START_S"], sources["EMORI_SESSION_START_S"]) == ("130", "hardware")
    assert (env["EMORI_SESSION_START_MS"], sources["EMORI_SESSION_START_MS"]) \
        == ("120000", "hardware")
    for key, value in _REACHY_MINI_TUNING.items():
        assert (env[key], sources[key]) == (value, "hardware"), key


def test_the_profiles_tuning_stays_off_other_hardware_and_an_explicit_value_wins(tmp_path):
    dev = _env()
    ss.apply(dev, root=tmp_path)
    assert not set(_REACHY_MINI_TUNING) & set(dev)

    robot = _env(EMORI_HARDWARE_PROFILE="reachy-mini-wireless", EMORI_CUE_Z_MM="12",
                 EMORI_SESSION_START_S="60")
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_CUE_AMP="1.2")

    sources = ss.apply(robot, root=tmp_path)

    assert (robot["EMORI_CUE_Z_MM"], sources["EMORI_CUE_Z_MM"]) == ("12", "env")
    assert (robot["EMORI_SESSION_START_S"], sources["EMORI_SESSION_START_S"]) == ("60", "env")
    assert (robot["EMORI_CUE_AMP"], sources["EMORI_CUE_AMP"]) == ("1.2", "admin")
    assert sources["EMORI_CUE_BODY_YAW"] == "hardware"


@pytest.mark.parametrize("key, good, bad", [
    ("EMORI_SESSION_START_MS", ["0", "5000", "120000", "600000"],
     ["4999", "600001", "-1", "45 s", "1e5"]),
    ("EMORI_SESSION_START_S", ["0", "5", "130", "600", "130.5"],
     ["4.9", "601", "-5", "inf", "nan"]),
    ("EMORI_CUE_BODY_YAW", ["0", "1"], []),
    ("EMORI_CUE_Z_MM", ["0", "8.0", "18", "20"], ["20.5", "-1", "deep"]),
    ("EMORI_CUE_AMP", ["0.5", "1.6", "2.0"], ["0.4", "2.1", "0"]),
])
def test_the_tuning_validators_hold_the_readers_own_bounds(key, good, bad):
    for value in good:
        assert ss.validate(key, value) is None, (key, value)
    for value in bad:
        assert ss.validate(key, value) == "value_out_of_range", (key, value)
    assert ss.validate(key, "1\n2") == "value_has_newline"


@pytest.mark.parametrize("env, problems", [
    ({}, []),                                                   # the code defaults: 45/45
    (dict(_REACHY_MINI_TUNING), []),                            # the profile: 120/130
    ({"EMORI_SESSION_START_MS": "120000", "EMORI_SESSION_START_S": "60"},
     [("EMORI_SESSION_START_S", "EMORI_SESSION_START_MS")]),
    # Nothing behind them with no add-on: a long backstop is in order.
    ({"EMORI_SESSION_START_MS": "120000", "EMORI_SESSION_START_S": "600"}, []),
    # 0 is off and races nothing; an unreadable value is its reader's default.
    ({"EMORI_SESSION_START_MS": "120000", "EMORI_SESSION_START_S": "0"}, []),
    ({"EMORI_SESSION_START_MS": "soon", "EMORI_SESSION_START_S": "45"}, []),
])
def test_the_session_start_deadlines_order(env, problems):
    assert ss.deadline_order_problems(env) == problems


def test_the_deadline_defaults_are_the_readers_own(monkeypatch):
    from emori_adapter.hostapp import driver, server

    for key in ss.DEADLINE_DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    assert ss.DEADLINE_DEFAULTS["EMORI_SESSION_START_MS"] == server._session_start_ms()
    assert ss.DEADLINE_DEFAULTS["EMORI_SESSION_START_S"] == driver._session_start_s()


def test_a_pending_save_is_applied_as_if_written_and_nothing_is_written(tmp_path):
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_CUE_AMP="1.2", EMORI_SESSION_START_S="150")
    env = _env(EMORI_HARDWARE_PROFILE="reachy-mini-wireless", EMORI_SESSION_START_S="100")

    sources = ss.apply(env, root=tmp_path, pending={"EMORI_CUE_AMP": "1.4",
                                                    "EMORI_SESSION_START_S": None,
                                                    "EMORI_CUE_Z_MM": "12"})

    assert (env["EMORI_CUE_AMP"], sources["EMORI_CUE_AMP"]) == ("1.4", "admin")
    # Cleared: the layer below (.env here) is what the next start would run.
    assert (env["EMORI_SESSION_START_S"], sources["EMORI_SESSION_START_S"]) == ("100", "env")
    assert (env["EMORI_CUE_Z_MM"], sources["EMORI_CUE_Z_MM"]) == ("12", "admin")
    assert ss.read_env_file(ss.site_path(ss.SETTINGS_FILE, tmp_path)) == {
        "EMORI_CUE_AMP": "1.2", "EMORI_SESSION_START_S": "150"}


def test_body_yaw_takes_the_on_off_the_settings_tab_offers():
    assert ss.validate("EMORI_CUE_BODY_YAW", "true") == "value_not_bool"
    assert ss.validate("EMORI_CUE_BODY_YAW", "2") == "value_not_bool"


def test_a_value_the_validator_accepts_is_one_the_reader_keeps(monkeypatch):
    """The validators' bounds are the readers' own, so a saved value is never silently
    replaced by a code default (or clamped) when the host app reads it."""

    from emori_adapter.behavior import cues, repertoire
    from emori_adapter.hostapp import driver, server

    def reads(key, value, read):
        monkeypatch.setenv(key, value)
        return read()

    for value in ("5000", "600000", "0"):
        assert reads("EMORI_SESSION_START_MS", value, server._session_start_ms) == int(value)
    for value in ("5", "600", "0", "130"):
        assert reads("EMORI_SESSION_START_S", value, driver._session_start_s) == float(value)
    assert reads("EMORI_CUE_Z_MM", "20", repertoire.cue_z_limit_mm) == 20.0
    assert repertoire._CUE_Z_MM_MAX == 20.0
    assert reads("EMORI_CUE_BODY_YAW", "1", repertoire.cue_body_yaw_enabled) is True
    for value in ("0.5", "2.0"):
        assert reads("EMORI_CUE_AMP", value, cues.CueConfig.from_env).amp == float(value)


def _reader(name: str):
    from emori_adapter.behavior import cues, repertoire
    from emori_adapter.hostapp import driver, server

    return {
        "EMORI_SESSION_START_MS": (server._session_start_ms, 45000),
        "EMORI_SESSION_START_S": (driver._session_start_s, 45.0),
        "EMORI_CUE_Z_MM": (repertoire.cue_z_limit_mm, 8.0),
        "EMORI_CUE_BODY_YAW": (repertoire.cue_body_yaw_enabled, False),
        "EMORI_CUE_AMP": (lambda: cues.CueConfig.from_env().amp, 1.0),
    }[name]


@pytest.mark.parametrize("key", [
    "EMORI_SESSION_START_MS", "EMORI_SESSION_START_S",
    "EMORI_CUE_Z_MM", "EMORI_CUE_BODY_YAW", "EMORI_CUE_AMP",
])
@pytest.mark.parametrize("blank", ["", "  "])
def test_every_reader_takes_an_empty_line_as_its_code_default(monkeypatch, key, blank):
    """.env.example ships the session-start pair EMPTY so the profile can fill it; a box
    with no profile (the simulator, a dev box) then hands the readers an empty string."""

    read, default = _reader(key)
    monkeypatch.setenv(key, blank)

    assert read() == default


def test_an_empty_or_broken_start_deadline_never_stops_the_watchdog(monkeypatch):
    """Not float(os.environ.get(..., "45")): an empty line would raise in the
    WatchdogTicker's constructor and take the host app down with it."""

    from emori_adapter.hostapp import driver

    for value in ("", "abc", "-3", "inf", "nan"):
        monkeypatch.setenv("EMORI_SESSION_START_S", value)
        assert driver._session_start_s() == 45.0, value
    monkeypatch.delenv("EMORI_SESSION_START_S")
    assert driver._session_start_s() == 45.0


@pytest.mark.parametrize("value, code", [
    ('{"ca":{"hysteresis":{"pos":0.62}}}', None),
    ('{"en":{},"ca":{"guard":{"maxFires":30}}}', None),
    ("", "value_not_tt_patch"),
    ("{}", "value_not_tt_patch"),
    ('{"es":{"guard":{}}}', "value_not_tt_patch"),
    ('{"en":[1,2]}', "value_not_tt_patch"),
    ("[1]", "value_not_tt_patch"),
    ("{not json", "value_not_tt_patch"),
    ('{"en":{"x":"' + "a" * 5000 + '"}}', "value_out_of_range"),
])
def test_the_turn_taking_patch_is_judged_by_its_shape(value, code):
    assert ss.validate("EMORI_TT_OVERRIDES", value) == code


def test_the_profiles_turn_taking_patch_is_one_the_kiosk_accepts():
    from emori_adapter.hostapp import tt_inject

    value = ss.HARDWARE_PROFILES["reachy-mini-wireless"]["EMORI_TT_OVERRIDES"]
    patch = tt_inject.parse_tt_patch_env(value)

    assert set(patch) == {"en", "ca"} == set(tt_inject.TT_LANGS)
    assert patch["ca"] == patch["en"] == {"filler": {"enabled": False},
                                          "guard": {"maxFires": 30, "minAnswerMs": 1000}}
    assert ss.TT_PATCH_MAX_BYTES == tt_inject._MAX_BYTES
    # What the shape check refuses at the top level, the kiosk's own validator refuses too.
    for refused in ('{"es":{}}', '{"en":1}', "[]"):
        assert ss.validate("EMORI_TT_OVERRIDES", refused) == "value_not_tt_patch"
        assert tt_inject.parse_tt_patch_env(refused) == {}


def _many_leaves(count: int) -> str:
    return '{"en":{' + ",".join(f'"k{i}":1' for i in range(count)) + "}}"


# Patches the kiosk takes, and patches it refuses below the top level: a saved value
# outranks the profile's, so one the admin page let through and the host app then dropped
# would leave the robot with no turn-taking tuning at all, and nothing would say so.
_TT_CORPUS = [
    '{"ca":{"hysteresis":{"pos":0.62,"neg":0.45}}}',
    '{"en":{"filler":{"enabled":false},"guard":{"maxFires":30,"minAnswerMs":600}}}',
    '{"en":{},"ca":{"guard":{"maxFires":30}}}',
    '{"en":{"mode":"fast-path_1"}}',
    '{"en":{"a":{"b":{"c":1}}}}',
    _many_leaves(64),
    '{"en":{"guard":{"maxFires":[30]}}}',
    '{"en":{"guard":{"maxFires":"30 fires"}}}',
    '{"en":{"guard":{"maxFires":null}}}',
    '{"en":{"__proto__":{"x":1}}}',
    '{"en":{"constructor":1}}',
    '{"ca":{"guard":{"prototype":true}}}',
    '{"en":{"1x":1}}',
    '{"en":{"max-fires":1}}',
    '{"en":{"' + "k" * 33 + '":1}}',
    '{"en":{"mode":"' + "a" * 33 + '"}}',
    '{"en":{"mode":""}}',
    '{"en":{"a":{"b":{"c":{"d":1}}}}}',
    _many_leaves(65),
    '{"en":{"x":NaN}}',
    '{"en":{"x":Infinity}}',
    '{"en":{"x":1e999}}',
]


@pytest.mark.parametrize("value", _TT_CORPUS)
def test_the_admin_page_takes_exactly_the_turn_taking_patches_the_kiosk_takes(value):
    from emori_adapter.hostapp import tt_inject

    try:
        tt_inject.validate_tt_patch(json.loads(value))
        kiosk_takes = True
    except tt_inject.TtPatchError:
        kiosk_takes = False

    assert (ss.validate("EMORI_TT_OVERRIDES", value) is None) is kiosk_takes
    if not kiosk_takes:
        assert ss.validate("EMORI_TT_OVERRIDES", value) == "value_not_tt_patch"


def test_both_turn_taking_validators_share_their_limits():
    from emori_adapter.hostapp import tt_inject

    assert (ss.TT_PATCH_MAX_BYTES, ss._TT_MAX_DEPTH, ss._TT_MAX_LEAVES) \
        == (tt_inject._MAX_BYTES, tt_inject._MAX_DEPTH, tt_inject._MAX_LEAVES)
    assert ss._TT_KEY_RE.pattern == tt_inject._KEY_RE.pattern
    assert ss._TT_STR_RE.pattern == tt_inject._STR_RE.pattern
    assert ss._TT_BANNED_KEYS == tt_inject._BANNED_KEYS
    assert set(ss.SUPPORTED_LANGS) == set(tt_inject.TT_LANGS)
    # An integer no float can hold is refused, not raised on.
    assert ss.validate("EMORI_TT_OVERRIDES", '{"en":{"x":' + "9" * 400 + "}}") \
        == "value_not_tt_patch"


def test_a_saved_patch_the_kiosk_would_drop_never_shadows_the_profile(tmp_path):
    inert = '{"en":{"guard":{"maxFires":[30]}}}'
    with pytest.raises(ss.SettingsError) as refused:
        ss.save_settings({"EMORI_TT_OVERRIDES": inert}, root=tmp_path)
    assert refused.value.code == "value_not_tt_patch"
    # ...and one an older release saved is refused at start, so the profile applies and
    # the Settings tab names the saved value as the problem.
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_TT_OVERRIDES=inert)
    robot = _env(EMORI_HARDWARE_PROFILE="reachy-mini-wireless")

    sources = ss.apply(robot, root=tmp_path)

    assert sources["EMORI_TT_OVERRIDES"] == "hardware"
    assert robot["EMORI_TT_OVERRIDES"] == ss.HARDWARE_PROFILES[
        "reachy-mini-wireless"]["EMORI_TT_OVERRIDES"]
    assert ss.check_stored_settings(tmp_path) == {"EMORI_TT_OVERRIDES": "value_not_tt_patch"}


# --------------------------------------------------------------------------- no add-on installed
def test_keys_only_an_add_on_understands_do_not_crash_a_public_only_install(tmp_path, token,
                                                                          caplog):
    """A robot's settings file may hold keys an add-on registered. Without that add-on they
    are unknown: reported by ``check_stored_settings``, never applied, never fatal."""

    _snapshot(tmp_path, token)
    snap = ss.load_scope_snapshot("test-robot-01", token.fingerprint, root=tmp_path)
    snap["addon_provisioned"] = True                # a flag only an add-on reads
    ss.write_scope_snapshot(snap, root=tmp_path)
    _save(tmp_path, ss.SETTINGS_FILE, EMORI_ADDON_ONLY_SETTING="on", EMORI_CUE_AMP="1.2")
    env = _env(token, EMORI_ADDON_ONLY_SETTING="env-value")

    with caplog.at_level(logging.WARNING, logger="emori.site_settings"):
        sources = ss.apply(env, root=tmp_path)

    assert env["EMORI_ADDON_ONLY_SETTING"] == "env-value"       # left exactly as loaded
    assert "EMORI_ADDON_ONLY_SETTING" not in sources
    assert (env["EMORI_CUE_AMP"], sources["EMORI_CUE_AMP"]) == ("1.2", "admin")
    assert sources["EMORI_COURSE_ID"] == "pokamind"
    assert ss.check_stored_settings(tmp_path) == {}
    assert ss.validate("EMORI_ADDON_ONLY_SETTING", "on") == "key_unknown"
    with pytest.raises(ss.SettingsError) as refused:
        ss.save_settings({"EMORI_ADDON_ONLY_SETTING": "on"}, root=tmp_path)
    assert refused.value.code == "key_unknown"
