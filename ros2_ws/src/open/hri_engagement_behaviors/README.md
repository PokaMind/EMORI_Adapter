# hri_engagement_behaviors

Engagement-to-behaviour mapping. It reads the ROS4HRI
engagement level, the EMORI context and the sentence-level speech cue for one person, and
decides what the robot should do about it. It emits **at most one bounded skill request
per event**, and it never moves a robot itself: it holds no robot URL, no daemon client
and no motion code. `emori_skill_facade` is the only component allowed to turn a request
into motion, and it can refuse.

Licence: Apache-2.0 (`LICENSE`). Copyright 2026 Pokamind AB.

## The three rules the policy exists to enforce

1. **One request per event.** A cycle produces one request or none — never a settle plus
   an expression plus a gaze. The downstream intake holds a bounded queue and drops what
   does not fit; a lane that fires three requests per cycle spends its own budget and
   loses the one that mattered.
2. **Yield to speech.** While the robot is speaking, the sentence's own motion owns the
   head. A cycle that lands inside that window publishes **nothing**, not even an
   expression.
3. **Expression is state, not an event.** `set_expression` is requested only when the
   label actually changes, and only in place of a low-value motion. Re-announcing a face
   that is already on costs a queue slot and buys nothing.

Safety outranks all three: when `escalation` crosses the de-escalation threshold, the
cycle returns a single `calm_settle` at a reduced intensity and nothing else, and the
speaking window is cleared so the settle is not held back behind a sentence.

## The ladder

| REP-155 level (`hri_msgs/EngagementLevel`) | Motion |
|---|---|
| `DISENGAGING` (4), or `note == "attention-dropping"` | `nod` — the recovery bid, rate-limited by `recovery_cooldown_cycles` |
| `DISENGAGED` (1) | `head_tilt` |
| `ENGAGING` (2) | `antenna_wiggle` |
| `ENGAGED` (3) | `nod` every `nod_when_engaged_every`-th cycle, otherwise `look_around` |
| `UNKNOWN` (0) | nothing |

A sentence maps its cue family to one motion: `tension → calm_settle`,
`levity → antenna_wiggle`, `energy → talking`, `intimacy → listening`,
`softening → nod`, `release → calm_settle`, anything else → `talking`. The head is
reserved for `min(est_duration, cue_max_s)` seconds, and the `ended` marker settles the
robot only if a cue actually opened.

## Topics and actions

`<base>` is `/humans/persons/<person_id>` (`person_id` defaults to `learner_01`).

**Subscribes**

| Topic | Type |
|---|---|
| `<base>/engagement_status` | `hri_msgs/EngagementLevel` |
| `<base>/context` | `emori_msgs/Context` |
| `<base>/speech_cue` | `emori_msgs/SpeechCue` |

**Publishes / calls**

| Name | Type | Note |
|---|---|---|
| `/skill/set_expression` | `emori_msgs/SetExpression` (topic) | only on a label change |
| `/skill/replay_motion` | `emori_msgs/ReplayMotion` (action client) | one goal per event |
| `/skill/look_at` | `emori_msgs/LookAt` (action client) | client kept for the graph; the policy does not emit gazes today |
| `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | the ledger, every 5 s, status name `hri_engagement_behaviors` |

The ledger carries every policy counter (`cycles`, `engagement`, `cue`,
`yielded_to_speech`, `cooldown`, `expression_changes`, `settles`), the node's own
(`sent`, `expressions`, `dropped`, `result_<error_code|ok|rejected|exception>`), plus
`motion_owner` and `config_source`. The same numbers are logged at INFO every 30 s, so a
run that is already over can still be read out of `docker logs`.

## Parameters

| Parameter | Env | Default | Effect |
|---|---|---|---|
| `person_id` | — | `learner_01` | the REP-155 namespace this node watches |
| `behavior_yaml` | `EMORI_BEHAVIOR_CONFIG` | `""` | path to the tuning file; empty = built-in defaults |
| `motion_owner` | `EMORI_ROS_MOTION_OWNER` | `hostapp` | logged and put on the ledger only — the facade enforces it |
| `backchannel_sound` | — | `""` | accepted for launch-file compatibility; the ROS lane has no sound skill and says so at start-up |

A `behavior_yaml` that is missing or is a directory is an **ERROR** in the log naming the
path, not a silent fall-back. Whatever is in force, one INFO line at start-up states the
source and all five numbers:

    behaviour policy source=yaml:/emori/configs/behavior.yaml deescalate_at=0.60
    recovery_cooldown_cycles=2 nod_when_engaged_every=4 cue_max_s=4.00 deescalated_k=0.40

## Tuning (`behavior:` section of the YAML)

| Key | Default | Meaning |
|---|---|---|
| `escalation_threshold` | `0.6` | at or above this, the cycle de-escalates and does nothing else |
| `recovery_cooldown_cycles` | `2` | minimum cycles between two recovery nods |
| `nod_when_engaged_every` | `4` | nod cadence while engaged |
| `cue_max_s` | `4.0` | ceiling on the head reservation for one sentence |
| `deescalated_k` | `0.4` | intensity multiplier for a de-escalation settle |

`EMORI_CUE_DEESCALATE_AT` and `EMORI_CUE_MAX_S` override the corresponding keys when set
and parseable. Unknown keys are ignored, deliberately: the same file is read by the host
application's own engine, and each loader takes the keys it understands.

## Layout

* `policy.py` — the whole decision, pure stdlib, injected clock, no `rclpy` and nothing
  EMORI-specific. This is the file to read, and the file to unit-test.
* `behavior_node.py` — plumbing: parameters, subscriptions, action clients, dispatch,
  counters, `/diagnostics`.
* `pyhri_engagement_demo.py` — an optional consumer demo. It imports `pyhri` lazily
  because apt has no `ros-humble-pyhri`; declaring that dependency in `package.xml` would
  make `rosdep` fail for everyone building the package, so it is deliberately absent.

## Building it on its own

    mkdir -p ws/src && cd ws
    cp -r <emori_msgs> <hri_engagement_behaviors> src/
    sudo apt-get install -y ros-humble-hri-msgs python3-yaml
    colcon build --packages-select emori_msgs hri_engagement_behaviors
    source install/setup.bash
    ros2 run hri_engagement_behaviors behavior_node

Dependencies: `rclpy`, `hri_msgs`, `emori_msgs`, `diagnostic_msgs`, `python3-yaml`.
Nothing from the EMORI application.
