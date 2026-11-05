"""Render the one-page EMORI architecture figure.

This draws the system as it is implemented: the Pokamind session that supplies the
analysis feed, the hostapp that stamps and fans out every envelope, the five-node
ROS 2 / ROS4HRI graph, the bounded skill lane, the FIWARE chain, and the Reachy
daemon.

Every topic path, message type, route and port on the canvas is copied from the source
it labels. Keep it that way.

Output:

    docs/architecture/emori-system-architecture.svg

Render a PNG with headless Chromium:

    chrome --headless --disable-gpu --force-device-scale-factor=2 \\
           --screenshot=out.png --window-size=2400,1200 \\
           --default-background-color=ffffffff file:///<abs path>/<the svg>
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "docs" / "architecture" / "emori-system-architecture.svg"

W, H = 2400, 1200

TONES = {
    "poka": ("#2563eb", "#eff6ff", "#bfdbfe"),    # the Pokamind session
    "host": ("#b45309", "#fffbeb", "#fde68a"),    # emori-hostapp
    "ros": ("#7c3aed", "#f5f3ff", "#ddd6fe"),     # a ROS 2 node
    "std": ("#0f766e", "#f0fdfa", "#99f6e4"),     # a standard REP-155 topic
    "ext": ("#be123c", "#fff1f2", "#fecdd3"),     # an EMORI message extension
    "fiware": ("#047857", "#ecfdf5", "#a7f3d0"),
    "robot": ("#475569", "#f1f5f9", "#cbd5e1"),
}


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
    mono: tuple[str, ...] = ()
    badge: str = ""
    foot: str = ""
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
        ty += 21
    out.append(f'    <text x="{b.x + 16}" y="{ty}" class="b-title">{esc(b.title)}</text>')
    cy = ty + 23
    if b.lines:
        out.append(_text_lines(b.lines, b.x + 16, cy, "b-body", 17))
        cy += 17 * len(b.lines) + 4
    if b.mono:
        out.append(_text_lines(b.mono, b.x + 16, cy, "b-mono", 16))
    if b.foot:
        out.append(
            f'    <text x="{b.x + 16}" y="{b.y + b.h - 12}" class="b-foot">{esc(b.foot)}</text>'
        )
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


# ---------------------------------------------------------------- the figure

GROUP = (1100, 268, 1240, 500)   # the ROS 2 container

BOXES = (
    # the session that supplies the feed
    Box("b-poka", 60, 150, 250, 105, "poka", "Pokamind session",
        lines=("one analysis cycle about every",
               "2 seconds, plus one event per",
               "sentence the robot speaks")),
    Box("b-kiosk", 360, 150, 200, 105, "poka", "kiosk page",
        lines=("Chromium on the robot,",
               "no screen")),
    Box("b-hub", 610, 150, 280, 105, "host", "emori-hostapp",
        lines=("POST /api/cycle stamps the",
               "session identity, then fans",
               "the envelope out")),

    # what the hub feeds
    Box("b-driver", 60, 310, 250, 105, "host", "Translator + BehaviorEngine",
        lines=("engagement, affect and context,",
               "then motion intents. It also",
               "writes the local SQLite store.")),
    Box("b-record", 360, 310, 200, 105, "host", "recorder",
        lines=("one JSONL file per session,",
               "for deterministic replay")),
    Box("b-relay", 860, 310, 200, 105, "host", "/ws/relay",
        lines=("the broadcast the ROS",
               "graph listens to")),

    # the motion path
    Box("b-player", 60, 470, 250, 105, "host", "RobotPlayer",
        lines=("the only queue that",
               "moves the robot")),
    Box("b-skill", 360, 470, 200, 105, "host", "POST /api/skill",
        lines=("how the ROS graph",
               "gets in"),
        foot="off unless a flag and key are set"),
    Box("b-fclient", 610, 470, 200, 105, "host", "FiwareClient",
        lines=("writes NGSI-LD,",
               "one row per cycle")),
    Box("b-daemon", 60, 630, 250, 105, "robot", "reachy-mini-daemon",
        lines=("REST on :8000, started",
               "with --no-media")),
    Box("b-fiware", 610, 630, 700, 105, "fiware", "FIWARE",
        mono=("Orion-LD :1026   →   QuantumLeap :8668   →   TimescaleDB :5432   →   Grafana :3003",),
        foot="a broker is optional off the robot, so the SQLite store keeps the same numbers locally"),
    Box("b-robot", 60, 790, 250, 105, "robot", "Reachy Mini",
        lines=("6-DOF head, body yaw,",
               "two antennas, speaker")),

    # the ROS 2 graph
    Box("n-adapter", 1120, 340, 240, 120, "ros", "emori_adapter",
        lines=("runs the same Translator",
               "the hostapp runs, then",
               "publishes the person topics")),

    Box("t-tracked", 1410, 296, 290, 64, "std", "/humans/persons/tracked",
        mono=("hri_msgs/IdsList",)),
    Box("t-anon", 1410, 366, 290, 64, "std", ".../<id>/anonymous",
        mono=("std_msgs/Bool",)),
    Box("t-eng", 1410, 436, 290, 64, "std", ".../<id>/engagement_status",
        mono=("hri_msgs/EngagementLevel",)),
    Box("t-ctx", 1410, 506, 290, 64, "ext", ".../<id>/context",
        mono=("emori_msgs/Context",)),
    Box("t-cue", 1410, 576, 290, 64, "ext", ".../<id>/speech_cue",
        mono=("emori_msgs/SpeechCue",)),

    Box("n-analyzer", 1750, 300, 240, 100, "ros", "hri_context_analyzer",
        lines=("splits Context into one",
               "topic per channel")),
    Box("n-behav", 1750, 440, 240, 120, "ros", "hri_engagement_behaviors",
        lines=("decides the behaviour and",
               "asks for it by name. It holds",
               "no daemon URL.")),
    Box("n-bridge", 1750, 620, 240, 100, "ros", "fiware_bridge",
        lines=("historizes every cycle.",
               "Off on a live robot."),
        dashed=True),

    Box("d-fanout", 2060, 300, 260, 100, "ext", ".../<id>/context/*",
        lines=("20 single-purpose topics,",
               "plus the standard",
               "hri_msgs/Expression")),
    Box("d-skills", 2060, 440, 260, 120, "ext", "the skill lane",
        mono=("/skill/set_expression  topic",
              "/skill/replay_motion  action",
              "/skill/look_at        action")),
    Box("n-facade", 2060, 600, 260, 100, "ros", "skill_facade",
        lines=("the only node that runs a",
               "request, and it goes through",
               "the hostapp to do it")),
)

EDGES = (
    Edge("e-poka-kiosk", ((310, 202), (360, 202)), "poka", "the session posts to its host page",
         label="analysis + speech", label_at=(335, 142), anchor="middle"),
    Edge("e-kiosk-hub", ((560, 202), (610, 202)), "poka", "the page relays every envelope",
         label="POST /api/cycle", label_at=(585, 142), anchor="middle"),

    Edge("e-fan-stem", ((750, 255), (750, 282)), "host", "the fan-out", arrow=False, width=2.4),
    Edge("e-fan-spine", ((185, 282), (960, 282)), "host", "three consumers", arrow=False, width=2.4),
    Edge("e-fan-driver", ((185, 282), (185, 310)), "host", "the in-process driver", width=2.4),
    Edge("e-fan-record", ((460, 282), (460, 310)), "host", "the recorder and the local store", width=2.4),
    Edge("e-fan-relay", ((960, 282), (960, 310)), "host", "the relay broadcast", width=2.4),

    Edge("e-driver-player", ((185, 415), (185, 470)), "host", "motion intents reach the queue",
         label="MotionIntent", label_at=(199, 448)),
    Edge("e-driver-fiware", ((260, 415), (260, 442), (710, 442), (710, 470)), "host",
         "the driver is what writes to the broker", width=2.4),
    Edge("e-skill-player", ((360, 522), (310, 522)), "host",
         "a skill request joins the same queue",
         label="same queue", label_at=(335, 462), anchor="middle"),
    Edge("e-player-daemon", ((185, 575), (185, 630)), "host", "the queue paces the daemon",
         label="POST /api/move/*", label_at=(199, 608)),
    Edge("e-daemon-robot", ((185, 735), (185, 790)), "robot", "the daemon drives the hardware"),
    Edge("e-fclient-fiware", ((710, 575), (710, 630)), "host", "one row per cycle",
         label="NGSI-LD", label_at=(724, 608)),

    Edge("e-relay-adapter", ((1060, 362), (1090, 362), (1090, 400), (1120, 400)), "host",
         "the ROS graph reads the relay and never drives from it", width=3.0),

    Edge("e-pub-stem", ((1360, 400), (1385, 400)), "ros", "publishes", arrow=False, width=2.4),
    Edge("e-pub-spine", ((1385, 328), (1385, 608)), "ros", "the published topics", arrow=False, width=2.4),
    Edge("e-pub1", ((1385, 328), (1410, 328)), "ros", "tracked", width=2.2),
    Edge("e-pub2", ((1385, 398), (1410, 398)), "ros", "anonymous", width=2.2),
    Edge("e-pub3", ((1385, 468), (1410, 468)), "ros", "engagement_status", width=2.2),
    Edge("e-pub4", ((1385, 538), (1410, 538)), "ros", "context", width=2.2),
    Edge("e-pub5", ((1385, 608), (1410, 608)), "ros", "speech_cue", width=2.2),

    Edge("e-ctx-analyzer", ((1700, 530), (1710, 530), (1710, 350), (1750, 350)), "ext",
         "context reaches the analyzer", width=2.6),
    Edge("e-eng-behav", ((1700, 468), (1730, 468), (1730, 478), (1750, 478)), "std",
         "engagement_status reaches the engagement node", width=2.6),
    Edge("e-ctx-behav", ((1700, 538), (1740, 538), (1740, 505), (1750, 505)), "ext",
         "context reaches the engagement node", width=2.6),
    Edge("e-cue-behav", ((1700, 608), (1745, 608), (1745, 532), (1750, 532)), "ext",
         "speech_cue reaches the engagement node", width=2.6),
    Edge("e-ctx-bridge", ((1700, 546), (1720, 546), (1720, 670), (1750, 670)), "ext",
         "context reaches the bridge", width=2.6),

    Edge("e-analyzer-out", ((1990, 350), (2060, 350)), "ros", "one topic per channel", width=2.6),
    Edge("e-behav-out", ((1990, 500), (2060, 500)), "ros",
         "the node names a motion, it never writes one", width=2.8),
    Edge("e-skills-facade", ((2190, 560), (2190, 600)), "ext", "served by the facade", width=2.6),

    Edge("e-bridge-fiware", ((1870, 720), (1870, 830), (960, 830), (960, 735)), "ros",
         "the bridge writes the same schema, and is off on a live robot",
         dashed=True, width=2.4,
         label="NGSI-LD", label_at=(1420, 822), anchor="middle"),
    Edge("e-facade-skill", ((2190, 700), (2190, 790), (460, 790), (460, 575)), "ros",
         "the only way the graph reaches the robot", width=3.2,
         label="POST /api/skill", label_at=(1330, 782), anchor="middle"),
)

CAPTION = (
    ("Figure 1. The EMORI adapter architecture. Blue: the Pokamind session that supplies the analysis feed, and the kiosk page that hosts it. Amber: the emori-hostapp,",
     "which stamps the session identity onto every envelope, fans it out to three consumers, and owns the single queue that moves the robot. Violet: the ROS 2 graph,",
     "five nodes in one container on a local DDS domain. Teal: standard REP-155 topics. Rose: the two EMORI message extensions and the bounded skill lane. Green: the",
     "FIWARE analytics chain. Grey: the Reachy daemon and the robot itself."),
    ("No node in the ROS graph holds a daemon URL. The graph works out what the robot should do, and skill_facade asks for it over HTTP, so one queue keeps its ordering",
     "and its interlocks. The ROS person namespace is the stable person_id parameter, because the robot serves one learner at a time; the real per-session identity travels",
     "inside emori_msgs/Context. A sixth executable, pyhri_demo, is built but not launched: it reads the same topics through hri.HRIListener to show that standard ROS4HRI",
     "tooling picks the graph up unchanged."),
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
      .b-mono, .wire {{ font-family: "JetBrains Mono", Consolas, monospace; }}
      .canvas {{ fill: #ffffff; }}
      .kicker {{ font-size: 13px; font-weight: 800; letter-spacing: .15em; fill: #b45309; }}
      .title {{ font-size: 32px; font-weight: 800; letter-spacing: -.02em; fill: #0f172a; }}
      .subtitle {{ font-size: 15px; font-weight: 500; fill: #52637a; }}
      .group {{ fill: #fbfaff; stroke: #ddd6fe; stroke-width: 1.6; stroke-dasharray: 8 6; }}
      .group-label {{ font-size: 13px; font-weight: 800; letter-spacing: .06em; fill: #7c3aed; }}
      .box {{ stroke-width: 1.8; }}
      .box-dashed {{ stroke-dasharray: 6 5; }}
      .badge {{ font-size: 10px; font-weight: 850; letter-spacing: .1em; }}
      .b-title {{ font-size: 16.5px; font-weight: 800; fill: #111d2e; }}
      .b-body {{ font-size: 13px; font-weight: 500; fill: #35485d; }}
      .b-mono {{ font-size: 12px; font-weight: 500; fill: #1f3348; }}
      .b-foot {{ font-size: 11px; font-weight: 600; fill: #6b7c8e; }}
      .edge {{ fill: none; stroke-linecap: round; stroke-linejoin: round; }}
      .dashed {{ stroke-dasharray: 7 6; }}
      .wire {{ font-size: 12px; font-weight: 700; paint-order: stroke; stroke: #ffffff;
               stroke-width: 4.5px; stroke-linejoin: round; }}
      .legend {{ font-size: 12px; font-weight: 700; fill: #52637a; }}
      .cap {{ font-size: 14px; font-weight: 500; fill: #35485d; }}
      .foot {{ font-size: 11px; font-weight: 500; fill: #94a3b8; }}
{css}
    </style>
  </defs>"""


