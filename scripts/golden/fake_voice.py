"""A voice with scripted onsets, for runs in the simulator where nothing plays.

The player only learns about the robot's voice through ``VoiceLink``'s methods, so this has
the same ones and answers them from a schedule instead of a PipeWire tap. With no voice a
speech plan holds for its whole bound; with this one it performs, sustains, lingers and is
cut the way it would be on the robot.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Iterable, Optional

_SYLLABLE_S = 0.2
_LEVELS = (0.35, 0.8, 0.55, 1.0)


class FakeVoice:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 syllable_s: float = _SYLLABLE_S) -> None:
        self._clock = clock
        self._syllable_s = syllable_s
        self._cv = threading.Condition()
        self._speaking = False
        self._last_active: Optional[float] = None
        self._rise: Optional[float] = None
        self._onsets = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- the script ---------------------------------------------------------------------
    def rise(self) -> None:
        """The voice comes up out of silence."""

        now = self._clock()
        with self._cv:
            self._speaking = True
            self._last_active = now
            self._rise = now
            self._onsets += 1
            self._cv.notify_all()

    def syllable(self) -> None:
        with self._cv:
            self._last_active = self._clock()
            self._onsets += 1
            self._cv.notify_all()

    def hush(self) -> None:
        with self._cv:
            self._last_active = self._clock()
            self._speaking = False
            self._cv.notify_all()

    def play(self, spans: Iterable[tuple[float, float]], *, at: Optional[float] = None) -> None:
        """Speak each ``(start_s, end_s)``, measured from ``at``. Returns at once."""

        origin = self._clock() if at is None else at
        ordered = sorted(spans)
        self._stop.clear()
        self._thread = threading.Thread(target=self._perform, args=(origin, ordered),
                                        name="fake-voice", daemon=True)
        self._thread.start()

    def _wait_until(self, moment: float) -> bool:
        left = moment - self._clock()
        return left > 0 and self._stop.wait(left)

    def _perform(self, origin: float, spans: list[tuple[float, float]]) -> None:
        for start_s, end_s in spans:
            if self._wait_until(origin + start_s):
                return
            self.rise()
            next_syllable = self._clock() + self._syllable_s
            end = origin + end_s
            while next_syllable < end:
                if self._wait_until(next_syllable):
                    self.hush()
                    return
                self.syllable()
                next_syllable += self._syllable_s
            if self._wait_until(end):
                self.hush()
                return
            self.hush()

    def wait_done(self, timeout_s: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout_s)
        return not thread.is_alive()

    # --- the VoiceLink surface ----------------------------------------------------------
    enabled = True
    available = True

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        self._stop.set()

    def join(self, *, timeout_s: float = 1.0) -> bool:
        return self.wait_done(timeout_s)

    @property
    def level(self) -> float:
        with self._cv:
            return _LEVELS[self._onsets % len(_LEVELS)]

    @property
    def active(self) -> bool:
        with self._cv:
            return self._speaking

    @property
    def onset_count(self) -> int:
        with self._cv:
            return self._onsets

    def _touch_locked(self) -> None:
        if self._speaking:
            self._last_active = self._clock()

    def silent_for_s(self) -> float:
        with self._cv:
            self._touch_locked()
            if self._last_active is None:
                return math.inf
            return max(0.0, self._clock() - self._last_active)

    def rose_since(self, t0: float) -> bool:
        with self._cv:
            return self._rise is not None and self._rise >= t0

    def active_since(self, t0: float) -> bool:
        with self._cv:
            self._touch_locked()
            return self._last_active is not None and self._last_active >= t0

    def wait_for_frame(self, timeout_s: float) -> None:
        with self._cv:
            self._cv.wait(min(timeout_s, 0.02))

    def wait_active(self, timeout_s: float, *, stop: Optional[threading.Event] = None) -> bool:
        deadline = self._clock() + timeout_s
        with self._cv:
            while not self._speaking:
                left = deadline - self._clock()
                if left <= 0 or (stop is not None and stop.is_set()):
                    return False
                self._cv.wait(min(0.05, left))
            return True

    def wait_for_onset(self, timeout_s: float, *, after: Optional[int] = None,
                       stop: Optional[threading.Event] = None,
                       abort: Optional[Callable[[], bool]] = None) -> bool:
        deadline = self._clock() + timeout_s
        with self._cv:
            base = self._onsets if after is None else after
            while self._onsets <= base:
                if abort is not None:
                    self._cv.release()
                    try:
                        if abort():
                            return False
                    finally:
                        self._cv.acquire()
                left = deadline - self._clock()
                if left <= 0 or (stop is not None and stop.is_set()):
                    return False
                self._cv.wait(min(0.05, left))
            return True

    def snapshot(self) -> dict:
        with self._cv:
            return {"enabled": True, "available": True, "active": self._speaking,
                    "level": _LEVELS[self._onsets % len(_LEVELS)], "onsets": self._onsets}
