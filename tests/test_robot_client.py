"""HttpRobotClient tests over an httpx MockTransport — the production transport.

Verify the exact daemon REST surface and the mm/degrees -> metres/radians
conversion, so a unit regression here never has to be debugged on the CM4.
"""

from __future__ import annotations

import json
import math

import httpx

from emori_adapter.models import HeadPose, MotionIntent
from emori_adapter.robot.client import HttpRobotClient


class _DaemonRecorder:
    def __init__(self, *, healthy: bool = True, media_ok: bool = True):
        self.healthy = healthy
        self.media_ok = media_ok
        self.requests: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/daemon/status":
            # The real daemon returns the full DaemonStatus model (~15 fields); the
            # client only asserts state=="running". Mirror the real shape so the test
            # exercises the actual response, not a fictional minimal dict.
            return httpx.Response(200, json={
                "type": "daemon_status",
                "robot_name": "reachy_mini",
                "state": "running" if self.healthy else "starting",
                "wireless_version": True,
                "no_media": True,
                "version": "1.8.1",
            })
        if request.url.path.startswith("/api/media/") and not self.media_ok:
            return httpx.Response(403, text="media disabled")
        return httpx.Response(200)


def _client(rec: _DaemonRecorder, mode_rec: _DaemonRecorder | None = None) -> HttpRobotClient:
    """Point the client's pools at mock transports.

    ``mode_rec`` lets a test give the motor-mode pool its OWN transport, so it can prove
    mode traffic and motion traffic really are on separate connection pools.
    """
    c = HttpRobotClient("http://daemon.test:8000")
    c._http = httpx.Client(base_url="http://daemon.test:8000",
                           transport=httpx.MockTransport(rec.handler))
    c._mode_http = httpx.Client(base_url="http://daemon.test:8000",
                                transport=httpx.MockTransport((mode_rec or rec).handler))
    return c


def test_health_uses_daemon_status():
    rec = _DaemonRecorder()
    assert _client(rec).health() is True
    assert rec.requests == [("GET", "/api/daemon/status", None)]


def test_health_false_when_not_running():
    rec = _DaemonRecorder(healthy=False)
    assert _client(rec).health() is False


def test_goto_converts_mm_degrees_to_metres_radians():
    rec = _DaemonRecorder()
    c = _client(rec)
    # antennas authored as (left, right) = (0.8, 0.2); the daemon wire order is
    # [right, left], so the body must carry [0.2, 0.8] (asymmetric values catch the swap).
    c.apply(MotionIntent(kind="goto", head=HeadPose(z=12.0, pitch=16.0, yaw=-18.0),
                         antennas=(0.8, 0.2), duration=0.4, interpolation="ease_in_out"))
    method, path, body = rec.requests[-1]
    assert (method, path) == ("POST", "/api/move/goto")
    pose = body["head_pose"]
    assert pose["z"] == 12.0 / 1000.0
    assert abs(pose["pitch"] - math.radians(16.0)) < 1e-9
    assert abs(pose["yaw"] - math.radians(-18.0)) < 1e-9
    assert body["antennas"] == [0.2, 0.8]  # [right, left]
    assert body["duration"] == 0.4
    assert body["interpolation"] == "ease_in_out"


def test_set_target_and_wake_up_endpoints():
    rec = _DaemonRecorder()
    c = _client(rec)
    c.wake_up()
    c.apply(MotionIntent(kind="set_target", head=HeadPose(pitch=4.0), antennas=(0.8, 0.2)))
    paths = [p for _, p, _ in rec.requests]
    assert "/api/move/play/wake_up" in paths
    assert "/api/move/set_target" in paths
    target = rec.requests[-1][2]
    assert "target_head_pose" in target
    # set_target also swaps (left, right) -> daemon [right, left]
    assert target["target_antennas"] == [0.2, 0.8]


def test_set_motor_mode_endpoint():
    rec = _DaemonRecorder()
    c = _client(rec)
    c.set_motor_mode("gravity_compensation")
    method, path, _ = rec.requests[-1]
    assert (method, path) == ("POST", "/api/motors/set_mode/gravity_compensation")


