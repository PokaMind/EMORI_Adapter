"""Optional demo: consume engagement via the official pyhri ``HRIListener``.

This shows the *preferred* ROS4HRI consumption API (the ``hri`` module from the
libhri/pyhri package). It is intentionally NOT on the behaviour critical path
(``behavior_node`` uses plain rclpy subscriptions so the PoC works even if pyhri
isn't installed). Run it to demonstrate that the EMORI adapter's published person
+ engagement_status are picked up by standard ROS4HRI tooling::

    ros2 run hri_engagement_behaviors pyhri_demo

Requires ``ros-humble-pyhri`` / ``ros-humble-libhri``.
"""

from __future__ import annotations

import rclpy


def main() -> None:
    try:
        from hri import HRIListener
    except ImportError:
        print("pyhri (the 'hri' module) is not installed - skipping pyhri demo. "
              "Install ros-humble-pyhri / ros-humble-libhri to enable it.")
        return

    rclpy.init()
    listener = HRIListener("emori_pyhri_listener")  # spawns its own internal node
    node = rclpy.create_node("emori_pyhri_demo")

    def tick() -> None:
        persons = getattr(listener, "tracked_persons", {}) or {}
        if not persons:
            node.get_logger().info("no tracked persons yet")
            return
        for pid, person in persons.items():
            eng = getattr(person, "engagement_status", None)
            node.get_logger().info(f"[pyhri] person={pid} engagement_status={eng}")

    node.create_timer(1.0, tick)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
