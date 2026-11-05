# Guide 3 — How the robot reacts to a session

This explains the path from a Pokamind analysis cycle to a Reachy Mini movement,
and the exact behaviour policy.

## The pipeline (one cycle, ~2 s)

```
AnalysisCycle (31 styles + metrics + body/prosody; nulls dropped)
   │  translation/
   ├─ engagement.py  → engagement_score 0..1 → EngagementLevel (hysteresis + trend)
   ├─ expression.py  → valence/arousal → hri Expression label
   └─ context.py     → engagement_trend, escalation, rapport, attention, note
   ▼
DerivedSignals ──► behavior/engine.py (BehaviorEngine.step) ──► [MotionIntent, ...]
   ▼
robot/client.py (HttpRobotClient) ──► daemon REST /api/move/goto ──► Reachy Mini moves
```

Three entry points run this same code: the **hostapp live driver** (the kiosk page
POSTs each cycle to `/api/cycle`), **`emori-run`** from the CLI, and the **ROS 2**
nodes from `/humans/*` topics.

## Leaving with the head

Push the head back firmly and hold it for a little over half a second. Whatever is running ends:
a session goes through Pokamind's end so its analysis still lands, and an add-on mode goes back
to idle. The antennas flick twice when the push is felt, and in a session the current sentence's
movement stops, because the end itself can take many seconds. It reads where the head is against where
the motors were told to put it, so it also works while the robot is moving. Over a scripted
session with nobody touching the head, the head stayed back of its command past the threshold
for 0.27 s at most, and a hold takes about 0.6 s. During a session's opening, the end waits for
the first turn, because the page refuses it before then. At idle a push does nothing.

## How engagement is computed

`engagement_score` (0..1) fuses, in order of reliability:
1. **metrics Pokamind already computes** (weight 0.55) — on the **live** contract
   that is `body_language_metrics.engagement` only (facial metrics never arrive
   live); the richer mock also feeds `facial.engagement` / `facial.eye_contact`;
2. **communication-style mass** (weight 0.30) — engagement-positive styles
   (mirroring, acknowledging, investigative, exploratory, lively, energetic,
   complimentary, still) minus negative (withdrawing, muted, quiet, abrupt,
   reserved, ambiguous), measured as a **deviation from the session's own running
   baseline**: the live distribution is dense (31 styles at ~3–10% each), so
   absolute masses barely move — the adaptive baseline restores the dynamic range
   on both the live and mock shapes (tunable: `style_gain`, `style_alpha`,
   `style_warmup`);
3. **categorical body language** (weight 0.15) — lean_forward / nodding raise it,
   lean_back / shaking lower it (live values are folded onto the canonical
   vocabulary first).

When the learner looks away, Pokamind sends a **null body frame** (every body value
`null`): the nulls are dropped and the cycle degrades to a styles-only estimate —
the robot keeps moving instead of freezing.

The score is quantised to the REP-155 `EngagementLevel` with a dead-zone + trend so
the robot doesn't flip-flop on noise (bands are tunable in `configs/behavior.yaml`,
`translation:` section):

| Condition | Level |
|---|---|
| score ≥ 0.62 (`engagement_high`) | `ENGAGED` |
| score ≤ 0.38 (`engagement_low`) | `DISENGAGED` |
| mid-band, rising | `ENGAGING` |
| mid-band, falling | `DISENGAGING` |
| mid-band, stable | keeps the previous level |

## Session-event gestures (the live kiosk)

On top of the per-cycle policy, the hostapp driver fires:

| Event | Robot behaviour |
|---|---|
| badge scanned / session starts | **greeting gesture** (+ spoken greeting & topic menu) |
| AI utterance (`pokamind_ai_utterance`) | **talking gesture** while the AI voice plays (beats scale with text length). A dedup guard drops literal double-deliveries (same `seq`+text); legitimately repeated lines still gesture |
| no data for `EMORI_IDLE_AFTER_S` (default 8 s) | **idle ticker**: a gentle look-around every 5 s, so a stream stall or a between-sessions lull never freezes the robot |
| session completed | **farewell gesture** + spoken summary |

## How a sentence's motion stays with its voice

The robot listens to its own speaker, so its head follows what is really being said rather
than a guess at when it will be said:

