"""The gaze thread: a camera frame in, at most one bounded head target out.

This is the only file in ``gaze/`` that knows about ROS, and it is deliberately thin.
Every decision it makes lives somewhere pure and tested: where the face is
(``geometry``), which face (``tracking``), how far the head may move (``controller``).
What is left here is plumbing, and the plumbing has three properties worth naming.

**One extra thread, not a sixth process.** The node already runs four thread families
(Pollen's 50 Hz control loop, the arbiter's asyncio loop, the camera capture, the rclpy
executor). Gaze adds one more, at nice 19 - the same thing Pollen's own ``FaceTracker``
does, for the same reason - and ONNX Runtime releases the GIL for the whole inference,
so it genuinely runs beside the control-loop callbacks rather than in front of them.

**The head pose travels WITH the frame.** Composing a 200-400 ms old pixel with a fresh
head pose re-adds the motion already executed: at 0.25 rad/s that is ~0.075 rad of
overshoot and then a correction the other way, i.e. visible hunting around a person who
is standing still. Carrying the pose is what makes this a controller rather than an
open loop with a delay.

**It is fail-closed.** ``gaze.enabled`` is a CAPABILITY (is the detector built at all);
``POST /api/emori/gaze`` is the RUN switch and defaults to false. An idle kiosk in a
public corridor therefore runs no face detector on nobody's behalf. Frames never leave
this process: the only thing published is a normalised bounding box and a confidence,
and the face id is a random 4-hex handle with a 10 s lifetime - a tracking handle,
deliberately not a re-identifier.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
import uuid

import numpy as np
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

from hri_msgs.msg import IdsList, NormalizedRegionOfInterest2D

from reachy_mini_ros2.gaze.controller import (
    COUNTER_SKIPPED_NO_ANCHOR,
    GazeController,
    GazeParams,
)
from reachy_mini_ros2.gaze.geometry import (
    DEFAULT_HEAD_TO_CAMERA_TRANSFORM,
    intrinsics_for_frame,
    look_at_image_pose,
)
from reachy_mini_ros2.gaze.tracking import AdaptiveCenterFilter, Tracker, to_observation
from reachy_mini_ros2.gaze.vision import FaceDetector

FACES_TOPIC = "/humans/faces/tracked"

#: The five fields ``NormalizedRegionOfInterest2D`` is expected to declare. Checked once
#: at construction: ``hri_context_analyzer/contracts.py`` exists because
#: ``hri_msgs/Expression`` is not stable across releases, and the same doctrine applies
#: here - a message-shape surprise must degrade one topic, never take the robot down.
_ROI_FIELDS = ("xmin", "ymin", "xmax", "ymax", "c")

#: How often the faces topic is republished even when nothing changed. Same discipline
#: as ``MotionStatus``: answerable by a tool that attached a moment ago, without being
#: five messages a second into a still room.
_FACES_REPEAT_S = 1.0

#: How long the gaze thread waits for a frame before looping to re-check its stop flag.
_SLOT_TIMEOUT_S = 0.5


class _FrameSlot:
    """Latest frame + the head pose and clock reading that belong to it.

    The pose is sampled on the CAMERA thread, at the instant the frame was pulled,
    because composing a 200-400 ms old pixel with a fresh head pose re-adds the motion
    already executed: overshoot ~= latency x slew = 0.3 s x 0.25 rad/s = 0.075 rad, then
    a correction the other way -- visible hunting around a STATIONARY face.

    One slot, latest wins: a gaze thread that fell behind must skip frames, never build
    a queue of stale ones.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._item: tuple[np.ndarray, np.ndarray | None, float] | None = None
        self._ready = threading.Event()

    def offer(self, frame: np.ndarray, head_pose: Any, at: float) -> None:
        """CAMERA thread. Never blocks, never copies, never raises."""

        with self._lock:
            self._item = (frame, head_pose, at)
        self._ready.set()

    def take(self, timeout: float):
        """GAZE thread. The newest frame offered, or None if none arrived in time."""

        if not self._ready.wait(timeout):
            return None
        with self._lock:
            item, self._item = self._item, None
            self._ready.clear()
        return item


