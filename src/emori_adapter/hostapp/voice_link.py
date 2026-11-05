"""VoiceLink: the robot's OWN voice, as a live signal the motion player can follow.

The per-sentence expression lane plans motion from the text of each sentence and
starts it at Pokamind's playback-onset event. That is onset-aligned but blind: the
plan's length is a word-count estimate, the beats are a fixed metronome, and when the
voice pauses (a TTS chunk arrives late, the sentence ends early, the next sentence
preempts) the head keeps performing the estimate. Measured live: motion
that ran after the voice had stopped, a recorded Pollen move under the next
sentence's plan, beats with no relation to the words.

This module closes that loop without touching the speech path at all. The kiosk
Chromium plays the AI's voice through PipeWire into the robot's one USB speaker;
PipeWire exposes every sink's MONITOR, i.e. exactly what is being sent to the
loudspeaker. ``pw-record -P '{ stream.capture.sink=true }'`` taps it, 16 kHz mono s16,
ahead of the room by however long the speaker takes to play what it is given. From that
stream we keep three things, at 50 frames/s:

* ``active`` -- the voice is audible right now (energy above an adaptive noise floor,
  with a short hangover so consonant gaps do not read as silence);
* ``onset_count`` -- the number of syllable/word onsets so far (a rise out of a ≥40 ms
  dip, or a sharp energy jump), which is what the talking beats are paced on;
* ``level`` -- the normalised envelope (auto-gain), which the antennas follow.

Everything here is BEST-EFFORT and fail-safe: no ``pw-record`` (a dev box, the
simulator), no sink, a dead stream -- ``available`` reads False and the player falls
back to the fixed pacing it always had. It taps the SPEAKER, never the microphone:
the signal is the robot's own voice and nothing a person says ever passes through
here.

Nothing in this module touches the robot or the session; it is a sensor.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import statistics
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional

from emori_adapter import motion_trace

log = logging.getLogger("emori.hostapp.voice_link")

try:  # numpy is present on the robot (2.5.1) and in dev; the pure-python path is the fallback
    import numpy as _np
except Exception:  # noqa: BLE001 - optional accelerator only
    _np = None

_RATE = 16000
_FRAME_MS = 20
_FRAME_SAMPLES = _RATE * _FRAME_MS // 1000        # 320
_FRAME_BYTES = _FRAME_SAMPLES * 2                  # s16 mono
# Speech must clear max(3x the tracked noise floor, this absolute RMS) -- ~ -55 dBFS on
# the int16 scale, well below the quietest TTS word and above the stream's dither.
_ABS_MIN_RMS = 60.0
_NOISE_RATIO = 3.0
# The voice stays 'active' this long after the last energetic frame: consonant gaps and
# the dip between two words are ~30-120 ms, a real pause is longer.
_HANGOVER_S = 0.20
# An onset is a rise out of a dip of at least this many inactive frames (40 ms), or a
# sharp jump in energy while already voiced (a stressed syllable), never closer together
# than the refractory so one word cannot fire twice.
_ONSET_DIP_FRAMES = 2
_ONSET_JUMP_RATIO = 2.2
_ONSET_REFRACTORY_S = 0.12
# Auto-gain for ``level``: a running peak that decays slowly so a whole sentence reads
# 0..1, never pinned at 1 by the first loud word.
_PEAK_DECAY = 0.995
_RETRY_BACKOFF_S = 2.0
_MAX_RETRIES = 10
# How many gaps between frames the status reads, about two seconds of them.
_FRAME_GAP_WINDOW = 100


def _rms(frame: bytes) -> float:
    """RMS of one s16 little-endian mono frame."""

    if len(frame) < 2:
        return 0.0
    if _np is not None:
        arr = _np.frombuffer(frame[: len(frame) - (len(frame) % 2)], dtype="<i2").astype("float32")
        if arr.size == 0:
            return 0.0
        return float(math.sqrt(float(_np.mean(arr * arr))))
    n = len(frame) // 2
    total = 0.0
    for i in range(n):
        lo = frame[2 * i]
        hi = frame[2 * i + 1]
        v = (hi << 8) | lo
        if v >= 32768:
            v -= 65536
        total += v * v
    return math.sqrt(total / n) if n else 0.0


def default_enabled() -> bool:
    """``EMORI_VOICE_LINK`` (default on): the one kill-switch for the tap."""

    raw = (os.environ.get("EMORI_VOICE_LINK", "") or "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


# The sink deploy/robot/voice-delay.sh puts in front of the speaker when a delay is set.
VOICE_DELAY_SINK = "emori_voice"


def voice_delay_ms() -> int:
    """``EMORI_VOICE_DELAY_MS``: unset, 0 or unreadable is no delay."""

    try:
        return max(0, int(os.environ.get("EMORI_VOICE_DELAY_MS", "") or 0))
    except ValueError:
        return 0


def pw_record_command(sink: str = "") -> list[str]:
    """The exact capture command: the default sink's monitor unless a sink is named.

    ``stream.capture.sink=true`` is PipeWire's documented way to record what a sink
    is PLAYING rather than a microphone; ``--raw -`` streams headerless s16 to stdout
    (a WAV header would be written once at the end, which never comes).
    """

    cmd = ["pw-record", "-P", "{ stream.capture.sink=true }",
           "--rate", str(_RATE), "--channels", "1", "--format", "s16",
           "--latency", "20ms", "--raw"]
    if sink:
        cmd += ["--target", sink]
    cmd.append("-")
    return cmd


def tap_command(sink: str = "", *, which: Callable[[str], Optional[str]] = shutil.which) -> list[str]:
    """What the tap runs: :func:`pw_record_command`, with nothing held back on the way.

    pw-record writes through stdio, which holds 4096 bytes when stdout is a pipe: 128 ms at this
    rate. The voice would then read up to 128 ms late, and the seven frames of a block would all
    carry the same time. ``stdbuf -o0`` takes that buffer out, so the audio arrives with each
    PipeWire cycle. It comes with coreutils, so its absence is a hand-made machine, where the tap
    still works and is later.
    """

    cmd = pw_record_command(sink)
    if which("stdbuf"):
        return ["stdbuf", "-o0", *cmd]
    log.warning("voice link: no stdbuf here, so the voice reads up to 128 ms late")
    return cmd


class VoiceLink:
    """Live envelope of the robot's own speaker output. See the module docstring."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        enabled: Optional[bool] = None,
        sink: Optional[str] = None,
        spawn: Optional[Callable[[list[str]], subprocess.Popen]] = None,
        which: Callable[[str], Optional[str]] = shutil.which,
    ) -> None:
        self._clock = clock
        self._enabled = default_enabled() if enabled is None else bool(enabled)
        self._sink = (sink if sink is not None else os.environ.get("EMORI_VOICE_LINK_SINK", "")) or ""
        if not self._sink and sink is None and voice_delay_ms() > 0:
            self._sink = VOICE_DELAY_SINK
        self._spawn = spawn
        self._which = which
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        # --- signal state (all under _cv) ---
        self._available = False
        self._level = 0.0
        self._rms = 0.0
        self._noise = _ABS_MIN_RMS
        self._peak = _ABS_MIN_RMS * 3.0
        self._frame_active = False
        self._traced_active = False
        self._inactive_run = 0
        self._last_active_at: Optional[float] = None
        self._onsets = 0
        self._last_onset_at: Optional[float] = None
        # Recent rises out of a dip, which is how a sentence starts: a syllable inside a word
        # jumps, and only a word or a sentence comes up from silence.
        self._rises: deque[float] = deque(maxlen=16)
        self._frames = 0
        self._last_frame_at: Optional[float] = None
        self._frame_gaps: deque[float] = deque(maxlen=_FRAME_GAP_WINDOW)
        self._prev_rms = 0.0
        self._warned = False

    # --- lifecycle ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> bool:
        """Start the tap. False (and a single WARNING) when it cannot run here."""

        if not self._enabled:
            return False
        if self._thread is not None:
            return self._available
        if self._spawn is None and not self._which("pw-record"):
            self._warn_once("voice link unavailable: pw-record not installed; fixed pacing")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voice-link", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        with self._cv:
            self._available = False
            self._cv.notify_all()

    def join(self, *, timeout_s: float = 1.0) -> bool:
        t = self._thread
        if t is None or threading.current_thread() is t:
            return True
        t.join(timeout=max(0.0, timeout_s))
        return not t.is_alive()

    def _run(self) -> None:
        retries = 0
        cmd = tap_command(self._sink, which=self._which)
        while not self._stop.is_set():
            try:
                proc = (self._spawn(cmd) if self._spawn is not None else subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0))
            except Exception as e:  # noqa: BLE001 - the tap is optional
                self._warn_once(f"voice link unavailable: {type(e).__name__}; fixed pacing")
                return
            self._proc = proc
            out = proc.stdout
            got_any = False
            pending = bytearray()
            try:
                while not self._stop.is_set():
                    chunk = out.read(_FRAME_BYTES) if out is not None else b""
                    if not chunk:
                        break
                    if not got_any:
                        got_any = True
                        with self._cv:
                            self._available = True
                            self._cv.notify_all()
                        log.warning("voice link: tapping the speaker output (%s)",
                                    self._sink or "default sink")
                    # A read returns what the last cycle wrote, which is rarely one frame, and
                    # every count of frames here means 20 ms each.
                    pending += chunk
                    whole = len(pending) - len(pending) % _FRAME_BYTES
                    if whole:
                        self.feed(bytes(pending[:whole]))
                        del pending[:whole]
            except Exception:  # noqa: BLE001 - a read error is a restart, never a crash
                pass
            finally:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                self._proc = None
                with self._cv:
                    self._available = False
                    self._cv.notify_all()
            if self._stop.is_set():
                return
            # What gives up is a capture that never delivers. One that worked and died later is a
            # PipeWire restart, and a robot sees those over its whole uptime.
            retries = 0 if got_any else retries + 1
            if retries > _MAX_RETRIES:
                self._warn_once("voice link: capture keeps dying; giving up (fixed pacing)")
                return
            if self._stop.wait(_RETRY_BACKOFF_S):
                return

    def _warn_once(self, message: str) -> None:
        if self._warned:
            return
        self._warned = True
        log.warning(message)

    # --- the signal ---------------------------------------------------------
    def feed(self, data: bytes, *, now: Optional[float] = None) -> None:
        """Process raw s16 mono audio. Public so tests (and any source) can drive it."""

        if not data:
            return
        for off in range(0, len(data) - _FRAME_BYTES + 1, _FRAME_BYTES):
            self._frame(data[off:off + _FRAME_BYTES], now)
        tail = len(data) % _FRAME_BYTES
        if tail >= 64 and len(data) < _FRAME_BYTES:
            # A short final chunk still carries energy information (tests, pipe ends).
            self._frame(data, now)

    def _frame(self, frame: bytes, now: Optional[float]) -> None:
        rms = _rms(frame)
        t = self._clock() if now is None else now
        with self._cv:
            self._frames += 1
            if self._last_frame_at is not None:
                self._frame_gaps.append(t - self._last_frame_at)
            self._last_frame_at = t
            # Noise floor: follows quiet quickly, rises only slowly -- a loud voice must
            # never drag the floor up to itself.
            if rms < self._noise * 1.5:
                self._noise = max(_ABS_MIN_RMS * 0.5, 0.9 * self._noise + 0.1 * rms)
            else:
                self._noise = min(self._noise * 1.01 + 0.5, rms)
            thr = max(self._noise * _NOISE_RATIO, _ABS_MIN_RMS)
            frame_active = rms > thr
            onset = False
            if frame_active:
                rise_from_dip = self._inactive_run >= _ONSET_DIP_FRAMES or self._last_active_at is None
                jump = self._prev_rms > 0.0 and rms > self._prev_rms * _ONSET_JUMP_RATIO and rms > thr * 2.0
                if (rise_from_dip or jump) and (
                        self._last_onset_at is None or t - self._last_onset_at >= _ONSET_REFRACTORY_S):
                    onset = True
                self._inactive_run = 0
                self._last_active_at = t
            else:
                self._inactive_run += 1
            # Auto-gain envelope.
            self._peak = max(thr * 3.0, self._peak * _PEAK_DECAY, rms)
            self._level = min(1.0, rms / self._peak) if frame_active else max(0.0, self._level * 0.7)
            self._rms = rms
            self._prev_rms = rms
            self._frame_active = frame_active
            if onset:
                self._onsets += 1
                self._last_onset_at = t
                if rise_from_dip:
                    self._rises.append(t)
                    motion_trace.emit("voice", at=t, edge="onset", kind="dip")
            is_active = self._active_locked(t)
            if is_active != self._traced_active:
                self._traced_active = is_active
                if is_active:
                    motion_trace.emit("voice", at=t, edge="rise")
                else:
                    motion_trace.emit("voice", at=t, edge="fall", last=self._last_active_at)
            self._cv.notify_all()

    @property
    def available(self) -> bool:
        with self._cv:
            return self._available

    @property
    def active(self) -> bool:
        """True while the voice is audible (with the hangover)."""

        with self._cv:
            return self._active_locked(self._clock())

    def _active_locked(self, t: float) -> bool:
        return self._last_active_at is not None and (t - self._last_active_at) <= _HANGOVER_S

    @property
    def level(self) -> float:
        with self._cv:
            return self._level

    @property
    def onset_count(self) -> int:
        with self._cv:
            return self._onsets

    def silent_for_s(self) -> float:
        """Seconds since the voice was last audible (inf if never)."""

        with self._cv:
            if self._last_active_at is None:
                return math.inf
            return max(0.0, self._clock() - self._last_active_at)

    def rose_since(self, t0: float) -> bool:
        """Did the voice come up out of a dip at or after ``t0``?"""

        with self._cv:
            return bool(self._rises) and self._rises[-1] >= t0

    def wait_for_frame(self, timeout_s: float) -> None:
        """Wait for the next audio frame, 20 ms at most while the tap is delivering."""

        with self._cv:
            self._cv.wait(max(0.0, timeout_s))

    def active_since(self, t0: float) -> bool:
        """Was the voice audible at any moment at or after ``t0``?"""

        with self._cv:
            return self._last_active_at is not None and self._last_active_at >= t0

    def wait_active(self, timeout_s: float, *, stop: Optional[threading.Event] = None) -> bool:
        """Block until the voice is audible (True) or the timeout passes (False)."""

        deadline = self._clock() + max(0.0, timeout_s)
        with self._cv:
            while True:
                if self._active_locked(self._clock()):
                    return True
                if not self._available or (stop is not None and stop.is_set()):
                    return False
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.05))

    def wait_for_onset(self, timeout_s: float, *, after: Optional[int] = None,
                       stop: Optional[threading.Event] = None,
                       abort: Optional[Callable[[], bool]] = None) -> bool:
        """Block until an onset beyond ``after`` (default: the current count) lands.

        Returns True on an onset, False on timeout / stop / ``abort()`` / the tap
        going away. Callers pass ``abort`` so a preempted plan stops waiting at once.
        """

        deadline = self._clock() + max(0.0, timeout_s)
        with self._cv:
            base = self._onsets if after is None else after
            while True:
                if self._onsets > base:
                    return True
                if not self._available or (stop is not None and stop.is_set()):
                    return False
                if abort is not None:
                    self._cv.release()
                    try:
                        if abort():
                            return False
                    finally:
                        self._cv.acquire()
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.05))

    def snapshot(self) -> dict:
        """Status-page view: no audio, just the live shape of the signal."""

        with self._cv:
            t = self._clock()
            return {
                "enabled": self._enabled,
                "available": self._available,
                "active": self._active_locked(t),
                "level": round(self._level, 3),
                "rms": round(self._rms, 1),
                "noise_rms": round(self._noise, 1),
                "onsets": self._onsets,
                "frames": self._frames,
                # About 20 when frames arrive as they are made, and about 0 when they arrive in
                # blocks.
                "frame_gap_ms": (round(statistics.median(self._frame_gaps) * 1000.0, 1)
                                 if self._frame_gaps else None),
                "silent_for_s": (None if self._last_active_at is None
                                 else round(max(0.0, t - self._last_active_at), 2)),
            }
