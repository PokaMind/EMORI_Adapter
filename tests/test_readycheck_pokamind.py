"""Readycheck and THIS robot's own Pokamind token.

Three gates and one changed chain:

* ``pokamind_token`` -- local facts: a token is installed, it parses, it names this
  robot, the running hostapp actually uses it (a WARN while a robot is still on the
  shared fleet secret, which works until Pokamind switches it off);
* ``pokamind_whoami`` -- Pokamind's own answer, asked with the robot's credential:
  active, owned by an org, the kiosk language and the session course inside scope;
* ``voice_menu_in_scope`` -- every option the menu speaks is a module Pokamind lets
  THIS robot start;
* ``session_chain`` / ``badge_session`` probe logins are signed with the credential the
  kiosk itself would use, and their hints say which one.
"""

from __future__ import annotations

import dataclasses

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from emori_adapter import robot_token
from emori_adapter.hostapp.pokamind_whoami import WhoamiResult
from emori_adapter.hostapp.readycheck import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    HttpResult,
    MenuProbe,
    SsoResult,
    SystemProbe,
    TokenProbe,
    announce_failure,
    collect,
    evaluate,
    exit_code,
)
from tests.test_readycheck import (
    _CALLBACK,
    _CHAIN_ENV,
    _MENU_OPTIONS,
    GOLDEN_TOKEN,
    TOKEN,
    _by_name,
    _FakeProbe,
    _healthy,
    _site,
    _tokenless,
    whoami,
    whoami_module,
)

SEED = GOLDEN_TOKEN.rsplit(".", 1)[1]


def _gate(evidence, name):
    return _by_name(evaluate(evidence))[name]


# --- pokamind_token -----------------------------------------------------------------------


def test_a_robot_on_its_own_token_passes():
    gate = _gate(_healthy(), "pokamind_token")

    assert (gate.status, gate.code) == (PASS, "robot_token")


def test_a_robot_without_its_token_fails_early_with_the_one_line_fix():
    results = evaluate(_tokenless())
    gate = _by_name(results)["pokamind_token"]

    assert (gate.status, gate.code) == (FAIL, "pokamind_token_missing")
    assert gate.hint.startswith("install this robot's Pokamind token: admin page -> "
                                "Pokamind -> Replace token, or Deploy-to-Robot")
    assert "docs/deploy-a-new-robot.md" in gate.hint
    assert exit_code(results) == 1


def test_an_unusable_token_fails_with_its_reason_and_never_the_value():
    broken = TokenProbe(state="invalid", source="file",
                        path="/opt/emori/secrets/pokamind_robot_token", error="seed_invalid")

    gate = _gate(_healthy(token=broken, whoami=None), "pokamind_token")

    assert (gate.status, gate.code) == (FAIL, "pokamind_token_invalid")
    assert "seed_invalid" in gate.hint and "/opt/emori/secrets/pokamind_robot_token" in gate.hint
    assert "Replace token" in gate.hint


def test_an_emori_robot_id_for_another_robot_is_a_mismatch_naming_both():
    gate = _gate(_healthy(env_robot_id="ward-3"), "pokamind_token")

    assert (gate.status, gate.code) == (FAIL, "robot_id_mismatch")
    assert "ward-3" in gate.hint and "test-robot-01" in gate.hint
    assert SEED not in gate.hint
    # The remedy is to SET the id, never to delete the line: the dashboard's fallback
    # person-code salt and the admin page's robot name are derived from it.
    assert "EMORI_ROBOT_ID=test-robot-01" in gate.hint and "EMORI_DASH_SALT" in gate.hint
    assert "delete the EMORI_ROBOT_ID" not in gate.hint
    # Unset is fine: the id is derived from the token.
    assert _gate(_healthy(env_robot_id=""), "pokamind_token").status == PASS


def test_a_roster_that_pins_another_drop_in_account_fails_without_naming_it():
    gate = _gate(_healthy(anonymous_is_robot=False), "pokamind_token")

    assert (gate.status, gate.code) == (WARN, "anonymous_identity_not_this_robot")
    assert "robot-test-robot-01@emori.local" in gate.hint
    assert _gate(_healthy(anonymous_is_robot=True), "pokamind_token").status == PASS
    assert _gate(_healthy(anonymous_is_robot=None), "pokamind_token").status == PASS


