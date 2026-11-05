# emori_skill_native

`ros_native`: the transport that lets `emori_skill_facade` move a robot that is itself a
ROS 2 node, with no HTTP hop, no hostapp in the path and no Reachy daemon anywhere.

In v1 a skill request left the graph by POSTing the host application's single-writer
`/api/skill` intake. In v2 the robot is `reachy_mini_ros2` — it owns Pollen's backend
in-process and its arbiter holds the interlocks the hostapp's `RobotPlayer` queue holds in
v1 (lanes, preemption, drop-if-busy, head reservation, owner lock). This package turns
one bounded skill request into goals on that node:

| intent kind | becomes |
|---|---|
| `goto` / `look_at` / `antennas` | a `reachy_mini_msgs/action/GoTo` goal on `/reachy_mini/goto` |
| `set_target` | a `reachy_mini_msgs/msg/BodyTarget` on `/reachy_mini/set_target` |
| `play_recorded` | a `PlayRecordedMove` goal (`pollen-robotics/reachy-mini-emotions-library`) |
| `wake_up` | `std_srvs/Trigger` on `/reachy_mini/wake_up` |
| `play_sound` / `wobble` | skipped and counted (the v2 node carries no media) |

Licence: Apache-2.0, Copyright 2026 Pokamind AB. See
[`LICENSING.md`](../../../../LICENSING.md).

## Relationship to the other packages

It imports the EMORI adapter core (`emori_adapter.behavior.skill_to_intents`) on purpose,
so it lives under `ros2_ws/src/emori/`: that is the *same* mapping the host application's
intake uses, so `replay_motion nod` performs the same gesture whichever side asked for it,
and an out-of-range parameter is refused with the same `out_of_range` code. The packages
under `ros2_ws/src/open/` (`hri_context_analyzer`, `hri_engagement_behaviors`,
`emori_skill_facade`, `emori_msgs` and the others) stay free of any import of the core
*and* of this package — `tests/test_ros_standalone.py` asserts both.

## How the facade finds it

`setup.py` publishes an entry point:

```
emori_skill_facade.transports = ros_native = emori_skill_native.native:make_transport
```

so `EMORI_SKILL_TRANSPORT=ros_native` (or `skill_transport:=ros_native` in
`v2.launch.py`) resolves with nobody importing this package first. Importing it also
registers the name directly (`register_transport` in `__init__.py`), which covers an
overlay whose distribution metadata is unreadable.

## Units and order, in one place

`GoalSpec` is the entire conversion contract and it is pure:

* `HeadPose` millimetres and degrees → **metres and a quaternion** (Pollen's own
  `Rotation.from_euler("xyz", [roll, pitch, yaw])` convention);
* antennas authored as `(left, right)` → the wire's **`[right, left]`**;
* every goal carries `lane="expression"` and `client="ros"` — except a de-escalation
  settle (`engagement:deescalate`, `cue:end`), which goes on the **safety** lane, because
  the calm a person is owed must not be dropped by the rule that stops expression
  flooding the queue.

## One batch at a time

A skill request expands to several intents (a cue plan is a shape, not a pose). They are
sent one after another on one worker thread, and a newer batch cancels what is left of
the previous one — including the goal in flight. The transport's answer is the **first**
goal's result: `completed` is an acceptance, `dropped` / `not_owner` / `head_reserved`
are the shared refusal vocabulary, and a server that is not there inside `timeout_s` is
`unreachable` and never confused with one that said no.

Ledger (readable from the facade's `/diagnostics` ledger and the rehearsal):
`goals_sent`, `goals_completed`, `goals_refused_<code>`, `batches_cancelled`,
`targets_published`, `skipped_<kind>`, `unreachable`.

## Dependencies

`rclpy`, `emori_skill_facade`, `reachy_mini_msgs`, `geometry_msgs`, `std_srvs`, and the
pip-installed adapter core (`emori_adapter`, no rosdep key — the image installs it with
`pip install .[pokamind]`).

Tested off the robot by `tests/test_skill_native.py`: the `rclpy` imports are guarded and the
mapping half runs on a machine with no ROS at all.
