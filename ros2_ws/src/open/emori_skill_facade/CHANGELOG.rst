^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package emori_skill_facade
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

0.1.0 (2026-09-16)
------------------
* First release. The one component allowed to turn a decision into motion, published
  on its own so it can be replaced.
* Serves ``/skill/replay_motion`` and ``/skill/look_at`` (actions) and
  ``/skill/set_expression`` (topic), and hands each request to a transport.
* Transport plug point (``transport.py``): ``hostapp_http`` posts to the host
  application's single-writer ``/api/skill`` intake with an ``X-Emori-Skill-Key`` header;
  ``log`` accepts and records everything without a network, a key or a robot. Another
  transport is one more class here plus one launch argument.
* An unknown transport name is refused with a ``ValueError`` listing the known names; the
  node logs an ERROR and falls back to ``log`` rather than to a live wire.
* The ``EMORI_ROS_MOTION_OWNER`` gate lives in the node, so it holds for every transport
  ever plugged in. Unless the owner is exactly ``ros``, every request is suppressed and
  counted.
* A ``/diagnostics`` ledger (status name ``emori_skill_facade``) every 5 s: the six
  counters plus owner, transport and URL host, with the severity an operator would want —
  ERROR for an owner that may post and no key, WARN when ``unreachable`` grows.
* ``contract.py`` is pure stdlib with no ``urllib`` and no ROS, so the decision half can
  be tested on a machine with neither.
