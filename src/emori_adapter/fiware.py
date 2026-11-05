"""Lightweight NGSI-LD client so an EMORI session can historize to FIWARE directly
(no ROS container needed). Used by the hostapp live driver (per-session entities)
and the emori-session orchestrator; the ROS ``fiware_bridge`` writes the same
canonical schema (:mod:`emori_adapter.fiware_schema`).

* create the ``Session`` + ``Person`` entities in Orion-LD,
* register the QuantumLeap subscription (idempotently — a restart must NOT add a
  duplicate subscription, which would multiply every time-series row),
* POST metric attributes per cycle (each call = one time-series row),
* write an end-of-session summary attribute.

All calls are best-effort and never raise into the caller — FIWARE being down
degrades the dashboard, never the session.
"""

from __future__ import annotations

import logging
import re

import httpx

from emori_adapter import fiware_schema as schema

log = logging.getLogger("emori.fiware")

CORE_CTX = schema.CORE_CTX


def _now() -> str:
    return schema._now()


def broker_reachable(orion_url: str, *, timeout: float = 1.0) -> bool:
    """Cheap liveness probe: is there an NGSI-LD broker at ``orion_url`` at all?

    ``EMORI_FIWARE=1`` is the shipped default, but most robots run on a LAN with no
    Orion-LD. Without this probe every session start paid three HTTP timeouts and
    left a wall of scary WARNINGs that mean nothing. One 1 s GET /version answers
    the question, so the flag can safely be left on.

    MUST NOT be called from the asyncio event loop -- it is a blocking httpx call.
    Production reaches it only via LiveDriver._start_fiware, which runs the whole
    factory on the 'fiware-session-start' daemon thread (driver.py).
    """

    try:
        r = httpx.get(f"{orion_url.rstrip('/')}/version", timeout=timeout)
    except httpx.HTTPError:
        return False
    # Any answer at all proves something is listening and speaking HTTP; Orion-LD
    # returns 200, and a proxy in front of it may well answer 401/404.
    return r.status_code < 500


def _problem_shape(r) -> str:
    """A body-free description of an error response.

    An NGSI-LD broker echoes the OFFENDING PAYLOAD back in the ``detail`` member
    of its ProblemDetails, so printing ``r.text`` beside an entity id prints the
    identity that was in it -- ``"name": {"value": "Anna Example"}`` for the Person
    entity a badge session creates.  ``type`` is a URI from a FIXED registry
    (``https://uri.etsi.org/ngsi-ld/errors/BadRequestData`` and its siblings), so
    it answers "what did the broker object to" and can carry nothing of ours;
    the content type is the fallback when there is no problem document at all.
    """

    try:
        payload = r.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is simply not a problem document
        payload = None
    if isinstance(payload, dict):
        kind = payload.get("type")
        if isinstance(kind, str) and kind:
            return kind[:80]
    return str(r.headers.get("content-type") or "?")[:80]


def _attr_names(attrs: dict) -> str:
    """The attribute NAMES in an append, rendered safely.

    Names are ours on every in-repo path (``push`` derives them from the
    canonical schema, ``write_summary`` writes four literals), but ``push_attrs``
    takes an already-flattened dict from the ROS bridge, so the one caller that
    is not this file supplies the keys.  An NGSI-LD attribute name is an
    identifier; anything that is not one is reported as its shape rather than
    printed, so this stays a diagnostic and never becomes a way for an upstream
    message to write free text into the journal.
    """

    names = [k if isinstance(k, str) and _ATTR_NAME_RE.fullmatch(k) else "?"
             for k in sorted(attrs, key=str)]
    return ",".join(names[:20]) or "-"


