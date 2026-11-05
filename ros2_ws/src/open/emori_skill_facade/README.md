# emori_skill_facade

The one component in the EMORI graph allowed to turn a decision into robot motion — and
the plug point where that decision leaves ROS.

It serves a small, bounded skill surface (`/skill/replay_motion`, `/skill/look_at`,
`/skill/set_expression`) and hands each request to a **transport**: the host
application's single-writer `/api/skill` intake, or another skill transport. It is its
own package precisely because that boundary should
be replaceable without touching the node that decides.

Licence: Apache-2.0 (`LICENSE`). Copyright 2026 Pokamind AB.

## Two gates, in this order

1. **The owner gate, in the node.** `EMORI_ROS_MOTION_OWNER` is `hostapp` (default),
   `ros`, or `none`; anything unrecognised is read as `hostapp`. Unless it is exactly
   `ros`, every request is counted as `suppressed` and goes no further. The gate lives in
   the node rather than in a transport, so it holds for every transport ever plugged in —
   including ones that do not exist yet.
2. **The intake, downstream.** Even with the owner set to `ros`, the receiving side may
   refuse: wrong key, head reserved, rate limit, queue full. Each answer is mapped to an
   outcome and counted. The facade never retries and never queues; a refused expression is
   one the robot was right not to make a second later.

There is exactly one writer to the robot at any time: the host application's
`RobotPlayer` queue on v1, the robot node's arbiter on v2 — never this graph's decision
nodes. That is the invariant the whole package exists to keep.

## Transports

| Name | What it does |
|---|---|
| `hostapp_http` (default) | `POST` the skill body to `EMORI_SKILL_URL` with an `X-Emori-Skill-Key` header and a short timeout. HTTP status and `error_code` are mapped through `contract.py`. |
| `log` | Accepts everything and records the body. No network, no key, no robot. This is what benches and the standalone smoke run: the node's whole path executes and nothing can move. |
| `ros_native` | Provided by the `emori_skill_native` package (entry point group `emori_skill_facade.transports`, also registered at import), and only present when it is installed: sends `GoTo` and `PlayRecordedMove` goals, `BodyTarget` messages and a `wake_up` service call to the `reachy_mini_ros2` node. The v2 default. |

Adding another skill transport is one class in `transport.py` plus one launch argument —
`select_transport()` is the seam, not a rewrite. An unknown transport name is a
`ValueError` listing the known names; the node catches it, logs an ERROR and falls back
to `log`, because defaulting to a live wire on a typo is the one failure that moves a
robot nobody asked to move.

## Interfaces

| Name | Type | Direction |
|---|---|---|
| `/skill/replay_motion` | `emori_msgs/action/ReplayMotion` | action server |
| `/skill/look_at` | `emori_msgs/action/LookAt` | action server |
| `/skill/set_expression` | `emori_msgs/SetExpression` | subscription |
| `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | publisher, every `ledger_period_s` |

The ledger's status name is `emori_skill_facade` and its `hardware_id` is the transport
name. Values: `posted`, `accepted`, `dropped`, `refused`, `suppressed`, `unreachable`,
plus `motion_owner`, `transport` and `url_host`, and with `ros_native` the transport's own
`goals_sent`, `goals_completed`, `goals_refused_<code>`, `batches_cancelled`,
`targets_published` and `skipped_<kind>`. Level is ERROR when the owner is `ros`,
the transport is `hostapp_http` and the key is empty (every request will be refused);
WARN when `unreachable` grew since the last tick; OK otherwise — including a facade that
is suppressing everything, which is the shipped default and not a fault.

`posted` is counted by the node immediately after the owner gate and before the transport
is called, so `posted + suppressed` always equals the number of requests the graph made,
whichever transport is in use.

## Parameters

| Parameter | Env | Default | Effect |
|---|---|---|---|
| `motion_owner` | `EMORI_ROS_MOTION_OWNER` | `hostapp` | `ros` is the only value that lets anything through |
| `transport` | `EMORI_SKILL_TRANSPORT` | `hostapp_http` | `hostapp_http`, `log`, or a registered transport such as `ros_native` |
| `skill_url` | `EMORI_SKILL_URL` | `http://127.0.0.1:8080/api/skill` | `hostapp_http` only |
| `skill_key` | `EMORI_SKILL_KEY` | `""` | shared secret, `X-Emori-Skill-Key` |
| `timeout_s` | `EMORI_SKILL_TIMEOUT_S` | `1.0` | per request; short on purpose |
| `ledger_period_s` | — | `5.0` | `/diagnostics` cadence |

## Layout

* `contract.py` — the pure decision half: owner normalisation, the three request bodies,
  the outcome vocabulary, the counters. Stdlib only, and no `urllib` either: it decides,
  it does not hop.
* `transport.py` — `SkillTransport`, `HostappHttpTransport`, `LogOnlyTransport`,
  `select_transport`. The transport plug point.
* `facade_node.py` — the ROS half. `rclpy` is imported behind a guard so the file can be
  read and reasoned about on a machine with no ROS at all.

## Building it on its own

    mkdir -p ws/src && cd ws
    cp -r <emori_msgs> <emori_skill_facade> src/
    colcon build --packages-select emori_msgs emori_skill_facade
    source install/setup.bash
    EMORI_SKILL_TRANSPORT=log ros2 run emori_skill_facade skill_facade

Dependencies: `rclpy`, `emori_msgs`, `diagnostic_msgs`, `action_msgs`. Nothing from the
EMORI application, and with `EMORI_SKILL_TRANSPORT=log` nothing outside the container
either.
