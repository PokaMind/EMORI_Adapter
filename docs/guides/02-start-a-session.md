# Guide 2 — Start a training session

A "session" is one run of Pokamind analysis flowing into the robot. The **hostapp is
the single live driver**: the kiosk session page
POSTs every `pokamind_*` postMessage to `POST /api/cycle`, which drives the robot,
FIWARE, the `/ws/relay` broadcast (ROS 2 + recorder), and the session tracker.
`GET /api/status` shows the live counters at any moment.

Boot the stack first ([Guide 1](01-start-the-simulation.md)):

```powershell
.\scripts\sim_full_up.ps1      # daemon + FIWARE + ROS 2 (live) + hostapp on :8095
```

## 0. Speech models (one-time, recommended)

The robot speaks (server-side Piper TTS, `GET /api/tts`) and listens (server-side
Vosk STT, `POST /api/stt`). Without the models the kiosk pages fall back to the
browser's `speechSynthesis` / Web Speech automatically (needs Chrome/Edge + internet).

```powershell
uv pip install --python .venv-reachy -e ".[speech,stt]"
.\scripts\get_voice_models.ps1   # Piper -> voices\{ca,en}.onnx ; Vosk -> models\vosk-{ca,en}
```

## 1. Live session against Pokamind

A live session needs a **robot token** from Pokamind and, only for a non-default server,
`POKAMIND_BASE`:

- `POKAMIND_BASE`, the Pokamind API origin: empty means the public one
  (`https://api.pokamind.com`); set it in `.env` only for another server;
- a **robot token** that Pokamind issued for this robot (`pkr1.<robot-id>.<…>`). Put it in
  `secrets\pokamind_robot_token` at the repository root (`secrets/` is gitignored) or,
  for development only, in `POKAMIND_ROBOT_TOKEN`. What the token is and how to get,
  install and test it: [deploy-a-new-robot.md](../deploy-a-new-robot.md) and
  [configuration.md](../configuration.md#this-robots-pokamind-token-per-robot).

Start the simulator stack, then open the kiosk:

```powershell
.\scripts\sim_full_up.ps1                    # daemon + FIWARE + ROS 2 + hostapp on :8095
.\scripts\launch_kiosk_browser.ps1 -OpenOnly # just the Chrome kiosk on the running stack
```

(Add `-Kiosk` for fullscreen. The launcher auto-grants camera and mic and allows audio
autoplay, so the screenless flow below runs with no prompts.)

The flow is fully **screenless** — no touching the kiosk:

1. the kiosk lands on `/` and watches through the camera;
2. show a **staff QR badge** to the camera — the QR code of the sample roster entry
   `EMORI:anna-001` (make one with
   `.\.venv-reachy\Scripts\python.exe -c "import segno; segno.make('EMORI:anna-001').save('anna-001.png', scale=10)"`,
   or print badges from the admin People tab); it is decoded by vendored jsQR, with no
   CDN — or do nothing and stay **anonymous**;
3. the robot performs a **greeting gesture** and speaks the greeting + topic menu;
4. the learner says **"one" / "two" / "three"** to pick the module;
5. the hostapp mints the partner **SSO** JWT and `/session` loads — this is the
   moment the session activates — Pokamind runs in the iframe;
6. the robot reacts to every analysis cycle ([Guide 3](03-how-the-robot-reacts.md));
7. on completion the robot speaks a **summary**, the results report is emailed
   (written to `artifacts\outbox\<session>.html` when no SMTP is configured), and
   the kiosk loops back to `/` for the next learner.

Every session is auto-recorded to `artifacts\recordings\<session>.jsonl`.

## 2. Deterministic replay (no Pokamind needed)

Replay a recording through the full live stack — robot, FIWARE, ROS 2, summary,
email, all of it:

```powershell
.\scripts\sim_full_up.ps1 -Replay tests\fixtures\live_session.jsonl

# or against an already-running stack (--fast skips the original inter-cycle timing):
.\.venv-reachy\Scripts\python.exe scripts\replay_session.py artifacts\recordings\<session>.jsonl --base-url http://localhost:8095
```

## 3. Dev loops without the hostapp: `emori-run`

`emori-run` drives the same translation → behaviour pipeline from the CLI.
Sources: `mock | csv | pokamind`. Robot clients: `http | dry`.

```powershell
# mock arc -> simulated robot, real time (~2 s cadence)
.\.venv-reachy\Scripts\emori-run.exe --source mock --robot http --base-url http://127.0.0.1:8000

# fast logic-only trace (no robot, no network) - great for understanding the mapping
.\.venv-reachy\Scripts\emori-run.exe --source mock --robot dry --no-realtime

# replay a captured Pokamind data.csv
.\.venv-reachy\Scripts\emori-run.exe --source csv --csv path\to\data.csv --robot http

# tap the LIVE relay as an extra consumer (hostapp running + a session active;
# the default URL is ws://localhost:8080/ws/relay - point it at :8095 for the simulator stack)
.\.venv-reachy\Scripts\emori-run.exe --source pokamind --ws-url ws://localhost:8095/ws/relay --robot dry

# historize each cycle to Orion-LD + load the tuning file (behaviour AND translation)
.\.venv-reachy\Scripts\emori-run.exe --source mock --robot dry --no-realtime --fiware --config configs\behavior.yaml
```

You'll see a per-cycle trace and a summary table: engagement level, valence/arousal,
the expression label, the context note, and which gestures fired.

## Watching the ROS 2 side

With the full stack up, the ROS 2 container republishes the live session as ROS4HRI
messages. The person namespace is always `learner_01`; the badge identity travels inside
`emori_msgs/Context`:

```powershell
docker exec emori-ros2 /entrypoint.sh ros2 topic echo /humans/persons/learner_01/context
```

More commands, and what each node publishes:
[../ros4hri-mapping.md](../ros4hri-mapping.md#inspect-at-runtime).

The SSO contract and the per-cycle envelope shape are documented in
**[../pokamind-integration.md](../pokamind-integration.md)**.

Next: **[Guide 3 — How the robot reacts](03-how-the-robot-reacts.md)**.
