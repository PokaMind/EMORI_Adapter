# reachy_mini_msgs

The ROS 2 interfaces for driving a [Reachy Mini](https://github.com/pollen-robotics/reachy_mini)
as a **native ROS component**: Pollen's transport-free `Backend` owned in-process by
`reachy_mini_ros2`, with no HTTP daemon anywhere in the motion path.

Licence: Apache-2.0 (`LICENSE`). Copyright 2026 Pokamind AB.

## Interfaces

| File | What it is |
|---|---|
| `msg/MotionStatus.msg` | What the motion arbiter is doing and who may move the robot. Latched (`transient_local`, depth 1) on `/reachy_mini/motion_status`. |
| `msg/BodyTarget.msg` | One streaming pose target on `/reachy_mini/set_target`. Unqueued; refused `busy` while a move runs, exactly as the daemon answers `{"status":"ignored"}`. |
| `action/GoTo.action` | Interpolated move to a head pose / antennas / body yaw over a duration. |
| `action/PlayRecordedMove.action` | One named move from a HuggingFace dataset. |
| `srv/SetMotorMode.srv` | `enabled` / `disabled` / `gravity_compensation`. |
| `srv/StopMove.srv` | Cancel one move, or (empty id) every accepted move. |
| `srv/SetOwner.srv` | Take or release the exclusive motion owner lock, with a TTL. |

## Conventions, which are Pollen's and not ours

* **Units**: metres and radians. Never millimetres, never degrees — those belong to the
  host application's own `HeadPose` and are converted at the edge.
* **Antenna order**: `[right, left]`. This is the daemon's wire order. Pollen's MuJoCo
  backend uses `[left, right]` internally with a sign flip; that quirk stays inside
  `reachy_mini_ros2/backend_host.py` and never reaches a message.
* **`head_pose`**: the head frame relative to `base_link`, as a `geometry_msgs/Pose`.
  Pollen's backend speaks 4×4 matrices; `reachy_mini_ros2.conversions` is the only
  place the two forms meet.
* **Joint order**, wherever nine joints appear (`/joint_states`, and the `head_joints`
  the REST shim reports): `body_yaw`, `stewart_1` … `stewart_6`, `right_antenna`,
  `left_antenna`.

## Arbitration is part of the request

`GoTo` and `PlayRecordedMove` carry `lane`, `client` and `preempt` because a robot with
more than one thing wanting to move it needs an answer to "who wins", and burying that
answer in a node's private state is what made the first version hard to reason about.

| Lane | Meaning |
|---|---|
| `safety` | Outranks everything. Cancels the running move and flushes queued expression work. A calm-settle is safety, not expression. |
| `interaction` | The mechanics of a conversation: wake, look, recenter. With `preempt`, cuts a running *expression* move short — and nothing else. |
| `expression` | Affect. Refused when the head is reserved, when this client is not the expression owner, and dropped when two requests are already waiting. |

A refused goal **succeeds** with `completed=false` and an `error_code` from
`bad_lane`, `not_owner`, `head_reserved`, `dropped`, `queue_full`, `busy`,
`backend_error`. It is not aborted: "the arbiter decided not to" is an answer, and a
caller has to be able to tell it apart from a fault.

A goal that was *accepted* and then did not run ends the same way, with an `error_code`
from `cancelled`, `preempted`, `timeout`, `backend_error` or `motors_disabled`.
`motors_disabled` means the motors were not under position control when the move came up
to run — almost always because a `goto_sleep` ran first, which ends by de-torquing the
robot. It is checked at the moment the move runs rather than when it is accepted, because
the answer changes while a request waits its turn. `wake_up` re-enables the motors before
it moves, so the way out of `motors_disabled` is a wake.

## Compatibility

Append at the end, never reorder, never rename, never retype — the same rule
`emori_msgs` follows, for the same reason: a released interface is a promise to people
who cannot be asked to rebuild.
