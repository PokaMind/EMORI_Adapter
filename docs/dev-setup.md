# Developer setup

## Golden rules

1. **Python 3.12 for the sim venv, never 3.14.** `reachy-mini[mujoco]` pins
   `mujoco==3.3.0`, which has **no Python 3.14 wheel**, and the package itself requires
   `>=3.10,<3.14` (the CM4's Debian Trixie ships 3.13, which is supported). Whatever
   your system `python` is, pin the venv: `uv venv .venv-reachy --python 3.12`.
2. **Sim runs natively on Windows** (MuJoCo is ~60 fps native vs ~1–2 fps under WSLg).
3. **ROS 2 runs in Docker** (ROS4HRI is Linux-only). The container reaches the Windows
   host via `host.docker.internal`.
4. **The Reachy daemon must bind `0.0.0.0` for the ROS 2 container** to reach it:
   `reachy-mini-daemon --sim --fastapi-host 0.0.0.0 --no-localhost-only`. If the
   container can't connect, allow Python/uvicorn through **Windows Firewall** (inbound,
   ports 8000 / 1026 / 8668), or set the rule for "Docker Desktop Backend".
5. **The hostapp owns the only queue that moves the robot.** No ROS node holds a daemon
   URL; motion leaves the graph as a bounded request to the hostapp's `/api/skill`.
   `EMORI_ROS_MOTION_OWNER` (`hostapp` default / `ros` / `none`) picks which lane may
   send one, and it is enforced on both sides — never let two processes command one
   head. On live stacks also set `FIWARE_BRIDGE_ENABLED=0`: the hostapp is the single
   FIWARE writer and two writers double every time-series row.

## Requirements

- **Python 3.12** for the simulator venv (`reachy-mini[mujoco]` pins `mujoco==3.3.0`);
  Python 3.10 to 3.13 for the package without the simulator.
- [uv](https://docs.astral.sh/uv/) to create the venv and install into it.
- **Docker** with Compose, for the ROS 2 images and the FIWARE stack.
- **git**.

## Full local setup

```powershell
cd <repo root>

# 1) sim venv + SDK + this package (hostapp hub + live source + Piper TTS + Vosk STT)
uv venv .venv-reachy --python 3.12
uv pip install --python .venv-reachy "reachy-mini[mujoco]"
uv pip install --python .venv-reachy -e ".[dev,hostapp,pokamind,speech,stt]"
.\scripts\get_voice_models.ps1           # voices/{ca,en}.onnx + models/vosk-{ca,en}

# 2) tests (pure-Python core + hostapp + e2e replay + the pure halves of the ROS
#    nodes, loaded by path with guarded rclpy imports; no robot/ROS needed).
#    Keep the sim daemon and Orion-LD DOWN while the suite runs.
.\.venv-reachy\Scripts\python.exe -m pytest -p no:cacheprovider tests
#    NB: pyproject sets addopts="-q", so adding another -q hides the final count line.

# 3) lint
.\.venv-reachy\Scripts\ruff.exe check src tests

# 4) the basic dev loop: simulator + mock source (see docs/guides/01 + 02)
.\.venv-reachy\Scripts\reachy-mini-daemon.exe --sim
.\.venv-reachy\Scripts\emori-run.exe --source mock --robot http

# 5) THE one-command full topology (daemon --sim --no-media + FIWARE + ROS 2 graph
#    + hostapp hub on :8095) — optionally with a deterministic replay:
.\scripts\sim_full_up.ps1
.\scripts\sim_full_up.ps1 -Replay tests\fixtures\live_session.jsonl
#    -MotionOwner ros hands expressive motion to the ROS graph: it mounts /api/skill
#    with a generated -SkillKey and starts scripts\dev\loopback_proxy.py on -ProxyPort
#    8096, because Docker Desktop cannot host-network and the intake refuses any
#    caller that is not loopback. See docs/motion-owner-rehearsal.md.
.\scripts\sim_full_up.ps1 -MotionOwner ros

# 6) the ROS 2 graph + FIWARE by hand instead (see docs/guides/02)
.\.venv-reachy\Scripts\reachy-mini-daemon.exe --sim --fastapi-host 0.0.0.0 --no-localhost-only
docker compose -f deploy/fiware/docker-compose.yml up -d
docker build -t emori/ros2-humble:latest -f deploy/ros2/Dockerfile .
docker compose -f deploy/ros2/docker-compose.yml up
#    EMORI_SOURCE=pokamind EMORI_WS_URL=ws://host.docker.internal:8095/ws/relay switches
#    the publisher to the live relay; on live stacks also set FIWARE_BRIDGE_ENABLED=0.

# 7) v2: the robot itself as a native ROS 2 node (MuJoCo inside the container, REST shim
#    on :8000). STOP the Windows reachy-mini-daemon first: both want :8000 and the
#    failure is silent. See docs/ros2-v2-native-robot.md.
docker compose -f deploy/ros2/docker-compose.v2.yml up --build
curl -s localhost:8000/api/daemon/status     # "backend": "reachy_mini_ros2" only on v2

# 8) the self-contained scripted session (no Pokamind at all): topic -> spoken opening ->
#    live-analysis motion + "talking" -> AI turns -> spoken summary -> FIWARE
.\.venv-reachy\Scripts\emori-session.exe --robot http --orion-url http://localhost:1026
```

Smaller loops: `emori-run --source mock --robot dry --no-realtime` is logic only (no robot,
no network); `emori-run --source pokamind` reads the hostapp's relay (`POKAMIND_WS_URL`,
code default `ws://localhost:8080/ws/relay`); `--config configs/behavior.yaml` loads the
behaviour + translation tuning.

