"""Results delivery: report rendering, recipient policy, outbox fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emori_adapter.hostapp.driver import SessionInfo
from emori_adapter.hostapp.results_email import ResultsMailer, render_report


def _stats(**kw) -> dict:
    base = dict(
        session=SessionInfo(session_id="anna-001-20260611-103000", person_id="anna-001",
                            name="Anna", email="anna@example.org", lang="en",
                            topic="Difficult Conversations", anonymous=False),
        avg_engagement=0.71, dominant_style="mirroring", max_escalation=0.42,
        cycles=24, utterances=6, lang="en",
        summary="That's the end of our practice. Your engagement averaged 71 percent...",
        results={"styles": {"mirroring": 18.2}, "partner": "emori-robot"},
    )
    base.update(kw)
    return base


def test_render_report_contains_the_essentials():
    body = render_report(_stats())
    assert "Anna" in body
    assert "71%" in body
    assert "mirroring" in body
    assert "Difficult Conversations" in body
    assert "Pokamind results" in body


def test_render_report_escapes_html():
    body = render_report(_stats(dominant_style="<script>alert(1)</script>"))
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_outbox_fallback_writes_html_and_meta(tmp_path):
    mailer = ResultsMailer(smtp_host=None, always_to=["trainer@example.org"],
                           outbox_dir=str(tmp_path))
    out = mailer.deliver(_stats())
    assert out["delivered"] is True and out["via"] == "outbox"
    html_file = tmp_path / "anna-001-20260611-103000.html"
    meta_file = tmp_path / "anna-001-20260611-103000.json"
    assert html_file.exists() and meta_file.exists()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["to"] == ["anna@example.org", "trainer@example.org"]
    assert "Anna" in meta["subject"]


# --- what the dashboard backfill has to be able to read back ------------------------
# scripts/dashboard_backfill.py rebuilds session rows from these files. Without the
# scores in them every backfilled session imports with NULL performance_scores --
# the "dashboard shows zeros" report.

def test_outbox_meta_carries_the_scores_the_backfill_needs(tmp_path):
    mailer = ResultsMailer(smtp_host=None, outbox_dir=str(tmp_path))
    scores = {"communication_clarity": 80, "adaptability": 55}
    mailer.deliver(_stats(results={"performance_scores": scores, "is_roleplay": True,
                                   "session_analysis": "Anna hesitated at the door.",
                                   "transcript": [{"role": "user", "text": "hello"}]}))

    meta = json.loads((tmp_path / "anna-001-20260611-103000.json").read_text(
        encoding="utf-8"))

    assert meta["performance_scores"] == scores
    assert meta["is_roleplay"] is True
    # Numbers and a flag only: the transcript and the analysis text stay out, and
    # nothing about the person enters the file that was not already in it.
    assert "transcript" not in meta
    assert "session_analysis" not in meta
    assert "hesitated" not in json.dumps(meta, ensure_ascii=False)


def test_a_session_with_no_scores_writes_no_key_rather_than_a_null(tmp_path):
    """An absent key and a null are the same to the importer -- but a fabricated
    ``{}`` would read as "scored, every competency missing"."""

    mailer = ResultsMailer(smtp_host=None, outbox_dir=str(tmp_path))
    mailer.deliver(_stats(results={"performance_scores": {}, "summary": "x"}))

    meta = json.loads((tmp_path / "anna-001-20260611-103000.json").read_text(
        encoding="utf-8"))

    assert "performance_scores" not in meta
    assert "is_roleplay" not in meta

    # A results payload that is not a dict at all must not break delivery either.
    assert mailer.deliver(_stats(results=None))["delivered"] is True


# --- the subject is in the language of the person the report is for. It follows
# the same ca/en pattern as driver._SUMMARY_TEXT,
# keyed off the session's language (SessionTracker.finish() stamps stats["lang"]).

def test_subject_line_is_localized_to_the_session_language(tmp_path):
    mailer = ResultsMailer(smtp_host=None, outbox_dir=str(tmp_path))
    mailer.deliver(_stats(lang="ca", session=SessionInfo(
        session_id="anna-001-20260611-103000", person_id="anna-001",
        name="Anna", email="anna@example.org", lang="ca",
        topic="Difficult Conversations", anonymous=False)))

    meta = json.loads(
        (tmp_path / "anna-001-20260611-103000.json").read_text(encoding="utf-8"))
    assert meta["subject"] == "Informe de la sessió EMORI — Anna — anna-001-20260611-103000"


def test_subject_line_falls_back_to_english_for_an_unsupported_lang(tmp_path):
    mailer = ResultsMailer(smtp_host=None, outbox_dir=str(tmp_path))
    mailer.deliver(_stats(lang="fr"))

    meta = json.loads(
        (tmp_path / "anna-001-20260611-103000.json").read_text(encoding="utf-8"))
    assert meta["subject"] == "EMORI session report — Anna — anna-001-20260611-103000"


def test_subject_line_defaults_to_english_when_lang_is_absent_from_stats(tmp_path):
    """Not every on_session_end producer is guaranteed to stamp "lang" -- must not
    crash, and must read exactly like an explicit "en"."""
    mailer = ResultsMailer(smtp_host=None, outbox_dir=str(tmp_path))
    stats = _stats()
    del stats["lang"]

    mailer.deliver(stats)

    meta = json.loads(
        (tmp_path / "anna-001-20260611-103000.json").read_text(encoding="utf-8"))
    assert meta["subject"] == "EMORI session report — Anna — anna-001-20260611-103000"


def test_anonymous_sessions_never_email_the_placeholder(tmp_path):
    mailer = ResultsMailer(smtp_host=None, always_to=["trainer@example.org"],
                           outbox_dir=str(tmp_path))
    stats = _stats(session=SessionInfo(session_id="s", person_id="robot-anonymous",
                                       email="robot-anonymous@emori.local", anonymous=True))
    assert mailer.recipients(stats) == ["trainer@example.org"]


def test_empty_session_not_delivered(tmp_path):
    mailer = ResultsMailer(outbox_dir=str(tmp_path))
    out = mailer.deliver(_stats(cycles=0))
    assert out["delivered"] is False
    assert list(tmp_path.iterdir()) == []


# ----- what the JOURNAL learns about a delivery -----
#
# Root logging reaches journald: these lines are WRITTEN on the appliance, to a
# file that anyone holding the box can read and that survives reboots.  One
# completed session per staff member, each carrying their
# work mailbox and the SSO student id `_mint_session_id` prefixes onto every
# session id.  Delivery is still fully diagnosable -- from pseudonyms.

class _FakeSmtp:
    """Enough of smtplib.SMTP to exercise the send branch."""

    contexts: list = []

    def __init__(self, host, port, timeout=15, fail=None, context=None):
        self.host, self.port, self.fail = host, port, fail
        if context is not None:      # smtplib.SMTP_SSL: TLS from the first byte
            _FakeSmtp.contexts.append(("ssl", context))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        _FakeSmtp.contexts.append(("starttls", context))

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if self.fail is not None:
            raise self.fail


def test_a_sent_report_is_logged_as_a_count_and_a_person_code(monkeypatch, caplog):
    import logging
    import smtplib

    from emori_adapter.hostapp import metrics_store

    monkeypatch.setenv("EMORI_DASH_SALT", "test-salt")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    mailer = ResultsMailer(smtp_host="smtp.example.org", sender="robot@example.org",
                           always_to=["trainer@example.net"])
    caplog.set_level(logging.INFO, logger="emori.hostapp.email")

    out = mailer.deliver(_stats())

    assert out["delivered"] is True and out["via"] == "smtp"
    # The MAIL still goes to the participant -- only the journal is redacted.
    assert out["to"] == ["anna@example.org", "trainer@example.net"]
    logged = "\n".join(caplog.messages)
    assert "anna@example.org" not in logged and "trainer@example.net" not in logged
    assert "2 recipient(s)" in logged                       # delivery happened, to how many
    assert metrics_store.person_code("anna-001")[0] in logged   # ...and for which learner
    assert "'example.org'" in logged and "'example.net'" in logged  # participant vs trainer


def test_a_refused_recipient_is_logged_as_a_code_not_an_address(monkeypatch, tmp_path,
                                                                caplog):
    """smtplib puts the payload in the message: SMTPRecipientsRefused's SOLE arg
    is the {address: (code, reason)} dict, so str(e) is every failed address
    verbatim -- printed exactly when an operator is reading the journal."""

    import logging
    import smtplib

    monkeypatch.setattr(
        smtplib, "SMTP",
        lambda *a, **kw: _FakeSmtp(*a, **kw, fail=smtplib.SMTPRecipientsRefused(
            {"anna@example.org": (550, b"5.1.1 User unknown")})))
    mailer = ResultsMailer(smtp_host="smtp.example.org", sender="robot@example.org",
                           outbox_dir=str(tmp_path))
    caplog.set_level(logging.WARNING, logger="emori.hostapp.email")

    out = mailer.deliver(_stats())

    assert out["via"] == "outbox"            # still falls through, still delivered
    logged = "\n".join(caplog.messages)
    assert "anna@example.org" not in logged
    assert "SMTPRecipientsRefused" in logged  # the actionable half: the address is wrong
    assert "code=550" in logged


def test_the_outbox_line_names_a_glob_not_the_student_id(tmp_path, caplog):
    """The FILE keeps the minted id (it is the report, on the same card as the
    roster); the journal gets a scrubbed id an operator can still `ls` with."""

    import logging

    mailer = ResultsMailer(smtp_host=None, outbox_dir=str(tmp_path))
    caplog.set_level(logging.INFO, logger="emori.hostapp.email")

    mailer.deliver(_stats())

    logged = "\n".join(caplog.messages)
    assert "anna-001" not in logged
    assert "*20260611-103000.html" in logged
    # ...and the glob it printed actually finds the file it is about.
    assert list(tmp_path.glob("*20260611-103000.html")) == [
        tmp_path / "anna-001-20260611-103000.html"]


def test_a_failed_outbox_write_reports_the_errno_not_the_path(monkeypatch, tmp_path,
                                                              caplog):
    """OSError.__str__ appends `: '<filename>'`, and that basename is the student
    id.  The subclass plus the errno NUMBER is the diagnosis instead: ENOSPC(28)
    and EROFS(30) are both a bare OSError."""

    import logging
    import os as os_module

    from emori_adapter.hostapp import results_email

    def full_card(path, *a, **kw):
        raise OSError(28, "No space left on device",
                      str(tmp_path / "anna-001-20260611-103000.html"))

    monkeypatch.setattr(results_email, "open", full_card, raising=False)
    monkeypatch.setattr(os_module, "makedirs", lambda *a, **kw: None)
    mailer = ResultsMailer(smtp_host=None, outbox_dir=str(tmp_path))
    caplog.set_level(logging.WARNING, logger="emori.hostapp.email")

    out = mailer.deliver(_stats())

    assert out["delivered"] is False
    logged = "\n".join(caplog.messages)
    assert "anna-001" not in logged
    assert "errno=28" in logged                  # full card, not read-only, not permission
    assert "for 20260611-103000" in logged       # ...and for which session


def test_completion_flow_delivers_via_hook(monkeypatch, tmp_path):
    """End-to-end through /api/cycle: completion triggers the delivery hook."""
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient
    from tests.test_live_contract import live_envelope

    monkeypatch.setattr(server, "DEMO", True)
    delivered = []
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "fiware_factory": None, "idle": False,
        "tts": None, "stt": None,
        "recorder": type("NullRec", (), {"write": staticmethod(lambda *a, **k: None)})(),
        "on_session_end": delivered.append,
    })
    with TestClient(app) as c:
        d = c.post("/api/start", json={"student_id": "anna-001"}).json()
        c.get(d["session_url"])
        for i in range(3):
            c.post("/api/cycle", json=live_envelope(seq=i))
        c.post("/api/cycle", json={"type": "pokamind_session_completed",
                                   "status": "completed", "session_id": "x",
                                   "partner": "demo", "results": {"k": 1}})
    assert len(delivered) == 1
    stats = delivered[0]
    assert stats["cycles"] == 3
    assert stats["results"] == {"k": 1}
    assert stats["session"].person_id == "anna-001"


# --- the sender: no built-in address ------------------------------------------------
# A public tree must not invent a From address on somebody else's domain: the sender is
# EMORI_SMTP_FROM, else an e-mail-shaped EMORI_SMTP_USER, else nothing is sent.

_SMTP_ENV = ("EMORI_SMTP_HOST", "EMORI_SMTP_PORT", "EMORI_SMTP_USER", "EMORI_SMTP_PASS",
             "EMORI_SMTP_FROM", "EMORI_RESULTS_TO", "EMORI_SMTP_TLS_VERIFY")


@pytest.fixture()
def smtp_env(monkeypatch):
    for name in _SMTP_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_there_is_no_branded_default_sender(smtp_env):
    from emori_adapter.hostapp import results_email

    assert ResultsMailer().sender == ""
    assert ResultsMailer.from_env().sender == ""
    source = Path(results_email.__file__).read_text(encoding="utf-8")
    assert "@pokamind.com" not in source


def test_the_sender_is_smtp_from_else_an_email_shaped_user(smtp_env):
    from emori_adapter.hostapp.results_email import sender_from

    smtp_env.setenv("EMORI_SMTP_USER", "mailer@example.org")
    assert ResultsMailer.from_env().sender == "mailer@example.org"
    smtp_env.setenv("EMORI_SMTP_FROM", "robot@example.org")
    assert ResultsMailer.from_env().sender == "robot@example.org"
    # A login name that is not an address is never used as one.
    assert sender_from({"EMORI_SMTP_USER": "apikey"}) == ""
    assert sender_from({"EMORI_SMTP_USER": "  "}) == ""


def test_a_host_without_a_sender_drops_the_report_to_the_outbox(smtp_env, tmp_path,
                                                                caplog):
    import logging
    import smtplib

    attempts = []
    smtp_env.setattr(smtplib, "SMTP", lambda *a, **kw: attempts.append(a) or _FakeSmtp(*a))
    smtp_env.setenv("EMORI_SMTP_HOST", "smtp.example.org")
    smtp_env.setenv("EMORI_SMTP_USER", "apikey")
    smtp_env.setenv("EMORI_RESULTS_TO", "trainer@example.org")
    mailer = ResultsMailer.from_env()
    mailer.outbox_dir = str(tmp_path)
    caplog.set_level(logging.WARNING, logger="emori.hostapp.email")

    out = mailer.deliver(_stats())

    assert attempts == []                    # no connection at all
    assert out["delivered"] is True and out["via"] == "outbox"
    assert "smtp_from_unset" in "\n".join(caplog.messages)
    meta = json.loads((tmp_path / "anna-001-20260611-103000.json").read_text(
        encoding="utf-8"))
    assert meta["from"] == ""


def test_an_unparseable_port_falls_back_to_587(smtp_env):
    smtp_env.setenv("EMORI_SMTP_PORT", "five-eight-seven")
    assert ResultsMailer.from_env().smtp_port == 587
    smtp_env.setenv("EMORI_SMTP_PORT", "2525")
    assert ResultsMailer.from_env().smtp_port == 2525


# --- TLS: the mail server's certificate is checked unless explicitly opted out ------


def _sent_over(smtp_env, port: int, verify: str | None) -> tuple:
    import smtplib

    _FakeSmtp.contexts = []
    smtp_env.setattr(smtplib, "SMTP", _FakeSmtp)
    smtp_env.setattr(smtplib, "SMTP_SSL", _FakeSmtp)
    smtp_env.setenv("EMORI_SMTP_HOST", "smtp.example.org")
    smtp_env.setenv("EMORI_SMTP_PORT", str(port))
    smtp_env.setenv("EMORI_SMTP_FROM", "robot@example.org")
    if verify is not None:
        smtp_env.setenv("EMORI_SMTP_TLS_VERIFY", verify)
    assert ResultsMailer.from_env().send_test("trainer@example.org") == (True, "ok")
    assert len(_FakeSmtp.contexts) == 1
    return _FakeSmtp.contexts[0]


@pytest.mark.parametrize("verify", [None, "", "1", "false", "no"])
def test_starttls_verifies_the_certificate_and_the_host_by_default(smtp_env, verify):
    import ssl

    how, context = _sent_over(smtp_env, 587, verify)

    assert how == "starttls"
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True


def test_port_465_is_tls_from_the_first_byte_and_verified_too(smtp_env):
    import ssl

    how, context = _sent_over(smtp_env, 465, None)

    assert how == "ssl"  # no STARTTLS on an SMTPS port
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True


def test_only_an_explicit_zero_turns_verification_off_and_it_is_logged(smtp_env, caplog):
    import logging
    import ssl

    with caplog.at_level(logging.WARNING, logger="emori.hostapp.email"):
        how, context = _sent_over(smtp_env, 587, "0")

    assert how == "starttls"
    assert context.verify_mode == ssl.CERT_NONE and context.check_hostname is False
    assert "EMORI_SMTP_TLS_VERIFY=0" in caplog.text
    from emori_adapter.hostapp.results_email import tls_verify

    assert tls_verify({"EMORI_SMTP_TLS_VERIFY": " 0 "}) is False
    assert tls_verify({}) is True and tls_verify({"EMORI_SMTP_TLS_VERIFY": "off"}) is True


def test_the_opt_out_is_a_validated_site_setting_behind_the_pin():
    from emori_adapter import site_settings
    from emori_adapter.admin import server as admin_server

    assert site_settings.validate("EMORI_SMTP_TLS_VERIFY", "0") is None
    assert site_settings.validate("EMORI_SMTP_TLS_VERIFY", "1") is None
    assert site_settings.validate("EMORI_SMTP_TLS_VERIFY", "false") == "value_not_bool"
    spec = site_settings.REGISTRY["EMORI_SMTP_TLS_VERIFY"]
    assert (spec.owner, spec.secret, spec.file) == ("hostapp", False,
                                                    site_settings.SETTINGS_FILE)
    # Turning it off decides whether the stored password goes to an unchecked server.
    assert "EMORI_SMTP_TLS_VERIFY" in admin_server.PIN_SETTINGS


# --- the admin page's "Send test e-mail" -------------------------------------------


class _RecordingSmtp(_FakeSmtp):
    sent: list = []

    def send_message(self, msg):
        super().send_message(msg)
        _RecordingSmtp.sent.append(msg)


def test_send_test_sends_a_fixed_message_with_no_learner_data(monkeypatch, caplog):
    import logging
    import smtplib

    from emori_adapter.hostapp.results_email import send_test

    _RecordingSmtp.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _RecordingSmtp)
    caplog.set_level(logging.INFO, logger="emori.hostapp.email")

    sent, code = send_test("trainer@example.org",
                           environ={"EMORI_SMTP_HOST": "smtp.example.org",
                                    "EMORI_SMTP_FROM": "robot@example.org"})

    assert (sent, code) == (True, "ok")
    (msg,) = _RecordingSmtp.sent
    assert msg["To"] == "trainer@example.org" and msg["From"] == "robot@example.org"
    assert msg["Subject"] == "EMORI test e-mail"
    assert "trainer@example.org" not in "\n".join(caplog.messages)


@pytest.mark.parametrize("to, environ, code", [
    ("not-an-address", {"EMORI_SMTP_HOST": "smtp.example.org",
                        "EMORI_SMTP_FROM": "robot@example.org"}, "recipient_invalid"),
    (None, {"EMORI_SMTP_HOST": "smtp.example.org"}, "recipient_invalid"),
    ("trainer@example.org", {}, "smtp_host_unset"),
    ("trainer@example.org", {"EMORI_SMTP_HOST": "smtp.example.org",
                             "EMORI_SMTP_USER": "apikey"}, "smtp_from_unset"),
])
def test_send_test_refuses_with_a_code(monkeypatch, to, environ, code):
    import smtplib

    from emori_adapter.hostapp.results_email import send_test

    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **kw: pytest.fail("no connection"))

    assert send_test(to, environ=environ) == (False, code)


@pytest.mark.parametrize("failure, code", [
    ("auth", "smtp_auth_failed"),
    ("refused", "smtp_recipient_refused"),
    ("sender", "smtp_sender_refused"),
    ("smtp", "smtp_failed"),
    ("network", "smtp_unreachable"),
])
def test_send_test_maps_a_failure_to_a_code_never_an_address(monkeypatch, caplog,
                                                             failure, code):
    import logging
    import smtplib

    from emori_adapter.hostapp.results_email import send_test

    error = {
        "auth": smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials for mailer"),
        "refused": smtplib.SMTPRecipientsRefused(
            {"trainer@example.org": (550, b"5.1.1 User unknown")}),
        "sender": smtplib.SMTPSenderRefused(553, b"no", "robot@example.org"),
        "smtp": smtplib.SMTPDataError(554, b"rejected"),
        "network": ConnectionRefusedError(111, "Connection refused"),
    }[failure]
    if failure == "network":
        def connect(*a, **kw):
            raise error
        monkeypatch.setattr(smtplib, "SMTP", connect)
    else:
        monkeypatch.setattr(smtplib, "SMTP", lambda *a, **kw: _FakeSmtp(*a, fail=error))
    caplog.set_level(logging.WARNING, logger="emori.hostapp.email")

    result = send_test("trainer@example.org",
                       environ={"EMORI_SMTP_HOST": "smtp.example.org",
                                "EMORI_SMTP_FROM": "robot@example.org"})

    assert result == (False, code)
    logged = "\n".join(caplog.messages)
    assert "trainer@example.org" not in logged and "robot@example.org" not in logged
    assert "bad credentials" not in logged
