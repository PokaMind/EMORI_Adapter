# ROS4HRI mapping (REP-155) — topics, messages, QoS

How EMORI publishes/consumes ROS4HRI. Authoritative field lists: `configs/mapping.yaml`,
`src/emori_adapter/constants.py`, and `ros2_ws/src/open/emori_msgs/{msg,action}/` (`Context.msg`,
`SpeechCue.msg`, `SetExpression.msg`, `LookAt.action`, `ReplayMotion.action`, plus the hub's
session, speech, gesture, motion, add-on and gateway interfaces, listed in that package's README).

## Every ROS channel at a glance

What a running graph contains, read from the launch files and the nodes. The sections
below explain each part. "v2" marks what only exists when the robot itself is a ROS 2
node ([ros2-v2-native-robot.md](ros2-v2-native-robot.md)).

**Nodes** (`ros2 node list`):

| Node | Package (executable) | When it runs |
|---|---|---|
| `/emori_adapter` | `emori_adapter_ros` (`publisher_node`) | always |
| `/hri_context_analyzer` | `hri_context_analyzer` | always |
| `/hri_engagement_behaviors` | `hri_engagement_behaviors` (`behavior_node`) | always |
| `/skill_facade` | `emori_skill_facade` | always |
| `/fiware_bridge` | `fiware_bridge` (`bridge_node`) | only when the launch argument `with_fiware` is true. Both robot compose profiles and the v2 simulator profile set `EMORI_WITH_FIWARE=false`, because the hostapp is the FIWARE writer; the v1 simulator profile leaves it on |
| `/reachy_mini` | `reachy_mini_ros2` (`robot_node`) | v2 only |
| `/reachy_camera` | `v4l2_camera` | only with the launch argument `with_camera_v4l2:=true` (the Route B unit sets it) |
| `/reachy_audio` | `audio_common` (`audio_capturer_node`) | only with `robot.launch.py with_audio:=true`; not run on hardware |

So a v1 robot shows four nodes and a v2 robot on Route A five. `pyhri_demo` is an
executable that no launch file starts.

**Topics.** Unless a row says otherwise, the QoS is the rclpy default: reliable, volatile,
depth 10.

| Topic | Type | Publisher → subscribers | Notes |
|---|---|---|---|
| `/humans/persons/tracked` | `hri_msgs/IdsList` | `emori_adapter` | reliable, transient_local, depth 1 |
| `/humans/persons/<id>/anonymous` | `std_msgs/Bool` | `emori_adapter` | reliable, transient_local, depth 1 |
| `/humans/persons/<id>/engagement_status` | `hri_msgs/EngagementLevel` | `emori_adapter` → `hri_engagement_behaviors` | |
| `/humans/persons/<id>/context` | `emori_msgs/Context` | `emori_adapter` → `hri_context_analyzer`, `hri_engagement_behaviors`, `fiware_bridge` | |
| `/humans/persons/<id>/speech_cue` | `emori_msgs/SpeechCue` | `emori_adapter` → `hri_engagement_behaviors` | live source only, while the robot speaks |
| `/humans/persons/<id>/context/*`, `/humans/persons/<id>/expression` | see [below](#topics-published-by-hri_context_analyzer) | `hri_context_analyzer` | 21 topics; some are published only when the channel was reported |
| `/skill/set_expression` | `emori_msgs/SetExpression` | `hri_engagement_behaviors` → `skill_facade` | |
| `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | `hri_engagement_behaviors`, `skill_facade`; v2: `reachy_mini` | one status per node, every 5 s |
| v2: `/joint_states` | `sensor_msgs/JointState` | `reachy_mini` | name and position only, 9 joints, ≈50 Hz |
| v2: `/reachy_mini/head_pose` | `geometry_msgs/PoseStamped` | `reachy_mini` | frame `base_link` |
| v2: `/tf` | `tf2_msgs/TFMessage` | `reachy_mini` | `base_link → head` only; no `/tf_static`, no robot description |
| v2: `/reachy_mini/motion_status` | `reachy_mini_msgs/MotionStatus` | `reachy_mini` | reliable, transient_local, depth 1; on change and every 1 s |
| v2: `/reachy_mini/camera/image_raw`, `/reachy_mini/camera/camera_info` | `sensor_msgs/Image` (rgb8), `sensor_msgs/CameraInfo` | `reachy_mini`, or `/reachy_camera` with `with_camera_v4l2:=true` | listed but silent when no camera feeds the node |
| v2: `/reachy_mini/set_target` | `reachy_mini_msgs/BodyTarget` | `skill_facade` (`ros_native` transport) → `reachy_mini` | streaming target, attributed to client `ros` |
| v2: `/humans/faces/tracked` | `hri_msgs/IdsList` | `reachy_mini` (face look-at) | reliable, transient_local, depth 1; once a second and on change; `[]` while no face is locked. Only when the look-at component was built |
| v2: `/humans/faces/<id>/roi` | `hri_msgs/NormalizedRegionOfInterest2D` | `reachy_mini` (face look-at) | frame `eye_camera`; one at a time, from the first lock |

Every node also has the standard `/rosout`, `/parameter_events` and parameter services,
and every action below has its hidden `_action/` topics and services.

**Actions:**

| Action | Type | Server | Client |
|---|---|---|---|
| `/skill/replay_motion` | `emori_msgs/action/ReplayMotion` | `skill_facade` | `hri_engagement_behaviors` |
| `/skill/look_at` | `emori_msgs/action/LookAt` | `skill_facade` | `hri_engagement_behaviors` (the policy sends no look-at goals today) |
| v2: `/reachy_mini/goto` | `reachy_mini_msgs/action/GoTo` | `reachy_mini` | `skill_facade` (`ros_native`) |
| v2: `/reachy_mini/play_recorded_move` | `reachy_mini_msgs/action/PlayRecordedMove` | `reachy_mini` | `skill_facade` (`ros_native`) |

**Services** (v2, all served by `reachy_mini`): `/reachy_mini/wake_up` and
`/reachy_mini/goto_sleep` (`std_srvs/Trigger`; `skill_facade` with `ros_native` calls
`wake_up`), `/reachy_mini/set_motor_mode` (`reachy_mini_msgs/srv/SetMotorMode`),
`/reachy_mini/stop` (`StopMove`), `/reachy_mini/set_head_reserved` (`std_srvs/SetBool`)
and `/reachy_mini/set_owner` (`SetOwner`).

The graph is loopback-only on a robot: `ROS_LOCALHOST_ONLY=1`, `ROS_DOMAIN_ID=42`, Fast
DDS. See [Inspect at runtime](#inspect-at-runtime) for how to look at it.

## Person identity

One learner at a time. The **ROS person id is the stable `person_id` parameter**
(`learner_01` by default): the robot serves a single learner, so the standard
`/humans/persons/<id>/` tree and the EMORI `/context` extension never churn between kiosk
sessions. The TRUE per-session identity (roster student id + minted session id, stamped by
the hostapp at `/api/cycle`) rides **inside** `emori_msgs/Context.person_id`/`session_id` —
identity-keyed consumers (the FIWARE bridge) read those fields, not the topic name. We
publish a minimal but REP-155-valid person:
`tracked` + `anonymous` (latched), plus `engagement_status`. (We do not run
`hri_person_manager` fusion: there is exactly one known learner; fusion from face/voice
`candidate_matches` is the upgrade path.)

## Topics published by `emori_adapter_ros` (the adapter)

| Topic | Type | QoS | Notes |
|---|---|---|---|
| `/humans/persons/tracked` | `hri_msgs/IdsList` | reliable, transient_local (latched), depth 1 | current persons |
| `/humans/persons/<id>/anonymous` | `std_msgs/Bool` | reliable, transient_local, depth 1 | REP-155 person well-formedness |
| `/humans/persons/<id>/engagement_status` | `hri_msgs/EngagementLevel` | volatile, depth 10 | the standard engagement topic |
| `/humans/persons/<id>/context` | `emori_msgs/Context` | volatile, depth 10 | full structured signal (EMORI extension) |
| `/humans/persons/<id>/speech_cue` | `emori_msgs/SpeechCue` | volatile, depth 10 | one sentence of the ROBOT's own speech, at that sentence's playback onset — **live source only** |

`EngagementLevel.level` uses the REP-155 constants exactly: `UNKNOWN=0, DISENGAGED=1,
ENGAGING=2, ENGAGED=3, DISENGAGING=4` — identical to
`emori_adapter.models.EngagementLevel`.

> **Latched topics use `transient_local` durability** so late subscribers (including
> `pyhri`'s `HRIListener`) see the last value. Mismatched QoS is the usual reason a
> subscriber "sees nothing".

The expression label also rides as the `expression` string field on `emori_msgs/Context`,
next to `valence`/`arousal`; `hri_context_analyzer` republishes it as a standard
`hri_msgs/Expression` on `/humans/persons/<id>/expression` (below). The bounded *request*
to wear an expression is a third thing entirely — `emori_msgs/SetExpression` on
`/skill/set_expression` — because "what we observed" and "what we are asking the robot to
do" must never share a topic.

## `emori_msgs/SpeechCue` — what the robot's own voice is doing

Published per SENTENCE (not per cycle) on the live source, at that sentence's playback
onset, from the same relay socket the cycles arrive on (`PokamindBridgeSource.events()`
interleaves both). Deliberately **not** `hri_msgs/LiveSpeech`: LiveSpeech carries what the
HUMAN said; this is assistant speech, which is what a behaviour node needs in order to move
the head *while that sentence is spoken*.

`header, person_id, session_id, seq, text, cue_category, cue_body, lang, source,
est_duration, source_time, ended, cue_category_authored`

- `text` is the sentence that was synthesised, without cue markup. The bracket vocabulary
  (`[family| direction]`) never reaches this field: the publisher strips it on the way
  out, so a subscriber can render this string without ever rendering a tag.
- `cue_category` is one of the six wire families — `tension`, `levity`, `energy`,
  `intimacy`, `softening`, `release` — or `""`. It is the family the robot performs, which
  can differ from the one Pokamind forwarded: that one is `cue_category_authored`, `""`
  when Pokamind forwarded none. `cue_body` (the freeform English direction)
  is currently always `""`; a consumer must treat it as optional.
- `est_duration` is an estimate, `0.333 × word_count` clamped to `[0.9, 6.0]` s, so the
  robot's motion envelope roughly matches the voice the learner hears.
- `source_time` is the envelope's own timestamp (correlation), distinct from `header.stamp`
  (when this node republished it).
- `ended = true` is the end-of-turn marker: empty text, and the cue lane settles.

A mock arc has no voice and therefore publishes no cues at all — a subscriber that sees
none is looking at a session with no speech, never at a fabricated one.

## `emori_msgs/MotionPlanEvent` — what happened to each sentence's motion

Published on `/emori/motion/plan_events` by the hub's `/emori/motion` node, one message per
step of a sentence's plan, reliable, volatile, depth 10. Where `SpeechCue` says what the robot
is about to perform, this says what it did. The node exists when the hub hosts its facades
(`EMORI_HUB_FACADES`).

`header, plan_id, seq, family, slug, started, ended, outcome, first_apply_delay_s,
family_authored, family_source, skip_reason`

- A sentence with a plan gives three messages: planned (neither flag), `started`, `ended`. A
  sentence skipped before it reaches the player gives one, with `ended` and outcome `skipped`.
  A deduplicated double delivery is not a sentence and gives none. Batches that are not a
  sentence's, such as the greeting, a cycle's motion or the end-of-turn settle, give none.
- `plan_id` is one per sentence, and is the id the motion trace's records carry when
  `EMORI_MOTION_TRACE` is on. `seq` is the number on the sentence's envelope.
- `family` is the family performed, the same word this lane puts in `SpeechCue.cue_category`,
  and `""` on the talking flow or when nothing was planned. `family_authored` is what Pokamind
  forwarded. `family_source` is how the family was chosen: `pokamind` (it forwarded one), `text`
  (read from the sentence's words), `delivery` (read from its punctuation), `carried` (from an
  earlier sentence of the turn), `dialogue` (a conversational recording), or `""`. `slug` is
  the planner's name for the plan, or why there is none.
- `first_apply_delay_s` runs from the sentence's event to the plan's first command, so it
  includes the wait for the voice. It is `-1.0` until `started`.
- `outcome`, with `ended`:

| `outcome` | Meaning |
|---|---|
| `finished` | Everything up to the plan's last moving intent went out. |
| `preempted` | Something newer took the head first: the next sentence, the end of the turn, an escalation, the end of the session. |
| `voice_end` | The voice stopped with intents still to go, so the head settled and the rest was dropped. |
| `skipped` | The plan never performed. `skip_reason` says why: `no_voice` (the voice never came, so only the settle was sent), `head_reserved` (the head is an input device), `not_owner` (the motion lane is closed, or the player refused the batch), `empty_text` (a sentence with no words and no family), `cue_only` (a cue with no words on a robot with no voice signal), `cue_motion_off` (`EMORI_CUE_MOTION=0`). |

The driver and the player report each step with one non-blocking put on a bounded queue, and
the node drains the queue from its 5 Hz state tick, so a message can be up to 200 ms behind its
step and `header.stamp` is when it was published. A step the queue cannot hold is lost and
counted: `plan_events_dropped` under `emori_hub/motion` on `/diagnostics`, with
`plan_events_failed` and `plan_events_unmatched` beside it. The robot moves the same either way.

The management dashboard shows the same steps counted by family and by outcome, for a window
and for one session. Those counts are kept in memory and written once, when the session ends.

## The skill lane — decide in ROS, move through the hostapp

The ROS graph is an **authority, not a driver**. `hri_engagement_behaviors` consumes
`context` + `speech_cue` + `engagement_status` and emits *bounded skill requests*; it holds
no daemon URL and authors no motor values. `skill_facade` is the only ROS component that
turns a request into motion, and it does so by POSTing the hostapp's `/api/skill` — never
the Reachy daemon — so the hostapp's single `RobotPlayer` queue keeps its generation guards,
its `drop_if_busy` interlock, the `busy` gate the push-turn detector needs, and the
add-on ownership check.

**One bounded request per event.** The decision half is a pure module,
`ros2_ws/src/open/hri_engagement_behaviors/hri_engagement_behaviors/policy.py` (stdlib only, injected clock, unit-tested on the host
with no ROS). Its rules, in order: a de-escalation cycle returns exactly one
`calm_settle` and nothing else; a cycle that falls inside a sentence's speaking budget
returns **nothing at all** — not even an expression; otherwise the ladder yields at most
one motion, and an expression is only requested when the label actually *changes* and it
is not displacing a recovery nod. A sentence is one request that owns the head for
`min(est_duration, cue_max_s)`. Expression is state, not a per-cycle event, so the lane
cannot flood the 12-slot queue.

**The facade is its own package, `emori_skill_facade`**, split into a pure `contract.py`
(the counters, the request bodies, the error-code mapping — no ROS import at all), a
`transport.py`, and the node. `transport.py` is the **transport plug point**: another
skill interface is one more class there and one launch argument, not a rewrite.
`EMORI_SKILL_TRANSPORT` picks between `hostapp_http` (the v1 default, the `/api/skill`
POST with `X-Emori-Skill-Key`), `log` (accepts everything and records it — for benches and
the standalone smoke, where there is no hostapp to POST at) and, on a v2 robot,
`ros_native` (registered by `emori_skill_native`, the v2 default: goals straight to the
robot node). An unknown name is refused loudly and degrades to `log`: a typo must never
be the reason a robot moves.

**Both nodes publish a ledger on `/diagnostics`** (`diagnostic_msgs/DiagnosticArray`,
every 5 s, and the same line at INFO every 30 s) so a run can be audited without reading
logs. On v2 the robot node adds a third status, `reachy_mini`
([ros2-v2-native-robot.md](ros2-v2-native-robot.md)):

| Status name | `hardware_id` | Values |
|---|---|---|
| `hri_engagement_behaviors` | the `person_id` | `cycles`, `engagement`, `cue`, `yielded_to_speech`, `cooldown`, `expression_changes`, `settles`, `sent`, `expressions`, `dropped`, `result_<code\|ok\|rejected\|exception>`, `motion_owner`, `config_source` |
| `emori_skill_facade` | the transport name | `posted`, `accepted`, `dropped`, `refused`, `suppressed`, `unreachable`, `motion_owner`, `transport`, `url_host`; with `ros_native` also the transport's own `goals_sent`, `goals_completed`, `goals_refused_<code>`, `batches_cancelled`, `targets_published`, `skipped_<kind>` |

`posted` is counted by the node right after the owner gate and before the transport runs,
so `posted + suppressed` is always the number of requests the graph made, whichever
transport is plugged in. The facade's status level is ERROR when the owner is `ros` and
the key is empty under `hostapp_http`, WARN when `unreachable` grew since the last tick,
and OK otherwise — including a facade suppressing everything, which is the shipped
default and not a fault.

The tuning file the policy reads is `EMORI_BEHAVIOR_CONFIG`, bind-mounted read-only
into the container (`EMORI_ROS_BEHAVIOR_CONFIG_HOST` picks the host-side file; the image
carries a copy as the default). The node logs one INFO line with the effective config and
its source at start, and an ERROR naming the path if it was set but unreadable — a tuning
file nobody loaded is otherwise a fault nobody notices.

| Topic / action | Type | Direction |
|---|---|---|
| `/skill/set_expression` | `emori_msgs/SetExpression` (topic) | behaviours → facade |
| `/skill/replay_motion` | `emori_msgs/action/ReplayMotion` | behaviours → facade |
| `/skill/look_at` | `emori_msgs/action/LookAt` | behaviours → facade |

The policy requests `replay_motion` and `set_expression` only. `/skill/look_at` is served
by the facade (and by `/api/skill`), but the engagement policy does not send gaze requests
today. All three use the default QoS: reliable, volatile, depth 10.

`SetExpression` is a topic and not an action on purpose: an expression is fire-and-forget
state, not a goal with a result worth waiting on. It carries `label` (the `hri_msgs`
vocabulary), `confidence` (**measured**: how sure the perception side is), `intensity` (the
planner's amplitude `k`) and `reason` (`cue:tension`, `engagement:recovery`, …). The two are
separate fields because publishing a planning number as a measured confidence would make
every downstream reading of it a fiction.

`ReplayMotion` takes a name from the closed vocabulary
(`greeting|farewell|nod|listening|thinking|calm_settle|talking|head_tilt|antenna_wiggle|look_around`),
`LookAt` a bounded gaze target; both answer `error_code` — `""` on success, otherwise the
`/api/skill` code (or `dropped` when the queue was busy with motion that outranks a skill
request). `ReplayMotion` reports `completed`, `LookAt` reports `reached`.

### `POST /api/skill` (hostapp) — the single-writer intake

Off by default: it mounts only when `EMORI_SKILL_API=1` **and** `EMORI_SKILL_KEY` is set
(a secret-less "enabled" state cannot exist), refuses any client that is not loopback, and
is rate-limited per skill KIND.

```
Header: X-Emori-Skill-Key: <EMORI_SKILL_KEY>
{"skill":"set_expression","label":"neutral|happy|sad|angry|surprised|scared|disgusted","confidence":0..1}
{"skill":"look_at","yaw_deg":-30..30,"pitch_deg":-25..25,"duration":0.2..1.5}
{"skill":"replay_motion","name":<repertoire name>,"intensity":0..1}

200 {"ok":true,"submitted":<n intents>,"dropped":false}
400 {"error_code":"bad_skill"|"out_of_range"}     # params are range-CHECKED, never clamped
403 {"error_code":"skill_api_disabled"|"bad_key"|"not_loopback"}
409 {"ok":false,"error_code":"not_owner"}         # an add-on mode owns the robot / not the ROS lane
423 {"ok":false,"error_code":"head_reserved"}     # push-turn selection is reading the head
429 {"error_code":"rate_limited"}                 # refused, never queued
```

`rate_limited`, `head_reserved` and `dropped` are transient (try the next cue); the rest
mean "you are not the writer" and repeating changes nothing.

Two rules the intake applies that a caller cannot see from the contract alone:

* **A de-escalation settle is never dropped behind expression.** `replay_motion` with
  `name == "calm_settle"` is submitted with `drop_if_busy=False`; everything else keeps
  `drop_if_busy=True`. Safety outranks expression — the same rule the hostapp's own lane
  applies when it preempts a live cue.
* **Every answer is counted.** `driver.counters` gains `skill_accepted`, `skill_dropped`,
  `skill_intents_submitted` and a `skill_refused_<error_code>` for every refusal path, so
  `GET /api/status` is the hostapp-side half of the facade's ledger and the two can be
  reconciled request by request.

`EMORI_ROS_OWNER_FALLBACK_S` (float, **default `0` = off**) is the unattended-venue
escape hatch: with the owner set to `ros`, a live session, at least one cycle in, and no
accepted skill request for longer than that window, the hostapp takes its expressive
lanes back, logs a WARNING and counts `owner_fallback_episodes`; the next accepted request
hands them straight back. `none` never falls back. Leave it at `0` for any measurement —
a rehearsal that silently becomes a hostapp run halfway through would still pass.

### `EMORI_ROS_MOTION_OWNER` — `hostapp` (default) | `ros` | `none`

Enforced on **both** sides of the boundary: the facade will not POST, and the intake answers
409 `not_owner`. The engagement node always computes and always publishes, so switching the
owner changes who executes, never who decides. On `ros` the hostapp stands its own
expressive lanes down (`LiveDriver.expressive_motion_enabled`, counter
`engagement_suppressed_by_owner`) so exactly one lane submits. An unrecognised value reads as
`hostapp` on both sides — a typo must never open a second writer. Deployment, the readycheck
`ros2` gate and the flip procedure are in `docs/onrobot-runbook.md` Step 5b.

## Sources: mock and live (`source:=mock|pokamind`)

The adapter node takes a `source` parameter:

- `mock` — the deterministic engagement arc (the standalone demo); `mock_shape:=live`
  mimics the thin live envelope (styles + body only) instead of the rich one.
- `pokamind` — **LIVE data**: a reader thread connects to the hostapp relay (`ws_url`,
  default `ws://host.docker.internal:8080/ws/relay`), where the hostapp's `/api/cycle`
  hub broadcasts every identity-stamped envelope of the real kiosk session. The reader
  reconnects across sessions, and a `session_id` change resets the stateful Translator
  (engagement bands, style baselines). The node is an **observer** of the relay — the
  hostapp remains the single robot driver on the live stack.

## Topics published by `hri_context_analyzer`

Fans the structured `Context` out to single-purpose, REP-155-extension scalar topics that
any downstream module can consume without the custom message:

| Topic | Type | Published |
|---|---|---|
| `/humans/persons/<id>/context/escalation` | `std_msgs/Float32` (0..1) | every cycle |
| `/humans/persons/<id>/context/rapport` | `std_msgs/Float32` (0..1) | every cycle |
| `/humans/persons/<id>/context/attention` | `std_msgs/Float32` (0..1) | every cycle |
| `/humans/persons/<id>/context/engagement_trend` | `std_msgs/Float32` (-1..1) | every cycle |
| `/humans/persons/<id>/context/dominant_style` | `std_msgs/String` | every cycle |
| `/humans/persons/<id>/context/engagement_score` | `std_msgs/Float32` (0..1) | every cycle — the continuous signal, without the custom message |
| `/humans/persons/<id>/context/valence` | `std_msgs/Float32` (-1..1) | every cycle |
| `/humans/persons/<id>/context/arousal` | `std_msgs/Float32` (-1..1) | every cycle |
| `/humans/persons/<id>/context/degraded` | `std_msgs/Bool` | every cycle (`report_flags == 0`) |
| `/humans/persons/<id>/context/age_s` | `std_msgs/Float32` | LIVE source only, when the envelope carried a clock (the mock arc stamps a fixed epoch, so it withholds freshness rather than reporting ~a decade of it) |
| `/humans/persons/<id>/context/body/{posture,head_position,gesture_type,energy}` | `std_msgs/String` | when that categorical was reported |
| `/humans/persons/<id>/context/body/{engagement,confidence}` | `std_msgs/Float32` (0..1) | only with the BODY flag |
| `/humans/persons/<id>/context/face/positivity` | `std_msgs/Float32` (0..1) | only with the FACE flag (**never on the live wire**) |
| `/humans/persons/<id>/context/voice/warmth` | `std_msgs/Float32` (0..1) | only with the VERBAL flag |
| `/humans/persons/<id>/context/voice/pitch_variety` | `std_msgs/Float32` (0..1) | only with the TONE flag |
| `/humans/persons/<id>/context/top_styles` | `std_msgs/String` | `name:0.31,name:0.18,…` top-5 |
| `/humans/persons/<id>/expression` | `hri_msgs/Expression` | the standard REP-155 topic, from `Context.expression`; **withheld on a degraded cycle** (an empty cycle derives the origin of the valence/arousal plane, i.e. "neutral" at confidence 1.0, and this topic has nowhere to say "degraded") |

**A flag-gated topic is not published at all when its flag is clear.** A subscriber sees a
stale-or-absent topic, never a fabricated `0.0` — "we did not measure this" and "we measured
zero" are different sessions, and conflating them poisons every metric computed downstream.
`degraded` is the deliberate exception: it is published every cycle, because silence on a
health signal is indistinguishable from a dead node.

REP-155 doesn't define `/context`, but it is explicitly extensible (the spec and
`hri_person_manager` itself add per-person sub-topics like `proxemic_space`). We namespace
clearly under `.../context/` and avoid colliding with standard names (`face_id`, `body_id`,
`voice_id`, `anonymous`, `alias`, `engagement_status`, `location_confidence`).

## Consumers

- **`hri_engagement_behaviors`** subscribes `engagement_status` (standard), `context` (rich)
  and `speech_cue` (per-sentence) → `BehaviorEngine` + the cue projection → **bounded skill
  requests** on `/skill/*`. It holds no daemon URL: `skill_facade` — now its own package,
  `emori_skill_facade` — is the only component that executes one, through the hostapp's
  `/api/skill` (see "The skill lane"). While a sentence is being spoken the per-cycle lane
  yields the head to the cue lane and publishes **nothing** on that cycle — except a
  de-escalation cycle, which preempts, because safety outranks expression. A separate
  `pyhri_demo` node shows consumption via the official `hri.HRIListener`
  (`person.engagement_status`).
- **`fiware_bridge`** subscribes the full `Context` topic and historizes every cycle to
  Orion-LD using the canonical schema (`src/emori_adapter/fiware_schema.py`). Entity
  identity comes from the **message** (`Context.session_id`/`person_id`, stamped by the
  hostapp on the live path), not from a launch parameter — a new session id rolls over to
  fresh per-session `urn:ngsi-ld:Session:<student>-<utctimestamp>` entities. On the live
  stack set `FIWARE_BRIDGE_ENABLED=0`: the hostapp already historizes every cycle, and two
  writers would double every time-series row.

## `emori_msgs/Context` fields

**v1 — the interpretation** (unchanged, and the order is a golden list: every existing
subscriber is compiled against it, so the block is append-only):
`person_id, session_id, seq, engagement_score (0..1), engagement_level (uint8),
valence (-1..1), arousal (-1..1), expression (hri vocab), expression_confidence,
engagement_trend, escalation, rapport, attention, dominant_style, topic, note` (+ `Header`).

**v2 — the raw observation** (appended after v1): what the session analysis reported about
the person, alongside the interpretation rather than instead of it —
`posture, head_position, gesture_type, overall_energy` (categorical, `""` = absent),
`body_engagement, body_confidence, face_positivity, voice_warmth, pitch_variety` (0..1),
`style_names[] / style_values[]` (top-5, descending), `report_flags` (bit0 BODY, bit1 FACE,
bit2 TONE, bit3 VERBAL, bit4 STYLES), `lang`, `age_s`.

**The constants are in the interface file.** Appended after `age_s`, so anyone can read
the wire contract with `ros2 interface show emori_msgs/msg/Context` and no EMORI code
installed at all:

```
float32 NOT_REPORTED=-1.0
uint8 REPORT_BODY=1
uint8 REPORT_FACE=2
uint8 REPORT_TONE=4
uint8 REPORT_VERBAL=8
uint8 REPORT_STYLES=16
```

`hri_context_analyzer` reads them off the generated class (`Context.NOT_REPORTED`,
`Context.REPORT_BODY`, …) rather than importing them from `emori_adapter`, and the node
file contains no `-1.0` literal anywhere. A contract test pins the three spellings — the
message, the analyzer's pure `contracts.py`, and `emori_adapter.translation` — against
each other, so the wire and the core cannot drift apart silently. The constants are
**not** fields: the v1 field order stays the golden, append-only list above.

Every v2 float uses **`-1.0` = NOT REPORTED**. On the live wire only styles and
`body_language` arrive, so `face_positivity` / `voice_warmth` / `pitch_variety` are always
`-1.0` there and are only populated by the rich mock/replay shape; a lost face nulls the
body channels too. The mapping is one pure function, `translation/person_report.py`, shared
by both consumers, so the ROS payload and the hostapp's own reading cannot drift apart.
`report_flags == 0` is the spec's "degraded status" and `age_s` is its "freshness" — no
third message type.

## Privacy: what is and is not on the DDS wire

DDS has no access control beyond `ROS_LOCALHOST_ONLY=1`, so the identity gate is applied
**before** anything is published, in
`ros2_ws/src/emori/emori_adapter_ros/emori_adapter_ros/privacy.py` (pure, stdlib,
unit-tested). Three knobs, all documented in `.env.example` and forwarded by both compose
files:

| Knob | Default | Effect |
|---|---|---|
| `EMORI_ROS_DEIDENTIFY` | `1` (on) | `Context`/`SpeechCue` `person_id` and `session_id` become `p-<sha256(salt:value)[:16]>` |
| `EMORI_ROS_SPEECH_TEXT` | `0` (off) | when off, `SpeechCue.text` and `cue_body` go out empty |
| `EMORI_ROS_ID_SALT` | unset | unset means a fresh 16-byte salt per process; the node logs `salt=ephemeral` |

Only `1/true/yes/on` and `0/false/no/off` move a knob — a misspelling falls back to the
protective default, because a privacy control a typo disables is not a control.

What the gate deliberately does **not** touch:

* **The topic namespace.** `/humans/persons/learner_01/...` is a launch parameter, not an
  identity. Pseudonymising it would churn the graph every session for no privacy gain;
  the identity rides inside the message and that is the half the gate acts on.
* **`cue_category`, `est_duration`, `lang`, `ended`, `seq`.** The engagement policy budgets
  the head from exactly those fields. Sweeping them up would look like a privacy win and
  land as a robot that stares straight ahead while it speaks. `cue_body` *does* travel
  with `text`: it is free-form direction written about the same sentence.

With an ephemeral salt, pseudonyms are stable inside one run (a consumer can group a
session) and meaningless across a restart — the node says so at start, because "these two
captures cannot be joined" is a limitation an analyst has to know before planning around
it. And note the consequence for `fiware_bridge`: keyed on the in-message ids, it would
historize pseudonyms under the default posture. It is off on the robot, where the hostapp
historizes with the real ids.

### Proving it from outside

`tests/ros/outside_consumer.py` is a plain `rclpy` node with **zero** EMORI imports; it
ships in `deploy/ros2/Dockerfile.consumer` (the ROS base image for the graph's distro plus
`ros-<distro>-hri-msgs` and `ros-<distro>-tf2-msgs`, nothing else) and
`scripts/outside_consumer_check.py` runs it against a live adapter container: v1 on
Humble, and with `--v2` the v2 graph on Jazzy, where it also reads `/joint_states` and
`/tf`. Use the same distro as the publisher: a Humble consumer and a Jazzy publisher
discover each other and never match. It reads the vocabulary off the installed `Expression` class rather than a copy
of our own list, so it compares us against the interface. `emori_msgs/msg/Context` appears
in its topic list and is **not decodable** there — that is the honest boundary of "standard
ROS4HRI", and it is recorded in the report as `context_decodable: false` rather than hidden.

> **Containerised consumers need a UDP-only transport.** Two containers that share only a
> *network* namespace look like one host to Fast DDS, so it prefers shared memory — but
> `/dev/shm` is per-container. Discovery succeeds, delivery silently never happens: a
> perfect `ros2 topic list` and zero samples. Run such a consumer with
> `FASTDDS_BUILTIN_TRANSPORTS=UDPv4` (what the checker does), or give it
> `--ipc container:emori-ros2` — but that needs the adapter container started
> `--ipc=shareable`, i.e. changing the graph you are trying to measure. This applies on
> the robot too, not just in test.

## Distro / install

Targets **ROS 2 Humble** (the reference distro for the ros4hri stack and tutorials). The
container installs `ros-humble-hri-msgs` (required) and tries `ros-humble-pyhri` /
`ros-humble-libhri` (optional). The `hri` Python module lives in the **libhri** repo
(`humble-devel`) — the standalone `ros4hri/pyhri` repo is ROS 1 only; don't pip-install that.
`hri_engagement_behaviors` deliberately does **not** declare a `pyhri` exec_depend: apt
has no `ros-humble-pyhri`, so declaring it would break `rosdep` for everyone, and the
critical path uses plain `rclpy`.

**Ten packages**, all Apache-2.0; the six under `ros2_ws/src/open/` are standalone:

| Package | Licence | Depends on `emori_adapter`? |
|---|---|---|
| `emori_msgs` | Apache-2.0 | no (interfaces only) |
| `hri_context_analyzer` | Apache-2.0 | **no** |
| `hri_engagement_behaviors` | Apache-2.0 | **no** |
| `emori_skill_facade` | Apache-2.0 | **no** |
| `reachy_mini_msgs` (Jazzy) | Apache-2.0 | no (interfaces only) |
| `reachy_mini_ros2` (Jazzy) | Apache-2.0 | **no** |
| `emori_adapter_ros` | Apache-2.0 | yes — it is the bridge, and it is allowed to |
| `fiware_bridge` | Apache-2.0 | yes |
| `emori_skill_native` (Jazzy) | Apache-2.0 | yes — it reuses the core's skill mapping |
| `emori_adapter_bringup` | Apache-2.0 | no — launch files and parameters only |

A contract test walks every `.py` in the standalone packages with `ast` and fails on any
`emori_adapter` import, and
`deploy/ros2/Dockerfile.standalone` is the executable version of the same claim: it
installs `ros-humble-hri-msgs`, copies only those four packages, runs `colcon build` with
**no pip install of the core**, and starts `context_analyzer`, `behavior_node` and
`skill_facade` (with `EMORI_SKILL_TRANSPORT=log`). `scripts/ros_standalone_check.py`
builds and runs it; CI runs it as the `ros2-standalone` job, with a third step that
asserts the image *cannot* `import emori_adapter`. Release mechanics —
`bloom-release` and the `ros/rosdistro` PR — are in [ros4hri-release.md](ros4hri-release.md);
the licences are in [../LICENSING.md](../LICENSING.md).

`hri_msgs/Expression`'s field type differs across hri_msgs releases (a string in some, a
`uint8` with class constants in others), so `hri_context_analyzer` **introspects** the
installed message rather than assuming one; the label→constant-name table lives in the
adapter core (`translation/person_report.py`) so it is unit-testable with no ROS present.

