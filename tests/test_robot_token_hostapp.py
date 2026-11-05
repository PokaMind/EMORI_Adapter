"""THIS robot's own Pokamind token, wired through the hostapp.

* training SSO: the kiosk mints EdDSA with the token, its only credential; without one
  it serves demo sessions and mints nothing;
* identity: the token's robot_id is the robot's Pokamind learner; EMORI_ROBOT_ID may be
  unset (derived) and a conflicting one is reported, never obeyed;
* ``/healthz`` says which credential is in force, never the credential.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, unquote, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from emori_adapter import robot_token

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
SEED = GOLDEN_TOKEN.rsplit(".", 1)[1]


def _golden():
    return robot_token.parse_token(GOLDEN_TOKEN)


def _public_key():
    return Ed25519PublicKey.from_public_bytes(_golden().public_key)


# --- shared with the other hostapp suites ------------------------------------------------


def use_golden_token(monkeypatch, server) -> robot_token.RobotToken:
    """Bind the golden token into an imported hostapp module, as its start would."""

    load = robot_token.TokenLoad(token=_golden(), source="env")
    monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", load)
    monkeypatch.setattr(server, "ROBOT_TOKEN", load.token)
    return load.token


def sso_claims(session_url: str) -> dict:
    """The VERIFIED claims of the partner-SSO token a ``/session?sso=...`` URL carries,
    as Pokamind checks them (EdDSA, the golden token's public key, the SSO audience)."""

    token = _sso_token(session_url)
    return jwt.decode(token, _public_key(), algorithms=["EdDSA"],
                      audience="pokamind-partner-sso")


def _use_token(monkeypatch, value: str = GOLDEN_TOKEN) -> robot_token.TokenLoad:
    """Configure the token the way a robot does at start: env, then the process cache."""

    monkeypatch.setenv("POKAMIND_ROBOT_TOKEN", value)
    robot_token.reset_configured()
    return robot_token.configured()


# --- identity ------------------------------------------------------------------------------


def test_the_token_s_robot_id_is_the_robot_s_own_learner(monkeypatch):
    from emori_adapter.hostapp.identity import pokamind_robot_id, robot_identity

    monkeypatch.delenv("EMORI_ROBOT_ID", raising=False)
    _use_token(monkeypatch)

    assert pokamind_robot_id() == "test-robot-01"
    assert robot_identity()["email"] == "robot-test-robot-01@emori.local"
    assert robot_identity()["student_id"] == "robot-test-robot-01"


def test_a_conflicting_emori_robot_id_never_wins_over_the_token(monkeypatch):
    from emori_adapter.hostapp.identity import robot_identity

    monkeypatch.setenv("EMORI_ROBOT_ID", "ward-3")
    _use_token(monkeypatch)

    # Pokamind admits robot-<id>@emori.local only for the robot whose key signed: any
    # other id would be refused as identity_not_in_scope.
    assert robot_identity()["email"] == "robot-test-robot-01@emori.local"


def test_without_a_token_emori_robot_id_is_the_identity_as_before(monkeypatch):
    from emori_adapter.hostapp.identity import pokamind_robot_id, robot_identity

    monkeypatch.setenv("EMORI_ROBOT_ID", "Ward 3")

    assert pokamind_robot_id() == "ward-3"
    assert robot_identity()["email"] == "robot-ward-3@emori.local"


# --- the hostapp: demo default, /healthz, the SSO mint ---------------------------------------


@pytest.mark.parametrize("environ, token, demo", [
    ({}, None, True),
    ({"HOSTAPP_DEMO": "0"}, None, True),         # no token: demo, whatever it says
    ({"EMORI_PARTNER_SECRET": "x" * 40}, None, True),  # a retired secret changes nothing
    ({}, "token", False),
    ({"HOSTAPP_DEMO": "0"}, "token", False),
    ({"HOSTAPP_DEMO": "1"}, "token", True),
])
def test_without_a_token_the_hostapp_is_always_in_demo_mode(environ, token, demo):
    from emori_adapter.hostapp import server

    assert server.demo_default(environ, token=token) is demo


def test_the_credential_in_force_is_named_never_shown(monkeypatch):
    from emori_adapter.hostapp import server

    monkeypatch.setattr(server, "ROBOT_TOKEN", None)
    assert server.pokamind_credential() == "none"
    monkeypatch.setattr(server, "ROBOT_TOKEN", _golden())
    assert server.pokamind_credential() == "robot_token"


def _app(monkeypatch, *, token: bool, overrides=None):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.delenv("EMORI_ROBOT_ID", raising=False)
    # The demo flag as server.demo_default would bind it at import for this robot.
    monkeypatch.setattr(server, "DEMO", not token)
    monkeypatch.setattr(server, "PARTNER", "emori-robot")
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    monkeypatch.setattr(server, "COURSE_ID", "example-course")
    if token:
        load = _use_token(monkeypatch)
    else:
        monkeypatch.delenv("POKAMIND_ROBOT_TOKEN", raising=False)
        robot_token.reset_configured()
        load = robot_token.configured()
    monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", load)
    monkeypatch.setattr(server, "ROBOT_TOKEN", load.token)
    base = {"robot_client": DryRunRobotClient(), "fiware_factory": None, "idle": False,
            "watchdog": False, "gesture": False, "tts": None, "stt": None,
            "speak": lambda text, code: None}
    base.update(overrides or {})
    return TestClient(server._build_app(overrides=base))


def _sso_token(session_url: str) -> str:
    sso = unquote(parse_qs(urlparse(session_url).query)["sso"][0])
    return parse_qs(urlparse(sso).query)["token"][0]


def test_healthz_reports_which_credential_the_process_runs_with(monkeypatch):
    assert _app(monkeypatch, token=True).get("/healthz").json()["pokamind_credential"] == (
        "robot_token")
    tokenless = _app(monkeypatch, token=False).get("/healthz").json()
    assert (tokenless["pokamind_credential"], tokenless["demo"]) == ("none", True)
    body = _app(monkeypatch, token=True).get("/healthz").text
    assert SEED not in body


def test_healthz_names_the_key_in_force_by_its_public_fingerprint(monkeypatch):
    # readycheck compares it with the token on disk: after a rotation both are
    # "robot_token", and only the key says which one the running process signs with.
    health = _app(monkeypatch, token=True).get("/healthz").json()
    assert health["pokamind_key_fingerprint"] == _golden().fingerprint == "Vkdap1RjR0wChd9d"
    assert _app(monkeypatch, token=False).get("/healthz").json()[
        "pokamind_key_fingerprint"] is None


def test_a_token_robot_mints_eddsa_sso_for_its_own_learner(monkeypatch):
    from emori_adapter.hostapp import server

    client = _app(monkeypatch, token=True)
    second = server._load_menu(server.EMORI_LANG).options["2"]

    reply = client.post("/api/voice-select", json={"text": "number two please"}).json()
    token = _sso_token(reply["session_url"])
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, _public_key(), algorithms=["EdDSA"],
                        audience="pokamind-partner-sso")

    assert header == {"alg": "EdDSA", "typ": "JWT", "kid": "test-robot-01"}
    assert claims["robot_id"] == "test-robot-01"
    assert claims["module_id"] == second.module_id
    assert claims["course_id"] == "example-course"
    assert claims["emori_kiosk"] is True
    assert claims["exp"] - claims["iat"] == 600
    # The anonymous drop-in is the robot's OWN learner, derived from the token.
    assert claims["email"] == "robot-test-robot-01@emori.local"
    assert reply["sso_url"].endswith("&partner=emori-robot")


