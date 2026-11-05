"""The four graph nodes that run in the hub's process when ``EMORI_HUB_GRAPH=1``.

With that setting the motor container runs the robot node alone and these four come here,
on the ``ros-graph`` executor, classes as they are. Each builder takes the app and returns
the node. The fifth member of the old launch file, the FIWARE bridge, stays off: the session
code is the one writer to the broker.

**What this moves and what it must not.** Their callbacks run on ``ros-graph``, and the
adapter node is fed on the ``ros-fanout`` thread, so neither shares a thread, a Python lock or
an executor with the path a session's motion takes. That path reaches them only through
:meth:`FanOut.offer`, one queue put, and a node that is slow costs topics, counted in
:meth:`FanOut.stats`. What they do share is the interpreter's lock: an analysis turns into a
burst of Python and of publishes that hold it, in the process that plans the next sentence's
moves, where in the motor container it had processes of its own. That cost is measured with the
graph on and off on the robot, and this setting stays off unless the two agree.

**Who moves the robot.** ``skill_facade`` reads ``EMORI_ROS_MOTION_OWNER``, and on anything
but ``ros`` it counts each request and sends nothing, as it did in the motor container. So
hosting the graph here adds no second source of motion. No parameter here sets that.

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from emori_hub.fanout import build_fanout

__all__ = [
    "GRAPH_BUILDERS",
    "HUB_SOURCE",
    "build_adapter_node",
    "build_behavior_node",
    "build_context_analyzer",
    "build_skill_facade",
]

log = logging.getLogger("emori.hub.graph")

#: The adapter node's ``source`` parameter here, from ``config/graph.yaml``.
HUB_SOURCE = "hub"


def _no_subscriber(_event: Any) -> None:
    """Where session events go when no session node is there to publish them."""


def build_adapter_node(
    app: Any,
    *,
    node_factory: Optional[Callable[[], Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Any:
    """``/emori_adapter``, fed by the app's fan-out, starting one if nothing has.

    Anything but the ``hub`` source stops the start. The node's default is its demo arc, and
    made-up person data on the real topics is worse than no topics.
    """

    if node_factory is None:
        from emori_adapter_ros.publisher_node import EmoriAdapterNode as node_factory
    node = node_factory()
    if node.source_kind != HUB_SOURCE:
        node.destroy_node()
        raise RuntimeError(f"emori_adapter came up with source {node.source_kind!r}, not "
                           f"{HUB_SOURCE!r}: the hub's parameter file did not reach it")

    fanout = getattr(app.state, "fanout", None)
    ours = fanout is None
    if ours:
        fanout = build_fanout(app, publish=_no_subscriber, environ=environ)
    fanout.attach_feed(node.feed)

    destroy = node.destroy_node

    def destroy_node() -> Any:
        try:
            fanout.attach_feed(None)
            if ours:
                fanout.stop()
                fanout.uninstall()
        except Exception:  # noqa: BLE001 - the node must still be released
            log.warning("the adapter node's feed did not stop cleanly", exc_info=True)
        return destroy()

    node.destroy_node = destroy_node
    return node


def build_context_analyzer(_app: Any) -> Any:
    """``/hri_context_analyzer``."""

    from hri_context_analyzer.context_analyzer_node import ContextAnalyzerNode

    return ContextAnalyzerNode()


def build_behavior_node(_app: Any) -> Any:
    """``/hri_engagement_behaviors``. Computes and publishes; the facade decides what happens."""

    from hri_engagement_behaviors.behavior_node import EngagementBehaviorNode

    return EngagementBehaviorNode()


def build_skill_facade(_app: Any) -> Any:
    """``/skill_facade``, the only node of the four that could move the robot."""

    from emori_skill_facade.facade_node import SkillFacadeNode

    return SkillFacadeNode()


GRAPH_BUILDERS: tuple[Callable[[Any], Any], ...] = (
    build_adapter_node,
    build_context_analyzer,
    build_behavior_node,
    build_skill_facade,
)
