^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package hri_engagement_behaviors
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

0.1.0 (2026-09-16)
------------------
* Initial release.
* Maps ROS4HRI engagement, EMORI context and sentence-level
  speech cues to bounded skill requests. It holds no robot client and no daemon URL.
* At most **one** request per event: a cycle sends an expression, a replay or a gaze,
  never two of them, and a recovery sends one.
* A cycle inside the speaking window publishes nothing at all, expression included, so a
  yielded cycle never spends a queue slot.
* ``set_expression`` is treated as state: requested only on a label change, and only in
  place of a low-value motion. No per-sentence expression: it would be a second bounded plan
  competing with the sentence's own motion for the same head.
* De-escalation preempts everything: one ``calm_settle`` at a reduced intensity, the
  speaking window cleared so safety is not queued behind speech.
* ``policy.py``: the whole decision as pure stdlib Python with an injected clock.
* ``EMORI_BEHAVIOR_CONFIG``: a missing or unreadable path is an
  ERROR naming it, and the effective configuration is logged in one line at start-up.
  Tuning keys include ``cue_max_s`` and ``deescalated_k``.
* A ``/diagnostics`` ledger (``diagnostic_msgs/DiagnosticArray``, status name
  ``hri_engagement_behaviors``) every 5 s, and the same numbers in the log every 30 s.
* Motion requests are served by the separate ``emori_skill_facade`` package.
* No ``pyhri`` ``exec_depend``: apt has no ``ros-humble-pyhri``, so declaring it
  would make ``rosdep install`` fail. The demo node imports it
  lazily and is the only thing that needs it.
