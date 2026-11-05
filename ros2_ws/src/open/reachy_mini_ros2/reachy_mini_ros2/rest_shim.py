"""The REST compatibility shim: Pollen's daemon dialect, served by the ROS 2 node.

v2 deletes the `reachy_mini` daemon process and gives the robot's backend to
:mod:`reachy_mini_ros2.robot_node` instead. The EMORI hostapp, however, speaks exactly
one robot protocol — the daemon's REST API, through
``emori_adapter.robot.client.HttpRobotClient`` — and that client is the single writer
for every gesture EMORI has ever shipped. Rewriting it as a ROS client on the same day
we move the backend would change two things at once and leave no way to tell which one
broke the robot.

So the node keeps answering on ``http://127.0.0.1:8000`` with the daemon's own routes,
field names and status codes. The hostapp cannot tell the difference; ``REACHY_BASE_URL``
never changes; the migration to a native ROS client becomes a later, separate decision.
The one deliberate tell is ``GET /api/daemon/status`` → ``backend: "reachy_mini_ros2"``,
so tooling (the rehearsal harness) can refuse to run against a *real* daemon squatting
on the port.

Three design rules hold this together:

* **Every route is a call onto the arbiter.** The shim owns no motion policy. Lanes,
  preemption, drop-if-busy, head reservation and the owner lock live in
  :mod:`reachy_mini_ros2.arbiter`, so the hostapp (one client, named ``hostapp``) and
  the ROS graph are arbitrated by the same rules rather than racing each other.
* **No handler ever awaits a backend coroutine.** The backend's moves run on the
  arbiter's own asyncio loop in its own thread; every method this module calls
  (``submit``/``stop``/``running``/``set_target``/``set_motor_mode`` and
  ``backend_host.present_*``) is thread-safe and returns immediately. The handlers are
  plain ``def``, so Starlette runs them in its threadpool and the HTTP loop is never
  blocked by a 2 s goto.
* **Pollen's own pydantic models where they exist.** ``FullState``, ``FullBodyTarget``
  and ``XYZRPYPose``/``Matrix4x4Pose`` are imported from ``reachy_mini.daemon.app.models``
  so the wire shape cannot drift from the daemon we are imitating. The handful of models
  Pollen declares *inside its routers* (``GotoModelRequest``, ``MoveUUID``,
  ``MotorStatus``, ``PlaySoundRequest``) are re-declared here with identical field names,
  because importing those router modules would drag FastAPI dependencies, huggingface_hub
  and the whole backend package into the node for four field definitions.

Headers (both optional, both documented for a future hostapp):

* ``X-Reachy-Client`` — overrides the arbiter ``client`` name (default ``hostapp``).
  The owner lock and the per-client counters key on it.
* ``X-Reachy-Lane`` — overrides the arbiter lane (default ``interaction``). A hostapp
  that learns to label its expressive motion sends ``expression`` here and becomes
  droppable; safety-critical recentring keeps the default.

Units on the wire are the daemon's: metres, radians, and antennas as ``[right, left]``.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

__all__ = ["RestShim", "build_app", "start_rest_shim"]

DEFAULT_CLIENT = "hostapp"
DEFAULT_LANE = "interaction"

# --- Pollen's wire models, and the state reads --------------------------------------
# They live in `state_snapshot`, which the ROS services share. The shim is also loaded by
# path (the tests, the route probes) with no package around it, so a failed relative
# import falls back to loading the sibling file under one shared module name.


def _load_state_snapshot() -> Any:
    try:
        from . import state_snapshot as loaded

        return loaded
    except ImportError:
        import importlib.util
        import sys
        from pathlib import Path

        name = "reachy_mini_ros2_state_snapshot_by_path"
        loaded = sys.modules.get(name)
        if loaded is None:
            sibling = Path(__file__).with_name("state_snapshot.py")
            if not sibling.is_file():
                raise     # ImportError, which the node handles: it comes up without the shim
            spec = importlib.util.spec_from_file_location(name, sibling)
            loaded = importlib.util.module_from_spec(spec)
            # Registered before execution: `@dataclass` resolves annotations through
            # `sys.modules[cls.__module__]`.
            sys.modules[name] = loaded
            spec.loader.exec_module(loaded)
        return loaded


_state = _load_state_snapshot()
BACKEND_MARKER = _state.BACKEND_MARKER
SHIM_VERSION = _state.SHIM_VERSION
AnyPose = _state.AnyPose
FullBodyTarget = _state.FullBodyTarget
Matrix4x4Pose = _state.Matrix4x4Pose
XYZRPYPose = _state.XYZRPYPose
_import_sibling = _state.import_sibling
_conversions = _state.pose_conversions
_local_xyzrpy_to_matrix = _state._local_xyzrpy_to_matrix
_local_matrix_to_xyzrpy = _state._local_matrix_to_xyzrpy
_mode_value = _state.mode_value
_rpy_of = _state.rpy_of

try:  # pragma: no cover - environment-dependent
    from reachy_mini.io.protocol import MotorControlMode

    MOTOR_MODES: tuple[str, ...] = tuple(m.value for m in MotorControlMode)
except Exception:  # pragma: no cover - reachy_mini absent
    MOTOR_MODES = ("enabled", "disabled", "gravity_compensation")

try:  # pragma: no cover - environment-dependent
    from reachy_mini.utils.interpolation import InterpolationTechnique

    INTERPOLATIONS: tuple[str, ...] = tuple(i.value for i in InterpolationTechnique)
except Exception:  # pragma: no cover - reachy_mini absent
    INTERPOLATIONS = ("linear", "minjerk", "ease_in_out", "cartoon")


# --- models Pollen declares inside its routers, re-declared here ---------------------


class GotoModelRequest(BaseModel):
    """``POST /api/move/goto`` body (``daemon/app/routers/move.py``)."""

    head_pose: Optional[AnyPose] = None
    antennas: Optional[tuple[float, float]] = None
    body_yaw: Optional[float] = None
    duration: float
    interpolation: str = "minjerk"

    @field_validator("interpolation")
    @classmethod
    def _known_interpolation(cls, value: str) -> str:
        # The daemon types this field as an enum, so an unknown technique is a 422 there.
        # Keep that: a typo must fail loudly at the boundary instead of reaching the
        # arbiter and becoming a silent linear move.
        if value not in INTERPOLATIONS:
            raise ValueError(f"interpolation must be one of {INTERPOLATIONS}")
        return value


class MoveUUID(BaseModel):
    """``{"uuid": ...}`` — the daemon's move handle.

    Pollen types this ``UUID``; here it is a string, because the id is minted by the
    arbiter and is only ever echoed back to us in ``POST /api/move/stop``. Parsing it as
    a UUID would turn a perfectly valid arbiter id into a 422 and leave a recorded move
    running under the next sentence — the exact failure ``stop_recorded_move`` exists
    to prevent.
    """

    uuid: str


class MotorStatus(BaseModel):
    """``GET /api/motors/status`` (``daemon/app/routers/motors.py``)."""

    mode: str


class PlaySoundRequest(BaseModel):
    """``POST /api/media/play_sound`` (``daemon/app/routers/media.py``)."""

    file: str


# --- EMORI's own two flags, under a namespace Pollen's daemon has never used ---------


class GazeFlag(BaseModel):
    """``POST /api/emori/gaze`` — should the node be looking at faces right now."""

    enabled: bool


class ReservedFlag(BaseModel):
    """``POST /api/emori/head_reserved`` — is a human holding the head right now."""

    reserved: bool


# --- lazily resolved siblings --------------------------------------------------------
# `arbiter` and `conversions` are sibling modules of the node and may not exist
# (or may not be importable — no rclpy on a dev box, no package install under pytest)
# when this module is imported. Resolving them on FIRST USE and falling back to local
# equivalents keeps the shim loadable by path, which is how it gets tested at all.

_request_cls_cache: Optional[type] = None


@dataclass(frozen=True)
class _LocalMotionRequest:
    """Field-for-field copy of ``arbiter.MotionRequest``; used only as a fallback.

    Every construction in this module is by keyword, so the real dataclass drops in
    without caring about field order.
    """

    kind: str
    lane: str = DEFAULT_LANE
    client: str = DEFAULT_CLIENT
    head_pose: Any = None
    antennas: Optional[tuple[float, float]] = None
    body_yaw: Optional[float] = None
    duration: float = 0.5
    interpolation: str = "minjerk"
    dataset: str = ""
    move: str = ""
    preempt: bool = False
    reason: str = ""
    supersede: bool = False


def _motion_request_cls() -> type:
    """``arbiter.MotionRequest`` when importable, else the local copy above."""
    global _request_cls_cache
    if _request_cls_cache is None:
        found = _import_sibling("arbiter", ("MotionRequest",))
        _request_cls_cache = found[0] if found else _LocalMotionRequest
    return _request_cls_cache


# --- small helpers -------------------------------------------------------------------


class _Log:
    """Adapter over stdlib loggers AND rclpy's, which only takes a single string."""

    def __init__(self, log: Any | None) -> None:
        self._log = log if log is not None else logging.getLogger("reachy_mini.rest_shim")

    def _emit(self, level: str, message: str) -> None:
        fn = getattr(self._log, level, None)
        if fn is None:
            return
        try:
            fn(message)
        except Exception:  # noqa: BLE001 - logging must never take the shim down
            pass

    def debug(self, message: str) -> None:
        self._emit("debug", message)

    def info(self, message: str) -> None:
        self._emit("info", message)

    def warning(self, message: str) -> None:
        self._emit("warning", message)


