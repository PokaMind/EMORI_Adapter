"""Prove the analytics interface: read one EMORI session back out of FIWARE and
recompute the ARISE engagement-recovery figure WITHOUT any EMORI code.

This is what an outside analytics consumer actually runs. It is therefore
deliberately crippled in the two ways such a consumer is crippled:

* it imports nothing from ``emori_adapter`` (a consumer has the broker, not our
  source tree -- ``tests/test_arise_consumer_check.py`` asserts that with ``ast``);
* it speaks only stdlib HTTP, so it runs on a plain Python with no wheels at all.

It reads the two numbers the robot publishes at the end of a session
(``recoveryAttempts``/``recoverySuccesses`` on the NGSI-LD ``Session`` entity in
Orion-LD) and, separately, the whole ``engagementLevel`` time series out of
QuantumLeap, then recomputes the same two numbers from that series with the
robot's own state machine re-implemented here from scratch. If the two disagree,
one of the two feeds is lying and the KPI built on them is worthless -- which is
the entire point of the check, and why it exits non-zero on disagreement rather
than printing a warning.

    python scripts/arise_consumer_check.py --session latest

Exit codes: ``0`` the two agree (or the summary is absent and the series is the
only source), ``1`` they disagree (or the series could not be read at all),
``2`` there is no such session.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Sequence

# --- the wire contract, copied (not imported) from the producer ---------------
# A consumer cannot import emori_adapter.fiware_schema, so these literals are
# duplicated on purpose. They are pinned against the real ones in
# tests/test_arise_consumer_check.py, which MAY import EMORI.
CORE_CTX = "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context-v1.8.jsonld"
# NGSI-LD carries @context in the body on POST; a GET has no body, so the context
# travels as a Link header instead (ETSI GS CIM 009, clause 6.3.5).
LINK_HEADER = (
    f'<{CORE_CTX}>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)
SESSION_TYPE = "Session"
SESSION_URN_PREFIX = "urn:ngsi-ld:Session:"
LEVEL_ATTR = "engagementLevel"
ATTEMPTS_ATTR = "recoveryAttempts"
SUCCESSES_ATTR = "recoverySuccesses"

# --- the recovery rule, replicated from the robot -----------------------------
# src/emori_adapter/hostapp/metrics_store.py:409-465 (RecoveryTracker) and its
# module constants. The window is the producer's default (90.0 s): a consumer that
# scored the same series against a different window would report a disagreement that
# is its own fault.
DROP_LEVELS = frozenset({1, 4})  # DISENGAGED, DISENGAGING (REP-155)
ENGAGED_LEVEL = 3
UNKNOWN_LEVEL = 0
RECOVERY_WINDOW_S = 90.0

EXIT_AGREE = 0
EXIT_DISAGREE = 1
EXIT_NO_SESSION = 2

# Exactly the keys this script's stdout contract promises, in order.
REPORT_KEYS = ("session", "recoveryAttempts", "recoverySuccesses", "figure",
               "series_attempts", "series_successes", "series_figure", "agree")


# --------------------------------------------------------------------------
# pure: the figure and the rule behind it
# --------------------------------------------------------------------------
def recovery_figure(attempts: int, successes: int) -> float | None:
    """The ARISE KPI ratio, or ``None`` when nobody's attention ever dropped.

    ``None``, never ``0.0``: a session in which the learner never disengaged did
    not fail to recover them, and averaging a fabricated zero into a fleet KPI is
    how "engagement recovery > 70%" becomes a number nobody can defend.
    """

    attempts = int(attempts)
    if attempts <= 0:
        return None
    return float(successes) / float(attempts)


def drops_and_recoveries(series: Sequence[tuple[float, int]],
                         window_s: float = RECOVERY_WINDOW_S) -> tuple[int, int]:
    """``(attempts, successes)`` from a time-ordered ``(epoch_s, level)`` series.

    A from-scratch re-implementation of the robot's ``RecoveryTracker``:

    * entering DISENGAGED(1) or DISENGAGING(4) from anything else is one attempt
      (a sustained dip counts once, not once per cycle);
    * reaching ENGAGED(3) within ``window_s`` of that entry is one success, and
      it closes the attempt -- at most one success per attempt;
    * later than the window the attempt simply expires (the learner came back,
      but not because the robot's nudge worked);
    * the FIRST sample is never an attempt. There is no transition to observe
      yet, so there is nothing the robot could have caused.

    Samples that are not a (number, number) pair are skipped without disturbing
    the chain, exactly as the producer's ``int()``/``float()`` guard does.
    """

    attempts = 0
    successes = 0
    previous: int | None = None
    dropped_at: float | None = None
    for stamp, value in series:
        try:
            level = int(value)
            moment = float(stamp)
        except (TypeError, ValueError):
            continue
        prior, previous = previous, level
        if dropped_at is not None and moment - dropped_at > float(window_s):
            dropped_at = None
        if level in DROP_LEVELS:
            if prior is not None and prior not in DROP_LEVELS:
                dropped_at = moment
                attempts += 1
            continue
        if level == ENGAGED_LEVEL and dropped_at is not None:
            dropped_at = None
            successes += 1
    return attempts, successes


def parse_timestamp(text: Any) -> float | None:
    """An ISO-8601 stamp (QuantumLeap's ``index``, Orion's ``observedAt``) as epoch
    seconds. A stamp with no zone is UTC -- QuantumLeap stores UTC and says so by
    omitting the offset. ``None`` for anything unparseable."""

    raw = str(text or "").strip()
    if not raw:
        return None
    if raw[-1] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


def coerce_level(value: Any) -> int | None:
    """An engagement level as an int, or ``None``. QuantumLeap hands back whatever
    its column type inferred, so a level can arrive as ``3``, ``3.0`` or ``"3"``."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def level_series(payload: Any) -> list[tuple[float, int]]:
    """QuantumLeap's ``{"index": [...], "values": [...]}`` as a sorted series.

    Leading UNKNOWN(0) samples are dropped. The producer's engagement tracker
    commits to a real level on cycle one and never reports UNKNOWN, so a leading
    0 is not a cycle at all: it is the seed value ``session_entity()`` writes into
    every metric attribute so QuantumLeap infers the right column types. Counting
    it would make the robot's real first cycle a *transition* and invent one
    attention drop per session -- in the series half of this check only, which is
    precisely the false disagreement this script must not produce.
    """

    if not isinstance(payload, dict):
        return []
    index = payload.get("index")
    values = payload.get("values")
    if not isinstance(index, list) or not isinstance(values, list):
        return []
    out: list[tuple[float, int]] = []
    for stamp, value in zip(index, values):
        moment = parse_timestamp(stamp)
        level = coerce_level(value)
        if moment is None or level is None:
            continue
        out.append((moment, level))
    out.sort(key=lambda pair: pair[0])
    seeded = 0
    while seeded < len(out) and out[seeded][1] == UNKNOWN_LEVEL:
        seeded += 1
    return out[seeded:]


# --------------------------------------------------------------------------
# pure: reading an NGSI-LD entity
# --------------------------------------------------------------------------
def attr_value(entity: Any, name: str) -> Any:
    """The value of one NGSI-LD attribute, normalized or key-values form."""

    if not isinstance(entity, dict):
        return None
    attr = entity.get(name)
    if isinstance(attr, dict):
        return attr.get("value")
    return attr


def counter_value(entity: Any, name: str) -> int | None:
    """An int summary counter, or ``None`` when the attribute is absent.

    Absent is a real answer: the robot writes these two only when its local store
    actually counted them, so "unknown" must not collapse into 0.
    """

    value = attr_value(entity, name)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def entity_stamp(entity: Any) -> str:
    """A sortable "when was this session last touched" key.

    ISO-8601 UTC strings sort lexicographically, so the newest entity is simply
    the largest stamp across its system attributes and every attribute's
    ``observedAt``. Empty for an entity that carries no stamp at all, which then
    loses to any entity that does.
    """

    if not isinstance(entity, dict):
        return ""
    stamps = [str(entity.get(key) or "") for key in ("modifiedAt", "createdAt")]
    for value in entity.values():
        if isinstance(value, dict):
            stamps.append(str(value.get("observedAt") or ""))
            stamps.append(str(value.get("modifiedAt") or ""))
    return max(stamps) if stamps else ""


def pick_latest(entities: Iterable[Any]) -> dict | None:
    """The most recently touched Session entity, or ``None`` for an empty list.

    The id breaks a tie so the choice is deterministic on a broker that stamps
    two entities in the same millisecond (a replay does exactly that).
    """

    candidates = [e for e in entities if isinstance(e, dict) and e.get("id")]
    if not candidates:
        return None
    return max(candidates, key=lambda e: (entity_stamp(e), str(e.get("id"))))


def session_urn(selector: str) -> str:
    """``latest`` is resolved elsewhere; anything else is a URN or a bare id."""

    value = str(selector or "").strip()
    if value.startswith(SESSION_URN_PREFIX):
        return value
    return SESSION_URN_PREFIX + value


# --------------------------------------------------------------------------
# pure: the verdict
# --------------------------------------------------------------------------
def build_report(session: str | None, summary: tuple[int | None, int | None],
                 series: tuple[int, int] | None) -> dict:
    """The stdout contract: exactly ``REPORT_KEYS``, in order.

    Agreement is on the two COUNTERS, not on the ratio they produce: 3/2 and 6/4
    are the same figure and a consumer that accepted them as equal would be
    papering over a feed that lost half its rows. A null series (QuantumLeap
    unreachable or refusing) is never agreement -- an unread series proves
    nothing, and saying "agree" about it would turn this check into decoration.
    """

    attempts, successes = summary
    have_summary = attempts is not None and successes is not None
    if series is None:
        agree = False
        s_attempts: int | None = None
        s_successes: int | None = None
        s_figure = None
    else:
        s_attempts, s_successes = series
        s_figure = recovery_figure(s_attempts, s_successes)
        # No summary means the series is the only source; there is nothing to
        # contradict it, and a consumer reading a session whose summary never
        # landed (a power cut before finalize) still gets a usable figure.
        agree = (attempts, successes) == (s_attempts, s_successes) if have_summary else True
    return {
        "session": session,
        "recoveryAttempts": attempts,
        "recoverySuccesses": successes,
        "figure": recovery_figure(attempts, successes) if have_summary else None,
        "series_attempts": s_attempts,
        "series_successes": s_successes,
        "series_figure": s_figure,
        "agree": agree,
    }


def summarise(report: dict) -> str:
    """The one-line human answer that goes to stderr beside the JSON."""

    def ratio(figure: Any) -> str:
        return "n/a" if figure is None else f"{float(figure):.1%}"

    def pair(a: Any, b: Any) -> str:
        return "absent" if a is None or b is None else f"{a}/{b}"

    session = report.get("session") or "no session"
    if not report.get("session"):
        verdict = "NO SESSION"
    elif report.get("series_attempts") is None:
        verdict = "SERIES UNREADABLE"
    else:
        verdict = "AGREE" if report.get("agree") else "DISAGREE"
    return (f"{session}: summary {pair(report.get('recoveryAttempts'), report.get('recoverySuccesses'))}"
            f" ({ratio(report.get('figure'))})"
            f" vs series {pair(report.get('series_attempts'), report.get('series_successes'))}"
            f" ({ratio(report.get('series_figure'))}) -> {verdict}")


# --------------------------------------------------------------------------
# HTTP (stdlib only)
# --------------------------------------------------------------------------
def get_json(url: str, headers: dict, timeout: float) -> tuple[int, Any]:
    """``(status, parsed body)``. ``status`` is 0 when nothing answered at all.

    Never raises: this script's job is to report what the two brokers said, and a
    broker being down is one of the answers it has to be able to print.
    """

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(getattr(response, "status", 0) or 0)
            raw = response.read()
    except urllib.error.HTTPError as e:  # a 4xx/5xx is an answer, not a failure
        status = int(e.code)
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception:  # noqa: BLE001 - URLError, socket timeout, bad URL
        return 0, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None


def orion_headers(tenant: str) -> dict:
    """What ``emori_adapter.fiware.FiwareClient`` sends, translated to a GET."""

    return {"Accept": "application/json", "NGSILD-Tenant": tenant, "Link": LINK_HEADER}


def quantumleap_headers(tenant: str, service_path: str = "/") -> dict:
    """QuantumLeap is NGSI-v2-shaped and wants ``Fiware-Service``, NOT
    ``NGSILD-Tenant`` -- it answers "No records found" without it. The value is
    the same tenant, carried into QuantumLeap by the subscription's
    ``receiverInfo`` (deploy/fiware/subscriptions/quantumleap-subscription.json)."""

    return {"Accept": "application/json", "Fiware-Service": tenant,
            "Fiware-ServicePath": service_path}


def fetch_sessions(orion_url: str, tenant: str, timeout: float) -> list:
    """Every Session entity in the tenant, with system attributes when the broker
    offers them (``options=sysAttrs`` is how ``modifiedAt`` becomes visible; a
    broker that rejects it still answers the plain query)."""

    base = orion_url.rstrip("/") + "/ngsi-ld/v1/entities"
    query = {"type": SESSION_TYPE, "limit": "1000"}
    headers = orion_headers(tenant)
    for options in ("sysAttrs", None):
        params = dict(query, options=options) if options else dict(query)
        status, body = get_json(f"{base}?{urllib.parse.urlencode(params)}", headers, timeout)
        if status == 200 and isinstance(body, list):
            return body
    return []


def fetch_session(orion_url: str, tenant: str, urn: str, timeout: float) -> dict | None:
    url = (orion_url.rstrip("/") + "/ngsi-ld/v1/entities/"
           + urllib.parse.quote(urn, safe=":"))
    status, body = get_json(url, orion_headers(tenant), timeout)
    if status == 200 and isinstance(body, dict):
        return body
    return None


def fetch_level_series(ql_url: str, tenant: str, urn: str, timeout: float,
                       service_path: str = "/") -> list[tuple[float, int]] | None:
    """The ``engagementLevel`` history of one Session, or ``None`` when the store
    could not be read.

    ``GET /v2/entities/<id>/attrs/<attr>`` is QuantumLeap's NGSI-TSDB reader; it
    answers ``{"entityId", "attrName", "index": [...], "values": [...]}``. A 404
    is a real, empty answer (nothing was ever historized for this entity) and is
    NOT the same as ``None``: the difference decides whether this script can
    claim to have checked anything.
    """

    url = (ql_url.rstrip("/") + "/v2/entities/" + urllib.parse.quote(urn, safe=":")
           + "/attrs/" + urllib.parse.quote(LEVEL_ATTR) + "?limit=10000")
    status, body = get_json(url, quantumleap_headers(tenant, service_path), timeout)
    if status == 404:
        return []
    if status != 200:
        return None
    return level_series(body)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arise_consumer_check.py",
        description="Recompute the ARISE engagement-recovery figure from FIWARE two ways "
                    "(Orion-LD summary vs QuantumLeap series) and compare them.",
        epilog="Exit 0 they agree, 1 they disagree or the series is unreadable, "
               "2 no such session.",
    )
    parser.add_argument("--orion-url", default="http://localhost:1026",
                        help="NGSI-LD broker base URL (default: %(default)s)")
    parser.add_argument("--ql-url", default="http://localhost:8668",
                        help="QuantumLeap base URL (default: %(default)s)")
    parser.add_argument("--tenant", default="pokamind",
                        help="NGSILD-Tenant on Orion, Fiware-Service on QuantumLeap "
                             "(default: %(default)s)")
    parser.add_argument("--session", default="latest",
                        help="'latest', a full urn:ngsi-ld:Session:<id>, or a bare session id "
                             "(default: %(default)s)")
    parser.add_argument("--service-path", default="/",
                        help="Fiware-ServicePath sent to QuantumLeap (default: %(default)s)")
    parser.add_argument("--window-s", type=float, default=RECOVERY_WINDOW_S,
                        help="recovery window in seconds; MUST match the producer's "
                             "RECOVERY_WINDOW_S (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="per-request HTTP timeout in seconds (default: %(default)s)")
    return parser


def run(args: argparse.Namespace) -> tuple[dict, int]:
    """``(report, exit code)``. Split from :func:`main` so a test can read both."""

    if str(args.session).strip().lower() == "latest":
        entity = pick_latest(fetch_sessions(args.orion_url, args.tenant, args.timeout))
    else:
        entity = fetch_session(args.orion_url, args.tenant,
                               session_urn(args.session), args.timeout)
    if entity is None:
        # No entity, nothing to reconcile: every number is null and `agree` is
        # false, so a caller that only reads the JSON still cannot mistake a
        # missing session for a clean run.
        return build_report(None, (None, None), None), EXIT_NO_SESSION
    urn = str(entity.get("id") or "")
    series = fetch_level_series(args.ql_url, args.tenant, urn, args.timeout, args.service_path)
    counts = None if series is None else drops_and_recoveries(series, args.window_s)
    summary = (counter_value(entity, ATTEMPTS_ATTR), counter_value(entity, SUCCESSES_ATTR))
    report = build_report(urn, summary, counts)
    return report, EXIT_AGREE if report["agree"] else EXIT_DISAGREE


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report, code = run(args)
    print(json.dumps({key: report[key] for key in REPORT_KEYS}, indent=2))
    print(summarise(report), file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