def test_motor_mode_does_not_share_the_motion_connection_pool():
    """Mode changes must NOT ride the pool that carries robot motion.

    On a real robot (reachy_mini v1.9.0) a rejected mode returns HTTP 500 with an empty
    body and aborts the TCP connection, which kills whatever request is next on that
    pool. Measured on a robot on the LAN: 64 rejections cost 9 lost robot gestures. Isolating
    the pools means a rejected mode can never drop a gesture.
    """
    motion, mode = _DaemonRecorder(), _DaemonRecorder()
    c = _client(motion, mode_rec=mode)
    c.set_motor_mode("gravity_compensation")
    assert [p for _, p, _ in mode.requests] == ["/api/motors/set_mode/gravity_compensation"]
    assert motion.requests == []  # motion pool untouched by the mode change


def test_rejected_motor_mode_is_not_retried():
    """A mode the daemon refuses is attempted ONCE, then latched off."""
    class _RefusesMode(_DaemonRecorder):
        def handler(self, request: httpx.Request) -> httpx.Response:
            super().handler(request)
            if "/api/motors/set_mode/" in request.url.path:
                return httpx.Response(500)  # v1.9.0: empty-body 500
            return httpx.Response(200)

    rec = _RefusesMode()
    c = _client(rec)
    c.set_motor_mode("gravity_compensation")
    after_first = len(rec.requests)
    c.set_motor_mode("gravity_compensation")
    c.set_motor_mode("gravity_compensation")
    assert len(rec.requests) == after_first  # no repeat traffic, no log spam


def test_motor_mode_latch_is_per_mode():
    """One refused mode must not disable the modes that still work."""
    class _RefusesOnlyGravity(_DaemonRecorder):
        def handler(self, request: httpx.Request) -> httpx.Response:
            super().handler(request)
            if request.url.path.endswith("/gravity_compensation"):
                return httpx.Response(500)
            return httpx.Response(200)

    rec = _RefusesOnlyGravity()
    c = _client(rec)
    c.set_motor_mode("gravity_compensation")
    c.set_motor_mode("enabled")
    assert rec.requests[-1][1] == "/api/motors/set_mode/enabled"


def test_motor_mode_transport_error_is_latched_and_never_raises():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection aborted")

    rec = _DaemonRecorder()
    c = _client(rec)
    c._mode_http = httpx.Client(base_url="http://daemon.test:8000",
                                transport=httpx.MockTransport(explode))
    c.set_motor_mode("gravity_compensation")  # must not raise
    c.set_motor_mode("gravity_compensation")
    # motion still works after the mode pool blew up
    c.apply(MotionIntent(kind="goto", head=HeadPose(pitch=2.0)))
    assert rec.requests[-1][1] == "/api/move/goto"


def test_close_closes_both_pools():
    rec = _DaemonRecorder()
    c = _client(rec)
    c.close()
    assert c._http.is_closed
    assert c._mode_http.is_closed


def test_media_failure_latch_mutes_further_audio():
    rec = _DaemonRecorder(media_ok=False)
    c = _client(rec)
    c.apply(MotionIntent(kind="play_sound", sound="mmhm.wav"))
    n_after_first = len(rec.requests)
    c.apply(MotionIntent(kind="play_sound", sound="mmhm.wav"))
    # second attempt never hits the network: the latch flipped on the 403
    assert len(rec.requests) == n_after_first
    # motion is unaffected by the media latch
    c.apply(MotionIntent(kind="goto", head=HeadPose(pitch=2.0)))
    assert rec.requests[-1][1] == "/api/move/goto"


def test_wobble_endpoints():
    rec = _DaemonRecorder()
    c = _client(rec)
    c.apply(MotionIntent(kind="wobble", wobble=True))
    c.apply(MotionIntent(kind="wobble", wobble=False))
    paths = [p for _, p, _ in rec.requests]
    assert "/api/media/wobbling/enable" in paths
    assert "/api/media/wobbling/disable" in paths


def test_network_errors_never_raise():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("daemon down")

    c = HttpRobotClient("http://daemon.test:8000")
    c._http = httpx.Client(base_url="http://daemon.test:8000",
                           transport=httpx.MockTransport(explode))
    assert c.health() is False
    c.apply(MotionIntent(kind="goto", head=HeadPose(pitch=2.0)))  # must not raise
    c.wake_up()


# --- Pollen recorded moves: play returns the daemon's uuid, stop uses its REAL contract