- A sentence's plan waits for its voice to start, and its first move replaces whatever the head
  is still doing, so it never waits behind the last sentence.
- Only one head move runs at a time. A beat is not released until the last one has finished, so
  moves cannot build up and fall further and further behind.
- When the voice has been silent for 0.40 s the head settles, even in the middle of a move, and
  it picks up again if the voice comes back. A sentence that outlasts its plan keeps moving in
  the same family until its last word.
- A gesture from the engagement lane gives way as soon as a sentence starts, and brings the head
  back as it goes.
- A recorded move starts just before its first motion, so a recording that opens with a held
  pose does not leave the head still under the first words.

`EMORI_MOTION_SYNC=0` puts back the older timing. `scripts/motion_sync_run.py` and
`scripts/motion_sync_report.py` measure all of this per sentence on the robot.

Even so, a head takes longer to answer a command than a voice takes to reach the speaker.
`EMORI_VOICE_DELAY_MS` holds the voice back to close that gap. Try it with real sessions first:
the page thinks the voice has ended sooner than the room hears it, so the microphone can open
on the end of a sentence.

## The per-cycle behaviour policy (highest priority first)

| Situation | Robot behaviour | Why |
|---|---|---|
| **Escalation** (`context.escalation ≥ 0.6` — sustained high arousal + negative valence) | disable wobble + slow, grounded head + softened (drooped) antennas | **de-escalation**: never escalate back; healthcare context |
| **DISENGAGING / attention dropping** | return gaze to learner + affirming nod + antenna re-capture wiggle (+ optional "mm-hm") | **engagement recovery** (the >70% recovery-success metric) |
| **DISENGAGED** | curious head tilt + antenna wiggle | gentle attention seeking |
| **ENGAGING** | perk antennas + lean in | affirm the re-engagement |
| **ENGAGED** | attentive gaze that gently scans the learner + a slow nod every 4 cycles | maintain visible presence (the >80% attention metric) |

Then **affect** modulates antenna posture when the head gesture leaves them free:
negative valence → empathetic droop; strong positive valence + arousal → perk
(mirroring positivity).

Cooldowns prevent spamming: the full recovery routine fires at most every 2 cycles,
audio backchannels at most every 3. On the live path, engagement gestures are also
**dropped when the robot is mid-gesture** (speech gestures never are) so reactions
stay real-time instead of queueing up.

## Expressive outputs used

6-DOF head (gaze / nod / tilt), body yaw, 2 antennas (perk / droop / wiggle), speaker
(robot voice + backchannel), and the daemon's audio-reactive "wobbling" (disabled
during de-escalation). **No eyes/LEDs** — the robot has none
([hardware-notes.md](../hardware-notes.md)).

## Watch it happen

- **Live kiosk**: the MuJoCo window moves; `GET http://localhost:8095/api/status`
  shows the counters (`cycles`, `utterances`, `utterances_deduped`, `idle_ticks`,
  `parse_errors`, `relay_clients`, ...) and the active session.
- **`emori-run`**: the console prints the per-cycle decision trace:

  ```
  cyc 06 | eng=0.46 DISENGAGING | val=-0.32 aro=-0.68 sad | attention-dropping
         | -> recovery: return gaze to learner, recovery: affirming nod, recovery: re-capture attention
  ```

- **ROS 2 / FIWARE**: `docker logs -f emori-ros2` shows the live publisher; the
  Grafana dashboard (`http://localhost:3003/d/emori-session`) plots the metrics
  over time.

## Tuning

`configs/behavior.yaml` has two sections, applied on **every** path (hostapp live
driver, `emori-run --config`, ROS 2):

- `behavior:` — escalation threshold, cooldowns, nod cadence, backchannel sound;
- `translation:` — the engagement bands (`engagement_high: 0.62`,
  `engagement_low: 0.38`) and the adaptive style scaling (`style_gain: 4.0`,
  `style_alpha: 0.05`, `style_warmup: 3`).

The hostapp loads it automatically (override the path with `EMORI_BEHAVIOR_CONFIG`);
pass `--config configs/behavior.yaml` to `emori-run`; ROS 2 node parameters live in
`ros2_ws/src/emori/emori_adapter_bringup/config/topics.yaml`. The idle cadence is the
`EMORI_IDLE_AFTER_S` env var (default 8; `0` disables the ticker).
