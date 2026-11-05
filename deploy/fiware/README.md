# The FIWARE analytics stack

Orion-LD + QuantumLeap + TimescaleDB + Grafana, as one compose project
(`emori-fiware`). This is where EMORI session data is historized and read; **it
does not run on the robot.** The robot is a client of it.

```
hostapp (robot)  --NGSI-LD-->  Orion-LD :1026  --subscription-->  QuantumLeap :8668
                                    |                                   |
                              MongoDB :27017                     TimescaleDB :5432
                                                                        |
                                                                  Grafana :3003
```

## Run it on a non-robot host

```bash
docker compose -f deploy/fiware/docker-compose.yml up -d

curl http://localhost:1026/version     # Orion-LD
curl http://localhost:8668/version     # QuantumLeap  -> {"version": "1.0.0"}
```

Grafana comes up on <http://localhost:3003> (`admin` / `emori`, anonymous viewing
on) with the "EMORI Session" dashboard provisioned at `/d/emori-session`.

The credentials in `docker-compose.yml` (Grafana `admin`/`emori`, TimescaleDB
`postgres`/`postgres`) are local lab defaults: change `GF_SECURITY_ADMIN_PASSWORD`
and `POSTGRES_PASSWORD` (and the matching `password` in
`grafana/provisioning/datasources/timescale.yaml`) before the stack is reachable from
anything but a trusted network.

| Service | Host port | Container | Notes |
|---|---|---|---|
| Orion-LD | 1026 | `emori-orion` | NGSI-LD broker; the robot writes here |
| MongoDB | 27017 | `emori-mongo` | Orion's own store |
| QuantumLeap | 8668 | `emori-quantumleap` | time series; the consumer reads here |
| TimescaleDB | 5432 | `emori-timescale` | `postgres`/`postgres` (lab default), db `quantumleap` |
| Grafana | 3003 | `emori-grafana` | dashboards |

Tear down with `down`, or `down -v` to also drop the two volumes (`mongo-data`,
`ts-data`) and wipe the history.

## Opening the ports to the robot

The compose file publishes on all interfaces, so on the same LAN there is nothing
further to configure in Docker — only the host firewall and the robot's config.

**Robot side**, one line in its `.env`:

```ini
EMORI_FIWARE=1
ORION_URL=http://<analytics-host>:1026
NGSILD_TENANT=pokamind
```

Nothing else on the robot changes. `EMORI_FIWARE=1` is safe to leave on with no
broker reachable: the hostapp probes `GET $ORION_URL/version` once per session and
degrades to a silent no-op, recording a `fiware_skipped` event instead.

The tenant is whatever `NGSILD_TENANT` says (`pokamind` above), and QuantumLeap keeps
its tables in the TimescaleDB schema `mt<tenant>` (here `mtpokamind`). The commands and
dashboard below use that value; change them together if you change the tenant.

**Analytics host firewall.** Only **1026** needs to be reachable from the robot.
8668, 5432 and 3003 are for the analysts, and 27017 should never leave the host.

For example, with `<robot-subnet>/24` replaced by the robot's network:

```powershell
# Windows: allow only the robot's subnet to reach Orion
New-NetFirewallRule -DisplayName "EMORI Orion-LD" -Direction Inbound -Protocol TCP `
  -LocalPort 1026 -RemoteAddress <robot-subnet>/24 -Action Allow
```

```bash
# Linux (ufw), same restriction
sudo ufw allow from <robot-subnet>/24 to any port 1026 proto tcp
```

> Neither Orion-LD nor QuantumLeap authenticates anything here. This stack is for a
> trusted lab network or a VPN, never a public address. The one mitigation baked in
> is that the robot pseudonymises identities *before* they become entity ids, so
> the broker holds `S-xxxxxx` codes and no roster identity — see
> `docs/analytics-interface.md`.

`QL_NOTIFY_URL` stays `http://quantumleap:8668/v2/notify`: the subscription
endpoint is resolved from **Orion's** network, not from yours.

## What is in this directory

| Path | What it is |
|---|---|
| `docker-compose.yml` | the five services above |
| `entities/{person,session}.json` | seed entities, for a broker you want populated without a session |
| `subscriptions/quantumleap-subscription.json` | the single historization subscription |
| `ngsi-context.jsonld` | the EMORI term definitions, for a consumer that wants expanded names |
| `grafana/provisioning/` | the TimescaleDB datasource and the session dashboard |

`entities/` and `subscriptions/` are **generated** from the canonical schema —
`python scripts/gen_fiware_fixtures.py` rewrites them from
`src/emori_adapter/fiware_schema.py`, so a fixture can never drift from what the
writers actually produce. Edit the schema, not the JSON.

Seeding a fresh broker by hand (the writers do this themselves at session start,
idempotently — this is only for a broker you want populated before any session):

```bash
for f in entities/person.json entities/session.json; do
  curl -X POST http://localhost:1026/ngsi-ld/v1/entities/ \
       -H "Content-Type: application/ld+json" -H "NGSILD-Tenant: pokamind" \
       -d @deploy/fiware/$f
done
curl -X POST http://localhost:1026/ngsi-ld/v1/subscriptions/ \
     -H "Content-Type: application/ld+json" -H "NGSILD-Tenant: pokamind" \
     -d @deploy/fiware/subscriptions/quantumleap-subscription.json
```

## Reading data back

QuantumLeap stores one table per entity type per tenant, in schema `mt<tenant>` —
so `mtpokamind.etsession`, keyed by `entity_id` over `time_index`. That is what the
Grafana panels query.

Over HTTP, and note the **different tenant header on each broker**:

```bash
curl -H "NGSILD-Tenant: pokamind" "http://localhost:1026/ngsi-ld/v1/entities?type=Session"
curl -H "Fiware-Service: pokamind" "http://localhost:8668/v2/entities"
```

The end-to-end proof that this interface is usable by an outside party:

```bash
python scripts/arise_consumer_check.py --session latest
```

See `docs/analytics-interface.md` for the consumer contract and what that check
asserts.