def test_recorded_move_play_and_stop_follow_the_daemon_contract():
    """``POST /api/move/stop`` takes a ``MoveUUID`` body -- NOT ``/stop/{uuid}``.

    The first shipped version posted to a path the daemon answers 404, so no
    recording was ever cut: a 2.5-5 s Pollen move kept playing under the next
    sentence's plan (verified against the live v1.9.0 openapi).
    """

    class _MoveDaemon(_DaemonRecorder):
        def handler(self, request):
            body = json.loads(request.content) if request.content else None
            self.requests.append((request.method, request.url.path, body))
            if request.url.path.startswith("/api/move/play/recorded-move-dataset/"):
                return httpx.Response(200, json={"uuid": "0b3c4f1e-0000-4000-8000-000000000001"})
            return httpx.Response(200)

    rec = _MoveDaemon()
    c = _client(rec)
    uuid = c.play_recorded_move("fear1")
    assert uuid == "0b3c4f1e-0000-4000-8000-000000000001"
    assert rec.requests[-1][1] == (
        "/api/move/play/recorded-move-dataset/pollen-robotics/reachy-mini-emotions-library/fear1")
    c.stop_recorded_move(uuid)
    method, path, body = rec.requests[-1]
    assert (method, path) == ("POST", "/api/move/stop")
    assert body == {"uuid": uuid}


def test_stop_recorded_move_ignores_an_empty_uuid():
    rec = _DaemonRecorder()
    c = _client(rec)
    c.stop_recorded_move("")
    assert all(p != "/api/move/stop" for _, p, _ in rec.requests)


# --- v2 node extras: the two arbiter gates the hostapp pushes ------------------
#
# These are EXTRA routes on the v2 shim (`/api/emori/*`), a namespace Pollen's daemon
# has never used. Under v1 the daemon answers 404 and the hostapp must degrade to
# exactly its pre-v2 behaviour -- quietly, once, and for ever. Under a v2 node with
# gaze switched off the answer is 503, which is a TODAY answer and must not latch.


def test_gaze_and_head_reserved_post_the_v2_routes():
    rec = _DaemonRecorder()
    c = _client(rec)

    assert c.set_gaze(True) is True
    assert rec.requests[-1] == ("POST", "/api/emori/gaze", {"enabled": True})

    assert c.set_head_reserved(False) is True
    assert rec.requests[-1] == ("POST", "/api/emori/head_reserved", {"reserved": False})


def test_a_404_latches_the_route_off_and_warns_exactly_once(caplog):
    """v1 / Pollen's daemon: ask once, say so once, never ask again.

    The latch is what keeps a v1 rollback silent: the driver's 10 Hz reconciler
    would otherwise POST a 404 ten times a second for the life of the process.
    """

    class _NoEmoriRoutes(_DaemonRecorder):
        def handler(self, request: httpx.Request) -> httpx.Response:
            super().handler(request)
            if request.url.path.startswith("/api/emori/"):
                return httpx.Response(404)
            return httpx.Response(200)

    rec = _NoEmoriRoutes()
    c = _client(rec)
    with caplog.at_level("WARNING", logger="emori.robot"):
        assert c.set_gaze(True) is False
        after_first = len(rec.requests)
        assert c.set_gaze(False) is False
        assert c.set_gaze(True) is False

    assert len(rec.requests) == after_first  # the second call makes NO request at all
    warnings = [r for r in caplog.records if "/api/emori/gaze" in r.getMessage()]
    assert len(warnings) == 1
    assert "face look-at" in warnings[0].getMessage()


def test_the_404_latch_is_per_route():
    """A daemon that grew one route but not the other must not lose both."""

    class _OnlyHeadReserved(_DaemonRecorder):
        def handler(self, request: httpx.Request) -> httpx.Response:
            super().handler(request)
            if request.url.path == "/api/emori/gaze":
                return httpx.Response(404)
            return httpx.Response(200)

    rec = _OnlyHeadReserved()
    c = _client(rec)
    assert c.set_gaze(True) is False
    assert c.set_head_reserved(True) is True
    assert rec.requests[-1][1] == "/api/emori/head_reserved"