_ATTR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class FiwareClient:
    def __init__(
        self,
        *,
        orion_url: str = "http://localhost:1026",
        tenant: str = "pokamind",
        session_id: str = "sim-session",
        person_id: str = "learner_01",
        person_name: str = "",
        ql_notify_url: str = "http://quantumleap:8668/v2/notify",
        timeout: float = 5.0,
    ) -> None:
        self.orion = orion_url.rstrip("/")
        self.tenant = tenant
        self.session_id = session_id
        self.person_id = person_id
        self.person_name = person_name
        self.session_urn = schema.session_urn(session_id)
        self.person_urn = schema.person_urn(person_id)
        self.ql_notify_url = ql_notify_url
        self._hdr = {"Content-Type": "application/ld+json", "NGSILD-Tenant": tenant}
        self._http = httpx.Client(timeout=timeout)
        self.enabled = True

    # --- setup ---
    def ensure_session(self, topic: str = "") -> None:
        person = schema.person_entity(self.person_id, name=self.person_name)
        session = schema.session_entity(self.session_id, self.person_id, topic=topic)
        for e in (person, session):
            self._create_entity(e)
        self._create_subscription()

    def _create_entity(self, entity: dict) -> None:
        try:
            r = self._http.post(f"{self.orion}/ngsi-ld/v1/entities/", json=entity, headers=self._hdr)
            # The TYPE, never the id: ``ensure_session`` creates exactly one
            # Person and one Session, so "Person created / Session created" is
            # the whole diagnostic, while ``urn:ngsi-ld:Person:anna-001`` would
            # write an SSO student id into a journal that is world-readable and
            # survives reboots.  (The session URN is de-identified upstream --
            # server.py:_default_fiware_factory -- and the person one is a
            # pseudonym for the same reason.)
            kind = entity.get("type") or "entity"
            if r.status_code in (201, 204, 409):
                log.info("entity %s %s", kind, "exists" if r.status_code == 409 else "created")
            else:
                # 4xx/5xx (not just network errors) means the entity isn't there; disable so we
                # don't silently write to a non-existent entity and lose the whole session's metrics.
                # Never a broker BODY here: a validation error echoes the payload
                # it rejected, which for the Person entity is the staff member's
                # name.  See :func:`_problem_shape`.
                log.warning("create %s -> %s (%s); disabling FIWARE",
                            kind, r.status_code, _problem_shape(r))
                self.enabled = False
        except httpx.HTTPError as e:  # noqa: BLE001
            log.warning("Orion unreachable (entity): %s", e)
            self.enabled = False

    def _subscription_exists(self, sub: dict) -> bool:
        """True if an equivalent subscription already exists (same description +
        notification endpoint). Orion-LD always assigns a fresh id on POST, so the
        409 branch never fires — this lookup is the real idempotency guard."""
        try:
            r = self._http.get(f"{self.orion}/ngsi-ld/v1/subscriptions/",
                               headers={"NGSILD-Tenant": self.tenant})
            if r.status_code != 200:
                return False
            target = sub["notification"]["endpoint"]["uri"]
            for existing in r.json():
                if (existing.get("description") == sub.get("description")
                        and existing.get("notification", {}).get("endpoint", {}).get("uri") == target):
                    return True
        except (httpx.HTTPError, ValueError):  # noqa: BLE001
            pass
        return False

    def _create_subscription(self) -> None:
        sub = schema.subscription_body(self.ql_notify_url, self.tenant)
        try:
            if self._subscription_exists(sub):
                log.info("QuantumLeap subscription already registered; not duplicating")
                return
            r = self._http.post(f"{self.orion}/ngsi-ld/v1/subscriptions/", json=sub, headers=self._hdr)
            if r.status_code in (201, 204):
                log.info("QuantumLeap subscription registered")
            else:
                log.warning("subscription -> %s (%s)", r.status_code, _problem_shape(r))
        except httpx.HTTPError as e:  # noqa: BLE001
            log.warning("Orion unreachable (subscription): %s", e)

    # --- per-cycle + summary ---
    def push(self, signals) -> None:
        if not self.enabled:
            return
        self._set_attrs(schema.metric_values(signals))

    def push_attrs(self, values: dict) -> None:
        """Write an already-flattened canonical attribute dict (the ROS bridge path,
        where the values come from an emori_msgs/Context message, not DerivedSignals)."""
        if not self.enabled:
            return
        self._set_attrs(values)

    def write_summary(self, text: str, avg_engagement: "float | None", *,
                      recovery_attempts: int | None = None,
                      recovery_successes: int | None = None) -> None:
        if not self.enabled:
            return
        attrs: dict = {"sessionSummary": text}
        # Absent, never 0.0, when no analysis cycle ever arrived (a covered lens, an
        # audio-only conversation, a camera path that is down): a fleet KPI has to be
        # able to tell "nobody was engaged" from "nobody measured". Exactly the rule
        # the recovery counters below have always followed.
        if avg_engagement is not None:
            attrs["avgEngagement"] = float(avg_engagement)
        # Written only when the caller actually counted them: a session whose local
        # store was unavailable must publish "unknown" (attribute absent), never a
        # fabricated 0 that would drag the fleet-wide recovery KPI down.
        if recovery_attempts is not None:
            attrs["recoveryAttempts"] = int(recovery_attempts)
        if recovery_successes is not None:
            attrs["recoverySuccesses"] = int(recovery_successes)
        self._set_attrs(attrs)

    def _set_attrs(self, attrs: dict) -> None:
        # POST .../attrs appends NEW attributes and updates existing ones (PATCH /attrs/{x}
        # cannot create a missing attribute -> 404). One observedAt per call = one TS row.
        now = _now()
        body = {k: {"type": "Property", "value": v, "observedAt": now} for k, v in attrs.items()}
        body["@context"] = CORE_CTX
        try:
            r = self._http.post(f"{self.orion}/ngsi-ld/v1/entities/{self.session_urn}/attrs",
                                json=body, headers=self._hdr)
            # NGSI-LD append returns 204 (all updated) or 207 (partial - some attrs not updated).
            #
            # Never the BODY, on either branch. A 207 answers with `notUpdated`, whose
            # members quote the attribute VALUE the broker refused -- and one of the
            # attributes travelling here is `sessionSummary`, the session's own summary
            # text. A rejected append would therefore write the conversation into a
            # journal that is world-readable and survives reboots, which is precisely
            # what every other line in this module is careful not to do. The attribute
            # NAMES are ours, so they are safe and they are the whole diagnostic:
            # "which attributes went, what did the broker call the problem".
            if r.status_code == 207:
                log.warning("set attrs partial failure (207) on %s (%s)",
                            _attr_names(attrs), _problem_shape(r))
            elif r.status_code >= 400:
                log.debug("set attrs -> %s (%s)", r.status_code, _problem_shape(r))
        except httpx.HTTPError as e:  # noqa: BLE001
            log.debug("set attrs failed: %s", e)

    def close(self) -> None:
        self._http.close()


class NullFiwareClient:
    """No-op FIWARE sink (when --no-fiware)."""

    enabled = False

    def ensure_session(self, topic: str = "") -> None: ...
    def push(self, signals) -> None: ...
    def push_attrs(self, values: dict) -> None: ...
    def write_summary(self, text: str, avg_engagement: "float | None", *,
                      recovery_attempts: int | None = None,
                      recovery_successes: int | None = None) -> None: ...
    def close(self) -> None: ...
