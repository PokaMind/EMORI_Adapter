"""The EMORI-free analytics consumer: does it stay EMORI-free, and is its copy of
the recovery rule the SAME rule the robot ran?

Two halves, and the split is the point:

* the script may not import ``emori_adapter`` (asserted with ``ast``, because an
  import that only fires on a live broker would not show up any other way);
* this test file MAY import it, and does — the whole value of the script is that
  its independent re-implementation lands on the robot's numbers, so the rule is
  checked against the real :class:`RecoveryTracker` on random level sequences
  rather than against a handful of hand-written cases.

The HTTP half runs a real loopback server rather than ``respx``. ``respx``
patches httpx, and this script deliberately speaks stdlib urllib so an ARISE
partner needs no wheels at all; a real socket checks the URL, the query string
and — the part that actually breaks in the field — the two DIFFERENT tenant
headers Orion-LD and QuantumLeap want.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import random
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from emori_adapter import fiware_schema as schema
from emori_adapter.hostapp import metrics_store

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "arise_consumer_check.py"
_URN = "urn:ngsi-ld:Session:S-ab12cd-20260916"


def _load():
    spec = importlib.util.spec_from_file_location("arise_consumer_check", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = _load()


# --------------------------------------------------------------------------
# the standalone-consumer rule
# --------------------------------------------------------------------------
def test_the_script_imports_nothing_from_emori_adapter():
    """An ARISE consumer has the broker and nothing else."""

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "emori_adapter"]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root == "emori_adapter":
                offenders.append(node.module or "")
    assert offenders == [], f"{_SCRIPT.name} imports EMORI: {offenders}"


def test_the_script_only_needs_the_standard_library():
    """No third-party import either — httpx is a wheel a partner may not have."""

    stdlib_ok = {"argparse", "datetime", "json", "sys", "urllib", "typing", "__future__"}
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add((node.module or "").split(".")[0])
    assert roots <= stdlib_ok, f"unexpected dependency: {sorted(roots - stdlib_ok)}"


# --------------------------------------------------------------------------
# the rule, against the producer's own state machine
# --------------------------------------------------------------------------
def test_the_constants_match_the_producer():
    assert CHECK.RECOVERY_WINDOW_S == metrics_store.RECOVERY_WINDOW_S == 90.0
    assert CHECK.DROP_LEVELS == metrics_store.DROP_LEVELS == frozenset({1, 4})
    assert CHECK.ENGAGED_LEVEL == metrics_store.ENGAGED_LEVEL == 3
    assert CHECK.CORE_CTX == schema.CORE_CTX
    assert CHECK.LEVEL_ATTR in schema.METRIC_ATTRS
    assert CHECK.ATTEMPTS_ATTR in schema.SUMMARY_ATTRS
    assert CHECK.SUCCESSES_ATTR in schema.SUMMARY_ATTRS
    assert CHECK.session_urn("S-1") == schema.session_urn("S-1")


def _tracker_counts(series, window_s=metrics_store.RECOVERY_WINDOW_S):
    """The same series through the robot's own tracker."""

    tracker = metrics_store.RecoveryTracker(window_s=window_s)
    attempts = successes = 0
    for stamp, level in series:
        for kind in tracker.observe("s1", level, stamp):
            attempts += kind == "attention_drop"
            successes += kind == "recovery_success"
    return attempts, successes


@pytest.mark.parametrize("seed", range(40))
def test_the_rule_matches_the_hostapp_tracker_on_random_sequences(seed):
    rng = random.Random(seed)
    stamp = 1_760_000_000.0
    series = []
    for _ in range(rng.randint(1, 60)):
        # Gaps straddle the 90 s window on purpose: expiry is the branch a
        # re-implementation gets wrong, and it is invisible on a 2 s cadence.
        stamp += rng.choice([2.0, 2.0, 2.0, 15.0, 95.0])
        series.append((stamp, rng.randint(0, 4)))
    assert CHECK.drops_and_recoveries(series) == _tracker_counts(series)


def test_the_first_sample_is_never_an_attempt():
    series = [(0.0, 1), (2.0, 1), (4.0, 3)]
    # One recovery and NO attempt: there was no transition into level 1 to see.
    assert CHECK.drops_and_recoveries(series) == (0, 0)
    assert CHECK.drops_and_recoveries(series) == _tracker_counts(series)


def test_a_sustained_dip_counts_once():
    series = [(0.0, 3), (2.0, 1), (4.0, 1), (6.0, 1), (8.0, 3)]
    assert CHECK.drops_and_recoveries(series) == (1, 1)


def test_a_late_return_is_not_a_recovery():
    series = [(0.0, 3), (2.0, 1), (200.0, 3)]
    assert CHECK.drops_and_recoveries(series) == (1, 0)
    assert CHECK.drops_and_recoveries(series) == _tracker_counts(series)


