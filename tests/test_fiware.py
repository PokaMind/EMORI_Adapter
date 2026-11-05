"""FiwareClient tests over an httpx MockTransport — no Orion needed.

Cover the canonical schema (one attribute set everywhere), the idempotent
subscription (a restart must NOT duplicate it), the 0.001 float seeding that
keeps QuantumLeap's column types right, and the disable-on-failure latch.
"""

from __future__ import annotations

import json

import httpx

from emori_adapter import fiware_schema as schema
from emori_adapter.fiware import FiwareClient
from emori_adapter.models import ContextSignal, DerivedSignals, EngagementLevel


def _signals(**kw) -> DerivedSignals:
    base = dict(timestamp="2026-06-11T10:00:00Z", session_id="s1", person_id="p1", seq=3,
                engagement_score=0.74, engagement_level=EngagementLevel.ENGAGED,
                valence=0.3, arousal=0.1, expression="happy", expression_confidence=0.4,
                top_styles=[("mirroring", 0.2)],
                context=ContextSignal(engagement_trend=0.12, escalation=0.05, rapport=0.7,
                                      attention=0.74, dominant_style="mirroring"))
    base.update(kw)
    return DerivedSignals(**base)


class _OrionRecorder:
    """Fake Orion: records requests, scriptable subscription listing."""

    def __init__(self, existing_subs: list | None = None):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.existing_subs = existing_subs or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/ngsi-ld/v1/subscriptions/":
            return httpx.Response(200, json=self.existing_subs)
        if request.method == "POST" and request.url.path == "/ngsi-ld/v1/entities/":
            return httpx.Response(201)
        return httpx.Response(204)


def _client(recorder: _OrionRecorder, **kw) -> FiwareClient:
    c = FiwareClient(session_id="anna-001-20260611", person_id="anna-001",
                     person_name="Anna", **kw)
    c._http = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    return c


def test_ensure_session_creates_canonical_entities_and_subscription():
    rec = _OrionRecorder()
    c = _client(rec)
    c.ensure_session(topic="Difficult Conversations")

    posts = [(p, b) for m, p, b in rec.requests if m == "POST"]
    entities = [b for p, b in posts if p == "/ngsi-ld/v1/entities/"]
    subs = [b for p, b in posts if p == "/ngsi-ld/v1/subscriptions/"]
    assert len(entities) == 2 and len(subs) == 1

    person = next(e for e in entities if e["type"] == "Person")
    session = next(e for e in entities if e["type"] == "Session")
    assert person["id"] == "urn:ngsi-ld:Person:anna-001"
    assert person["name"]["value"] == "Anna"
    assert session["id"] == "urn:ngsi-ld:Session:anna-001-20260611"
    assert session["topic"]["value"] == "Difficult Conversations"
    assert session["sessionType"]["value"] == schema.DEFAULT_SESSION_TYPE == "wellbeing"
    # every canonical metric attr is seeded, floats with 0.001 (BIGINT bug guard)
    for attr, typ in schema.METRIC_ATTRS.items():
        assert attr in session, f"missing seed for {attr}"
        if typ is float:
            assert session[attr]["value"] == schema.FLOAT_SEED
    assert session["engagementLevel"]["value"] == 0
    # the subscription watches the full canonical set incl. the summary attrs
    assert set(subs[0]["watchedAttributes"]) == set(schema.WATCHED_ATTRS)


def test_subscription_is_idempotent_across_restarts():
    sub = schema.subscription_body("http://quantumleap:8668/v2/notify", "pokamind")
    rec = _OrionRecorder(existing_subs=[{
        "id": "urn:ngsi-ld:Subscription:abc",
        "description": sub["description"],
        "notification": {"endpoint": {"uri": "http://quantumleap:8668/v2/notify"}},
    }])
    c = _client(rec)
    c.ensure_session()
    sub_posts = [p for m, p, _ in rec.requests if m == "POST" and "subscriptions" in p]
    assert sub_posts == [], "duplicate subscription POSTed despite an existing one"


def test_push_writes_all_canonical_metrics_including_trend():
    rec = _OrionRecorder()
    c = _client(rec)
    c.push(_signals())
    method, path, body = rec.requests[-1]
    assert path.endswith("/entities/urn:ngsi-ld:Session:anna-001-20260611/attrs")
    sent = {k for k in body if k != "@context"}
    assert sent == set(schema.METRIC_ATTRS), "push diverged from the canonical schema"
    assert body["engagementTrend"]["value"] == 0.12  # historically never written
    assert body["engagementLevel"]["value"] == 3
    # one shared observedAt per call = one time-series row
    stamps = {v["observedAt"] for k, v in body.items() if k != "@context"}
    assert len(stamps) == 1


