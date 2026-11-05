"""Readycheck and the robot-local settings layer: the ``site_config`` gate.

A robot from the public tree configures itself (``site_settings``): admin-saved values,
then ``.env``, then this robot's Pokamind record (the scope snapshot), then its hardware
profile, then the shipped defaults. The gate proves the layer is usable and says where
every value came from:

* FAIL ``pokamind_base_invalid`` -- the API origin is not https (http only for loopback);
* FAIL ``site_settings_invalid`` -- a value saved on this robot is refused, so NOT in force;
* WARN ``scope_not_synced`` / ``scope_stale`` / ``scope_robot_mismatch`` -- the record the
  robot derives from is missing, old, or someone else's;
* WARN ``partner_mismatch`` -- an EXPLICIT ``EMORI_PARTNER_NAME`` drifted from the record;
* WARN ``site_restart_pending`` -- the running hostapp has changes waiting for a restart;
* PASS ``site_config_ok`` -- with ``KEY=source`` pairs, names only, never a value.

The sources are THIS process's: readycheck re-runs the layer when it starts, so a value
inherited from whoever started it (emori-admin) is derived again, never mistaken for a
``.env`` value.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import time

import pytest

from emori_adapter import robot_token, site_settings
from emori_adapter.hostapp import readycheck
from emori_adapter.hostapp.pokamind_whoami import WhoamiResult
from emori_adapter.hostapp.readycheck import (
    FAIL,
    PASS,
    SAFE_CODE_RE,
    SKIP,
    WARN,
    HttpResult,
    SiteProbe,
    SystemProbe,
    collect,
    evaluate,
    format_matrix,
)
from tests.test_readycheck import (
    _SITE_NOW,
    GOLDEN_FINGERPRINT,
    GOLDEN_TOKEN,
    _by_name,
    _FakeProbe,
    _golden_load,
    _healthy,
    _site,
    _tokenless,
    whoami,
)

_DAY = 86400.0
_SECRETS = ("hunter2-smtp-pass", "12ab-not-a-pin", "user:hunter2@")


def _gate(**evidence):
    return _by_name(evaluate(_healthy(**evidence)))["site_config"]


# --- the verdicts --------------------------------------------------------------------


def test_a_robot_configured_in_env_passes_and_lists_every_source():
    gate = _gate()

    assert (gate.status, gate.code) == (PASS, "site_config_ok")
    for pair in ("EMORI_COURSE_ID=env", "EMORI_LANG=env", "EMORI_ROBOT_NAME=env",
                 "POKAMIND_BASE=env"):
        assert pair in gate.hint
    assert "Pokamind scope ok, contract 2" in gate.hint


def test_the_detail_names_every_layer_a_value_came_from():
    sources = (("EMORI_COURSE_ID", "pokamind"), ("EMORI_ROBOT_NAME", "hardware"),
               ("EMORI_SMTP_HOST", "admin"), ("EMORI_ROBOT_NAME", "default"))

    gate = _gate(site=_site(sources=sources))

    assert gate.status == PASS
    assert gate.hint.split(" (")[0] == " ".join(f"{key}={source}" for key, source in sources)


def test_a_settings_layer_nobody_asked_about_skips_rather_than_passing():
    gate = _gate(site=SiteProbe())

    assert (gate.status, gate.code) == (SKIP, "site_config_not_probed")


def test_a_layer_with_nothing_set_still_says_so():
    gate = _gate(site=_site(sources=()))

    assert gate.status == PASS
    assert "no robot-local setting has a value" in gate.hint


@pytest.mark.parametrize("base, slug", [
    ("http://192.0.2.5:4000", "value_not_https"),
    ("ftp://api.example.org", "value_not_https"),
    ("https://user:hunter2@api.example.org", "value_not_https"),
    ("https://api.example.org?x=1", "value_not_https"),
])
def test_an_unusable_api_origin_fails_without_quoting_it(base, slug):
    gate = _gate(pokamind_base=base)

    assert (gate.status, gate.code) == (FAIL, "pokamind_base_invalid")
    assert slug in gate.hint and "Settings -> Server" in gate.hint
    assert base not in gate.hint and "hunter2" not in gate.hint
    assert site_settings.DEFAULT_POKAMIND_BASE in gate.hint  # what emptying it gives


@pytest.mark.parametrize("base", ["https://api.pokamind.com", "http://localhost:4000",
                                  "http://127.0.0.1:8001", "https://api.example.org:8443"])
def test_an_https_or_loopback_origin_is_fine(base):
    assert _gate(pokamind_base=base).status == PASS


def test_where_a_bad_origin_was_set_is_named():
    admin = _gate(pokamind_base="http://192.0.2.5",
                  site=_site(sources=(("POKAMIND_BASE", "admin"),)))
    env = _gate(pokamind_base="http://192.0.2.5")

    assert "saved on the admin page" in admin.hint
    assert "set in /opt/emori/.env" in env.hint


def test_a_refused_saved_value_fails_naming_the_key_never_the_value():
    gate = _gate(site=_site(invalid=(("EMORI_ADMIN_PIN", "value_out_of_range"),
                                     ("EMORI_SMTP_PORT", "value_not_port"))))

    assert (gate.status, gate.code) == (FAIL, "site_settings_invalid")
    assert "EMORI_ADMIN_PIN (value_out_of_range)" in gate.hint
    assert "EMORI_SMTP_PORT (value_not_port)" in gate.hint
    assert "NOT in force" in gate.hint
    assert "configs/site/settings.env" in gate.hint and "secrets/admin.env" in gate.hint


def test_the_worst_finding_leads_and_every_other_one_is_still_named():
    gate = _gate(pokamind_base="http://192.0.2.5",
                 site=_site(invalid=(("EMORI_SMTP_PORT", "value_not_port"),),
                            restart_pending=True, restart_reasons=("settings_saved",)))

    assert (gate.status, gate.code) == (FAIL, "pokamind_base_invalid")
    assert "(also: site_settings_invalid, site_restart_pending)" in gate.hint


@pytest.mark.parametrize("status", ["scope_missing", "scope_unreadable",
                                    "scope_schema_unknown"])
def test_a_token_without_a_usable_scope_warns_scope_not_synced(status):
    gate = _gate(site=_site(scope_status=status, scope_fetched_at=None, scope_contract=None))

    assert (gate.status, gate.code) == (WARN, "scope_not_synced")
    assert status in gate.hint and "Sync now" in gate.hint
    assert "test-robot-01" in gate.hint


def test_a_scope_older_than_a_week_warns_and_says_how_old():
    gate = _gate(site=_site(scope_fetched_at=_SITE_NOW - 8 * _DAY - 60))

    assert (gate.status, gate.code) == (WARN, "scope_stale")
    assert "8 days old" in gate.hint and "Sync now" in gate.hint
    # A week to the second is not yet stale.
    fresh = _gate(site=_site(scope_fetched_at=_SITE_NOW - site_settings.SCOPE_STALE_S))
    assert fresh.status == PASS


def test_another_robots_scope_is_named_as_ignored():
    gate = _gate(site=_site(scope_status="scope_robot_mismatch",
                            scope_robot_id="other-robot-02", scope_fetched_at=None))

    assert (gate.status, gate.code) == (WARN, "scope_robot_mismatch")
    assert "robot 'other-robot-02'" in gate.hint and "ignored" in gate.hint


def test_a_foreign_scope_whose_owner_cannot_be_read_is_still_named_foreign():
    gate = _gate(site=_site(scope_status="scope_robot_mismatch", scope_robot_id="",
                            scope_fetched_at=None))

    assert gate.code == "scope_robot_mismatch"
    assert "belongs to another robot" in gate.hint


def test_an_older_token_of_this_robot_is_a_mismatch_too():
    gate = _gate(site=_site(scope_status="scope_fingerprint_mismatch", scope_fetched_at=None))

    assert (gate.status, gate.code) == (WARN, "scope_robot_mismatch")
    assert "another token of this robot" in gate.hint
    assert "scope_fingerprint_mismatch" in gate.hint


def test_no_token_means_no_scope_to_judge():
    gate = _by_name(evaluate(_tokenless(site=_site(scope_status="", scope_fetched_at=None))))[
        "site_config"]

    assert gate.status == PASS
    assert "no robot token: nothing is derived from Pokamind" in gate.hint


def test_derivation_switched_off_judges_no_scope():
    gate = _gate(site=_site(auto=False, scope_status="scope_missing"))

    assert gate.status == PASS
    assert "EMORI_SITE_AUTO=0" in gate.hint


def test_an_ignored_optional_whoami_key_is_named_in_the_detail():
    gate = _gate(site=_site(scope_ignored=("app_origin", "modules[0].menu_name")))

    assert gate.status == PASS
    assert "ignored as malformed: app_origin, modules[0].menu_name" in gate.hint


def test_an_explicit_partner_that_drifted_from_the_record_warns():
    gate = _gate(partner_env="staging-partner")

    assert (gate.status, gate.code) == (WARN, "partner_mismatch")
    assert "'staging-partner'" in gate.hint and "'emori-robot'" in gate.hint
    assert "protocol constant" in gate.hint


def test_the_partner_is_judged_only_when_set_explicitly():
    assert _gate(partner_env="").status == PASS
    assert _gate(partner_env="emori-robot").status == PASS


def test_the_partner_comes_from_the_live_record_else_the_last_synced_one():
    live = _gate(partner_env="emori-robot", whoami=whoami(partner="another-partner"),
                 site=_site(scope_partner="emori-robot"))
    offline = _gate(partner_env="emori-robot",
                    whoami=WhoamiResult(code="whoami_unreachable"),
                    site=_site(scope_partner="another-partner"))
    unknown = _gate(partner_env="emori-robot", whoami=None, site=_site(scope_partner=""))

    assert live.code == offline.code == "partner_mismatch"
    assert unknown.status == PASS  # nothing to compare with


def test_a_partner_value_that_is_not_a_name_is_not_quoted():
    gate = _gate(partner_env="x y\tz")

    assert gate.code == "partner_mismatch"
    assert "x y" not in gate.hint and "another value" in gate.hint


@pytest.mark.parametrize("reasons, words", [
    (("scope_changed",), "emori-admin restarts the host app by itself"),
    (("addon_provisioned", "scope_changed"), "emori-admin restarts the host app by itself"),
    (("settings_saved",), "Apply now puts them in force"),
    (("scope_changed", "calibration_saved"), "Apply now puts them in force"),
])
def test_changes_waiting_for_a_restart_warn_and_say_who_applies_them(reasons, words):
    gate = _gate(site=_site(restart_pending=True, restart_reasons=reasons))

    assert (gate.status, gate.code) == (WARN, "site_restart_pending")
    assert ", ".join(reasons) in gate.hint
    assert words in gate.hint and "never during a session" in gate.hint


def test_an_smtp_host_without_a_sender_warns_that_reports_stay_in_the_outbox():
    gate = _gate(smtp_sender_missing=True)

    assert (gate.status, gate.code) == (WARN, "smtp_from_unset")
    assert "EMORI_SMTP_FROM" in gate.hint and "artifacts/outbox/" in gate.hint
    assert "Send test e-mail" in gate.hint


@pytest.mark.parametrize("environ, missing", [
    ({}, False),  # no SMTP at all: the outbox is the design, not a fault
    ({"EMORI_SMTP_HOST": "smtp.example.org"}, True),
    ({"EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_USER": "apikey"}, True),
    ({"EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_USER": "robot@example.org"}, False),
    ({"EMORI_SMTP_HOST": "smtp.example.org", "EMORI_SMTP_FROM": "robot@example.org"}, False),
    ({"EMORI_SMTP_HOST": "  ", "EMORI_SMTP_USER": "apikey"}, False),
])
def test_collect_picks_the_sender_exactly_as_the_mailer_does(environ, missing):
    from emori_adapter.hostapp import results_email

    assert collect(_FakeProbe(environ)).smtp_sender_missing is missing
    assert readycheck.smtp_sender(environ.get("EMORI_SMTP_FROM", ""),
                                  environ.get("EMORI_SMTP_USER", "")) == (
        results_email.sender_from(environ))


@pytest.mark.parametrize("pending", [False, None])
def test_nothing_pending_or_an_unasked_hostapp_is_no_finding(pending):
    assert _gate(site=_site(restart_pending=pending)).status == PASS


def test_a_stale_scope_and_a_pending_restart_are_both_reported():
    gate = _gate(site=_site(scope_fetched_at=_SITE_NOW - 30 * _DAY, restart_pending=True,
                            restart_reasons=("scope_changed",)))

    assert gate.code == "scope_stale"
    assert "(also: site_restart_pending)" in gate.hint


def test_the_matrix_prints_the_sources_under_their_own_marker():
    text = format_matrix(evaluate(_healthy()))

    lines = [line for line in text.splitlines() if line.startswith("  = site_config: ")]
    assert len(lines) == 1 and "EMORI_COURSE_ID=env" in lines[0]
    assert text.count("  ! ") == 0 and text.count("  ~ ") == 0


def test_every_site_verdict_keeps_the_code_grammar():
    scenarios = [
        _site(), SiteProbe(), _site(invalid=(("EMORI_SMTP_PORT", "value_not_port"),)),
        _site(scope_status="scope_missing"), _site(scope_status="scope_robot_mismatch"),
        _site(scope_fetched_at=0.0), _site(restart_pending=True),
    ]
    for site in scenarios:
        gate = _gate(site=site, partner_env="staging-partner")
        assert SAFE_CODE_RE.fullmatch(gate.code), gate
        assert gate.status in (PASS, SKIP) or gate.hint, gate


def test_a_site_config_failure_is_a_local_fault_when_spoken():
    # "I cannot reach Pokamind" is for the link gates; a refused saved value is local.
    assert "site_config" not in readycheck._LINK_GATES


# --- collect() ------------------------------------------------------------------------


class _SiteProbe(_FakeProbe):
    def __init__(self, environ, *, site=None, healthz=None, answer=None, load=None):
        super().__init__(environ, answers={
            "http://127.0.0.1:8080/healthz": healthz if healthz is not None else HttpResult(
                True, 200, {"status": "ok", "demo": False})})
        self._site_answer = site if site is not None else _site()
        self._answer = answer
        self._load = load if load is not None else robot_token.TokenLoad()
        self.site_asked: list = []
        self.asked: list = []

    def robot_token(self):
        return self._load

    def site_config(self, robot_id, fingerprint=""):
        self.site_asked.append((robot_id, fingerprint))
        return self._site_answer

    def hostapp_site(self, url, token):
        self.asked.append((url, token))
        return self._answer if self._answer is not None else HttpResult(
            True, 200, {"restart_pending": True, "restart_reasons": ["scope_changed"]})


def _token_load():
    return robot_token.TokenLoad(token=robot_token.parse_token(GOLDEN_TOKEN), source="file",
                                 path="/opt/emori/secrets/pokamind_robot_token")


def test_collect_judges_the_scope_against_the_token_in_force():
    with_token = _SiteProbe({}, load=_token_load())
    without = _SiteProbe({})

    collect(with_token)
    collect(without)

    assert with_token.site_asked == [("test-robot-01", GOLDEN_FINGERPRINT)]
    assert without.site_asked == [("", "")]


def test_collect_asks_the_running_hostapp_with_the_admin_token_in_a_header_only():
    answer = HttpResult(True, 200, {
        "restart_pending": True,
        "restart_reasons": ["scope_changed", "settings_saved", "scope_changed", "Bad Code!"],
        # The rest of the answer carries non-secret values; none of it is kept.
        "keys": [{"key": "EMORI_SMTP_HOST", "value": "smtp.private.example"}],
    })
    probe = _SiteProbe({"EMORI_ADMIN_TOKEN": "admin-token-value"}, answer=answer)

    evidence = collect(probe)

    assert probe.asked == [("http://127.0.0.1:8080/api/admin/site", "admin-token-value")]
    assert evidence.site.restart_pending is True
    assert evidence.site.restart_reasons == ("scope_changed", "settings_saved")
    assert "smtp.private.example" not in repr(evidence)
    assert "admin-token-value" not in repr(evidence)


@pytest.mark.parametrize("environ, healthz", [
    ({}, None),  # no admin token: nothing to ask with
    ({"EMORI_ADMIN_TOKEN": "t", "EMORI_READYCHECK_HOSTAPP_URL": "http://192.0.2.9:8080"},
     None),  # never sent off this machine
    ({"EMORI_ADMIN_TOKEN": "t"}, HttpResult(False)),  # the hostapp is not answering
    ({"EMORI_ADMIN_TOKEN": "t"}, HttpResult(True, 503, {"status": "starting"})),
])
def test_collect_does_not_ask_without_a_token_or_a_live_local_hostapp(environ, healthz):
    probe = _SiteProbe(environ, healthz=healthz)
    if "EMORI_READYCHECK_HOSTAPP_URL" in environ:
        probe._answers[environ["EMORI_READYCHECK_HOSTAPP_URL"] + "/healthz"] = HttpResult(
            True, 200, {"status": "ok"})

    evidence = collect(probe)

    assert probe.asked == []
    assert (evidence.site.restart_pending, evidence.site.restart_reasons) == (None, ())


@pytest.mark.parametrize("answer", [
    HttpResult(False), HttpResult(True, 401, {"error": "unauthorized"}),
    HttpResult(True, 200, {"restart_pending": "yes", "restart_reasons": "scope_changed"}),
])
def test_an_unusable_hostapp_answer_is_not_read_as_nothing_pending(answer):
    evidence = collect(_SiteProbe({"EMORI_ADMIN_TOKEN": "t"}, answer=answer))

    assert evidence.site.restart_pending is None and evidence.site.restart_reasons == ()


def test_a_probe_without_the_settings_reader_skips_the_gate():
    evidence = collect(_FakeProbe({}))

    assert evidence.site == SiteProbe()
    assert _by_name(evaluate(evidence))["site_config"].code == "site_config_not_probed"


def test_collect_reads_an_explicit_partner_only():
    assert collect(_FakeProbe({})).partner_env == ""
    assert collect(_FakeProbe({"EMORI_PARTNER_NAME": " staging "})).partner_env == "staging"
    # The protocol constant still names the partner of the probe logins when nothing
    # is set.
    probe = _FakeProbe({}, token_load=_golden_load())
    collect(probe)
    assert probe.sso_urls[0].endswith("&partner=emori-robot")


# --- the real probe -------------------------------------------------------------------


@pytest.fixture
def layer(monkeypatch, tmp_path):
    """A clean process: the settings layer re-run on an environment we control.

    ``site_settings.apply`` writes into os.environ and records this process's sources;
    both are put back at teardown, and every file lives under ``tmp_path``.
    """

    saved = dict(os.environ)
    for key in list(site_settings.REGISTRY) + [
            site_settings.SOURCES_ENV, site_settings.AUTO_ENV, site_settings.PROFILE_ENV,
            robot_token.TOKEN_ENV, robot_token.TOKEN_FILE_ENV, "EMORI_PARTNER_NAME"]:
        os.environ.pop(key, None)
    os.environ[robot_token.TOKEN_FILE_ENV] = str(tmp_path / "secrets" / "no-token-file")
    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root=None: Path(root) if root is not None else tmp_path)
    monkeypatch.setattr(site_settings, "_last_sources", site_settings._last_sources)
    monkeypatch.setattr(site_settings, "_last_invalid", site_settings._last_invalid)
    monkeypatch.setattr(site_settings, "_last_scope_status",
                        site_settings._last_scope_status)
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved)


def _snapshot(root: Path, *, robot_id="test-robot-01", course="fresh-course",
              fetched_at=None, ignored=(), contract=2, provisioned=False):
    token = robot_token.parse_token(GOLDEN_TOKEN)
    body = {
        "robot_id": robot_id, "display_name": "Example Site", "status": "active",
        "credential": {"kind": "fleet", "key_fingerprint": token.fingerprint,
                       "key_version": 1},
        "org": "example-org", "partner": "emori-robot", "course_id": course,
        "languages": ["ca", "en"], "pathways": ["Example pathway"], "identity_domains": [],
        "modules": [], "features": {"feature_a": provisioned, "feature_b": False,
                                    "feature_c": True},
        "default_language": "ca", "app_origin": None, "ignored_keys": list(ignored),
    }
    snapshot = site_settings.merge_scope_snapshot(
        None, body, contract=contract,
        key_fingerprint=token.fingerprint if robot_id == token.robot_id else None,
        key_version=1, now=time.time() if fetched_at is None else fetched_at)
    site_settings.write_scope_snapshot(snapshot, root=root)


def test_the_probe_reports_this_processs_sources_and_scope(layer, monkeypatch):
    _snapshot(layer, ignored=("app_origin",), provisioned=True)
    monkeypatch.setattr(site_settings, "_last_sources",
                        {"EMORI_LANG": "env", "EMORI_COURSE_ID": "pokamind",
                         "lower_case": "env", "EMORI_SMTP_HOST": "nonsense"})

    site = SystemProbe().site_config("test-robot-01", GOLDEN_FINGERPRINT)

    assert site.probed and site.auto
    assert site.sources == (("EMORI_COURSE_ID", "pokamind"), ("EMORI_LANG", "env"))
    assert (site.scope_status, site.scope_contract, site.scope_partner) == ("ok", 2,
                                                                            "emori-robot")
    assert site.scope_ignored == ("app_origin",)
    # Only an installed add-on turns the sticky flag on.
    assert site.addon_provisioned is False
    assert site.scope_fetched_at is not None and abs(site.now - site.scope_fetched_at) < 60
    assert site.invalid == ()


def test_the_probe_names_whose_scope_it_found_and_never_reads_it_as_this_robots(layer):
    _snapshot(layer, robot_id="other-robot-02")

    site = SystemProbe().site_config("test-robot-01", GOLDEN_FINGERPRINT)

    assert (site.scope_status, site.scope_robot_id) == ("scope_robot_mismatch",
                                                        "other-robot-02")
    assert site.scope_fetched_at is None and site.scope_partner == ""


def test_the_probe_reports_a_missing_scope_and_asks_nothing_without_a_token(layer):
    probe = SystemProbe()

    assert probe.site_config("test-robot-01", GOLDEN_FINGERPRINT).scope_status == (
        "scope_missing")
    assert probe.site_config("", "").scope_status == ""


def test_the_probe_reads_the_kill_switch(layer):
    os.environ[site_settings.AUTO_ENV] = "0"

    assert SystemProbe().site_config("", "").auto is False


def test_a_refused_saved_value_is_named_never_quoted(layer):
    (layer / "configs" / "site").mkdir(parents=True)
    (layer / "configs/site/settings.env").write_text(
        "EMORI_SMTP_PORT=not-a-port\nEMORI_SMTP_HOST=smtp.example.org\n", encoding="utf-8")
    (layer / "secrets").mkdir()
    (layer / "secrets/admin.env").write_text("EMORI_ADMIN_PIN=12ab-not-a-pin\n",
                                             encoding="utf-8")
    (layer / "secrets/site.env").write_text("EMORI_SMTP_PASS=hunter2-smtp-pass\n",
                                            encoding="utf-8")

    site = SystemProbe().site_config("", "")
    gate = _gate(site=dataclasses.replace(_site(), invalid=site.invalid))

    assert site.invalid == (("EMORI_ADMIN_PIN", "value_out_of_range"),
                            ("EMORI_SMTP_PORT", "value_not_port"))
    assert gate.code == "site_settings_invalid"
    for secret in _SECRETS + ("not-a-port",):
        assert secret not in repr(site) and secret not in gate.hint


def test_inherited_derived_values_are_derived_again_never_read_as_dot_env(layer):
    """emori-admin's Health tab spawns readycheck with ITS environment: values the
    settings layer put there from Pokamind, marked as such. This process strips them
    and derives them again, so the matrix reports what the hostapp's next start would
    see -- not a stale course labelled as an explicit .env value."""

    _snapshot(layer, course="fresh-course")
    dotenv = layer / ".env"
    dotenv.write_text("EMORI_LANG=en\n", encoding="utf-8")
    os.environ.update({
        robot_token.TOKEN_ENV: GOLDEN_TOKEN,
        # What the parent's own layer derived (and marked): an OLD sync's course.
        site_settings.SOURCES_ENV: json.dumps({"EMORI_COURSE_ID": "pokamind"}),
        "EMORI_COURSE_ID": "stale-course",
        # Explicit in the parent (no marker entry): an explicit value, as before.
        "EMORI_LANG": "en",
    })

    site_settings.apply(os.environ, dotenv_file=str(dotenv))
    site = SystemProbe().site_config("test-robot-01", GOLDEN_FINGERPRINT)

    assert dict(site.sources)["EMORI_COURSE_ID"] == "pokamind"
    assert os.environ["EMORI_COURSE_ID"] == "fresh-course"
    assert dict(site.sources)["EMORI_LANG"] == "env"
    gate = _gate(site=dataclasses.replace(_site(), sources=site.sources))
    assert "EMORI_COURSE_ID=pokamind" in gate.hint and "EMORI_COURSE_ID=env" not in gate.hint


def test_an_inherited_value_nothing_derives_any_more_is_dropped_not_kept_as_env(layer):
    os.environ.update({
        robot_token.TOKEN_ENV: GOLDEN_TOKEN,
        site_settings.SOURCES_ENV: json.dumps({"EMORI_COURSE_ID": "pokamind"}),
        "EMORI_COURSE_ID": "stale-course",
    })  # ...and no scope on disk any more: nothing derives the course now

    site_settings.apply(os.environ, dotenv_file=None)
    site = SystemProbe().site_config("test-robot-01", GOLDEN_FINGERPRINT)

    assert "EMORI_COURSE_ID" not in dict(site.sources)
    assert "EMORI_COURSE_ID" not in os.environ
    assert collect(_FakeProbe(dict(os.environ))).course_id_env == "emori"


def test_no_secret_value_reaches_any_output(layer, monkeypatch, capsys):
    """Every value the layer read stays inside site_settings: the matrix and the JSON
    carry key names, layer names and codes -- a secret is only ever "set"."""

    (layer / "secrets").mkdir()
    (layer / "secrets/site.env").write_text("EMORI_SMTP_PASS=hunter2-smtp-pass\n",
                                            encoding="utf-8")
    (layer / "secrets/admin.env").write_text(
        "EMORI_DASH_PASSWORD=hunter2-dash-pass\nEMORI_ADMIN_PIN=12ab-not-a-pin\n",
        encoding="utf-8")
    site_settings.apply(os.environ, dotenv_file=None)
    site = SystemProbe().site_config("", "")
    assert {("EMORI_SMTP_PASS", "admin"), ("EMORI_DASH_PASSWORD", "admin")} <= set(
        site.sources)
    assert site.invalid == (("EMORI_ADMIN_PIN", "value_out_of_range"),)

    for evidence, named in ((_healthy(site=site), "EMORI_ADMIN_PIN (value_out_of_range)"),
                            (_healthy(site=dataclasses.replace(site, invalid=())),
                             "EMORI_SMTP_PASS=admin")):
        results = evaluate(evidence)
        text = format_matrix(results) + json.dumps([dataclasses.asdict(r) for r in results])
        assert named in text
        for secret in _SECRETS + ("hunter2",):
            assert secret not in text
    # The same through main(), which prints the matrix or the JSON.
    monkeypatch.setattr(readycheck, "collect", lambda probe: _healthy(site=site))
    readycheck.main(["--json", "--no-speak"])
    readycheck.main(["--no-speak"])
    out = capsys.readouterr().out
    assert "site_settings_invalid" in out and "EMORI_ADMIN_PIN" in out
    assert "hunter2" not in out and "12ab" not in out


def test_the_probe_never_raises(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("broken layer")

    monkeypatch.setattr(site_settings, "effective_sources", _boom)
    monkeypatch.setattr(site_settings, "check_stored_settings", _boom)
    monkeypatch.setattr(site_settings, "scope_snapshot_status", _boom)

    site = SystemProbe().site_config("test-robot-01", GOLDEN_FINGERPRINT)

    assert site.probed and site.sources == () and site.scope_status == "scope_unreadable"


def test_the_shipped_settings_files_and_docs_are_the_ones_the_hints_name():
    root = Path(__file__).resolve().parents[1]
    config_doc = (root / "docs/configuration.md").read_text(encoding="utf-8")
    guide = (root / readycheck._TOKEN_DOC).read_text(encoding="utf-8")

    assert "## Where each setting comes from" in config_doc
    assert "## Legacy" not in config_doc  # this release has no legacy credential
    assert readycheck._SETTINGS_DOC.endswith("#where-each-setting-comes-from")
    for code in ("site_config_ok", "pokamind_base_invalid", "site_settings_invalid",
                 "site_restart_pending", "scope_not_synced", "scope_stale",
                 "scope_robot_mismatch", "partner_mismatch", "voice_menu_placeholder_ids",
                 "voice_menu_auto_draft", "admin_pin_unset"):
        assert f"`{code}`" in guide, code


def test_the_auto_applied_reasons_are_the_hostapps_pokamind_reasons():
    from emori_adapter.hostapp import admin_api

    assert readycheck._AUTO_APPLIED_REASONS == {admin_api.REASON_SCOPE_CHANGED,
                                                "addon_provisioned"}
    assert set(admin_api.RESTART_REASONS) - readycheck._AUTO_APPLIED_REASONS == {
        admin_api.REASON_SETTINGS_SAVED}


def test_a_token_probe_that_is_not_configured_is_never_handed_to_the_layer():
    probe = _SiteProbe({}, load=robot_token.TokenLoad(source="file", error="seed_invalid"))

    evidence = collect(probe)

    assert probe.site_asked == [("", "")]
    assert evidence.token.state == "invalid"