def test_one_success_per_attempt():
    series = [(0.0, 3), (2.0, 1), (4.0, 3), (6.0, 3), (8.0, 3)]
    assert CHECK.drops_and_recoveries(series) == (1, 1)


def test_an_unparseable_sample_is_skipped_without_breaking_the_chain():
    series = [(0.0, 3), (1.0, "x"), (2.0, 1), (4.0, 3)]
    assert CHECK.drops_and_recoveries(series) == (1, 1)


def test_the_window_is_configurable_for_a_differently_tuned_producer():
    series = [(0.0, 3), (2.0, 1), (40.0, 3)]
    assert CHECK.drops_and_recoveries(series, window_s=90.0) == (1, 1)
    assert CHECK.drops_and_recoveries(series, window_s=10.0) == (1, 0)


# --------------------------------------------------------------------------
# the figure
# --------------------------------------------------------------------------
def test_recovery_figure_is_none_when_nothing_ever_dropped():
    assert CHECK.recovery_figure(0, 0) is None
    assert CHECK.recovery_figure(4, 3) == 0.75
    assert CHECK.recovery_figure(2, 0) == 0.0


# --------------------------------------------------------------------------
# reading the two feeds
# --------------------------------------------------------------------------
def test_level_series_parses_quantumleap_and_sorts_it():
    payload = {"index": ["2026-09-16T10:00:04.000", "2026-09-16T10:00:02.000"],
               "values": [3, 1]}
    assert CHECK.level_series(payload) == [
        (CHECK.parse_timestamp("2026-09-16T10:00:02Z"), 1),
        (CHECK.parse_timestamp("2026-09-16T10:00:04Z"), 3),
    ]


def test_level_series_drops_the_seeded_unknown_row():
    """``session_entity()`` seeds engagementLevel=0 so QuantumLeap infers the right
    column type. Counting it would invent one attention drop per session."""

    payload = {"index": ["2026-09-16T10:00:00", "2026-09-16T10:00:02", "2026-09-16T10:00:04"],
               "values": [0, 1, 3]}
    series = CHECK.level_series(payload)
    assert [level for _, level in series] == [1, 3]
    # The robot saw level 1 as its first cycle, so it counted no drop; kept, the
    # seed would have made that first cycle a TRANSITION and invented one.
    assert CHECK.drops_and_recoveries(series) == (0, 0) == _tracker_counts(series)
    raw = [(CHECK.parse_timestamp(s), v)
           for s, v in zip(payload["index"], payload["values"])]
    assert CHECK.drops_and_recoveries(raw) == (1, 1)


def test_level_series_keeps_a_later_unknown():
    payload = {"index": ["2026-09-16T10:00:00", "2026-09-16T10:00:02"], "values": [3, 0]}
    assert [level for _, level in CHECK.level_series(payload)] == [3, 0]


def test_level_series_survives_junk():
    assert CHECK.level_series(None) == []
    assert CHECK.level_series({"index": "no", "values": []}) == []
    assert CHECK.level_series({"index": ["nope"], "values": [3]}) == []


def test_parse_timestamp_treats_a_naked_stamp_as_utc():
    assert CHECK.parse_timestamp("2026-09-16T10:00:00") == \
        CHECK.parse_timestamp("2026-09-16T10:00:00Z")
    assert CHECK.parse_timestamp("") is None
    assert CHECK.parse_timestamp("not a date") is None


def test_counter_value_tells_absent_from_zero():
    entity = {"recoveryAttempts": {"type": "Property", "value": 0}}
    assert CHECK.counter_value(entity, "recoveryAttempts") == 0
    assert CHECK.counter_value(entity, "recoverySuccesses") is None
    assert CHECK.counter_value({"recoveryAttempts": 4}, "recoveryAttempts") == 4


def test_pick_latest_takes_the_most_recently_observed_session():
    old = {"id": "urn:ngsi-ld:Session:a",
           "engagementLevel": {"observedAt": "2026-09-15T10:00:00Z"}}
    new = {"id": "urn:ngsi-ld:Session:b",
           "engagementLevel": {"observedAt": "2026-09-16T10:00:00Z"}}
    assert CHECK.pick_latest([old, new]) is new
    assert CHECK.pick_latest([new, old]) is new
    assert CHECK.pick_latest([]) is None
    # A deterministic tie-break, because a replay stamps two entities alike.
    tie_a = {"id": "urn:ngsi-ld:Session:a", "modifiedAt": "2026-09-16T10:00:00Z"}
    tie_b = {"id": "urn:ngsi-ld:Session:b", "modifiedAt": "2026-09-16T10:00:00Z"}
    assert CHECK.pick_latest([tie_a, tie_b])["id"].endswith(":b")