def header() -> str:
    legend = []
    lx = 1230
    for label, tone in (("Pokamind", "poka"), ("hostapp", "host"), ("ROS 2 node", "ros"),
                        ("REP-155 topic", "std"), ("EMORI message", "ext"),
                        ("FIWARE", "fiware"), ("robot", "robot")):
        c = TONES[tone]
        legend.append(
            f'    <rect x="{lx}" y="40" width="15" height="15" rx="3" fill="{c[1]}"'
            f' stroke="{c[0]}" stroke-width="1.7"/>'
            f'<text x="{lx + 21}" y="53" class="legend">{esc(label)}</text>'
        )
        lx += 32 + int(len(label) * 7.4)
    return "\n".join((
        f'  <rect x="0" y="0" width="{W}" height="{H}" class="canvas"/>',
        '  <g id="header">',
        '    <text x="40" y="44" class="kicker">EMORI ADAPTER · REACHY MINI</text>',
        '    <text x="40" y="82" class="title">From a Pokamind session to ROS4HRI signals, robot behaviour and FIWARE analytics</text>',
        '    <text x="40" y="106" class="subtitle">The ROS 2 graph works out what the robot should do. The hostapp is the only component that moves it.</text>',
        "\n".join(legend),
        "  </g>",
    ))


def group_svg() -> str:
    x, y, w, h = GROUP
    return "\n".join((
        '  <g id="ros-group">',
        "    <title>the ROS 2 container</title>",
        f'    <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" class="group"/>',
        f'    <text x="{x + 20}" y="{y + 26}" class="group-label">'
        'ROS 2 HUMBLE  ·  ONE CONTAINER, FIVE NODES, ONE LOCAL DDS DOMAIN</text>',
        "  </g>",
    ))


