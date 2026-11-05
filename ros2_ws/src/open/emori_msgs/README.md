# emori_msgs

The interfaces the EMORI graph adds on top of [ROS4HRI](https://www.ros.org/reps/rep-0155.html).
REP-155 says what a *person* looks like on the graph; it says nothing about the signals
of a conversational session that a social robot actually reasons over. `emori_msgs` is
that gap, and only that gap: interfaces and no code.

Licence: Apache-2.0 (`LICENSE`). Copyright 2026 Pokamind AB.

## Why it is its own package

Everything else in the EMORI stack can be replaced without touching the wire. The wire
itself cannot: a compiled subscriber binds field offsets, so a reorder is a silent
mis-read on a robot in front of a person. `emori_msgs` is therefore append-only by rule
(pinned by `tests/test_ros_contracts.py` in the parent repository) and carries its
contract as message constants, so a consumer can read the whole thing with

    ros2 interface show emori_msgs/msg/Context

and nothing of ours installed.

## Interfaces

| Interface | What it carries |
|---|---|
| `msg/Context` | One analysis cycle for one person: engagement score/level, valence, arousal, expression + confidence, trend, escalation, rapport, attention, the dominant communication style and the top-5 styles, the body/face/voice observations, `report_flags`, `lang`, `age_s`. A REP-155 extension, not a replacement. |
| `msg/SpeechCue` | One sentence the assistant is about to say: the cue family performed and the one Pokamind authored, the estimated duration, the source clock, `source` (`turn` or `scenario`) and an `ended` marker. `text` is empty unless `EMORI_ROS_SPEECH_TEXT=1`. The behaviour lane budgets the head from this. |
| `msg/SetExpression` | The bounded expression request: label, confidence, intensity, and a `reason` string that makes every motion traceable to the decision that asked for it. |
| `action/ReplayMotion` | Play a named motion at an intensity. Result carries `error_code` so a refusal is legible, not just a failure. |
| `action/LookAt` | Aim the head, bounded in yaw/pitch/duration. Same result shape. |

### The hub's interfaces

The process that hosts the session logic beside the graph (`emori_hub`) publishes and serves
these. They need nothing but this package to read.

| Interface | What it carries |
|---|---|
| `msg/SessionState` | Latched. The kiosk session: phase, counters, watchdog state, last outcome. Ids are pseudonyms unless `EMORI_ROS_DEIDENTIFY=0`. |
| `msg/SessionEvent` | One message per relayed session envelope, typed and gated, in order. `seq` has no gaps, so a missing number is a loss you can see. `text` is empty unless `EMORI_ROS_SPEECH_TEXT=1`; `payload_json` is empty unless `EMORI_ROS_RAW_ENVELOPES=1`. |
| `msg/GestureEvent`, `msg/GestureState` | A recognised head gesture; the gesture sensor's state (latched). |
| `msg/VoiceState` | The robot's own voice as its speaker output shows it. |
| `msg/PlayerState` | Latched. The motion player's queue and owner. |
| `msg/MotionIntent` | The adapter's motion command in the same units. |
| `msg/MotionPlanEvent` | On `/emori/motion/plan_events`. One step in a sentence's motion plan: planned, started, ended. Carries the family performed, the family Pokamind forwarded and how it was chosen, the outcome (`finished`, `preempted`, `voice_end`, `skipped` with a `skip_reason`) and the delay to the first command. |
| `msg/AddonStatus`, `msg/KioskLanguage` | Latched. The installed add-on; the kiosk's live language. |
| `srv/SessionCommand`, `srv/StartSession`, `srv/GetVoiceMenu`, `srv/SetTtOverrides` | Session: begin, end, start from a menu choice or a badge, read the spoken menu, patch turn-taking overrides. |
| `srv/SetLanguage`, `srv/Recognize`, `action/Say` | Speech: set the language, recognise a clip, speak a sentence. |
| `srv/SubmitMotionBatch` | Hand a batch to the motion player. |
| `srv/GetHubStatus` | What the status and health routes answer, without credentials. |

## The constants on `Context`

The two things a consumer must know to read a `Context` correctly are constants on the
message, not documentation:

| Constant | Value | Meaning |
|---|---|---|
| `NOT_REPORTED` | `-1.0` | This float was **not observed**. It is not a zero. A plotted zero and a measured zero are the same pixel; the sentinel is what keeps "nobody looked" distinguishable from "scored nothing". |
| `REPORT_BODY` | `1` | bit 0 of `report_flags`: the body channel reported this cycle |
| `REPORT_FACE` | `2` | bit 1: the face channel reported |
| `REPORT_TONE` | `4` | bit 2: the voice channel reported |
| `REPORT_VERBAL` | `8` | bit 3: the verbal channel reported |
| `REPORT_STYLES` | `16` | bit 4: the communication-style ranking is present |

`report_flags == 0` means the cycle is degraded: no channel reported, so every
flag-gated value in it is meaningless regardless of what it contains.

## Building it on its own

On a plain ROS 2 Humble with no EMORI anything:

    mkdir -p ws/src && cd ws
    cp -r <this directory> src/emori_msgs
    rosdep install --from-paths src -y --ignore-src
    colcon build --packages-select emori_msgs
    source install/setup.bash
    ros2 interface list | grep emori_msgs

Dependencies are `std_msgs`, `builtin_interfaces` and `action_msgs` — all of
`ros-humble-ros-base`. Nothing else.

## Compatibility rule

Append fields at the END, never reorder, never rename, never change a type. A new
optional channel is a new field plus a new `REPORT_*` bit; a removed one keeps its field
and stops setting its bit. The parent repository's contract tests fail the build if this
rule is broken.