def test_session_selector_accepts_a_bare_id_or_a_urn():
    assert CHECK.session_urn("S-1") == _URN.rsplit(":", 1)[0] + ":S-1"
    assert CHECK.session_urn(_URN) == _URN


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------
def test_the_report_has_exactly_the_contract_keys():
    report = CHECK.build_report(_URN, (4, 3), (4, 3))
    assert tuple(report) == CHECK.REPORT_KEYS
    assert report["figure"] == report["series_figure"] == 0.75
    assert report["agree"] is True


def test_an_absent_summary_leaves_the_series_as_the_only_source():
    report = CHECK.build_report(_URN, (None, None), (4, 3))
    assert report["figure"] is None and report["series_figure"] == 0.75
    assert report["agree"] is True


def test_an_unread_series_is_never_agreement():
    report = CHECK.build_report(_URN, (4, 3), None)
    assert report["series_attempts"] is None and report["series_figure"] is None
    assert report["agree"] is False


def test_the_same_ratio_from_different_counters_is_still_a_disagreement():
    """3/2 and 6/4 are the same figure; a feed that lost half its rows is not."""

    report = CHECK.build_report(_URN, (6, 4), (3, 2))
    assert report["figure"] == report["series_figure"]
    assert report["agree"] is False


def test_the_stderr_line_names_both_sides():
    line = CHECK.summarise(CHECK.build_report(_URN, (4, 3), (4, 2)))
    assert _URN in line and "4/3" in line and "4/2" in line and "DISAGREE" in line
    assert "\n" not in line
    assert "absent" in CHECK.summarise(CHECK.build_report(_URN, (None, None), (0, 0)))


# --------------------------------------------------------------------------
# the HTTP paths, against a real loopback broker
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _broker(routes: dict):
    """A loopback stand-in for Orion-LD and QuantumLeap; yields (base_url, seen)."""

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep pytest output clean
            pass

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            parsed = urllib.parse.urlsplit(self.path)
            seen.append({"path": parsed.path,
                         "query": urllib.parse.parse_qs(parsed.query),
                         "headers": {k.lower(): v for k, v in self.headers.items()}})
            status, payload = routes.get(parsed.path, (404, {"error": "Not Found"}))
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _entity(attempts=None, successes=None, urn=_URN):
    entity = {"id": urn, "type": "Session",
              "engagementLevel": {"type": "Property", "value": 3,
                                  "observedAt": "2026-09-16T10:00:46Z"}}
    if attempts is not None:
        entity["recoveryAttempts"] = {"type": "Property", "value": attempts}
    if successes is not None:
        entity["recoverySuccesses"] = {"type": "Property", "value": successes}
    return entity


def _ql_rows(levels, urn=_URN):
    index = [f"2026-09-16T10:00:{2 * i:02d}.000" for i in range(len(levels))]
    return {"attrName": "engagementLevel", "entityId": urn, "index": index, "values": levels}


# Seed 0, then 3 -> 1 (one attempt) -> 3 (one success) -> 4 (a second attempt,
# never recovered): the series the routes below historize.
_LEVELS = [0, 3, 1, 3, 4, 4]
_ROUTES_OK = {
    "/ngsi-ld/v1/entities": (200, [_entity(2, 1)]),
    f"/ngsi-ld/v1/entities/{_URN}": (200, _entity(2, 1)),
    f"/v2/entities/{_URN}/attrs/engagementLevel": (200, _ql_rows(_LEVELS)),
}


def _run(base, seen_holder=None, *extra):
    argv = ["--orion-url", base, "--ql-url", base, *extra]
    return CHECK.main(argv)


def test_the_series_confirms_the_summary_and_the_check_exits_zero(capsys):
    with _broker(dict(_ROUTES_OK)) as (base, seen):
        code = _run(base)
    out = json.loads(capsys.readouterr().out)
    assert code == CHECK.EXIT_AGREE
    assert tuple(out) == CHECK.REPORT_KEYS
    assert out["session"] == _URN
    assert (out["recoveryAttempts"], out["recoverySuccesses"]) == (2, 1)
    assert (out["series_attempts"], out["series_successes"]) == (2, 1)
    assert out["figure"] == out["series_figure"] == 0.5
    assert out["agree"] is True
    assert {req["path"] for req in seen} == {
        "/ngsi-ld/v1/entities", f"/v2/entities/{_URN}/attrs/engagementLevel"}


def test_each_broker_gets_the_tenant_header_it_actually_wants():
    with _broker(dict(_ROUTES_OK)) as (base, seen):
        _run(base, None, "--tenant", "arise")
    orion = next(r for r in seen if r["path"].startswith("/ngsi-ld"))
    quantumleap = next(r for r in seen if r["path"].startswith("/v2"))
    assert orion["headers"]["ngsild-tenant"] == "arise"
    assert CHECK.CORE_CTX in orion["headers"]["link"]
    assert "json-ld#context" in orion["headers"]["link"]
    # QuantumLeap is NGSI-v2 shaped: NGSILD-Tenant means nothing to it.
    assert quantumleap["headers"]["fiware-service"] == "arise"
    assert quantumleap["headers"]["fiware-servicepath"] == "/"
    assert "ngsild-tenant" not in quantumleap["headers"]


