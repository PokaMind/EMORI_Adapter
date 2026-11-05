"""Robot clients that realise :class:`MotionIntent`s on the Reachy Mini daemon.

Two implementations:

* :class:`HttpRobotClient`   - talks to the daemon's REST API (verified endpoints
  ``/api/move/goto``, ``/api/move/set_target``, ``/api/media/play_sound``,
  ``/api/media/wobbling/{enable,disable}``, ``/api/move/play/wake_up``,
  ``/api/motors/set_mode/{mode}``, ``/api/daemon/status``, ``/api/state/full``).
  Works from the Windows host (Slice 1), from the ROS 2
  container (Slice 2, base_url = http://host.docker.internal:8000), AND on the CM4
  — REST is the one deployed transport (the SDK WebSocket client was removed).
* :class:`DryRunRobotClient` - records intents, no network. For tests / CI / a
  visual-free demo.

Robot audio note: all robot speech flows through the kiosk Chromium (hostapp
/api/tts -> page <audio> -> speaker); there is deliberately NO daemon audio-upload
path here — the on-robot daemon runs --no-media (motion only).

REST geometry note (verified against the daemon OpenAPI): ``XYZRPYPose`` is
**metres + radians**, so we convert from the adapter's mm + degrees here.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Optional, Protocol

import httpx

from emori_adapter import motion_trace
from emori_adapter.models import HeadPose, MotionIntent

log = logging.getLogger("emori.robot")

# The two v2 gate routes are advisory and are re-asserted ten times a second, so they
# get a much shorter deadline than motion does and a cool-off after any refusal.
_FLAG_TIMEOUT_S = 0.5
_FLAG_BACKOFF_S = 2.0


def _xyzrpy(head: HeadPose) -> dict[str, float]:
    """Adapter units (mm, degrees) -> daemon XYZRPYPose (metres, radians)."""
    return {
        "x": head.x / 1000.0,
        "y": head.y / 1000.0,
        "z": head.z / 1000.0,
        "roll": math.radians(head.roll),
        "pitch": math.radians(head.pitch),
        "yaw": math.radians(head.yaw),
    }


def _antennas_wire(antennas: tuple[float, float]) -> list[float]:
    """Adapter antenna order (left, right) -> daemon wire order [right, left].

    The behaviour layer authors antenna postures as ``(left, right)`` (how a human
    reads a gesture), but the daemon's ``SetAntennasCmd`` is ``[right, left]`` (index
    0 = RIGHT antenna; verified against reachy_mini v1.8.1). Confining the swap to
    this one boundary — exactly like ``_xyzrpy`` centralises the unit conversion —
    keeps every asymmetric gesture (curious tilt, wink, wiggle) landing on the
    intended physical antenna instead of its mirror image."""
    return [float(antennas[1]), float(antennas[0])]


class RobotClient(Protocol):
    def health(self) -> bool: ...
    def wake_up(self) -> None: ...
    def set_motor_mode(self, mode: str) -> None: ...
    def apply(self, intent: MotionIntent) -> None: ...
    def read_state(self) -> Optional[dict]: ...
    # v2-node extras. Part of the Protocol rather than a duck-typed getattr because
    # the driver pushes them on an edge and a client that silently lacked them would
    # leave the arbiter holding a stale gate with nothing in the logs to say so.
    def set_gaze(self, enabled: bool) -> bool: ...
    def set_head_reserved(self, reserved: bool) -> bool: ...
    def play_recorded_move(self, name: str, *, supersede: bool = False) -> str | None: ...
    def stop_recorded_move(self, uuid: str) -> None: ...
    def close(self) -> None: ...


def motion_sync_enabled() -> bool:
    """``EMORI_MOTION_SYNC`` (default on): the first move starts with the voice.

    Off puts back the older timing: beats released before the last one has finished, moves
    that wait their turn, and the voice bound from when the player reaches a sentence.
    """

    raw = (os.environ.get("EMORI_MOTION_SYNC", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


class HttpRobotClient:
    """Default client: drives the daemon over its REST API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)
        # Motor-mode changes get their OWN connection pool. A daemon that REFUSES a mode
        # aborts the TCP connection rather than answering cleanly (reachy_mini v1.9.0
        # returns an empty-body HTTP 500 for `gravity_compensation`), and the abort kills
        # whatever request is next on that pool. Measured on the real robot: 64 refusals
        # cost 9 dropped robot gestures in a single session. Keeping motion off this pool
        # means a refused mode can never swallow a gesture.
        self._mode_http = httpx.Client(base_url=self.base_url, timeout=timeout)
        # Modes this daemon has already refused: attempted once, then latched off. Without
        # the latch, RobotPlayer re-asserts on every idle tick and after every self-move,
        # so one unsupported mode becomes a permanent stream of failures and log noise.
        self._modes_refused: set[str] = set()
        # Same latch lineage as _modes_refused, one level up: routes this robot does not
        # serve at all. Only a 404 latches -- that is Pollen's daemon or a v1 node, and it
        # will never grow the route without a restart. A 503 (a v2 node with gaze disabled
        # today) deliberately does NOT, because a restart may enable it.
        self._routes_absent: set[str] = set()
        # ...and the TRANSIENT half of the same idea: `time.monotonic()` after which a
        # route that just failed may be tried again. Measured on this dev box, a POST to
        # a port nothing is listening on costs 2.03 s before httpx gives up. The gesture
        # ticker reconciles these two gates ten times a second, so without a backoff a
        # robot node that is merely restarting would take the head SENSOR down with it —
        # the thread would spend every one of its ticks waiting on a socket. A 503 (gaze
        # disabled today) gets the same treatment for the opposite reason: it is a
        # perfectly fast answer, and asking for it ten times a second for ever is spam.
        self._routes_backoff: dict[str, float] = {}
        self._media_ok = True  # flips False after first media failure to avoid log spam
        self._supersede = motion_sync_enabled()

    # --- connectivity ---
    def health(self) -> bool:
        # /api/daemon/status returns the full DaemonStatus model (~15 fields); we
        # only assert its `state` field == "running". (GET /health-check is NOT a
        # readiness endpoint — it's a POST-only watchdog-reset route — so use
        # /api/daemon/status.) Parse JSON rather than substring-match the body so
        # we're robust to key ordering.
        try:
            r = self._http.get("/api/daemon/status")
            if r.status_code != 200:
                return False
            return r.json().get("state") == "running"
        except (httpx.HTTPError, ValueError) as e:  # noqa: BLE001
            log.warning("daemon status check failed: %s", e)
            return False

    def wake_up(self) -> None:
        self._post("/api/move/play/wake_up", {})

    def set_motor_mode(self, mode: str) -> None:
        """Set the daemon's motor control mode (verified route
        ``POST /api/motors/set_mode/{mode}``; ``MotorControlMode`` values:
        ``enabled`` | ``disabled`` | ``gravity_compensation``).

        EMORI sets ``gravity_compensation`` so the back-drivable HEAD can be
        nudged by hand for the screenless head-tilt session control (the daemon
        otherwise boots motors ``enabled`` = stiff and holding position, which
        resists the touch). Must be RE-ASSERTED after ``wake_up()``/any self-move,
        which re-enable (stiffen) the motors.

        Not every daemon build supports every mode — reachy_mini v1.9.0 refuses
        ``gravity_compensation`` outright — so a refusal is latched per mode and never
        retried, and the whole exchange runs on :attr:`_mode_http` so a refusal cannot
        take a queued robot gesture down with it. A latched mode degrades gracefully:
        the head simply stays stiff (the head-tilt gesture then never fires)."""
        if mode in self._modes_refused:
            return
        try:
            r = self._mode_http.post(f"/api/motors/set_mode/{mode}", json={})
            if r.status_code >= 400:
                self._modes_refused.add(mode)
                log.warning(
                    "daemon refused motor mode %r (HTTP %s); not retrying. The head stays "
                    "stiff, so any back-drivable head-tilt gesture will not fire.",
                    mode, r.status_code,
                )
        except httpx.HTTPError as e:  # noqa: BLE001 - a refusal often aborts the connection
            self._modes_refused.add(mode)
            log.warning("motor mode %r failed (%s); not retrying.", mode, e)

    # --- v2 node extras (404 on v1: latched off, warned once) -----------------
    def set_gaze(self, enabled: bool) -> bool:
        """Tell the robot node whether face look-at is wanted right now.

        This is the RUN switch, not the capability switch: ``gaze.enabled`` in the
        node's parameters decides whether a detector may exist at all, and this
        decides whether it runs this second. It is fail-closed in the node (default
        off), so an idle kiosk in a public corridor is not running a face detector.
        """
        return self._emori_flag("/api/emori/gaze", {"enabled": bool(enabled)})

    def set_head_reserved(self, reserved: bool) -> bool:
        """Arm the arbiter's expression-lane head reservation (a hand on the head).

        The arbiter has had ``/reachy_mini/set_head_reserved`` since it was written and
        nothing ever called it, so the ROS graph's expression lane was never actually
        protected while a human had hold of the head. This is the caller.
        """
        return self._emori_flag("/api/emori/head_reserved", {"reserved": bool(reserved)})

    def _emori_flag(self, path: str, body: dict) -> bool:
        """POST one boolean gate; True only when the robot acknowledged it.

        Returning False (rather than raising) is what lets the driver keep the flag
        it PUSHED separate from the flag it WANTS: an unacknowledged push leaves the
        pushed state alone, so the next reconciler tick simply tries again.
        """
        if path in self._routes_absent:
            return False
        now = time.monotonic()
        if now < self._routes_backoff.get(path, 0.0):
            return False
        try:
            # A per-request timeout, well under the pool's: these two are advisory gates
            # whose next chance to be right arrives in 100 ms, so waiting seconds for one
            # buys nothing and costs the caller's tick.
            r = self._http.post(path, json=body, timeout=_FLAG_TIMEOUT_S)
        except httpx.HTTPError as e:  # noqa: BLE001 - a transport blip: retry, but later
            self._routes_backoff[path] = now + _FLAG_BACKOFF_S
            log.debug("POST %s failed: %s", path, e)
            return False
        if r.status_code == 404:
            self._routes_absent.add(path)
            log.warning(
                "%s is not served (HTTP 404): this robot is Pollen's daemon or a v1 node, "
                "so %s stays hostapp-local and is never asked again. Gaze and the arbiter "
                "head reservation are v2-only.", path,
                "face look-at" if "gaze" in path else "the head reservation")
            return False
        if r.status_code >= 400:
            # 503 = a v2 node with gaze disabled. NOT latched: a restart may enable it,
            # so this backs off rather than giving up.
            self._routes_backoff[path] = now + _FLAG_BACKOFF_S
            log.debug("POST %s -> HTTP %s", path, r.status_code)
            return False
        self._routes_backoff.pop(path, None)
        return True

    # --- intent dispatch ---
    def apply(self, intent: MotionIntent) -> None:
        if intent.kind in ("goto", "look_at"):
            self._goto(intent)
        elif intent.kind == "set_target":
            self._set_target(intent)
        elif intent.kind == "antennas":
            self._goto(intent)  # antenna-only goto
        elif intent.kind == "play_sound":
            self._play_sound(intent.sound)
        elif intent.kind == "wobble":
            self._wobble(bool(intent.wobble))
        elif intent.kind == "wake_up":
            self.wake_up()
        elif intent.kind == "play_recorded":
            # Fire-and-forget path only; RobotPlayer normally routes this kind
            # through play_recorded_move() so it can stop the move on preemption.
            self.play_recorded_move(intent.move or "", supersede=intent.supersede)
        else:  # pragma: no cover - guarded by the Literal type
            log.debug("ignoring unknown intent kind: %s", intent.kind)

    # --- Pollen recorded moves (the official emotion library) --------------
    # Dataset verified present in the daemon's HF cache; the daemon streams it
    # at 50 fps itself, so playback is one POST and the move runs in real time.
    RECORDED_DATASET = "pollen-robotics/reachy-mini-emotions-library"

    def play_recorded_move(self, name: str, *, supersede: bool = False) -> str | None:
        """Start one official recorded emotion move; returns its move UUID.

        The daemon returns ``{"uuid": ...}`` (MoveUUID) and keeps playing until
        the recording ends or ``stop_recorded_move`` is called with that uuid.
        """
        if not name:
            return None
        try:
            r = self._http.post(
                f"/api/move/play/recorded-move-dataset/{self.RECORDED_DATASET}/{name}",
                json={}, headers=self._move_headers(supersede))
            if r.status_code < 400:
                data = r.json() if r.content else {}
                uuid = data.get("uuid") if isinstance(data, dict) else None
                return str(uuid) if uuid else None
            log.warning("recorded move %r refused: HTTP %s", name, r.status_code)
        except Exception as e:  # noqa: BLE001 - a failed flourish must not kill the queue
            log.warning("recorded move %r failed: %s", name, e)
        return None

    def stop_recorded_move(self, uuid: str) -> None:
        """Stop a running recorded move (preemption: the next sentence wins).

        The daemon's contract (v1.9.0 OpenAPI) is
        ``POST /api/move/stop`` with a ``MoveUUID`` JSON body -- NOT a path
        parameter. ``/api/move/stop/{uuid}`` answers 404, so no recorded move would
        ever be cut and a 2.5-5 s Pollen performance would keep playing under the
        next sentence's plan (two writers into one head) and outlive the voice it
        was meant to accompany.
        """
        if not uuid:
            return
        try:
            r = self._http.post("/api/move/stop", json={"uuid": str(uuid)})
            if r.status_code >= 400:
                log.warning("stop recorded move %s refused: HTTP %s", uuid, r.status_code)
        except Exception as e:  # noqa: BLE001
            log.debug("stop recorded move %s failed: %s", uuid, e)

    def _goto(self, intent: MotionIntent) -> None:
        body: dict = {"duration": intent.duration, "interpolation": intent.interpolation}
        if intent.head is not None:
            body["head_pose"] = _xyzrpy(intent.head)
        if intent.antennas is not None:
            body["antennas"] = _antennas_wire(intent.antennas)
        if intent.body_yaw is not None:
            body["body_yaw"] = float(intent.body_yaw)
        self._post("/api/move/goto", body, headers=self._move_headers(intent.supersede))

    def _set_target(self, intent: MotionIntent) -> None:
        body: dict = {}
        if intent.head is not None:
            body["target_head_pose"] = _xyzrpy(intent.head)
        if intent.antennas is not None:
            body["target_antennas"] = _antennas_wire(intent.antennas)
        if intent.body_yaw is not None:
            body["target_body_yaw"] = float(intent.body_yaw)
        self._post("/api/move/set_target", body)

    def _play_sound(self, sound: str | None) -> None:
        if not sound or not self._media_ok:
            return
        try:
            r = self._http.post("/api/media/play_sound", json={"file": sound})
            if r.status_code >= 400:
                log.info("play_sound(%s) -> HTTP %s (media disabled?); muting further audio", sound, r.status_code)
                self._media_ok = False
        except httpx.HTTPError as e:  # noqa: BLE001
            log.info("play_sound failed (%s); muting further audio", e)
            self._media_ok = False

    def _wobble(self, enable: bool) -> None:
        path = "/api/media/wobbling/enable" if enable else "/api/media/wobbling/disable"
        try:
            self._http.post(path, json={})
        except httpx.HTTPError as e:  # noqa: BLE001
            log.debug("wobble toggle failed: %s", e)

    def _move_headers(self, supersede: bool) -> dict[str, str]:
        """Pollen's daemon ignores both of these; only the v2 node acts on them."""

        headers = {"X-Reachy-Supersede": "1"} if supersede and self._supersede else {}
        tag = motion_trace.current_tag()
        if tag:
            headers["X-Reachy-Trace"] = tag
        return headers

    def _post(self, path: str, body: dict, *, headers: Optional[dict] = None) -> None:
        try:
            r = self._http.post(path, json=body, headers=headers)
            if r.status_code >= 400:
                motion_trace.emit("post", path=path, status=r.status_code)
                log.warning("POST %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
        except httpx.HTTPError as e:  # noqa: BLE001
            motion_trace.emit("post", path=path, status=0)
            log.warning("POST %s failed: %s", path, e)

    def read_state(self) -> Optional[dict]:
        """Snapshot of the robot's present pose for gesture sensing: head_pose,
        body_yaw, antennas_position. Only the HEAD (6× XL330-M288-T) is
        back-drivable under ``gravity_compensation``; the body-yaw (XC330-M288-PG)
        and antenna (XL330-M077-T) motors are not torque-controllable and go limp
        in that mode. The gesture sensor reads ``head_pose`` ONLY, so that's fine —
        a human touch deflects the compliant head and shows up here (provided
        :meth:`set_motor_mode` set ``gravity_compensation``). Returns None on any
        transport error (the gesture sensor then simply skips that tick)."""
        try:
            r = self._http.get("/api/state/full")
            if r.status_code != 200:
                return None
            return r.json()
        except (httpx.HTTPError, ValueError) as e:  # noqa: BLE001
            log.debug("read_state failed: %s", e)
            return None

    def close(self) -> None:
        self._http.close()
        self._mode_http.close()


class DryRunRobotClient:
    """Records intents instead of sending them. Used by tests and ``--robot dry``."""

    def __init__(self) -> None:
        self.applied: list[MotionIntent] = []
        self.recorded_played: list[str] = []
        self.recorded_stopped: list[str] = []
        # The two v2 gates, recorded rather than sent. Tests assert on the EDGES here,
        # which is the whole contract of driver.sync_robot_flags.
        self.gaze_calls: list[bool] = []
        self.head_reserved_calls: list[bool] = []

    def set_gaze(self, enabled: bool) -> bool:
        self.gaze_calls.append(bool(enabled))
        return True

    def set_head_reserved(self, reserved: bool) -> bool:
        self.head_reserved_calls.append(bool(reserved))
        return True

    def play_recorded_move(self, name: str, *, supersede: bool = False) -> str | None:
        self.recorded_played.append(name)
        return "dry-%d" % len(self.recorded_played)

    def stop_recorded_move(self, uuid: str) -> None:
        self.recorded_stopped.append(uuid)

    def health(self) -> bool:
        return True

    def wake_up(self) -> None:
        self.applied.append(MotionIntent(kind="wake_up", reason="wake_up"))

    def set_motor_mode(self, mode: str) -> None:
        self.motor_mode = mode

    def apply(self, intent: MotionIntent) -> None:
        self.applied.append(intent)

    def read_state(self) -> Optional[dict]:
        # Tests inject a state by setting `.state`; default is a neutral resting pose.
        return getattr(self, "state", {
            "head_pose": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            "body_yaw": 0.0, "antennas_position": [0.0, 0.0],
        })

    def close(self) -> None:  # noqa: D401 - nothing to close
        pass
