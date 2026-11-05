"""End-of-session results delivery: render an HTML report and send it.

'EMORI emails these' (the Pokamind completion message carries the full
``results: analysisData``): this module renders the report from the completion
stats + Pokamind results and

* sends it via SMTP when ``EMORI_SMTP_HOST`` is configured (the deployed robot), or
* writes it to ``artifacts/outbox/<session>.html`` (+ ``.json``) otherwise (the sim) —
  the rendering/delivery path is exercised either way, and the outbox file is the
  exact HTML the email would contain.

Recipients: the session's roster email (badge identity) plus ``EMORI_RESULTS_TO``
(the trainer/team inbox). Anonymous sessions go to the trainer inbox only.

The sender is ``EMORI_SMTP_FROM``, else ``EMORI_SMTP_USER`` when that is an e-mail
address. There is no built-in sender: with ``EMORI_SMTP_HOST`` set and neither of them
usable, nothing is sent and the report goes to the outbox (journal code
``smtp_from_unset``; readycheck warns). :func:`send_test` sends a fixed test message
with no learner data, for the admin page's "Send test e-mail".

Transport: implicit TLS on port 465, STARTTLS on every other port, and the server's
certificate is VERIFIED either way (``ssl.create_default_context()``: the system's
trust store and the host name). ``EMORI_SMTP_TLS_VERIFY=0`` is the explicit opt-out
for a server with a self-signed certificate; it is logged as a WARNING on every send,
because the SMTP password then goes to whoever answers.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Mapping, Optional

from emori_adapter import site_settings
from emori_adapter.hostapp import metrics_store

log = logging.getLogger("emori.hostapp.email")

OUTBOX_DIR = os.environ.get("EMORI_OUTBOX", "artifacts/outbox")


def render_report(stats: dict) -> str:
    """Render the session report HTML from the completion stats (driver.on_completed)."""
    info = stats.get("session")
    name = html.escape(getattr(info, "name", "") or "Guest")
    topic = html.escape(getattr(info, "topic", "") or "training session")
    session_id = html.escape(getattr(info, "session_id", "") or "unknown")
    avg = round(float(stats.get("avg_engagement") or 0.0) * 100)
    dominant = html.escape(str(stats.get("dominant_style") or "neutral"))
    esc = float(stats.get("max_escalation") or 0.0)
    summary = html.escape(str(stats.get("summary") or ""))
    cycles = int(stats.get("cycles") or 0)

    rows = ""
    results = stats.get("results")
    if isinstance(results, dict):
        for key, value in sorted(results.items()):
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)[:400]
            rows += (f"<tr><td style='padding:4px 12px;color:#555'>{html.escape(str(key))}</td>"
                     f"<td style='padding:4px 12px'>{html.escape(str(value))}</td></tr>")
    results_table = (f"<h3 style='margin:18px 0 6px'>Pokamind results</h3>"
                     f"<table style='border-collapse:collapse;font-size:13px'>{rows}</table>"
                     if rows else "")

    return f"""<!doctype html><html><body style="font-family:system-ui,sans-serif;color:#222">