def test_write_summary():
    rec = _OrionRecorder()
    c = _client(rec)
    c.write_summary("Great session.", 0.71)
    _, _, body = rec.requests[-1]
    assert body["sessionSummary"]["value"] == "Great session."
    assert body["avgEngagement"]["value"] == 0.71
    # Uncounted recovery must stay ABSENT, not be published as a fabricated 0.
    assert "recoveryAttempts" not in body and "recoverySuccesses" not in body


def test_write_summary_publishes_recovery_counters():
    rec = _OrionRecorder()
    c = _client(rec)
    c.write_summary("Great session.", 0.71, recovery_attempts=3, recovery_successes=2)
    _, _, body = rec.requests[-1]
    assert body["recoveryAttempts"]["value"] == 3
    assert body["recoverySuccesses"]["value"] == 2
    # The engagement-recovery KPI must be answerable from Orion/QuantumLeap alone.
    assert {"recoveryAttempts", "recoverySuccesses"} <= set(schema.WATCHED_ATTRS)


def test_broker_reachable_probe_is_cheap_and_fails_closed():

    seen: list[str] = []

    def up(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"Orion-LD version": "1.5.1"})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    import emori_adapter.fiware as fiware_module

    real_get = httpx.get
    try:
        httpx.get = lambda url, timeout=None: httpx.Client(  # type: ignore[assignment]
            transport=httpx.MockTransport(up)).get(url)
        assert fiware_module.broker_reachable("http://orion:1026") is True
        assert seen == ["/version"]
        httpx.get = lambda url, timeout=None: httpx.Client(  # type: ignore[assignment]
            transport=httpx.MockTransport(down)).get(url)
        assert fiware_module.broker_reachable("http://orion:1026") is False
    finally:
        httpx.get = real_get


def test_entity_create_failure_disables_client():
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ngsi-ld/v1/entities/":
            return httpx.Response(422, text="bad entity")
        return httpx.Response(200, json=[])

    c = FiwareClient(session_id="s", person_id="p")
    c._http = httpx.Client(transport=httpx.MockTransport(failing))
    c.ensure_session()
    assert c.enabled is False
    c.push(_signals())  # must be a silent no-op, not a network call


# ----- what the JOURNAL learns about a session -----
#
# Root logging reaches journald, and journald on this appliance is world-readable
# to anyone holding the box and survives reboots.  These lines fire twice per
# session start on every robot with a broker.

def test_the_entity_lines_name_the_type_not_the_person(caplog):
    import logging

    rec = _OrionRecorder()
    c = _client(rec)
    caplog.set_level(logging.INFO, logger="emori.fiware")

    c.ensure_session(topic="Difficult Conversations")

    logged = "\n".join(caplog.messages)
    # `ensure_session` creates exactly one of each, so the TYPE is the whole
    # diagnostic -- "did entity creation work" never needed to know who.
    assert "entity Person created" in logged
    assert "entity Session created" in logged
    assert "anna-001" not in logged and "urn:ngsi-ld:Person" not in logged


