# On-robot runbook: first bring-up of new hardware, and debugging

Bring up the full EMORI experience **on the robot's CM4** with **no screen**: the robot
sees (CSI camera), hears (reSpeaker mic), speaks (Piper + Pokamind's voice on the 5 W
speaker), and moves (the hostapp driver). This runs the **same software verified in
simulation** (`scripts/sim_full_up.ps1` boots the identical topology on the dev box);
only the camera bridge, the Xvfb Chromium, and the daemon media-handoff are robot-only
and are validated here.

**Use this page** for the first bring-up of a new piece of hardware (a new robot model, a
new camera, a new image from Pollen), and as the reference when something on a robot is
wrong. **Do not use it** for routine work:

* **Provisioning another robot of a supported model** — follow
  [deploy-a-new-robot.md](deploy-a-new-robot.md).
* **Shipping new code to a robot** — one double-click: `Deploy-to-Robot.cmd` (Windows) or
  `Deploy-to-Robot.command` (macOS/Linux) in the repo root, with the robot on the same
  Wi-Fi. It finds the robot, ships the committed code, runs Steps 1–4 below via
  `install.sh` and ends on `readycheck`.
* **Day-to-day operation** (Wi-Fi, volume, language, restarts, logs) — the phone admin
  page on `:8070`, [robot-admin.md](robot-admin.md).
* **Optional add-ons** — separately installed packages under `addons/<name>/`, deployed by `install.sh`: [addons.md](addons.md).

Read [adr-001-onrobot-screenless.md](adr-001-onrobot-screenless.md) for the why.

Commands use 8090, the port a deployed robot runs the hostapp on (`HOSTAPP_PORT` in the
shipped `.env`; the code default is 8080).

## Topology on the CM4 — ONE driver, one hub

```
reachy-mini-daemon --no-media     # motion ONLY (drop-in override; releases camera/mic/speaker)
  ▲ daemon REST (goto/antennas)
emori-hub (:8090)                 # the single robot driver, in its container over ROS 2
                                  # (emori-hostapp, the same program, stays as the way back):
  ├ kiosk pages (QR badge + verbal menu, /api/tts Piper voice, /api/stt Vosk ears)
  ├ partner JWT-SSO mint  → Pokamind iframe (module pre-picked, device-check auto-advance)
  ├ /api/cycle            ← the session page relays every pokamind_* postMessage
  │   ├→ Translator → BehaviorEngine → daemon  (robot body language, talking gestures)
  │   ├→ FIWARE (per-session NGSI-LD entities → QuantumLeap → TimescaleDB → Grafana)
  │   ├→ /ws/relay broadcast  (ROS 2 live publisher, recorder, debuggers)
  │   └→ session tracker → spoken summary + results email + kiosk loop to '/'
emori-camera-bridge               # libcamera(IMX708) → /dev/video40 (v4l2loopback)
emori-kiosk                       # Xvfb + real Chromium → hostapp '/' → session
ROS 2 container (optional)        # ros4hri graph, source:=pokamind reads /ws/relay
FIWARE stack (LAN server)         # Orion-LD + QuantumLeap + TimescaleDB + Grafana
```

There is **no separate adapter service**: the hostapp drives the robot in-process, and
`/ws/relay` carries every stamped envelope, produced by `/api/cycle`, for *observers*
(ROS 2, recorders).

## Prerequisites
- `rpicam-hello --list-cameras` shows **imx708** (the robot camera).
- The robot needs a Pokamind token (see [deploy-a-new-robot.md](deploy-a-new-robot.md)).
- `python3 --version` — Trixie ships **3.13**; the package installs there (`requires-python <3.14`).

## Step 1 — daemon: motion only (persistent across reboots)

The image's real unit runs a **launcher script** (`.../wireless/launcher.sh` inside
`/venvs/mini_daemon`) that sets GStreamer/libcamera env before starting the daemon —
there is **no `/usr/bin/reachy-mini-daemon` binary**. The override therefore points
ExecStart at `deploy/robot/emori-daemon-launcher.sh`, an env-matched mirror that adds
`--no-media`. On every OS update, re-diff the image launcher against the mirror first.
```bash
cd /opt/emori && chmod +x deploy/robot/emori-daemon-launcher.sh
# verify the image's launcher env still matches ours: systemctl cat reachy-mini-daemon
sudo mkdir -p /etc/systemd/system/reachy-mini-daemon.service.d
sudo cp deploy/robot/reachy-mini-daemon-override.conf \
     /etc/systemd/system/reachy-mini-daemon.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart reachy-mini-daemon
curl -s localhost:8000/api/daemon/status        # state: running, no_media: true
```
Note (v1.9.0 drift): `POST /api/daemon/start` requires `?wake_up=<bool>` and
`POST /api/daemon/stop` requires `?goto_sleep=<bool>`; a bare call 422s.

**Motors boot `disabled`** (the launcher passes `--no-wake-up-on-start`), so a `goto`
returns 200 but nothing moves until they are enabled. **The hostapp enables them
automatically at daemon-ready** (`RobotPlayer` → `set_mode/enabled` before `wake_up`);
to force manually: `curl -X POST localhost:8000/api/motors/set_mode/enabled`.

