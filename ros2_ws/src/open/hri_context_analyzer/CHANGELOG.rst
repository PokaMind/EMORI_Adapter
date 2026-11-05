^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package hri_context_analyzer
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

0.1.0 (2026-09-16)
------------------
* Initial release.
* Fans ``emori_msgs/Context`` out as per-person REP-155
  extension topics under ``/humans/persons/<id>/context/``.
* A channel that did not report is not published at all — not as a zero, not as the
  sentinel. ``/context/degraded`` says out loud when no channel reported.
* Publishes the standard ``hri_msgs/Expression`` topic, introspecting the installed
  message (string in hri_msgs 2.1.0, ``uint8`` in older releases) instead of assuming a
  shape, and naming any label it cannot express in a warning.
* No dependency on the EMORI application: the sentinel and the reporting flags are read
  off ``emori_msgs/Context`` and the fan-out rule lives in ``contracts.py``, pure stdlib
  and testable with no ROS installed. The package builds and runs in an image where the
  EMORI core is not installed.
* Start-up check: the installed ``Context.REPORT_*`` constants are compared against
  ``contracts.ReportFlags`` and any disagreement is an ERROR naming the offenders.
