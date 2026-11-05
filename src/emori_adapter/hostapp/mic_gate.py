"""Close the microphone to sound that is not coming from the person in front of the robot.

The robot's voice-activity detection cannot tell a voice across the room from the person it
is talking to: the mic chip's automatic gain lifts distant talk to the level of a near voice,
so background conversation passes as a turn and gets an answer. The chip does know the
direction a voice comes from, and the robot's own camera knows whether anybody is in front of
it. This gate uses those two to mute the shared capture source, so that it works for every
consumer of the microphone at once, including a page in the kiosk that the robot cannot change.

A voice from the side is shut out. Two people speaking from the SAME direction are not: no
audio-only test can part them.

Every doubt opens the gate, because a robot that cannot hear the person in front of it is far
worse than one that hears a little too much:

* a direction that cannot be read says nothing, and the mic stays open;
* it stays open while the robot itself speaks, whose voice is not evidence of anything;
* a robot that has not seen a face this session has nothing to compare against, so a dark
  room or a camera fault cannot deafen it;
* off-axis sound has to last before it closes, and closing lapses on its own soon after;
* the gate reopens when it is stopped and whenever the hub starts, in every mode, because a
  mute survives in PipeWire after the process that set it.

``EMORI_MIC_GATE`` is ``off``, ``observe`` (decide and log, touch nothing) or ``on``.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("emori.hostapp.mic_gate")

OFF = "off"
OBSERVE = "observe"
ON = "on"

_WPCTL = "wpctl"
_SOURCE = "@DEFAULT_AUDIO_SOURCE@"
_WPCTL_TIMEOUT_S = 3.0

# The mic chip's direction of arrival, from Pollen's control protocol: USB vendor request,
# service 20, command 19, answering (radians, speech flag) as two little-endian floats.
_USB_VENDOR = 0x38FB
_USB_PRODUCT = 0x1001
_DOA_RESID = 20
_DOA_CMD = 19
_DOA_LEN = 8
_USB_TIMEOUT_MS = 100
# Straight ahead of the mic array, in the chip's own frame: 0 left, pi/2 front, pi right.
FRONT_RAD = math.pi / 2


def gate_mode() -> str:
    """``EMORI_MIC_GATE``, with anything unrecognised reading as off."""

    raw = (os.environ.get("EMORI_MIC_GATE", "") or "").strip().lower()
    return raw if raw in (OBSERVE, ON) else OFF


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if not low <= value <= high:
        log.warning("%s=%s is outside %s..%s; using %s", name, raw, low, high, default)
        return default
    return value


class ChipDirection:
    """The chip's direction reading, over USB. Answers None whenever it cannot."""

    def __init__(self) -> None:
        self._device: Any = None
        self._usb: Any = None
        self._failed_at = 0.0

    def read(self) -> Optional[tuple[float, bool]]:
        """(radians, speech flag), or None."""

        try:
            device = self._open()
            if device is None:
                return None
            usb = self._usb
            request = usb.util.build_request_type(
                usb.util.CTRL_IN, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE)
            raw = bytes(device.ctrl_transfer(request, 0, 0x80 | _DOA_CMD, _DOA_RESID,
                                             _DOA_LEN + 1, _USB_TIMEOUT_MS))
            # The first byte is the chip's status; anything else means no reading.
            if len(raw) < _DOA_LEN + 1 or raw[0] != 0:
                return None
            import struct

            angle, speech = struct.unpack("<ff", raw[1:_DOA_LEN + 1])
        except Exception:  # noqa: BLE001 - a USB fault is "no evidence", never a closed mic
            self._device = None
            self._failed_at = time.monotonic()
            log.debug("direction read failed", exc_info=True)
            return None
        if not (math.isfinite(angle) and math.isfinite(speech)):
            return None
        return angle, speech > 0.0

    def _open(self) -> Any:
        if self._device is not None:
            return self._device
        # Do not spend a USB scan ten times a second on a robot without this chip.
        if time.monotonic() - self._failed_at < 30.0:
            return None
        import usb.core
        import usb.util

        self._usb = usb
        backend = None
        try:
            import libusb_package

            backend = libusb_package.get_libusb1_backend()
        except Exception:  # noqa: BLE001 - pyusb may find a system libusb on its own
            backend = None
        self._device = usb.core.find(idVendor=_USB_VENDOR, idProduct=_USB_PRODUCT, backend=backend)
        if self._device is None:
            self._failed_at = time.monotonic()
        return self._device


