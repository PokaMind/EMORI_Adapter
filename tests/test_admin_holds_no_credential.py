"""emori-admin holds no robot credential.

The phone admin service serves Wi-Fi, audio, the PIN, the hotspot, the dashboard
password and the settings page; every Pokamind call goes through the host app. So its
own settings application (``site_settings.load_admin_environment``, run by
``emori-admin``'s ``main()``) must leave it with what it serves and nothing more: not
the robot's Pokamind token (neither the env value nor the token file, whose seed is
never even decoded), not ``secrets/site.env`` (the SMTP password, an add-on's
values), not the host app's own secrets in ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from emori_adapter import robot_token, site_settings
from tests.test_robot_token_hostapp import GOLDEN_TOKEN, SEED

_SITE_SMTP_PASS = "site-env-smtp-password-value"
_ENV_SMTP_PASS = "dotenv-smtp-password-value"
_SKILL_KEY = "skill-key-value-0123456789"
_DASH_SALT = "dash-salt-value-0123456789"
_RETIRED = "retired-shared-secret-value-0123456789"
_PIN = "482915"
_ADMIN_TOKEN = "admin-token-value-0123456789"
_FOREIGN_VALUES = (GOLDEN_TOKEN, SEED, _SITE_SMTP_PASS, _ENV_SMTP_PASS, _SKILL_KEY,
                   _DASH_SALT, _RETIRED)


def _install(tmp_path: Path) -> Path:
    """An /opt/emori-shaped install: .env, the three settings files, the token file."""

    root = tmp_path / "opt-emori"
    (root / "secrets").mkdir(parents=True)
    (root / "configs" / "site").mkdir(parents=True)
    (root / ".env").write_text(
        f"EMORI_ADMIN_PIN={_PIN}\n"
        f"EMORI_ADMIN_TOKEN={_ADMIN_TOKEN}\n"
        "EMORI_ADMIN_PORT=8070\n"
        "EMORI_ROBOT_ID=test-robot-01\n"
        # A dev-box style token in .env, and the host app's secrets.
        f"POKAMIND_ROBOT_TOKEN={GOLDEN_TOKEN}\n"
        f"EMORI_SMTP_PASS={_ENV_SMTP_PASS}\n"
        f"EMORI_SKILL_KEY={_SKILL_KEY}\n"
        f"EMORI_DASH_SALT={_DASH_SALT}\n"
        # An old robot's leftover shared secret.
        f"EMORI_PARTNER_SECRET={_RETIRED}\n",
        encoding="utf-8")
    (root / site_settings.SITE_SECRETS_FILE).write_text(
        f"EMORI_SMTP_PASS={_SITE_SMTP_PASS}\n", encoding="utf-8")
    (root / site_settings.ADMIN_SECRETS_FILE).write_text(
        "EMORI_AP_SSID=emori-test-robot-01\n", encoding="utf-8")
    (root / site_settings.SETTINGS_FILE).write_text(
        "EMORI_SMTP_HOST=smtp.example.org\n", encoding="utf-8")
    (root / "secrets" / "pokamind_robot_token").write_text(GOLDEN_TOKEN + "\n",
                                                            encoding="ascii")
    return root


@pytest.fixture()
def no_token_reads(monkeypatch):
    """Fail the test if anything resolves, parses or decodes a robot token."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("emori-admin read the robot token")

    for name in ("configured", "load_token", "parse_token"):
        monkeypatch.setattr(robot_token, name, refuse)


def _foreign_left(environ) -> list[str]:
    found = sorted(key for key in site_settings.admin_foreign_keys() if key in environ)
    found += sorted(key for key, value in environ.items()
                    if any(secret in str(value) for secret in _FOREIGN_VALUES))
    return found


def test_emori_admin_starts_with_no_robot_token_seed_and_no_site_secrets(
        tmp_path, monkeypatch, no_token_reads):
    import uvicorn

    from emori_adapter import logging_setup
    from emori_adapter.admin import server as admin_server

    root = _install(tmp_path)
    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root_=None: Path(root_) if root_ is not None else root)
    monkeypatch.setattr(site_settings, "find_install_dotenv",
                        lambda start: str(root / ".env"))
    # What a parent process (or an older unit's EnvironmentFile=) may already have put
    # there: removed as well, not merely not added.
    process_env = {key: value for key, value in os.environ.items()
                   if key not in site_settings.REGISTRY}
    process_env.update({"POKAMIND_ROBOT_TOKEN": GOLDEN_TOKEN,
                        "EMORI_SMTP_PASS": _SITE_SMTP_PASS,
                        "EMORI_POKAMIND_TOKEN_FILE": str(root / "secrets" /
                                                         "pokamind_robot_token")})
    for key in (*site_settings.REGISTRY, *site_settings.admin_foreign_keys(),
                site_settings.SOURCES_ENV, "EMORI_ADMIN_PIN", "EMORI_ADMIN_TOKEN",
                "EMORI_HARDWARE_PROFILE"):
        if key not in ("POKAMIND_ROBOT_TOKEN", "EMORI_SMTP_PASS"):
            process_env.pop(key, None)
    monkeypatch.setattr(os, "environ", process_env)
    served: dict = {}
    monkeypatch.setattr(admin_server, "create_app", lambda: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: served.update(kw))
    monkeypatch.setattr(logging_setup, "setup_journal_logging", lambda: None)

    admin_server.main()

    assert served["port"] == 8070  # it started
    env = os.environ
    assert _foreign_left(env) == []
    # ...and it has everything it serves: its PIN and admin token from .env, its
    # hotspot from secrets/admin.env, the non-secret settings.
    assert (env["EMORI_ADMIN_PIN"], env["EMORI_ADMIN_TOKEN"]) == (_PIN, _ADMIN_TOKEN)
    assert env["EMORI_AP_SSID"] == "emori-test-robot-01"
    assert env["EMORI_SMTP_HOST"] == "smtp.example.org"
    # Nothing was derived from Pokamind: that takes the token, which it never loads.
    assert site_settings.SOURCE_POKAMIND not in env.get(site_settings.SOURCES_ENV, "")