def test_a_robot_without_a_token_mints_nothing_and_serves_a_demo_session(monkeypatch):
    from emori_adapter.hostapp import server

    # A retired shared secret left in an old .env changes nothing.
    monkeypatch.setenv("EMORI_PARTNER_SECRET", "x" * 40)
    client = _app(monkeypatch, token=False)
    # Even a process whose DEMO flag says live never signs without a token.
    monkeypatch.setattr(server, "DEMO", False)

    reply = client.post("/api/voice-select", json={"text": "number two please"}).json()

    assert reply["demo"] is True and "sso_url" not in reply
    assert reply["session_url"].startswith("/session?demo=1&sid=")
    assert "token=" not in reply["session_url"]


def test_a_retired_credential_left_in_env_is_named_once_and_never_shown(monkeypatch,
                                                                         caplog):
    from emori_adapter.hostapp import server

    value = "do-not-print-" + "z" * 30
    environ = {"EMORI_PARTNER_SECRET": value, "EMORI_ROBOT_SECRET": value,
               "EMORI_ROBOT_ID": "test-robot-01"}

    with caplog.at_level(logging.WARNING, logger="emori.hostapp"):
        named = server._report_retired_credentials(environ)

    assert named == ["EMORI_PARTNER_SECRET", "EMORI_ROBOT_SECRET"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("no longer used" in w for w in warnings)
    assert all(value not in r.getMessage() for r in caplog.records)
    assert server._report_retired_credentials({"EMORI_PARTNER_SECRET": " "}) == []


def test_a_robot_without_a_token_says_so_at_start_with_the_fix(monkeypatch, caplog):
    from emori_adapter.hostapp import server

    monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", robot_token.TokenLoad())
    monkeypatch.setattr(server, "ROBOT_TOKEN", None)

    with caplog.at_level(logging.INFO, logger="emori.hostapp"):
        server._report_pokamind_credential()

    message = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    assert "no Pokamind token" in message and "demo sessions only" in message
    assert "Replace token" in message and "Deploy-to-Robot" in message


def test_a_conflicting_robot_id_is_logged_as_an_error_with_ids_only(monkeypatch, caplog):
    from emori_adapter.hostapp import server

    load = _use_token(monkeypatch)
    monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", load)
    monkeypatch.setattr(server, "ROBOT_TOKEN", load.token)
    monkeypatch.setenv("EMORI_ROBOT_ID", "ward-3")

    with caplog.at_level(logging.INFO, logger="emori.hostapp"):
        server._report_pokamind_credential()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "ward-3" in errors[0].getMessage() and "test-robot-01" in errors[0].getMessage()
    assert all(SEED not in r.getMessage() for r in caplog.records)


def test_an_unusable_token_is_reported_and_the_kiosk_serves_demo_only(monkeypatch, caplog):
    from emori_adapter.hostapp import server

    load = _use_token(monkeypatch, GOLDEN_TOKEN[:-1])
    assert load.state == "invalid"
    monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", load)
    monkeypatch.setattr(server, "ROBOT_TOKEN", None)

    with caplog.at_level(logging.INFO, logger="emori.hostapp"):
        server._report_pokamind_credential()

    message = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    assert "seed_invalid" in message and "demo sessions only" in message
    assert SEED[:-1] not in message
    assert server.pokamind_credential() == "none"
