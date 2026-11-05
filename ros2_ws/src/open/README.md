# The standalone EMORI ROS 2 packages

The six packages in this directory are standalone ROS 2 packages (**Apache-2.0**). They
build and run without the EMORI core: nothing here imports `emori_adapter` or depends on
a package under the sibling [`../emori/`](../emori/), which holds the four packages that
do use the core. colcon does not care about the split (it recurses into both); the split
makes it obvious from the path what can be built and used on its own. The
repository-wide licence overview is [`LICENSING.md`](../../../LICENSING.md).

## What is here

| Package | What it is | Distro |
|---|---|---|
| [`emori_msgs`](emori_msgs/) | The interfaces: `Context`, `SpeechCue`, the skill messages and actions | Humble |
| [`hri_context_analyzer`](hri_context_analyzer/) | Turns a per-person `Context` into ROS4HRI engagement and context topics | Humble |
| [`hri_engagement_behaviors`](hri_engagement_behaviors/) | The engagement policy that decides when the robot should react, and how | Humble |
| [`emori_skill_facade`](emori_skill_facade/) | The bounded skill surface (`/skill/*`) and the pluggable transport where a decision leaves ROS | Humble |
| [`reachy_mini_msgs`](reachy_mini_msgs/) | Interfaces for the Reachy Mini as a ROS 2 node (`GoTo`, `PlayRecordedMove`, motion status) | Jazzy |
| [`reachy_mini_ros2`](reachy_mini_ros2/) | Pollen's Reachy Mini backend driven in-process as a ROS 2 node, with an arbiter and a REST shim on `:8000` | Jazzy |

The two `reachy_mini_*` packages are a robot driver anybody with a Reachy Mini can run.
Each package's own `README.md` documents its topics, parameters and how to build it; the
packages that use the core are described in [`../emori/README.md`](../emori/README.md),
and every channel of the running graph is indexed in
[`docs/ros4hri-mapping.md`](../../../docs/ros4hri-mapping.md#every-ros-channel-at-a-glance).
The v1 Humble image builds all ten packages, so `ros2 interface list` on a v1 robot shows
`reachy_mini_msgs` although no robot node runs there.
Releasing the packages to the ROS index is described in
[`docs/ros4hri-release.md`](../../../docs/ros4hri-release.md).

## Building the packages on their own

The four Humble packages, on a plain ROS 2 Humble with no EMORI anything:

    mkdir -p ws/src && cd ws
    cp -r <repo>/ros2_ws/src/open/{emori_msgs,hri_context_analyzer,hri_engagement_behaviors,emori_skill_facade} src/
    sudo apt-get install -y ros-humble-hri-msgs
    rosdep install --from-paths src -y --ignore-src
    colcon build --packages-select emori_msgs hri_context_analyzer hri_engagement_behaviors emori_skill_facade
    source install/setup.bash

The same thing as a reproducible image, which is also the proof that nothing here needs
the EMORI core (the image has no Python package installer at all):

    docker build -t emori/ros-standalone:amd64 -f deploy/ros2/Dockerfile.standalone .
    docker run --rm emori/ros-standalone:amd64    # starts the three nodes, lists the graph

`scripts/ros_standalone_check.py` (in the repository root) does both and checks the node
list.

The two `reachy_mini_*` packages need **Jazzy** (Pollen's SDK requires Python 3.11 or
newer) plus the SDK from pip, which has no rosdep key:

    pip install "reachy-mini[mujoco]==1.10.0"    # simulator / dev; drop [mujoco] on a robot
    colcon build --packages-select reachy_mini_msgs reachy_mini_ros2
    ros2 launch reachy_mini_ros2 robot.launch.py  # MuJoCo, headless

Face look-at needs `ros-jazzy-hri-msgs` (rosdep installs it) and `onnxruntime` (a
`reachy-mini` dependency); without them the node still runs and logs that gaze is
unavailable.

## Tests

No package ships its own `test/` directory: the tests live in the repository's `tests/`,
which loads the pure-Python halves of these nodes by path so they run without ROS. The
ones that guard this directory as a whole are:

* `tests/test_ros_standalone.py` — no `.py` here imports `emori_adapter` (checked with
  `ast`, not grep);
* `tests/test_ros_publishing.py` — licences, `package.xml` metadata, changelogs, the
  standalone Dockerfile, and that no package here depends on a package under `../emori/`;
* `tests/test_ros_contracts.py` — the message definitions, the publisher/subscriber graph
  and the launch files agree with each other.

A standalone `colcon test --packages-select <pkg>` therefore runs nothing.

## Licence

Apache-2.0, Copyright 2026 Pokamind AB; the verbatim text is in each package's `LICENSE`.
The exception is `reachy_mini_ros2/reachy_mini_ros2/gaze/{vision,geometry,tracking}.py`,
which are vendored and modified from Pollen Robotics' `reachy_mini` 1.10.0 (Apache-2.0,
Copyright 2025 Pollen Robotics) and listed in
[`reachy_mini_ros2/THIRD_PARTY_NOTICES.md`](reachy_mini_ros2/THIRD_PARTY_NOTICES.md).
Contributions are welcome under the same licence — see [`CONTRIBUTING.md`](CONTRIBUTING.md).
