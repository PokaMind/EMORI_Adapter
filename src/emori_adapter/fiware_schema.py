"""THE canonical NGSI-LD data model for EMORI sessions — single source of truth.

Every FIWARE writer (the Python :class:`~emori_adapter.fiware.FiwareClient` on the
live/hostapp path AND the ROS 2 ``fiware_bridge``) and every static deploy fixture
(``deploy/fiware/entities/*.json``, the QuantumLeap subscription) derives from this
module, so the Session entity has exactly ONE schema — historically there were three
divergent attribute sets and the dashboard could never rely on any of them.

QuantumLeap/TimescaleDB note: float attributes are seeded 0.001 (never 0.0) —
Orion-LD normalises a stored 0.0 back to integer 0 in notifications, which makes
QuantumLeap infer a BIGINT column and then reject every real float.
"""

from __future__ import annotations

import datetime as dt

CORE_CTX = "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context-v1.8.jsonld"

# Per-cycle metric attributes (name -> python type). engagementLevel is the only
# genuine int (REP-155 enum 0..4); everything else numeric is a float.
METRIC_ATTRS: dict[str, type] = {
    "engagementLevel": int,
    "engagementScore": float,
    "valence": float,
    "arousal": float,
    "escalation": float,
    "rapport": float,
    "attention": float,
    "engagementTrend": float,
    "dominantStyle": str,
}

# End-of-session summary attributes. recoveryAttempts/recoverySuccesses are the
# NGSI-LD half of the engagement-recovery KPI: a consumer
# reading only Orion/QuantumLeap (no access to the robot's local store) can compute
# it from these two counters alone. Genuine ints, so QuantumLeap's BIGINT inference
# is correct and the FLOAT_SEED gotcha does not apply.
SUMMARY_ATTRS: dict[str, type] = {
    "sessionSummary": str,
    "avgEngagement": float,
    "recoveryAttempts": int,
    "recoverySuccesses": int,
}

# Session classification. A Property (not a type split) so one Session entity type
# keeps serving every EMORI mode; 'wellbeing' is what the training roleplay is.
DEFAULT_SESSION_TYPE = "wellbeing"

# Everything QuantumLeap should historize.
WATCHED_ATTRS: list[str] = [*METRIC_ATTRS, *SUMMARY_ATTRS]

FLOAT_SEED = 0.001  # see module docstring


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def session_urn(session_id: str) -> str:
    return f"urn:ngsi-ld:Session:{session_id}"


def person_urn(person_id: str) -> str:
    return f"urn:ngsi-ld:Person:{person_id}"


def person_entity(person_id: str, *, name: str = "", role: str = "learner") -> dict:
    entity = {
        "id": person_urn(person_id),
        "type": "Person",
        "role": {"type": "Property", "value": role},
        "@context": CORE_CTX,
    }
    if name:
        entity["name"] = {"type": "Property", "value": name}
    return entity


def session_entity(session_id: str, person_id: str, *, topic: str = "",
                   session_type: str = DEFAULT_SESSION_TYPE,
                   observed_at: str | None = None) -> dict:
    """A freshly seeded Session entity (all metric attrs present so QuantumLeap's
    column types are right from row one)."""
    now = observed_at or _now()
    entity: dict = {
        "id": session_urn(session_id),
        "type": "Session",
        "learner": {"type": "Relationship", "object": person_urn(person_id)},
        "topic": {"type": "Property", "value": topic or "training"},
        "sessionType": {"type": "Property", "value": session_type or DEFAULT_SESSION_TYPE},
        "@context": CORE_CTX,
    }
    for attr, typ in METRIC_ATTRS.items():
        if typ is int:
            value: object = 0
        elif typ is float:
            value = FLOAT_SEED
        else:
            value = "neutral"
        entity[attr] = {"type": "Property", "value": value, "observedAt": now}
    return entity


def metric_values(signals) -> dict:
    """Flatten DerivedSignals into the canonical attribute dict (one TS row)."""
    return {
        "engagementLevel": int(signals.engagement_level),
        "engagementScore": float(signals.engagement_score),
        "valence": float(signals.valence),
        "arousal": float(signals.arousal),
        "escalation": float(signals.context.escalation),
        "rapport": float(signals.context.rapport),
        "attention": float(signals.context.attention),
        "engagementTrend": float(signals.context.engagement_trend),
        "dominantStyle": signals.context.dominant_style or "neutral",
    }


def subscription_body(ql_notify_url: str, tenant: str,
                      description: str = "EMORI QuantumLeap historization") -> dict:
    """The (single) QuantumLeap subscription. ``description`` doubles as the
    idempotency key: writers look for an existing subscription with this
    description + endpoint before POSTing a new one."""
    return {
        "type": "Subscription",
        "description": description,
        "entities": [{"type": "Session"}],
        "watchedAttributes": WATCHED_ATTRS,
        "notification": {
            "attributes": WATCHED_ATTRS,
            "format": "normalized",
            "endpoint": {
                "uri": ql_notify_url,
                "accept": "application/json",
                "receiverInfo": [{"key": "fiware-service", "value": tenant}],
            },
        },
        "@context": CORE_CTX,
    }
