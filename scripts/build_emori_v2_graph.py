"""Render the EMORI v2 architecture figure: the robot as a native ROS 2 component.

An architecture figure, not a progress figure. Every box is classified by what it IS
(hostapp, robot node, Pollen driver, EMORI ROS 2 graph, REP-155 standard topic, EMORI
extension, hardware, outside consumer), never by whether it is finished. It draws the v2
arrangement (``build_emori_system_graph.py`` draws v1): Pollen's driver inside a ROS 2
node, the EMORI graph beside it in the same container, and the hostapp as one client of
the robot through a REST shim that speaks the daemon's dialect.

No caption and no footer text on purpose.

Output:

    docs/architecture/emori-v2-native-robot.svg

Render the PNG with headless Chromium at this canvas's size (2400x1000 CSS px, scale 2 ->
4800x2000). Use ``--headless=old`` where the new headless mode exits 0 and silently
leaves the previous PNG in place (seen with some Chrome builds on Windows).

    chrome --headless=old --disable-gpu --force-device-scale-factor=2 \\
           --screenshot=out.png --window-size=2400,1000 \\
           file:///.../docs/architecture/emori-v2-native-robot.svg
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "docs" / "architecture" / "emori-v2-native-robot.svg"

W, H = 2400, 1000

# Kind -> (stroke, fill, accent). One tone per kind of element; no status tones.
TONES = {
    "host": ("#b45309", "#fffbeb", "#fde68a"),      # the hostapp and its lanes
    "node": ("#1d4ed8", "#eff6ff", "#bfdbfe"),      # the robot node (ours)
    "driver": ("#475569", "#f8fafc", "#cbd5e1"),    # Pollen's driver, used as-is
    "ros": ("#7c3aed", "#f5f3ff", "#ddd6fe"),       # the EMORI ROS 2 graph
    "rep": ("#0f766e", "#f0fdfa", "#99f6e4"),       # REP-155 standard interface
    "ext": ("#be123c", "#fff1f2", "#fecdd3"),       # EMORI extension interface
    "hw": ("#1f2937", "#f3f4f6", "#d1d5db"),        # hardware
    "out": ("#15803d", "#f0fdf4", "#bbf7d0"),       # outside consumers
}

LEGEND = (
    ("HOSTAPP", "host"),
    ("ROBOT NODE", "node"),
    ("POLLEN DRIVER", "driver"),
    ("EMORI ROS 2 GRAPH", "ros"),
    ("REP-155 STANDARD", "rep"),
    ("EMORI EXTENSION", "ext"),
    ("HARDWARE", "hw"),
    ("OUTSIDE CONSUMER", "out"),
)


def esc(text: str) -> str:
    return escape(str(text), quote=True)


@dataclass(frozen=True)
class Box:
    id: str
    x: int
    y: int
    w: int
    h: int
    tone: str
    title: str
    lines: tuple[str, ...] = ()
    badge: str = ""
    tag: str = ""
    dashed: bool = False


@dataclass(frozen=True)
class Edge:
    id: str
    points: tuple[tuple[int, int], ...]
    tone: str
    title: str
    label: str = ""
    label_at: tuple[int, int] | None = None
    anchor: str = "start"
    dashed: bool = False
    width: float = 2.6
    arrow: bool = True


@dataclass(frozen=True)
class Group:
    id: str
    x: int
    y: int
    w: int
    h: int
    label: str
    title: str


def _pts(points):
    return " ".join(f"{x},{y}" for x, y in points)


def _text_lines(items, x, y, cls, lh):
    spans = [
        f'<tspan x="{x}" dy="{0 if i == 0 else lh}">{esc(t)}</tspan>'
        for i, t in enumerate(items)
    ]
    return f'    <text x="{x}" y="{y}" class="{cls}">' + "".join(spans) + "</text>"


def box_svg(b: Box) -> str:
    cls = f"box box-{b.tone}" + (" box-dashed" if b.dashed else "")
    out = [
        f'  <g id="{esc(b.id)}">',
        f"    <title>{esc(b.title)}</title>",
        f'    <rect x="{b.x}" y="{b.y}" width="{b.w}" height="{b.h}" rx="9" class="{cls}"/>',
    ]
    ty = b.y + 27
    if b.badge:
        out.append(
            f'    <text x="{b.x + 16}" y="{ty}" class="badge badge-{b.tone}">{esc(b.badge)}</text>'
        )
        if b.tag:
            out.append(
                f'    <text x="{b.x + b.w - 16}" y="{ty}" class="tag"'
                f' text-anchor="end">{esc(b.tag)}</text>'
            )
        ty += 21
    out.append(f'    <text x="{b.x + 16}" y="{ty}" class="b-title">{esc(b.title)}</text>')
    if b.lines:
        out.append(_text_lines(b.lines, b.x + 16, ty + 23, "b-body", 17))
    out.append("  </g>")
    return "\n".join(out)


def edge_svg(e: Edge) -> str:
    cls = f"edge edge-{e.tone}" + (" dashed" if e.dashed else "")
    marker = f' marker-end="url(#a-{e.tone})"' if e.arrow else ""
    out = [
        f'  <g id="{esc(e.id)}">',
        f"    <title>{esc(e.title)}</title>",
        f'    <polyline points="{_pts(e.points)}" class="{cls}"'
        f' stroke-width="{e.width}"{marker}/>',
    ]
    if e.label:
        lx, ly = e.label_at
        out.append(
            f'    <text x="{lx}" y="{ly}" class="wire w-{e.tone}"'
            f' text-anchor="{e.anchor}">{esc(e.label)}</text>'
        )
    out.append("  </g>")
    return "\n".join(out)


def group_svg(g: Group) -> str:
    return "\n".join((
        f'  <g id="{esc(g.id)}">',
        f"    <title>{esc(g.title)}</title>",
        f'    <rect x="{g.x}" y="{g.y}" width="{g.w}" height="{g.h}" rx="16" class="group"/>',
        f'    <text x="{g.x + 20}" y="{g.y + 26}" class="group-label">{esc(g.label)}</text>',
        "  </g>",
    ))


GROUPS = (
    Group("g-node", 600, 150, 890, 580,
          "reachy_mini  ·  ROS 2 NODE  ·  OWNS POLLEN'S BACKEND IN-PROCESS  ·  NO DAEMON",
          "the robot node"),
    Group("g-graph", 1530, 150, 820, 580,
          "EMORI ROS4HRI GRAPH  ·  SAME CONTAINER",
          "the EMORI ROS 2 graph"),
)

BOXES = (
    # --- left: the hostapp, one client of the robot -----------------------
    Box("l-poka", 50, 160, 250, 96, "host", "Pokamind session", badge="HOSTAPP",
        lines=("analysis cycles, speech events",)),
    Box("l-kiosk", 50, 276, 250, 56, "host", "kiosk page", badge="HOSTAPP"),
    Box("l-hub", 50, 352, 250, 96, "host", "hostapp · POST /api/cycle", badge="HOSTAPP",
        lines=("identity stamp, fan-out",)),
    Box("l-driver", 50, 468, 250, 118, "host", "Translator + BehaviorEngine",
        badge="HOSTAPP",
        lines=("in-process expressive lanes", "gated by EMORI_ROS_MOTION_OWNER")),
    Box("l-player", 50, 606, 250, 118, "host", "RobotPlayer", badge="HOSTAPP",
        lines=("single writer, 12-slot queue",)),
    Box("l-relay", 330, 352, 220, 96, "host", "/ws/relay", badge="HOSTAPP",
        lines=("WebSocket broadcast",)),
    Box("l-fclient", 330, 468, 220, 118, "host", "FiwareClient", badge="HOSTAPP",
        lines=("NGSI-LD writer",)),

    # --- centre: the robot node ------------------------------------------
    Box("c-shim", 620, 190, 270, 130, "node", "REST shim · :8000", badge="ROBOT NODE",
        lines=("daemon REST dialect", "FastAPI on a uvicorn thread")),
    Box("c-arb", 910, 190, 280, 130, "node", "MotionArbiter", badge="ROBOT NODE",
        lines=("asyncio, one move at a time", "safety > interaction > expression",
               "owner lock, head reservation")),
    Box("c-actions", 1210, 190, 260, 130, "node", "actions + services", badge="ROBOT NODE",
        tag="reachy_mini_msgs",
        lines=("GoTo, PlayRecordedMove", "wake_up, goto_sleep, set_motor_mode",
               "stop, set_owner, set_head_reserved", "set_target (topic)")),

    Box("c-backend", 620, 345, 420, 130, "driver", "BackendHost (Pollen Backend)",
        badge="POLLEN DRIVER", tag="reachy-mini 1.10.0",
        lines=("in-process, 50 Hz control loop, IK / FK",
               "mujoco | mockup | robot")),
    Box("c-life", 1060, 345, 410, 130, "node", "lifecycle", badge="ROBOT NODE",
        lines=("wake_up on start", "reset_to_sleep on SIGINT",
               "state: error and exit on loop death")),

    Box("c-percep", 620, 500, 420, 130, "node", "perception streams", badge="ROBOT NODE",
        lines=("/reachy_mini/camera/image_raw, camera_info",
               "GStreamer v4l2src on camera.device | MuJoCo",
               "v4l2_camera (Route B) | audio_common (optional)")),
    Box("c-pub", 1060, 500, 410, 130, "node", "state publishers", badge="ROBOT NODE",
        lines=("/joint_states 50 Hz", "/reachy_mini/head_pose, /tf",
               "/reachy_mini/motion_status, /diagnostics")),

    # --- right: the EMORI graph, native to the robot ---------------------
    Box("r-adapter", 1550, 190, 240, 118, "ros", "emori_adapter", badge="EMORI ROS 2 GRAPH",
        lines=("relay → ROS4HRI person topics", "pseudonymised ids")),
    Box("r-bridge", 1550, 330, 240, 118, "ros", "fiware_bridge", badge="EMORI ROS 2 GRAPH",
        lines=("NGSI-LD historisation",)),
    Box("r-facade", 1550, 468, 240, 118, "ros", "emori_skill_facade",
        badge="EMORI ROS 2 GRAPH",
        lines=("transport registry", "hostapp_http | log | entry points")),
    Box("r-native", 1550, 606, 240, 118, "ros", "emori_skill_native",
        badge="EMORI ROS 2 GRAPH",
        lines=("ros_native transport", "skills → action goals")),

    Box("t-tracked", 1810, 190, 250, 52, "rep", "/humans/persons/tracked", tag="REP-155",
        badge=" "),
    Box("t-anon", 1810, 252, 250, 52, "rep", ".../<id>/anonymous", tag="REP-155", badge=" "),
    Box("t-eng", 1810, 314, 250, 52, "rep", ".../<id>/engagement_status", tag="REP-155",
        badge=" "),
    Box("t-ctx", 1810, 376, 250, 52, "ext", ".../<id>/context", tag="EMORI ext", badge=" "),
    Box("t-cue", 1810, 438, 250, 52, "ext", ".../<id>/speech_cue", tag="EMORI ext",
        badge=" "),

    Box("r-analyzer", 2080, 190, 250, 118, "ros", "hri_context_analyzer",
        badge="EMORI ROS 2 GRAPH",
        lines=("Context → /context/* scalars", "+ /expression")),
    Box("r-behav", 2080, 330, 250, 150, "ros", "hri_engagement_behaviors",
        badge="EMORI ROS 2 GRAPH",
        lines=("policy: one request per event", "speech yield, de-escalation")),
    Box("r-skill", 2080, 510, 250, 100, "ext", "/skill/*", badge="EMORI EXTENSION",
        lines=("set_expression (topic)", "replay_motion, look_at (actions)")),

    # --- bottom: what surrounds the container -----------------------------
    Box("b-analytics", 50, 830, 520, 130, "out", "Analytics consumer (FIWARE)",
        badge="OUTSIDE CONSUMER",
        lines=("Orion-LD / QuantumLeap, off-robot",
               "recoveryAttempts, recoverySuccesses")),
    Box("b-hw", 600, 830, 870, 130, "hw", "Reachy Mini wireless", badge="HARDWARE",
        dashed=True,
        lines=("CM4 · 9 Dynamixel servos, /dev/ttyAMA3, 1 Mbaud",
               "IMX708 → v4l2loopback /dev/video40 (kiosk), /dev/video41 (ROS tee)",
               "PipeWire audio",
               "ROS 2 Jazzy container, host network, device passthrough")),
    Box("b-tools", 1500, 830, 400, 130, "out", "any ROS 2 tool or ROS4HRI node",
        badge="OUTSIDE CONSUMER",
        lines=("rviz, rosbag, hri_msgs subscribers",
               "/joint_states, /tf, REP-155 topics")),
    Box("b-loop", 1930, 830, 420, 130, "node", "face look-at (in-process)",
        badge="ROBOT NODE",
        lines=("camera frame → YuNet → set_target (ros:gaze)",
               "publishes /humans/faces/tracked, .../<id>/roi",
               "run switch: POST /api/emori/gaze")),
)

EDGES = (
    # the hostapp chain
    Edge("e-poka-kiosk", ((175, 256), (175, 276)), "host", "the session runs in the kiosk page"),
    Edge("e-kiosk-hub", ((175, 332), (175, 352)), "host", "the page relays each envelope"),
    Edge("e-hub-driver", ((175, 448), (175, 468)), "host", "the in-process driver"),
    Edge("e-driver-player", ((175, 586), (175, 606)), "host", "motion intents reach the queue"),
    Edge("e-hub-relay", ((300, 400), (330, 400)), "host", "the broadcast"),
    Edge("e-driver-fclient", ((300, 521), (330, 521)), "host", "the driver writes to the broker"),
    Edge("e-player-shim", ((300, 665), (580, 665), (580, 255), (620, 255)), "host",
         "the hostapp drives the robot through the shim, unchanged", width=3.0,
         label="POST /api/move/*", label_at=(310, 658)),
    Edge("e-relay-adapter", ((440, 352), (440, 135), (1510, 135), (1510, 245), (1550, 245)), "host",
         "the ROS graph reads the relay", width=2.6,
         label="ws://…/ws/relay", label_at=(1000, 128), anchor="middle"),

    # inside the robot node
    Edge("e-shim-arb", ((890, 255), (910, 255)), "node", "shim requests enter the arbiter",
         width=2.6),
    Edge("e-actions-arb", ((1210, 255), (1190, 255)), "node",
         "action and service requests enter the arbiter", width=2.6),
    Edge("e-arb-backend", ((1000, 320), (1000, 345)), "node",
         "the arbiter drives Pollen's backend, one move at a time", width=2.8),
    Edge("e-backend-hw", ((1040, 440), (1050, 440), (1050, 830)), "driver",
         "the control loop talks to the servos", width=2.8,
         label="serial, 50 Hz", label_at=(1058, 760)),
    Edge("e-backend-pub", ((1050, 565), (1060, 565)), "driver",
         "the backend's state feeds the publishers", width=2.6),
    Edge("e-hw-percep", ((830, 830), (830, 630)), "hw",
         "camera and audio devices feed the perception streams", width=2.6,
         label="/dev/video41 · PipeWire", label_at=(838, 760)),
    Edge("e-pub-tools", ((1470, 565), (1480, 565), (1480, 796), (1700, 796), (1700, 830)),
         "out", "an outside tool subscribes to standard messages", width=2.8,
         label="standard messages", label_at=(1590, 789), anchor="middle"),

    # the EMORI graph
    Edge("e-adapter-stem", ((1790, 245), (1800, 245)), "ros", "publishes", arrow=False,
         width=2.4),
    Edge("e-adapter-spine", ((1800, 216), (1800, 464)), "ros", "the published topics",
         arrow=False, width=2.4),
    Edge("e-pub1", ((1800, 216), (1810, 216)), "ros", "tracked", width=2.2),
    Edge("e-pub2", ((1800, 278), (1810, 278)), "ros", "anonymous", width=2.2),
    Edge("e-pub3", ((1800, 340), (1810, 340)), "ros", "engagement_status", width=2.2),
    Edge("e-pub4", ((1800, 402), (1810, 402)), "ros", "context", width=2.2),
    Edge("e-pub5", ((1800, 464), (1810, 464)), "ros", "speech_cue", width=2.2),
    Edge("e-ctx-bridge", ((1800, 380), (1790, 380)), "ros", "context reaches the bridge",
         width=2.2),
    Edge("e-consumer-spine", ((2070, 245), (2070, 464)), "ros", "the topics the nodes read",
         arrow=False, width=2.4),
    Edge("e-eng-in", ((2060, 340), (2070, 340)), "ros", "engagement_status", arrow=False,
         width=2.2),
    Edge("e-ctx-in", ((2060, 402), (2070, 402)), "ros", "context", arrow=False, width=2.2),
    Edge("e-cue-in", ((2060, 464), (2070, 464)), "ros", "speech_cue", arrow=False, width=2.2),
    Edge("e-to-analyzer", ((2070, 245), (2080, 245)), "ros", "context reaches the analyzer",
         width=2.2),
    Edge("e-to-behav", ((2070, 405), (2080, 405)), "ros",
         "the signals reach the engagement node", width=2.2),
    Edge("e-behav-skill", ((2205, 480), (2205, 510)), "ros", "bounded skill requests",
         width=2.6),
    Edge("e-skill-facade", ((2080, 555), (1790, 555)), "ros", "served by the facade",
         width=2.6, label="bounded skill requests", label_at=(1935, 548), anchor="middle"),
    Edge("e-facade-native", ((1670, 586), (1670, 606)), "ros",
         "the selected transport", width=2.6),
    Edge("e-native-actions", ((1550, 700), (1500, 700), (1500, 255), (1470, 255)), "ros",
         "skills become action goals on the robot node", width=3.0,
         label="GoTo goals", label_at=(1495, 694), anchor="end"),

    # face look-at, inside the robot node
    Edge("e-loop-actions", ((1990, 830), (1990, 760), (1515, 760), (1515, 300), (1470, 300)),
         "node", "face look-at streams head targets to the arbiter",
         width=2.4, label="set_target (in-process)", label_at=(1750, 753), anchor="middle"),

    # analytics
    Edge("e-fclient-analytics", ((450, 586), (450, 830)), "host",
         "the hostapp historises to the off-robot broker", width=2.6,
         label="NGSI-LD", label_at=(458, 720)),
)


def style_block() -> str:
    markers = "\n".join(
        f'    <marker id="a-{t}" markerWidth="9" markerHeight="9" refX="7.4" refY="4.5"'
        f' orient="auto" markerUnits="strokeWidth"><path d="M 0 0 L 9 4.5 L 0 9 z"'
        f' fill="{c[0]}"/></marker>'
        for t, c in TONES.items()
    )
    css = "\n".join(
        f"      .box-{t} {{ fill: {c[1]}; stroke: {c[0]}; }}\n"
        f"      .badge-{t} {{ fill: {c[0]}; }}\n"
        f"      .edge-{t} {{ stroke: {c[0]}; }}\n"
        f"      .w-{t} {{ fill: {c[0]}; }}"
        for t, c in TONES.items()
    )
    return f"""  <defs>
{markers}
    <style>
      text {{ font-family: Inter, "Segoe UI", Helvetica, Arial, sans-serif; fill: #16243a; }}
      .wire, .tag {{ font-family: "JetBrains Mono", Consolas, monospace; }}
      .canvas {{ fill: #ffffff; }}
      .kicker {{ font-size: 13px; font-weight: 800; letter-spacing: .15em; fill: #1d4ed8; }}
      .title {{ font-size: 31px; font-weight: 800; letter-spacing: -.02em; fill: #0f172a; }}
      .subtitle {{ font-size: 15px; font-weight: 500; fill: #52637a; }}
      .group {{ fill: #fbfaff; stroke: #c7d2fe; stroke-width: 1.6; stroke-dasharray: 8 6; }}
      .group-label {{ font-size: 13px; font-weight: 800; letter-spacing: .06em; fill: #1d4ed8; }}
      .box {{ stroke-width: 1.8; }}
      .box-dashed {{ stroke-dasharray: 6 5; }}
      .badge {{ font-size: 10px; font-weight: 850; letter-spacing: .1em; }}
      .tag {{ font-size: 10px; font-weight: 700; fill: #7c8896; letter-spacing: .06em; }}
      .b-title {{ font-size: 16px; font-weight: 800; fill: #111d2e; }}
      .b-body {{ font-size: 13px; font-weight: 500; fill: #35485d; }}
      .edge {{ fill: none; stroke-linecap: round; stroke-linejoin: round; }}
      .dashed {{ stroke-dasharray: 7 6; }}
      .wire {{ font-size: 12px; font-weight: 700; paint-order: stroke; stroke: #ffffff;
               stroke-width: 4.5px; stroke-linejoin: round; }}
      .legend {{ font-size: 12.5px; font-weight: 700; fill: #52637a; }}
{css}
    </style>
  </defs>"""


def header() -> str:
    """The legend only: no kicker, no title, no subtitle. The boxes say what they are."""

    legend = []
    lx = 40
    for label, tone in LEGEND:
        c = TONES[tone]
        legend.append(
            f'    <rect x="{lx}" y="40" width="15" height="15" rx="3" fill="{c[1]}"'
            f' stroke="{c[0]}" stroke-width="1.7"/>'
            f'<text x="{lx + 21}" y="53" class="legend">{esc(label)}</text>'
        )
        lx += 34 + int(len(label) * 7.6)
    return "\n".join((
        f'  <rect x="0" y="0" width="{W}" height="{H}" class="canvas"/>',
        '  <g id="legend">',
        "\n".join(legend),
        "  </g>",
    ))


def render_svg() -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"'
        f' viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">',
        '  <title id="t">EMORI architecture: the robot as a native ROS 2 component</title>',
        '  <desc id="d">The hostapp chain on the left reaches the robot through a REST shim '
        "inside the robot node. The robot node owns Pollen's backend in-process: a motion "
        'arbiter with three lanes, actions and services, state publishers, perception '
        'streams and a lifecycle that parks the robot on shutdown. The EMORI ROS4HRI graph '
        'runs in the same container and sends skills to the node natively as action goals. '
        'Below: the hardware, an off-robot analytics consumer, any ROS 2 tool reading '
        'standard messages, and the in-process face look-at component.</desc>',
        style_block(),
        header(),
        '  <g id="diagram" transform="translate(0,-40)">',
    ]
    parts.extend(group_svg(g) for g in GROUPS)
    parts.extend(edge_svg(e) for e in EDGES)
    parts.extend(box_svg(b) for b in BOXES)
    parts.append("  </g>")
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> None:
    SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SVG_PATH.write_text(render_svg(), encoding="utf-8", newline="\n")
    print(SVG_PATH)


if __name__ == "__main__":
    main()
