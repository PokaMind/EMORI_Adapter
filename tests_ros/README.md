# tests_ros

Tests that run against a real `rclpy`. The rest of the suite (`tests/`) drives the hub's ROS
code through injected fakes and needs no ROS; the fakes encode assumptions about `rclpy`, and
these tests are where each assumption meets the real thing.

| File | Assumption |
|---|---|
| `test_service_client.py` | `call_async` and `service_is_ready` work from caller threads while another thread spins; `remove_pending_request` drops a late answer; a list assigns to a fixed `float64[2]` |
| `test_executors.py` | `MultiThreadedExecutor(num_threads=)` runs that many callbacks at once; `SupervisedSpin` drives it and reports a context that went away; `executor.shutdown()` blocks on a running callback and says so when given a timeout |
| `test_signals_and_shutdown.py` | `SignalHandlerOptions.NO` leaves the process's own handlers in place; `try_shutdown` may be called again and before `init`; the hub's bindings start and stop a context |
| `test_robot_link.py` | the robot client and link against a stand-in robot node answering the real message types (`link_scenario.py`) |
| `test_facade_nodes.py` | the real app with the hub's six facade nodes on `NodeGroup`'s executor and a client over DDS: each builder accepts its bindings (node, QoS, callback groups, the `Say` action server); every service answers with the interface's fields; latched samples reach a late subscriber; a relayed envelope becomes a typed, gapless `SessionEvent` (rosidl checks every assignment); `/diagnostics` carries the gateway's entries; stopping leaves no exception and the gesture tee off. The graph's four nodes come up from `config/graph.yaml` with source `hub` and a fed envelope reaches `/emori_adapter`'s topics |

## Running them

They skip when `rclpy` cannot be imported, so `pytest tests_ros` on a computer without ROS
collects and skips. Where they are meant to run, set `EMORI_REQUIRE_ROS=1`: a missing `rclpy`
or interface package is then a failure, so a broken image cannot pass by running nothing.

CI runs them in the hub image (`scripts/ci/hub-ci.sh tests`, job `ros2-hub`). Locally, with a
ROS 2 Jazzy environment sourced and the workspace built:

```
EMORI_REQUIRE_ROS=1 python3 -m pytest tests_ros -p no:cacheprovider
```

Each test uses a context of its own on a domain derived from the process, and services
named for the test, so two runs on one computer do not meet.
