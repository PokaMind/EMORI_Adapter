# The analytics interface (FIWARE), and how an external consumer verifies it

What an external consumer needs in order to read an EMORI session without reading
EMORI's code. The check that it works is a script that does exactly that:
`scripts/arise_consumer_check.py` (the name is historical; it recomputes the
engagement-recovery KPI), which imports nothing from `emori_adapter` and speaks only the
standard library.

## The broker does not live on the robot

The robot writes; it does not host. `deploy/fiware/docker-compose.yml` runs on an
**analytics host** — a laptop, a VM, whatever you have — and the only thing that
changes on the robot is its FIWARE settings:

```ini
EMORI_FIWARE=1
ORION_URL=http://<analytics-host>:1026
NGSILD_TENANT=pokamind
```

`pokamind` is the default tenant. It is configurable through `NGSILD_TENANT`, and
QuantumLeap stores the history in the TimescaleDB schema `mt<tenant>` (so `mtpokamind` by
default). The provisioned Grafana dashboard queries `mtpokamind`, so change it too if you
change the tenant.

Nothing else moves. There is no ROS container in this path, no `fiware_bridge`, no
extra port on the robot: the hostapp's own NGSI-LD writer
(`src/emori_adapter/fiware.py`) is the producer, and it makes exactly three kinds
of outbound HTTP call to Orion-LD — create the `Person` and `Session` entities and
register the QuantumLeap subscription at session start, append the metric
attributes once per analysis cycle, append the summary attributes once at the end.

Two properties of that writer matter to a consumer:

* **It is best-effort.** A broker that is down degrades the dashboard and never
  the session. The hostapp probes `GET $ORION_URL/version` once per session (1 s)
  and silently becomes a no-op if nothing answers, recording a `fiware_skipped`
  event locally. So "no entity in Orion" means the broker was unreachable, not
  that the session did not happen.
* **The identities are already pseudonyms.** `urn:ngsi-ld:Session:S-xxxxxx` and
  `urn:ngsi-ld:Person:S-xxxxxx` are HMAC codes; the roster student id never
  reaches the broker. A consumer cannot re-identify a learner from this feed and
  is not supposed to be able to.

QuantumLeap is fed by Orion, not by the robot: Orion holds one subscription
(`deploy/fiware/subscriptions/quantumleap-subscription.json`, registered
idempotently by the writer) that notifies `http://quantumleap:8668/v2/notify` on
every watched attribute. That URL is resolved **on Orion's network**, which is why
it is a container name and not `localhost`.

## The consumer contract

| Thing | Value |
|---|---|
| Entity type | `Session` |
| Entity id | `urn:ngsi-ld:Session:<session-id>` |
| Tenant | `pokamind` — `NGSILD-Tenant` on Orion-LD, **`Fiware-Service` on QuantumLeap** |
| `@context` | `https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context-v1.8.jsonld`, as a `Link` header on GET |
| Related entity | `learner` → `urn:ngsi-ld:Person:<person-code>` |

Per-cycle attributes (one time-series row each, ~one row every 2 s):
`engagementLevel` (REP-155 int 0-4), `engagementScore`, `valence`, `arousal`,
`escalation`, `rapport`, `attention`, `engagementTrend`, `dominantStyle`.

End-of-session attributes: `sessionSummary`, `avgEngagement`, and the two that
carry the recovery KPI:

| Attribute | Meaning |
|---|---|
| `recoveryAttempts` | how many times the learner's attention dropped |
| `recoverySuccesses` | how many of those drops the robot pulled back within the window |

The figure is `recoverySuccesses / recoveryAttempts`, and it is **undefined, not
zero**, when `recoveryAttempts` is 0: a learner who never disengaged was not
failed to be recovered. For the same reason both attributes are *absent* rather
than 0 when the robot could not count them — a fabricated zero in either half
would drag a fleet-wide KPI down and nobody could tell why.

The canonical definitions of all of the above live in
`src/emori_adapter/fiware_schema.py`; the consumer check duplicates the handful of
literals it needs and `tests/test_arise_consumer_check.py` pins the duplicates
against the real ones.

