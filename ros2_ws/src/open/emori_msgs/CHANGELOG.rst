^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package emori_msgs
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Forthcoming
-----------
* The hub's interfaces, for the process that hosts the session logic beside the robot's ROS 2
  graph. All of them are additions; no existing field moved.
* ``msg/SessionState`` (latched): the kiosk session's phase, counters, watchdog state and last
  outcome. ``msg/SessionEvent``: one typed, gated message per relayed session envelope, with a
  per-process ``seq`` that has no gaps, so a missing number is a visible loss. Its ``text`` is
  empty unless the speech gate is open, its ids are pseudonyms unless de-identification is
  off, and ``payload_json`` is empty unless raw envelopes were asked for.
* ``msg/GestureEvent``, ``msg/GestureState`` (latched): the head-gesture sensor.
  ``msg/VoiceState``: the robot's own voice as its speaker output shows it.
  ``msg/PlayerState`` (latched): the motion player. ``msg/MotionIntent``: the adapter's motion
  command in the same units. ``msg/MotionPlanEvent``: one step in a sentence's motion plan,
  with the family performed, the family Pokamind forwarded (``family_authored``), how the family
  was chosen (``family_source``), the outcome and why a sentence was skipped (``skip_reason``).
  ``msg/AddonStatus`` (latched): the installed add-on. ``msg/KioskLanguage`` (latched).
* ``srv/SessionCommand``, ``srv/StartSession``, ``srv/GetVoiceMenu``, ``srv/SetTtOverrides``:
  the session. ``srv/SetLanguage``, ``srv/Recognize``, ``action/Say``: speech.
  ``srv/SubmitMotionBatch``: motion. ``srv/GetHubStatus``: the gateway.
* ``msg/SpeechCue``: ``cue_category_authored`` appended, the family Pokamind itself forwarded,
  beside ``cue_category``, which is the family the robot performs.

0.1.0 (2026-09-16)
------------------
* Initial release.
* ``msg/Context``: one analysis cycle for one person as a REP-155 extension —
  engagement score and level, valence, arousal, expression and confidence, trend,
  escalation, rapport, attention, dominant style and the top-5 style ranking, the
  body/face/voice observations, ``report_flags``, ``lang`` and ``age_s``.
* ``msg/Context`` constants: ``NOT_REPORTED=-1.0`` and ``REPORT_BODY/FACE/TONE/VERBAL/
  STYLES``. The wire contract travels with the interface, so a consumer can read it
  with ``ros2 interface show`` and no EMORI code installed.
* ``msg/SpeechCue``: one sentence of assistant speech with its cue family, estimated
  duration, source clock and ``ended`` marker.
* ``msg/SetExpression``: the bounded expression request (label, confidence, intensity,
  ``reason``).
* ``action/ReplayMotion`` and ``action/LookAt``: the two bounded motion skills, each
  returning an ``error_code`` so a refusal is legible rather than merely a failure.
* Compatibility rule for everything after this release: append at the END, never
  reorder, never rename, never retype.