#: Refusal codes that mean "this move does not exist" rather than "not now". The daemon
#: answers 404 for an unknown dataset or move name, and `HttpRobotClient` logs a 404
#: differently from a 409, so the distinction is worth keeping even though the arbiter's
#: own refusal vocabulary has no entry for it.
NOT_FOUND_CODES = ("not_found", "unknown_move", "unknown_dataset", "no_such_move")


def _pose_to_matrix(pose: Any) -> np.ndarray:
    """``AnyPose`` (either representation) -> 4x4, through ``conversions``."""
    flat = getattr(pose, "m", None)
    if flat is not None:
        return np.asarray(flat, dtype=float).reshape((4, 4))
    to_matrix, _ = _conversions()
    return np.asarray(
        to_matrix(
            float(pose.x), float(pose.y), float(pose.z),
            float(pose.roll), float(pose.pitch), float(pose.yaw),
        ),
        dtype=float,
    )


def _refusal(outcome: Any) -> JSONResponse:
    """An arbiter refusal as the HTTP answer the hostapp's client already understands."""
    code = str(getattr(outcome, "error_code", "") or "refused")
    status = 404 if code in NOT_FOUND_CODES else 409
    return JSONResponse(status_code=status, content={"error_code": code, "detail": code})


def _wanted(header: str) -> bool:
    """A flag header: absent, empty or 0 is off."""

    return header.strip() not in ("", "0")