def test_the_admin_loader_keeps_what_it_serves_and_drops_every_foreign_key(
        tmp_path, no_token_reads):
    root = _install(tmp_path)
    env = {"POKAMIND_ROBOT_TOKEN": GOLDEN_TOKEN, "EMORI_SKILL_KEY": _SKILL_KEY,
           "EMORI_DASH_PASSWORD": "dashboard-password-1"}

    sources = site_settings.load_admin_environment(env, root=root,
                                                   dotenv_file=str(root / ".env"))

    assert _foreign_left(env) == []
    assert env["EMORI_ADMIN_PIN"] == _PIN and env["EMORI_DASH_PASSWORD"] == (
        "dashboard-password-1")
    assert sources["EMORI_AP_SSID"] == site_settings.SOURCE_ADMIN
    assert "EMORI_SMTP_PASS" not in sources
    assert site_settings.SOURCE_POKAMIND not in sources.values()


def test_the_foreign_keys_are_the_token_and_every_site_secret_but_none_of_its_own():
    foreign = site_settings.admin_foreign_keys()

    assert robot_token.TOKEN_ENV in foreign
    assert {key for key, spec in site_settings.REGISTRY.items()
            if spec.file == site_settings.SITE_SECRETS_FILE} <= foreign
    assert {"EMORI_SKILL_KEY", "EMORI_ROS_ID_SALT", "EMORI_DASH_SALT"} <= foreign
    assert set(site_settings.RETIRED_CREDENTIAL_KEYS) <= foreign
    # What it serves stays: its own secrets and the machine token it calls the host
    # app with.
    assert not foreign & {*site_settings.admin_keys(), "EMORI_ADMIN_TOKEN",
                          "EMORI_ADMIN_SALT"}


def test_the_hostapp_still_applies_the_whole_layer(tmp_path):
    root = _install(tmp_path)
    env: dict[str, str] = {}

    site_settings.apply(env, root=root, dotenv_file=None)

    # The host app's own process does read secrets/site.env: it sends the e-mail.
    assert env["EMORI_SMTP_PASS"] == _SITE_SMTP_PASS


def test_a_leftover_credential_no_list_names_never_reaches_emori_admin(
        tmp_path, no_token_reads):
    # An older robot's .env can carry a credential this release no longer reads and no
    # list in the tree names. Any credential-shaped key emori-admin does not own is
    # dropped, from .env and from what a parent process handed it; other leftovers stay.
    root = _install(tmp_path)
    with (root / ".env").open("a", encoding="utf-8") as handle:
        handle.write("EMORI_OLD_STORE_TOKEN=leftover-bearer-value-0123456789\n"
                     "EMORI_OLD_STORE_HOST=localhost:9999\n")
    env = {"EMORI_OLD_UPLOAD_SECRET": "inherited-secret-value-0123456789",
           "EMORI_DASH_PASSWORD": "dashboard-password-1"}

    site_settings.load_admin_environment(env, root=root, dotenv_file=str(root / ".env"))

    assert "EMORI_OLD_STORE_TOKEN" not in env and "EMORI_OLD_UPLOAD_SECRET" not in env
    assert not any("leftover-bearer" in value or "inherited-secret" in value
                   for value in env.values())
    assert env["EMORI_OLD_STORE_HOST"] == "localhost:9999"
    # Its own credentials stay.
    assert (env["EMORI_ADMIN_PIN"], env["EMORI_ADMIN_TOKEN"]) == (_PIN, _ADMIN_TOKEN)
    assert env["EMORI_DASH_PASSWORD"] == "dashboard-password-1"


@pytest.mark.parametrize("key, refused", [
    ("EMORI_OLD_STORE_TOKEN", True), ("EMORI_SOME_SECRET", True),
    ("EMORI_UPLINK_PASSWORD", True), ("EMORI_VENDOR_API_KEY", True),
    ("EMORI_ROS_ID_SALT", True), ("POKAMIND_ROBOT_TOKEN", True),
    ("EMORI_ADMIN_PIN", False), ("EMORI_ADMIN_TOKEN", False), ("EMORI_ADMIN_SALT", False),
    ("EMORI_AP_PASSWORD", False), ("EMORI_DASH_PASSWORD", False),
    ("EMORI_ADMIN_PORT", False), ("EMORI_SMTP_HOST", False), ("GIT_ASKPASS", False),
    ("EMORI_KEYBOARD_MODE", False),
])
def test_which_keys_emori_admin_refuses(key, refused):
    assert site_settings.admin_refuses(key) is refused
