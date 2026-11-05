# The EMORI ROS 2 packages that use the core

The five packages in this directory import the `emori_adapter` Python core (or, for
`emori_adapter_bringup`, start nodes that do). They are Apache-2.0 like the rest of the
repository. The standalone packages, which never import the core, are in
[`../open/`](../open/README.md); every channel of the running graph is indexed in
[`docs/ros4hri-mapping.md`](../../../docs/ros4hri-mapping.md#every-ros-channel-at-a-glance).

| Package | What it is | Uses the core |
|---|---|---|
| `emori_adapter_ros` | The `publisher_node` (node name `emori_adapter`): the core's session model (live relay or mock arc) onto the REP-155 person topics, `Context` and `SpeechCue` | yes |
| `fiware_bridge` | The `bridge_node`: historizes every `Context` to Orion-LD with the core's FIWARE schema. Off on a robot, where the hostapp writes | yes |
| `emori_adapter_bringup` | The launch files `slice2.launch.py` (v1) and `v2.launch.py` (v2), and `config/topics.yaml` | no code of its own; launches the nodes that do |
| `emori_skill_native` | The `ros_native` skill transport for v2 ([README](emori_skill_native/README.md)) | yes |
| `emori_hub` | `main.py`: `python -m emori_hub.main`, the hostapp's app on uvicorn with rclpy beside it. `robot_link.py`: `RosRobotClient`, the core's robot client over the robot node's services, on node `/emori/robot_link` | yes |

## `emori_adapter` (`emori_adapter_ros/publisher_node`)

| Parameter | Default | Effect |
|---|---|---|
| `source` | `mock` (launch argument from env `EMORI_SOURCE`) | `mock` = the deterministic engagement arc; `pokamind` = the live session from the hostapp relay |
| `ws_url` | `ws://host.docker.internal:8080/ws/relay` (launch argument from env `EMORI_WS_URL`) | the hostapp relay; the robot compose profiles set `ws://127.0.0.1:8090/ws/relay` |
| `person_id` | `learner_01` | the stable topic namespace |
| `session_id` | `sim-session` | mock only |
| `cycle_period_s` / `cycles_per_phase` | `2.0` / `3` | mock only |
| `frame_id` | `camera` | header frame of the published messages |
| `loop` | `true` | mock only: restart the arc at its end |
| `mock_shape` | `rich` | `rich` \| `live` (the thin envelope the live session sends) |

Environment, read once at start: `EMORI_ROS_DEIDENTIFY` (default on: pseudonymous ids),
`EMORI_ROS_SPEECH_TEXT` (default off: `SpeechCue.text` empty) and `EMORI_ROS_ID_SALT`
(unset = a random salt per process). See the privacy section of
[`docs/ros4hri-mapping.md`](../../../docs/ros4hri-mapping.md).

## `fiware_bridge` (`fiware_bridge/bridge_node`)

| Parameter | Default | Effect |
|---|---|---|
| `person_id` | `learner_01` | the topic namespace it subscribes under |
| `orion_url` | env `ORION_URL`, else `http://host.docker.internal:1026` | the broker |
| `tenant` | env `NGSILD_TENANT`, else `pokamind` | the NGSI-LD tenant |
| `ql_notify_url` | env `QL_NOTIFY_URL`, else `http://quantumleap:8668/v2/notify` | QuantumLeap's notify endpoint |
| `enabled` | true unless env `FIWARE_BRIDGE_ENABLED=0` | `0` keeps the node up and writing nothing |

Started through the bringup launch files, the bridge also gets `config/topics.yaml`, whose
`orion_url` and `tenant` then win over the `ORION_URL` and `NGSILD_TENANT` defaults.

## Launch files (`emori_adapter_bringup`)

`config/topics.yaml` is passed to every EMORI node as a `/**` parameter file: `person_id`,
`session_id`, `cycle_period_s`, `cycles_per_phase`, `source`, `ws_url`, `orion_url` and
`tenant`. The launch arguments below override `person_id`, and for the adapter `source`
and `ws_url`.

`slice2.launch.py` (v1):

| Argument | Env | Default |
|---|---|---|
| `source` | `EMORI_SOURCE` | `mock` |
| `ws_url` | `EMORI_WS_URL` | `ws://host.docker.internal:8080/ws/relay` |
| `person_id` | — | `learner_01` |
| `skill_url` | `EMORI_SKILL_URL` | `http://host.docker.internal:8080/api/skill` |
| `skill_transport` | `EMORI_SKILL_TRANSPORT` | `hostapp_http` (`hostapp_http` \| `log`) |
| `with_fiware` | `EMORI_WITH_FIWARE` | `true` |

`v2.launch.py` takes the same arguments, with `skill_transport` defaulting to
`ros_native`, plus the robot node's `backend` (`REACHY_BACKEND`, `mujoco`), `serialport`
(`REACHY_SERIALPORT`), `wireless_version` (`REACHY_WIRELESS`), `rest_port`
(`REACHY_REST_PORT`, `8000`), `with_camera_v4l2` (`WITH_CAMERA_V4L2`) and
`expression_owner` (`EMORI_ROS_MOTION_OWNER`, `hostapp`), which it forwards to
`reachy_mini_ros2`'s `robot.launch.py`. The robot node's other arguments are in
[its README](../open/reachy_mini_ros2/README.md#launch-arguments).

## `emori_hub` (`python -m emori_hub.main`)

One process: the hostapp's FastAPI app on uvicorn on the main thread, and rclpy on threads
beside it. Three executors, so that nothing on the session side can starve a response a
caller is waiting for:

| Thread | Executor | Runs |
|---|---|---|
| `ros-link` | single-threaded | `/emori/robot_link`: the robot client's responses and the heartbeat's time stamp, nothing else |
| `ros-facade` | multi-threaded, 3 | nodes that expose the app on the graph |
| `ros-graph` | multi-threaded, 3 | with [`EMORI_HUB_GRAPH=1`](../../../docs/configuration.md#emori_hub_graph), the graph's four nodes (`graph.py`), with parameters from `config/graph.yaml` |
| `ros-fanout` | none, one plain thread | the relayed envelopes behind a queue: `/emori/session/events`, and `/emori_adapter`'s feed (`fanout.py`) |

A group with no nodes has no executor and no thread. A session's motion runs on none of
these. It reaches the fan-out through one queue put, and a node that falls behind loses
envelopes, which are counted and show as gaps in `SessionEvent.seq`, and costs motion nothing.

**A stop.** On SIGTERM or SIGINT uvicorn stops serving and runs the app's lifespan teardown
as it does for the hostapp. Then ROS goes, last started first: `ros-graph`, `ros-facade`, the
robot client, `ros-link`, the context. The exit code is 0. The hub parks nothing: the head
holds its last target. A failed start leaves by the same path, with exit code 1, or uvicorn's
own code when it could not bind or the lifespan's startup failed.

**A dead executor.** When an executor's thread ends with nobody asking, or the link's executor
does nothing but raise for about two seconds, the process stops itself the same way and exits
with code 70, so that its supervisor builds a new one. A callback that raises is logged and
costs only that callback. A stop the process started itself must be over within 12 s, and the
ROS teardown within 3 s of the server's return; past that it exits anyway, which also covers a
thread that would hold the interpreter open.

It takes its environment from the JSON file named by `EMORI_HUB_ENV_JSON`, when that is set,
before anything of the core is loaded. A file that is named and cannot be read stops the start.
The robot transport is [`EMORI_ROBOT_TRANSPORT`](../../../docs/configuration.md#emori_robot_transport).

### Speech and gesture nodes

Two nodes on `ros-facade` show part of the app on the graph and offer part of its commands.
Each is built by a function taking the app and returning the node, and takes everything from
the objects the HTTP routes use.

| Node | Offers |
|---|---|
| `/emori/speech` (`speech_node.py`) | action `/emori/speech/say`, services `/emori/speech/recognize` and `/emori/speech/set_language`, topics `/emori/speech/voice` (10 Hz, only while subscribed), `/emori/language` and `/emori/audio/mic_profile` (latched) |
| `/emori/gesture` (`gesture_node.py`) | topics `/emori/gesture/events` and `/emori/gesture/state` (latched), services `/emori/gesture/reset_sensing` and `/emori/gesture/inject_turn` |

`say` is refused while a training session runs or an add-on mode holds the robot, and holds the
voice tap off while it plays, as the kiosk page does for its own line. `inject_turn` is refused
unless [`EMORI_HUB_GESTURE_INJECT`](../../../docs/configuration.md#emori_hub_gesture_inject) is on.
A head turn is published by a function that replaces the gesture ticker's `on_direction` hook,
publishes, and then calls the hook it replaced.

Neither node is on the path a session's motion takes. They never call the robot client, and a
reader that waits on a lock another thread takes runs on a thread of its own. What they cannot
publish is counted (`facade_kit.Counters`), not queued.

## Building

These packages need the core installed into the same Python as ROS
(`pip install ".[pokamind]"` from the repository root, as the v1 image does), then:

    colcon build --packages-select emori_msgs emori_adapter_ros fiware_bridge emori_adapter_bringup

`emori_skill_native`, `emori_hub` and `v2.launch.py` also need the two `reachy_mini_*`
packages, and therefore Jazzy.