class MicGate:
    """One thread, polling while a mode holds the microphone. Fail-open throughout."""

    def __init__(
        self,
        *,
        mode: Optional[str] = None,
        direction: Optional[Callable[[], Optional[tuple[float, bool]]]] = None,
        face_age: Optional[Callable[[], Optional[float]]] = None,
        robot_speaking: Optional[Callable[[], bool]] = None,
        run: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        poll_s: float = 0.1,
    ) -> None:
        self.mode = gate_mode() if mode is None else mode
        self._direction = direction if direction is not None else ChipDirection().read
        # Seconds since a face was last seen this session; None before the first.
        self._face_age = face_age if face_age is not None else (lambda: None)
        self._robot_speaking = robot_speaking if robot_speaking is not None else (lambda: False)
        self._run = run if run is not None else subprocess.run
        self._clock = clock if clock is not None else time.monotonic
        self._poll_s = poll_s
        half = _env_float("EMORI_MIC_GATE_HALF_WIDTH_DEG", 50.0, 10.0, 90.0)
        self._half_width = math.radians(half)
        self._face_hold_s = _env_float("EMORI_MIC_GATE_FACE_HOLD_S", 8.0, 1.0, 120.0)
        self._close_after_s = _env_float("EMORI_MIC_GATE_CLOSE_AFTER_S", 0.3, 0.1, 5.0)
        self._reopen_after_s = _env_float("EMORI_MIC_GATE_REOPEN_AFTER_S", 1.5, 0.2, 30.0)
        self._lock = threading.Lock()
        # One decision and its wpctl call at a time: a mute landing after the release that
        # should have prevented it would leave the microphone shut with nobody to open it.
        self._effect_lock = threading.Lock()
        self._stop = threading.Event()
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._off_axis_since: Optional[float] = None
        self._off_axis_last: Optional[float] = None
        self._warned = False
        self.counters = {"polls": 0, "no_reading": 0, "speech": 0, "off_axis": 0,
                         "closed_direction": 0, "closed_no_face": 0, "reopened": 0}

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        """Clear a mute left behind by a process that died holding it, then run."""

        self._mute(False)
        if self.mode == OFF:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="mic-gate", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._open("stop")

    def set_active(self, active: bool) -> None:
        """A mode has taken or released the microphone. Released always means open."""

        with self._lock:
            self._active = bool(active)
            self._off_axis_since = None
            self._off_axis_last = None
        if not active:
            self._open("released")

    def snapshot(self) -> dict:
        """Numbers and a state only, for /api/status."""

        return {"mode": self.mode, "active": self._active, "closed": self._closed,
                **self.counters}

    # --- the decision ------------------------------------------------------------------

    def _on_axis(self, angle: float) -> bool:
        return abs(angle - FRONT_RAD) <= self._half_width

    def tick(self) -> None:
        """One poll. Public so a test can drive it without a thread."""

        if not self._active:
            return
        self.counters["polls"] += 1
        now = self._clock()
        if self._no_face_for_too_long():
            self._close("no_face")
            return
        if self._is_robot_speaking():
            self._off_axis_since = self._off_axis_last = None
            self._open("robot speaking")
            return
        reading = self._direction()
        if reading is None:
            self.counters["no_reading"] += 1
            self._off_axis_since = self._off_axis_last = None
            self._open("no reading")
            return
        angle, speech = reading
        if speech:
            self.counters["speech"] += 1
            if self._on_axis(angle):
                self._off_axis_since = self._off_axis_last = None
                self._open("speech in front")
                return
            self.counters["off_axis"] += 1
            if self._off_axis_since is None:
                self._off_axis_since = now
            self._off_axis_last = now
            if now - self._off_axis_since >= self._close_after_s:
                self._close("direction")
            return
        # Quiet. A closed gate lapses by itself, and a gap of quiet starts the count again.
        if self._off_axis_last is not None and now - self._off_axis_last >= self._reopen_after_s:
            self._off_axis_since = self._off_axis_last = None
            self._open("quiet")
        elif self._off_axis_last is None:
            self._open("no reason to close")

    def _is_robot_speaking(self) -> bool:
        try:
            return bool(self._robot_speaking())
        except Exception:  # noqa: BLE001 - no answer is no evidence
            return False

    def _no_face_for_too_long(self) -> bool:
        try:
            age = self._face_age()
        except Exception:  # noqa: BLE001 - no answer is no evidence
            return False
        return age is not None and age > self._face_hold_s

    def _close(self, why: str) -> None:
        with self._effect_lock:
            # Asked again here: the mode may have let go of the microphone since this tick
            # began, and nothing would come back to open it.
            if self._closed or not self._active or self._stop.is_set():
                return
            self.counters["closed_no_face" if why == "no_face" else "closed_direction"] += 1
            log.warning("mic gate: closing (%s)%s", why,
                        " [observe]" if self.mode == OBSERVE else "")
            self._closed = True
            if self.mode == ON:
                self._mute(True)

    def _open(self, why: str) -> None:
        with self._effect_lock:
            if not self._closed:
                return
            self.counters["reopened"] += 1
            log.warning("mic gate: opening (%s)%s", why,
                        " [observe]" if self.mode == OBSERVE else "")
            self._closed = False
            if self.mode == ON:
                self._mute(False)

    def _mute(self, muted: bool) -> None:
        argv = [_WPCTL, "set-mute", _SOURCE, "1" if muted else "0"]
        try:
            result = self._run(argv, capture_output=True, text=True,
                               timeout=_WPCTL_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            self._warn(f"wpctl failed ({type(error).__name__})")
            return
        if getattr(result, "returncode", 0):
            self._warn(f"wpctl exited {result.returncode}")

    def _warn(self, message: str) -> None:
        if self._warned:
            log.debug("mic gate: %s", message)
            return
        self._warned = True
        log.warning("mic gate: %s", message)

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_s):
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - never let a fault hold the mic shut
                self._warn(f"a tick failed ({type(error).__name__})")
                log.debug("mic gate tick failed", exc_info=True)
                self._open("error")
