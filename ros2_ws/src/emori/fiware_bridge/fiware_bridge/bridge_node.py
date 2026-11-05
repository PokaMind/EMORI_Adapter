"""ROS 2 -> FIWARE NGSI-LD bridge.

Subscribes to the full ``emori_msgs/Context`` topic and historizes every cycle to
Orion-LD using the CANONICAL schema (:mod:`emori_adapter.fiware_schema`) via the
shared :class:`~emori_adapter.fiware.FiwareClient` — the bridge and the Python
hostapp writer can no longer diverge.

Entity identity comes from the MESSAGE (``Context.session_id`` / ``person_id``,
stamped by the hostapp on the live path), not from a static launch parameter: a
new session id rolls the bridge over to fresh per-session entities, so consecutive
sessions never overwrite each other.

The ``enabled`` parameter (env ``FIWARE_BRIDGE_ENABLED``) exists because on the
LIVE stack the hostapp already historizes every cycle — running both writers would
double every time-series row. The standalone mock graph keeps it on.
"""

from __future__ import annotations

import os

import rclpy
from rclpy.node import Node

from emori_msgs.msg import Context

from emori_adapter import fiware_schema as schema
from emori_adapter.fiware import FiwareClient


class FiwareBridgeNode(Node):
    def __init__(self) -> None:
        super().__init__("fiware_bridge")
        self.declare_parameter("person_id", "learner_01")  # ROS topic namespace only
        self.declare_parameter("orion_url", os.environ.get("ORION_URL", "http://host.docker.internal:1026"))
        self.declare_parameter("tenant", os.environ.get("NGSILD_TENANT", "pokamind"))
        self.declare_parameter("ql_notify_url", os.environ.get("QL_NOTIFY_URL", "http://quantumleap:8668/v2/notify"))
        self.declare_parameter("enabled", os.environ.get("FIWARE_BRIDGE_ENABLED", "1") != "0")

        self.person_id = self.get_parameter("person_id").value
        self.orion = str(self.get_parameter("orion_url").value).rstrip("/")
        self.tenant = self.get_parameter("tenant").value
        self.ql_notify = self.get_parameter("ql_notify_url").value
        self.enabled = bool(self.get_parameter("enabled").value)

        self._client: FiwareClient | None = None
        self._session_id = ""

        base = f"/humans/persons/{self.person_id}"
        self.create_subscription(Context, f"{base}/context", self._on_context, 10)
        if self.enabled:
            self.get_logger().info(f"FIWARE bridge active: Orion={self.orion} (entities keyed off Context.session_id)")
        else:
            self.get_logger().info("FIWARE bridge DISABLED (the hostapp is the live writer)")

    def _on_context(self, msg: Context) -> None:
        if not self.enabled:
            return
        if msg.session_id != self._session_id or self._client is None:
            # New session -> fresh per-session entities (and subscription idempotency
            # is handled inside FiwareClient: it lists before creating).
            if self._client is not None:
                self._client.close()
            self._session_id = msg.session_id
            self._client = FiwareClient(
                orion_url=self.orion, tenant=self.tenant,
                session_id=msg.session_id or "sim-session",
                person_id=msg.person_id or self.person_id,
                ql_notify_url=self.ql_notify,
            )
            self._client.ensure_session(topic=msg.topic)
            self.get_logger().info(f"historizing session {msg.session_id} (person {msg.person_id})")
        self._client.push_attrs({
            "engagementLevel": int(msg.engagement_level),
            "engagementScore": float(msg.engagement_score),
            "valence": float(msg.valence),
            "arousal": float(msg.arousal),
            "escalation": float(msg.escalation),
            "rapport": float(msg.rapport),
            "attention": float(msg.attention),
            "engagementTrend": float(msg.engagement_trend),
            "dominantStyle": msg.dominant_style or "neutral",
        })

    def destroy_node(self) -> bool:
        try:
            if self._client is not None:
                self._client.close()
        finally:
            return super().destroy_node()


def main() -> None:
    rclpy.init()
    node = FiwareBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
