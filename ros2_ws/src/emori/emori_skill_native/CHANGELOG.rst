^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package emori_skill_native
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

0.1.0 (2026-09-16)
------------------
* First release, for v2's native robot node. Adds the ``ros_native`` transport: a bounded
  skill request becomes ``GoTo`` / ``PlayRecordedMove`` goals, a ``BodyTarget`` message
  and a ``Trigger`` call on ``reachy_mini_ros2``, with no HTTP hop and no daemon.
* Reuses the host application's own ``skill_to_intents`` mapping (the adapter core), so
  the gesture and the bounds errors are identical whichever side asked for the skill.
  This is why the package imports the core and lives under ``ros2_ws/src/emori/``.
* Pure mapping half (``intents_to_requests``, ``first_result_outcome``,
  ``rpy_to_quaternion``): millimetres and degrees become metres and a quaternion in
  Pollen's ``from_euler("xyz", ...)`` convention, and antennas authored ``(left, right)``
  reach the wire as ``[right, left]``.
* Every goal carries ``lane="expression"``, ``client="ros"``; a de-escalation settle
  (``engagement:deescalate`` / ``cue:end``) goes on the ``safety`` lane instead, so the
  ownership gate never holds back a calm-settle.
* One batch at a time on one worker thread; a newer batch cancels the previous one's
  remaining goals and the goal in flight, so motion never outlives the decision.
* Ledger: ``goals_sent``, ``goals_completed``, ``goals_refused_<code>``,
  ``batches_cancelled``, ``targets_published``, ``skipped_<kind>``, ``unreachable``.
* Registered by entry point (``emori_skill_facade.transports``) *and* by an explicit
  ``register_transport`` call at import, so "installed" and "selectable" cannot drift.
