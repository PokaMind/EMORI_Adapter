"""One executor on its own thread, spun so that a failure is either survived or seen.

``Executor.spin`` ends at the first exception a callback raises, and a thread that ends
says nothing: the process goes on answering HTTP with a graph side that no longer turns
over. So the hub does not use it. :class:`SupervisedSpin` calls ``spin_once`` in a loop:

* A callback that raises costs that callback. It is logged, at a bounded rate, and the
  next one still runs.
* An executor that only ever raises is a broken one. With ``fault_limit`` set, that many
  faults in a row with no clean spin between them end the loop. Leave it unset where
  callbacks are other people's code, because a timer that raises on every tick looks the
  same from here and must not take the process down.
* However the loop ends, :meth:`SupervisedSpin.death` says so unless somebody asked for it,
  including when the thread died of something this code never saw. That is what a
  supervisor polls.

Nothing here imports ROS: an executor is anything with ``spin_once``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

__all__ = ["COMPLAINT_PERIOD_S", "FAULT_PAUSE_S", "SPIN_SLICE_S", "SupervisedSpin"]

log = logging.getLogger("emori.hub.spin")

#: How long one ``spin_once`` waits for work, which is also how soon a stop is noticed.
SPIN_SLICE_S = 0.25
#: After a fault the loop waits this long, so an executor that fails at once cannot make it
#: a busy one.
FAULT_PAUSE_S = 0.05
#: A repeated fault is logged at most this often, with a count of the ones left out.
COMPLAINT_PERIOD_S = 30.0


class SupervisedSpin:
    """Spins ``executor`` on a thread called ``name`` until :meth:`stop`."""

    def __init__(
        self,
        name: str,
        executor: Any,
        *,
        ok: Callable[[], bool],
        fatal: tuple[type[BaseException], ...] = (),
        fault_limit: Optional[int] = None,
        logger: logging.Logger = log,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._executor = executor
        self._ok = ok
        self._fatal = fatal
        self._fault_limit = fault_limit
        self._log = logger
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ended_because = ""
        self._complained_at: float | None = None
        self._left_out = 0

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def death(self) -> str:
        """Why this spin ended with nobody asking; empty while it runs or once it was told to."""

        if self._stop.is_set() or self._thread is None:
            return ""
        if self._ended_because:
            return self._ended_because
        return "" if self._thread.is_alive() else "its thread ended"

    def stop(self, timeout: float = 2.0) -> bool:
        """Ask the loop to end and wait for it. False when it is still in there."""

        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        if thread.is_alive():
            self._log.warning("%s did not stop within %g s", self.name, timeout)
            return False
        return True

    def _run(self) -> None:
        faults = 0
        while not self._stop.is_set():
            if not self._ok():
                self._ended_because = "the ROS context went away"
                return
            try:
                self._executor.spin_once(timeout_sec=SPIN_SLICE_S)
            except self._fatal:
                self._ended_because = "the ROS context went away"
                return
            except Exception as error:  # noqa: BLE001 - this is the point of the class
                faults += 1
                self._complain(error)
                if self._fault_limit is not None and faults >= self._fault_limit:
                    self._ended_because = f"its executor raised {faults} times in a row"
                    return
                self._stop.wait(FAULT_PAUSE_S)
            else:
                faults = 0

    def _complain(self, error: Exception) -> None:
        now = self._clock()
        if self._complained_at is not None and now - self._complained_at < COMPLAINT_PERIOD_S:
            self._left_out += 1
            return
        self._log.warning("%s: the executor raised; carrying on (%d not logged since the last)",
                          self.name, self._left_out, exc_info=error)
        self._complained_at = now
        self._left_out = 0