<div style="max-width:640px;margin:0 auto;padding:24px">
<h2 style="margin:0 0 4px">EMORI practice session report</h2>
<p style="color:#666;margin:0 0 16px">{name} &middot; {topic} &middot; {session_id}</p>
<table style="border-collapse:collapse;font-size:14px">
<tr><td style="padding:4px 12px;color:#555">Average engagement</td><td style="padding:4px 12px"><b>{avg}%</b></td></tr>
<tr><td style="padding:4px 12px;color:#555">Dominant communication style</td><td style="padding:4px 12px"><b>{dominant}</b></td></tr>
<tr><td style="padding:4px 12px;color:#555">Peak escalation</td><td style="padding:4px 12px">{esc:.2f}</td></tr>
<tr><td style="padding:4px 12px;color:#555">Analysis cycles</td><td style="padding:4px 12px">{cycles}</td></tr>
</table>
<h3 style="margin:18px 0 6px">What the robot said</h3>
<p style="background:#f4f5fa;padding:12px 16px;border-radius:8px">{summary}</p>
{results_table}
<p style="color:#999;font-size:12px;margin-top:24px">EMORI &mdash; Embodied Multimodal Observation and Response Interface.
Full time-series on the trainer dashboard (Grafana &rarr; EMORI Session).</p>
</div></body></html>"""


# Same ca/en pattern as driver._SUMMARY_TEXT: the subject is the one part of the
# email a non-English-reading recipient sees before opening it, so it follows the
# session's language (SessionTracker.finish() stamps ``stats["lang"]``) rather than
# always reading English regardless of who the report is for.
_SUBJECT_TEXT = {
    "en": "EMORI session report — {name} — {session_id}",
    "ca": "Informe de la sessió EMORI — {name} — {session_id}",
}


def _domain_of(address: str) -> str:
    """The mail domain, or a shape word -- never the address itself.

    ``addr.rsplit("@", 1)[-1]`` looks like a domain extractor and is one only
    when there IS an ``@``: on a malformed roster entry it returns the whole
    string, which is precisely the identifying local-part this line exists to
    keep out of the journal.  A recipient list is operator-supplied (roster
    ``email`` fields and EMORI_RESULTS_BCC), so "malformed" is a typo away.
    """
    _, sep, domain = address.rpartition("@")
    if not sep:
        return "(no-domain)"
    return domain.strip().lower() or "(no-domain)"


def _smtp_failure_code(e: BaseException) -> str:
    """The numeric SMTP status of a send failure, and nothing else from it.

    ``SMTPResponseException`` and friends carry ``smtp_code`` directly.
    ``SMTPRecipientsRefused`` does NOT -- its sole argument IS the
    ``{address: (code, reason)}`` dict, which is why ``str(e)`` renders every
    refused mailbox verbatim -- so the codes are lifted out of it and the
    addresses (its keys) are left where they are.
    """

    code = getattr(e, "smtp_code", None)
    if isinstance(code, int):
        return str(code)
    refused = getattr(e, "recipients", None)
    if isinstance(refused, dict):
        codes = sorted({reply[0] for reply in refused.values()
                        if isinstance(reply, tuple) and reply
                        and isinstance(reply[0], int)})
        if codes:
            return "/".join(str(c) for c in codes)
    return "-"


def _outcome_meta(results) -> dict:
    """The outbox's machine-readable outcome fields, or {} when there are none.

    Only what the dashboard stores about an outcome: the competency scores (a
    ``{competency: 0..100}`` mapping) and whether Pokamind called the module a
    roleplay.  Absent keys rather than nulls, so an old outbox file and a new one
    that simply had no scores are the same thing to the importer -- and so this
    can never write a fabricated 0 into a KPI.  The scores go through
    ``metrics_store``'s own filter, which is also what the live path stores.
    """

    if not isinstance(results, dict):
        return {}
    out: dict = {}
    scores = results.get("performance_scores")
    if metrics_store.scores_json(scores) is not None:
        out["performance_scores"] = scores
    flag = metrics_store.roleplay_flag(results.get("is_roleplay"))
    if flag is not None:
        out["is_roleplay"] = bool(flag)
    return out


def is_email(value: object) -> bool:
    """One e-mail address, by the same rule the admin page validates with."""

    return (isinstance(value, str) and bool(value.strip())
            and site_settings.validate("EMORI_SMTP_FROM", value) is None)


def sender_from(environ: Optional[Mapping[str, str]] = None) -> str:
    """The From address: ``EMORI_SMTP_FROM``, else ``EMORI_SMTP_USER`` when it is an
    e-mail address, else ``""`` (nothing is sent: the report goes to the outbox)."""

    env = os.environ if environ is None else environ
    explicit = (env.get("EMORI_SMTP_FROM") or "").strip()
    if explicit:
        return explicit
    user = (env.get("EMORI_SMTP_USER") or "").strip()
    return user if is_email(user) else ""


def _port(environ: Mapping[str, str]) -> int:
    raw = (environ.get("EMORI_SMTP_PORT") or "").strip() or "587"
    try:
        port = int(raw)
    except ValueError:
        log.warning("EMORI_SMTP_PORT is not a number; using 587")
        return 587
    return port if 1 <= port <= 65535 else 587


# Implicit TLS (SMTPS): the one port where the connection is TLS from the first byte.
SMTPS_PORT = 465


def tls_verify(environ: Optional[Mapping[str, str]] = None) -> bool:
    """``EMORI_SMTP_TLS_VERIFY``: False only for the exact value ``0``.

    Anything else -- unset, empty, ``1``, a typo -- verifies: a misspelt opt-out must
    never be the reason a password goes to an unverified server.
    """

    env = os.environ if environ is None else environ
    return (env.get("EMORI_SMTP_TLS_VERIFY") or "").strip() != "0"


def tls_context(verify: bool = True) -> ssl.SSLContext:
    """The TLS context for the SMTP connection: certificate and host name checked,
    unless ``verify`` is False (``EMORI_SMTP_TLS_VERIFY=0``)."""

    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


# The admin page's "Send test e-mail": a fixed text, no learner data, no report.
_TEST_SUBJECT = "EMORI test e-mail"
_TEST_BODY = ("This is a test message from the EMORI robot's admin page.\n"
              "If you can read it, the robot can send session reports by e-mail.\n")


class ResultsMailer:
    """Send (SMTP) or drop (outbox) the report. Construct once per app."""

    def __init__(
        self,
        *,
        smtp_host: Optional[str] = None,
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_pass: str = "",
        sender: str = "",
        always_to: Optional[list[str]] = None,
        outbox_dir: str = OUTBOX_DIR,
        verify_tls: bool = True,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.sender = sender
        self.always_to = always_to or []
        self.outbox_dir = outbox_dir
        self.verify_tls = bool(verify_tls)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "ResultsMailer":
        """From the process environment, or from ``environ`` (the admin page tests the
        settings a restart would use, before they are applied)."""

        env = os.environ if environ is None else environ
        to = [a.strip() for a in (env.get("EMORI_RESULTS_TO") or "").split(",") if a.strip()]
        return cls(
            smtp_host=(env.get("EMORI_SMTP_HOST") or "").strip() or None,
            smtp_port=_port(env),
            smtp_user=env.get("EMORI_SMTP_USER", "") or "",
            smtp_pass=env.get("EMORI_SMTP_PASS", "") or "",
            sender=sender_from(env),
            always_to=to,
            verify_tls=tls_verify(env),
        )

    def _send(self, msg: EmailMessage) -> None:
        if not self.verify_tls:
            log.warning("EMORI_SMTP_TLS_VERIFY=0: the mail server's TLS certificate is "
                        "NOT checked, so the SMTP password goes to whoever answers")
        context = tls_context(self.verify_tls)
        if self.smtp_port == SMTPS_PORT:
            connection = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15,
                                          context=context)
        else:
            connection = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
        with connection as smtp:
            if self.smtp_port != SMTPS_PORT:
                smtp.starttls(context=context)
            if self.smtp_user:
                smtp.login(self.smtp_user, self.smtp_pass)
            smtp.send_message(msg)

    def send_test(self, to: object) -> tuple[bool, str]:
        """Send the fixed test message to ``to``: ``(sent, code)``. Never raises.

        Codes: ``ok``, ``recipient_invalid``, ``smtp_host_unset``, ``smtp_from_unset``,
        ``smtp_auth_failed``, ``smtp_recipient_refused``, ``smtp_sender_refused``,
        ``smtp_unreachable``, ``smtp_failed``. Logged without any address.
        """

        if not is_email(to):
            return False, "recipient_invalid"
        if not self.smtp_host:
            return False, "smtp_host_unset"
        if not self.sender:
            log.warning("smtp_from_unset: set EMORI_SMTP_FROM (or an e-mail address as "
                        "EMORI_SMTP_USER); no test e-mail sent")
            return False, "smtp_from_unset"
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = str(to).strip()
        msg["Subject"] = _TEST_SUBJECT
        msg.set_content(_TEST_BODY)
        try:
            self._send(msg)
        except smtplib.SMTPAuthenticationError as e:
            return self._test_failed("smtp_auth_failed", e)
        except smtplib.SMTPRecipientsRefused as e:
            return self._test_failed("smtp_recipient_refused", e)
        except smtplib.SMTPSenderRefused as e:
            return self._test_failed("smtp_sender_refused", e)
        except smtplib.SMTPException as e:
            return self._test_failed("smtp_failed", e)
        except OSError as e:  # DNS, a refused connection, a timeout, TLS
            log.warning("test e-mail not sent (smtp_unreachable %s errno=%s)",
                        type(e).__name__, getattr(e, "errno", "-"))
            return False, "smtp_unreachable"
        except Exception as e:  # noqa: BLE001 - the admin page gets a code, not a trace
            return self._test_failed("smtp_failed", e)
        log.info("test e-mail sent to 1 recipient %s", [_domain_of(str(to))])
        return True, "ok"

    @staticmethod
    def _test_failed(code: str, e: BaseException) -> tuple[bool, str]:
        # The exception TYPE and the SMTP status only: smtplib puts addresses and the
        # server's reply to a LOGIN in the message (see deliver()).
        log.warning("test e-mail not sent (%s %s code=%s)", code, type(e).__name__,
                    _smtp_failure_code(e))
        return False, code

    def recipients(self, stats: dict) -> list[str]:
        out = list(self.always_to)
        info = stats.get("session")
        email = getattr(info, "email", "") or ""
        anonymous = bool(getattr(info, "anonymous", True))
        # Anonymous drop-ins have placeholder addresses — never email those.
        if email and not anonymous and not email.endswith("@emori.local"):
            out.insert(0, email)
        return out

    def deliver(self, stats: dict) -> dict:
        """Render + send (or drop to the outbox). Returns what happened."""
        if not stats.get("cycles"):
            return {"delivered": False, "reason": "empty session"}
        body = render_report(stats)
        to = self.recipients(stats)
        info = stats.get("session")
        session_id = getattr(info, "session_id", "") or "session"
        # What the JOURNAL is allowed to know about this delivery.  A robot's
        # journal is world-readable to anyone holding the box and survives
        # reboots, so a completed session must not write down a staff
        # member's mailbox or the SSO student id `_mint_session_id` prefixes
        # onto every session id.  The same two pseudonyms the dashboard uses:
        # `S-xxxxxx` joins straight to its row, and the scrubbed session id
        # still globs the outbox file.
        person = metrics_store.person_code(getattr(info, "person_id", "") or "")[0]
        safe_session = metrics_store.deidentify_session_id(session_id)
        lang = str(stats.get("lang") or "en")
        lang = lang if lang in _SUBJECT_TEXT else "en"
        subject = _SUBJECT_TEXT[lang].format(
            name=getattr(info, "name", "") or "Guest", session_id=session_id)

        if self.smtp_host and to and not self.sender:
            # No built-in sender: a public tree must not invent a From address on
            # somebody else's domain. The report still lands in the outbox.
            log.warning("smtp_from_unset: EMORI_SMTP_HOST is set but there is no sender "
                        "(EMORI_SMTP_FROM, or an e-mail address as EMORI_SMTP_USER); "
                        "the report goes to the outbox")
        elif self.smtp_host and to:
            try:
                msg = EmailMessage()
                msg["From"] = self.sender
                msg["To"] = ", ".join(to)
                msg["Subject"] = subject
                msg.set_content("Your EMORI session report is attached (HTML).")
                msg.add_alternative(body, subtype="html")
                self._send(msg)
                # "It was delivered, to how many, for whom" -- never to WHICH
                # address.  Domains only when they differ, which is the one
                # distinction an operator asks for (participant vs the trainer
                # inbox) and the only one that is not a person.
                log.info("results emailed for person=%s to %d recipient(s) %s",
                         person, len(to), sorted({_domain_of(addr) for addr in to}))
                return {"delivered": True, "via": "smtp", "to": to}
            except Exception as e:  # noqa: BLE001 - email failure never breaks the kiosk
                # The exception TYPE and the SMTP code, never str(e): smtplib
                # puts the payload in the message.  SMTPRecipientsRefused's sole
                # arg IS the {address: (code, reason)} dict, SMTPSenderRefused
                # carries .sender, and SMTPAuthenticationError carries the
                # server's reply to a LOGIN.  "SMTPRecipientsRefused code=550"
                # is the whole actionable diagnosis ("that address is wrong")
                # without writing the address into the journal -- the same
                # slug-from-type discipline as admin/probes._error_slug.
                log.warning("SMTP send failed (%s code=%s); dropping to outbox",
                            type(e).__name__, _smtp_failure_code(e))

        # Sim / no SMTP: write the exact would-be email to the outbox.
        try:
            os.makedirs(self.outbox_dir, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
            html_path = os.path.join(self.outbox_dir, f"{safe}.html")
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(body)
            meta = {"subject": subject, "to": to, "from": self.sender,
                    "avg_engagement": stats.get("avg_engagement"),
                    "dominant_style": stats.get("dominant_style"),
                    "summary": stats.get("summary")}
            # The two fields the dashboard backfill cannot reconstruct from
            # anything else on the card.  `scripts/dashboard_backfill.py` rebuilds
            # session rows from these files, and without the scores every
            # backfilled session imports with NULL performance_scores -- which is
            # exactly the "the dashboard shows zeros" report.  Numbers and a flag
            # only: no transcript, no analysis text, and nothing about the person
            # that the file does not already carry.
            meta.update(_outcome_meta(stats.get("results")))
            with open(os.path.join(self.outbox_dir, f"{safe}.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2, ensure_ascii=False)
            # The FILE still carries the minted id (it is the report, and it
            # lives on the same card as the roster), but the journal does not:
            # a glob an operator can paste is enough to find it.
            log.info("results report dropped to %s/*%s.html", self.outbox_dir, safe_session)
            return {"delivered": True, "via": "outbox", "path": html_path, "to": to}
        except OSError as e:
            # str(e) would append `: '<filename>'`, and that basename is the
            # student id: "[Errno 28] No space left on device:
            # 'artifacts/outbox/anna-001-...'".  The subclass plus the errno
            # NUMBER separates a full card (ENOSPC, a bare OSError) from a
            # read-only one (EROFS, also bare) from a permission fault, which is
            # the whole diagnosis -- and the scrubbed id still says which session
            # lost its report.
            log.warning("outbox write failed (%s errno=%s) for %s",
                        type(e).__name__, getattr(e, "errno", "-"), safe_session)
            return {"delivered": False, "reason": type(e).__name__}


def deliver_results(stats: dict) -> None:
    """Default on_session_end hook for the hostapp."""
    ResultsMailer.from_env().deliver(stats)


def send_test(to: object, *, environ: Optional[Mapping[str, str]] = None
              ) -> tuple[bool, str]:
    """The admin page's "Send test e-mail": ``(sent, code)``
    (:meth:`ResultsMailer.send_test`). ``environ`` is the configuration to test
    (default: this process's)."""

    return ResultsMailer.from_env(environ).send_test(to)