def caption_svg() -> str:
    out = ['  <g id="caption">',
           f'    <rect x="36" y="930" width="{W - 72}" height="212" rx="12"'
           ' fill="#fcfcfd" stroke="#e8edf3" stroke-width="1.4"/>']
    y = 966
    for para in CAPTION:
        out.append(_text_lines(para, 60, y, "cap", 20))
        y += 20 * len(para) + 14
    out.append("  </g>")
    return "\n".join(out)


def render_svg() -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"'
        f' viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">',
        '  <title id="t">The EMORI adapter architecture: the Pokamind session feed, the '
        'hostapp hub, the five-node ROS 2 / ROS4HRI graph, the FIWARE chain and the robot</title>',
        '  <desc id="d">A Pokamind session emits an analysis cycle about every two seconds '
        'and one event per spoken sentence. The kiosk page relays each envelope to the '
        'hostapp, which stamps the session identity and fans it out to the in-process '
        'driver, the recorder and the /ws/relay broadcast. The emori_adapter node reads '
        'that broadcast and publishes three standard REP-155 person topics plus two EMORI '
        'extensions, Context and SpeechCue. A context analyzer splits Context into '
        'single-purpose topics, an engagement node names a bounded motion on the /skill '
        'lane, and a FIWARE bridge historizes to NGSI-LD. Only skill_facade runs a request, '
        'and it does so by posting to the hostapp, which owns the single robot queue.</desc>',
        style_block(),
        header(),
        group_svg(),
    ]
    parts.extend(edge_svg(e) for e in EDGES)
    parts.extend(box_svg(b) for b in BOXES)
    parts.append(caption_svg())
    parts.append(
        f'  <text x="40" y="{H - 14}" class="foot">Generated by '
        "scripts/build_emori_system_graph.py from ros2_ws/src/**, src/emori_adapter/** "
        "and deploy/**.</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_svg(path: Path = SVG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_svg(), encoding="utf-8", newline="\n")
    return path


if __name__ == "__main__":
    print(write_svg())