### The recovery rule, in full

Both counters come from one state machine over the `engagementLevel` series
(`RecoveryTracker`, `src/emori_adapter/hostapp/metrics_store.py`):

* entering DISENGAGED(1) or DISENGAGING(4) **from anything else** is one attempt —
  a dip that lasts ten cycles counts once, not ten times;
* reaching ENGAGED(3) within **90 s** (`RECOVERY_WINDOW_S`) of that entry is one
  success, and it closes the attempt: at most one success per attempt;
* later than the window the attempt just expires. The learner came back, but not
  because the robot's nudge worked, and counting those would make the KPI mean
  nothing;
* the **first** sample of a session is never an attempt. There is no transition to
  observe yet, so there is nothing the robot could have caused.

This is computed from engagement levels alone, so it is independent of
`EMORI_ROS_MOTION_OWNER` — who moves the robot does not change who counts.

One wrinkle a consumer must know about: `session_entity()` seeds every metric
attribute when the entity is created, so the *first* historized `engagementLevel`
row of a session can be the seed value `0` (UNKNOWN). The robot's own tracker
never sees it, because the robot's engagement tracker commits to a real level on
cycle one and never reports UNKNOWN. A consumer that counts the seed turns the
robot's genuine first cycle into a transition and invents one attention drop per
session. `arise_consumer_check.py` drops leading UNKNOWN rows for exactly that
reason.

## Reading it

Orion-LD holds current state, QuantumLeap holds history, and they want
**different tenant headers**. That is the single most common reason a consumer
sees "No records found":

```bash
# current state (NGSI-LD)
curl -H "NGSILD-Tenant: pokamind" \
     "http://localhost:1026/ngsi-ld/v1/entities?type=Session&limit=5"

# history of one attribute (QuantumLeap's NGSI-TSDB reader)
curl -H "Fiware-Service: pokamind" -H "Fiware-ServicePath: /" \
     "http://localhost:8668/v2/entities/urn:ngsi-ld:Session:<id>/attrs/engagementLevel"
# -> {"attrName": "engagementLevel", "entityId": "...", "index": [...], "values": [...]}
```

## The check

```bash
python scripts/arise_consumer_check.py --session latest
```

It resolves the session (`latest` = the most recently touched `Session` entity in
the tenant, or pass a full URN or a bare session id), reads
`recoveryAttempts`/`recoverySuccesses` off the entity, reads the whole
`engagementLevel` series out of QuantumLeap, recomputes the two counters from that
series with the rule above re-implemented from scratch, and compares. Output on
stdout is exactly:

```json
{
  "session": "urn:ngsi-ld:Session:S-ab12cd-YYYYMMDD",
  "recoveryAttempts": 2,
  "recoverySuccesses": 1,
  "figure": 0.5,
  "series_attempts": 2,
  "series_successes": 1,
  "series_figure": 0.5,
  "agree": true
}
```

plus a one-line human verdict on stderr. Exit codes:

| Code | Meaning |
|---|---|
| 0 | the two agree — or the summary is absent and the series is the only source |
| 1 | they disagree, **or** the series could not be read at all |
| 2 | there is no such session |

Agreement is on the two counters, not on the ratio: 3/2 and 6/4 are the same
figure, and a feed that silently lost half its rows must not pass. An unread
series is never agreement either — proving nothing is not the same as proving
they match.

Other flags: `--orion-url`, `--ql-url`, `--tenant`, `--service-path`, `--timeout`,
and `--window-s` (default 90.0). `--window-s` must match the producer's
`RECOVERY_WINDOW_S`; scoring the same series against a different window produces a
disagreement that is the consumer's own fault.

## What this proves, and what it does not

It proves the analytics interface is *self-sufficient*: a party with the broker and
no EMORI source can recompute the recovery figure and can audit it against the
raw series rather than trusting a number the robot asserted. It does not prove the
figure is a good measure of anything — that is the study's question, not the
interface's.

## See also

* `deploy/fiware/README.md` — running the stack on a non-robot host.
* `src/emori_adapter/fiware_schema.py` — the canonical model every writer derives
  from.