### Known test-suite noise

- `tests/test_hostapp.py::test_robot_side_walkaway_finalize_sends_the_session_page_home`
  is timing-sensitive and fails intermittently; re-run it alone before investigating.
- `tests/test_robot_player_voice.py::test_a_speech_plan_waits_for_the_voice_then_moves`
  is timing-sensitive under full-suite load; it passes alone.
- Otherwise the suite and `ruff check src tests` are clean, so a new failure or a new
  ruff finding is caused by your change.

### Other scripts worth knowing

| Script | What it does |
|---|---|
| `scripts/sim_up.ps1` | Just the MuJoCo daemon (`--sim`), the first step of guide 01 |
| `scripts/replay_session.py` | Replays a recorded `artifacts/recordings/*.jsonl` session through a running hostapp |
| `scripts/rehearse_motion_owner.py`, `scripts/rehearse_on_robot.py` | The motion-owner rehearsal, in the sim or against a robot ([motion-owner-rehearsal.md](motion-owner-rehearsal.md)) |
| `scripts/ros_standalone_check.py` | Builds `Dockerfile.standalone` and checks the open packages run with no EMORI core |
| `scripts/outside_consumer_check.py`, `scripts/arise_consumer_check.py` | EMORI-free consumers of the ROS topics and of the FIWARE analytics |
| `scripts/build_emori_*_graph.py` | Regenerate the SVG/PNG figures in `docs/architecture/` (never hand-edit those) |

A **live Pokamind session** (real analysis, real AI voice) needs a Pokamind robot token
(and `POKAMIND_BASE` only if you are not using the public API): see [guides/02-start-a-session.md](guides/02-start-a-session.md) and
[deploy-a-new-robot.md](deploy-a-new-robot.md). For the physical robot:
[onrobot-runbook.md](onrobot-runbook.md).

### Live-mode flow (what `sim_full_up.ps1` wires together)

The kiosk page relays every `pokamind_*` postMessage to the hostapp's `POST /api/cycle`,
which stamps the session identity and fans out to the in-process robot driver, FIWARE,
the `/ws/relay` broadcast (consumed by the ROS 2 publisher with `source:=pokamind` and
`emori-run --source pokamind`), the JSONL recorder, and the session tracker (spoken
summary + results email). Robot voice/ears are server-side on the hostapp: `GET /api/tts`
(Piper) and `POST /api/stt` (Vosk), with browser fallbacks when models are absent.
`GET /api/status` shows the live counters.

