# EMORI Adapter — architecture

## The three EMORI layers

| Layer | Role | This repo |
|---|---|---|
| **Perception** | Reachy Mini sensors + Pokamind multimodal analysis | consumes Pokamind's **live per-cycle analysis** via the hostapp hub (mock / CSV / JSONL replay for dev) |
| **Understanding** | engagement / communication-style / context inference | `emori_adapter.translation` + `hri_context_analyzer` |
| **Interaction** | robot behaviour + kiosk + analytics | hostapp driver + `emori_adapter.behavior` + `hri_engagement_behaviors` + `fiware_bridge` |

This repository contains the Pokamind-to-ROS4HRI adapter, the ROS 2 packages and the
FIWARE bridge.

## Development topology: native simulator on Windows, ROS 2 in Docker, joined by HTTP

Why:
- **MuJoCo renders ~60 fps native on Windows but ~1–2 fps under WSLg** → the sim stays on Windows.
- **The Reachy daemon speaks HTTP/WebSocket on `localhost:8000`, not DDS** → the Windows sim and the Linux ROS 2 container talk over a trivial HTTP hop; no WSL2↔Windows DDS discovery.
- **ROS4HRI is Linux-only** → ROS 2 runs in one Docker container, keeping its DDS graph self-contained.
- **Python 3.12 for the sim venv** (`mujoco==3.3.0` has no 3.14 wheel); the package itself
  installs on `>=3.10,<3.14` — the CM4's Debian Trixie ships **Python 3.13** and is supported.

## The hostapp — ONE live driver, one event spine

The host application (`src/emori_adapter/hostapp/`) is the single component that
drives the robot during a live session, and the producer for every downstream consumer. On a
robot it runs over ROS 2 in the `emori-hub` container, and reaches the motor container through
`emori_hub`'s robot link in place of daemon REST; the same program runs as `emori-hostapp` on
the simulator, and on a robot as the way back:

```
 kiosk Chromium ── /session page (Pokamind iframe) ──POST /api/cycle──►  emori-hostapp
                   relays every pokamind_* postMessage                  (the hub, :8080)
                                                                              │
   /api/cycle stamps the session identity (minted at SSO time as             │ fan-out
   <student>-<utctimestamp>, activated when /session?sid= loads),             ▼
   sanitizes null body values, then fans each envelope out to:
     1. the in-process robot driver   Translator → BehaviorEngine → RobotPlayer → daemon REST
     2. FIWARE                        per-session NGSI-LD entities (per-cycle push + summary;
                                      canonical schema in fiware_schema.py, idempotent QL sub)
     3. /ws/relay broadcast           ROS 2 live publisher (source:=pokamind),
                                      emori-run --source pokamind, debuggers
     4. JSONL recorder                artifacts/recordings/<session>.jsonl (EMORI_RECORD=1)
     5. session tracker               localized (ca/en) spoken summary (+ /api/tts URL),
                                      results email (EMORI_SMTP_* or artifacts/outbox/),
                                      kiosk loops back to '/' for the next learner
```

All robot speech flows through the kiosk Chromium: `GET /api/tts` renders WAVs
server-side with Piper (`voices/{ca,en}.onnx`, `scripts/get_voice_models.ps1`/`.sh`)
and the pages play them (fallback: browser `speechSynthesis` when Piper is absent). STT is
`POST /api/stt` (16 kHz PCM) with server-side Vosk (`models/vosk-{ca,en}`; fallback: Web
Speech). There is **no daemon audio-upload path** — the daemon runs `--no-media` and does
motion only. An idle ticker (`EMORI_IDLE_AFTER_S=8`) keeps the robot visibly alive when
cycles stall; `GET /api/status` exposes live counters (cycles, utterances + dedups, parse
errors, sessions, idle ticks, relay clients, robot/tts/stt health).

## Two vertical slices