def _traced(reason: str, tag: str) -> str:
    """The caller's own name for a move, kept where the arbiter's trace can show it."""

    return f"{reason}:{tag.strip()[:40]}" if tag.strip() else reason


def _uuid(outcome: Any) -> dict[str, str]:
    return {"uuid": str(getattr(outcome, "move_id", "") or "")}


# --- the app -------------------------------------------------------------------------


def build_app(
    arbiter: Any,
    backend_host: Any,
    *,
    version: str | None = None,
    backend_name: str = BACKEND_MARKER,
    robot_name: str = "reachy_mini",
    wireless_version: bool = False,
    simulation_enabled: bool | None = None,
    mockup_sim_enabled: bool | None = None,
    log: Any | None = None,
    state_cache_s: float = 0.05,
    gaze: Callable[[bool], None] | None = None,
    gaze_active: Callable[[], bool] | None = None,
    face_seen: Callable[[], float | None] | None = None,
) -> FastAPI:
    """Build the FastAPI app. Split out from :func:`start_rest_shim` so it is testable.

    ``gaze`` / ``gaze_active`` are the face look-at component's run switch and its
    liveness, wired in by ``robot_node`` when the component exists. Both ``None`` is a
    node without gaze, and that is the whole compatibility story: the two ``/api/emori``
    routes still exist, and the gaze one answers 503 rather than 404.
    """
    logger = _Log(log)
    app = FastAPI(title="reachy_mini_ros2 REST shim", version=version or SHIM_VERSION)
    state = _state.make_state_snapshot(
        arbiter,
        backend_host,
        version=version,
        backend_name=backend_name,
        robot_name=robot_name,
        wireless_version=wireless_version,
        simulation_enabled=simulation_enabled,
        mockup_sim_enabled=mockup_sim_enabled,
        state_cache_s=state_cache_s,
        gaze_active=gaze_active,
        face_seen=face_seen,
    )
    media_warned: dict[str, bool] = {"said": False}

    def _inert_media(route: str) -> dict[str, str]:
        if not media_warned["said"]:
            media_warned["said"] = True
            logger.info(
                f"REST shim: {route} answered 200 but is inert — v2's node carries no "
                "media server (EMORI's robot speech goes through the kiosk browser)."
            )
        return {"status": "ok"}

    def _submit(kind_: str, client: str, lane: str, **fields: Any) -> Any:
        request_cls = _motion_request_cls()
        return arbiter.submit(request_cls(kind=kind_, lane=lane, client=client, **fields))

    # --- daemon ----------------------------------------------------------------------

    @app.get("/api/daemon/status")
    def daemon_status() -> dict[str, Any]:
        return state.status()

    @app.post("/health-check")
    def health_check() -> dict[str, str]:
        # POST-only on the daemon too (it is a watchdog reset, not a readiness probe);
        # readiness is `GET /api/daemon/status`.
        return {"status": "ok"}

    # --- motors ----------------------------------------------------------------------

    @app.get("/api/motors/status")
    def motor_status() -> MotorStatus:
        return MotorStatus(mode=state.motor_mode())

    @app.post("/api/motors/set_mode/{mode}")
    def set_motor_mode(mode: str) -> Any:
        if mode not in MOTOR_MODES:
            raise HTTPException(status_code=422, detail=f"unknown motor mode {mode!r}")
        outcome = arbiter.set_motor_mode(mode)
        if not getattr(outcome, "accepted", False):
            # 5xx, not 4xx, because this is the shape v1.9.0's firmware refusal has and
            # `HttpRobotClient` latches ANY >=400 per mode. Keeping the status honest
            # means a backend that cannot do gravity_compensation degrades exactly the
            # way the hostapp already expects: attempted once, then never again.
            code = str(getattr(outcome, "error_code", "") or "backend_error")
            logger.warning(f"REST shim: motor mode {mode!r} refused ({code})")
            return JSONResponse(status_code=500, content={"error_code": code})
        return {"status": f"motors changed to {mode} mode"}

    # --- moves -----------------------------------------------------------------------

    @app.post("/api/move/play/wake_up")
    def play_wake_up(
        x_reachy_client: str = Header(default=DEFAULT_CLIENT, alias="X-Reachy-Client"),
        x_reachy_lane: str = Header(default=DEFAULT_LANE, alias="X-Reachy-Lane"),
    ) -> Any:
        outcome = _submit("wake_up", x_reachy_client, x_reachy_lane, reason="wake_up")
        if not getattr(outcome, "accepted", False):
            return _refusal(outcome)
        return _uuid(outcome)

    @app.post("/api/move/play/goto_sleep")
    def play_goto_sleep(
        x_reachy_client: str = Header(default=DEFAULT_CLIENT, alias="X-Reachy-Client"),
        x_reachy_lane: str = Header(default=DEFAULT_LANE, alias="X-Reachy-Lane"),
    ) -> Any:
        outcome = _submit("goto_sleep", x_reachy_client, x_reachy_lane, reason="goto_sleep")
        if not getattr(outcome, "accepted", False):
            return _refusal(outcome)
        return _uuid(outcome)

    @app.post("/api/move/goto")
    def goto(
        goto_req: GotoModelRequest,
        x_reachy_client: str = Header(default=DEFAULT_CLIENT, alias="X-Reachy-Client"),
        x_reachy_lane: str = Header(default=DEFAULT_LANE, alias="X-Reachy-Lane"),
        x_reachy_supersede: str = Header(default="", alias="X-Reachy-Supersede"),
        x_reachy_trace: str = Header(default="", alias="X-Reachy-Trace"),
    ) -> Any:
        head = _pose_to_matrix(goto_req.head_pose) if goto_req.head_pose is not None else None
        antennas = (
            (float(goto_req.antennas[0]), float(goto_req.antennas[1]))
            if goto_req.antennas is not None
            else None
        )
        outcome = _submit(
            "goto",
            x_reachy_client,
            x_reachy_lane,
            head_pose=head,
            antennas=antennas,
            body_yaw=goto_req.body_yaw,
            duration=float(goto_req.duration),
            interpolation=goto_req.interpolation,
            reason=_traced("rest:goto", x_reachy_trace),
            supersede=_wanted(x_reachy_supersede),
        )
        if not getattr(outcome, "accepted", False):
            return _refusal(outcome)
        return _uuid(outcome)

    @app.post("/api/move/set_target")
    def set_target(target: FullBodyTarget) -> dict[str, str]:
        head = (
            _pose_to_matrix(target.target_head_pose)
            if target.target_head_pose is not None
            else None
        )
        antennas = (
            (float(target.target_antennas[0]), float(target.target_antennas[1]))
            if target.target_antennas is not None
            else None
        )
        outcome = arbiter.set_target(
            head_pose=head, antennas=antennas, body_yaw=target.target_body_yaw
        )
        if getattr(outcome, "accepted", False):
            return {"status": "ok"}
        # The daemon's exact answer while a trajectory runs; the arbiter says "busy" for
        # the same situation. A stream that fights a goto is dropped, never queued.
        reason = str(getattr(outcome, "error_code", "") or "refused")
        return {"status": "ignored", "reason": "move_running" if reason == "busy" else reason}

    @app.post("/api/move/play/recorded-move-dataset/{dataset_name:path}/{move_name}")
    def play_recorded_move_dataset(
        dataset_name: str,
        move_name: str,
        x_reachy_client: str = Header(default=DEFAULT_CLIENT, alias="X-Reachy-Client"),
        x_reachy_lane: str = Header(default=DEFAULT_LANE, alias="X-Reachy-Lane"),
        x_reachy_supersede: str = Header(default="", alias="X-Reachy-Supersede"),
        x_reachy_trace: str = Header(default="", alias="X-Reachy-Trace"),
    ) -> Any:
        if not move_name:
            raise HTTPException(status_code=404, detail="move name is required")
        outcome = _submit(
            "play_recorded",
            x_reachy_client,
            x_reachy_lane,
            dataset=dataset_name,
            move=move_name,
            reason=_traced(f"rest:recorded:{move_name}", x_reachy_trace),
            supersede=_wanted(x_reachy_supersede),
        )
        if not getattr(outcome, "accepted", False):
            return _refusal(outcome)
        return _uuid(outcome)

    @app.post("/api/move/stop")
    def stop_move(body: MoveUUID) -> dict[str, str]:
        stopped = int(arbiter.stop(body.uuid))
        if stopped == 0:
            # The client passes the uuid it was given; a 404 here means the move already
            # finished, which is worth a log line but never an error on the hostapp side.
            raise HTTPException(
                status_code=404, detail=f"Running move with UUID {body.uuid} not found"
            )
        return {"message": f"Stopped move with UUID: {body.uuid}"}

    @app.get("/api/move/trace")
    def move_trace(after: int = 0) -> list[dict[str, Any]]:
        return arbiter.trace_since(after)

    @app.get("/api/move/running")
    def running_moves() -> list[dict[str, str]]:
        return [{"uuid": str(move_id)} for move_id in arbiter.running()]

    # --- state -----------------------------------------------------------------------

    @app.get("/api/state/full")
    def full_state(use_pose_matrix: bool = False) -> dict[str, Any]:
        return state.snapshot(use_pose_matrix)

    # --- EMORI extensions ------------------------------------------------------------
    # Two flags the node cannot compute for itself: whether a face look-at is WANTED
    # right now (a training session, or an add-on mode that is actually in
    # conversation) and whether a human has hold of the head. Both live under
    # `/api/emori/`, a namespace Pollen's daemon has never used, so a hostapp pointed at
    # a real daemon gets a clean 404 and latches the feature off.

    @app.post("/api/emori/gaze")
    def emori_gaze(body: GazeFlag) -> Any:
        if gaze is None:
            # 503, NOT 404. 404 is reserved to mean "this robot is v1 / the daemon", and
            # the hostapp client LATCHES on 404 and never asks again - which would be
            # wrong for a v2 node that merely has `gaze.enabled:=false` today and may
            # have it true after the next restart.
            return JSONResponse(status_code=503,
                                content={"status": "unavailable", "reason": "gaze_disabled"})
        gaze(bool(body.enabled))
        return {"status": "ok", "enabled": bool(body.enabled)}

    @app.post("/api/emori/head_reserved")
    def emori_head_reserved(body: ReservedFlag) -> dict[str, Any]:
        # No injection needed: `arbiter.set_head_reserved` has existed since the arbiter
        # was written and has been dead code ever since, because nothing could reach it.
        # This route is what finally protects the expression lane while a human has hold
        # of the head.
        reserved = bool(body.reserved)
        snapshot_fn = getattr(arbiter, "motion_snapshot", None)
        before = (snapshot_fn() or {}).get("head_reserved") if callable(snapshot_fn) else None
        arbiter.set_head_reserved(reserved)
        if before is not None and bool(before) != reserved:
            # One line per transition, so the container log is the timeline of who held
            # the head and when.
            logger.info("head reservation %s by the host application"
                        % ("armed" if reserved else "released"))
        return {"status": "ok", "reserved": reserved}

    # --- media (inert) ---------------------------------------------------------------
    # The node has no media server: EMORI's robot speech goes through the kiosk browser
    # and its camera through the v4l2loopback bridge, exactly as under `--no-media`.
    # These answer 200 so a play_sound never latches `HttpRobotClient._media_ok` off for
    # a reason that is not about audio at all.

    @app.post("/api/media/play_sound")
    def play_sound(body: PlaySoundRequest) -> dict[str, str]:
        return _inert_media(f"play_sound({body.file})")

    @app.post("/api/media/wobbling/enable")
    def enable_wobbling() -> dict[str, str]:
        return _inert_media("wobbling/enable")

    @app.post("/api/media/wobbling/disable")
    def disable_wobbling() -> dict[str, str]:
        return _inert_media("wobbling/disable")

    return app