class _GazeDisabled(_DaemonRecorder):
    """A v2 node whose gaze component is switched off today: 503, not 404."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        super().handler(request)
        if request.url.path == "/api/emori/gaze":
            return httpx.Response(503, json={"status": "unavailable",
                                             "reason": "gaze_disabled"})
        return httpx.Response(200)


def test_a_503_does_not_latch_because_gaze_may_be_enabled_later(monkeypatch):
    """503 = a v2 node with `gaze.enabled:=false` TODAY. A restart may change that,
    so unlike a 404 this is a transient answer and the route stays live -- it merely
    cools off, so the 10 Hz reconciler does not ask ten times a second for ever."""

    from emori_adapter.robot import client as client_module

    rec = _GazeDisabled()
    c = _client(rec)
    assert c.set_gaze(True) is False
    assert "/api/emori/gaze" not in c._routes_absent   # never latched off

    before = len(rec.requests)
    assert c.set_gaze(True) is False
    assert len(rec.requests) == before                 # ...but cooling off right now

    monkeypatch.setattr(client_module, "_FLAG_BACKOFF_S", 0.0)
    c._routes_backoff.clear()
    assert c.set_gaze(True) is False
    assert len(rec.requests) == before + 1             # and asked again afterwards


def test_a_dead_node_costs_the_caller_one_attempt_per_cool_off(monkeypatch):
    """The gesture ticker reconciles these gates ten times a second.

    A POST to a port nothing is listening on costs seconds before httpx gives up
    (measured 2.03 s on the dev box), so without a cool-off a robot node that is merely
    restarting would take the head SENSOR down with it: the thread would spend every
    tick of its 10 Hz budget waiting on a socket.
    """

    calls: list[str] = []

    def explode(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("node restarting")

    c = HttpRobotClient("http://daemon.test:8000")
    c._http = httpx.Client(base_url="http://daemon.test:8000",
                           transport=httpx.MockTransport(explode))

    for _ in range(20):                                # two seconds of reconciler ticks
        c.set_gaze(True)
    assert calls == ["/api/emori/gaze"]

    c._routes_backoff.clear()                          # the cool-off elapsed
    c.set_gaze(True)
    assert len(calls) == 2


def test_the_flag_routes_use_a_deadline_the_sensor_can_afford():
    """An advisory gate whose next chance to be right is 100 ms away must not be given
    the pool's multi-second motion deadline."""

    from emori_adapter.robot import client as client_module

    seen: list[object] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200)

    c = HttpRobotClient("http://daemon.test:8000")
    c._http = httpx.Client(base_url="http://daemon.test:8000", timeout=5.0,
                           transport=httpx.MockTransport(record))
    c.set_head_reserved(True)

    assert seen and seen[0]["connect"] == client_module._FLAG_TIMEOUT_S


def test_a_transport_error_on_a_flag_never_raises_and_never_latches():
    calls: list[str] = []

    def explode(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("node restarting")

    c = HttpRobotClient("http://daemon.test:8000")
    c._http = httpx.Client(base_url="http://daemon.test:8000",
                           transport=httpx.MockTransport(explode))
    assert c.set_gaze(True) is False
    assert c.set_head_reserved(True) is False
    assert len(calls) == 2
    assert c._routes_absent == set()  # a blip is not a missing route


def test_dry_run_client_records_both_flags():
    from emori_adapter.robot.client import DryRunRobotClient

    c = DryRunRobotClient()
    assert c.set_gaze(True) is True
    assert c.set_gaze(False) is True
    assert c.set_head_reserved(True) is True
    assert c.gaze_calls == [True, False]
    assert c.head_reserved_calls == [True]


class _HeaderDaemon:
    def __init__(self) -> None:
        self.headers: list = []

    def handler(self, request):
        self.headers.append(request.headers)
        if request.url.path.startswith("/api/move/play/"):
            return httpx.Response(200, json={"uuid": "0b3c4f1e-0000-4000-8000-000000000001"})
        return httpx.Response(200, json={"uuid": "0b3c4f1e-0000-4000-8000-000000000002"})


def test_a_superseding_intent_says_so_on_the_wire():
    rec = _HeaderDaemon()
    c = _client(rec)
    c.apply(MotionIntent(kind="goto", duration=0.2, supersede=True))
    assert rec.headers[-1]["x-reachy-supersede"] == "1"
    c.apply(MotionIntent(kind="goto", duration=0.2))
    assert "x-reachy-supersede" not in rec.headers[-1]
    c.play_recorded_move("fear1", supersede=True)
    assert rec.headers[-1]["x-reachy-supersede"] == "1"


def test_the_kill_switch_keeps_supersede_off_the_wire(monkeypatch):
    monkeypatch.setenv("EMORI_MOTION_SYNC", "0")
    rec = _HeaderDaemon()
    c = _client(rec)
    c.apply(MotionIntent(kind="goto", duration=0.2, supersede=True))
    assert "x-reachy-supersede" not in rec.headers[-1]


def test_a_traced_move_carries_its_name(monkeypatch):
    rec = _HeaderDaemon()
    c = _client(rec)
    monkeypatch.setattr("emori_adapter.motion_trace.current_tag", lambda: "9.2")
    c.apply(MotionIntent(kind="goto", duration=0.2))
    assert rec.headers[-1]["x-reachy-trace"] == "9.2"
