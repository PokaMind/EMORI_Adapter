"""A one-shot place for the page a kiosk is to be sent to on its next visit to ``/``.

Something outside the browser that wants a session started has no way to drive the page. It
can mint the session and queue the landing page's ``reload`` verb, and the reload is a plain
``GET /``. This slot is what that request finds: if a target is waiting, ``/`` answers with a
redirect to it instead of rendering the landing page.

Nothing puts anything here unless a caller asked, so with nothing asked the slot is empty and
``/`` is exactly what it was. A target is taken once, and it expires: a page that never
reloaded must not turn some later, unrelated visit to ``/`` into the start of a session. A
target nobody collected is still a minted session that somebody has to undo, so
:meth:`StartSlot.abandoned` hands it back once.

:class:`LandingSeen` is how a caller knows the kiosk is free to be sent anywhere. The landing
page says what it is doing each time it polls for ``reload``.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

__all__ = ["LANDING_FRESH_S", "LANDING_IDLE_PHASE", "LandingSeen", "StartSlot", "TTL_S"]

#: Long enough for the landing page's reload poll to find the verb, short enough that a
#: target nobody collected is gone before anyone could mistake it for a request.
TTL_S = 30.0

_PREFIX = "/session"

#: The landing page polls every 2 s. Older than this, nobody can say the page is still there.
LANDING_FRESH_S = 5.0
#: The page's phase when nobody is using it: the camera looks for a badge and the mic is shut.
LANDING_IDLE_PHASE = "scan"
_LANDING_PAGES_MAX = 32
_LANDING_PAGE_ID_MAX = 32


class LandingSeen:
    """What each open landing page last said it was doing, and when.

    Busy wins: a second tab left on ``/`` says it is scanning, and that must not hide a kiosk
    reading its menu to someone. A stray tab can make the robot look busy, the safe way round.
    """

    def __init__(self, fresh_s: float = LANDING_FRESH_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fresh_s = fresh_s
        self._clock = clock
        self._lock = threading.Lock()
        self._pages: dict[str, tuple[str, float]] = {}
        self._held_until = float("-inf")

    def note(self, phase: str, page: str = "") -> None:
        now = self._clock()
        with self._lock:
            self._pages = {name: seen for name, seen in self._pages.items()
                           if now - seen[1] < self._fresh_s}
            if len(self._pages) >= _LANDING_PAGES_MAX:
                self._pages.clear()
            self._pages[page[:_LANDING_PAGE_ID_MAX]] = (phase, now)

    def hold(self, seconds: float) -> None:
        """Say busy for this long, for a line the robot plays itself with no page involved."""

        self._held_until = self._clock() + seconds

    def idle(self) -> bool:
        """True only while the kiosk is on the landing page and nobody is using it."""

        now = self._clock()
        if now < self._held_until:
            return False
        with self._lock:
            fresh = [phase for phase, when in self._pages.values()
                     if now - when < self._fresh_s]
        return bool(fresh) and all(phase == LANDING_IDLE_PHASE for phase in fresh)


class StartSlot:
    """At most one target, handed out once."""

    def __init__(self, ttl_s: float = TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._target: Optional[str] = None
        self._expires = 0.0
        self._leased = False
        self._abandoned: Optional[str] = None

    def put(self, target: str, *, leased: bool = False) -> None:
        """Hold ``target``, replacing any other. Only a path on this origin's ``/session``.

        ``leased`` says the mint claimed a badge, so the add-on's reservation is this start's own.
        """

        if (not isinstance(target, str) or not target.startswith(_PREFIX)
                or target[len(_PREFIX):len(_PREFIX) + 1] not in ("", "?", "/")
                or any(ch in target for ch in "\r\n\\")):
            raise ValueError("the start slot holds a /session path and nothing else")
        with self._lock:
            self._target = target
            self._expires = self._clock() + self._ttl_s
            self._leased = leased
            self._abandoned = None

    def take(self) -> Optional[str]:
        """The waiting target, once, or None when there is none or it ran out."""

        with self._lock:
            self._expire()
            target, self._target = self._target, None
            return target

    def clear(self) -> None:
        """Somebody else's session is opening: what is waiting will not be collected."""

        with self._lock:
            if self._target is not None:
                self._abandoned = self._target
            self._target = None

    def abandoned(self) -> Optional[str]:
        """A target that ran out or was cleared without being taken, once, or None."""

        with self._lock:
            self._expire()
            target, self._abandoned = self._abandoned, None
            return target

    def _expire(self) -> None:
        if self._target is not None and self._clock() >= self._expires:
            self._abandoned, self._target = self._target, None

    def leased(self) -> bool:
        with self._lock:
            return self._target is not None and self._leased

    def waiting(self) -> bool:
        """Whether a target is held and still good. Does not take it."""

        with self._lock:
            return self._target is not None and self._clock() < self._expires