> **Head-tilt session control is BLOCKED on daemon v1.9.0.** The screenless begin/end
> gesture needs the HEAD in `gravity_compensation`, but that mode returns an
> empty-body **HTTP 500** on this firmware (`disabled` fails too; only `enabled`
> works, across power cycles). The hostapp latches the refusal and degrades
> silently; the head simply stays stiff and the tilt gesture never fires. The shipped
> gesture is the push-turn instead (`EMORI_GESTURE_MODE=push_turn`,
> `hostapp/gestures.py`): a firm head turn on stiff motors, which needs no
> `gravity_compensation`. Sessions start from a QR badge or the spoken menu; B/S/E are
> development keys on the kiosk page. A firm push of the head BACK, held a little over half a
> second, leaves whatever is running; the antennas flick twice when it is felt
> ([configuration](configuration.md#emori_gesture_back_exit-emori_gesture_back_threshold_rad-emori_gesture_back_hold_ticks)).

## Step 2 — camera bridge (robot CSI camera → Chromium)
```bash
sudo bash deploy/robot/setup-camera-bridge.sh   # v4l2loopback + gst plugins, /dev/video40
sudo cp deploy/robot/camera-bridge.service /etc/systemd/system/emori-camera-bridge.service
sudo systemctl enable --now emori-camera-bridge
v4l2-ctl -d /dev/video40 --list-formats-ext     # CAPTURE (YUY2) ONLY while the bridge runs
```

The pipeline lives in `deploy/robot/camera-bridge.sh`; tune it without
editing the unit by writing `/etc/default/emori-camera` and restarting:

```bash
sudo tee /etc/default/emori-camera <<'EOF'
EMORI_CAM_FPS=15            # 15 doubles libcamera's exposure ceiling (66 ms/frame)
EMORI_CAM_BRIGHTNESS=0.05   # videobalance, -1..1 (software; amplifies noise too)
EMORI_CAM_CONTRAST=1.30     # videobalance, 0..2
EMORI_CAM_SRC_CONTROLS=     # e.g. "auto-focus-mode=2" — ONLY names gst-inspect shows
# v2 face look-at only. EMPTY (the default) means NO tee: the single-output
# pipeline the kiosk always runs. Set to 41 to tee a second loopback at 320x240 @
# 10 fps for the ROS robot node — see Step 5c.
EMORI_CAM_ROS_VIDEO_NR=
EOF
sudo systemctl restart emori-camera-bridge
gst-inspect-1.0 libcamerasrc                      # which controls this build exposes
EMORI_CAM_PRINT_PIPELINE=1 /opt/emori/deploy/robot/camera-bridge.sh   # dry-run
```

The kiosk decoder is tuned from the URL, i.e. `Environment=EMORI_KIOSK_URL=` in
`kiosk.service`: `?qrcrop=0.55` (fraction of the frame decoded, and the size of the
on-screen box), `?qrdark=32` (dynamic range under which the page says "too dark"),
`?qrlit=64` (range above which the expensive passes are skipped), `?qrms=100` (ms
between scan ticks). The screen is virtual, so read the `light NN% · RANGE · pass Nms`
line with a screenshot:

```bash
DISPLAY=:99 gst-launch-1.0 -q ximagesrc num-buffers=1 ! videoconvert ! pngenc ! filesink location=/tmp/k.png
```

### Badge reader checks (in the real room)

These can only be answered on the robot, at the kiosk position and in the room's
light. Record what you measure, not what you expect.

| # | Check | How | Pass condition |
|---|------|-----|----------------|
| G-QR1 | libcamerasrc control inventory | `gst-inspect-1.0 libcamerasrc` | record which of `exposure-time` / `analogue-gain` / `ae-enable` / `auto-focus-mode` / `lens-position` exist on THIS build |
| G-QR2 | Bridge survives the pipeline | `sudo systemctl restart emori-camera-bridge && systemctl status emori-camera-bridge`; `v4l2-ctl -d /dev/video40 --list-formats-ext` | `active (running)`, no restart loop, CAPTURE YUY2 1280x720 |
| G-QR3 | Frame exposure | at the real kiosk position and room light: `gst-launch-1.0 -q v4l2src device=/dev/video40 num-buffers=1 ! videoconvert ! jpegenc ! filesink location=/tmp/cam.jpg` | record mean luma AND max-min dynamic range for 30 fps/no-balance vs 15 fps/balance |
| G-QR4 | Decode at kiosk distance | hold a printed badge at 20 / 30 / 40 / 50 cm; screenshot the kiosk | record decode / no-decode per distance, and which `pass` name the readout shows |
| G-QR5 | Focus | G-QR1 output; if no AF property, sweep distances in G-QR4 | either continuous AF is enabled via `EMORI_CAM_SRC_CONTROLS`, **or** the sharp distance band is known and marked on the kiosk plinth |
| G-QR6 | CM4 cost | `vcgencmd measure_temp` + `top -bn1` after 10 min idle scanning, vs a soak without scanning | temperature inside the soak budget; Chromium not pegged |
| G-QR7 | Per-pass latency on ARM | the `…· pass Nms` field in the screenshot | worst pass < 400 ms; if not, raise `?qrms=` or drop pass 2 by setting `?qrlit=0` |
| G-QR8 | Verbal path untouched | with no badge, say "one" | the session starts — the spoken menu stays the primary path and QR stays a staff-identified convenience |

Decoder robustness is unit-tested off the robot in `tests/test_qr_reader_robustness.py`
(synthetic frames, the vendored `jsqr.js`); that is not a decode rate, which only the
checks above can give.

## Step 3 — install EMORI + speech models
```bash
sudo mkdir -p /opt/emori && sudo chown pollen /opt/emori
git clone <this repo> /opt/emori && cd /opt/emori
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[hostapp,pokamind,speech,stt]"   # hub + live source + Piper + Vosk
bash scripts/get_voice_models.sh                  # voices/{ca,en}.onnx + models/vosk-{ca,en}
cp .env.example .env                              # leave the empty values empty: see below
sed -i 's/^EMORI_HARDWARE_PROFILE=$/EMORI_HARDWARE_PROFILE=reachy-mini-wireless/' .env  # as install.sh
cp configs/roster.example.yaml configs/roster.yaml          # nobody yet: the People tab adds people
mkdir -p configs/site/auto secrets && chmod 700 secrets      # robot-local; no deploy touches them
```

Nothing site-specific is typed into `.env`. The course, languages, pathways, display
name and spoken menu come from the robot's Pokamind record once its
token is installed (admin page -> Pokamind -> Replace token); the admin PIN, hotspot and
dashboard credentials are what `install.sh` generates. Overrides go on the admin page:
Settings (server, results e-mail, analytics, data), Menu (the spoken menu's wording and
order), plus the tab of any installed add-on. How each value is chosen, and what a
person still enters: [deploy-a-new-robot.md](deploy-a-new-robot.md), "Per-robot
settings". This is the by-hand version of what `deploy/robot/install.sh` does; on a
supported robot model, use the one-click deploy instead.

## Step 4 — services: hostapp → kiosk
```bash
sudo cp deploy/robot/emori-hostapp.service /etc/systemd/system/emori-hostapp.service
sudo cp deploy/robot/kiosk.service        /etc/systemd/system/emori-kiosk.service
sudo systemctl enable --now emori-hostapp emori-kiosk
curl -s localhost:8090/api/status | python3 -m json.tool    # robot:true, tts/stt:true
```
The kiosk opens **`/`** — the QR-badge + verbal-menu landing. Badge (or anonymous) →
robot greets + speaks the menu (Piper) → learner says "one/two/three" (Vosk) → SSO mint
with `module_id` (no topic screen) + `emori_lang` → the Pokamind session loads in the
iframe → the session runs → spoken summary → the kiosk loops back to `/` for the next
learner.

> **Session start.** The SSO mint sends `emori_kiosk:true` (from both `/api/start` and
> `/api/voice-select`), so the device-check auto-advance and the `emori_kiosk`-gated
> `results` forwarding — the spoken summary + results email — are **active**.
> First-turn auto-begin is intentionally not timed: the session page begins the first
> turn (`wake`, the same command as a head nudge or the `B` key) only when the first
> `pokamind_tts_ended` says the scenario intro has finished playing, so the microphone
> never opens while the first question is still playing. On the first live session on a
> new robot, explicitly verify the first turn is heard and the end-of-session
> summary/email fire.

**Audio under systemd:** `kiosk.service` sets `XDG_RUNTIME_DIR=/run/user/1000`; ensure
`pollen` is in the `audio` group and uid 1000 (`id pollen`), else adjust the unit.

**Trixie one-time:** in the kiosk's Chromium profile, set `chrome://flags → "PipeWire
Camera support" = Disabled` (else Chromium ignores `/dev/video40`). Persists in `--user-data-dir`.

## Step 5 — analytics (FIWARE) and ROS 2 (optional)
Run the FIWARE stack on a LAN server: `docker compose -f deploy/fiware/docker-compose.yml up -d`,
then point the robot at it, on the admin page (Settings -> Analytics) or in its `.env`
(`EMORI_FIWARE=1`, `ORION_URL=http://<lan-server>:1026`,
`QL_NOTIFY_URL=http://quantumleap:8668/v2/notify` — the latter resolves on the FIWARE
host's compose network, where Orion sends notifications). The hostapp historizes every
cycle + the summary; Grafana ships a provisioned **EMORI Session** dashboard
(`http://<lan-server>:3003/d/emori-session`).

For the ROS 2 graph on live data **off the robot** (a LAN server watching the robot), run the
sim/off-robot compose file pointed at it:
```bash
EMORI_SOURCE=pokamind EMORI_WS_URL=ws://<robot>:8090/ws/relay \
FIWARE_BRIDGE_ENABLED=0 \
docker compose -f deploy/ros2/docker-compose.yml up -d
```
(`FIWARE_BRIDGE_ENABLED=0`: the hostapp already historizes every cycle and the summary, and
two writers double every time-series row. No ROS node holds a daemon URL: motion leaves
the graph only as a skill request, see Step 5b.)

Running the FIWARE stack off the robot is also what makes the analytics interface
checkable without touching the robot: point the robot's `ORION_URL` at the LAN host and an
EMORI-free consumer can read a `Session` and recompute the recovery figure from
QuantumLeap's own series. `scripts/arise_consumer_check.py` is that consumer, and
`docs/analytics-interface.md` is its contract. The recovery window is 90 s
(`metrics_store.RECOVERY_WINDOW_S`), and the check compares the two **counters**, not just
the ratio, so a feed that lost half its rows cannot pass.

To run the same graph **on the robot**, use Step 5b: it is a different compose file for
reasons that break a robot quietly (host networking, bounded CPU/memory, log rotation).

## Step 5b — the ROS 2 (ros4hri) lane ON the robot (optional)

Off by default and additive: without `EMORI_WITH_ROS2=1` nothing below is installed, no
container runs, and `readycheck` costs you one environment read. A failed ROS container can
never take a session down — it is a sixth unit that observes and asks, and the hostapp
remains the single robot writer either way.

**Why a container.** The robot is Trixie / aarch64 / Python 3.13 and ROS 2 Humble targets
Ubuntu 22.04 / Python 3.10, with no build for Trixie, so the v1 graph runs Humble in ONE
host-networked container reading `ws://127.0.0.1:${HOSTAPP_PORT}/ws/relay`. (v2 runs Jazzy
on the robot instead: [deploy/robot/ros2_native/README.md](../deploy/robot/ros2_native/README.md).)

### 1. Build the arm64 image OFF the robot
There is no compiler on the CM4 and no build time to spare on an SD card, so the image is
built on a dev machine and carried over as a tarball:
```bash
docker buildx build --platform linux/arm64 -f deploy/ros2/Dockerfile \
    -t emori/ros2-humble:latest --load .
docker save emori/ros2-humble:latest | gzip > emori-ros2-humble-arm64.tgz
scp emori-ros2-humble-arm64.tgz pollen@reachy-mini.local:/opt/emori/
```
`ros:humble-ros-base` **publishes a `linux/arm64/v8` manifest** (check with
`docker buildx imagetools inspect ros:humble-ros-base`), so the primary path resolves; the
fallbacks, if a future tag ever drops it, are `ros:humble-ros-core` + `apt-get install
ros-humble-rclpy ros-humble-hri-msgs`, then multi-staging the build.

An accidental **amd64** image installs and starts perfectly on the dev machine and dies on
the robot with a bare `exec format error`, which is why the Dockerfile echoes
`TARGETPLATFORM` and `uname -m` into the build log — read that line before you `scp`.

### 2. Enable it on the robot
```bash
EMORI_WITH_ROS2=1 bash deploy/robot/install.sh     # step 11: loads the tarball, installs the unit
sudo systemctl status emori-ros2
docker logs --tail 50 emori-ros2
```
Step 11 needs `docker` (`sudo apt-get install -y docker.io docker-compose-plugin && sudo
usermod -aG docker pollen`); without it, it says so and enables nothing. On a v2 robot (the
fence `/opt/emori/V2_OWNS_THE_BUS` is up) it never installs this graph, whatever
`EMORI_WITH_ROS2` says and whatever image is on the card, and it disables and stops an
`emori-ros2.service` it finds enabled or active there (one left from the robot's v1 days would
start at the next boot), because a v1 graph beside the v2 container is a second process
deciding what the motors do. The unit has
deliberately **no `PartOf=`/`BindsTo=`** in either direction: a ROS restart must never bounce
the kiosk (a live session page would be lost), and a hostapp restart must not kill the
graph — the bridge reconnects on its own.

Set `EMORI_WITH_ROS2=1` in `/opt/emori/.env` too, so `readycheck` knows the lane is armed.
(A v2 robot committed to v2 needs neither: its fence arms the gate, and step 11 skips it. A
v2 robot only *on trial*, with no fence, still needs `EMORI_WITH_ROS2=1` for readycheck to
check its container.)

### 3. The readycheck gate
`emori-readycheck` gains a `ros2` gate. On every normal robot it is **PASS `ros2_lane_off`** —
a feature nobody armed is not a dependency that could not be probed, and a SKIP there would
fail `--strict` on a perfectly healthy robot. Armed (`EMORI_WITH_ROS2=1`, or on a v2 robot the
fence `/opt/emori/V2_OWNS_THE_BUS` alone, which no `EMORI_WITH_ROS2=0` overrides), it proves:

| Gate answer | What it means | What to do |
|---|---|---|
| `ros2_up` | container running, the whole graph up inside it, no second FIWARE writer, and the lane has somewhere to POST | — |
| `docker_unavailable` (SKIP) | armed but docker is not installed (on a fenced robot: and no `reachy-mini-ros2.service` either) | v1: run step 11's apt line, or set `EMORI_WITH_ROS2=0`. Fenced: install docker ([deploy-a-new-robot.md](deploy-a-new-robot.md) step 7) or lower the fence; `EMORI_WITH_ROS2=0` does not switch the check off there |
| `docker_unreachable` (SKIP) | docker is installed but did not answer readycheck (`docker version` failed), so the container's state is unknown | `sudo usermod -aG docker pollen`, then log in again; or dockerd is down: `systemctl status docker` |
| `ros2_container_absent` (FAIL) | v1: no `emori-ros2` container exists. Fenced (v2): no `emori-ros2-v2` container, and nothing can move the robot | v1: load the arm64 tarball and rerun step 11. v2: `docker compose -f deploy/ros2/docker-compose.v2.robot.yml up -d` (the image first: the v2 README, Route A), or lower the fence if the robot goes back to v1 |
| `ros2_container_stopped` (FAIL) | it exists but is not running | v1: `docker logs emori-ros2` — an amd64 image dies here with `exec format error`. v2: `docker logs --tail 50 emori-ros2-v2`, then `docker start emori-ros2-v2` (a missing `/dev/video41` fails its start) |
| `ros2_fiware_bridge_on` (FAIL) | `FIWARE_BRIDGE_ENABLED` is not `0` **inside** the container | fix `deploy/ros2/docker-compose.robot.yml` (v2: `docker-compose.v2.robot.yml`) and recreate the container; otherwise every analytics KPI is counted twice |
| `ros2_owner_drift` (FAIL) | the running container's motion owner ≠ `/opt/emori/.env` | v1: `sudo systemctl restart emori-ros2`. v2: set it in `/opt/emori/deploy/ros2/.env`, then `docker compose -f deploy/ros2/docker-compose.v2.robot.yml up -d` (never `systemctl restart emori-ros2` on a v2 robot: it starts the v1 graph). Compose interpolates once, at `up` |
| `ros2_graph_incomplete` (FAIL) | the container is running but a node is missing from `ros2 node list` | `docker logs emori-ros2` (v2: `emori-ros2-v2`) — `ros2 launch` keeps the rest alive when one node dies at import, so that node's topics publish nothing all session (a missing `hri_msgs`, an unbuilt overlay) |
| `ros2_graph_unlistable` (FAIL) | `ros2 node list` itself failed inside the container | `docker exec emori-ros2 /entrypoint.sh ros2 node list` — usually `/emori/ros2_ws/install` was never built |
| `ros2_graph_unreadable` (SKIP) | the `docker exec` could not run at all | as for `docker_unreachable` |
| `skill_api_not_mounted` (FAIL) | owner is `ros` but `/api/skill` is not mounted | set `EMORI_SKILL_API=1` **and** `EMORI_SKILL_KEY`, restart `emori-hostapp` |
| `two_ros_graphs_running` (FAIL) | the v1 container `emori-ros2` and the v2 container `emori-ros2-v2` both run | stop one: `docker compose -f deploy/ros2/docker-compose.v2.robot.yml down` for v2, `sudo systemctl stop emori-ros2` for v1 |
| `ros2_env_unreadable` (SKIP) | `docker exec printenv` could not read the container's environment | as for `docker_unavailable` |
| `ros2_up_v2` (PASS) | v2: as `ros2_up`, and `:8000` is served by the robot node's shim | — |
| `ros2_up_native` (PASS) | fenced, native route (`reachy-mini-ros2.service`, no container): `:8000` is served by the robot node's shim. The container checks above do not apply | — |
| `v1_graph_on_v2_robot` (FAIL) | fenced: the v1 container `emori-ros2` runs and `emori-ros2-v2` does not, so nothing moves the robot (the v1 graph asks Pollen's daemon, which the fence keeps off) | `sudo systemctl disable --now emori-ros2` (step 11 does it too), then bring the v2 graph up: `docker compose -f deploy/ros2/docker-compose.v2.robot.yml up -d`, or `sudo systemctl start reachy-mini-ros2` on the native route |
| `daemon_port_unreachable` (SKIP) | v2: nothing answers on `:8000` | `docker logs emori-ros2-v2` |
| `shim_not_serving_the_daemon_port` (FAIL) | v2: the container runs but `:8000` is not the shim (no `backend: reachy_mini_ros2`) | the daemon was never stopped (`sudo systemctl stop reachy-mini-daemon`; the fence is `/opt/emori/V2_OWNS_THE_BUS`) or the shim failed to bind: `docker logs emori-ros2-v2 \| grep 'REST shim'` |
| `hostapp_unreachable` (SKIP) | owner is `ros` on an HTTP transport and the hostapp did not answer the intake probe | `systemctl status emori-hostapp` |

The `gaze` gate (v2 face look-at, Step 5c) answers `gaze_lane_off` (PASS: look-at not
armed: no `EMORI_CAM_ROS_VIDEO_NR` in `/etc/default/emori-camera`, which the gate reads,
and no `EMORI_READYCHECK_ROS_VIDEO_DEVICE` override), `ros_camera_ready` (PASS),
`ros_camera_node_missing` (FAIL: the second loopback does not exist; run
`ros-loopback.sh`), `ros_camera_not_fed` (FAIL: it exists but `EMORI_CAM_ROS_VIDEO_NR` is
unset, so the bridge tees nothing into it) or `v4l2_ctl_unavailable` (SKIP).

It asks the *container* about `FIWARE_BRIDGE_ENABLED` and the motion owner (`docker exec
printenv`), never `.env`, because an edited `.env` with nothing restarted is exactly the
failure that reads as "ROS mode does not work". It never runs `env` — the container also
holds `EMORI_SKILL_KEY`, and a readycheck record gets pasted into chats. It also asks the
container `ros2 node list`, because a running container is not a running graph. TOPIC-level
checks stay manual (below) — they need the launch file's `person_id`, and the gate must not
become a second source of truth for it.

### 4. `EMORI_ROS_MOTION_OWNER` — who actually moves the robot
Two knobs on purpose. `EMORI_SKILL_API` says whether the hostapp will *accept* a skill
request at all; `EMORI_ROS_MOTION_OWNER` says which lane may *send* one. Both the ROS facade
and the hostapp intake enforce the owner, so no single edit can open a second writer.

| Value | Hostapp lane | ROS lane | Use |
|---|---|---|---|
| `hostapp` (**default**) | drives | computes, publishes `/skill/*`, the facade POSTs **nothing** | the default robot. Turning the graph on for the first time is a non-event: you get every ROS topic and zero behaviour change |
| `ros` | its engagement + cue motion is suppressed (watchdog / greeting / add-on motion is unaffected) | the facade POSTs `/api/skill` | ROS-driven motion: movement genuinely originates in the ROS graph, still through the one `RobotPlayer` queue |
| `none` | suppressed | POSTs nothing | pure observation / benchmarking |

An unrecognised value reads as `hostapp` on **both** sides — a typo must never be the reason
a second lane starts writing.

**On a v2 robot keep `hostapp`.** Measured on a Reachy Mini Wireless: with
`owner=ros` the robot stands still while it speaks, because the hostapp drops every
expression cue as `not_owner` and the ROS lane makes one gesture per sentence. With
`hostapp` the hostapp plans the speech motion and look-at runs on the `interaction` lane by
itself. `ros` stays available for ROS-driven experiments, set on both sides.

**All three values are rehearsed end to end in the simulator** by
`scripts/rehearse_motion_owner.py` (the harness and how to read its two ledgers:
[motion-owner-rehearsal.md](motion-owner-rehearsal.md)). At `owner=hostapp` the ROS
graph computes the same requests it computes at `owner=ros`, and the facade refuses
every one of them. At `owner=ros` the hostapp accepts them with zero refusals of any
kind, and at `owner=none` only the ungated interaction mechanics (greeting/farewell,
recenter, watchdog) move the head.

Two knobs that are not in the table and default to off:

* `EMORI_ROS_OWNER_FALLBACK_S` (float, default `0` = off). With `owner=ros`, a live
  session, at least one cycle in, and no accepted skill request for that many seconds,
  the hostapp takes its expressive lanes back, logs one WARNING and counts
  `owner_fallback_episodes`; the next accepted request hands them back. It exists for an
  unattended venue where a wedged container would otherwise mean a still robot all
  evening. `none` never falls back. Leave it at `0` while measuring anything.
* `EMORI_SKILL_TRANSPORT` (default `hostapp_http`). The other value, `log`, makes the
  facade record requests instead of sending them — useful on a bench, never on a robot
  you want to move.

To flip it (**both** sides -- the container env alone is not the flip; see below):
```bash
# The sed only edits a line that EXISTS. If the key is absent from .env, the sed
# succeeds while changing nothing: the container then says `ros`, the hostapp has never
# heard of it, and both lanes author motion into one queue. Append when it is missing:
grep -q '^EMORI_ROS_MOTION_OWNER=' /opt/emori/.env \
  && sudo sed -i 's/^EMORI_ROS_MOTION_OWNER=.*/EMORI_ROS_MOTION_OWNER=ros/' /opt/emori/.env \
  || echo 'EMORI_ROS_MOTION_OWNER=ros' | sudo tee -a /opt/emori/.env
# owner=ros needs an intake to POST at: both of these, or every request is 403
grep -E '^EMORI_SKILL_(API|KEY)=' /opt/emori/.env      # EMORI_SKILL_API=1, EMORI_SKILL_KEY=<secret>
sudo systemctl restart emori-hostapp emori-ros2        # BOTH: the container reads .env at `up`
bash /opt/emori/deploy/robot/readycheck.sh | grep ros2   # must be PASS ros2_up, not ros2_owner_drift
```
The container needs the **same** `EMORI_SKILL_KEY` as the hostapp; it is compared with a
constant-time match and a mismatch is a silent 403 `bad_key` on every request.

### 5. Verify the graph is actually receiving live data
```bash
# TOPICS — the three person topics and the one fire-and-forget skill message:
docker exec emori-ros2 bash -lc 'source /opt/ros/humble/setup.bash \
  && source /emori/ros2_ws/install/setup.bash && ros2 topic list | grep -E "/humans|/skill"'
# ACTIONS — replay_motion and look_at are action servers, NOT topics:
docker exec emori-ros2 bash -lc 'source /opt/ros/humble/setup.bash \
  && source /emori/ros2_ws/install/setup.bash && ros2 action list'
# during a live session:
docker exec emori-ros2 bash -lc 'source /opt/ros/humble/setup.bash \
  && source /emori/ros2_ws/install/setup.bash && ros2 topic echo --once \
  /humans/persons/learner_01/context'
```
`topic list` shows `/humans/persons/<id>/{context,engagement_status,speech_cue,anonymous,expression}`,
the `/humans/persons/<id>/context/<key>` fan-out, and `/skill/set_expression`. `action list`
shows `/skill/replay_motion` and `/skill/look_at` — they are **actions**, so `topic list`
only ever shows their `…/_action/{feedback,status}` plumbing and never the bare names;
looking for them there makes a healthy robot read as broken.

Two topics are *listed* whether or not anything crosses them, because their publishers are
created at node start — so a name in `topic list` is not evidence of data:
* `speech_cue` publishes only on the LIVE source, and only while the AI is speaking (a mock
  arc has no voice, which is the honest answer rather than a fabricated one);
* `expression` is withheld on a degraded cycle (`report_flags == 0`), and is dropped
  entirely if this container's `hri_msgs/Expression` spells a label differently — the node
  logs the installed field shape once at start and warns once per dropped label, so
  `docker logs emori-ros2 | grep expression` is the answer, not `topic list`.

That the graph is *running* (rather than the container that hosts it) is the `ros2`
readycheck gate's own `ros2 node list` check — `ros2 launch` keeps the survivors alive when
one node dies at import, so a half-dead graph passes every other check.

With `owner=ros`, the proof that exactly one lane is writing is on the hostapp side:
```bash
curl -s localhost:8090/api/status | python3 -m json.tool | grep -iE "suppressed|skill_"
# engagement_suppressed_by_owner climbs each cycle while the robot still moves;
# skill_accepted climbs with it, and every skill_refused_* stays absent (i.e. 0)
```
The hostapp's own expressive lanes stand down and the motion you see arrives through
`/api/skill` instead. A `skill_refused_bad_key` that climbs means the container and the
hostapp hold different `EMORI_SKILL_KEY` values — the comparison is constant-time and the
403 is otherwise silent.

The graph's own half of the same ledger is on `/diagnostics`, every 5 s:
```bash
docker exec emori-ros2 bash -lc 'source /opt/ros/humble/setup.bash \
  && source /emori/ros2_ws/install/setup.bash && timeout 12 ros2 topic echo /diagnostics'
```
Two statuses: `emori_skill_facade` (`posted accepted dropped refused suppressed
unreachable`, plus `motion_owner`, `transport`, `url_host`) and
`hri_engagement_behaviors` (`cycles engagement cue yielded_to_speech cooldown
expression_changes settles`, plus `sent`, `dropped`, `result_*`, `motion_owner`,
`config_source`). `posted` on one side and `skill_accepted` on the other should
reconcile request for request; if they do not, something between them is eating requests.
`config_source=yaml:/emori/configs/behavior.yaml` is the proof the tuning file was
actually read — it is bind-mounted read-only from
`${EMORI_ROS_BEHAVIOR_CONFIG_HOST:-/opt/emori/configs/behavior.yaml}`, and the node logs
an ERROR naming the path if it was set but unreadable.

> **A second container on the same graph needs a UDP-only transport.** Containers that
> share a network namespace look like one host to Fast DDS, which then prefers shared
> memory — but `/dev/shm` is per-container, so discovery works and delivery silently does
> not. Start any such consumer with `FASTDDS_BUILTIN_TRANSPORTS=UDPv4`. A perfect
> `ros2 topic list` with zero samples is this, nine times out of ten.

See `docs/ros4hri-mapping.md` for the full realized graph, the `SpeechCue` payload, the
`/api/skill` contract and the privacy gate; `docs/motion-owner-rehearsal.md` for the
rehearsal harness and how to read the two ledgers.

## Step 5c — v2 face look-at (optional, v2 only)

Face look-at is the v2 robot node turning the head toward the largest face it sees. It
reads a second, low-resolution loopback (`/dev/video41`, 320x240 @ 10 fps) that the
camera bridge tees off the kiosk camera, and runs the vendored YuNet detector inside the
`emori-ros2-v2` container. Bring it up one step at a time, each revertable on its own:
the camera tee (`EMORI_CAM_ROS_VIDEO_NR=41`, Step 2), then the container, then look-at
itself (the `/dev/video41` section of
[deploy/robot/ros2_native/README.md](../deploy/robot/ros2_native/README.md); readycheck's
`gaze` gate).

What still holds, and what to reach for when it misbehaves:

| Knob or fact | Effect |
|---|---|
| `EMORI_CAM_ROS_VIDEO_NR=` (empty) in `/etc/default/emori-camera`, then restart `emori-camera-bridge` | The one-line rollback of the camera change: no tee, the pipeline the kiosk has always run |
| `EMORI_GESTURE_RESIDUAL=0` in `/opt/emori/.env`, then restart `emori-hostapp` | Puts push-turn back on the absolute head pose and reverts the cue lane, the skill intake's 423 and the idle scan together, on purpose |
| `POST :8000/api/emori/gaze {"enabled":false}` | Turns look-at off at runtime; the head returns to the pose it held when look-at was enabled |
| `EMORI_GAZE_DETECT_HZ` (`5.0`) | Measured on the CM4: YuNet 71 ms per 320x240 frame; `/joint_states` 45.4 Hz at 5 Hz detection, 46.6 Hz at 3 Hz. Keep `/joint_states` above 45 Hz (the node itself WARNs only below 40 Hz); lower it first when it dips |
| `docker compose ... up -d` vs `docker restart -t 20` | `up -d` RECREATES the container and discards anything `docker cp`'d into it; `restart` keeps it. Recreate first, patch after |
| `docker compose up -d` on the robot | Compose does not read `/opt/emori/.env`: it takes its variables from the shell or `/opt/emori/deploy/ros2/.env` (absent by default). Its motion owner defaults to `hostapp`, which is what a v2 robot runs (with `ros` the robot stands still while it speaks, measured); set `ros` there and in `/opt/emori/.env` only for ROS-driven motion |
| Model check | Read the sha256 INSIDE the container (`docker exec emori-ros2-v2 sha256sum /opt/emori/models/face_detection_yunet_2026may.onnx`, expect `ebafce4e…22f0`); the host copy is mounted beside it at `/opt/emori/models-host` and is not what the node opens |
| Shipping a new v2 image | Never `docker save` piped into `docker load` on the robot (the card does not have the space for both); push through a local registry over `ssh -R 5000`, with at least 700 MB free before the pull. The v1 Humble tarball of Step 5b §1 is different: `install.sh` step 11 loads it with `docker load`, and that is the supported v1 route |
| `v4l2loopback` | `exclusive_caps=1` needs a live writer before a reader sees capture caps, so start the producer first; `v4l2loopback-ctl add` creates a root:root 0600 node, which `deploy/robot/ros-loopback.sh` fixes to root:video 0660 |
| GStreamer on the robot | `decodebin` stalls in PREROLLING on the loopback (use `avidemux ! jpegdec`), and there is no `avdec_h264` (ship MJPEG AVI) |
| `restart emori-hostapp` | Also restarts the kiosk (`PartOf=emori-hostapp.service` in `kiosk.service`) |

## Validation checklist (on the unit)
1. `rpicam-hello --list-cameras` → imx708 present.
2. Daemon `--no-media` after a **reboot** (the drop-in): `/api/daemon/status` →
   `no_media:true`, motors report `enabled` once the hostapp is up (they boot
   `disabled`), and a `goto` physically moves the head (verify a `head_pose` delta in
   `/api/state/full`, not just a 200). *The head-tilt gesture needs
   `gravity_compensation`, which daemon v1.9.0 refuses (see Step 1); check the push-turn
   instead.*
3. Camera bridge up; `v4l2-ctl -d /dev/video40 --list-formats-ext` → CAPTURE YUY2.
4. Kiosk Chromium: `https://webrtc.github.io/samples/src/content/getusermedia/resolution/`
   → **ReachyCam** lists + live video (toggle the PipeWire flag if not).
5. `curl localhost:8090/api/status` → `robot:true, tts:true, stt:true`; show a badge →
   the robot waves + greets by name through the **robot speaker** (Piper, no internet).
6. Say "one" → SSO → Pokamind session starts (the device check auto-advances); the AI voice
   plays from the speaker; the robot does talking gestures + body language.
7. Walk away mid-session / cover the camera → null-body frames are dropped (no freeze;
   `/api/status` parse_errors stays 0) and the idle ticker keeps the robot alive.
   `/api/status` `last_present_s` climbs while `body_ever_seen` stays true; with
   `EMORI_ABSENCE_S` enabled (default 0 = off) the session then ends via
   `presence_ends`, WITH the analysis. Presence comes from these Pokamind cycles —
   never from the daemon (see "Presence / walk-away").
8. Finish a session → spoken summary → `artifacts/outbox/` (or a real email if SMTP is
   set) → the kiosk is back on `/` for the next learner; Grafana shows the session.
9. Replay determinism: `python scripts/replay_session.py tests/fixtures/live_session.jsonl
   --base-url http://localhost:8090 --fast` exercises the whole stack robot-side.
10. 10–15 min soak: `vcgencmd measure_temp` + `top` — no thermal throttle (NV12/720p).
11. Resilience: kill the gst bridge → systemd restarts it → Chromium recovers the camera;
    reboot → units come up in order (daemon → hostapp → bridge → kiosk).

## Known risks
- **One process owns libcamera** — keep the daemon in `--no-media` (the drop-in) so it
  never reacquires the camera (an `acquire_media()` from any client kills the bridge).
  This is also why the daemon can never be a presence/face sensor — see
  "Presence / walk-away" below before touching walk-away logic.
- **`release_media()` is all-or-nothing** (camera+mic+speaker + signaling), so the
  daemon's audio-reactive "wobble" is off — fine, the hostapp drives the head.
- **Echo**: the AI voice exits the speaker centimetres from the reSpeaker mic that feeds
  Pokamind's analysis. Chromium's getUserMedia echoCancellation is the mitigation;
  validate it in the Xvfb/ALSA configuration during validation step 6.
- **CPU/thermal** on the CM4 with bridge + Chromium + daemon — mitigate with NV12 native
  caps, 720p@30, no rotation.
- **Producer-before-consumer**: `exclusive_caps=1` means `/dev/video40` only advertises
  CAPTURE once the gst producer is feeding it — start the bridge before the kiosk.
- **Clock**: partner SSO JWTs carry `iat`/`exp` minted on-robot — keep NTP active
  (`timedatectl`), or SSO will 401 after drift.
- **Data at rest**: `configs/roster.yaml` holds staff PII and `artifacts/{outbox,
  recordings,last_results.json}` hold session data, unencrypted on a shared device.
  Keep the roster minimal, prefer the anonymous flow where acceptable, and add a cron
  cleanup for `artifacts/` (e.g. `find /opt/emori/artifacts -mtime +30 -delete`)
  aligned with your retention policy.

## Presence / walk-away — the supported signal, and the daemon dead end

**Do NOT wire presence or walk-away to the robot daemon.** The daemon is forced to
`--no-media` on every boot (`deploy/robot/emori-daemon-launcher.sh` →
`... --no-wake-up-on-start --no-media`, installed by
`deploy/robot/reachy-mini-daemon-override.conf`) so that EMORI owns the camera
(gst bridge → `/dev/video40` → Chromium) and the mic/speaker (kiosk + Piper). A
media-less daemon has no vision pipeline and no beamformer, therefore:

- `GET /api/daemon/status` reports `no_media: true` and its **`face_target` is never
  populated** (it reports `detected:false`);
- there is **no usable direction-of-arrival / `doa`** value to key presence off;
- **never** call a route that makes the daemon reacquire media — `POST
  /api/media/acquire`, or any face-tracking enable route (`tracking/enable`). One
  process owns libcamera: an `acquire_media()` from any
  client **kills the camera bridge**, and `release_media()` is all-or-nothing
  (camera + mic + speaker + signaling), so you also lose the mic and the speaker. Not
  from the hostapp, not from a debug script, not "just to check".

Reading `face_target`/`doa` is harmless-but-empty; the trap is the follow-up thought
*"then let me enable media first"*. That trade is never worth it — it takes the live
session down. (The `play_sound` / `wobbling` routes listed in `hardware-notes.md` are
media routes too, and are correspondingly inert here — that is why the daemon's
audio-reactive wobble is off and the hostapp drives the head instead.)

**The supported presence signal is Pokamind's per-cycle body analysis**, already
wired:

| Fact | Where |
|---|---|
| a cycle carrying `body_language_metrics`/`body_language` proves a body was seen | `has_body` in `hostapp/driver.py` — the same field `translation/engagement.py` reads |
| camera evidence refreshes the presence clock **and** arms the session latch | `LiveDriver.mark_present()` (`hostapp/driver.py`) |
| an accepted turn refreshes the clock but **never arms** the latch (audio must not enable an absence-end) | `LiveDriver.refresh_presence()`, called from `on_turn_submitted` |
| ambient VAD refreshes **nothing** session-level (a noisy room is not presence) | `on_user_speaking_activity` (no `touch()`) |
| the end decision | `WatchdogTicker._absence_due()`; fires `_request_pokamind_end("presence_ends")` so the learner's analysis, spoken summary and results email are still captured |
| observability on the robot | `GET /api/status` → `last_present_s`, `body_ever_seen` |
| the knob | `EMORI_ABSENCE_S` — **default `0`, i.e. OFF** (`.env.example`). Enable it on the robot (suggested start: `20`) and tune against a learner who looks down at notes mid-answer. While it is off, a departed learner still ends through the blind `EMORI_WALKAWAY_S=45` timer. |

A camera-off / audio-only session never arms `body_ever_seen`, so it can never
absence-end — by design; it falls through to `EMORI_WALKAWAY_S`.

If the Pokamind body signal is ever *measured* unreliable on the robot (watch
`body_ever_seen` and `last_present_s` in `/api/status` across real sessions), the only
sanctioned fallback is adapter-side and daemon-independent: open the v4l2loopback
**CAPTURE** device `/dev/video40` (multi-reader) from the hostapp and run a cheap
face/motion detector. Budget it against CM4 thermals first — Chromium, Piper and the
gst bridge already share that CPU (see "CPU/thermal" above) — and justify it with
measurements, not a hunch. Still no daemon media, ever.