def test_the_real_probe_compares_the_roster_s_drop_in_account(monkeypatch, tmp_path):
    roster = tmp_path / "roster.yaml"
    roster.write_text("course_id: c\n", encoding="utf-8")
    monkeypatch.setenv("EMORI_ROSTER", str(roster))
    monkeypatch.setenv("POKAMIND_ROBOT_TOKEN", GOLDEN_TOKEN)
    robot_token.reset_configured()

    assert SystemProbe().roster_anonymous_is_robot("test-robot-01") is True
    # An @emori.local pin is replaced by the robot's own account under a token ...
    roster.write_text("course_id: c\nanonymous:\n  student_id: room-3\n", encoding="utf-8")
    assert SystemProbe().roster_anonymous_is_robot("test-robot-01") is True
    # ... a staff-domain shared account is not, and readycheck says so.
    roster.write_text("course_id: c\nanonymous:\n  email: reception@example.org\n", encoding="utf-8")
    assert SystemProbe().roster_anonymous_is_robot("test-robot-01") is False


def test_a_saved_token_the_running_hostapp_has_not_picked_up_is_a_warning():
    stale = HttpResult(True, 200, {"status": "ok", "demo": False, "robot": True,
                                   "pokamind_credential": "none"})

    gate = _gate(_healthy(healthz=stale), "pokamind_token")

    assert (gate.status, gate.code) == (WARN, "token_not_in_force")
    assert "restart emori-hostapp" in gate.hint
    # An older hostapp that does not report the field is not judged.
    old = HttpResult(True, 200, {"status": "ok", "demo": False, "robot": True})
    assert _gate(_healthy(healthz=old), "pokamind_token").status == PASS


def _running_with_key(fingerprint):
    return HttpResult(True, 200, {"status": "ok", "demo": False, "robot": True,
                                  "pokamind_credential": "robot_token",
                                  "pokamind_key_fingerprint": fingerprint})


def test_a_rotated_token_the_running_hostapp_still_signs_with_the_old_key_fails():
    # Rotate -> Replace token -> no restart. Every other Pokamind gate proves the token
    # ON DISK, so the robot reads all green while Pokamind refuses each session the kiosk
    # mints (invalid_assertion: the old key). Both credentials are "robot_token"; only
    # the key fingerprint tells them apart.
    gate = _gate(_healthy(healthz=_running_with_key("OLDKEYfingerprnt")), "pokamind_token")

    assert (gate.status, gate.code) == (FAIL, "token_not_in_force")
    assert "restart emori-hostapp" in gate.hint
    assert TOKEN.fingerprint in gate.hint and "OLDKEYfingerprnt" in gate.hint
    assert SEED not in gate.hint


def test_the_running_hostapp_signing_with_the_installed_key_passes():
    gate = _gate(_healthy(healthz=_running_with_key(TOKEN.fingerprint)), "pokamind_token")
    assert (gate.status, gate.code) == (PASS, "robot_token")
    # A hostapp that could not compute its fingerprint reports null: not judged.
    assert _gate(_healthy(healthz=_running_with_key(None)), "pokamind_token").status == PASS


def test_a_world_readable_token_file_is_a_warning():
    loose = dataclasses.replace(TOKEN, insecure_permissions=True)

    gate = _gate(_healthy(token=loose), "pokamind_token")

    assert (gate.status, gate.code) == (WARN, "token_file_permissions")
    assert "chmod 600" in gate.hint


# --- pokamind_whoami ------------------------------------------------------------------------


def test_whoami_passes_for_an_active_robot_inside_its_scope():
    assert (_gate(_healthy(), "pokamind_whoami").status,
            _gate(_healthy(), "pokamind_whoami").code) == (PASS, "whoami_active")


def test_a_robot_without_a_token_skips_whoami_and_the_menu_scope_gate():
    gates = _by_name(evaluate(_tokenless()))

    assert (gates["pokamind_whoami"].status, gates["pokamind_whoami"].code) == (
        SKIP, "no_robot_token")
    assert (gates["voice_menu_in_scope"].status, gates["voice_menu_in_scope"].code) == (
        SKIP, "no_robot_token")


def test_one_dead_transport_is_one_failure_line():
    gates = _by_name(evaluate(_healthy(pokamind=HttpResult(False),
                                       whoami=WhoamiResult(code="whoami_unreachable"))))

    assert gates["pokamind"].status == FAIL
    assert (gates["pokamind_whoami"].status, gates["pokamind_whoami"].code) == (
        SKIP, "pokamind_unreachable")


