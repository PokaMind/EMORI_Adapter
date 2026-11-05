"""Operator-tunable __EMORI_TT turn-taking overrides, adapter side.

The Pokamind web app reads window.__EMORI_TT[lang] when a session starts and merges it
over its built-in turn-taking defaults. A headless kiosk has no console to set it.
These tests pin the ADAPTER half only: a validating store, an env seed, an env-gated HTTP
write route, and a /session page that pushes the patch to EXPLICIT origins only.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from emori_adapter.hostapp import server
from emori_adapter.hostapp.tt_inject import (
    TtOverrideStore,
    TtPatchError,
    parse_tt_patch_env,
    frontend_origin,
    target_origins,
    validate_tt_patch,
)
from emori_adapter.robot.client import DryRunRobotClient

# A realistic noisy-room patch: raise the voice-activity hysteresis and shorten the
# ca vad-only fallback. Leaf names are ones the web app accepts.
GOOD = {"ca": {"hysteresis": {"pos": 0.62, "neg": 0.45}, "vadOnlySilenceS": 2.0}}


# --- validation: what may ever reach a browser ----------------------------------------

def test_valid_patch_survives_validation_unchanged():
    assert validate_tt_patch(GOOD) == GOOD


@pytest.mark.parametrize("bad", [
    {"es": {"hysteresis": {"pos": 0.6}}},           # not a kiosk language
    {"ca": {"__proto__": {"pos": 0.6}}},            # a deep merge assigning out[k] -> prototype walk
    {"ca": {"constructor": {"pos": 0.6}}},
    {"ca": {"hysteresis": {"pos": [0.6]}}},         # lists are not tunable leaves
    {"ca": {"hysteresis": {"pos": float("nan")}}},  # not JSON, not a threshold
    {"ca": {"a": {"b": {"c": {"d": {"e": 1}}}}}},   # deeper than the depth cap
    {"ca": {"9bad": 1}},                            # key charset
    {"ca": {"curve": {"mode": "linear;alert(1)"}}}, # string leaf charset
    {"ca": 0.5},                                    # a language must map to a dict
    [{"ca": {}}],                                   # top level must be a dict
    "ca",
])
def test_hostile_patches_rejected(bad):
    with pytest.raises(TtPatchError) as excinfo:
        validate_tt_patch(bad)
    assert excinfo.value.code == "invalid_tt_patch"


def test_leaf_count_is_capped():
    with pytest.raises(TtPatchError):
        validate_tt_patch({"ca": {f"k{i}": float(i) for i in range(200)}})


def test_short_safe_string_leaf_is_allowed():
    assert validate_tt_patch({"en": {"curve": {"mode": "linear"}}}) == {
        "en": {"curve": {"mode": "linear"}}}


def test_store_deep_copies_so_a_caller_cannot_mutate_it():
    store = TtOverrideStore()
    source = {"ca": {"hysteresis": {"pos": 0.62}}}
    store.set(source)
    source["ca"]["hysteresis"]["pos"] = 0.99
    assert store.snapshot()["patch"] == {"ca": {"hysteresis": {"pos": 0.62}}}
    store.snapshot()["patch"]["ca"]["hysteresis"]["pos"] = 0.01
    assert store.snapshot()["patch"]["ca"]["hysteresis"]["pos"] == 0.62


def test_env_seed_parses_and_a_malformed_seed_is_inert():
    assert parse_tt_patch_env(json.dumps(GOOD)) == GOOD
    assert parse_tt_patch_env("") == {}
    assert parse_tt_patch_env("   ") == {}
    assert parse_tt_patch_env("{not json") == {}
    assert parse_tt_patch_env(json.dumps({"es": {"vadOnly": True}})) == {}


# --- origins: the patch must never be broadcast ---------------------------------------

def test_target_origins_default_is_the_pokamind_origin_only():
    assert target_origins("", "https://app.pokamind.com/partner") == ["https://app.pokamind.com"]


def test_target_origins_normalized_and_never_wildcard():
    origins = target_origins(
        "https://app.pokamind.com/, javascript:alert(1), *, http://LOCALHOST:3000, ",
        "http://localhost:4000")
    assert origins == ["https://app.pokamind.com", "http://localhost:3000",
                       "http://localhost:4000"]
    assert "*" not in origins
    assert all(o.startswith(("http://", "https://")) and " " not in o for o in origins)


def test_target_origins_rejects_userinfo_and_junk():
    assert target_origins("http://evil@localhost:3000, null, //localhost:3000", "") == []


# The regression these pin: with EMORI_TT_TARGET_ORIGINS unset and
# POKAMIND_BASE=https://api.pokamind.com, the ONLY target would be the API origin --
# while the session document (which reads window.__EMORI_TT) runs on
# https://app.pokamind.com after the SSO 302.  The browser would drop every patch in
# silence and the kiosk would run on the built-in 2.5 s default.

def test_target_origins_includes_the_frontend_the_session_actually_runs_on():
    origins = target_origins("", "https://api.pokamind.com")
    assert origins == ["https://api.pokamind.com", "https://app.pokamind.com"]


def test_a_development_setup_names_its_app_origin_itself():
    # No port guess for a local server: the record's app_origin (or
    # EMORI_TT_TARGET_ORIGINS) names the document the session runs on.
    assert target_origins("", "http://localhost:5000") == ["http://localhost:5000"]
    assert target_origins("", "http://localhost:5000", "http://localhost:3000") == [
        "http://localhost:5000", "http://localhost:3000"]


def test_frontend_origin_is_conservative_about_shapes_it_does_not_know():
    assert frontend_origin("https://pokamind.com") is None      # no api. label
    assert frontend_origin("http://localhost:4000") is None      # a local server
    assert frontend_origin("http://localhost:5000") is None
    assert frontend_origin("not a url") is None
    assert frontend_origin("") is None


def test_derived_origin_never_duplicates_an_explicit_one():
    origins = target_origins("https://app.pokamind.com", "https://api.pokamind.com")
    assert origins == ["https://app.pokamind.com", "https://api.pokamind.com"]


# --- HTTP surface ---------------------------------------------------------------------

def _app(monkeypatch):
    monkeypatch.setattr(server, "DEMO", True)
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    return server._build_app(overrides={
        "robot_client": DryRunRobotClient(),
        "fiware_factory": None,
        "idle": False,
        "watchdog": False,
        "gesture": False,
        "tts": None,
        "stt": None,
        "recorder": type("NullRec", (), {"write": staticmethod(lambda *a, **k: None)})(),
        "on_session_end": None,
    })


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("EMORI_TT_INJECT_HTTP", raising=False)
    monkeypatch.delenv("EMORI_TT_OVERRIDES", raising=False)
    monkeypatch.setenv("EMORI_TT_TARGET_ORIGINS", "http://localhost:3000")
    with TestClient(_app(monkeypatch)) as c:
        yield c


def test_write_route_is_disabled_by_default(client):
    response = client.post("/api/tt-overrides", json={"patch": GOOD})
    assert response.status_code == 403
    assert response.json() == {"error_code": "tt_inject_disabled"}
    assert client.get("/api/tt-overrides").json() == {"rev": 0, "patch": {}}


def test_gated_write_publishes_the_patch_and_bumps_the_poll_revision(client, monkeypatch):
    monkeypatch.setenv("EMORI_TT_INJECT_HTTP", "1")
    assert client.get("/api/pending-cmd").json()["tt_rev"] == 0
    accepted = client.post("/api/tt-overrides", json={"patch": GOOD})
    assert accepted.status_code == 202
    assert accepted.json() == {"rev": 1, "patch": GOOD}
    assert client.get("/api/tt-overrides").json() == {"rev": 1, "patch": GOOD}
    # the existing 250 ms /session poll learns there is something new with NO extra request
    assert client.get("/api/pending-cmd").json()["tt_rev"] == 1


def test_gated_write_rejects_a_hostile_patch_without_changing_state(client, monkeypatch):
    monkeypatch.setenv("EMORI_TT_INJECT_HTTP", "1")
    response = client.post("/api/tt-overrides",
                           json={"patch": {"ca": {"__proto__": {"pos": 0.9}}}})
    assert response.status_code == 400
    assert response.json() == {"error_code": "invalid_tt_patch"}
    assert client.get("/api/tt-overrides").json() == {"rev": 0, "patch": {}}


def test_env_seed_is_live_at_boot(monkeypatch):
    monkeypatch.setenv("EMORI_TT_OVERRIDES", json.dumps(GOOD))
    with TestClient(_app(monkeypatch)) as c:
        assert c.get("/api/tt-overrides").json() == {"rev": 1, "patch": GOOD}


def test_malformed_env_seed_leaves_the_channel_inert(monkeypatch):
    monkeypatch.setenv("EMORI_TT_OVERRIDES", "{not json")
    with TestClient(_app(monkeypatch)) as c:
        assert c.get("/api/tt-overrides").json() == {"rev": 0, "patch": {}}
        assert c.get("/api/pending-cmd").json()["tt_rev"] == 0


# --- the page: outbound only, explicit origins ----------------------------------------

def test_session_page_pushes_tt_patch_to_explicit_origins_only(client):
    html = client.get("/session?sid=nonexistent").text
    assert "cmd:'tt_patch'" in html
    assert "__TT_ORIGINS__" not in html  # token substituted
    assert json.dumps(["http://localhost:3000", "http://localhost:4000"]) in html
    # the DATA-carrying command must never be broadcast to every origin
    assert "cmd:'tt_patch',patch:patch},'*'" not in html
    # and the parent must not start ACCEPTING an emori_cmd from inside the frame:
    # the inbound listener still filters on the pokamind_ prefix only (pages.py:403)
    assert "d.type==='emori_cmd'" not in html
    assert "d.type.indexOf('pokamind_')===0" in html

def test_the_patch_is_re_pushed_until_the_frame_acknowledges_it(client):
    """A single push on the revision edge loses a race it cannot see.

    The patch is posted about 250 ms after the frame document loads, but the
    listener that applies it only exists once the session UI has mounted seconds
    later -- and postMessage has no buffer. Without retries, days of sessions can
    pass without one ``tt-patch-applied`` ACK in the journal, while the operator's
    .env setting sits unapplied and the kiosk runs profile defaults. A silent
    channel is worse than no channel, so delivery now retries until the frame
    answers, and a frame that (re)navigates re-arms it.
    """

    html = client.get("/session?sid=nonexistent").text
    # the retry loop exists, is bounded, and runs on the same 250 ms poll
    assert "function ttRetry()" in html
    assert "TT_MAX_TRIES" in html
    assert "ttRetry();" in html
    # both verdicts stop it (re-sending a rejected patch would be rejected again),
    # read through a guarded helper because a hostile frame can make any property
    # THROW on access and this listener also carries the frame-alive proof
    assert "function isTtAck(m)" in html
    assert "m.stage==='tt-patch-applied'||m.stage==='tt-patch-rejected'" in html
    assert "if(isTtAck(d)){ ttAcked=true;" in html
    # a new frame document has neither the patch nor a listener: re-arm on load
    assert "f.onload=()=>{ ttRev=-1; ttAcked=false; ttTries=0; }" in html
