"""The ``ros_native`` skill transport, registered the moment this package is imported.

The entry point in ``setup.py`` is the route the facade normally takes: it resolves
``EMORI_SKILL_TRANSPORT=ros_native`` with no import of this package at all.  This call
is the other half of the same promise, for the case where the entry point is not there
to be read - a colcon overlay built from a tree that was then moved, an editable install
whose metadata went stale, a developer who imported the module in a console.  Either
route alone is enough; having both means "the transport is installed" and "the transport
is selectable" cannot drift apart while a robot stands in front of somebody.

Registration is deliberately import-time and deliberately cheap: it hands over a
factory, so nothing here touches ``rclpy``, builds a client or reads the environment
until the facade actually asks for this transport by name.
"""

from emori_skill_facade.transport import register_transport

from emori_skill_native.native import NativeTransport, make_transport

register_transport(NativeTransport.name, make_transport)

__all__ = ["NativeTransport", "make_transport"]