@pytest.mark.parametrize("code, words", [
    ("robot_revoked", "REVOKED"),
    ("robot_unknown", "never issued"),
    ("whoami_auth_failed", "rotated"),
    ("whoami_not_supported", "predates"),
    ("whoami_key_mismatch", "rotated"),
    ("invalid_whoami_response", "version"),
])
def test_each_whoami_failure_is_its_own_code_with_its_own_remedy(code, words):
    gate = _gate(_healthy(whoami=WhoamiResult(code=code, http_status=401)), "pokamind_whoami")

    assert (gate.status, gate.code) == (FAIL, code)
    assert words in gate.hint
    assert "test-robot-01" in gate.hint


def test_a_403_that_was_not_pokamind_points_at_the_network_not_the_token():
    gate = _gate(_healthy(whoami=WhoamiResult(code="whoami_unreachable", http_status=403)),
                 "pokamind_whoami")

    assert (gate.status, gate.code) == (FAIL, "whoami_unreachable")
    assert "(HTTP 403)" in gate.hint
    assert "captive portal" in gate.hint and "says nothing about the token" in gate.hint


def test_a_refused_signature_names_a_credential_mismatch_on_pokamind():
    # Pokamind's record of this robot's credential can disagree with a current token:
    # it looks exactly like a rotated token unless the hint says so.
    gate = _gate(_healthy(whoami=WhoamiResult(code="whoami_auth_failed", http_status=401)),
                 "pokamind_whoami")

    assert "ask Pokamind to check this robot's credential" in gate.hint
    assert "docs/deploy-a-new-robot.md" in gate.hint


def test_an_unsafe_code_never_reaches_the_matrix():
    gate = _gate(_healthy(whoami=WhoamiResult(code="Not A Code!")), "pokamind_whoami")

    assert (gate.status, gate.code) == (FAIL, "whoami_failed")


@pytest.mark.parametrize("overrides, code", [
    ({"status": "revoked"}, "robot_not_active"),
    ({"org": ""}, "whoami_org_missing"),
    ({"languages": ("es",)}, "kiosk_language_not_in_scope"),
    ({"course_id": "another-course"}, "course_id_not_in_scope"),
])
def test_pokamind_scope_problems_that_refuse_every_session_fail(overrides, code):
    gate = _gate(_healthy(whoami=whoami(**overrides)), "pokamind_whoami")

    assert (gate.status, gate.code) == (FAIL, code)


def test_an_empty_language_or_course_scope_is_not_enforced():
    gate = _gate(_healthy(whoami=whoami(languages=(), course_id="")), "pokamind_whoami")

    assert gate.status == PASS


def test_the_course_check_names_the_setting_that_is_wrong():
    voice = _gate(_healthy(course_id_env="other-course"), "pokamind_whoami")
    roster = _gate(_healthy(badge_course_id="emori-demo"), "pokamind_whoami")

    assert voice.code == roster.code == "course_id_not_in_scope"
    assert "EMORI_COURSE_ID" in voice.hint and "other-course" in voice.hint
    assert "configs/roster.yaml" in roster.hint and "emori-demo" in roster.hint


def test_a_course_derived_from_the_record_is_in_scope_by_construction():
    # EMORI_COURSE_ID unset locally: the settings layer took it from whoami.course_id,
    # and a roster with no course of its own follows it.
    derived = _site(sources=(("EMORI_COURSE_ID", "pokamind"),))

    gate = _gate(_healthy(course_id_env="example-course", badge_course_id="example-course",
                          site=derived), "pokamind_whoami")

    assert (gate.status, gate.code) == (PASS, "whoami_active")