## Networking cheat-sheet

| From | To | URL |
|---|---|---|
| Windows host | daemon | `http://127.0.0.1:8000` |
| Windows host | hostapp hub | `http://localhost:8095` (the simulator scripts' port; `:8080` is the code default, `:8090` the robot's) |
| Windows host | Orion / QuantumLeap / Grafana | `http://localhost:1026` / `:8668` / `:3003` |
| ROS 2 container | daemon (host) | `http://host.docker.internal:8000` |
| ROS 2 container | hostapp relay (host) | `ws://host.docker.internal:8095/ws/relay` |
| ROS 2 container | hostapp skill intake (host) | `http://host.docker.internal:8096/api/skill` → `scripts/dev/loopback_proxy.py` → `127.0.0.1:8095`. On the robot the container is host-networked and hits `127.0.0.1` directly; on Docker Desktop it cannot, and the intake refuses non-loopback callers, so the rehearsal inserts a loopback hop rather than weakening the gate |
| ROS 2 container | Orion (host) | `http://host.docker.internal:1026` |
| Orion (FIWARE net) | QuantumLeap | `http://quantumleap:8668/v2/notify` |

## Ports

| Service | Port |
|---|---|
| Reachy daemon (REST/WS + dashboard) | 8000 |
| EMORI hostapp (kiosk + /api/cycle + /ws/relay) | 8080 code default · 8090 on the robot · **8095** in the simulator scripts (any free port; 8080/8090 are often taken by Docker/WSL) |
| loopback proxy (`scripts/dev/loopback_proxy.py`, only with `-MotionOwner ros`) | 8096 |
| Orion-LD | 1026 · MongoDB 27017 |
| QuantumLeap | 8668 · TimescaleDB 5432 |
| Grafana | 3003 (provisioned "EMORI Session" dashboard at `/d/emori-session`; local lab credentials are in `deploy/fiware/docker-compose.yml`, change them before exposing the stack) |

## Teardown

```powershell
docker compose -f deploy/ros2/docker-compose.yml down
docker compose -f deploy/fiware/docker-compose.yml down        # add -v to wipe the time-series
# stop the daemon / hostapp: Ctrl-C (or close) their windows
```

## Continuous integration

`.github/workflows/ci.yml` runs four jobs.

| Job | What it proves | Needs ROS |
|---|---|---|
| `test` (Python 3.10, 3.12, 3.13) | `ruff check src tests tests_ros`, `pytest tests`, and that `tests_ros/` collects and skips without `rclpy` | no |
| `ros2-standalone` | the four Apache-2.0 packages build and their nodes run with no EMORI core in the image | yes (image) |
| `ros2-graph` | the full Humble graph comes up on the mock source | yes (image) |
| `ros2-hub` | the hub image builds through the motor layer, the hub base and the hub layer, starts beside a motor node on the mockup backend, reaches it over ROS 2 services, passes `tests_ros/` and stops on SIGTERM with exit 0 | yes (image) |

`ros2-hub` is `scripts/ci/hub-ci.sh`, one subcommand per step. Its base image is built from
the checkout by `deploy/ros2/Dockerfile.v2.robot`; a robot's own base is a filesystem that
not built from this checkout, so the layering a robot ends up with is proved on the robot and not
there. `tests_ros/` holds the checks of what the hub assumes of `rclpy` that the fakes in
`tests/` cannot make (`tests_ros/README.md`). `scripts/golden_ci.py` records the scenarios
kept under `tests/fixtures/golden/` again and compares their structure with `--exact-only`; the
job fails when none are kept.

## Where to find things