# --- the server ----------------------------------------------------------------------


class RestShim:
    """A running shim: the app, the uvicorn server, and the daemon thread it lives on."""

    def __init__(
        self,
        *,
        app: FastAPI,
        server: uvicorn.Server,
        thread: threading.Thread,
        host: str,
        port: int,
        sock: socket.socket,
        log: Any | None = None,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._server = server
        self._thread = thread
        self._socket = sock
        self._log = _Log(log)
        self._stopped = False

    @property
    def sock(self) -> socket.socket:
        """The listening socket. Read by the tests that assert how it was bound."""

        return self._socket

    @property
    def url(self) -> str:
        """A URL that actually resolves, even when bound to every interface."""
        host = "127.0.0.1" if self.host in ("", "0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(self._server, "started", False):
                return True
            if not self._thread.is_alive():
                return False
            time.sleep(0.01)
        return False

    def stop(self, timeout: float = 5.0) -> None:
        """Ask uvicorn to exit and join its thread. Idempotent; never raises."""
        if self._stopped:
            return
        self._stopped = True
        self._server.should_exit = True
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            # A connection wedged mid-response must not hold up the node's shutdown.
            self._server.force_exit = True
            self._thread.join(timeout=timeout)
        try:
            self._socket.close()
        except OSError:  # pragma: no cover - already closed by uvicorn
            pass


def start_rest_shim(
    arbiter: Any,
    backend_host: Any,
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    version: str | None = None,
    backend_name: str = BACKEND_MARKER,
    log: Any | None = None,
    robot_name: str = "reachy_mini",
    wireless_version: bool = False,
    simulation_enabled: bool | None = None,
    mockup_sim_enabled: bool | None = None,
    state_cache_s: float = 0.05,
    wait_ready_s: float = 10.0,
    gaze: Callable[[bool], None] | None = None,
    gaze_active: Callable[[], bool] | None = None,
    face_seen: Callable[[], float | None] | None = None,
) -> RestShim:
    """Serve the daemon dialect on ``host:port`` from a daemon thread; never blocks.

    ``port=0`` binds a free port and reports it on :attr:`RestShim.port` — that is how
    the tests run several shims at once without fighting a real daemon for :8000.

    A port already in use raises ``OSError`` here, in the CALLER's thread, rather than
    inside uvicorn's thread where it would become a log line and a node that reports
    itself up while nothing answers on :8000.
    """
    logger = _Log(log)
    host = host or "0.0.0.0"
    app = build_app(
        arbiter,
        backend_host,
        version=version,
        backend_name=backend_name,
        robot_name=robot_name,
        wireless_version=wireless_version,
        simulation_enabled=simulation_enabled,
        mockup_sim_enabled=mockup_sim_enabled,
        log=log,
        state_cache_s=state_cache_s,
        gaze=gaze,
        gaze_active=gaze_active,
        face_seen=face_seen,
    )

    # Bind BEFORE handing the socket to uvicorn: with `port=0` the kernel picks the port,
    # and asking uvicorn afterwards would mean reaching into `server.servers[0].sockets`
    # after a race on `server.started`. asyncio calls `listen()` itself in `create_server`,
    # so the socket must be bound and not listening here.
    family, socktype, proto, _canon, sockaddr = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )[0]
    sock = socket.socket(family, socktype, proto)
    # SO_REUSEADDR, and it is not boilerplate: it is the whole daemon-to-node handover.
    # On a robot the hostapp polls :8000 ten times a second, so the moment Pollen's daemon
    # stops there are dozens of its connections in TIME_WAIT on local port 8000, and a
    # bare bind() fails with EADDRINUSE for a minute afterwards. Without it the node can
    # come up owning the serial bus with `shim=disabled`, which is
    # the one state worse than not starting -- the motors have an owner and the host
    # application has nothing to talk to. asyncio's own create_server sets this on
    # POSIX; we bind the socket ourselves, so we set it ourselves.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(sockaddr)
    bound_port = int(sock.getsockname()[1])

    config = uvicorn.Config(
        app,
        host=host,
        port=bound_port,
        # The shim lives inside a ROS 2 node; uvicorn's default log config calls
        # `dictConfig` and would take the node's logging with it.
        log_config=None,
        log_level="warning",
        access_log=False,
        lifespan="off",
        # The daemon's websocket routes (`/api/move/ws/*`, `/api/state/ws/full`) are not
        # part of the hostapp contract and v2 publishes that state as ROS topics instead.
        # Declaring "none" also keeps uvicorn from importing the deprecated legacy
        # websockets stack into the node for a surface nothing uses.
        ws="none",
    )
    server = uvicorn.Server(config)

    def _serve() -> None:
        setup = getattr(config, "setup_event_loop", None)
        if setup is not None:
            try:
                setup()
            except AttributeError:
                # uvicorn >= 0.36 replaced it with `get_loop_factory`, which `run()`
                # applies itself; the attribute survives only to raise this.
                pass
        try:
            server.run(sockets=[sock])
        except Exception as exc:  # noqa: BLE001 - a dead shim must not kill the node
            logger.warning(f"REST shim stopped: {exc}")

    thread = threading.Thread(target=_serve, name="reachy-rest-shim", daemon=True)
    thread.start()

    shim = RestShim(
        app=app, server=server, thread=thread, host=host, port=bound_port, sock=sock, log=log
    )
    if not shim.wait_until_ready(wait_ready_s):
        logger.warning(f"REST shim did not come up on {shim.url} within {wait_ready_s}s")
    else:
        logger.info(f"REST shim serving the daemon dialect on {shim.url} (backend={backend_name})")
    return shim