@pytest.mark.parametrize("source, words", [
    ("env", "set explicitly"),
    ("admin", "saved on the admin page"),
    # Derived from a sync older than the record: syncing fixes it, not an edit.
    ("pokamind", "Pokamind tab -> Sync now"),
    # Nothing sets it, so the code default is what sessions are claimed in.
    (None, "nothing sets it"),
])
def test_the_course_hint_names_where_the_effective_course_came_from(source, words):
    sources = (("EMORI_COURSE_ID", source),) if source else ()
    course = "emori" if source is None else "other-course"

    gate = _gate(_healthy(course_id_env=course, badge_course_id=course,
                          site=_site(sources=sources)), "pokamind_whoami")

    assert (gate.status, gate.code) == (FAIL, "course_id_not_in_scope")
    assert f"EMORI_COURSE_ID (voice menu) is '{course}'" in gate.hint
    assert words in gate.hint and "Apply now" in gate.hint
    if source == "admin":
        # Clearing the saved value hands the course back to the token.
        assert "Settings tab clear EMORI_COURSE_ID" in gate.hint
        assert "follows this robot's token" in gate.hint
    if source == "env":
        # A saved value outranks .env, so the admin page can set it; only an emptied
        # .env line lets the token decide.
        # The first remedy, so the sentence starts with it.
        assert "Set EMORI_COURSE_ID to 'example-course' on the admin page's Settings tab" \
            in gate.hint
        assert "empty the EMORI_COURSE_ID= line" in gate.hint
        assert "follows this robot's token" in gate.hint
    # A roster that merely follows the effective course is not blamed a second time.
    assert "configs/roster.yaml" not in gate.hint


def test_a_roster_pinning_its_own_wrong_course_is_named_with_its_fix():
    gate = _gate(_healthy(badge_course_id="emori-demo"), "pokamind_whoami")

    assert gate.code == "course_id_not_in_scope"
    assert "configs/roster.yaml course_id (badges, drop-ins) is 'emori-demo'" in gate.hint
    assert "Delete the roster's own `course_id:` line" in gate.hint
    assert "EMORI_COURSE_ID (voice menu)" not in gate.hint  # that one is in scope


def test_collect_claims_the_probe_login_in_the_effective_course():
    for raw in (None, "", "   "):
        env = {key: value for key, value in _CHAIN_ENV.items() if key != "EMORI_COURSE_ID"}
        if raw is not None:
            env["EMORI_COURSE_ID"] = raw
        probe = _TokenProbe(env, load=_token_load(), sso=SsoResult(True, 302, _CALLBACK))

        evidence = collect(probe)

        # Empty means unset: identity.DEFAULT_COURSE_ID, exactly what the kiosk claims.
        assert evidence.course_id_env == "emori"
        assert _decode(probe.sso_urls[0])[1]["course_id"] == "emori"


def test_partner_mismatch_is_judged_only_against_an_explicit_partner_name():
    other = whoami(partner="another-partner")
    site = _gate(_healthy(whoami=other, partner_env=""), "site_config")
    explicit = _gate(_healthy(whoami=other, partner_env="emori-robot"), "site_config")

    assert site.status == PASS  # the protocol constant is not a setting to compare
    assert (explicit.status, explicit.code) == (WARN, "partner_mismatch")
    assert "'another-partner'" in explicit.hint
    # collect() records only what is set: unset is "", never the constant.
    env = {key: value for key, value in _CHAIN_ENV.items() if key != "EMORI_PARTNER_NAME"}
    assert collect(_TokenProbe(env, load=_token_load())).partner_env == ""
    assert collect(_TokenProbe(_CHAIN_ENV, load=_token_load())).partner_env == "emori-robot"


def test_a_module_in_scope_that_cannot_start_is_a_warning_naming_it():
    modules = (whoami_module(_MENU_OPTIONS[0][1]), whoami_module(_MENU_OPTIONS[1][1]),
               whoami_module("example-ca-module-5", available=False,
                             problem="not_visible_to_robot"))

    gate = _gate(_healthy(whoami=whoami(modules=modules)), "pokamind_whoami")

    assert (gate.status, gate.code) == (WARN, "whoami_module_problem")
    assert "example-ca-module-5 (not_visible_to_robot)" in gate.hint


# --- voice_menu_in_scope --------------------------------------------------------------------


def test_every_offered_module_in_scope_and_startable_passes():
    gate = _gate(_healthy(), "voice_menu_in_scope")

    assert (gate.status, gate.code) == (PASS, "menu_in_scope")


def test_an_offered_module_outside_the_scope_fails_naming_the_spoken_option():
    scope = (whoami_module(_MENU_OPTIONS[0][1]),)

    gate = _gate(_healthy(whoami=whoami(modules=scope)), "voice_menu_in_scope")

    assert (gate.status, gate.code) == (FAIL, "voice_menu_module_not_in_scope")
    key, module_id = _MENU_OPTIONS[1]
    assert f"say '{key}' -> module {module_id}" in gate.hint
    assert _MENU_OPTIONS[0][1] not in gate.hint