```
src/emori_adapter/            Python core (ROS-free): sources/ translation/ behavior/ robot/ speech/
  hostapp/                    the host application, the ONLY robot driver, run by emori-hub on a robot; extensions.py is the add-on seam
  admin/                      emori-admin, the phone admin service (:8070)
ros2_ws/src/open/             standalone ROS 2 packages (never import emori_adapter)
ros2_ws/src/emori/            ROS 2 packages that use the emori_adapter core (bridge, fiware_bridge, bringup, v2 transport)
deploy/robot/                 install.sh, one-click-deploy.sh, systemd units, camera bridge, kiosk
deploy/robot/ros2_native/     v2: v2-stack.sh (the deploy's v1/v2 switch), the fence, Route B (pixi + RoboStack)
deploy/ros2/                  ROS images: Dockerfile (v1 Humble), .v2 / .v2.robot (Jazzy), .standalone, .consumer
deploy/fiware/                Orion-LD + QuantumLeap + TimescaleDB + Grafana
configs/                      templates + dev menus: behavior.yaml, mapping.yaml, roster.example, voice_topics*
  prod/                       NOT in this tree: a fork that ships a fixed spoken menu puts it here
  site/                       robot-local, never shipped (gitignored): settings.env, pokamind_scope.json,
                              auto/ (menus built from Pokamind), voice_topics.<lang>.yaml (saved menus)
scripts/  scripts/dev/        dev + ops tools; dev-box helpers
tests/                        the whole suite; no ROS needed (node halves loaded by path)
docs/                         documentation, indexed in docs/README.md
.env.example                  every setting, short comments; long form in docs/configuration.md
```

| Looking for | Where it is |
|---|---|
| What moves the robot, and when | `src/emori_adapter/behavior/`, `configs/behavior.yaml`, `docs/guides/03-how-the-robot-reacts.md` |
| Pokamind cycle → engagement signals | `src/emori_adapter/translation/`, `src/emori_adapter/models.py` |
| Live session driver, watchdog, turn-taking | `src/emori_adapter/hostapp/driver.py`, `hostapp/watchdog.py` |
| Kiosk pages, partner SSO mint | `hostapp/pages.py`, `hostapp/jwt_sso.py`, `hostapp/server.py` |
| This robot's Pokamind token (per-robot Ed25519 key, `pkr1.<robot-id>.<key>`) | `src/emori_adapter/robot_token.py`; whoami client `hostapp/pokamind_whoami.py` (sends `X-EMORI-Contract: 2`); what the robot takes from it `hostapp/pokamind_scope.py`; dashboard tab `admin/` + `hostapp/admin_api.py`; tokens are issued by Pokamind only; runbook `docs/deploy-a-new-robot.md` |
| Where a setting's value comes from (admin page > `.env` > Pokamind > hardware profile > default) | `src/emori_adapter/site_settings.py` (applied by `config.py` at import), `docs/configuration.md` "Where each setting comes from" |
| Proof that a token configures a fresh robot, and that a robot in service keeps its configuration | `tests/test_token_provisioning_e2e.py` (stand-in Pokamind, real admin route, a real next start), `tests/test_in_service_compat.py` |
| Pokamind scope sync (whoami snapshot, course/languages/pathways/name, restart reasons) | `hostapp/pokamind_scope.py` -> `configs/site/pokamind_scope.json`; `docs/pokamind-integration.md` |
| Spoken topic menu | `hostapp/voice_topics.py`; built from this robot's Pokamind modules by `hostapp/menu_builder.py` into `configs/site/auto/`; order: `EMORI_VOICE_TOPICS_<LANG>` > `configs/site/voice_topics.<lang>.yaml` (admin Menu tab) > `configs/prod/` (forks) > `configs/site/auto/` > dev menus (`server._menu_candidates`) |
| Badge roster / learner identity | `hostapp/identity.py`, `configs/roster.example.yaml` |
| Add people + print QR badges (phone admin People tab) | `hostapp/roster_admin.py` (validation, in-place YAML edit, live reload), `hostapp/badges.py` (segno QR/SVG/A4 sheet), routes `hostapp/admin_api.py` `/api/admin/roster*` proxied by `admin/server.py` `/api/people*`; UI `admin/page.py`; ops `docs/robot-admin.md` "Add people and print badges" |
| Head gestures (push-turn) | `hostapp/gestures.py` |
| Optional add-ons (entry point, hooks, deploy convention) | `hostapp/extensions.py`; `docs/addons.md` |
| Phone admin Settings/Menu tabs, credentials (PIN, hotspot, dashboard password), auto-apply | `admin/server.py` (`/api/settings`, `/api/menu*`, `/api/pin/change`, `/api/credentials/*`, `/api/setup`), `admin/site_apply.py`, `admin/page.py`; hostapp side `hostapp/admin_api.py` `/api/admin/site`, `/api/admin/menu*`; ops `docs/robot-admin.md` "Settings and credentials" |
| Readiness gates (`emori-readycheck`) | `hostapp/readycheck.py`, `deploy/robot/readycheck.sh` |
| Deploy-to-Robot's robot-side token check, robot identity, remote deploy | `deploy/robot/install-token.sh` + `hostapp/token_verify.py`; `src/emori_adapter/robot_identity.py` (`ROBOT_IDENTITY`); `--port`/`--jump` in `scripts/deploy_to_robot.py`; runbook `docs/deploy-a-new-robot.md` |
| Post-deploy smoke test (control plane and roleplay end to end, on the robot) | `scripts/rig_smoke.py`, `deploy/robot/rig-smoke.sh`; tests `tests/test_rig_smoke.py`; how to run and read it: `docs/deploy-a-new-robot.md` §5 |
| Management dashboard (`:8090/dashboard`) | `hostapp/dashboard.py`, `hostapp/dashboard_page.py`, `hostapp/metrics_store.py` |
| What each sentence's motion plan did: the stream, `/emori/motion/plan_events`, the dashboard's counts | `src/emori_adapter/plan_events.py`, the hub's `motion_node.py`, `docs/ros4hri-mapping.md` |
| ROS → robot skill intake (`/api/skill`) | `hostapp/skill_api.py`; the ROS side is `ros2_ws/src/open/emori_skill_facade` |
| A setting / env var | `.env.example`, then `docs/configuration.md`; its effective source on a robot: readycheck `site_config`, admin Settings |
| ROS topics and messages | `docs/ros4hri-mapping.md`, `ros2_ws/src/open/emori_msgs/` |
| One-click deploy | `Deploy-to-Robot.cmd` → `scripts/deploy_to_robot.py` → `deploy/robot/one-click-deploy.sh` → `install.sh`, then `deploy/robot/ros2_native/v2-stack.sh` (v2 unless `EMORI_ROS2_STACK=v1`) |
| Licences, the standalone ROS 2 packages | `LICENSING.md`, `ros2_ws/src/open/README.md` |
| Contributing, security reports | `CONTRIBUTING.md`, `SECURITY.md` |