On a v1 robot the whole graph runs in one container and there is no native ROS 2 at all —
see `docs/onrobot-runbook.md` Step 5b for the arm64 build, the opt-in install and the
`ros2` readycheck gate. A v2 robot runs the Jazzy graph, with `reachy_mini_ros2` owning the
motors (see [ros2-v2-native-robot.md](ros2-v2-native-robot.md)).

## Inspect at runtime

On a robot the graph is loopback-only (`ROS_LOCALHOST_ONLY=1`, `ROS_DOMAIN_ID=42`, Fast
DDS): `ros2 topic list` from a laptop, or from a robot shell that has not set the same
values, shows nothing. Inside a container, run the commands through the image's
`/entrypoint.sh`, which sources ROS and the workspace overlay (`docker exec` does not run
the image's entrypoint by itself).

**v1 robot** (the Humble container `emori-ros2`):

```bash
R='docker exec emori-ros2 /entrypoint.sh'
$R ros2 node list            # /emori_adapter /hri_context_analyzer /hri_engagement_behaviors /skill_facade
$R ros2 topic list | grep -E '/humans|/skill|/diagnostics'
$R ros2 action list          # /skill/look_at /skill/replay_motion
$R ros2 topic echo --once /humans/persons/learner_01/context
$R ros2 topic echo /humans/persons/learner_01/speech_cue   # only while the robot speaks
$R timeout 12 ros2 topic echo /diagnostics                  # the ledgers, every 5 s
$R ros2 interface show emori_msgs/msg/Context               # fields AND the constants
```

**v2 robot, Route A** (the Jazzy container `emori-ros2-v2`): the same commands with
`R='docker exec emori-ros2-v2 /entrypoint.sh'`. The node list gains `/reachy_mini`,
`ros2 action list` gains `/reachy_mini/goto` and `/reachy_mini/play_recorded_move`, and
`ros2 service list | grep reachy_mini` shows `wake_up`, `goto_sleep`, `set_motor_mode`,
`stop`, `set_head_reserved` and `set_owner`. The robot-side checklist is in
[ros2-v2-native-robot.md](ros2-v2-native-robot.md#looking-at-a-running-v2-robot).

**v2 robot, Route B** (no container): set up the shell the way the unit does, then run
the commands without the prefix:

```bash
source /opt/emori/ros2/.pixi/envs/default/setup.bash
source /opt/emori/ros2_ws/install/setup.bash
export ROS_LOCALHOST_ONLY=1 ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
ros2 node list
```

A name in `ros2 topic list` is not evidence of data: publishers exist from node start,
so all 21 analyzer topics are listed at once. `speech_cue` (live source, while the robot
speaks), `expression` (not on a degraded cycle) and the flag-gated analyzer topics stay
silent until there is something to say; use `ros2 topic echo --once` or
`ros2 topic hz` to see data.

An outside subscriber must run the same ROS distro as the publisher (Humble for v1, Jazzy
for v2): a Humble subscriber and a Jazzy publisher discover each other and never match.