def test_an_offered_module_in_scope_but_not_startable_fails_with_its_problem():
    scope = (whoami_module(_MENU_OPTIONS[0][1]),
             whoami_module(_MENU_OPTIONS[1][1], available=False, problem="not_found"))

    gate = _gate(_healthy(whoami=whoami(modules=scope)), "voice_menu_in_scope")

    assert (gate.status, gate.code) == (FAIL, "voice_menu_module_unavailable")
    assert f"module {_MENU_OPTIONS[1][1]} (not_found)" in gate.hint


def test_no_whoami_or_no_menu_skips_instead_of_guessing():
    failed = _healthy(whoami=WhoamiResult(code="robot_revoked"))
    no_menu = _healthy(menu=MenuProbe(False, "menu_loader_unavailable"))

    assert _gate(failed, "voice_menu_in_scope").code == "whoami_unavailable"
    assert _gate(no_menu, "voice_menu_in_scope").code == "voice_menu_unavailable"
    assert _gate(failed, "voice_menu_in_scope").status == SKIP


def test_a_whoami_proof_settles_the_voice_menu_gate():
    """Without whoami, a production robot can only WARN voice_menu_unverified_against_prod."""

    prod = dict(menu=MenuProbe(True, "", _MENU_OPTIONS,
                               source="configs/prod/voice_topics.ca.yaml",
                               lang="ca", prod_source="configs/prod/voice_topics.ca.yaml",
                               digest="sha256:a", prod_digest="sha256:a"))

    assert (_gate(_healthy(**prod), "voice_menu").status,
            _gate(_healthy(**prod), "voice_menu").code) == (PASS, "modules_verified_by_whoami")
    assert _gate(_tokenless(**prod), "voice_menu").code == "voice_menu_unverified_against_prod"
    # ...and a menu module outside the scope is no proof at all.
    narrow = whoami(modules=(whoami_module(_MENU_OPTIONS[0][1]),))
    assert _gate(_healthy(whoami=narrow, **prod), "voice_menu").code == (
        "voice_menu_unverified_against_prod")


# --- the session chain, signed by the kiosk's own credential ---------------------------------


def test_a_rejected_token_robot_login_names_the_token_and_the_whoami_check():
    gate = _gate(_healthy(sso=SsoResult(True, 403, "", "error", "module_not_in_scope")),
                 "session_chain")

    assert gate.code == "sso_token_rejected"
    assert "module_not_in_scope" in gate.hint
    assert TOKEN.masked in gate.hint and SEED not in gate.hint
    assert "pokamind_whoami" in gate.hint


def test_a_rejected_badge_login_on_a_token_robot_names_the_scope_code():
    gate = _gate(_healthy(badge_sso=SsoResult(True, 403, "", "error", "pathway_not_in_scope")),
                 "badge_session")

    assert gate.code == "badge_sso_rejected"
    assert "pathway_not_in_scope" in gate.hint


def test_a_badge_rejection_without_a_scope_code_points_at_the_roster_and_the_token():
    gate = _gate(_healthy(badge_sso=SsoResult(True, 400, "", "error")), "badge_session")

    assert gate.code == "badge_sso_rejected"
    assert "configs/roster.yaml" in gate.hint and "docs/deploy-a-new-robot.md" in gate.hint
    assert TOKEN.masked in gate.hint


def test_a_whoami_failure_is_spoken_as_a_pokamind_link_problem():
    class Speaker:
        calls: list = []

        def speak(self, url, lang, text):
            self.calls.append(text)
            return True

    evidence = _healthy(whoami=WhoamiResult(code="robot_revoked"))
    probe = Speaker()

    announce_failure(evaluate(evidence), evidence, probe)

    assert probe.calls == ["No puc connectar amb Pokamind."]


# --- collect() with a token ---------------------------------------------------------------


class _TokenProbe(_FakeProbe):
    def __init__(self, environ, *, load=None, answer=None, **kwargs):
        super().__init__(environ, **kwargs)
        self._load = load if load is not None else robot_token.TokenLoad()
        self._answer = answer
        self.whoami_calls: list = []

    def robot_token(self):
        return self._load

    def whoami(self, base_url, token):
        self.whoami_calls.append((base_url, token))
        return self._answer if self._answer is not None else whoami()


def _token_load():
    return robot_token.TokenLoad(token=robot_token.parse_token(GOLDEN_TOKEN), source="file",
                                 path="/opt/emori/secrets/pokamind_robot_token")


