"""A pass-through :class:`RobotClient` wrapper that records every call.

``TracingRobotClient`` wraps any robot client and appends one JSON line per call to a
:class:`TraceSink`: the method, ``t_start`` (``time.monotonic()`` when the call began),
the call's duration, its arguments (a :class:`MotionIntent` in full, including
``reason``) and its return value or the exception it raised.

``reason`` is recorded here because it is the one place it survives: the REST client puts
none on the wire, and a different client behind the same interface may substitute its own.
Comparing two stacks therefore compares what the driver asked for, not how each transport
spelled it.

The wrapper changes nothing: arguments go through untouched, return values and exceptions
come back untouched, and a failure of the trace itself (full disk, closed file) is
swallowed so it can never reach the caller. Methods the driver resolves by name
(``play_recorded_move``, ``stop_recorded_move``, ``set_gaze``, ``set_head_reserved``)
are looked up on the wrapped client at call time, so a client that lacks one still lacks
it through the wrapper.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from emori_adapter.models import MotionIntent

log = logging.getLogger("emori.robot.tracing")


class TraceSink:
    """Thread-safe JSON-lines writer. Without a path, records are kept in memory only.

    With a path nothing is kept, so a long recording cannot grow, and writes reach the disk
    once a second at most, so the calls being timed do not wait on it.
    """

    _FLUSH_S = 1.0

    def __init__(self, path: str | Path | None = None) -> None:
        self.records: list[dict] = []
        self._lock = threading.Lock()
        self._fh = None
        self._failed = False
        self._flushed_at = 0.0
        if path is not None:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._fh = p.open("a", encoding="utf-8")

    def write(self, record: dict) -> None:
        with self._lock:
            if self._fh is None:
                self.records.append(record)
                return
            try:
                self._fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
                now = time.monotonic()
                if now - self._flushed_at >= self._FLUSH_S:
                    self._fh.flush()
                    self._flushed_at = now
            except Exception:  # noqa: BLE001 - a trace is never worth a failed robot call
                if not self._failed:
                    self._failed = True
                    log.warning("trace write failed", exc_info=True)

    def close(self) -> None:
        with self._lock:
            fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, MotionIntent):
        return value.model_dump(mode="json")
    return value


class TracingRobotClient:
    """Implements the whole ``RobotClient`` protocol plus the recorded-move pair."""

    def __init__(self, inner: Any, sink: TraceSink | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._inner = inner
        self.sink = sink if sink is not None else TraceSink()
        self._clock = clock

    @property
    def inner(self) -> Any:
        return self._inner

    def _call(self, method: str, args: dict, *call_args: Any, **call_kwargs: Any) -> Any:
        fn = getattr(self._inner, method)  # a missing method reaches the caller, untraced
        t_start = self._clock()
        try:
            ret = fn(*call_args, **call_kwargs)
        except BaseException as exc:
            self._record(method, t_start, args, error=f"{type(exc).__name__}: {exc}")
            raise
        self._record(method, t_start, args, ret=ret)
        return ret

    def _record(self, method: str, t_start: float, args: dict, *,
                ret: Any = None, error: Optional[str] = None) -> None:
        try:
            rec: dict = {
                "method": method,
                "t_start": t_start,
                "dur_s": self._clock() - t_start,
                "args": {k: _jsonable(v) for k, v in args.items()},
            }
            if error is not None:
                rec["error"] = error
            elif method == "read_state":
                # The tick cadence is what matters; the pose is not carried.
                rec["ret"] = None if ret is None else "state"
            else:
                rec["ret"] = ret
            self.sink.write(rec)
        except Exception:  # noqa: BLE001
            log.debug("trace record failed", exc_info=True)

    # --- RobotClient protocol ---
    def health(self) -> bool:
        return self._call("health", {})

    def wake_up(self) -> None:
        return self._call("wake_up", {})

    def set_motor_mode(self, mode: str) -> None:
        return self._call("set_motor_mode", {"mode": mode}, mode)

    def apply(self, intent: MotionIntent) -> None:
        return self._call("apply", {"intent": intent}, intent)

    def read_state(self) -> Optional[dict]:
        return self._call("read_state", {})

    def set_gaze(self, enabled: bool) -> bool:
        return self._call("set_gaze", {"enabled": enabled}, enabled)

    def set_head_reserved(self, reserved: bool) -> bool:
        return self._call("set_head_reserved", {"reserved": reserved}, reserved)

    def close(self) -> None:
        try:
            return self._call("close", {})
        finally:
            self.sink.close()

    # --- recorded moves (resolved by name by the player) ---
    def play_recorded_move(self, name: str, *, supersede: bool = False) -> str | None:
        # ``supersede`` is forwarded only when set, so a wrapped client whose method
        # does not take it keeps working.
        if supersede:
            return self._call("play_recorded_move", {"name": name, "supersede": True},
                              name, supersede=True)
        return self._call("play_recorded_move", {"name": name}, name)

    def stop_recorded_move(self, uuid: str) -> None:
        return self._call("stop_recorded_move", {"uuid": uuid}, uuid)

    def __getattr__(self, name: str) -> Any:
        # Anything else (a fake's inspection attributes, for one) is read from the
        # wrapped client.
        if name.startswith("__") or name in ("_inner", "sink", "_clock"):
            raise AttributeError(name)
        return getattr(self._inner, name)