class GazeComponent:
    """Face look-at, running inside the robot node beside the camera capture."""

    def __init__(self, node: Any, arbiter: Any, backend_host: Any, params: GazeParams,
                 *, camera_frame: str = "eye_camera") -> None:
        self._node = node
        self._log = node.get_logger()
        self._arbiter = arbiter
        self._backend_host = backend_host
        self._params = params
        self._camera_frame = camera_frame

        self._slot = _FrameSlot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._flag_lock = threading.Lock()
        self._flag_enabled = False
        self._flag_dirty = False

        self._controller = GazeController(params)
        self._tracker = Tracker(
            min_area_frac=params.min_area_frac, max_jump=params.max_jump,
            max_misses=params.max_misses, select_score=params.select_score,
            switch_margin=params.switch_margin, switch_ticks=params.switch_ticks)
        self._filter = AdaptiveCenterFilter()

        self._intrinsics: tuple[np.ndarray, np.ndarray] | None = None
        self._frame_size: tuple[int, int] = (0, 0)
        self._model_state = "not loaded"

        self._counters: dict[str, int] = {}
        self._detections = 0
        self._last_rate_sample = time.monotonic()
        self._last_detections = 0
        self._gaze_hz = 0.0
        self._faces_detected = 0
        self._faces_eligible = 0
        # Monotonic time of the last frame with a SELECTED face. Unlike is_active()
        # (an ACCEPTED head write within 1 s) it stays fresh while the head has
        # already arrived and writes nothing -- i.e. while a learner stands still.
        self._last_face_at: float | None = None
        self._face_score = 0.0
        self._last_detect = 0.0
        self._last_move_end = -1e9
        # Whether somebody else's HEAD move was running when this thread last looked.
        # The falling edge is what re-anchors the slew, and it is read from the
        # arbiter rather than inferred from our own `busy` refusals: a tick that was
        # deadbanded never reaches the arbiter at all, so a move that started and ended
        # between two of our writes would otherwise be missed entirely.
        self._head_was_moving = False
        self._warned_at: dict[str, float] = {}

        # The latest face aim as ((pitch, yaw), monotonic seconds), or None. Written by
        # the gaze thread, read by whoever is about to move the head (`pose_anchor`); a
        # tuple rebind is atomic, so the reader never sees half of one.
        self._aim: tuple[tuple[float, float], float] | None = None
        # The follower's goal: (target pose, invert, is_rest) or None to hold. Only the
        # gaze thread reads or writes it, like the controller itself.
        self._goal: tuple[Any, bool, bool] | None = None
        self._last_servo = 0.0

        self._face_id = ""
        self._face_id_expires_at = 0.0
        self._switches_seen = 0
        self._published_ids: list[str] = []
        self._faces_published_at = -1e9

        latched = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._faces_pub = node.create_publisher(IdsList, FACES_TOPIC, latched)
        self._roi_pub: Any = None
        self._roi_topic = ""

        declared = NormalizedRegionOfInterest2D.get_fields_and_field_types()
        self._roi_ok = all(field in declared for field in _ROI_FIELDS)
        if not self._roi_ok:
            self._log.warn(
                "the installed hri_msgs/NormalizedRegionOfInterest2D does not declare "
                f"{_ROI_FIELDS}; face ROIs will not be published. Gaze itself is "
                "unaffected. Check `ros2 interface show "
                "hri_msgs/msg/NormalizedRegionOfInterest2D`.")

    # --- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="reachy_mini_gaze",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # --- the camera thread's one call ---------------------------------------
    def offer(self, frame: np.ndarray, head_pose: Any, at: float) -> None:
        """Hand the newest frame over.  Called from the CAMERA thread only."""

        self._slot.offer(frame, head_pose, at)

    # --- the run flag (POST /api/emori/gaze) --------------------------------
    def set_enabled(self, enabled: bool) -> None:
        """The host application's run switch: is a face look-at wanted right now.

        Defaults to FALSE and is asserted at hostapp startup, so a node that has been up
        for days with a stale flag is corrected rather than trusted.

        Called from one of the REST shim's HTTP worker threads, so it only RECORDS the
        change; the gaze thread applies it at the top of its next tick (<= 200 ms). The
        controller is not thread-safe and never needs to be: this way exactly one thread
        ever touches its slew anchor, its rest pose and its relax clock.
        """

        enabled = bool(enabled)
        with self._flag_lock:
            if enabled == self._flag_enabled:
                return
            self._flag_enabled = enabled
            self._flag_dirty = True
        if not enabled:
            # Now, not at the next tick: a gesture arriving in the next 200 ms must not
            # be aimed at a face gaze has just been told to stop looking at.
            self._aim = None
            self._goal = None
        self._log.info(f"gaze {'enabled' if enabled else 'disabled'} by the host application")

    def run_flag(self) -> bool:
        """The run switch as last set: is a face look-at wanted right now.

        Distinct from :meth:`is_active`, which says a target was accepted a moment ago.
        This is the fail-closed privacy flag itself, so a tool can ask whether the face
        detector is permitted to run.
        """

        return self._enabled_flag()

    def _apply_flag_change(self) -> None:
        """GAZE thread. Arm or disarm the controller if the flag moved."""

        with self._flag_lock:
            if not self._flag_dirty:
                return
            self._flag_dirty = False
            enabled = self._flag_enabled
        # The rest pose is the pose that was COMMANDED at the instant gaze was armed: in
        # a training session that is start_session's -14 deg look-up, in an add-on mode
        # it is that mode's feedback pose.
        self._controller.set_enabled(enabled, self._commanded())
        if enabled:
            self._tracker = Tracker(
                min_area_frac=self._params.min_area_frac, max_jump=self._params.max_jump,
                max_misses=self._params.max_misses,
                select_score=self._params.select_score,
                switch_margin=self._params.switch_margin,
                switch_ticks=self._params.switch_ticks)
            self._filter.reset()
            return

        # `/humans/faces/tracked` is TRANSIENT_LOCAL, so its last sample is what every
        # REP-155 consumer that attaches later is told. Leaving `ids=[fXXXX]` latched
        # while the detector is off would advertise a face nobody is tracking, for the
        # rest of the process. `_faces_published_at` is rewound so the publish is
        # unconditional rather than subject to the change-only rule.
        self._faces_published_at = -1e9
        self._publish_ids([], time.monotonic())
        self._face_score = 0.0

    def pose_anchor(self) -> tuple[float, float] | None:
        """``(pitch, yaw)`` to centre other authors' head moves on, or None.

        None unless the run flag is up and a face was aimed at within `anchor_ttl_s`:
        with nobody in front of the robot a gesture is authored about neutral and is
        played about neutral, exactly as before this existed.
        """

        if not (self._params.enabled and self._params.anchor_moves and self._enabled_flag()):
            return None
        aim = self._aim
        if aim is None or time.monotonic() - aim[1] > self._params.anchor_ttl_s:
            return None
        return aim[0]

    def is_active(self) -> bool:
        """True while a target was accepted within the last second."""

        return self._controller.is_active(time.monotonic())

    def face_seen_s(self) -> float | None:
        """Seconds since a face was last selected; None if none yet or gaze is off."""

        if self._last_face_at is None or not (self._params.enabled and self._enabled_flag()):
            return None
        return max(0.0, time.monotonic() - self._last_face_at)

    # --- /diagnostics -------------------------------------------------------
    def diagnostics(self) -> dict[str, str]:
        """The gaze block of the node's DiagnosticStatus, as strings.

        Gaze never raises the node's severity: a face detector that cannot load is not a
        robot that is unwell, and `reachy_mini`'s level is the robot's triage signal for
        the control loop.
        """

        now = time.monotonic()
        elapsed = max(1e-6, now - self._last_rate_sample)
        if elapsed >= 1.0:
            self._gaze_hz = (self._detections - self._last_detections) / elapsed
            self._last_rate_sample = now
            self._last_detections = self._detections

        values = {
            "gaze_enabled": str(self._params.enabled and self._flag_enabled).lower(),
            "gaze_active": str(self.is_active()).lower(),
            "gaze_hz": f"{self._gaze_hz:.1f}",
            "faces_detected": str(self._faces_detected),
            "faces_eligible": str(self._faces_eligible),
            "face_id": self._face_id,
            "face_score": f"{self._face_score:.2f}",
            "gaze_lost_s": f"{self._controller.lost_for(now):.1f}",
            "gaze_switches": str(self._tracker.switches),
            "gaze_model": self._model_state,
        }
        values.update({key: str(count) for key, count in sorted(self._counters.items())})
        return values

    # --- the thread ---------------------------------------------------------
    def _run(self) -> None:
        self._lower_priority()
        detector = self._build_detector()
        if detector is None:
            return

        while not self._stop.is_set():
            item = self._slot.take(self._wait_s())
            if self._servo_on() and self._params.enabled and self._enabled_flag():
                # Between frames: glide towards the goal the last detection set. A frame
                # that is not due for detection still counts as a follower tick.
                now = time.monotonic()
                self._apply_flag_change()
                self._note_motion_edge(now)
                try:
                    self._servo_tick(now)
                except Exception as failure:  # noqa: BLE001 - one bad step is not a dead robot
                    self._warn_every(30.0, f"gaze follower step failed: {failure}")
            if item is None:
                continue
            frame, head_pose, _captured_at = item

            now = time.monotonic()
            if now - self._last_detect < self._period():
                continue
            # Recorded HERE, not at the bottom: every branch below is one tick of the
            # `detect_hz` loop, including the ones that `continue`. Advancing the clock
            # only on the full path let the relax-on-disable and the settle window run
            # at the CAMERA's rate instead - 10 steps a second where `max_step_rad`
            # documents 5, i.e. 0.5 rad/s where the comment promises 0.25.
            self._last_detect = now

            self._apply_flag_change()
            # Before anything is computed from the slew anchor, because the target is
            # built FROM it: a move that ended since the last tick left the head
            # somewhere gaze did not choose, and a target anchored on the stale value
            # spends the whole accumulated step budget in one 200 ms tick.
            self._note_motion_edge(now)

            if not (self._params.enabled and self._enabled_flag()):
                self._quiet_tick(now)
                continue

            # M19's knob, default 0.0: skip N seconds of ticks after a move completes, so
            # a hold pose that was just authored is not overwritten before it is seen.
            if now - self._last_move_end < self._params.settle_after_move_s:
                continue

            try:
                self._tick(detector, frame, head_pose, now)
            except Exception as failure:  # noqa: BLE001 - one bad tick is not a dead robot
                self._warn_every(30.0, f"gaze tick failed: {failure}")

    def _note_motion_edge(self, now: float) -> None:
        """Re-anchor the slew on the falling edge of somebody else's head move.

        ``motion_snapshot()`` exists for exactly this kind of poll: four fields under
        the arbiter's lock, no backend read and no counter copy, so asking once per
        detection costs nothing on a CM4 that is also running the 50 Hz loop.

        ``head_moving`` rather than plain ``active``: an antenna-only cue refuses gaze
        ``busy`` without moving the head at all, so the anchor it left is still true and
        re-anchoring for one would throw away a slew that is half done.
        """

        try:
            moving = bool(self._arbiter.motion_snapshot().get("head_moving", False))
        except Exception as failure:  # noqa: BLE001 - degraded, never fatal
            # Said out loud rather than swallowed: without this poll the slew anchor is
            # never re-anchored after somebody else's move, and the symptom is a head
            # that lurches once per cue gesture - which reads as a mechanical fault.
            self._warn_every(300.0, f"gaze cannot poll the arbiter for the end of a "
                                    f"move ({failure}); the head may step abruptly "
                                    "after a cue gesture")
            return
        if self._head_was_moving and not moving:
            self._last_move_end = now
            self._controller.reanchor()
        self._head_was_moving = moving

    def _tick(self, detector: FaceDetector, frame: np.ndarray, head_pose: Any,
              now: float) -> None:
        height, width = int(frame.shape[0]), int(frame.shape[1])
        if self._intrinsics is None or self._frame_size != (width, height):
            # From the ACTUAL frame shape, never from the node's published CameraInfo,
            # which is labelled "Nominal, not calibrated" with d all zeros.
            self._intrinsics = intrinsics_for_frame(
                width, height, full_width_px=self._params.sensor_full_width_px,
                fx_scale=self._params.fx_scale, fy_scale=self._params.fy_scale)
            self._frame_size = (width, height)
            K, _D = self._intrinsics
            self._log.info(
                f"gaze: {width}x{height} intrinsics fx={K[0, 0]:.1f} fy={K[1, 1]:.1f} "
                f"cx={K[0, 2]:.1f} cy={K[1, 2]:.1f}")
        K, D = self._intrinsics

        # The detector wants BGR; the camera publishes RGB.
        faces = detector.detect(np.ascontiguousarray(frame[:, :, ::-1]))
        face = self._tracker.select(faces, width, height)
        self._detections += 1
        if face is not None:
            self._last_face_at = now
        self._faces_detected = self._tracker.last_detected
        self._faces_eligible = self._tracker.last_eligible

        observation = to_observation(face, width, height, K, D, now)
        if observation.center is not None:
            observation.center = self._filter.update(observation.center)
        if face is None and not self._tracker.has_target:
            self._filter.reset()

        self._publish_faces(face, observation, width, height, now)

        if observation.center is None:
            if self._servo_on():
                # Hold where we are; after `relax_s` the goal becomes the rest pose and
                # the follower walks back to it at the same gentle speed.
                rest = self._controller.relax_goal(now)
                self._goal = None if rest is None else (rest, False, True)
                return
            # Deliberately NOT routed through `_write`'s `target is None` branch.
            # `handle_loss` returns None for the overwhelmingly common cases - never
            # locked, inside the hold, already relaxed - and counting those as
            # `gaze_deadband` would make an empty room indistinguishable from a person
            # sitting still, which is the exact pair an operator reads these two
            # counters to tell apart.
            relax = self._controller.handle_loss(now, self._commanded())
            if relax is not None:
                self._write(relax, now)
            return

        # Invert `_center()`'s normalisation so the FILTERED centre is what is aimed at:
        # the tracker and the filter both work in [-1, 1] (which is what makes max_jump
        # and the dead zone resolution-independent) and the geometry works in pixels.
        u = (observation.center[0] + 1.0) / 2.0 * (width - 1)
        v = (observation.center[1] + 1.0) / 2.0 * (height - 1)
        world_head = np.eye(4) if head_pose is None else np.asarray(head_pose, dtype=float)
        target = look_at_image_pose(u, v, K, D, world_head,
                                    DEFAULT_HEAD_TO_CAMERA_TRANSFORM)
        self._controller.note_seen(now)
        self._aim = (self._controller.aim(target), now)
        self._face_score = float(observation.score)
        if self._servo_on():
            self._goal = (target, True, False)
            return
        # The COMMANDED pose is read fresh: roll is a carry rather than a measurement,
        # and the freshest author's intent is the right one to carry.
        self._write(self._controller.limit(target, self._commanded()), now)

    def _write(self, target: Any, now: float) -> None:
        if target is None:
            # The limiter names its own counter: it returns None both for "the head is
            # already where this observation would put it" and for "nobody can say where
            # the head is", and those must not share a line on /diagnostics.
            counter = self._controller.skip_counter
            self._bump(counter)
            if counter == COUNTER_SKIPPED_NO_ANCHOR:
                self._warn_every(
                    60.0,
                    "gaze has no anchor to slew from: the backend reports no commanded "
                    "head pose, so no target can be built without guessing that the "
                    "head is at neutral. Nothing will be written until it does.")
            return
        outcome = self._arbiter.set_target(
            head_pose=target, antennas=None, body_yaw=None,
            client=self._params.client, lane=self._params.lane)
        accepted = bool(getattr(outcome, "accepted", False))
        code = "" if accepted else str(getattr(outcome, "error_code", "") or "unknown")
        # `busy` is counted here and nowhere else; the busy -> idle EDGE is detected
        # before the target is built (`_note_motion_edge`), because by the time an
        # outcome comes back the target that lurched has already been committed.
        self._bump(self._controller.on_outcome(accepted, code, now))

        if code == "not_owner":
            owner = ""
            try:
                owner = str(self._arbiter.status().get("expression_owner", ""))
            except Exception:  # noqa: BLE001 - a status read must not break the tick
                pass
            self._warn_every(
                60.0,
                f"gaze targets are refused not_owner: the expression owner is {owner!r}, "
                f"not {self._params.client.split(':', 1)[0]!r}. Set "
                "EMORI_ROS_MOTION_OWNER=ros, or launch with gaze.lane:=interaction.")
        elif code and code not in ("busy", "head_reserved"):
            self._warn_every(30.0, f"gaze set_target refused {code!r}")

    # --- the follower -------------------------------------------------------
    def _servo_on(self) -> bool:
        return float(self._params.servo_hz) > 0.0

    def _wait_s(self) -> float:
        """How long the gaze thread may sleep waiting for a frame."""

        if self._servo_on() and self._goal is not None:
            return 1.0 / float(self._params.servo_hz)
        return _SLOT_TIMEOUT_S

    def _servo_tick(self, now: float) -> None:
        """One glide step towards the goal, at most `servo_hz` times a second."""

        if self._goal is None:
            return
        dt = now - self._last_servo
        if dt < 0.8 / float(self._params.servo_hz):
            return
        self._last_servo = now
        try:
            # Somebody else's move owns the head (a cue beat, a recorded move): wait
            # for it rather than collect a `busy` refusal 25 times a second. The
            # falling edge re-anchors the slew (`_note_motion_edge`).
            if bool(self._arbiter.motion_snapshot().get("active", False)):
                return
        except Exception:  # noqa: BLE001 - degraded to "try and take the refusal"
            pass
        target, invert, is_rest = self._goal
        step_dt = min(dt, 2.0 / float(self._params.servo_hz))
        limited = self._controller.limit(target, self._commanded(), invert=invert,
                                         servo_dt=step_dt)
        if limited is None:
            # Arrived (the deadband) or blind (no anchor): drop the goal until the next
            # detection sets one, so the counters keep counting at `detect_hz` as they
            # always did. Arrived at rest also ends the walk.
            if is_rest:
                self._controller.mark_relaxed()
            self._goal = None
            self._bump(self._controller.skip_counter)
            return
        self._write(limited, now)

    def _quiet_tick(self, now: float) -> None:
        """The run flag is false: walk the head back once, then spend nothing at all."""

        target = self._controller.go_quiet(self._commanded())
        if target is None:
            return
        self._write(target, now)

    # --- REP-155 publishing -------------------------------------------------
    def _publish_faces(self, face: Any, observation: Any, width: int, height: int,
                       now: float) -> None:
        if face is None:
            if not self._tracker.has_target:
                self._publish_ids([], now)
                # /diagnostics must stop advertising a lock that is gone: `face_id` is
                # "the current lock, empty when none", and an operator triaging a robot
                # cannot tell a held lock from a dead one otherwise. The score is about
                # THIS tick and there is no face on it; the id survives its TTL so a
                # re-acquisition inside the window still reuses it.
                self._face_score = 0.0
                if now >= self._face_id_expires_at:
                    self._face_id = ""
            return

        self._publish_ids([self._current_face_id(now)], now)
        if not self._roi_ok:
            return

        x, y, w, h = face.bbox
        roi = NormalizedRegionOfInterest2D()
        roi.header.stamp = self._node.get_clock().now().to_msg()
        roi.header.frame_id = self._camera_frame
        # Clipped to [0, 1]: the message's own docstring requires it, and YuNet's box
        # regression really does run off the frame edge on a face at the border.
        roi.xmin = float(np.clip(x / width, 0.0, 1.0))
        roi.ymin = float(np.clip(y / height, 0.0, 1.0))
        roi.xmax = float(np.clip((x + w) / width, 0.0, 1.0))
        roi.ymax = float(np.clip((y + h) / height, 0.0, 1.0))
        roi.c = float(observation.score)
        if self._roi_pub is not None:
            self._roi_pub.publish(roi)

    def _publish_ids(self, ids: list[str], now: float) -> None:
        if ids == self._published_ids and (now - self._faces_published_at) < _FACES_REPEAT_S:
            return
        message = IdsList()
        message.header.stamp = self._node.get_clock().now().to_msg()
        message.header.frame_id = self._camera_frame
        message.ids = list(ids)
        self._faces_pub.publish(message)
        self._published_ids = list(ids)
        self._faces_published_at = now

    def _current_face_id(self, now: float) -> str:
        """The id of the locked face, minted or reused.

        Kept across a LOST lock for `id_ttl_s` (10 s): a re-acquisition inside that
        window is the same person, and reusing the id means a flickering lock in a busy
        room churns DDS discovery at most once per 10 s instead of once every 4 s on a
        box already at loadavg 4.7. A deliberate challenger switch re-mints regardless -
        that one really is somebody else.
        """

        switched = self._tracker.switches != self._switches_seen
        expired = now >= self._face_id_expires_at
        if not self._face_id or switched or expired:
            self._face_id = f"f{uuid.uuid4().hex[:4]}"
            self._switches_seen = self._tracker.switches
            self._rebuild_roi_publisher()
        self._face_id_expires_at = now + self._params.id_ttl_s
        return self._face_id

    def _rebuild_roi_publisher(self) -> None:
        """At most one ROI publisher is alive at a time."""

        if not self._roi_ok:
            return
        if self._roi_pub is not None:
            try:
                self._node.destroy_publisher(self._roi_pub)
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
        self._roi_topic = f"/humans/faces/{self._face_id}/roi"
        self._roi_pub = self._node.create_publisher(
            NormalizedRegionOfInterest2D, self._roi_topic, 10)

    # --- small helpers ------------------------------------------------------
    def _build_detector(self) -> FaceDetector | None:
        """The detector, or a WARN and a degraded node.

        Never fatal: the same doctrine as `_start_v4l2_camera`. A robot that refuses to
        start because it cannot look at faces has the priorities backwards.
        """

        try:
            detector = FaceDetector(self._params.model_path,
                                    self._params.score_threshold,
                                    self._params.nms_threshold)
        except Exception as failure:  # noqa: BLE001 - a missing model is degraded, not dead
            self._model_state = f"unavailable: {failure}"
            self._log.warn(
                f"gaze: the face detector could not be loaded from "
                f"{self._params.model_path!r} ({failure}); the robot will not look at "
                "faces. Everything else is unaffected. Check EMORI_GAZE_MODEL and the "
                "/opt/emori/models bind mount.")
            return None
        self._model_state = self._params.model_path
        self._log.info(f"gaze: YuNet loaded from {self._params.model_path}")
        return detector

    @staticmethod
    def _lower_priority() -> None:
        """Nice 19 on this thread only - what Pollen's own FaceTracker does."""

        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except Exception:  # noqa: BLE001 - not Linux, or not permitted
            pass

    def _period(self) -> float:
        return 1.0 / max(0.1, float(self._params.detect_hz))

    def _enabled_flag(self) -> bool:
        with self._flag_lock:
            return self._flag_enabled

    def _commanded(self) -> Any:
        try:
            return self._backend_host.commanded_head_pose()
        except Exception:  # noqa: BLE001 - a backend that cannot say is not an error
            return None

    def _bump(self, key: str) -> None:
        self._counters[key] = self._counters.get(key, 0) + 1

    def _warn_every(self, period: float, message: str) -> None:
        """A 5 Hz loop must never log per tick."""

        now = time.monotonic()
        if now - self._warned_at.get(message, -1e9) < period:
            return
        self._warned_at[message] = now
        self._log.warn(message)