def _decode(url: str) -> tuple[dict, dict]:
    token = url.split("token=", 1)[1].split("&", 1)[0]
    public = Ed25519PublicKey.from_public_bytes(robot_token.parse_token(GOLDEN_TOKEN).public_key)
    return (jwt.get_unverified_header(token),
            jwt.decode(token, public, algorithms=["EdDSA"], audience="pokamind-partner-sso"))


def test_collect_signs_the_probe_login_with_the_robots_own_token():
    probe = _TokenProbe(_CHAIN_ENV, load=_token_load(), sso=SsoResult(True, 302, _CALLBACK))

    evidence = collect(probe)

    header, claims = _decode(probe.sso_urls[0])
    assert header["kid"] == "test-robot-01" and header["alg"] == "EdDSA"
    assert claims["robot_id"] == "test-robot-01"
    assert claims["course_id"] == "example-course"
    assert claims["emori_kiosk"] is True and claims["emori_lang"] == "ca"
    assert claims["exp"] - claims["iat"] == 120  # the throwaway probe ttl, as before
    # As the robot's OWN learner: the only identity Pokamind lets it log in as whatever
    # its scope (the legacy throwaway address is refused, identity_not_in_scope).
    assert claims["email"] == "robot-test-robot-01@emori.local"
    assert claims["student_id"] == "robot-test-robot-01"
    assert probe.whoami_calls == [("http://localhost:4000", _token_load().token)]
    assert evidence.whoami.ok and evidence.course_id_env == "example-course"


def test_collect_signs_the_badge_leg_with_the_token_too():
    probe = _TokenProbe(_CHAIN_ENV, load=_token_load(), sso=SsoResult(True, 302, _CALLBACK),
                        badge_claims=("example-course", "Example pathway"))

    collect(probe)

    _header, claims = _decode(probe.sso_urls[1])
    assert claims["pathway"] == "Example pathway"
    assert claims["email"] == "robot-test-robot-01@emori.local"


# The identity Pokamind admits for a robot's own login, checked BEFORE any account
# lookup. Restated here so a probe identity Pokamind can never accept fails on the
# robot's side of the contract, not on a freshly deployed robot.
_EMORI_LOCAL = "emori.local"


def _identity_allowed(robot_id: str, identity_domains: list[str], email: str) -> bool:
    email = email.strip().lower()
    if email == f"robot-{robot_id}@{_EMORI_LOCAL}":
        return True  # rule 1: the robot's own anonymous learner
    domain = email.rpartition("@")[2]
    if domain == _EMORI_LOCAL:
        return False  # rule 2: robot A can never become robot B
    return domain in identity_domains  # rule 3: an explicitly listed staff domain


@pytest.mark.parametrize("identity_domains", [[], ["example.org"]])
def test_every_token_signed_probe_login_is_an_identity_pokamind_admits(identity_domains):
    # A robot can be issued with identity_domains=[]: the probe must pass the identity rules
    # with nothing listed, or session_chain FAILs on every correctly provisioned robot.
    probe = _TokenProbe(_CHAIN_ENV, load=_token_load(), sso=SsoResult(True, 302, _CALLBACK),
                        badge_claims=("example-course", "Example pathway"))

    collect(probe)

    assert len(probe.sso_urls) == 2  # the anonymous leg and the badge leg
    for url in probe.sso_urls:
        _header, claims = _decode(url)
        assert _identity_allowed(claims["robot_id"], identity_domains, claims["email"])


def test_the_token_probe_is_exactly_the_account_the_kiosk_logs_drop_ins_in_as(monkeypatch):
    from emori_adapter.hostapp.identity import robot_identity

    monkeypatch.setenv("EMORI_ROBOT_NAME", "Demo reception")
    monkeypatch.setenv("POKAMIND_ROBOT_TOKEN", GOLDEN_TOKEN)
    robot_token.reset_configured()
    probe = _TokenProbe({**_CHAIN_ENV, "EMORI_ROBOT_NAME": "Demo reception"},
                        load=_token_load(), sso=SsoResult(True, 302, _CALLBACK))

    collect(probe)

    _header, claims = _decode(probe.sso_urls[0])
    # Same student_id, email AND name: a first-ever probe creates the account the kiosk
    # would have created, never one named after the readiness check.
    assert ({"student_id": claims["student_id"], "email": claims["email"],
             "name": claims["name"]} == robot_identity())