def test_latest_asks_orion_for_sessions_and_an_explicit_urn_does_not(capsys):
    with _broker(dict(_ROUTES_OK)) as (base, seen):
        code = _run(base, None, "--session", _URN)
    capsys.readouterr()
    assert code == CHECK.EXIT_AGREE
    assert [r["path"] for r in seen][0] == f"/ngsi-ld/v1/entities/{_URN}"
    assert seen[0]["query"] == {}


def test_the_latest_query_is_typed_and_bounded():
    with _broker(dict(_ROUTES_OK)) as (base, seen):
        _run(base)
    listing = next(r for r in seen if r["path"] == "/ngsi-ld/v1/entities")
    assert listing["query"]["type"] == ["Session"]
    assert listing["query"]["limit"] == ["1000"]


def test_a_broker_that_rejects_sysattrs_still_answers(capsys):
    """Orion-LD takes ``options=sysAttrs``; a proxy or an older broker may not."""

    calls = {"n": 0}
    base_routes = dict(_ROUTES_OK)

    class _Flaky(dict):
        def get(self, key, default=None):
            if key == "/ngsi-ld/v1/entities":
                calls["n"] += 1
                if calls["n"] == 1:
                    return (400, {"type": "https://uri.etsi.org/ngsi-ld/errors/BadRequestData"})
            return base_routes.get(key, default)

    with _broker(_Flaky()) as (base, seen):
        code = _run(base)
    capsys.readouterr()
    assert code == CHECK.EXIT_AGREE
    assert calls["n"] == 2


def test_a_disagreement_exits_one(capsys):
    routes = dict(_ROUTES_OK)
    routes["/ngsi-ld/v1/entities"] = (200, [_entity(5, 5)])
    with _broker(routes) as (base, _seen):
        code = _run(base)
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == CHECK.EXIT_DISAGREE
    assert out["agree"] is False
    assert (out["recoveryAttempts"], out["series_attempts"]) == (5, 2)
    assert "DISAGREE" in captured.err


def test_no_session_exits_two(capsys):
    with _broker({"/ngsi-ld/v1/entities": (200, [])}) as (base, _seen):
        code = _run(base)
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == CHECK.EXIT_NO_SESSION
    assert out == dict.fromkeys(CHECK.REPORT_KEYS) | {"agree": False}
    # "no session" is not "the two disagree": the stderr line has to say which.
    assert "NO SESSION" in captured.err and "DISAGREE" not in captured.err


def test_an_unknown_urn_exits_two(capsys):
    with _broker(dict(_ROUTES_OK)) as (base, _seen):
        code = _run(base, None, "--session", "S-does-not-exist")
    capsys.readouterr()
    assert code == CHECK.EXIT_NO_SESSION


def test_a_quantumleap_that_never_historized_this_session_is_an_empty_series(capsys):
    """404 from QuantumLeap is a real answer: zero rows, not an unread feed."""

    routes = dict(_ROUTES_OK)
    routes["/ngsi-ld/v1/entities"] = (200, [_entity(0, 0)])
    del routes[f"/v2/entities/{_URN}/attrs/engagementLevel"]
    with _broker(routes) as (base, _seen):
        code = _run(base)
    out = json.loads(capsys.readouterr().out)
    assert code == CHECK.EXIT_AGREE
    assert (out["series_attempts"], out["series_successes"]) == (0, 0)
    assert out["series_figure"] is None


def test_a_broken_quantumleap_is_a_failure_not_an_agreement(capsys):
    routes = dict(_ROUTES_OK)
    routes[f"/v2/entities/{_URN}/attrs/engagementLevel"] = (500, {"error": "boom"})
    with _broker(routes) as (base, _seen):
        code = _run(base)
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == CHECK.EXIT_DISAGREE
    assert out["series_attempts"] is None and out["agree"] is False
    assert "SERIES UNREADABLE" in captured.err


def test_an_unreachable_broker_exits_two_rather_than_crashing(capsys):
    # Port 1 on loopback: nothing listens, and the connection is refused fast.
    code = CHECK.main(["--orion-url", "http://127.0.0.1:1", "--ql-url", "http://127.0.0.1:1",
                       "--timeout", "1"])
    capsys.readouterr()
    assert code == CHECK.EXIT_NO_SESSION


def test_help_works_without_a_broker(capsys):
    with pytest.raises(SystemExit) as excinfo:
        CHECK.main(["--help"])
    assert excinfo.value.code == 0
    assert "--session" in capsys.readouterr().out
