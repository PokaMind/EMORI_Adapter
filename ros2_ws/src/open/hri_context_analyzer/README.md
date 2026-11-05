# hri_context_analyzer

The Context Analysis Module. One node. It takes the
structured `emori_msgs/Context` a session produces and fans it out as small, standard
per-person topics under the ROS4HRI (REP-155) person namespace, so a downstream
behaviour or analytics module can subscribe to the one number it cares about instead of
learning our message.

Licence: Apache-2.0 (`LICENSE`). Copyright 2026 Pokamind AB.

## The one rule worth knowing

**A channel that did not report is not published.** Not published as zero, not published
as the sentinel — not published. `emori_msgs/Context` marks each observed channel with a
bit in `report_flags` and marks each unobserved float with `Context.NOT_REPORTED`
(`-1.0`), and this node honours both: a float is fanned out only when its channel's flag
is set *and* the value is not the sentinel.

The reason is that a stream of confident zeroes is indistinguishable, on a plot, from a
stream of measured zeroes — and the difference between them is a learner nobody looked at
versus a learner who scored nothing. `/context/degraded` (`std_msgs/Bool`, published
every cycle) says out loud when `report_flags == 0`.

## Topics

`<base>` below is `/humans/persons/<person_id>/context`, with `person_id` the node
parameter (default `learner_01`).

**Subscribes**

| Topic | Type |
|---|---|
| `<base>` | `emori_msgs/Context` |

**Publishes, every cycle**

| Topic | Type | Note |
|---|---|---|
| `<base>/escalation` | `std_msgs/Float32` | v1 |
| `<base>/rapport` | `std_msgs/Float32` | v1 |
| `<base>/attention` | `std_msgs/Float32` | v1 |
| `<base>/engagement_trend` | `std_msgs/Float32` | v1 |
| `<base>/dominant_style` | `std_msgs/String` | v1 |
| `<base>/engagement_score` | `std_msgs/Float32` | always computed |
| `<base>/valence` | `std_msgs/Float32` | always computed |
| `<base>/arousal` | `std_msgs/Float32` | always computed |
| `<base>/degraded` | `std_msgs/Bool` | `report_flags == 0` |

**Publishes, only when the channel reported**

| Topic | Type | Gate |
|---|---|---|
| `<base>/age_s` | `std_msgs/Float32` | value is not `NOT_REPORTED` |
| `<base>/body/posture`, `/body/head_position`, `/body/gesture_type`, `/body/energy` | `std_msgs/String` | non-empty text |
| `<base>/body/engagement`, `/body/confidence` | `std_msgs/Float32` | `REPORT_BODY` and not the sentinel |
| `<base>/face/positivity` | `std_msgs/Float32` | `REPORT_FACE` and not the sentinel |
| `<base>/voice/warmth` | `std_msgs/Float32` | `REPORT_VERBAL` and not the sentinel |
| `<base>/voice/pitch_variety` | `std_msgs/Float32` | `REPORT_TONE` and not the sentinel |
| `<base>/top_styles` | `std_msgs/String` | `REPORT_STYLES`; formatted `name:0.31,name:0.18` |
| `/humans/persons/<person_id>/expression` | `hri_msgs/Expression` | any channel reported |

The expression topic is the standard ROS4HRI one, written with whatever shape the
installed `hri_msgs` declares: 2.1.0 makes `Expression.expression` a **string** with
class constants (`NEUTRAL = "neutral"`), older releases made it a `uint8`. The node
introspects the field rather than assuming, and names the label in a warning when it
cannot be expressed instead of dropping it silently.

## Parameters

| Parameter | Default | Effect |
|---|---|---|
| `person_id` | `learner_01` | the REP-155 namespace segment this node reads and writes |

## Layout

* `context_analyzer_node.py` — the ROS half: parameters, publishers, subscription.
* `contracts.py` — the pure half: `ReportFlags`, `plan_fan_out`, `expression_field_shape`,
  `set_expression_field`, `format_styles`. Stdlib only, no `rclpy`, nothing EMORI-specific,
  so the fan-out rule can be unit-tested on a laptop with no ROS installed.

At start-up the node compares the five `REPORT_*` constants on the **installed**
`emori_msgs/Context` against `contracts.ReportFlags` and logs an ERROR naming the
offenders if they disagree. A renumbered bit would otherwise gate the wrong channel
forever, and the only symptom would be a topic that never publishes.

## Building it on its own

`ros-humble-hri-msgs` is on the ROS index; `emori_msgs` is in this repository and must be
in the same workspace (colcon builds it first, because this package depends on it).

    mkdir -p ws/src && cd ws
    cp -r <emori_msgs> <hri_context_analyzer> src/
    sudo apt-get install -y ros-humble-hri-msgs
    colcon build --packages-select emori_msgs hri_context_analyzer
    source install/setup.bash
    ros2 run hri_context_analyzer context_analyzer

Dependencies: `rclpy`, `std_msgs`, `hri_msgs`, `emori_msgs`. Nothing else, and in
particular nothing from the EMORI application — `deploy/ros2/Dockerfile.standalone` in
the parent repository builds and runs this node in an image where the EMORI core is not
installed, which is what keeps that claim true.