def test_the_token_probe_names_the_account_like_the_kiosk_when_no_name_is_set():
    probe = _TokenProbe(_CHAIN_ENV, load=_token_load(), sso=SsoResult(True, 302, _CALLBACK))

    collect(probe)

    assert _decode(probe.sso_urls[0])[1]["name"] == "EMORI"  # identity.DEFAULT_ROBOT_NAME


def test_a_robot_with_its_token_is_not_a_demo_rig():
    evidence = collect(_TokenProbe(_CHAIN_ENV, load=_token_load(),
                                   sso=SsoResult(True, 302, _CALLBACK)))

    assert evidence.start_demo_branch is False
    assert evidence.sso.reached
    # Without its token it is a demo rig.
    assert collect(_TokenProbe(_CHAIN_ENV)).start_demo_branch is True


def test_the_evidence_keeps_nothing_of_the_token_but_what_may_be_printed():
    evidence = collect(_TokenProbe({**_CHAIN_ENV, "EMORI_ROBOT_ID": "Test Robot 01"},
                                   load=_token_load()))

    assert evidence.token == TokenProbe(
        state="configured", source="file", path="/opt/emori/secrets/pokamind_robot_token",
        robot_id="test-robot-01", fingerprint="Vkdap1RjR0wChd9d", masked=TOKEN.masked)
    assert evidence.env_robot_id == "test-robot-01"  # slugged like identity.py
    assert SEED not in repr(evidence)
    assert SEED not in repr(dataclasses.asdict(evidence))


def test_collect_without_a_token_asks_whoami_nothing_and_mints_nothing():
    probe = _TokenProbe(_CHAIN_ENV, sso=SsoResult(True, 302, _CALLBACK))

    evidence = collect(probe)

    assert probe.whoami_calls == [] and probe.sso_urls == []
    assert evidence.whoami is None and evidence.token.state == "none"


def test_a_probe_without_the_token_methods_degrades_to_none():
    evidence = collect(_FakeProbe(_CHAIN_ENV))

    assert evidence.token.state == "none" and evidence.whoami is None


# --- the real probe's two new readers ------------------------------------------------------


def test_the_system_probe_reads_the_token_exactly_like_the_hostapp(monkeypatch, tmp_path):
    path = tmp_path / "secrets" / "pokamind_robot_token"
    path.parent.mkdir()
    path.write_text(GOLDEN_TOKEN + "\n", encoding="ascii")
    monkeypatch.setenv("EMORI_POKAMIND_TOKEN_FILE", str(path))

    load = SystemProbe().robot_token()

    assert (load.state, load.source, load.token.robot_id) == ("configured", "file",
                                                              "test-robot-01")


@respx.mock
def test_the_sso_probe_keeps_only_pokamind_s_fixed_scope_codes():
    url = "https://api.pokamind.com/partner/sso/login?token=x&partner=emori-robot"
    respx.get(url).mock(return_value=httpx.Response(403, json={"error": "module_not_in_scope"}))
    scoped = SystemProbe().sso(url)
    respx.get(url).mock(return_value=httpx.Response(
        401, json={"error": "Invalid token for anna@example.org"}))
    other = SystemProbe().sso(url)

    assert (scoped.status, scoped.error_field, scoped.error_code) == (
        403, "error", "module_not_in_scope")
    assert (other.status, other.error_field, other.error_code) == (401, "error", "")
    assert "anna" not in repr(other)


def test_the_provisioning_guide_the_hints_name_covers_every_refusal_code():
    from pathlib import Path

    from emori_adapter.hostapp.readycheck import _SSO_SCOPE_CODES, _TOKEN_DOC

    guide = (Path(__file__).resolve().parents[1] / _TOKEN_DOC).read_text(encoding="utf-8")

    for code in sorted(_SSO_SCOPE_CODES) + ["robot_id_mismatch", "pokamind_token_invalid",
                                            "pokamind_token_missing",
                                            "token_not_in_force", "whoami_not_supported",
                                            "whoami_auth_failed"]:
        assert f"`{code}`" in guide, code
    # A credential mismatch on Pokamind's side has its remedy, and a changed
    # EMORI_ROBOT_ID moves the dashboard's person codes unless the salt is pinned.
    assert "ask Pokamind to check this robot's credential" in guide
    assert "EMORI_DASH_SALT" in guide
