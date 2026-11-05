^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package reachy_mini_ros2
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

0.1.0 (2026-09-16)
------------------
* First release. The Reachy Mini as a native ROS 2 node: Pollen's transport-free
  ``Backend`` owned in-process, with no daemon and no HTTP hop in the motion path.
* ``backend_host.py``: builds ``MujocoBackend`` / ``MockupSimBackend`` / ``RobotBackend``
  from node parameters (including Pollen's own serial-port search, which refuses to
  guess when it finds none or several), installs ``Publisher`` hooks, runs
  ``wrapped_run`` on its own thread and waits on ``ready``. Caches the 50 Hz state so
  the REST shim's 10 Hz state poll never touches the control loop. Every ``reachy_mini``
  import is lazy, so the module parses and lints on a machine with neither ROS nor
  Pollen installed.
* ``arbiter.py``: one decision point for "may this move the robot, and when". Lanes
  ``safety`` > ``interaction`` > ``expression``; safety cancels the running move and
  flushes queued expression; ``preempt`` on interaction cuts a running expression move
  and nothing else; drop-if-busy at two queued; head reservation; an owner lock with a
  TTL that expires on the loop; a motor mode the backend refuses is latched off after
  one failure. One move at a time on one asyncio loop; cancellation is ``Task.cancel()``,
  which is what the daemon's own stop does. No ROS, and no hard dependency on
  ``reachy_mini`` either.
* ``conversions.py``: the one place Pollen's 4x4 matrices, ``{x,y,z,roll,pitch,yaw}``
  and ``geometry_msgs/Pose`` meet. Pure numpy, round-tripped against
  ``scipy.spatial.transform.Rotation`` - the convention Pollen itself uses - in
  ``tests/test_reachy_conversions.py``.
* ``robot_node.py``: ``/joint_states`` (9 joints, ~50 Hz), ``/reachy_mini/head_pose``,
  TF ``base_link`` -> ``head``, ``/reachy_mini/motion_status`` (latched
  ``transient_local`` depth 1, on change and every second), the MuJoCo camera on
  ``/reachy_mini/camera/image_raw`` + ``camera_info``, and ``/diagnostics`` under the
  status name ``reachy_mini``. Action servers ``/reachy_mini/goto`` and
  ``/reachy_mini/play_recorded_move``; services ``wake_up``, ``goto_sleep``,
  ``set_motor_mode``, ``stop``, ``set_head_reserved``, ``set_owner``; subscription
  ``/reachy_mini/set_target``.
* A refused goal SUCCEEDS with ``completed=false`` and an ``error_code`` rather than
  aborting: "the arbiter said no" and "something went wrong" need different responses
  from whoever asked.
* ``rest_shim.py`` is imported lazily and its absence is a WARNING, not a failure. A
  robot node that will not start because a compatibility layer is missing has the
  priorities backwards.
* ``launch/robot.launch.py`` with ``backend``, ``headless``, ``serialport``,
  ``wireless_version``, ``expression_owner``, ``rest_port``, ``with_camera_v4l2``
  (adds ``v4l2_camera`` and turns the in-process renderer off, so one topic keeps one
  publisher) and ``with_audio`` (``audio_common``, documented as untested).
* Python dependencies (``reachy-mini``, ``mujoco``, ``numpy``, ``scipy``, ``fastapi``,
  ``uvicorn``) are pip packages named in the README, deliberately not rosdep keys.
* Face look-at: the ``gaze/`` package (YuNet detector, geometry, tracking, controller)
  fed by the node's own camera capture, publishing ``/humans/faces/tracked`` and
  ``/humans/faces/<id>/roi``, configured by the ``gaze.*`` parameters and switched on and
  off at runtime by ``POST /api/emori/gaze``. ``POST /api/emori/head_reserved`` arms the
  head reservation over HTTP, and ``/api/state/full`` carries a ``motion`` block.
* ``sleep_on_shutdown``: park and de-torque on SIGINT (``auto`` = the robot backend
  only). If the control loop dies after having been ready the node exits with code 70 so
  its supervisor restarts it.
* Run on a real Reachy Mini Wireless through the Docker route (serial backend,
  ``/joint_states`` 48-49 Hz, clean SIGINT park, camera via the camera bridge's tee).
  Audio capture and the native (pixi) route have not been run on hardware.
  ``gravity_compensation`` is refused by the robot's v1.9.0 firmware and that refusal is
  inherited, latched, and documented rather than worked around.
