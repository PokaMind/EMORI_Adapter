# ADR-001 — Run the Pokamind session on the robot, screenless & audio-first, using the robot's own camera

**Status:** Accepted. A screen remains possible but is not covered here.

## Context
EMORI puts a Reachy Mini in a shared space such as a staff room; the learner must
**interact with the robot** (not a laptop/tablet), it must be **portable**, and it must use
the **robot's own camera, mic and speaker**. The Pokamind session runs in a web browser
(camera/mic via `getUserMedia`, the AI voice played by the page). Hardware facts:

- The **Reachy Mini Wireless has no display output** (no HDMI/DSI/USB-DP; the one USB-C is
  data-only/reflash; no spare USB) — you **cannot attach a screen** to it.
- The brain is a **CM4 (4 GB)** running a **headless** Raspberry Pi OS; the reachy daemon's
  GStreamer media server owns the camera+mic+speaker together.
- The session client is light (a frame every ~2 s + 10 s audio chunks; the analysis runs in
  the session service), its AI voice is played by the client, and a **headless device can
  sign in through partner SSO with a per-robot identity** (no human login). Audio-only
  sessions are supported, and a `module_id` in the SSO request skips topic selection.

The "portable + screen + robot-is-the-sensor" trilemma can't all hold with this hardware.
Dropping the **screen** (not the camera) resolves it and is the most embodied option.

## Decision
Run **the session on the CM4, screenless and audio-first**, using the robot's own devices:

- **Real Chromium on a virtual display (Xvfb)** loads the EMORI host app → the Pokamind
  session iframe (partner SSO with a per-robot identity, `module_id` pre-pick). Not
  `--headless` (that emits no audio). Auto-grant media
  (`--use-fake-ui-for-media-stream`, `--autoplay-policy=no-user-gesture-required`).
- **The robot's built-in CSI camera (IMX708)** feeds the session via a
  `libcamera → v4l2loopback (/dev/video40) → Chromium getUserMedia` bridge (a libcamera CSI
  cam isn't a plain V4L2 device; on Trixie also disable Chromium's "PipeWire Camera support").
- **The daemon runs `--no-media`** (motion only) so Chromium owns camera+mic+speaker;
  **motion is unaffected** (motors are independent of the media server). Same-device
  mic+speaker means the browser's **AEC cancels echo for free**, and the session's **own
  voice plays on the robot speaker** — no audio routing, no cross-origin hack.
- **Topic** = a `module_id` in the SSO request, chosen from the spoken menu or the badge.
  **Results** = a short **spoken** wrap-up + the detailed report on the **FIWARE/Grafana**
  trainer dashboard.
- The **adapter** keeps driving body language from the per-cycle relay; the **camera/screen
  problem is gone** because there is no screen.

## Alternatives rejected
- **Wireless + tablet** (screen + tablet camera/mic): viable and simplest, but the *tablet*
  is the sensor, not the robot → fails "robot is the camera/mic".
- **Lite + host PC + touchscreen**: gives robot-sensors + a screen, but **not portable**.
- **`chromium --headless`**: no speaker audio. **USB webcam**: no spare USB port on the Wireless.
- **Native (browser-free) session client**: feasible and lighter, but it would have to
  re-implement the client's sign-in, byte-exact audio chunking and TTS playback outside the
  browser. **Deferred.**

## Consequences
- **Positive:** fully embodied (robot sees/hears/speaks/moves), portable, no screen needed,
  every analysis modality of the session intact, reuses the entire EMORI stack (adapter,
  relay, FIWARE), and the audio/echo complexity disappears.
- **Costs/risks:** add a virtual-display + Chromium kiosk + camera-bridge to a headless CM4;
  manage the daemon↔Chromium device handoff (`--no-media`); CM4 CPU/thermal headroom; the
  Trixie PipeWire-camera flag; persisting the stack across OS reflashes. See
  [onrobot-runbook.md](onrobot-runbook.md).
- **Validated in simulation (this repo):** the full software path — session lifecycle
  (topic → opening → live analysis → robot motion + "talking" + AI turn → spoken summary) →
  MuJoCo motion + FIWARE time-series. The camera-bridge / Chromium-kiosk / `--no-media`
  handoff are robot-only and validated on the unit per the runbook.

## Identity & session start (QR staff badge)
Screenless means no typed login, so identity = **identification**, which drives the
per-user partner SSO. Decision: **anonymous drop-in by default, opt-in identity via a QR
staff badge.**

- The kiosk landing page scans the staff member's **QR badge with the robot camera**
  (jsQR, in-browser — the browser owns the camera, so no contention with the host app) and
  POSTs only the decoded payload to **`/api/identify`**.
- The host app resolves the payload against a small **roster** (`configs/roster.yaml`,
  badge → email/student_id + their assigned `module_id`/`pathway`; PII stays on the robot)
  and mints the partner SSO token.
- Unknown/empty → an **anonymous shared-robot identity** (frictionless, psychologically-safe
  practice, no per-staff tracking). Identified sessions give per-staff progress + onboarding
  completion + the trainer dashboard value, and the badge's `module_id`/`pathway` also
  **solves screenless topic selection** in the same tap.
- No extra hardware (reuses the camera; the Wireless has no spare USB for an NFC reader);
  no personal phone required (keeps it device-free). Per-staff data is opt-in + transparent.

Implemented in `src/emori_adapter/hostapp/identity.py` + `/api/identify` + the kiosk
identify page; verified in tests (`tests/test_identity.py`). NFC is a later hardware revision.

## Follow-ups
A native browser-free client (smaller footprint). Validate CM4 thermals on each new unit.