def test_a_rejected_entity_never_prints_the_body_the_broker_echoed(caplog):
    """An NGSI-LD broker returns the OFFENDING PAYLOAD in ProblemDetails.detail,
    and for the Person entity that payload holds the staff member's name."""

    import logging

    def rejecting(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ngsi-ld/v1/entities/":
            return httpx.Response(400, json={
                "type": "https://uri.etsi.org/ngsi-ld/errors/BadRequestData",
                "title": "Invalid attribute",
                "detail": '{"id": "urn:ngsi-ld:Person:anna-001", '
                          '"name": {"type": "Property", "value": "Anna Example"}}',
            })
        return httpx.Response(200, json=[])

    c = FiwareClient(session_id="20260611-103000", person_id="S-ab12cd", person_name="Anna Example")
    c._http = httpx.Client(transport=httpx.MockTransport(rejecting))
    caplog.set_level(logging.WARNING, logger="emori.fiware")

    c.ensure_session()

    logged = "\n".join(caplog.messages)
    assert c.enabled is False               # the latch still latches
    assert "create Person -> 400" in logged  # ...and still says what happened
    assert "BadRequestData" in logged        # from a FIXED error registry
    assert "Anna Example" not in logged and "anna-001" not in logged


def test_a_non_json_error_body_degrades_to_its_content_type(caplog):
    """A proxy's HTML error page is not a problem document; it is also not
    something to paste into a journal."""

    import logging

    def html_error(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ngsi-ld/v1/entities/":
            return httpx.Response(502, text="<html>anna@example.org</html>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, json=[])

    c = FiwareClient(session_id="s", person_id="p")
    c._http = httpx.Client(transport=httpx.MockTransport(html_error))
    caplog.set_level(logging.WARNING, logger="emori.fiware")

    c.ensure_session()

    logged = "\n".join(caplog.messages)
    assert "text/html" in logged
    assert "example.org" not in logged


def test_a_partial_attribute_append_never_prints_the_body_either(caplog):
    """The 207 branch was the last `r.text` in this module, and it is the worst
    one: `notUpdated` quotes the attribute VALUE the broker refused, and one of
    the attributes travelling through `_set_attrs` is `sessionSummary` -- the
    session's own summary text."""

    import logging

    summary = "Anna Example de-escalated a distressed relative in room 4"

    def partial(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attrs"):
            return httpx.Response(207, json={
                "type": "https://uri.etsi.org/ngsi-ld/errors/BadRequestData",
                "notUpdated": [{"attributeName": "sessionSummary",
                                "reason": "value rejected: " + summary}],
            })
        return httpx.Response(204)

    c = FiwareClient(session_id="20260611-103000", person_id="S-ab12cd")
    c._http = httpx.Client(transport=httpx.MockTransport(partial))
    caplog.set_level(logging.WARNING, logger="emori.fiware")

    c.write_summary(summary, 0.62)

    logged = "\n".join(caplog.messages)
    assert "207" in logged                      # still says what happened
    assert "sessionSummary" in logged           # ...and which attributes went
    assert "BadRequestData" in logged           # ...and what the broker called it
    assert "Anna Example" not in logged and "room 4" not in logged


def test_a_rejected_attribute_append_below_warning_is_body_free_too(caplog):
    """The 4xx branch logs at DEBUG, which EMORI_LOG_LEVEL=DEBUG makes visible on
    a deployed robot -- so it is bound by the same rule, not a weaker one."""

    import logging

    def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attrs"):
            return httpx.Response(400, json={
                "type": "https://uri.etsi.org/ngsi-ld/errors/BadRequestData",
                "detail": "Anna Example em va dir que se sent insegura",
            })
        return httpx.Response(204)

    c = FiwareClient(session_id="s", person_id="p")
    c._http = httpx.Client(transport=httpx.MockTransport(rejected))
    caplog.set_level(logging.DEBUG, logger="emori.fiware")

    c.push_attrs({"avgEngagement": 0.5})

    logged = "\n".join(caplog.messages)
    assert "BadRequestData" in logged
    assert "Anna Example" not in logged and "insegura" not in logged


def test_an_attribute_name_from_outside_this_file_is_a_shape_not_a_string(caplog):
    """`push_attrs` is the ROS-bridge path: the only caller that does not build
    the key set here.  A name is an identifier or it is reported as `?`."""

    import logging

    def partial(request: httpx.Request) -> httpx.Response:
        return httpx.Response(207, json={"type": "urn:x:Problem"}) \
            if request.url.path.endswith("/attrs") else httpx.Response(204)

    c = FiwareClient(session_id="s", person_id="p")
    c._http = httpx.Client(transport=httpx.MockTransport(partial))
    caplog.set_level(logging.WARNING, logger="emori.fiware")

    c.push_attrs({"avgEngagement": 0.5,
                  "Anna Example em va dir que se sent insegura": 1})

    logged = "\n".join(caplog.messages)
    assert "avgEngagement" in logged
    assert "Anna Example" not in logged and "insegura" not in logged
    assert "?" in logged


def test_static_fixtures_match_canonical_schema():
    """deploy/fiware JSONs are generated from the schema — verify no drift."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "deploy" / "fiware"
    session = json.loads((root / "entities" / "session.json").read_text(encoding="utf-8"))
    sub = json.loads((root / "subscriptions" / "quantumleap-subscription.json").read_text(encoding="utf-8"))

    for attr, typ in schema.METRIC_ATTRS.items():
        assert attr in session, f"fixture missing {attr} (run scripts/gen_fiware_fixtures.py)"
        if typ is float:
            assert session[attr]["value"] == schema.FLOAT_SEED, f"{attr} seeded 0.0 (BIGINT bug)"
    assert session["sessionType"]["value"] == schema.DEFAULT_SESSION_TYPE
    assert set(sub["watchedAttributes"]) == set(schema.WATCHED_ATTRS)
