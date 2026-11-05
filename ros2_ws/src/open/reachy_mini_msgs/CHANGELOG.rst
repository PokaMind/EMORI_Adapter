^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package reachy_mini_msgs
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

0.1.0 (2026-09-16)
------------------
* First release. The interface half of ``reachy_mini_ros2``: the Reachy Mini driven as a
  native ROS 2 component, with Pollen's transport-free ``Backend`` owned in-process and
  no HTTP daemon in the path.
* ``msg/MotionStatus``: what the motion arbiter is doing (``busy``, ``running_move_id``,
  ``queue_depth``) and who is allowed to move the robot (``owner``, ``head_reserved``,
  ``expression_owner``), plus ``motor_mode`` and ``backend``. Published latched
  (transient_local, depth 1) so a late subscriber learns the answer without waiting.
* ``msg/BodyTarget``: one streaming pose target, with a ``use_*`` flag per part so
  "leave the antennas alone" and "put the antennas at zero" stay distinguishable.
* ``action/GoTo`` and ``action/PlayRecordedMove``: the two motion goals, each carrying
  ``lane`` / ``client`` / ``preempt`` so arbitration is part of the request rather than
  a convention, and each returning ``completed`` + ``error_code`` + ``move_id`` so a
  refusal is legible rather than merely a failure.
* ``srv/SetMotorMode``, ``srv/StopMove``, ``srv/SetOwner``: motor mode (with the
  firmware's ``gravity_compensation`` refusal latched, as the host application does),
  cancellation (empty id = everything), and the owner lock with a TTL.
* Units: metres and radians everywhere; ``antennas`` is ``[right, left]``, Pollen's wire
  order; ``head_pose`` is the head frame relative to ``base_link``.
* Joint order, wherever nine joints appear: ``body_yaw``, ``stewart_1`` .. ``stewart_6``,
  ``right_antenna``, ``left_antenna``.
* Compatibility rule for everything after this release: append at the END, never
  reorder, never rename, never retype.