## Known issues

- The daemon has **no `/health` endpoint**: `GET /health-check` → **405** (POST/HEAD only).
  Use `GET /api/daemon/status` for readiness. The hostapp's probe is `GET /healthz`.
- SDK WebSocket `/ws/sdk` → **403** when the daemon runs `--no-localhost-only`. EMORI
  drives the robot over **REST**, which stays open, so this is harmless;
  `scripts/smoke_daemon.py` checks the REST surface.
- The hostapp forces uvicorn's **wsproto** WebSocket backend (`HOSTAPP_WS`): the
  `websockets` 15.x backend rejects `/ws/relay` with HTTP 403.
- QuantumLeap typed a metric column as **BIGINT** when Orion-LD emitted a seeded `0.0` as
  integer `0`; we seed float metrics with `0.001` so the column is `DOUBLE`. If you change
  metrics and see `invalid input syntax for type bigint`, wipe the stack with
  `docker compose -f deploy/fiware/docker-compose.yml down -v` and bring it back up.
- The **QuantumLeap REST query API** may return "No records" for NGSI-LD even when data is
  stored; query **TimescaleDB / Grafana** directly. With the default tenant
  (`NGSILD_TENANT=pokamind`) the table is `mtpokamind.etsession`.
- An analysis cycle can carry **`null` body values** (for example when the analysis of
  that cycle failed); `sanitize_envelope` drops them before the envelope is validated.
  If something looks stuck, check `parse_errors` in `GET /api/status` first.
