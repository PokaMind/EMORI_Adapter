# EMORI Adapter

EMORI turns a Pokamind communication-training session into the behaviour of a Pollen
Robotics **Reachy Mini** desktop robot. With no screen, the robot greets the learner,
starts the right training module from a QR badge or a spoken choice, reacts to the
session's live analysis with body language while it runs, and speaks a summary at the
end.

This repository is the robot side of that system:

- **`emori-hub`**, the container on the robot that hosts the host application over ROS 2, and
  the only component that drives the robot: kiosk pages, partner sign-in, the live-session
  driver, speech (Piper voices, Vosk ears), the readiness checks and the management dashboard
  (`http://<robot>:8090/dashboard`). The same program also runs without ROS 2 as `emori-hostapp`,
  which the simulator uses and which stays installed on a robot as the way back;
- **`emori-admin`**, the phone-first admin page on `:8070` (Wi-Fi, volume, language,
  people and QR badges, the robot's Pokamind token, restarts, logs);
- **the kiosk**, a Chromium page on a virtual display that hosts the Pokamind session and
  uses the robot's own camera, microphone and speaker;
- **a full ROS 2 / ROS4HRI graph** (REP-155 person topics, an engagement policy, a
  bounded skill lane) and the robot itself as a ROS 2 node, in the motor container;
- **FIWARE analytics** (Orion-LD, QuantumLeap, TimescaleDB, Grafana);
- **the deploy kit** that installs all of it on the robot with one double-click.

Pokamind is a separate, closed-source service run by Pokamind AB. It hosts the training
sessions and the analysis. This repository contains only the robot side and its client of
the Pokamind API.

> Pokamind is the intelligence; Reachy Mini is the embodiment. This adapter is the bridge.

## What you need

- A Pollen Robotics **Reachy Mini Wireless** (Raspberry Pi CM4 on board).
- Network access for the robot (site Wi-Fi with internet, or a computer that shares one).
- A computer with `git` and Python 3.10 to 3.13, on the same network as the robot or
  able to reach it over SSH.
- From Pokamind: **a per-robot token** (the API address defaults to the public one).
  Only Pokamind can issue a token (see [The Pokamind token](#the-pokamind-token)).

The simulator, the session replay and the test suite need neither a robot nor Pokamind:
see [Try it without a robot](#try-it-without-a-robot).

## Repository map

| Path | What lives there |
|---|---|
| `src/emori_adapter/` | The Python core, ROS-free: `sources/` (mock, CSV, live relay), `translation/`, `behavior/`, `robot/` (daemon client), `speech/`, `session.py`, `cli.py` (`emori-run`), the FIWARE client and schema |
| `src/emori_adapter/hostapp/` | `emori-hostapp`, the hub on the robot: kiosk pages, partner SSO, the `/api/cycle` fan-out, the in-process robot driver, `/ws/relay`, Piper/Vosk speech, `readycheck`, the management dashboard, and the add-on seam (`extensions.py`) |
| `src/emori_adapter/admin/` | `emori-admin`, the phone-first admin service on `:8070` |
| `ros2_ws/src/open/` | Standalone ROS 2 packages that never import the core: `emori_msgs`, `hri_context_analyzer`, `hri_engagement_behaviors`, `emori_skill_facade`, and the Reachy Mini driver node `reachy_mini_ros2` + `reachy_mini_msgs` ([README](ros2_ws/src/open/README.md)) |
| `ros2_ws/src/emori/` | ROS 2 packages that use the core: `emori_adapter_ros` (core → ROS bridge), `fiware_bridge`, `emori_adapter_bringup` (launch files), `emori_skill_native` (v2 transport) |
| `deploy/robot/` | What runs on the robot: `install.sh`, `one-click-deploy.sh`, systemd units, camera bridge, kiosk, `readycheck.sh`, `rig-smoke.sh` (post-deploy smoke test); `ros2_native/` is the v2 bare-metal route |
| `deploy/ros2/` | ROS 2 images (v1 Humble, v2 Jazzy, standalone, outside consumer) and their compose files |
| `deploy/fiware/` | Orion-LD + QuantumLeap + TimescaleDB + Grafana, with the provisioned "EMORI Session" dashboard |
| `configs/` | `behavior.yaml` (behaviour + translation tuning), `mapping.yaml`, roster and voice-menu templates, development menus with placeholder ids; `prod/` is not in this tree (a fork that ships a fixed spoken menu puts it there); `site/` is robot-local and never shipped (saved settings and menus, the Pokamind scope snapshot, `auto/` menus built from the robot's modules) |
| `scripts/` | Dev and ops tools: `sim_full_up.ps1`, `deploy_to_robot.py`, replay, rehearsals, figure generators; `scripts/dev/` holds dev-box helpers |
| `tests/` | The whole suite; needs no ROS (the nodes' pure halves are loaded by path) |
| `docs/` | The documentation, indexed in [docs/README.md](docs/README.md) |
| `.env.example` | Every setting with its shipped value; the long-form notes are in [docs/configuration.md](docs/configuration.md) |
| `Deploy-to-Robot.cmd` / `.command` | The one-click deploy launchers (Windows / macOS-Linux) |

## Architecture

```
        Pokamind session (iframe postMessage); mock/CSV/replay for development
                                   │  per ~2 s cycle: 31 communication styles + body language; one event per AI sentence
                                   ▼
  kiosk Chromium /session page ──POST /api/cycle──►  emori-hub: the only robot driver
   (QR badge + verbal menu,                          │ stamps the session id, fans out to:
    Piper voice, Vosk ears)                          ▼
     ├─ in-process driver: Translator → BehaviorEngine → RobotPlayer ─► ROS 2 ─► motor node ─► robot
     ├─ FIWARE: per-session NGSI-LD ─► Orion-LD ─► QuantumLeap ─► TimescaleDB ─► Grafana
     ├─ /ws/relay ─► ROS 2 graph: context analyzer ─► engagement behaviours ─► skill facade
     │                 └─ POST /api/skill (only when EMORI_ROS_MOTION_OWNER=ros) ─► same queue
     ├─ JSONL recorder (artifacts/recordings/ → deterministic replay)
     └─ session tracker ─► spoken summary + results email + kiosk back to '/'
```

The core (`sources → translation → behavior → robot client`) is plain Python shared by
`emori-run`, the hostapp's driver and the ROS nodes. The Reachy daemon speaks HTTP on
`:8000`, so the simulator and the ROS container meet over HTTP, not DDS.

Full picture: **[docs/architecture.md](docs/architecture.md)** (the hub, both slices, the
data model), [docs/architecture/emori-system-architecture.md](docs/architecture/emori-system-architecture.md)
(the generated figure), [docs/pokamind-integration.md](docs/pokamind-integration.md) (what
the session sends the robot).

## ROS 2 on the robot

There are two configurations, and in both exactly one component arbitrates motion: the
hostapp's queue in v1, the robot node's arbiter in v2.

- **v1 (`Deploy-to-Robot --v1`).** Pollen's `reachy-mini-daemon` owns the motors. The ROS 2 graph is
  an optional **Humble container** (`deploy/ros2/Dockerfile`, run by the
  `emori-ros2.service` unit) that `install.sh` step 11 enables when it runs with
  `EMORI_WITH_ROS2=1` in its environment. It reads the hostapp's `/ws/relay`, publishes the
  REP-155 person topics and asks the hostapp to move the robot through `POST /api/skill`.
  The image is built off the robot for `linux/arm64` and loaded by `install.sh` step 11
  ([docs/onrobot-runbook.md](docs/onrobot-runbook.md), Step 5b).
- **v2 (the default).** The robot itself is a **Jazzy** ROS 2 node: `reachy_mini_ros2` owns
  Pollen's backend in-process, publishes `/joint_states` and `/tf`, serves `GoTo` and
  `PlayRecordedMove` actions, arbitrates every client through one arbiter, and serves a
  REST shim on `:8000` that the hostapp cannot tell apart from the daemon. Route A runs it
  in Docker (`deploy/ros2/Dockerfile.v2.robot`); Route B runs it natively through pixi and
  RoboStack (`deploy/robot/ros2_native/`).

Who may move the robot is one knob, `EMORI_ROS_MOTION_OWNER`: `hostapp` (the default; the
graph computes and publishes but sends nothing), `ros` (the graph's decisions drive the
robot's expressive motion) or `none`. Both sides enforce it.

**Getting it onto a robot.** The one-click deploy installs v2 (Route A): it builds the
robot's arm64 image on the deploy computer when the robot has none (Docker with buildx;
about 45 minutes, asked first), ships it, fences Pollen's daemon and starts the node, and
goes back to v1 if the node does not answer
([docs/deploy-a-new-robot.md](docs/deploy-a-new-robot.md), step 6). Pollen's apps, SDK,
dashboard and WebRTC need v1: deploy with `--v1`.

- v1 graph: build the arm64 image on a workstation
  (`docker buildx build --platform linux/arm64 -f deploy/ros2/Dockerfile -t emori/ros2-humble:latest --load .`),
  `docker save emori/ros2-humble:latest | gzip > emori-ros2-humble-arm64.tgz`, copy it to
  `/opt/emori/` on the robot, then run `EMORI_WITH_ROS2=1 bash deploy/robot/install.sh` there
  and set `EMORI_WITH_ROS2=1` in `/opt/emori/.env` so readycheck checks the lane
  ([docs/onrobot-runbook.md](docs/onrobot-runbook.md), Step 5b).
- v2 by hand, or Route B: [deploy/robot/ros2_native/README.md](deploy/robot/ros2_native/README.md).

**Seeing it run.** `docker exec emori-ros2 /entrypoint.sh ros2 node list` (v1) or
`docker exec emori-ros2-v2 /entrypoint.sh ros2 node list` (v2, Route A); the readycheck
`ros2` gate PASSes `ros2_up` or `ros2_up_v2`. The per-flavour checklists are in
[docs/ros4hri-mapping.md](docs/ros4hri-mapping.md#inspect-at-runtime) and
[docs/ros2-v2-native-robot.md](docs/ros2-v2-native-robot.md).

The main interfaces, from [docs/ros4hri-mapping.md](docs/ros4hri-mapping.md) (topics
published by `emori_adapter_ros`):

| Topic | Type | QoS | Notes |
|---|---|---|---|
| `/humans/persons/tracked` | `hri_msgs/IdsList` | transient_local (latched) | current persons |
| `/humans/persons/<id>/anonymous` | `std_msgs/Bool` | transient_local | REP-155 person well-formedness |
| `/humans/persons/<id>/engagement_status` | `hri_msgs/EngagementLevel` | volatile, depth 10 | the standard engagement topic |
| `/humans/persons/<id>/context` | `emori_msgs/Context` | volatile, depth 10 | full structured signal (EMORI extension) |
| `/humans/persons/<id>/speech_cue` | `emori_msgs/SpeechCue` | volatile, depth 10 | one sentence of the ROBOT's own speech, at that sentence's playback onset — **live source only** |

The skill lane, from the same document:

| Topic / action | Type | Direction |
|---|---|---|
| `/skill/set_expression` | `emori_msgs/SetExpression` (topic) | behaviours → facade |
| `/skill/replay_motion` | `emori_msgs/action/ReplayMotion` | behaviours → facade |
| `/skill/look_at` | `emori_msgs/action/LookAt` | behaviours → facade |

`hri_context_analyzer` also republishes `Context` as single-purpose topics under
`/humans/persons/<id>/context/...` plus the standard `/humans/persons/<id>/expression`.
In v2 the robot node adds its own interfaces, from
[docs/ros2-v2-native-robot.md](docs/ros2-v2-native-robot.md):

| Interface | Type | Fields |
|---|---|---|
| `/joint_states` | `sensor_msgs/JointState` | all nine joints, ≈50 Hz, straight off Pollen's publisher hook |
| `/reachy_mini/head_pose` | `geometry_msgs/PoseStamped` | the present head pose |
| `/tf` | `tf2_msgs/TFMessage` | `base_link → head` |
| `/reachy_mini/motion_status` | `reachy_mini_msgs/MotionStatus` | `busy, running_move_id, queue_depth, owner, head_reserved, expression_owner, motor_mode, backend`. Latched (`transient_local`, depth 1), published on change and every 1 s |
| `/reachy_mini/goto` | `reachy_mini_msgs/action/GoTo` | goal `use_head, head_pose, use_antennas, antennas[2], use_body_yaw, body_yaw, duration, interpolation, lane, client, preempt, reason` → result `completed, error_code, move_id` → feedback `progress` |
| `/reachy_mini/play_recorded_move` | `.../action/PlayRecordedMove` | goal `dataset, name, lane, client, preempt, reason` → same result and feedback |
| `/reachy_mini/stop` | `.../srv/StopMove` | `move_id` (empty = all) → `stopped` |
| `/reachy_mini/set_owner` | `.../srv/SetOwner` | `client, ttl_s` (empty client = release) → `ok, owner` |

The node also serves `/reachy_mini/set_target`, `wake_up`, `goto_sleep`, `set_motor_mode`
and `set_head_reserved`, publishes the camera topics and `/diagnostics`, and, with face
look-at, `/humans/faces/*`: see the index below.

The eleven ROS 2 packages, all Apache-2.0:

| Package | Distro | What it does |
|---|---|---|
| `emori_msgs` | Humble | The interfaces: `Context`, `SpeechCue`, `SetExpression`, `LookAt`, `ReplayMotion` |
| `hri_context_analyzer` | Humble | Splits `Context` into single-purpose ROS4HRI topics |
| `hri_engagement_behaviors` | Humble | The engagement policy: decides when the robot should react, and names the motion |
| `emori_skill_facade` | Humble | The bounded `/skill/*` surface and the pluggable transport where a decision leaves ROS |
| `reachy_mini_msgs` | Jazzy | Interfaces for the Reachy Mini as a ROS 2 node (`GoTo`, `PlayRecordedMove`, motion status) |
| `reachy_mini_ros2` | Jazzy | Pollen's Reachy Mini backend as a ROS 2 node, with an arbiter and a REST shim on `:8000` |
| `emori_adapter_ros` | Humble | The bridge from the core's session model (live relay or mock) onto the person topics |
| `fiware_bridge` | Humble | NGSI-LD historization from the graph (off on a live robot, where the hostapp writes) |
| `emori_adapter_bringup` | Humble | The launch files (`slice2.launch.py` for v1, `v2.launch.py` for v2) |
| `emori_skill_native` | Jazzy | v2's `ros_native` transport: skill requests as robot-node actions, no HTTP hop |
| `emori_hub` | Jazzy | The host application hosted beside rclpy in one process, with its robot client over the robot node's services |

Every node, topic, action and service, with QoS:
[docs/ros4hri-mapping.md](docs/ros4hri-mapping.md#every-ros-channel-at-a-glance). The v2
robot node in depth, the deployment routes and how to verify them:
[docs/ros2-v2-native-robot.md](docs/ros2-v2-native-robot.md).

## Deploy to a robot

Shipping code to a robot is one double-click. Put the robot on the same Wi-Fi as your
computer (or join its hotspot) and run **`Deploy-to-Robot.cmd`** (Windows) or
**`Deploy-to-Robot.command`** (macOS and Linux). The chain is:

```
Deploy-to-Robot.cmd / .command
  → scripts/deploy_to_robot.py        finds the robot, SSH key, preflight, streams `git archive HEAD` into /opt/emori
  → deploy/robot/one-click-deploy.sh  on the robot: a full install, or a --quick restart
  → deploy/robot/install.sh           packages, venv, models, camera, token, units, secrets
  → readycheck                        ROBOT READY, or the failing gates
```

The launcher finds the robot (`reachy-mini.local`, the hotspot, then a scan; simulators
are ignored), installs an SSH key the first time (user `pollen`), ships the **committed**
code into `/opt/emori`, asks for **this robot's Pokamind token** and installs it, gives a
robot with no `.env` one (from `robot.local.env` if you made one, otherwise it asks only
for an admin PIN, generated when you leave it empty or when there is no keyboard), runs
`install.sh` and ends on `readycheck`, then shows the credentials the install generated
once more, and any readiness warnings. `--quick`
ships code and restarts without apt/pip (automatic when the robot has no internet);
`--host <ip>`, `--env <file>`, `--find`, `--token-file` and `--replace-token` do what they
say, and `--port`/`--jump` reach a robot on another network. It refuses, restarting
nothing, on a robot without passwordless sudo or running v2 without the daemon fence.

`install.sh` installs these systemd units: Pollen's daemon gets a `--no-media` override
(motion only), and EMORI adds `emori-hub` (`:8090`, in its container), `emori-hostapp` (the
way back, fenced off while the hub runs), `emori-kiosk` (Chromium
on Xvfb), `emori-camera-bridge` (the CSI camera as a v4l2loopback webcam), `emori-admin`
(`:8070`) with its mDNS adverts, and, only with `EMORI_WITH_ROS2=1`, `emori-ros2`. The deploy
then moves the robot to the ROS 2 stack and switches it to the hub
([deploy-a-new-robot.md](docs/deploy-a-new-robot.md#7-the-ros-2-hub)).

**What a person enters, and what the robot works out for itself.** A new robot needs
from a human: its **first network** (Pollen's own setup, before the first deploy;
[deploy-a-new-robot.md](docs/deploy-a-new-robot.md#before-you-start-a-brand-new-robots-first-network)),
**its Pokamind token** (the deploy prompt, or the admin page's Pokamind tab), the
**credentials** the install shows once (admin PIN, hotspot, dashboard password), and
the site **Wi-Fi** when it differs (the admin page's Network tab). People and their QR badges are
added on the People tab when the site wants them. Everything else configures itself:

- from the token, through the robot's Pokamind record (synced at start, every 6 hours
  and from the admin page): the **course**, the **languages** and the kiosk's boot
  language, the **pathways**, the robot's **display name**, the **spoken topic menu**
  (built from the robot's modules, in their order);
- generated on the robot by `install.sh`: the **admin PIN** (shown once), the admin
  token and cookie salt, the recovery **hotspot** name and password, the **dashboard
  password**, the ROS skill intake's key, and the **hardware profile** that supplies the
  tuning measured on the Reachy Mini Wireless.

Any of these can still be set by hand: a value saved on the admin page wins, then a
value in `/opt/emori/.env`, then Pokamind, then the hardware profile, then the code
default ([docs/configuration.md](docs/configuration.md#where-each-setting-comes-from)).
A value set in `.env` is never replaced by anything derived from Pokamind or the
hardware profile.

After a deploy:

```bash
ssh pollen@<robot> 'bash /opt/emori/deploy/robot/readycheck.sh'   # every readiness gate
ssh pollen@<robot> 'bash /opt/emori/deploy/robot/rig-smoke.sh'    # the control plane and roleplay end to end
```

`rig-smoke.sh` is not read-only: it makes the robot speak and move once. The full
procedure, step by step, including per-robot settings, deploying from
another network and every readycheck refusal code:
**[docs/deploy-a-new-robot.md](docs/deploy-a-new-robot.md)**. First bring-up of new
hardware and debugging: [docs/onrobot-runbook.md](docs/onrobot-runbook.md). Day-to-day
operation from a phone: [docs/robot-admin.md](docs/robot-admin.md).

## Optional add-ons

The kiosk runs complete on its own. Extra modes can be added as separately installed
packages that publish an entry point in the `emori_adapter.extensions` group; set
`EMORI_ADDONS=off` to ignore them. Writing and deploying one: [docs/addons.md](docs/addons.md).

## The Pokamind token

Every robot has its own Pokamind token, one line of the form `pkr1.<robot-id>.<key>`:

- `<key>` is the 32-byte seed of an **Ed25519** key pair (base64url, 43 characters). The
  robot uses it to sign short-lived assertions (its control-plane calls and its learners'
  sign-ins); Pokamind keeps only the public key.
- **Only Pokamind can issue, rotate or revoke a token.** The token's scope (organisation,
  course, languages, pathways, training modules and their menu order) is set by Pokamind, and changing the scope needs no change on the robot:
  the robot reads it back over the same signed channel (`GET /api/robot/whoami`), keeps
  the last good copy, rebuilds its spoken menu at once and applies the rest at its next
  idle restart.
- It is the only Pokamind-related input a robot needs. Pasted on the admin page, it is
  checked with Pokamind first, and the robot's scope is written before the token is
  saved, so the restart that follows comes up fully configured.
- On the robot it lives in `/opt/emori/secrets/pokamind_robot_token` (file 0600, directory
  0700). It is never committed, never put in `.env` or `robot.local.env`, and logs and
  status pages show it only masked (`pkr1.<robot-id>.…abcd`).
- The admin page on `:8070` can test it, replace it and (after the PIN again) reveal it.

Lost or leaked token: ask Pokamind to rotate or revoke it
([docs/deploy-a-new-robot.md](docs/deploy-a-new-robot.md), "Replace, rotate, revoke").

## Try it without a robot

The simulator runs the whole deployed topology on a Windows development machine, with no
robot and no Pokamind account:

```powershell
uv venv .venv-reachy --python 3.12      # not 3.14: mujoco 3.3.0 has no wheel for it
uv pip install --python .venv-reachy "reachy-mini[mujoco]"
uv pip install --python .venv-reachy -e ".[dev,hostapp,pokamind,speech,stt]"
.\scripts\get_voice_models.ps1          # Piper voices + Vosk models
.\scripts\sim_full_up.ps1 -Replay tests\fixtures\live_session.jsonl
```

`sim_full_up.ps1` starts the Reachy daemon in MuJoCo (`--sim --no-media`), the FIWARE
stack, the ROS 2 graph and the hostapp on `:8095`; `-Replay` drives it from a recorded
session, deterministically, and `-MotionOwner ros` hands expressive motion to the ROS
graph. Without the simulator at all, `emori-run --source mock --robot dry --no-realtime`
runs the translation and behaviour pipeline from the command line. Setup, ports and
gotchas: **[docs/dev-setup.md](docs/dev-setup.md)**, then the walkthroughs in
[docs/guides/](docs/guides/01-start-the-simulation.md).

## FIWARE analytics

The hostapp writes one NGSI-LD `Session` entity per training session (per-cycle engagement
metrics and an end-of-session summary, under pseudonymous ids) to an Orion-LD broker;
QuantumLeap historizes it into TimescaleDB and Grafana shows it. The stack does not run on
the robot:

```bash
docker compose -f deploy/fiware/docker-compose.yml up -d    # on an analytics host
```

and the robot's `.env` points at it (`EMORI_FIWARE=1`, `ORION_URL=http://<host>:1026`).
The stack ships local lab credentials: change them before exposing it. Running it:
[deploy/fiware/README.md](deploy/fiware/README.md). The consumer contract and how an
outside party verifies it: [docs/analytics-interface.md](docs/analytics-interface.md).

## Development

```bash
python -m pip install -e ".[dev,hostapp,pokamind]"
python -m pytest tests -p no:cacheprovider
python -m ruff check src tests
```

The test suite needs no ROS, no robot and no Pokamind. The pip extras are `sim`,
`pokamind`, `hostapp`, `speech`, `stt` and `dev`. Setting up, the repository
rules and how to send a change: **[CONTRIBUTING.md](CONTRIBUTING.md)**. Where each part of
the code lives: [docs/dev-setup.md](docs/dev-setup.md#where-to-find-things).

## Documentation

**[docs/README.md](docs/README.md)** lists every document by audience: operators
deploying and running robots, and developers working on this repository.

## Licence

Apache-2.0, Copyright 2026 Pokamind AB. See [LICENSE](LICENSE), [NOTICE](NOTICE) and
[LICENSING.md](LICENSING.md) (which also lists the third-party components). Security
issues: [SECURITY.md](SECURITY.md).

Developed within the EU-funded ARISE project.