### Slice 1 — Windows, no ROS (the fast inner loop + the live kiosk)
```
dev loop : MockPokamindSource | CsvReplaySource ─► Translator ─► BehaviorEngine
                                                       ─► HttpRobotClient ─► daemon REST ─► MuJoCo robot     (emori-run)

live     : Pokamind iframe ─postMessage─► /api/cycle (hostapp hub) ─► the same Translator/
           BehaviorEngine in-process ─► daemon REST ─► robot
                                 └─► /ws/relay ─► emori-run --source pokamind (observer)
```
Pure Python, unit-tested. `MockPokamindSource(shape="live")` emits the exact staged
Pokamind contract; `shape="rich"` adds the fields the live session does not send (facial,
tone, verbal, prosody) for development. One command boots the
complete deployed-robot topology in simulation: `scripts/sim_full_up.ps1`
(optionally `-Replay tests/fixtures/live_session.jsonl` for a deterministic run).

### Slice 2 — ROS 2 Humble in Docker (the standards-aligned graph)

The workspace has two halves, all of it Apache-2.0 ([`LICENSING.md`](../LICENSING.md)).
`ros2_ws/src/open/` holds standalone packages that never import `emori_adapter` and never
depend on a package under `ros2_ws/src/emori/`, which holds the packages that use the
`emori_adapter` core:

| `ros2_ws/src/open/` (standalone, imports nothing from `emori_adapter`) | `ros2_ws/src/emori/` (uses the `emori_adapter` core) |
|---|---|
| `emori_msgs`, `hri_context_analyzer`, `hri_engagement_behaviors`, `emori_skill_facade` — the v1 graph's open half | `emori_adapter_ros` — the bridge from the core's session model onto the wire |
| `reachy_mini_msgs`, `reachy_mini_ros2` — the robot itself as a node (v2) | `fiware_bridge`, `emori_adapter_bringup` (the launch files), `emori_skill_native` (v2's `ros_native` transport) |

`deploy/ros2/Dockerfile.standalone` builds the first four standalone packages and starts their
three nodes with no EMORI core installed at all. v2 — the robot as a native ROS 2 node,
no Pollen daemon — is its own write-up: [ros2-v2-native-robot.md](ros2-v2-native-robot.md).

```
 emori_adapter_ros (publisher, source:=mock|pokamind)
   ▲ source:=pokamind consumes ws://<hostapp>:8080/ws/relay — reconnects across sessions,
     fresh Translator per session id; stable ROS person namespace (learner_01), the TRUE
     per-session identity travels inside emori_msgs/Context
   ├─ /humans/persons/<id>/engagement_status   (hri_msgs/EngagementLevel)
   ├─ /humans/persons/<id>/context             (emori_msgs/Context)
   ├─ /humans/persons/tracked                  (hri_msgs/IdsList, latched)
   └─ /humans/persons/<id>/anonymous           (std_msgs/Bool, latched)
          │
          ├──► hri_context_analyzer ──► /humans/persons/<id>/context/{escalation,rapport,attention,engagement_trend,dominant_style}
          │
          ├──► hri_engagement_behaviors ──/skill/*──► emori_skill_facade
          │       ──POST /api/skill──► the hostapp's one RobotPlayer queue ──► daemon
          │       (only when EMORI_ROS_MOTION_OWNER=ros; the default `hostapp` makes the
          │        facade suppress every request. No ROS node holds a daemon URL.)
          │
          └──► fiware_bridge ──NGSI-LD──► Orion-LD ─sub─► QuantumLeap ─► TimescaleDB ─► Grafana
                  (FIWARE_BRIDGE_ENABLED=0 on live stacks — the hostapp is the writer;
                   entities keyed off Context.person_id/session_id, reuses the canonical FiwareClient)
```
The nodes share one container (local DDS); the skill facade and the FIWARE bridge reach
the Windows host via `host.docker.internal`. The container installs `.[pokamind]` so the
live relay source works out of the box. Both the behaviour node and the facade publish a
counter ledger as `diagnostic_msgs/DiagnosticArray` on `/diagnostics` every 5 s, which is
how a run is audited from outside without reading logs.

**Who moves the robot** is one knob, `EMORI_ROS_MOTION_OWNER` (`hostapp` default / `ros` /
`none`), enforced on both sides: the facade refuses to POST and the hostapp intake answers
409 `not_owner`. The graph always computes and always publishes, so the knob changes who
executes, never who decides. All three modes can be rehearsed end to end in the
simulator: see [motion-owner-rehearsal.md](motion-owner-rehearsal.md).

## Why the core is ROS-free

`src/emori_adapter/` has **no ROS import**. The Translator and BehaviorEngine are plain
Python so they (a) run in Slice 1 without ROS, (b) are unit-tested in CI without a ROS
install, (c) are imported unchanged inside the Slice-2 ROS nodes, and (d) power the
hostapp's in-process live driver. The ROS packages are thin wrappers: decode topics →
call the core → publish topics / HTTP.

## Data model (one cycle)

```
AnalysisCycle  →  Translator  →  DerivedSignals  →  BehaviorEngine  →  [MotionIntent]
(Pokamind shape)                 (engagement /                         (robot-agnostic
                                  valence-arousal /                     head/antenna/sound
                                  context)                              commands)
```
See `src/emori_adapter/models.py`. Live envelopes pass through `sanitize_envelope`
first: Pokamind sends `null` body values when an analysis cycle fails, and those are
dropped rather than crashing validation. Translation is calibrated for the live shape —
adaptive GroupBaseline style-channel scaling and real Pokamind body-vocabulary
normalization, tuned via the `translation:` section of `configs/behavior.yaml` (loaded
by the hostapp, `emori-run --config`, and the ROS behaviour node).

## How to verify

- **Unit and replay tests**: `python -m pytest tests -p no:cacheprovider`. The suite needs
  no ROS and no robot; `tests/test_e2e_replay.py` replays `tests/fixtures/live_session.jsonl`
  through the driver, FIWARE, relay, recorder, summary and email in-process
  (`scripts/replay_session.py` does the same from the command line).
- **The whole topology in simulation**: `scripts/sim_full_up.ps1` starts the daemon
  (`--sim --no-media`), FIWARE, the ROS 2 graph and the hostapp; add
  `-Replay tests/fixtures/live_session.jsonl` for a deterministic run. Grafana ships a
  provisioned "EMORI Session" dashboard (`http://localhost:3003/d/emori-session`), and
  `scripts/smoke_daemon.py` checks the robot over REST.
- **Who moves the robot**: `scripts/rehearse_motion_owner.py` runs the topology for each
  `EMORI_ROS_MOTION_OWNER` and checks the assertions described in
  [motion-owner-rehearsal.md](motion-owner-rehearsal.md).
  `scripts/outside_consumer_check.py` reads the REP-155 topics from a container with no
  EMORI code, `scripts/arise_consumer_check.py` recomputes the recovery figure from
  Orion-LD and QuantumLeap ([analytics-interface.md](analytics-interface.md)), and
  `scripts/ros_standalone_check.py` builds and starts the standalone packages with no core.
- **On a robot after a deploy**: `scripts/rig_smoke.py` (run on the robot by
  `deploy/robot/rig-smoke.sh`) drives the control plane and roleplay end to end (and any installed add-on's own checks); see
  [deploy-a-new-robot.md](deploy-a-new-robot.md).

## Known limitations / next steps

- The **QuantumLeap REST query API** is unreliable for NGSI-LD + TimescaleDB (a known
  maturity gap); the historian stores correctly and **Grafana/SQL** is the supported
  query path. CrateDB or Orion-LD TRoE+Mintaka are alternatives if the QL query API is
  needed.
- `pyhri` is demonstrated (`pyhri_demo`) but off the behaviour critical path (which uses
  plain rclpy) so the behaviour node runs even where `ros-humble-pyhri` isn't available.
- In v1 the robot's own state is not on the ROS graph (the daemon speaks HTTP only); v2's
  `reachy_mini_ros2` publishes `/joint_states`, `/tf` and a motion status
  ([ros2-v2-native-robot.md](ros2-v2-native-robot.md)), and v1 stays the default.
