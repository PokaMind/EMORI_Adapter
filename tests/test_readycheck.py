"""Readiness self-check: gate matrix, honest degradation to SKIP, remediation hints."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import time

import pytest

from emori_adapter import robot_token
from emori_adapter.hostapp import readycheck
from emori_adapter.hostapp.pokamind_whoami import Whoami, WhoamiModule, WhoamiResult
from emori_adapter.hostapp.readycheck import (
    FAIL,
    PASS,
    SAFE_CODE_RE,
    SKIP,
    SPOKEN_LINES,
    WARN,
    ClockProbe,
    CommandResult,
    DashboardData,
    Evidence,
    GateResult,
    HttpResult,
    IdentityProbe,
    MenuProbe,
    RouteProbe,
    SiteProbe,
    SsoResult,
    SystemProbe,
    TokenProbe,
    announce_failure,
    collect,
    evaluate,
    exit_code,
    file_digest,
    format_matrix,
    load_route_manifest,
)

# Real `v4l2-ctl --list-formats-ext` shape: a fed loopback enumerates at least one
# "[n]: 'FOURCC'" line; an unfed one prints the header and nothing else.
_V4L2_FED = (
    "ioctl: VIDIOC_ENUM_FMT\n"
    "\tType: Video Capture\n\n"
    "\t[0]: 'YUYV' (YUYV 4:2:2)\n"
    "\t\tSize: Discrete 640x480\n"
)
_V4L2_UNFED = "ioctl: VIDIOC_ENUM_FMT\n\tType: Video Capture\n"

# Pokamind's SSO answer: a 302 whose Location is an ABSOLUTE frontend callback URL.
# `emoriKiosk=1` is appended only when Pokamind has kiosk mode enabled for the partner
# *and* the token carried `emori_kiosk` - without it the kiosk never auto-advances
# the device-check step and the screenless robot waits at a device-check page nobody can click.
_FRONTEND_ORIGIN = "http://localhost:3000"
# Only what readycheck reads: the callback path and the kiosk flag.
_CALLBACK = (
    f"{_FRONTEND_ORIGIN}/auth/partner-callback/emori-robot"
    "?partner=emori-robot&emoriKiosk=1"
)
_MENU_OPTIONS = (("1", "MOD0000000000000001a"), ("2", "MOD0000000000000002b"))

# emori-admin's GET /healthz/deep on a v2 robot whose Wi-Fi and volume work
# (admin/backend_check.run).
_ADMIN_DEEP_OK = {
    "ok": True, "service": "emori-admin", "backend": "host", "reason": "v2_status",
    "wifi": {"ok": True, "route_missing": False, "error": ""},
    "volume": {"ok": True, "route_missing": False, "error": ""},
}


# THIS robot's own Pokamind token: the golden test vector. The green robot
# runs on its own per-robot credential, its only one; ``_tokenless`` is a robot whose
# token is missing (it signs nothing and serves demo sessions only).
GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
GOLDEN_FINGERPRINT = "Vkdap1RjR0wChd9d"
TOKEN = TokenProbe(state="configured", source="file",
                   path="/opt/emori/secrets/pokamind_robot_token", robot_id="test-robot-01",
                   fingerprint=GOLDEN_FINGERPRINT,
                   masked=robot_token.parse_token(GOLDEN_TOKEN).masked)
WHOAMI_AT = 1785000000.0


def whoami_module(module_id: str, *, available: bool = True, problem: str | None = None,
                  language: str = "ca") -> WhoamiModule:
    return WhoamiModule(module_id, f"Module {module_id[:6]}", "Example pathway",
                        language, available, problem)


def whoami(**overrides) -> WhoamiResult:
    """Pokamind's answer for the green robot: active, the menu's modules in scope."""

    fields = dict(
        robot_id="test-robot-01", display_name="Demo reception", status="active",
        credential_kind="fleet", key_fingerprint=GOLDEN_FINGERPRINT, key_version=1,
        org="example-org", partner="emori-robot", course_id="example-course",
        languages=("ca",), pathways=("Example pathway",), identity_domains=(),
        modules=tuple(whoami_module(module_id) for _key, module_id in _MENU_OPTIONS),
        features={"feature_c": True, "feature_b": False, "feature_a": False},
    )
    fields.update(overrides)
    return WhoamiResult(ok=True, code="ok", http_status=200, whoami=Whoami(**fields),
                        checked_at=WHOAMI_AT)


def _tokenless(**overrides) -> Evidence:
    """The same robot with no usable Pokamind token: nothing to sign a login or a whoami
    with, so no chain was walked, and its host app serves demo sessions only."""

    base = dict(token=TokenProbe(), whoami=None, sso=SsoResult(False),
                badge_sso=SsoResult(False), frontend=HttpResult(False),
                healthz=HttpResult(True, 200, {
                    "status": "ok", "demo": True, "partner": "emori-robot",
                    "pokamind_credential": "none", "roster_staff": 2, "robot": True}),
                start_demo_branch=True)
    base.update(overrides)
    return _healthy(**base)


# The green robot's menu: built from its Pokamind record, every name authored there.
_HEALTHY_MENU = MenuProbe(
    True, "", _MENU_OPTIONS, source="/opt/emori/configs/site/auto/voice_topics.ca.yaml",
    lang="ca", digest="sha256:a7", tier="auto", authored="pokamind",
)


def _healthy(**overrides) -> Evidence:
    """A green robot: every built-in gate passes."""

    base = dict(
        healthz=HttpResult(
            True,
            200,
            {"status": "ok", "demo": False, "partner": "emori-robot",
             "pokamind_credential": "robot_token", "roster_staff": 2, "robot": True},
        ),
        daemon=HttpResult(True, 200, {"state": "running", "no_media": True}),
        motors=HttpResult(True, 200, {"mode": "enabled"}),
        # Every route the code calls is in the daemon's own /openapi.json.
        daemon_routes=_routes_served(),
        tts=HttpResult(True, 200, None, "audio/wav"),
        stt=HttpResult(True, 200, {"available": True}),
        pokamind=HttpResult(True, 200, None, "text/html"),
        camera=CommandResult(True, 0, _V4L2_FED),
        # The v2 robot: the camera bridge tees a second loopback for the ROS robot node's
        # face look-at, and readycheck was told about it in /opt/emori/.env. A robot that
        # did not opt in is the `gaze_lane_off` PASS below, not this.
        gaze_video="/dev/video41",
        camera_ros=CommandResult(True, 0, _V4L2_FED),
        gesture_enabled=True,
        gesture_mode="push_turn",
        gesture_invert="1",
        reachy_base_url="http://127.0.0.1:8000",
        pokamind_base="http://localhost:4000",
        sso=SsoResult(True, 302, _CALLBACK),
        badge_sso=SsoResult(True, 302, _CALLBACK),
        badge_course_id="example-course",
        badge_pathway="Example pathway",
        frontend=HttpResult(True, 200, None, "text/html"),
        # Built from its Pokamind record, and Pokamind's live answer covers every option.
        menu=_HEALTHY_MENU,
        hostapp_url="http://127.0.0.1:8080",
        lang="ca",
        admin=HttpResult(True, 200, {"status": "ok", "service": "emori-admin"}),
        admin_deep=HttpResult(True, 200, dict(_ADMIN_DEEP_OK)),
        admin_token_set=True,
        start_demo_branch=False,
        dashboard_data=_fed_dashboard(),
        token=TOKEN,
        whoami=whoami(),
        course_id_env="example-course",
        # NTP-synchronized, and Pokamind's Date header agrees to within its resolution.
        clock=ClockProbe(ntp="yes", skew_s=0.2, uncertainty_s=0.6),
        # The board install.sh recorded is the board it runs on.
        identity=IdentityProbe(serial="10000000a1b2c3d4", wlan_mac="dc:a6:32:01:02:03",
                               recorded_serial="10000000a1b2c3d4",
                               recorded_robot_id="test-robot-01"),
        # Configured explicitly in .env, as every robot in service is, with a Pokamind
        # scope synced an hour ago and nothing waiting for a restart.
        site=_site(),
    )
    base.update(overrides)
    return Evidence(**base)


_SITE_NOW = 1785000000.0


def _site(**overrides) -> SiteProbe:
    """The settings layer of the green robot: explicit values, a fresh scope, no restart."""

    base = dict(
        probed=True,
        sources=(("EMORI_COURSE_ID", "env"),
                 ("EMORI_LANG", "env"), ("EMORI_ROBOT_NAME", "env"),
                 ("EMORI_PATHWAYS", "env"), ("POKAMIND_BASE", "env")),
        scope_status="ok", scope_fetched_at=_SITE_NOW - 3600.0, scope_contract=2,
        scope_partner="emori-robot", now=_SITE_NOW, restart_pending=False,
    )
    base.update(overrides)
    return SiteProbe(**base)


# A dashboard that is actually being fed: a heartbeat from seconds ago, and a last
# finished session that carries both a cycle series and competency scores.
_DASH_NOW = 1785000000.0


def _fed_dashboard(**overrides) -> DashboardData:
    base = dict(
        ok=True, path="/opt/emori/artifacts/dashboard.db", now=_DASH_NOW,
        last_heartbeat_ts=_DASH_NOW - 30.0, heartbeat_interval_s=60.0,
        finished_sessions=4, any_cycles_ever=True, last_completed=True,
        last_finished_at=_DASH_NOW - 600.0,
        last_finished_status="completed", last_finished_cycles=180,
        last_finished_scored=True,
    )
    base.update(overrides)
    return DashboardData(**base)


def _routes_served() -> RouteProbe:
    """A :8000 whose /openapi.json lists every route the shipped manifest needs."""

    needs, _ = load_route_manifest()
    return RouteProbe(needs=needs, reached=True,
                      openapi=tuple(sorted({(need.method, need.shape) for need in needs})))


# What `ros2 node list` prints inside a healthy container: slice2.launch.py's four
# unconditional nodes.  `fiware_bridge` is condition-gated and absent on the robot by
# design, so it must never be required.
_NODE_LIST = ("/emori_adapter\n/hri_context_analyzer\n"
              "/hri_engagement_behaviors\n/skill_facade\n")


def _ros2(**overrides) -> Evidence:
    """A robot that opted into the ROS 2 lane, with the whole lane healthy.

    The armed baseline: container running, the whole graph up inside it, the FIWARE
    bridge off, and the container agreeing with .env that the hostapp still owns motion.
    """

    base = dict(
        ros2_enabled=True,
        ros_motion_owner="hostapp",
        ros2_container=CommandResult(True, 0, "true\n"),
        ros2_fiware=CommandResult(True, 0, "0\n"),
        ros2_owner=CommandResult(True, 0, "hostapp\n"),
        ros2_nodes=CommandResult(True, 0, _NODE_LIST),
        # POST-only route, so a GET is 405 when it is mounted.
        skill_api=HttpResult(True, 405, {"detail": "Method Not Allowed"}),
    )
    base.update(overrides)
    return _healthy(**base)


def _by_name(results) -> dict[str, GateResult]:
    return {result.name: result for result in results}


# --- the green path -----------------------------------------------------------------


def test_healthy_rig_passes_every_gate():
    results = evaluate(_healthy())

    assert [result.status for result in results] == [PASS] * len(results), _by_name(results)
    assert exit_code(results) == 0
    assert len(results) == 28


def test_a_public_only_check_has_no_add_on_gates_and_no_add_on_warnings():
    from emori_adapter.hostapp import readycheck as rc

    # tests/conftest.py sets EMORI_ADDONS=off: nothing contributes.
    assert rc._contributed_gates() == ()
    assert [gate.__name__ for gate in rc.active_gates()] == [gate.__name__ for gate in rc.GATES]

    evidence = collect(_FakeProbe({}))
    assert evidence.addon == {} and evidence.clock_strict_reason == ""
    assert evidence.voices_missing == ()

    results = evaluate(_healthy())
    assert [result.name for result in results] == [gate.__name__[len("gate_"):]
                                                   for gate in rc.GATES]
    assert not [result for result in results if result.status == WARN]


# --- never fabricate a PASS ---------------------------------------------------------


def test_hostapp_down_fails_once_and_skips_its_dependents():
    evidence = _healthy(healthz=HttpResult(False))
    gates = _by_name(evaluate(evidence))

    assert (gates["hostapp"].status, gates["hostapp"].code) == (FAIL, "hostapp_unreachable")
    for name in ("demo_off", "robot_link", "tts", "stt"):
        assert gates[name].status == SKIP, name
        assert gates[name].code == "hostapp_unreachable", name
    # hostapp-independent gates still run for real
    assert gates["daemon"].status == PASS
    assert exit_code(evaluate(evidence)) == 1


def test_demo_mode_is_a_failure_not_a_pass():
    gate = _by_name(
        evaluate(_healthy(healthz=HttpResult(True, 200, {"status": "ok", "demo": True,
                                                         "robot": True})))
    )["demo_off"]

    assert (gate.status, gate.code) == (FAIL, "hostapp_in_demo_mode")
    assert "HOSTAPP_DEMO" in gate.hint and "RESTART" in gate.hint
    # Without a token there is no live mode to switch to: the one fix is the token.
    tokenless = _by_name(evaluate(_tokenless()))["demo_off"]
    assert (tokenless.status, tokenless.code) == (FAIL, "hostapp_in_demo_mode")
    assert "Replace token" in tokenless.hint and "Deploy-to-Robot" in tokenless.hint


def test_absent_demo_key_never_passes():
    evidence = _healthy(healthz=HttpResult(True, 200, {"status": "ok", "robot": True}))

    assert _by_name(evaluate(evidence))["demo_off"].status == FAIL
    assert _by_name(evaluate(evidence))["robot_link"].status == PASS


def test_hostapp_robot_link_down_is_reported_separately_from_the_daemon():
    evidence = _healthy(
        healthz=HttpResult(True, 200, {"status": "ok", "demo": False, "robot": False})
    )
    gates = _by_name(evaluate(evidence))

    assert (gates["robot_link"].status, gates["robot_link"].code) == (
        FAIL, "hostapp_robot_link_down")
    assert gates["daemon"].status == PASS  # the daemon is fine; the hostapp's view is not


# --- daemon / motors ----------------------------------------------------------------


def test_daemon_holding_media_fails_the_kiosk_contract():
    gate = _by_name(
        evaluate(_healthy(daemon=HttpResult(True, 200, {"state": "running",
                                                        "no_media": False})))
    )["daemon"]

    assert (gate.status, gate.code) == (FAIL, "daemon_owns_media")
    assert "robot-mode.sh emori" in gate.hint


def test_motors_disabled_after_a_power_cycle_fails_with_the_enable_hint():
    gate = _by_name(evaluate(_healthy(motors=HttpResult(True, 200, {"mode": "disabled"}))))["motors"]

    assert (gate.status, gate.code) == (FAIL, "motors_not_enabled")
    assert "/api/motors/set_mode/enabled" in gate.hint


def test_motors_skip_when_the_daemon_itself_is_unreachable():
    gates = _by_name(evaluate(_healthy(daemon=HttpResult(False), motors=HttpResult(False))))

    assert gates["daemon"].status == FAIL
    assert (gates["motors"].status, gates["motors"].code) == (SKIP, "daemon_unreachable")


# --- speech / tunnel ----------------------------------------------------------------


@pytest.mark.parametrize(
    "response, expected",
    [
        (HttpResult(True, 200, None, "audio/wav"), (PASS, "warm")),
        (HttpResult(True, 503, {"error": "tts unavailable"}, "application/json"),
         (FAIL, "tts_unavailable")),
        (HttpResult(True, 409, {"error": "tts blocked during live session"}, "application/json"),
         (SKIP, "tts_session_active")),
        (HttpResult(True, 500, {"error": "boom"}, "application/json"), (FAIL, "tts_error")),
        (HttpResult(True, 200, {"ok": True}, "application/json"), (FAIL, "tts_not_audio")),
        (HttpResult(False), (FAIL, "tts_probe_failed")),
    ],
)
def test_tts_gate(response, expected):
    gate = _by_name(evaluate(_healthy(tts=response)))["tts"]
    assert (gate.status, gate.code) == expected


def test_stt_unavailable_points_at_the_model_downloader():
    gate = _by_name(evaluate(_healthy(stt=HttpResult(True, 200, {"available": False}))))["stt"]

    assert (gate.status, gate.code) == (FAIL, "stt_unavailable")
    assert "get_voice_models" in gate.hint


def test_pokamind_gate_proves_transport_not_authorisation():
    assert _by_name(evaluate(_healthy(pokamind=HttpResult(False))))["pokamind"].status == FAIL
    # any answer below 5xx proves the tunnel carries HTTP; 401/404 must NOT fail the gate
    for status in (200, 302, 401, 404):
        evidence = _healthy(pokamind=HttpResult(True, status, None, "text/html"))
        assert _by_name(evaluate(evidence))["pokamind"].status == PASS, status
    evidence = _healthy(pokamind=HttpResult(True, 502, None, "text/html"))
    assert _by_name(evaluate(evidence))["pokamind"].code == "pokamind_server_error"


def test_pokamind_failure_hint_names_the_base_url_setting():
    gate = _by_name(evaluate(_healthy(pokamind=HttpResult(False))))["pokamind"]
    assert "POKAMIND_BASE" in gate.hint


# --- camera bridge ----------------------------------------------------


@pytest.mark.parametrize(
    "camera, expected",
    [
        (CommandResult(False), (SKIP, "v4l2_ctl_unavailable")),
        (CommandResult(True, 1, ""), (FAIL, "camera_node_missing")),
        (CommandResult(True, 0, _V4L2_UNFED), (FAIL, "camera_capture_not_advertised")),
        (CommandResult(True, 0, _V4L2_FED), (PASS, "capture_ready")),
    ],
)
def test_camera_bridge_gate(camera, expected):
    gate = _by_name(evaluate(_healthy(camera=camera)))["camera_bridge"]
    assert (gate.status, gate.code) == expected


# --- the v2 face look-at camera tee --------------------------------------------------


@pytest.mark.parametrize(
    "overrides, expected",
    [
        # A robot without the face look-at tee, or one that opted out. PASS, like the
        # optional ROS lane's `ros2_lane_off`: an unarmed feature is not a failure.
        ({"gaze_video": "", "camera_ros": CommandResult()},
         (PASS, "gaze_lane_off")),
        # Configured, but this box has no v4l2-ctl to ask with.
        ({"camera_ros": CommandResult(False)}, (SKIP, "v4l2_ctl_unavailable")),
        # The module was loaded with only video40, or the live add never ran. The
        # container cannot even START in this state: `devices:` on a missing node
        # fails container creation.
        ({"camera_ros": CommandResult(True, 1, "")}, (FAIL, "ros_camera_node_missing")),
        # The node exists but the bridge is teeing nothing into it -- the failure with
        # no symptom: the container comes up and the face topic is silently empty.
        ({"camera_ros": CommandResult(True, 0, _V4L2_UNFED)},
         (FAIL, "ros_camera_not_fed")),
        ({"camera_ros": CommandResult(True, 0, _V4L2_FED)}, (PASS, "ros_camera_ready")),
    ],
)
def test_gaze_gate(overrides, expected):
    gate = _by_name(evaluate(_healthy(**overrides)))["gaze"]
    assert (gate.status, gate.code) == expected


def test_the_two_camera_gates_judge_two_different_devices():
    """video40 is Chromium's and video41 is the node's, and they fail independently.

    The failure this pins is a gate that reads `ev.camera` for both: a robot whose kiosk
    camera is fine would then report the face tee healthy while the node opens nothing.
    """

    gates = _by_name(evaluate(_healthy(camera_ros=CommandResult(True, 1, ""))))

    assert gates["camera_bridge"].status == PASS
    assert (gates["gaze"].status, gates["gaze"].code) == (FAIL, "ros_camera_node_missing")

    gates = _by_name(evaluate(_healthy(camera=CommandResult(True, 1, ""))))

    assert gates["camera_bridge"].status == FAIL
    assert gates["gaze"].status == PASS


def test_a_missing_ros_loopback_names_the_script_that_creates_it_and_the_number():
    """An operator reading this line at 7 a.m. must not have to work out which device
    number the hint is about."""

    gate = _by_name(evaluate(_healthy(
        gaze_video="/dev/video41", camera_ros=CommandResult(True, 1, ""))))["gaze"]

    assert "ros-loopback.sh 41" in gate.hint
    assert "camera-bridge.service" in gate.hint


def test_an_unfed_ros_loopback_names_the_variable_that_feeds_it():
    """The device exists, so the fix is not to create it -- it is the ONE env var in
    /etc/default/emori-camera that turns the tee on, plus a bridge restart."""

    gate = _by_name(evaluate(_healthy(
        camera_ros=CommandResult(True, 0, _V4L2_UNFED))))["gaze"]

    assert "EMORI_CAM_ROS_VIDEO_NR" in gate.hint
    assert "restart emori-camera-bridge" in gate.hint


def test_an_unconfigured_gaze_gate_passes_rather_than_skipping():
    """PASS `gaze_lane_off`, the same answer the optional ROS lane gives -- and for the
    same reason, which this file states one gate over: "a feature nobody armed is not a
    dependency that could not be probed, and a SKIP there would fail --strict on a
    perfectly healthy robot".

    Face look-at is armed in two places (EMORI_CAM_ROS_VIDEO_NR in
    /etc/default/emori-camera, then EMORI_READYCHECK_ROS_VIDEO_DEVICE here), but two
    switches instead of one does not turn an unarmed feature into an unprobed
    dependency. Unarmed is every v1 robot and every v2 robot that has not turned look-at on,
    so a SKIP would make `readycheck.sh --strict` exit 2 on a healthy robot -- and a
    --strict that cries wolf on the fleet is a --strict nobody reads when it is right.
    """

    results = evaluate(_healthy(gaze_video="", camera_ros=CommandResult()))

    assert (_by_name(results)["gaze"].status, _by_name(results)["gaze"].code) == \
        (PASS, "gaze_lane_off")
    assert exit_code(results) == 0
    assert exit_code(results, strict=True) == 0


# --- the phone superadmin service ----------------------------------------------------


@pytest.mark.parametrize(
    "admin, expected",
    [
        (HttpResult(False), (WARN, "admin_unreachable")),
        (HttpResult(True, 503, {"status": "starting"}), (WARN, "admin_unhealthy")),
        (HttpResult(True, 200, None, "text/html"), (WARN, "admin_unhealthy")),
        # The hostapp's OWN /healthz shape: status=ok, no `service`. An
        # EMORI_ADMIN_PORT pointed at 8080 must not read as a healthy admin.
        (HttpResult(True, 200, {"status": "ok", "demo": False}),
         (WARN, "admin_port_foreign")),
        (HttpResult(True, 200, {"status": "ok", "service": "emori-admin"}), (PASS, "ok")),
    ],
)
def test_the_admin_gate_reports_the_phone_surface_without_blocking_the_robot(admin,
                                                                            expected):
    gate = _by_name(evaluate(_healthy(admin=admin)))["admin"]

    assert (gate.status, gate.code) == expected


def test_a_missing_admin_service_never_turns_the_matrix_red():
    """WARN, never FAIL: the robot runs sessions perfectly well without a phone
    admin, and a FAIL would block go-live and speak "I am not ready" over it."""

    results = evaluate(_healthy(admin=HttpResult(False)))

    assert exit_code(results) == 0
    assert exit_code(results, strict=True) == 0        # not a SKIP either
    hint = _by_name(results)["admin"].hint
    assert "systemctl restart emori-admin" in hint


# --- the admin PIN (pin_configured on emori-admin's /healthz) -------------------------


_ADMIN_OK = {"status": "ok", "service": "emori-admin"}


def test_an_admin_service_without_a_pin_warns_admin_pin_unset():
    gate = _by_name(evaluate(_healthy(
        admin=HttpResult(True, 200, dict(_ADMIN_OK, pin_configured=False)))))["admin"]

    assert (gate.status, gate.code) == (WARN, "admin_pin_unset")
    assert "EMORI_ADMIN_PIN" in gate.hint and "503" in gate.hint
    assert "systemctl restart emori-admin" in gate.hint


@pytest.mark.parametrize("payload", [
    dict(_ADMIN_OK, pin_configured=True),
    # An emori-admin that predates the field says nothing: that check is skipped.
    dict(_ADMIN_OK),
    dict(_ADMIN_OK, pin_configured="no"),  # not a boolean: not judged
])
def test_a_configured_or_unreported_pin_is_not_a_finding(payload):
    gate = _by_name(evaluate(_healthy(admin=HttpResult(True, 200, payload))))["admin"]

    assert (gate.status, gate.code) == (PASS, "ok")


def test_a_deploy_defect_still_outranks_a_missing_pin():
    # A v2 robot whose emori-admin is older than its /healthz/deep route: FAIL, and the
    # missing PIN does not hide it.
    gate = _by_name(evaluate(_healthy(
        admin=HttpResult(True, 200, dict(_ADMIN_OK, pin_configured=False)),
        admin_deep=HttpResult(True, 404, {}),
        daemon=HttpResult(True, 200, {"state": "running", "no_media": True,
                                      "backend": "reachy_mini_ros2"}))))["admin"]

    assert (gate.status, gate.code) == (FAIL, "admin_wifi_backend_missing")


def test_a_missing_pin_outranks_an_advisory_backend_finding():
    gate = _by_name(evaluate(_healthy(
        admin=HttpResult(True, 200, dict(_ADMIN_OK, pin_configured=False)),
        admin_token_set=False)))["admin"]

    assert gate.code == "admin_pin_unset"


# --- dashboard data liveness ---------------------------------------------------------


_DEMO_HEALTHZ = HttpResult(True, 200, {"status": "ok", "demo": True, "robot": True})


def test_the_demo_branch_is_reported_as_the_reason_the_dashboard_is_empty():
    """A demo session is never reported back, so every outcome column stays NULL."""

    gate = _by_name(evaluate(_healthy(healthz=_DEMO_HEALTHZ)))["dashboard_data"]

    assert (gate.status, gate.code) == (FAIL, "start_takes_demo_branch")
    assert "Replace token" in gate.hint and "HOSTAPP_DEMO" in gate.hint
    # ...and it is checked FIRST: a demo rig must not be sent to Pokamind to ask
    # about a module prompt template.
    demo_and_unscored = _healthy(healthz=_DEMO_HEALTHZ,
                                 dashboard_data=_fed_dashboard(last_finished_scored=False))
    assert _by_name(evaluate(demo_and_unscored))["dashboard_data"].code == (
        "start_takes_demo_branch")


def test_the_demo_answer_comes_from_the_running_process_not_this_one():
    """The repo's most-documented trap: an edited .env with no `systemctl restart`.

    The env this check reads says the secret is set; the process serving the kiosk
    was started before the edit and is still handing out the demo url.  Deriving the
    answer here would blame the camera bridge -- or send the operator to Pokamind to
    look at a module prompt template -- for a dashboard that is one restart away.
    """

    stale_env = _healthy(healthz=_DEMO_HEALTHZ, start_demo_branch=False)
    assert _by_name(evaluate(stale_env))["dashboard_data"].code == "start_takes_demo_branch"

    # And the other direction: this process's env looks like demo (no secret exported
    # into the readycheck shell) while the RUNNING hostapp is live.  Not a demo finding.
    stale_shell = _healthy(start_demo_branch=True)
    assert _by_name(evaluate(stale_shell))["dashboard_data"].status == PASS


def test_the_env_derived_demo_answer_is_the_fallback_when_the_hostapp_is_silent():
    """Nothing to ask: the derived answer is all there is, and it is still used."""

    down = _healthy(healthz=HttpResult(False), start_demo_branch=True)

    assert _by_name(evaluate(down))["dashboard_data"].code == "start_takes_demo_branch"


@pytest.mark.parametrize(
    "data, expected",
    [
        (DashboardData(ok=False, reason="db_missing", path="/opt/emori/x.db"),
         (FAIL, "dash_db_missing")),
        (DashboardData(ok=False, reason="db_no_schema", path="/opt/emori/x.db"),
         (FAIL, "dash_db_foreign")),
        # Could not look at all: honest degradation, never a fabricated PASS.
        (DashboardData(ok=False, reason="db_unreadable", path="/opt/emori/x.db"),
         (SKIP, "db_unreadable")),
        (DashboardData(ok=False, reason="db_path_unset"), (SKIP, "db_path_unset")),
    ],
)
def test_an_unreadable_dashboard_db_is_named_not_guessed(data, expected):
    gate = _by_name(evaluate(_healthy(dashboard_data=data)))["dashboard_data"]

    assert (gate.status, gate.code) == expected


def test_a_dashboard_db_nobody_is_writing_is_a_failure():
    """The hostapp is up and writing heartbeats -- but not into THIS file."""

    never = _fed_dashboard(last_heartbeat_ts=0.0)
    gate = _by_name(evaluate(_healthy(dashboard_data=never)))["dashboard_data"]

    assert (gate.status, gate.code) == (FAIL, "dash_db_not_written")
    assert "EMORI_DASH_DB" in gate.hint


def test_a_dead_heartbeat_loop_is_not_reported_as_a_wrong_path():
    """Beats ARE in this file, so the path is right and the LOOP is the fault.

    Two different fixes: one is an EMORI_DASH_DB that does not match the unit's, the
    other is a task that died in a process still answering /healthz.  A hint that
    sends an operator to the first for the second wastes the trip.
    """

    stale = _fed_dashboard(last_heartbeat_ts=_DASH_NOW - 4000.0)
    gate = _by_name(evaluate(_healthy(dashboard_data=stale)))["dashboard_data"]

    assert (gate.status, gate.code) == (FAIL, "dash_heartbeat_stale")
    assert "EMORI_DASH_DB" not in gate.hint
    assert "heartbeat" in gate.hint


def test_a_rig_with_the_heartbeat_writer_switched_off_is_not_accused_of_a_bad_path():
    """EMORI_HEARTBEAT_S=0 disables the writer by design (.env.example).

    ``server.py`` then never creates the task, ``heartbeat_interval_s()`` is 0 and no
    beat can ever exist -- so the liveness proof does not apply, and running it anyway
    accuses the operator of a misconfigured EMORI_DASH_DB on a database that is being
    written perfectly.
    """

    off = _fed_dashboard(heartbeat_interval_s=0.0, last_heartbeat_ts=0.0)

    assert _by_name(evaluate(_healthy(dashboard_data=off)))["dashboard_data"].status == PASS


def test_a_single_late_heartbeat_is_not_a_wrong_file():
    """Three cadences of slack (and a five-minute floor): one late beat on a busy
    CM4 must not accuse an operator of a misconfigured path."""

    late = _fed_dashboard(last_heartbeat_ts=_DASH_NOW - 240.0, heartbeat_interval_s=60.0)

    assert _by_name(evaluate(_healthy(dashboard_data=late)))["dashboard_data"].status == PASS


def test_a_stopped_hostapp_is_not_reported_as_a_path_mismatch():
    """No beats are expected while the hostapp is down; the file is not the fault."""

    evidence = _healthy(healthz=HttpResult(False),
                        dashboard_data=_fed_dashboard(last_heartbeat_ts=0.0))

    assert _by_name(evaluate(evidence))["dashboard_data"].status == PASS


def test_a_robot_that_has_finished_nothing_yet_skips_rather_than_passing():
    evidence = _healthy(dashboard_data=_fed_dashboard(finished_sessions=0,
                                                      any_cycles_ever=False,
                                                      last_completed=False,
                                                      last_finished_cycles=0,
                                                      last_finished_scored=False))
    gate = _by_name(evaluate(evidence))["dashboard_data"]

    assert (gate.status, gate.code) == (SKIP, "no_finished_session_yet")


def test_a_camera_path_that_has_never_produced_a_cycle_fails():
    """The only cycle claim the gate can prove: NOTHING here has ever recorded one."""

    gate = _by_name(evaluate(_healthy(
        dashboard_data=_fed_dashboard(any_cycles_ever=False,
                                      last_finished_cycles=0))))["dashboard_data"]

    assert (gate.status, gate.code) == (FAIL, "dash_no_cycles_ever")
    assert "camera" in gate.hint


def test_one_cycleless_session_warns_instead_of_turning_the_matrix_red():
    """A cycle-less session is an ORDINARY outcome, not a broken dashboard feed.

    dashboard.py declares it legitimate: cycles come from the CAMERA analysis
    stream, so a covered lens, a busy camera bridge or an audio-only conversation
    produces none.  Failing on it exits 1, which the admin phone dashboard paints
    red -- for a robot whose dashboard is being fed perfectly.
    """

    results = evaluate(_healthy(dashboard_data=_fed_dashboard(last_finished_cycles=0)))
    gate = _by_name(results)["dashboard_data"]

    assert (gate.status, gate.code) == (WARN, "dash_last_session_no_cycles")
    assert exit_code(results) == 0


def test_an_ordinary_abandoned_session_is_not_the_row_the_gate_judges(tmp_path):
    """`ended_at` is written for abandonments too, and they have 0 cycles by nature.

    The probe therefore samples the last COMPLETED session; a store whose newest
    finished row is a walk-away-before-the-first-cycle must not fail the gate.
    """

    from emori_adapter.hostapp.metrics_store import MetricsStore

    path = tmp_path / "dash.db"
    store = MetricsStore(str(path), salt="probe-salt")
    now = time.time()
    store.session_started("s-good", person_id="anna-001", started_at=now - 900)
    store.record_cycle("s-good", _dash_signals(), ts=now - 880)
    store.session_finished("s-good", ended_at=now - 700, status="completed",
                           results={"performance_scores": {"adaptability": 55}})
    # ...then an ordinary abandonment: a dead iframe, no cycles, no scores.
    store.session_started("s-dead", person_id="bob-002", started_at=now - 600)
    store.session_finished("s-dead", ended_at=now - 590, status="abandoned",
                           end_reason="session_never_started")
    store.close()

    data = SystemProbe().dashboard_data(str(path))

    assert (data.last_completed, data.last_finished_status) == (True, "completed")
    assert (data.last_finished_cycles, data.last_finished_scored) == (1, True)
    assert data.finished_sessions == 2 and data.any_cycles_ever is True
    healthy = _healthy(dashboard_data=_fed_dashboard(**{
        k: getattr(data, k) for k in ("any_cycles_ever", "last_completed",
                                      "last_finished_cycles", "last_finished_scored")}))
    assert _by_name(evaluate(healthy))["dashboard_data"].status == PASS


def test_a_store_whose_sessions_all_ended_early_has_no_row_to_judge():
    dashboard_data = _fed_dashboard(last_completed=False, last_finished_cycles=0,
                                    last_finished_status="", last_finished_scored=False)
    gate = _by_name(evaluate(_healthy(dashboard_data=dashboard_data)))["dashboard_data"]

    assert (gate.status, gate.code) == (SKIP, "no_completed_session_yet")


def test_an_unscored_last_session_warns_and_points_at_the_module_prompt():
    """WARN, not FAIL: the session ran and the learner got their summary; the
    missing scores are fixed on the Pokamind side, not on the robot."""

    results = evaluate(_healthy(dashboard_data=_fed_dashboard(last_finished_scored=False)))
    gate = _by_name(results)["dashboard_data"]

    assert (gate.status, gate.code) == (WARN, "dash_last_session_unscored")
    assert "analysis settings" in gate.hint.lower()
    assert exit_code(results) == 0
    assert exit_code(results, strict=True) == 0        # a WARN is not a SKIP either


# --- gesture ------------------------------------------------------------------------


def test_gesture_disabled_is_a_skip_and_back_drive_is_a_failure():
    skipped = _by_name(evaluate(_healthy(gesture_enabled=False)))["gesture"]
    assert (skipped.status, skipped.code) == (SKIP, "gesture_disabled")

    gate = _by_name(evaluate(_healthy(gesture_mode="back_drive")))["gesture"]
    assert (gate.status, gate.code) == (FAIL, "gesture_mode_back_drive")
    assert "push_turn" in gate.hint


def test_push_turn_without_the_measured_inversion_is_a_failure():
    """The two knobs are one physical fact (an operator LEFT reads +0.236 roll).

    Unset does not mean "no inversion needed": it means every head turn reads the
    opposite way, silently.  A robot measured the other way says so explicitly.
    """

    unset = _by_name(evaluate(_healthy(gesture_invert="")))["gesture"]
    assert (unset.status, unset.code) == (FAIL, "gesture_invert_unset")
    assert "EMORI_GESTURE_INVERT" in unset.hint

    explicit_zero = _by_name(evaluate(_healthy(gesture_invert="0")))["gesture"]
    assert explicit_zero.status == PASS

    # ...and it is not asked of a robot that has no gesture sensing at all.
    disabled = _by_name(evaluate(_healthy(gesture_enabled=False, gesture_invert="")))
    assert disabled["gesture"].status == SKIP


def test_a_truthy_but_not_literal_invert_value_is_not_a_configured_inversion():
    """The gate has to test what the DRIVER tests, which is the literal '1'.

    driver.py's GestureTicker reads ``os.environ.get(...) == "1"``, so
    EMORI_GESTURE_INVERT=true does not invert anything. Accepting any non-empty
    value here PASSed exactly the robot this gate exists to catch -- every head
    turn reading the opposite way -- with a green matrix
    over it, which is worse than not having the gate.
    """

    for raw in ("true", "True", "TRUE", "yes", "on", "1.0", "-1"):
        gate = _by_name(evaluate(_healthy(gesture_invert=raw)))["gesture"]
        assert (gate.status, gate.code) == (FAIL, "gesture_invert_not_literal_one"), raw
        assert "literal" in gate.hint

    # The two values the driver actually understands are the two that pass.
    assert _by_name(evaluate(_healthy(gesture_invert="1")))["gesture"].status == PASS
    assert _by_name(evaluate(_healthy(gesture_invert="0")))["gesture"].status == PASS


# --- session chain: mint -> SSO redirect -> frontend ---------------------------------
#
# Session-chain failures are silent: a frontend that is down, a token Pokamind refuses,
# a wrong socket URL.  Each one leaves a robot that says nothing.  These pins exist so every one of those links has its OWN
# code in the matrix instead of a shared "it did not work".


def test_a_green_chain_passes_and_a_tokenless_rig_skips_instead_of_failing():
    assert _by_name(evaluate(_healthy()))["session_chain"].status == PASS

    # A robot without its token has nothing to mint and no chain to walk.
    # pokamind_token and demo_off already FAIL that robot; the chain must not
    # double-report it.
    gate = _by_name(evaluate(_tokenless()))
    assert (gate["session_chain"].status, gate["session_chain"].code) == (
        SKIP, "no_robot_token")
    assert (gate["badge_session"].status, gate["pokamind_whoami"].status) == (SKIP, SKIP)
    # One fact, one code: every gate that skips for a missing token names it the same way.
    assert gate["badge_session"].code == "no_robot_token"


def test_the_chain_skips_when_pokamind_itself_never_answered():
    # Same dependency rule as motors-on-daemon: one dead transport must produce ONE
    # failure line, under the gate that actually probes the transport.
    gates = _by_name(evaluate(_healthy(pokamind=HttpResult(False), sso=SsoResult(False),
                                       frontend=HttpResult(False))))

    assert gates["pokamind"].status == FAIL
    assert (gates["session_chain"].status, gates["session_chain"].code) == (
        SKIP, "pokamind_unreachable")


@pytest.mark.parametrize(
    "sso, expected_code",
    [
        (SsoResult(False), "sso_unreachable"),
        (SsoResult(True, 400, "", "error"), "sso_token_rejected"),
        (SsoResult(True, 401, "", "error"), "sso_token_rejected"),
        (SsoResult(True, 500, "", "error"), "sso_server_error"),
        (SsoResult(True, 200), "sso_not_a_redirect"),
        (SsoResult(True, 302, ""), "sso_location_missing"),
        (SsoResult(True, 302, "/auth/partner-callback/emori-robot?emoriKiosk=1"),
         "sso_location_missing"),  # relative: no origin for the kiosk to load
        (SsoResult(True, 302, "http://localhost:4000/login?emoriKiosk=1"),
         "sso_callback_path_unexpected"),
        (SsoResult(True, 302, _CALLBACK.replace("&emoriKiosk=1", "")),
         "sso_kiosk_flag_missing"),
        (SsoResult(True, 302, _CALLBACK), None),
    ],
)
def test_each_broken_link_in_the_sso_leg_gets_its_own_classification(sso, expected_code):
    gate = _by_name(evaluate(_healthy(sso=sso)))["session_chain"]

    if expected_code is None:
        assert gate.status == PASS
    else:
        assert (gate.status, gate.code) == (FAIL, expected_code)


def test_a_rejected_token_quotes_the_status_and_the_field_name_but_never_the_body():
    # A 4xx SSO body can quote the rejected token or the learner's e-mail, so only the
    # status and the NAME of the error field may reach a matrix line or a journal.
    gate = _by_name(evaluate(_healthy(sso=SsoResult(True, 400, "", "error"))))["session_chain"]

    assert (gate.status, gate.code) == (FAIL, "sso_token_rejected")
    assert "400" in gate.hint
    assert "error" in gate.hint
    # The token that signed it, masked, and where to see its scope.
    assert TOKEN.masked in gate.hint and "pokamind_whoami" in gate.hint


def test_a_missing_kiosk_flag_names_the_flag_pokamind_must_enable():
    gate = _by_name(
        evaluate(_healthy(sso=SsoResult(True, 302, _CALLBACK.replace("&emoriKiosk=1", ""))))
    )["session_chain"]

    assert (gate.status, gate.code) == (FAIL, "sso_kiosk_flag_missing")
    assert "emoriKiosk" in gate.hint


@pytest.mark.parametrize(
    "frontend, expected_code",
    [
        (HttpResult(False), "frontend_unreachable"),
        (HttpResult(True, 502, None, "text/html"), "frontend_error"),
        (HttpResult(True, 200, None, "text/html"), None),
    ],
)
def test_the_frontend_origin_from_the_redirect_is_probed_and_classified(frontend, expected_code):
    gate = _by_name(evaluate(_healthy(frontend=frontend)))["session_chain"]

    if expected_code is None:
        assert gate.status == PASS
    else:
        assert (gate.status, gate.code) == (FAIL, expected_code)
        # The hint must name the origin that was actually probed, derived from the
        # redirect - that is the whole point of following the kiosk's own path.
        assert _FRONTEND_ORIGIN in gate.hint


# --- voice menu: provenance and Pokamind's scope -------------------------------------


def test_an_empty_menu_fails_before_anything_else_is_consulted():
    gate = _by_name(evaluate(_healthy(menu=MenuProbe(True, "", ()))))["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, "voice_menu_empty")


@pytest.mark.parametrize(
    "menu, expected",
    [
        # The loader could not even be imported (a dev box without the hostapp extras):
        # honest degradation, never a fabricated PASS.
        (MenuProbe(False, "menu_loader_unavailable"), (SKIP, "menu_loader_unavailable")),
        # A menu that RAISED is a real, robot-breaking config error.
        (MenuProbe(False, "voice_menu_unreadable"), (FAIL, "voice_menu_unreadable")),
    ],
)
def test_voice_menu_gate_never_fabricates_a_pass_for_a_menu_it_could_not_read(menu,
                                                                              expected):
    for evidence in (_healthy(menu=menu), _tokenless(menu=menu)):
        gate = _by_name(evaluate(evidence))["voice_menu"]
        assert (gate.status, gate.code) == expected


# --- the voice-menu gate on a production robot -------------------------------------
# Only Pokamind knows which modules this robot may start, so every tier reaches a
# verdict from the file it serves (provenance) and Pokamind's live answer (scope).


def _prod(menu: MenuProbe, **overrides) -> Evidence:
    """A production-pointed rig serving ``menu``."""

    base = dict(menu=menu, pokamind_base="https://api.pokamind.com", lang="en")
    base.update(overrides)
    return _healthy(**base)


_PROD_MENU = MenuProbe(
    True, "", (("1", "ProdEnglishModule001"),), source="configs/prod/voice_topics.yaml",
    lang="en", prod_source="configs/prod/voice_topics.yaml", digest="sha256:aa",
    prod_digest="sha256:aa",
)


def test_a_fork_menu_pokamind_does_not_cover_warns_and_says_where_to_look():
    gate = _by_name(evaluate(_prod(_PROD_MENU)))["voice_menu"]

    # Unproven, and it SAYS so - never "skip, not my problem".
    assert (gate.status, gate.code) == (WARN, "voice_menu_unverified_against_prod")
    assert "voice_menu_in_scope" in gate.hint


def test_a_fork_menu_pokamind_covers_whole_passes():
    scoped = whoami(languages=("en",), modules=(whoami_module("ProdEnglishModule001",
                                                              language="en"),))

    gate = _by_name(evaluate(_prod(_PROD_MENU, whoami=scoped)))["voice_menu"]

    assert (gate.status, gate.code) == (PASS, "modules_verified_by_whoami")


def test_a_menu_that_is_not_the_generated_production_one_fails():
    """A file-level deploy that shadowed the generated menu with the repository's own
    copy, seen from the gate.  Byte-difference is the evidence."""
    shadowed = MenuProbe(
        True, "", (("1", "ProdCatalanModule001"),), source="configs/voice_topics.ca.yaml",
        lang="ca", prod_source="configs/prod/voice_topics.ca.yaml", digest="sha256:bb",
        prod_digest="sha256:cc",
    )
    gate = _by_name(evaluate(_prod(shadowed, lang="ca")))["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, "voice_menu_not_the_prod_menu")
    assert "configs/voice_topics.ca.yaml" in gate.hint
    assert "configs/prod/voice_topics.ca.yaml" in gate.hint


def test_a_shadowing_file_with_identical_bytes_is_not_a_failure():
    # Same content at a different path is a cosmetic difference, not a dead menu; a gate
    # that cried wolf there would be a gate somebody suppresses.
    same = MenuProbe(
        True, "", (("1", "ProdEnglishModule001"),), source="configs/voice_topics.en.yaml",
        lang="en", prod_source="configs/prod/voice_topics.yaml", digest="sha256:aa",
        prod_digest="sha256:aa",
    )
    gate = _by_name(evaluate(_prod(same)))["voice_menu"]

    assert gate.status != FAIL


def test_a_production_robot_with_no_generated_menu_at_all_fails():
    orphan = MenuProbe(
        True, "", (("1", "AbCdEf012345"),), source="configs/voice_topics.yaml",
        lang="ca", prod_source="", digest="sha256:bb",
    )
    gate = _by_name(evaluate(_prod(orphan, lang="ca")))["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, "voice_menu_no_prod_menu")
    assert "EMORI_VOICE_TOPICS_CA" in gate.hint  # the documented way to point it elsewhere


def test_the_built_in_default_menu_on_a_production_robot_fails():
    # `source=""` means no file resolved at all, so VoiceMenu.default_menu()'s
    # placeholder ids are what the robot would speak.
    gate = _by_name(evaluate(_prod(
        MenuProbe(True, "", (("1", "demo-module-1"),), lang="en")
    )))["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, "voice_menu_built_in_default")


def test_the_shipped_example_menu_on_a_production_robot_fails():
    gate = _by_name(evaluate(_prod(MenuProbe(
        True, "", (("1", "PLACEHOLDER01"),),
        source="configs/voice_topics.example.yaml", lang="en",
        prod_source="configs/prod/voice_topics.yaml",
    ))))["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, "voice_menu_example_file")


def test_an_operator_override_is_reported_but_never_treated_as_a_clobber():
    """`EMORI_VOICE_TOPICS_CA=configs/voice_topics.ca.yaml` is how a robot is run while
    the menu under configs/prod/ is known to be wrong.  It is deliberate, visible, and
    the one menu decision a deploy cannot overwrite - so it must not read as the
    accident it looks like."""
    overridden = MenuProbe(
        True, "", (("1", "ProdCatalanModule001"),), source="configs/voice_topics.ca.yaml",
        lang="ca", prod_source="configs/prod/voice_topics.ca.yaml", digest="sha256:bb",
        prod_digest="sha256:cc", override_env="EMORI_VOICE_TOPICS_CA",
    )
    gate = _by_name(evaluate(_prod(overridden, lang="ca")))["voice_menu"]

    assert (gate.status, gate.code) == (WARN, "voice_menu_override_differs_from_prod")
    assert "EMORI_VOICE_TOPICS_CA" in gate.hint
    assert "voice_menu_in_scope" in gate.hint


def test_the_simulators_development_menu_is_never_a_robots_menu():
    """The tracked development menus (configs/voice_topics{,.ca}.yaml) carry ``dev-``
    ids: right for the simulator's demo sessions, dead on any Pokamind -- with or
    without a token."""
    dev = MenuProbe(True, "", (("1", "dev-en-module-1"),), source="configs/voice_topics.yaml",
                    lang="en", tier="dev", digest="sha256:de")

    for evidence in (_prod(dev), _tokenless(menu=dev, lang="en")):
        gate = _by_name(evaluate(evidence))["voice_menu"]
        assert (gate.status, gate.code) == (FAIL, "voice_menu_placeholder_ids")
        assert "say '1' -> module dev-en-module-1" in gate.hint


# --- the menu tiers of a robot from the public tree -----------------------------------
# site: saved on the admin page (or adopted from an older install); auto: built from
# this robot's Pokamind record; prod: a fork's menu; dev/builtin: never this robot's.


_SITE_MENU = MenuProbe(
    True, "", _MENU_OPTIONS, source="/opt/emori/configs/site/voice_topics.ca.yaml",
    lang="ca", digest="sha256:5e", tier="site",
)
_AUTO_MENU = MenuProbe(
    True, "", _MENU_OPTIONS, source="/opt/emori/configs/site/auto/voice_topics.ca.yaml",
    lang="ca", digest="sha256:a7", tier="auto", authored="pokamind",
)


def _menu_gates(menu: MenuProbe, **overrides) -> dict[str, GateResult]:
    return _by_name(evaluate(_prod(menu, **{"lang": "ca", **overrides})))


def test_a_menu_saved_on_this_robot_passes_once_pokamind_scopes_it():
    gates = _menu_gates(_SITE_MENU)

    assert (gates["voice_menu"].status, gates["voice_menu"].code) == (PASS, "voice_menu_site")
    assert gates["voice_menu_in_scope"].code == "menu_in_scope"


def test_a_saved_menu_outranks_an_older_installs_menu_without_being_a_shadow():
    older = dataclasses.replace(_SITE_MENU, prod_source="configs/prod/voice_topics.ca.yaml",
                                prod_digest="sha256:0d")

    gate = _menu_gates(older)["voice_menu"]

    assert (gate.status, gate.code) == (PASS, "voice_menu_site")


def test_a_saved_menu_nothing_has_verified_warns_and_says_who_can():
    gate = _by_name(evaluate(_tokenless(menu=_SITE_MENU)))["voice_menu"]

    assert (gate.status, gate.code) == (WARN, "voice_menu_unverified_against_prod")
    assert "saved on this robot" in gate.hint
    assert "only Pokamind can" in gate.hint and "Replace token" in gate.hint


def test_a_saved_menu_pokamind_could_not_be_asked_about_warns_without_blaming_it():
    gate = _menu_gates(_SITE_MENU, whoami=WhoamiResult(code="whoami_unreachable"))[
        "voice_menu"]

    assert (gate.status, gate.code) == (WARN, "voice_menu_unverified_against_prod")
    assert "pokamind_whoami" in gate.hint


def test_a_menu_built_from_the_record_with_spoken_names_passes():
    gate = _menu_gates(_AUTO_MENU)["voice_menu"]

    assert (gate.status, gate.code) == (PASS, "voice_menu_from_pokamind")


@pytest.mark.parametrize("authored, some", [("title_draft", False), ("mixed", True)])
def test_a_menu_drafted_from_module_titles_warns(authored, some):
    drafted = dataclasses.replace(_AUTO_MENU, authored=authored)

    results = evaluate(_prod(drafted, lang="ca"))
    gate = _by_name(results)["voice_menu"]

    assert (gate.status, gate.code) == (WARN, "voice_menu_auto_draft")
    assert "screen titles" in gate.hint and "Menu tab" in gate.hint
    assert f"'{authored}'" in gate.hint
    assert ("some of" in gate.hint) is some
    assert exit_code(results) == 0  # number selection still works


def test_a_drafted_menu_with_a_dead_module_still_fails_on_the_dead_module():
    drafted = dataclasses.replace(_AUTO_MENU, authored="title_draft")
    dead = whoami(modules=(whoami_module(_MENU_OPTIONS[0][1]),
                           whoami_module(_MENU_OPTIONS[1][1], available=False,
                                         problem="not_found")))

    gates = _menu_gates(drafted, whoami=dead)

    assert gates["voice_menu"].code == "voice_menu_auto_draft"
    gate = gates["voice_menu_in_scope"]
    assert (gate.status, gate.code) == (FAIL, "voice_menu_module_unavailable")
    assert f"say '2' -> module {_MENU_OPTIONS[1][1]} (not_found)" in gate.hint
    assert "Sync now" in gate.hint


def test_a_built_menu_is_not_failed_for_pokamind_being_unreachable_right_now():
    gates = _menu_gates(_AUTO_MENU, whoami=WhoamiResult(code="whoami_unreachable"))

    assert (gates["voice_menu"].status, gates["voice_menu"].code) == (
        PASS, "voice_menu_from_pokamind")
    assert gates["pokamind_whoami"].status == FAIL  # one line per fault, and it is there


def test_a_built_menu_pokamind_no_longer_covers_is_sent_to_sync():
    narrow = whoami(modules=(whoami_module(_MENU_OPTIONS[0][1]),))

    gates = _menu_gates(_AUTO_MENU, whoami=narrow)

    assert (gates["voice_menu"].status, gates["voice_menu"].code) == (
        WARN, "voice_menu_unverified_against_prod")
    assert "Sync now" in gates["voice_menu"].hint
    assert gates["voice_menu_in_scope"].code == "voice_menu_module_not_in_scope"
    assert "Sync now" in gates["voice_menu_in_scope"].hint


@pytest.mark.parametrize("module_id", ["example-ca-module-1", "demo-module-2",
                                       "dev-en-module-3"])
@pytest.mark.parametrize("menu, fix", [
    (_SITE_MENU, "Reset to Pokamind"),
    (dataclasses.replace(_SITE_MENU, tier="env", override_env="EMORI_VOICE_TOPICS_CA",
                         source="/srv/menus/ca.yaml"), "unset it"),
    (dataclasses.replace(_SITE_MENU, tier="prod",
                         source="configs/prod/voice_topics.ca.yaml",
                         prod_source="configs/prod/voice_topics.ca.yaml"),
     "remove configs/prod/voice_topics.ca.yaml"),
], ids=["site", "env", "prod"])
def test_placeholder_ids_on_a_robot_with_its_own_token_fail(module_id, menu, fix):
    offered = dataclasses.replace(menu, options=(("1", _MENU_OPTIONS[0][1]),
                                                 ("2", module_id)))

    gate = _menu_gates(offered)["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, "voice_menu_placeholder_ids")
    assert f"say '2' -> module {module_id}" in gate.hint
    assert _MENU_OPTIONS[0][1] not in gate.hint
    assert fix in gate.hint


def test_placeholder_ids_fail_without_a_token_too():
    offered = dataclasses.replace(_SITE_MENU, options=(("1", "example-ca-module-1"),))

    gate = _by_name(evaluate(_tokenless(menu=offered)))["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, "voice_menu_placeholder_ids")


def test_a_development_menu_on_a_production_robot_is_sent_to_its_own_menu():
    dev = MenuProbe(True, "", (("1", "AbCdEf012345"),), source="configs/voice_topics.yaml",
                    lang="en", tier="dev", digest="sha256:de")

    gate = _by_name(evaluate(_prod(dev)))["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, "voice_menu_no_prod_menu")
    assert "Sync now" in gate.hint and "Menu tab" in gate.hint
    assert "EMORI_VOICE_TOPICS_EN" in gate.hint
    assert "under configs/prod/ and redeploy" not in gate.hint


@pytest.mark.parametrize("menu, code", [
    (MenuProbe(True, "", (("1", "dev-en-module-1"),), source="configs/voice_topics.yaml",
               tier="dev"), "voice_menu_placeholder_ids"),
    (MenuProbe(True, "", (("1", "demo-module-1"),), lang="en", tier="builtin"),
     "voice_menu_built_in_default"),
    (MenuProbe(True, "", (("1", "PLACEHOLDER01"),),
               source="configs/voice_topics.example.yaml", tier="dev"),
     "voice_menu_example_file"),
], ids=["development", "builtin", "example"])
def test_a_menu_that_is_not_this_robots_names_the_way_to_its_own(menu, code):
    gate = _by_name(evaluate(_prod(menu)))["voice_menu"]

    assert (gate.status, gate.code) == (FAIL, code)
    assert "Pokamind tab -> Sync now" in gate.hint and "Menu tab" in gate.hint
    # ...and a robot without a token is told the one thing that gets it there.
    assert "Replace token" in gate.hint
    assert "under configs/prod" not in gate.hint


def test_an_out_of_scope_saved_menu_is_fixed_on_the_menu_tab():
    gate = _menu_gates(_SITE_MENU, whoami=whoami(modules=(whoami_module(
        _MENU_OPTIONS[0][1]),)))["voice_menu_in_scope"]

    assert gate.code == "voice_menu_module_not_in_scope"
    assert "Menu tab" in gate.hint and "configs/prod/voice_topics" not in gate.hint


def test_an_unavailable_module_on_a_saved_menu_names_the_menu_tab():
    dead = whoami(modules=(whoami_module(_MENU_OPTIONS[0][1], available=False),
                           whoami_module(_MENU_OPTIONS[1][1])))

    gate = _menu_gates(_SITE_MENU, whoami=dead)["voice_menu_in_scope"]

    assert gate.code == "voice_menu_module_unavailable"
    assert "Menu tab" in gate.hint and "configs/prod/" not in gate.hint


def test_an_unreadable_menu_names_every_tier_it_could_come_from():
    gate = _by_name(evaluate(_healthy(menu=MenuProbe(False, "voice_menu_unreadable"))))[
        "voice_menu"]

    assert gate.code == "voice_menu_unreadable"
    assert "Reset to Pokamind" in gate.hint and "Sync now" in gate.hint
    assert "configs/prod/voice_topics*" not in gate.hint


# --- the optional ROS 2 lane (gate_ros2) ---------------------------------------------


def test_the_ros2_lane_is_a_pass_and_not_a_skip_where_nobody_armed_it():
    """A feature nobody asked for is not a dependency that could not be probed.

    SKIP would fail `--strict` on a completely healthy shipped rig, which is how a
    strict boot-time readycheck ends up being run without --strict.
    """

    results = evaluate(_healthy())
    gate = _by_name(results)["ros2"]

    assert (gate.status, gate.code) == (PASS, "ros2_lane_off")
    assert exit_code(results, strict=True) == 0


def test_an_armed_and_healthy_ros2_lane_passes():
    assert _by_name(evaluate(_ros2()))["ros2"] == GateResult("ros2", PASS, "ros2_up")


def test_an_armed_lane_without_docker_is_an_unfinished_install_not_a_broken_robot():
    gate = _by_name(evaluate(_ros2(ros2_container=CommandResult(False))))["ros2"]

    assert (gate.status, gate.code) == (SKIP, "docker_unavailable")


@pytest.mark.parametrize("inspect, code", [
    # `docker inspect` on a name that does not exist exits non-zero.
    (CommandResult(True, 1, ""), "ros2_container_absent"),
    # Created but down - the crash-loop an "active" systemd unit hides.
    (CommandResult(True, 0, "false\n"), "ros2_container_stopped"),
])
def test_an_armed_lane_whose_container_is_not_running_fails(inspect, code):
    gate = _by_name(evaluate(_ros2(ros2_container=inspect)))["ros2"]

    assert (gate.status, gate.code) == (FAIL, code)


@pytest.mark.parametrize("fiware", [
    CommandResult(True, 0, "1\n"),
    CommandResult(True, 0, "true\n"),
    # printenv exits non-zero for a variable that is not set, and the bridge is
    # enabled by DEFAULT - so omission is the way you fall into this trap.
    CommandResult(True, 1, ""),
])
def test_a_second_fiware_writer_in_the_container_fails_the_gate(fiware):
    """The hostapp already historizes every cycle; two writers double every KPI."""

    gate = _by_name(evaluate(_ros2(ros2_fiware=fiware)))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_fiware_bridge_on")


def test_an_unreadable_container_env_is_a_skip_not_a_fabricated_pass():
    gate = _by_name(evaluate(_ros2(ros2_fiware=CommandResult(False))))["ros2"]

    assert (gate.status, gate.code) == (SKIP, "ros2_env_unreadable")


@pytest.mark.parametrize("dead", ["emori_adapter", "hri_context_analyzer",
                                  "hri_engagement_behaviors", "skill_facade"])
def test_a_node_that_died_at_import_is_not_a_running_container_reported_as_up(dead):
    """The container is a `ros2 launch`, and `ros2 launch` outlives its own nodes.

    A node that dies on an import (a missing `hri_msgs`, an unbuilt overlay) takes every
    topic it publishes with it, while `.State.Running` stays true and `printenv` keeps
    answering - so every other question this gate asks is still green and the entire
    `/context/*` fan-out publishes nothing for the whole session.  That is precisely the
    shape of green-looking nothing the gate exists to refuse one level up.
    """

    listed = "".join(line + "\n" for line in _NODE_LIST.split()
                     if line != f"/{dead}")

    gate = _by_name(evaluate(_ros2(ros2_nodes=CommandResult(True, 0, listed))))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_graph_incomplete")
    assert dead in gate.hint


def test_an_empty_graph_names_every_missing_node_rather_than_only_the_first():
    gate = _by_name(evaluate(_ros2(ros2_nodes=CommandResult(True, 0, "\n"))))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_graph_incomplete")
    for name in ("emori_adapter", "hri_context_analyzer", "hri_engagement_behaviors",
                 "skill_facade"):
        assert name in gate.hint


def test_the_condition_gated_fiware_bridge_is_never_required_to_be_running():
    """`with_fiware` is false on the robot ON PURPOSE - the hostapp is the writer.

    Requiring `fiware_bridge` in the node list would fail the correctly-configured rig
    and push an operator into turning the double-counting writer back on.
    """

    assert _by_name(evaluate(_ros2()))["ros2"] == GateResult("ros2", PASS, "ros2_up")


def test_a_namespaced_node_list_still_matches_the_launch_files_names():
    listed = "".join(f"/emori{name}\n" for name in _NODE_LIST.split())

    assert _by_name(evaluate(_ros2(ros2_nodes=CommandResult(True, 0, listed))))["ros2"] \
        == GateResult("ros2", PASS, "ros2_up")


def test_a_node_list_that_cannot_run_at_all_is_a_skip_and_a_failing_one_is_a_fail():
    """Two different unknowns: no probe (SKIP) and a probe that answered "no" (FAIL)."""

    unreadable = _by_name(evaluate(_ros2(ros2_nodes=CommandResult(False))))["ros2"]
    assert (unreadable.status, unreadable.code) == (SKIP, "ros2_graph_unreadable")

    failed = _by_name(evaluate(_ros2(ros2_nodes=CommandResult(True, 1, ""))))["ros2"]
    assert (failed.status, failed.code) == (FAIL, "ros2_graph_unlistable")


def test_a_dead_graph_is_reported_before_the_skill_intake_is_blamed():
    """A half-dead graph must not be reported as "your /api/skill is not mounted"."""

    gate = _by_name(evaluate(_ros2(
        ros_motion_owner="ros",
        ros2_owner=CommandResult(True, 0, "ros\n"),
        ros2_nodes=CommandResult(True, 0, "/emori_adapter\n"),
        skill_api=HttpResult(True, 404, {}),
    )))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_graph_incomplete")


def test_an_env_edited_without_recreating_the_container_is_reported_as_drift():
    """compose interpolates .env once, at `up`.  Editing EMORI_ROS_MOTION_OWNER and
    restarting nothing leaves the graph on the previous answer - which on the robot
    reads exactly like "ROS mode does not work"."""

    gate = _by_name(evaluate(_ros2(
        ros_motion_owner="ros", ros2_owner=CommandResult(True, 0, "hostapp\n"))))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "ros2_owner_drift")
    assert "restart emori-ros2" in gate.hint


def test_a_typo_owner_reads_as_the_shipped_default_on_both_sides():
    """skill_api.motion_owner degrades an unknown value to `hostapp` rather than to
    permission; this gate must normalise identically or a typo becomes "drift"."""

    gate = _by_name(evaluate(_ros2(
        ros_motion_owner="hsotapp", ros2_owner=CommandResult(True, 0, "hsotapp\n"))))["ros2"]

    assert (gate.status, gate.code) == (PASS, "ros2_up")


def test_the_ros_lane_owning_motion_with_no_intake_mounted_fails():
    """The graph would decide beautifully and move nothing: every POST gets 403."""

    gate = _by_name(evaluate(_ros2(
        ros_motion_owner="ros",
        ros2_owner=CommandResult(True, 0, "ros\n"),
        skill_api=HttpResult(True, 404, {"detail": "Not Found"}),
    )))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "skill_api_not_mounted")
    assert "EMORI_SKILL_API=1" in gate.hint


def test_the_ros_lane_owning_motion_with_the_intake_mounted_passes():
    gate = _by_name(evaluate(_ros2(
        ros_motion_owner="ros", ros2_owner=CommandResult(True, 0, "ros\n"))))["ros2"]

    assert (gate.status, gate.code) == (PASS, "ros2_up")


def test_an_unmounted_intake_is_not_a_fault_while_the_hostapp_owns_motion():
    """On the shipped default the facade POSTs nothing, so there is nothing to mount."""

    gate = _by_name(evaluate(_ros2(skill_api=HttpResult(True, 404, {}))))["ros2"]

    assert (gate.status, gate.code) == (PASS, "ros2_up")


def test_a_dead_hostapp_cannot_make_the_ros2_gate_accuse_the_skill_intake():
    gate = _by_name(evaluate(_ros2(
        ros_motion_owner="ros",
        ros2_owner=CommandResult(True, 0, "ros\n"),
        skill_api=HttpResult(False),
    )))["ros2"]

    assert (gate.status, gate.code) == (SKIP, "hostapp_unreachable")


# --- the spoken failure line ---------------------------------------------------------


class _SpeakingProbe:
    def __init__(self, spoken=True):
        self.spoken = spoken
        self.calls: list[tuple[str, str, str]] = []

    def speak(self, hostapp_url: str, lang: str, text: str) -> bool:
        self.calls.append((hostapp_url, lang, text))
        return self.spoken


def test_a_broken_link_is_spoken_in_the_kiosk_language():
    probe = _SpeakingProbe()
    evidence = _healthy(lang="ca", sso=SsoResult(False))

    assert announce_failure(evaluate(evidence), evidence, probe) is True
    assert probe.calls == [("http://127.0.0.1:8080", "ca", SPOKEN_LINES["ca"][0])]
    assert "Pokamind" in SPOKEN_LINES["ca"][0]


def test_the_spoken_line_never_blames_pokamind_for_a_local_fault():
    # "No puc connectar amb Pokamind" on a robot whose motors are disabled would be a lie,
    # and the operator would go and restart the laptop stack for nothing.
    probe = _SpeakingProbe()
    evidence = _healthy(lang="en", motors=HttpResult(True, 200, {"mode": "disabled"}),
                        whoami=whoami(languages=("ca", "en")))

    announce_failure(evaluate(evidence), evidence, probe)

    assert probe.calls == [("http://127.0.0.1:8080", "en", SPOKEN_LINES["en"][1])]


@pytest.mark.parametrize("gate_break", [
    {"sso": SsoResult(False)},
    {"menu": MenuProbe(True, "", (("1", "dev-en-module-1"),),
                       source="configs/voice_topics.yaml", tier="dev")},
    {"whoami": whoami(modules=(whoami_module(_MENU_OPTIONS[0][1]),))},
    {"pokamind": HttpResult(False), "sso": SsoResult(False), "frontend": HttpResult(False)},
])
def test_every_session_chain_link_speaks_the_pokamind_line(gate_break):
    probe = _SpeakingProbe()
    evidence = _healthy(lang="en", **gate_break)

    announce_failure(evaluate(evidence), evidence, probe)

    assert [call[2] for call in probe.calls] == [SPOKEN_LINES["en"][0]]


def test_a_green_rig_stays_silent_and_an_unknown_language_falls_back_to_english():
    probe = _SpeakingProbe()
    healthy = _healthy()
    assert announce_failure(evaluate(healthy), healthy, probe) is False
    assert probe.calls == []

    evidence = _healthy(lang="xx-YY", sso=SsoResult(False))
    announce_failure(evaluate(evidence), evidence, probe)
    assert probe.calls[-1][2] == SPOKEN_LINES["en"][0]

    # A regional tag of a language we DO speak still resolves to it.
    evidence = _healthy(lang="ca-ES", sso=SsoResult(False))
    announce_failure(evaluate(evidence), evidence, probe)
    assert probe.calls[-1][2] == SPOKEN_LINES["ca"][0]


@pytest.mark.parametrize("lang, expected", [("ca", "ca"), ("ca-ES", "ca"), ("EN_gb", "en"),
                                            ("xx-YY", "en"), ("", "en")])
def test_the_voice_always_matches_the_language_of_the_words(lang, expected):
    # PiperTts._voice normalises the tag and falls back to the FIRST loaded voice for a
    # language it has no model for (tts.py:50-52) - so asking for another language while
    # speaking the English fallback line can hand the sentence to the Catalan voice.  The resolved
    # language, not the raw EMORI_LANG, is what /api/tts must be asked for.
    probe = _SpeakingProbe()
    evidence = _healthy(lang=lang, sso=SsoResult(False))

    announce_failure(evaluate(evidence), evidence, probe)

    assert probe.calls[-1][1] == expected
    assert probe.calls[-1][2] == SPOKEN_LINES[expected][0]


def test_every_spoken_line_is_short_enough_to_be_heard_before_anyone_walks_away():
    for lang, lines in SPOKEN_LINES.items():
        assert len(lines) == 2, lang
        for line in lines:
            assert 0 < len(line) <= 48, (lang, line)


# --- structural invariants ----------------------------------------------------------


def test_gate_result_rejects_unsafe_codes_hintless_failures_and_bad_statuses():
    with pytest.raises(ValueError):
        GateResult("hostapp", FAIL, "Not_A_Safe_Code", "hint")
    with pytest.raises(ValueError):
        GateResult("hostapp", FAIL, "safe_code")  # a failure MUST carry remediation
    with pytest.raises(ValueError):
        GateResult("hostapp", "green", "safe_code", "hint")
    assert GateResult("hostapp", SKIP, "hostapp_unreachable").hint == ""


def test_every_outcome_keeps_the_code_grammar_and_every_failure_has_a_hint():
    scenarios = [
        _healthy(),
        _healthy(healthz=HttpResult(False)),
        _healthy(daemon=HttpResult(False), motors=HttpResult(False)),
        _healthy(gesture_enabled=False, gesture_mode="back_drive"),
        _healthy(camera=CommandResult(False)),
        _healthy(tts=HttpResult(True, 503, {}, "application/json"),
                 stt=HttpResult(True, 200, {"available": False}),
                 pokamind=HttpResult(False),
                 motors=HttpResult(True, 200, {"mode": "disabled"})),
        _healthy(sso=SsoResult(True, 400, "", "error"), frontend=HttpResult(False)),
        _healthy(sso=SsoResult(True, 302, _CALLBACK.replace("&emoriKiosk=1", ""))),
        _tokenless(),
        _tokenless(healthz=HttpResult(False)),
        _healthy(token=TokenProbe(state="invalid", source="file", path="/x",
                                  error="seed_invalid"), whoami=None),
        _healthy(env_robot_id="some-other-robot"),
        _healthy(whoami=WhoamiResult(code="robot_revoked", http_status=401)),
        _healthy(whoami=WhoamiResult(code="X" * 90)),
        _healthy(whoami=whoami(org="")),
        _healthy(whoami=whoami(languages=("es",))),
        _healthy(whoami=whoami(course_id="other-course")),
        _healthy(whoami=whoami(modules=(whoami_module("MOD0000000000000001a",
                                                      available=False,
                                                      problem="not_found"),))),
        _healthy(sso=SsoResult(True, 403, "", "error", "module_not_in_scope")),
        _healthy(menu=MenuProbe(False, "menu_loader_unavailable")),
        _healthy(menu=MenuProbe(False, "voice_menu_unreadable")),
        _healthy(menu=MenuProbe(True, "", (("1", "dev-en-module-1"),),
                                source="configs/voice_topics.yaml", tier="dev")),
        _tokenless(menu=MenuProbe(True, "", _MENU_OPTIONS,
                                  source="/opt/emori/configs/site/voice_topics.ca.yaml",
                                  tier="site")),
        _healthy(admin=HttpResult(False)),
        _healthy(admin=HttpResult(True, 200, {"status": "ok", "demo": False})),
        _healthy(healthz=_DEMO_HEALTHZ),
        _healthy(healthz=HttpResult(False), start_demo_branch=True),
        _healthy(dashboard_data=DashboardData(ok=False, reason="db_missing",
                                              path="/opt/emori/artifacts/dashboard.db")),
        _healthy(dashboard_data=DashboardData(ok=False, reason="db_no_schema", path="/x.db")),
        _healthy(dashboard_data=DashboardData(ok=False, reason="db_unreadable", path="/x.db")),
        _healthy(dashboard_data=_fed_dashboard(last_heartbeat_ts=0.0)),
        _healthy(dashboard_data=_fed_dashboard(last_heartbeat_ts=_DASH_NOW - 9000.0)),
        _healthy(dashboard_data=_fed_dashboard(heartbeat_interval_s=0.0,
                                               last_heartbeat_ts=0.0)),
        _healthy(dashboard_data=_fed_dashboard(finished_sessions=0)),
        _healthy(dashboard_data=_fed_dashboard(any_cycles_ever=False)),
        _healthy(dashboard_data=_fed_dashboard(last_completed=False)),
        _healthy(dashboard_data=_fed_dashboard(last_finished_cycles=0)),
        _healthy(dashboard_data=_fed_dashboard(last_finished_scored=False)),
        _healthy(gaze_video="", camera_ros=CommandResult()),
        _healthy(camera_ros=CommandResult(False)),
        _healthy(camera_ros=CommandResult(True, 1, "")),
        _healthy(camera_ros=CommandResult(True, 0, _V4L2_UNFED)),
        _ros2(ros2_container=CommandResult(False)),
        _ros2(ros2_container=CommandResult(True, 1, "")),
        _ros2(ros2_container=CommandResult(True, 0, "false\n")),
        _ros2(ros2_fiware=CommandResult(True, 0, "1\n")),
        _ros2(ros2_owner=CommandResult(True, 0, "ros\n")),
        _ros2(ros2_nodes=CommandResult(False)),
        _ros2(ros2_nodes=CommandResult(True, 1, "")),
        _ros2(ros2_nodes=CommandResult(True, 0, "/emori_adapter\n")),
        _ros2(ros_motion_owner="ros", ros2_owner=CommandResult(True, 0, "ros\n"),
              skill_api=HttpResult(True, 404, {})),
    ]
    seen_names: set[str] = set()
    for evidence in scenarios:
        for gate in evaluate(evidence):
            assert SAFE_CODE_RE.fullmatch(gate.name), gate.name
            assert SAFE_CODE_RE.fullmatch(gate.code), (gate.name, gate.code)
            assert gate.status not in (FAIL, WARN) or gate.hint, gate.name
            seen_names.add(gate.name)
    assert len(seen_names) == 28


def test_exit_code_is_nonzero_on_failure_and_strict_flags_skips():
    healthy = evaluate(_healthy())
    assert exit_code(healthy) == 0
    assert exit_code(healthy, strict=True) == 0

    skipped = evaluate(_healthy(camera=CommandResult(False)))
    assert exit_code(skipped) == 0
    assert exit_code(skipped, strict=True) == 2

    failed = evaluate(_healthy(motors=HttpResult(True, 200, {"mode": "disabled"})))
    assert exit_code(failed) == 1
    assert exit_code(failed, strict=True) == 1  # a real failure outranks a skip


def test_matrix_lists_every_gate_and_prints_hints_only_for_failures():
    results = evaluate(_healthy(motors=HttpResult(True, 200, {"mode": "disabled"})))
    text = format_matrix(results)

    for gate in results:
        assert gate.name in text
    assert "/api/motors/set_mode/enabled" in text
    assert text.count("  ! ") == 1
    assert "FAIL 1" in text
    assert "WARN 0" in text


def test_the_matrix_marks_an_advisory_apart_from_a_failure():
    """A WARN that reads like a FAIL is a WARN somebody stops reading."""

    results = [GateResult("daemon", PASS, "daemon_running"),
               GateResult("gaze", WARN, "gaze_lane_unfed", "feed the second loopback node")]
    text = format_matrix(results)

    assert any(line.split()[:2] == ["gaze", "WARN"] for line in text.splitlines())
    assert text.count("  ~ ") == 1
    assert text.count("  ! ") == 0
    assert "FAIL 0" in text and "WARN 1" in text


def test_a_warn_without_remediation_is_a_construction_error():
    """Same contract as FAIL: an advisory nobody can act on is noise."""

    with pytest.raises(ValueError, match="gate_hint_required"):
        GateResult("gaze", WARN, "gaze_lane_unfed")


# --- collection layer ---------------------------------------------------------------


class _FakeProbe:
    def __init__(self, environ: dict[str, str], *, sso: SsoResult | None = None,
                 menu: MenuProbe | None = None, answers: dict[str, HttpResult] | None = None,
                 badge_claims: tuple[str, str] = ("", ""),
                 dashboard_data: DashboardData | None = None,
                 commands: dict[tuple[str, ...], CommandResult] | None = None,
                 token_load: robot_token.TokenLoad | None = None):
        self._commands = commands or {}
        self._dashboard_data = dashboard_data if dashboard_data is not None else DashboardData()
        self.dash_db_paths: list[str] = []
        self._environ = environ
        self._sso = sso if sso is not None else SsoResult(False)
        self._menu = menu if menu is not None else MenuProbe(False, "menu_loader_unavailable")
        self._token_load = token_load if token_load is not None else robot_token.TokenLoad()
        self._answers = answers or {}
        self._badge_claims = badge_claims
        self.urls: list[str] = []
        self.bearers: list[str] = []
        self.sso_urls: list[str] = []
        self.menu_langs: list[str] = []
        self.argv: list[list[str]] = []

    def env(self, name: str, default: str = "") -> str:
        return self._environ.get(name, default)

    def http(self, url: str, *, bearer: str = "") -> HttpResult:
        self.urls.append(url)
        self.bearers.append(bearer)
        # Default: nothing answers, so collect must degrade rather than raise.
        return self._answers.get(url, HttpResult(False))

    def sso(self, url: str) -> SsoResult:
        self.sso_urls.append(url)
        return self._sso

    def voice_menu(self, lang: str) -> MenuProbe:
        self.menu_langs.append(lang)
        return self._menu

    def robot_token(self) -> robot_token.TokenLoad:
        return self._token_load

    def roster_badge_claims(self) -> tuple[str, str]:
        return self._badge_claims

    def command(
        self, argv: list[str], *, timeout_s: float | None = None
    ) -> CommandResult:
        self.argv.append(list(argv))
        if timeout_s is not None:
            self.command_timeouts = getattr(self, "command_timeouts", [])
            self.command_timeouts.append((tuple(argv), timeout_s))
        return self._commands.get(tuple(argv), CommandResult(True, 0, _V4L2_FED))

    def dashboard_data(self, path: str) -> DashboardData:
        self.dash_db_paths.append(path)
        return self._dashboard_data


def test_collect_targets_the_configured_endpoints_and_reads_the_deployment_env():
    probe = _FakeProbe(
        {
            "HOSTAPP_PORT": "8095",
            "REACHY_BASE_URL": "http://192.0.2.10:8000/",
            "POKAMIND_BASE": "http://localhost:4000/",
            "EMORI_LANG": "ca",
            "EMORI_GESTURE": "1",
            "EMORI_GESTURE_MODE": "push_turn",
            "EMORI_GESTURE_INVERT": "1",
        }
    )

    evidence = collect(probe)

    assert "http://127.0.0.1:8095/healthz" in probe.urls
    assert "http://127.0.0.1:8095/api/stt/health" in probe.urls
    assert "http://127.0.0.1:8095/api/tts?text=readycheck&lang=ca" in probe.urls
    assert "http://192.0.2.10:8000/api/daemon/status" in probe.urls
    assert "http://192.0.2.10:8000/api/motors/status" in probe.urls
    assert "http://localhost:4000/" in probe.urls
    assert probe.argv == [["v4l2-ctl", "-d", "/dev/video40", "--list-formats-ext"]]
    assert evidence.gesture_mode == "push_turn"
    assert evidence.gesture_invert == "1"
    assert evidence.reachy_base_url == "http://192.0.2.10:8000"
    assert evidence.healthz.reached is False  # degraded, not raised
    # An unconfigured robot is not probed for the ROS tee at ALL: exactly one v4l2-ctl
    # call, for the kiosk device. (The single-element argv assertion above is the same
    # promise; this is the field it lands in.)
    assert evidence.gaze_video == ""
    assert evidence.camera_ros == CommandResult()


def test_collect_probes_the_ros_tee_dot_env_names():
    """EMORI_READYCHECK_ROS_VIDEO_DEVICE still names the device outright, beside the
    kiosk device's own override."""

    probe = _FakeProbe({"EMORI_READYCHECK_ROS_VIDEO_DEVICE": "/dev/video41"})

    evidence = collect(probe)

    assert evidence.gaze_video == "/dev/video41"
    assert ["v4l2-ctl", "-d", "/dev/video41", "--list-formats-ext"] in probe.argv
    assert evidence.camera_ros.available is True


def _camera_defaults(probe, text: str) -> list[str]:
    """``probe`` answers /etc/default/emori-camera with ``text``; returns the paths read."""

    asked: list[str] = []

    def read_text(path: str) -> str:
        asked.append(path)
        return text if path == readycheck._CAMERA_DEFAULTS else ""

    probe.read_text = read_text
    return asked


def test_collect_finds_the_tee_the_camera_bridges_own_file_arms():
    """A fresh v2 robot arms the tee in ONE place, /etc/default/emori-camera (the documented
    v2 route). Reading the device number from there is what lets its gaze gate check the
    tee with no .env line -- the line a fresh robot never had (gaze_lane_off on a robot
    whose tee was running)."""

    probe = _FakeProbe({})
    asked = _camera_defaults(probe, "EMORI_CAM_FPS=30\nEMORI_CAM_ROS_VIDEO_NR=41\n")

    evidence = collect(probe)

    assert asked == [readycheck._CAMERA_DEFAULTS] == ["/etc/default/emori-camera"]
    assert evidence.gaze_video == "/dev/video41"
    assert ["v4l2-ctl", "-d", "/dev/video41", "--list-formats-ext"] in probe.argv
    assert _by_name(evaluate(evidence))["gaze"].code == "ros_camera_ready"


def test_an_explicit_ros_video_device_outranks_the_camera_file():
    probe = _FakeProbe({"EMORI_READYCHECK_ROS_VIDEO_DEVICE": "/dev/video42"})
    _camera_defaults(probe, "EMORI_CAM_ROS_VIDEO_NR=41\n")

    assert collect(probe).gaze_video == "/dev/video42"


@pytest.mark.parametrize("text, device", [
    ("", ""),
    ("EMORI_CAM_FPS=30\n", ""),
    ("#EMORI_CAM_ROS_VIDEO_NR=41\n", ""),
    ("EMORI_CAM_ROS_VIDEO_NR=41\r\n", "/dev/video41"),
    ('EMORI_CAM_ROS_VIDEO_NR="41"\n', "/dev/video41"),
    ("  EMORI_CAM_ROS_VIDEO_NR = 41 \n", "/dev/video41"),
    # The documented rollback (empty it) appended after the arming line: last one wins,
    # exactly as in the bridge's EnvironmentFile.
    ("EMORI_CAM_ROS_VIDEO_NR=41\nEMORI_CAM_ROS_VIDEO_NR=\n", ""),
    ("EMORI_CAM_ROS_VIDEO_NR=\nEMORI_CAM_ROS_VIDEO_NR=43\n", "/dev/video43"),
    ("EMORI_CAM_ROS_VIDEO_NR=../../dev/sda\n", ""),
    ("EMORI_CAM_ROS_VIDEO_NR_OLD=41\n", ""),
])
def test_the_camera_file_is_read_as_systemd_reads_it(text, device):
    assert readycheck.camera_tee_device(text) == device


def test_an_unreadable_camera_file_leaves_the_lane_off():
    probe = _FakeProbe({})
    probe.read_text = lambda path: (_ for _ in ()).throw(OSError("denied"))

    evidence = collect(probe)

    assert evidence.gaze_video == ""
    assert _by_name(evaluate(evidence))["gaze"].code == "gaze_lane_off"


def test_the_system_probe_reads_a_small_file_and_refuses_a_big_one(tmp_path):
    small = tmp_path / "emori-camera"
    small.write_bytes(b"EMORI_CAM_ROS_VIDEO_NR=41\n")
    big = tmp_path / "big"
    big.write_bytes(b"#" * (readycheck._CAMERA_DEFAULTS_MAX_BYTES + 1))

    probe = SystemProbe()

    assert probe.read_text(str(small)) == "EMORI_CAM_ROS_VIDEO_NR=41\n"
    assert probe.read_text(str(big)) == ""
    assert probe.read_text(str(tmp_path / "absent")) == ""


def test_collect_defaults_mirror_the_real_readers():
    probe = _FakeProbe({})
    evidence = collect(probe)

    assert evidence.reachy_base_url == "http://127.0.0.1:8000"  # server.py:94
    # server.py POKAMIND_BASE: the shipped API origin (the SSO login lives there).
    assert evidence.pokamind_base == "https://api.pokamind.com"
    # server.py COURSE_ID: the effective course, else identity.DEFAULT_COURSE_ID.
    assert evidence.course_id_env == "emori"
    assert evidence.gesture_enabled is True  # server.py:402-403
    assert evidence.gesture_mode == "back_drive"  # driver.py:999
    # The admin service's documented default port.
    assert "http://127.0.0.1:8070/healthz" in probe.urls


@pytest.mark.parametrize("raw, expected", [
    ("9000", "9000"),
    ("", "8070"),
    ("  8070 ", "8070"),
    # Not a port: probing "http://127.0.0.1:eighty-seventy/healthz" would report a
    # dead service rather than the configuration mistake, so fall back and let the
    # gate speak about the service itself.
    ("eighty-seventy", "8070"),
    ("8070/healthz", "8070"),
])
def test_collect_probes_the_admin_port_on_loopback(raw, expected):
    """Loopback, never EMORI_ADMIN_HOST: that is 0.0.0.0 so the phone can reach the
    service over the AP, and 0.0.0.0 is not an address anything can connect to."""

    probe = _FakeProbe({"EMORI_ADMIN_PORT": raw})

    collect(probe)

    assert f"http://127.0.0.1:{expected}/healthz" in probe.urls


# --- collecting the optional ROS 2 lane ----------------------------------------------

_INSPECT = ("docker", "inspect", "-f", "{{.State.Running}}", "emori-ros2")
_FIWARE_ENV = ("docker", "exec", "emori-ros2", "printenv", "FIWARE_BRIDGE_ENABLED")
_OWNER_ENV = ("docker", "exec", "emori-ros2", "printenv", "EMORI_ROS_MOTION_OWNER")
_NODE_LIST_CMD = ('for setup in /opt/ros/*/setup.bash; do source "$setup"; done '
                  "&& source /emori/ros2_ws/install/setup.bash "
                  "&& ros2 node list --no-daemon --spin-time 5")
_NODE_PROBE = ("docker", "exec", "emori-ros2", "bash", "-lc", _NODE_LIST_CMD)
_TRANSPORT_ENV = ("docker", "exec", "emori-ros2", "printenv", "EMORI_SKILL_TRANSPORT")
# The same four questions, asked of the v2 container (docs/ros2-v2-native-robot.md).
_V2_INSPECT = ("docker", "inspect", "-f", "{{.State.Running}}", "emori-ros2-v2")
_V2_FIWARE_ENV = ("docker", "exec", "emori-ros2-v2", "printenv", "FIWARE_BRIDGE_ENABLED")
_V2_OWNER_ENV = ("docker", "exec", "emori-ros2-v2", "printenv", "EMORI_ROS_MOTION_OWNER")
_V2_TRANSPORT_ENV = ("docker", "exec", "emori-ros2-v2", "printenv", "EMORI_SKILL_TRANSPORT")
_V2_NODE_PROBE = ("docker", "exec", "emori-ros2-v2", "bash", "-lc", _NODE_LIST_CMD)
_V2_NODE_LIST = _NODE_LIST + "/reachy_mini\n"


def test_a_rig_that_never_armed_ros2_does_not_shell_out_to_docker_at_all():
    """The lane is opt-in, and so is its cost: no docker, no /api/skill probe."""

    probe = _FakeProbe({})

    evidence = collect(probe)

    assert probe.argv == [["v4l2-ctl", "-d", "/dev/video40", "--list-formats-ext"]]
    assert not any(url.endswith("/api/skill") for url in probe.urls)
    assert evidence.ros2_enabled is False
    assert evidence.ros_motion_owner == "hostapp"  # skill_api.py's shipped default


def _ros2_v2(**overrides) -> Evidence:
    """A robot running v2: the robot IS the graph, and :8000 is the node's shim.

    The same four questions as `_ros2`, asked of a different container, plus the two the
    flavour adds: `reachy_mini` in the graph, and a daemon port that answers
    `backend: reachy_mini_ros2` instead of Pollen's daemon.
    """

    base = dict(
        ros2_enabled=True,
        ros_motion_owner="ros",
        ros2_flavor="v2",
        ros2_container=CommandResult(True, 0, "true\n"),
        ros2_fiware=CommandResult(True, 0, "0\n"),
        ros2_owner=CommandResult(True, 0, "ros\n"),
        ros2_transport=CommandResult(True, 0, "ros_native\n"),
        ros2_nodes=CommandResult(True, 0, _V2_NODE_LIST),
        daemon=HttpResult(True, 200, {"state": "running", "no_media": True,
                                      "backend": "reachy_mini_ros2"}),
        # v2 does not POST to the intake at all, so a robot with no intake is still ready.
        skill_api=HttpResult(True, 404, {"detail": "Not Found"}),
    )
    base.update(overrides)
    return _healthy(**base)


def test_a_healthy_v2_rig_passes_without_the_hostapp_intake():
    """v2's facade calls the robot node's actions. Demanding /api/skill there would fail
    a robot for missing a route nothing in its path uses."""

    gate = _by_name(evaluate(_ros2_v2()))["ros2"]
    assert (gate.status, gate.code) == (PASS, "ros2_up_v2")


def test_v2_without_the_robot_node_is_a_graph_with_nothing_to_move():
    """The four EMORI nodes keep deciding and publishing exactly as if there were a
    robot. Under v1 `reachy_mini` is not expected; under v2 its absence is the robot."""

    gate = _by_name(evaluate(_ros2_v2(
        ros2_nodes=CommandResult(True, 0, _NODE_LIST))))["ros2"]
    assert (gate.status, gate.code) == (FAIL, "ros2_graph_incomplete")
    assert "reachy_mini" in gate.hint


def test_v2_running_while_pollens_daemon_answers_the_port_is_a_fail():
    """The shim can lose :8000 to the TIME_WAIT sockets of the daemon it is replacing:
    the node comes up owning the serial bus, and the hostapp goes on talking to a
    daemon. Pollen has no `backend` field; that is the whole
    tell."""

    gate = _by_name(evaluate(_ros2_v2(
        daemon=HttpResult(True, 200, {"state": "running", "no_media": True}))))["ros2"]
    assert (gate.status, gate.code) == (FAIL, "shim_not_serving_the_daemon_port")


def test_both_graphs_running_is_refused_before_anything_else_is_judged():
    """Two engagement lanes, two publishers per topic, and two processes reaching for one
    serial bus. Neither container can see this from the inside."""

    gate = _by_name(evaluate(_ros2_v2(ros2_both_running=True)))["ros2"]
    assert (gate.status, gate.code) == (FAIL, "two_ros_graphs_running")


def test_an_http_transport_still_has_to_have_somewhere_to_post():
    """The ros_native exemption is exactly that: a transport that skips the intake. A v2
    container pointed back at the hostapp is judged like v1."""

    gate = _by_name(evaluate(_ros2_v2(
        ros2_transport=CommandResult(True, 0, "hostapp_http\n"),
        skill_api=HttpResult(True, 404, {"detail": "Not Found"}))))["ros2"]
    assert (gate.status, gate.code) == (FAIL, "skill_api_not_mounted")


def test_collect_asks_the_v2_container_when_that_is_the_one_running():
    """A v2 robot checked as v1 reports a container that does not exist, and a perfectly
    healthy robot fails its own readiness gate."""

    probe = _FakeProbe(
        {"EMORI_WITH_ROS2": "1", "EMORI_ROS_MOTION_OWNER": "ros", "HOSTAPP_PORT": "8090"},
        commands={
            _INSPECT: CommandResult(True, 0, "false\n"),
            _V2_INSPECT: CommandResult(True, 0, "true\n"),
            _V2_FIWARE_ENV: CommandResult(True, 0, "0\n"),
            _V2_OWNER_ENV: CommandResult(True, 0, "ros\n"),
            _V2_TRANSPORT_ENV: CommandResult(True, 0, "ros_native\n"),
            _V2_NODE_PROBE: CommandResult(True, 0, _V2_NODE_LIST),
        },
    )

    evidence = collect(probe)

    assert evidence.ros2_flavor == "v2"
    assert evidence.ros2_both_running is False
    for argv in (_V2_INSPECT, _V2_FIWARE_ENV, _V2_OWNER_ENV, _V2_NODE_PROBE):
        assert list(argv) in probe.argv, argv
    # ...and never the v1 container, which is not the one running.
    assert list(_FIWARE_ENV) not in probe.argv
    assert evidence.ros2_transport.stdout.strip() == "ros_native"


def test_a_v2_rigs_hints_name_the_v2_container():
    """A person told to run `docker logs emori-ros2` on a v2 robot runs a command that
    answers "No such container" while their graph is right there in front of them."""

    gate = _by_name(evaluate(_ros2_v2(
        ros2_nodes=CommandResult(True, 1, ""))))["ros2"]
    assert gate.code == "ros2_graph_unlistable"
    assert "emori-ros2-v2" in gate.hint


@pytest.mark.parametrize("overrides, code", [
    ({"ros_motion_owner": "hostapp"}, "ros2_owner_drift"),          # the container says ros
    ({"ros2_fiware": CommandResult(True, 0, "1\n")}, "ros2_fiware_bridge_on"),
    ({"ros2_nodes": CommandResult(True, 0, "/reachy_mini\n")}, "ros2_graph_incomplete"),
])
def test_a_v2_rig_is_never_told_to_restart_the_v1_unit(overrides, code):
    """On a fenced robot install.sh disables emori-ros2: `systemctl restart emori-ros2`
    would START the v1 graph beside the v2 one. The v2 container is compose's."""

    gate = _by_name(evaluate(_ros2_v2(**overrides)))["ros2"]

    assert gate.code == code
    assert "restart emori-ros2" not in gate.hint
    assert "docker logs emori-ros2 " not in gate.hint + " "
    if code != "ros2_graph_incomplete":
        assert "docker-compose.v2.robot.yml" in gate.hint


def test_an_armed_lane_asks_the_container_about_itself_and_probes_the_intake():
    """.env is interpolated by compose ONCE, at `up`, so the running container - not
    the file this process just read - is the authority on what it is doing."""

    probe = _FakeProbe(
        {"EMORI_WITH_ROS2": "1", "EMORI_ROS_MOTION_OWNER": "ros", "HOSTAPP_PORT": "8090"},
        commands={
            _INSPECT: CommandResult(True, 0, "true\n"),
            _FIWARE_ENV: CommandResult(True, 0, "0\n"),
            _OWNER_ENV: CommandResult(True, 0, "ros\n"),
            _NODE_PROBE: CommandResult(True, 0, _NODE_LIST),
        },
    )

    evidence = collect(probe)

    assert list(_INSPECT) in probe.argv
    assert list(_FIWARE_ENV) in probe.argv
    assert list(_OWNER_ENV) in probe.argv
    # ...and the graph itself, which none of the three above can see.
    assert list(_NODE_PROBE) in probe.argv
    # A GET on the POST-only route: it can never submit motion, and 405-vs-404 is the
    # whole answer to "does the graph have somewhere to POST".
    assert "http://127.0.0.1:8090/api/skill" in probe.urls
    assert evidence.ros2_enabled is True
    assert evidence.ros_motion_owner == "ros"
    assert evidence.ros2_fiware.stdout.strip() == "0"
    assert _by_name(evaluate(evidence))["ros2"].code == "hostapp_unreachable"  # no hostapp


def test_a_stopped_container_is_not_asked_questions_it_cannot_answer():
    """Each `docker exec` against a dead container is another probe timeout on a robot
    that already has its answer."""

    probe = _FakeProbe(
        {"EMORI_WITH_ROS2": "1"},
        commands={_INSPECT: CommandResult(True, 0, "false\n")},
    )

    evidence = collect(probe)

    assert list(_INSPECT) in probe.argv
    assert list(_FIWARE_ENV) not in probe.argv
    assert list(_OWNER_ENV) not in probe.argv
    assert list(_NODE_PROBE) not in probe.argv
    assert _by_name(evaluate(evidence))["ros2"].code == "ros2_container_stopped"


def _fenced(probe):
    """``probe`` on a robot committed to v2: the daemon fence is up."""

    asked: list[str] = []

    def path_exists(path: str) -> bool:
        asked.append(path)
        return path == readycheck._V2_FENCE_FLAG

    probe.path_exists = path_exists
    return asked


_V2_UP = {
    _INSPECT: CommandResult(True, 1, "Error: No such object: emori-ros2\n"),
    _V2_INSPECT: CommandResult(True, 0, "true\n"),
    _V2_FIWARE_ENV: CommandResult(True, 0, "0\n"),
    _V2_OWNER_ENV: CommandResult(True, 0, "hostapp\n"),
    _V2_TRANSPORT_ENV: CommandResult(True, 0, "ros_native\n"),
    _V2_NODE_PROBE: CommandResult(True, 0, _V2_NODE_LIST),
}


@pytest.mark.parametrize("with_ros2", [None, "", "0"])
def test_the_v2_fence_arms_the_ros2_gate_with_no_env_line(with_ros2):
    """A fresh v2 robot (deployed with only its token) has no EMORI_WITH_ROS2 line, and
    its ros2 gate said `ros2_lane_off` while the robot ran inside the v2 container. The
    fence is the v2 opt-in: Pollen's daemon cannot start, so that container is the only
    thing that moves the robot -- which no EMORI_WITH_ROS2=0 can make untrue."""

    environ = {"HOSTAPP_PORT": "8090"}
    if with_ros2 is not None:
        environ["EMORI_WITH_ROS2"] = with_ros2
    probe = _FakeProbe(environ, commands=dict(_V2_UP), answers={
        "http://127.0.0.1:8000/api/daemon/status": HttpResult(
            True, 200, {"state": "running", "no_media": True,
                        "backend": "reachy_mini_ros2"})})
    asked = _fenced(probe)

    evidence = collect(probe)

    assert readycheck._V2_FENCE_FLAG in asked
    assert evidence.v2_fence is True and evidence.ros2_enabled is True
    assert evidence.ros2_flavor == "v2"
    for argv in (_V2_INSPECT, _V2_FIWARE_ENV, _V2_OWNER_ENV, _V2_NODE_PROBE):
        assert list(argv) in probe.argv, argv
    assert _by_name(evaluate(evidence))["ros2"] == GateResult("ros2", PASS, "ros2_up_v2")


_V2_RUNNING = ("docker", "inspect", "-f", "{{.State.Running}}", readycheck._ROS2_V2_CONTAINER)


def test_a_motor_container_running_with_the_fence_down_is_a_failure():
    """Going to v1 turns the lane off, so nothing else looks: the container and Pollen's daemon
    can then both open the motor bus."""
    probe = _FakeProbe({}, commands={_V2_RUNNING: CommandResult(True, 0, "true\n")})
    probe.path_exists = lambda path: path == readycheck._V2_FENCE_DROPIN

    gate = _by_name(evaluate(collect(probe)))["ros2"]

    assert (gate.status, gate.code) == (FAIL, "motor_container_without_fence")
    assert "apply v1" in gate.hint


def test_a_robot_that_left_v2_with_the_container_gone_is_fine():
    probe = _FakeProbe({}, commands={_V2_RUNNING: CommandResult(True, 1, "No such object\n")})
    probe.path_exists = lambda path: path == readycheck._V2_FENCE_DROPIN

    assert _by_name(evaluate(collect(probe)))["ros2"].code == "ros2_lane_off"


def test_without_the_fence_an_unset_switch_still_leaves_the_lane_off():
    probe = _FakeProbe({})
    probe.path_exists = lambda path: False

    evidence = collect(probe)

    assert evidence.ros2_enabled is False
    assert not any(argv[0] == "docker" for argv in probe.argv)
    assert _by_name(evaluate(evidence))["ros2"].code == "ros2_lane_off"


@pytest.mark.parametrize("v2, code, named", [
    (CommandResult(True, 1, ""), "ros2_container_absent", "docker-compose.v2.robot.yml"),
    (CommandResult(True, 0, "false\n"), "ros2_container_stopped", "docker start emori-ros2-v2"),
])
def test_a_fenced_robot_without_its_container_is_told_about_v2_not_v1(v2, code, named):
    """With the fence up and no container running, the robot cannot move at all. The
    hint is about the v2 container, never the v1 Humble tarball this robot never had."""

    probe = _FakeProbe({}, commands={_INSPECT: CommandResult(True, 1, ""), _V2_INSPECT: v2})
    _fenced(probe)

    evidence = collect(probe)
    gate = _by_name(evaluate(evidence))["ros2"]

    assert evidence.ros2_flavor == "v2"
    assert (gate.status, gate.code) == (FAIL, code)
    assert named in gate.hint and "humble" not in gate.hint


_DOCKER_VERSION = ("docker", "version", "--format", "{{.Server.Version}}")


@pytest.mark.parametrize("fenced", [True, False])
def test_a_docker_that_does_not_answer_is_never_an_absent_container(fenced):
    """`docker inspect` exits 1 with an empty stdout for a missing container AND for a
    socket that refuses this user. Only docker answering makes the first one true: the
    other robot may well be running its container behind that socket."""

    probe = _FakeProbe({} if fenced else {"EMORI_WITH_ROS2": "1"}, commands={
        _INSPECT: CommandResult(True, 1, ""), _V2_INSPECT: CommandResult(True, 1, ""),
        _DOCKER_VERSION: CommandResult(True, 1, "")})
    if fenced:
        _fenced(probe)

    evidence = collect(probe)
    gate = _by_name(evaluate(evidence))["ros2"]

    assert list(_DOCKER_VERSION) in probe.argv
    assert (gate.status, gate.code) == (SKIP, "docker_unreachable")
    assert "docker group" in gate.hint and "NOTHING" not in gate.hint


def test_docker_is_asked_only_when_neither_inspect_could_tell():
    for v1, v2 in ((CommandResult(True, 0, "false\n"), CommandResult(True, 1, "")),
                   (CommandResult(True, 1, ""), CommandResult(True, 0, "true\n"))):
        probe = _FakeProbe({"EMORI_WITH_ROS2": "1"}, commands={_INSPECT: v1, _V2_INSPECT: v2})

        collect(probe)

        assert list(_DOCKER_VERSION) not in probe.argv


def _fenced_native(probe, *, native: bool = True):
    """``probe`` on a fenced robot that runs the v2 graph natively (Route B)."""

    present = {readycheck._V2_FENCE_FLAG} | ({readycheck._V2_NATIVE_UNIT} if native else set())
    probe.path_exists = lambda path: path in present


_SHIM = HttpResult(True, 200, {"state": "running", "backend": "reachy_mini_ros2"})


@pytest.mark.parametrize("docker", [CommandResult(False), CommandResult(True, 1, "")])
@pytest.mark.parametrize("daemon, status, code", [
    (_SHIM, PASS, "ros2_up_native"),
    (HttpResult(True, 200, {"state": "running"}), FAIL, "shim_not_serving_the_daemon_port"),
    (HttpResult(False), SKIP, "daemon_port_unreachable"),
])
def test_the_native_route_is_judged_by_its_shim_not_by_docker(docker, daemon, status, code):
    """Route B has no container. Before the fence armed this gate it read ros2_lane_off;
    a SKIP docker_unavailable would make --strict exit 2 on that healthy robot."""

    probe = _FakeProbe({}, commands={_INSPECT: docker, _V2_INSPECT: docker},
                       answers={"http://127.0.0.1:8000/api/daemon/status": daemon})
    _fenced_native(probe)

    evidence = collect(probe)
    gate = _by_name(evaluate(evidence))["ros2"]

    assert evidence.ros2_native is True
    assert (gate.status, gate.code) == (status, code)
    if code == "shim_not_serving_the_daemon_port":
        assert "reachy-mini-ros2" in gate.hint


def test_a_running_v2_container_is_checked_even_beside_a_native_unit():
    probe = _FakeProbe({}, commands=dict(_V2_UP), answers={
        "http://127.0.0.1:8000/api/daemon/status": _SHIM})
    _fenced_native(probe)

    assert _by_name(evaluate(collect(probe)))["ros2"].code == "ros2_up_v2"


def test_the_native_unit_means_nothing_without_the_fence():
    probe = _FakeProbe({})
    probe.path_exists = lambda path: path == readycheck._V2_NATIVE_UNIT

    evidence = collect(probe)

    assert evidence.ros2_native is False and evidence.ros2_enabled is False
    assert _by_name(evaluate(evidence))["ros2"].code == "ros2_lane_off"


def test_a_fenced_robot_without_docker_or_a_native_unit_still_skips():
    probe = _FakeProbe({}, commands={_INSPECT: CommandResult(False),
                                     _V2_INSPECT: CommandResult(False)})
    _fenced_native(probe, native=False)

    gate = _by_name(evaluate(collect(probe)))["ros2"]

    assert (gate.status, gate.code) == (SKIP, "docker_unavailable")


@pytest.mark.parametrize("native", [False, True])
def test_a_v1_graph_on_a_fenced_robot_is_a_failure_not_a_pass(native):
    """A v1 graph left enabled from the robot's v1 days comes back at a boot while the v2
    container is down. It asks Pollen's daemon to move the robot, and the fence keeps
    that daemon off: `ros2_up` for it would be a PASS for a robot nothing can move."""

    probe = _FakeProbe({}, commands={
        _INSPECT: CommandResult(True, 0, "true\n"), _V2_INSPECT: CommandResult(True, 0, "false\n"),
        _FIWARE_ENV: CommandResult(True, 0, "0\n"), _OWNER_ENV: CommandResult(True, 0, "hostapp\n"),
        _NODE_PROBE: CommandResult(True, 0, _NODE_LIST)})
    _fenced_native(probe, native=native)

    evidence = collect(probe)
    gate = _by_name(evaluate(evidence))["ros2"]

    assert evidence.ros2_flavor == "v1"
    assert (gate.status, gate.code) == (FAIL, "v1_graph_on_v2_robot")
    assert "systemctl disable --now emori-ros2" in gate.hint
    # ...and the v1 container is asked nothing more: its answers change nothing.
    for argv in (_FIWARE_ENV, _OWNER_ENV, _NODE_PROBE):
        assert list(argv) not in probe.argv, argv


def test_without_the_fence_a_running_v1_graph_still_passes():
    probe = _FakeProbe({"EMORI_WITH_ROS2": "1"}, commands={
        _INSPECT: CommandResult(True, 0, "true\n"), _V2_INSPECT: CommandResult(True, 1, ""),
        _FIWARE_ENV: CommandResult(True, 0, "0\n"), _OWNER_ENV: CommandResult(True, 0, "hostapp\n"),
        _NODE_PROBE: CommandResult(True, 0, _NODE_LIST)})
    probe.path_exists = lambda path: False

    assert _by_name(evaluate(collect(probe)))["ros2"] == GateResult("ros2", PASS, "ros2_up")


def test_readycheck_mirrors_the_constants_it_does_not_import():
    from emori_adapter.hostapp import identity, menu_builder, readycheck, server

    assert readycheck._DEFAULT_COURSE_ID == identity.DEFAULT_COURSE_ID
    assert readycheck._MENU_GENERATED_FROM == menu_builder.GENERATED_FROM
    assert readycheck._MENU_DRAFT_AUTHORED == {menu_builder.AUTHORED_DRAFT,
                                               menu_builder.AUTHORED_MIXED}
    assert readycheck._MENU_TIERS == set(server.MENU_SOURCES)


def test_menu_authored_believes_only_a_built_menu(tmp_path):
    from emori_adapter.hostapp import menu_builder
    from emori_adapter.hostapp.readycheck import menu_authored

    built = tmp_path / "built.yaml"
    built.write_text(menu_builder.render_menu({
        "prompt": "Say one.", "options": {"1": {"module_id": "AAAA", "name": "Topic",
                                                "keywords": ["topic"]}},
        "default": "1", "generated_from": menu_builder.GENERATED_FROM,
        "authored": menu_builder.AUTHORED_DRAFT}, "en"), encoding="utf-8")
    forged = tmp_path / "forged.yaml"
    forged.write_text("prompt: x\noptions: {}\nauthored: title_draft\n", encoding="utf-8")
    broken = tmp_path / "broken.yaml"
    broken.write_text("prompt: [unclosed\n", encoding="utf-8")

    assert menu_authored(str(built)) == "title_draft"
    assert menu_authored(str(forged)) == ""  # no generated_from mark: not a built menu
    assert menu_authored(str(broken)) == ""
    assert menu_authored(str(tmp_path / "absent.yaml")) == ""
    assert menu_authored("") == ""


def test_the_menu_probe_reports_the_tier_and_who_named_a_built_menu(monkeypatch, tmp_path):
    from emori_adapter import site_settings
    from emori_adapter.hostapp import menu_builder, server

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root=None: Path(root) if root is not None else tmp_path)
    for name in ("EMORI_VOICE_TOPICS", "EMORI_VOICE_TOPICS_EN", "EMORI_SITE_AUTO"):
        monkeypatch.delenv(name, raising=False)
    token = robot_token.parse_token(GOLDEN_TOKEN)
    monkeypatch.setattr(server, "ROBOT_TOKEN", token)
    whoami_body = {
        "robot_id": "test-robot-01", "display_name": "Example Site", "status": "active",
        "credential": {"kind": "fleet", "key_fingerprint": token.fingerprint,
                       "key_version": 1},
        "org": "example-org", "partner": "emori-robot", "course_id": "", "languages": ["en"],
        "pathways": [], "identity_domains": [], "modules": [],
        "features": {"feature_a": False, "feature_b": False, "feature_c": True}}
    site_settings.write_scope_snapshot(site_settings.merge_scope_snapshot(
        None, whoami_body, contract=1, key_fingerprint=token.fingerprint, key_version=1,
        now=time.time()), root=tmp_path)
    auto = menu_builder.auto_menu_path("en")
    auto.parent.mkdir(parents=True, exist_ok=True)
    auto.write_text(menu_builder.render_menu({
        "prompt": "Say one.", "options": {"1": {"module_id": "BuiltFromTheRecord01",
                                                "name": "Topic", "keywords": ["topic"]}},
        "default": "1", "generated_from": menu_builder.GENERATED_FROM,
        "authored": menu_builder.AUTHORED_MIXED}, "en"), encoding="utf-8")

    built = SystemProbe().voice_menu("en")

    assert (built.tier, built.authored, built.options) == (
        "auto", "mixed", (("1", "BuiltFromTheRecord01"),))
    assert Path(built.source) == auto

    saved = Path(server.site_menu_path("en"))
    saved.write_text("prompt: say one\noptions:\n  '1':\n    module_id: SiteSaved000000001\n"
                     "    name: Topic one\n", encoding="utf-8")
    site = SystemProbe().voice_menu("en")
    assert (site.tier, site.authored) == ("site", "")

    # Derivation off: the built menu is not served, and nothing else is on disk here.
    monkeypatch.setenv("EMORI_SITE_AUTO", "0")
    saved.unlink()
    assert SystemProbe().voice_menu("en").tier == "builtin"


def test_system_probe_degrades_instead_of_raising(tmp_path):
    probe = SystemProbe(timeout_s=0.2)

    assert probe.http("not-a-url").reached is False
    assert probe.http("http://127.0.0.1:9/healthz").reached is False  # refused, bounded by timeout
    assert probe.sso("http://127.0.0.1:9/partner/sso/login").reached is False
    assert probe.command(["emori-definitely-not-a-real-binary"]).available is False


def test_every_network_probe_is_bounded_by_a_tight_timeout():
    # A readycheck that hangs is the failure it exists to prevent: a boot-time hang is
    # indistinguishable, to the person in front of the robot, from a frozen session.
    assert SystemProbe()._timeout_s <= 5.0


# --- the session chain, collected ----------------------------------------------------


_CHAIN_ENV = {
    "HOSTAPP_PORT": "8090",
    "POKAMIND_BASE": "http://localhost:4000",
    "EMORI_LANG": "ca",
    "EMORI_PARTNER_NAME": "emori-robot",
    "EMORI_COURSE_ID": "example-course",
}


def _golden_load() -> robot_token.TokenLoad:
    return robot_token.TokenLoad(token=robot_token.parse_token(GOLDEN_TOKEN), source="file",
                                 path="/opt/emori/secrets/pokamind_robot_token")


def test_collect_follows_the_redirect_to_the_frontend_origin_and_never_past_it():
    probe = _FakeProbe(_CHAIN_ENV, sso=SsoResult(True, 302, _CALLBACK),
                       token_load=_golden_load())

    collect(probe)

    assert f"{_FRONTEND_ORIGIN}/" in probe.urls
    # The callback URL itself is NOT fetched: it burns a one-shot sign-in token that the
    # kiosk browser needs.
    assert not any(url.startswith(f"{_FRONTEND_ORIGIN}/auth/") for url in probe.urls)


def test_collect_skips_the_chain_entirely_without_a_token():
    # Without a token there is nothing to sign with: no login is minted, no whoami is
    # asked, and the chain gates SKIP while pokamind_token FAILs.
    probe = _FakeProbe(_CHAIN_ENV, sso=SsoResult(True, 302, _CALLBACK))

    evidence = collect(probe)

    assert probe.sso_urls == []
    assert evidence.token.state == "none" and evidence.whoami is None
    assert (evidence.sso.reached, evidence.frontend.reached) == (False, False)
    assert evidence.start_demo_branch is True
    # The menu is still read: provenance needs nothing but the filesystem.
    assert probe.menu_langs == ["ca"]


def test_collect_never_reads_a_retired_credential_or_a_module_backend():
    """The shared partner secret and the module-backend variables of older releases
    are not read at all: setting them changes nothing a gate sees."""

    retired = {"EMORI_PARTNER_SECRET": "x" * 40, "EMORI_ROBOT_SECRET": "y" * 64}
    plain = collect(_FakeProbe(_CHAIN_ENV, sso=SsoResult(True, 302, _CALLBACK)))
    with_old = _FakeProbe({**_CHAIN_ENV, **retired}, sso=SsoResult(True, 302, _CALLBACK))

    assert collect(with_old) == plain
    assert with_old.sso_urls == [] and with_old.bearers == [""] * len(with_old.bearers)


# --- file digests ---------------------------------------------------------------------


def test_file_digest_is_content_addressed_and_survives_an_absent_file(tmp_path):
    menu = tmp_path / "voice_topics.yaml"
    menu.write_text("prompt: hi\n", encoding="utf-8")
    first = file_digest(str(menu))
    assert first.startswith("sha256:")
    # A deploy rewrites every mtime; only the bytes may decide.
    menu.write_text("prompt: hi\n", encoding="utf-8")
    assert file_digest(str(menu)) == first
    menu.write_text("prompt: bye\n", encoding="utf-8")
    assert file_digest(str(menu)) != first
    assert file_digest(str(tmp_path / "nope.yaml")) == ""
    assert file_digest("") == ""


def _dash_signals():
    """One cycle's worth of DerivedSignals, as the live driver would record it."""

    from emori_adapter.models import ContextSignal, DerivedSignals, EngagementLevel

    return DerivedSignals(
        timestamp="2026-08-11T10:00:00Z", session_id="s", person_id="p", seq=1,
        engagement_score=0.7, engagement_level=EngagementLevel.ENGAGED, valence=0.2,
        arousal=0.1, expression="neutral", expression_confidence=0.4,
        top_styles=[("mirroring", 0.3)],
        context=ContextSignal(engagement_trend=0.0, escalation=0.1, rapport=0.6,
                              attention=0.7, dominant_style="mirroring"),
    )


def test_collect_resolves_the_demo_branch_exactly_as_api_start_does():
    """server.demo_default: always demo without a token, else HOSTAPP_DEMO=1 forces it."""

    # No token -> the demo url, whatever HOSTAPP_DEMO (or a retired secret) says.
    assert collect(_FakeProbe({})).start_demo_branch is True
    assert collect(_FakeProbe({"HOSTAPP_DEMO": "0"})).start_demo_branch is True
    assert collect(_FakeProbe({"EMORI_PARTNER_SECRET": "s3cret"})).start_demo_branch is True
    # A token, and the default (unset) HOSTAPP_DEMO -> live.
    assert collect(_FakeProbe({}, token_load=_golden_load())).start_demo_branch is False
    # A token, but demo pinned on -> still the demo url.
    assert collect(_FakeProbe({"HOSTAPP_DEMO": "1"},
                              token_load=_golden_load())).start_demo_branch is True


def test_collect_probes_the_same_dashboard_db_the_store_would_open():
    from emori_adapter.hostapp.metrics_store import DEFAULT_DB_PATH

    default = _FakeProbe({})
    collect(default)
    assert default.dash_db_paths == [DEFAULT_DB_PATH]

    pinned = _FakeProbe({"EMORI_DASH_DB": "/opt/emori/artifacts/dashboard.db"})
    collect(pinned)
    assert pinned.dash_db_paths == ["/opt/emori/artifacts/dashboard.db"]


def test_the_dashboard_db_probe_reads_a_real_store_without_writing_to_it(tmp_path,
                                                                        monkeypatch):
    """Against the REAL MetricsStore file, opened read-only."""

    import os as _os

    from emori_adapter.hostapp.metrics_store import MetricsStore

    monkeypatch.setenv("EMORI_HEARTBEAT_S", "60")
    path = tmp_path / "dash.db"
    store = MetricsStore(str(path), salt="probe-salt")
    now = time.time()
    store.session_started("s-scored", person_id="anna-001", started_at=now - 600)
    store.record_cycle("s-scored", _dash_signals(), ts=now - 590)
    store.session_finished("s-scored", ended_at=now - 300, status="completed",
                           results={"performance_scores": {"adaptability": 55},
                                    "session_analysis": "Steady."})
    store.heartbeat(ok=True, ts=now - 10)
    store.close()
    before = _os.path.getmtime(path)

    data = SystemProbe().dashboard_data(str(path))

    assert (data.ok, data.reason) == (True, "")
    assert data.path == _os.path.abspath(str(path))
    assert data.finished_sessions == 1
    assert (data.any_cycles_ever, data.last_completed) == (True, True)
    assert data.last_finished_cycles == 1
    assert data.last_finished_scored is True
    assert data.last_finished_status == "completed"
    assert data.now - data.last_heartbeat_ts < 120
    assert data.heartbeat_interval_s == 60.0
    # Read-only, in the strongest sense available: the file is untouched.
    assert _os.path.getmtime(path) == before
    # Nothing identifying travels in the record.
    assert "anna" not in json.dumps(data.__dict__, default=str)


def test_an_unscored_session_and_a_cycleless_one_are_reported_as_such(tmp_path):
    from emori_adapter.hostapp.metrics_store import MetricsStore

    path = tmp_path / "dash.db"
    store = MetricsStore(str(path), salt="probe-salt")
    now = time.time()
    store.session_started("s-blind", person_id="anna-001", started_at=now - 600)
    store.session_finished("s-blind", ended_at=now - 300, status="completed",
                           results={"session_analysis": "no scores in this payload"})
    store.close()

    data = SystemProbe().dashboard_data(str(path))

    assert (data.finished_sessions, data.last_finished_cycles) == (1, 0)
    assert data.last_finished_scored is False
    assert data.last_heartbeat_ts == 0.0
    assert (data.any_cycles_ever, data.last_completed) == (False, True)


def test_the_dashboard_db_probe_never_creates_the_file_it_cannot_find(tmp_path):
    """A read-write open would CREATE an empty db and then honestly report zero
    sessions - the exact fiction this gate exists to catch."""

    absent = tmp_path / "nowhere" / "dash.db"

    data = SystemProbe().dashboard_data(str(absent))

    assert (data.ok, data.reason) == (False, "db_missing")
    assert not absent.exists()
    assert SystemProbe().dashboard_data("").reason == "db_path_unset"


def test_a_foreign_file_at_the_dashboard_path_is_not_an_empty_dashboard(tmp_path):
    import sqlite3

    path = tmp_path / "other.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE something_else (x INTEGER)")
    conn.commit()
    conn.close()

    assert SystemProbe().dashboard_data(str(path)).reason == "db_no_schema"

    (tmp_path / "junk.db").write_bytes(b"not a database at all")
    assert SystemProbe().dashboard_data(str(tmp_path / "junk.db")).ok is False


def test_collect_reads_the_menu_the_way_the_hostapp_does(monkeypatch, tmp_path):
    # Not a re-implementation of the candidate list: the probe must go through the
    # hostapp's own loader, so a menu the robot would speak can never differ from the
    # menu the readycheck validates.
    from emori_adapter.hostapp import server

    yaml_path = tmp_path / "voice_topics.ca.yaml"
    yaml_path.write_text(
        "prompt: digues un numero\n"
        "options:\n"
        "  '1':\n"
        "    module_id: SEEDEDCAMODULE00001\n"
        "    name: Atencio al taulell\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EMORI_VOICE_TOPICS", str(yaml_path))

    menu = SystemProbe().voice_menu("ca")

    assert (menu.loaded, menu.options) == (True, (("1", "SEEDEDCAMODULE00001"),))
    assert menu.options == tuple(
        (key, option.module_id) for key, option in server._load_menu("ca").options.items()
    )


def test_voice_menu_probe_degrades_honestly(monkeypatch):
    monkeypatch.setenv("EMORI_VOICE_TOPICS", "configs/definitely-not-here.yaml")
    # A configured-but-absent file falls through to the built-in default, exactly as the
    # hostapp does - the probe must report what the robot would REALLY speak.
    assert SystemProbe().voice_menu("en").loaded is True

    def _raise(lang):
        raise ValueError("bad yaml")

    monkeypatch.setattr("emori_adapter.hostapp.server._load_menu", _raise)
    broken = SystemProbe().voice_menu("en")
    assert (broken.loaded, broken.reason) == (False, "voice_menu_unreadable")


def test_sso_probe_does_not_follow_redirects_and_keeps_the_body_out_of_the_record():
    import httpx

    from emori_adapter.hostapp import readycheck

    seen: dict[str, object] = {}

    class _Response:
        status_code = 400
        headers = {"location": ""}

        def json(self):
            return {"error": "Invalid token: Signature verification failed for eyJhbGciOi"}

    def _fake_get(url, **kwargs):
        seen.update(kwargs)
        return _Response()

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(httpx, "get", _fake_get)
        result = readycheck.SystemProbe(timeout_s=3.0).sso("http://localhost:4000/x")
    finally:
        monkeypatch.undo()

    assert seen["follow_redirects"] is False  # the redirect IS the evidence
    assert seen["timeout"] == 3.0
    assert (result.reached, result.status, result.error_field) == (True, 400, "error")
    assert "Signature" not in repr(result)


# --- entry points -------------------------------------------------------------------


def test_the_spoken_line_is_synthesised_by_the_hostapp_and_played_on_the_robot_speaker(
    monkeypatch, tmp_path,
):
    import httpx

    from emori_adapter.hostapp import readycheck

    requested: list[str] = []
    played: list[list[str]] = []
    audio = b"RIFF....WAVEfmt "

    class _Wav:
        status_code = 200
        headers = {"content-type": "audio/wav"}
        content = audio

    def _fake_get(url, **kwargs):
        requested.append(url)
        assert kwargs["timeout"] <= 5.0
        return _Wav()

    def _fake_run(argv, **kwargs):
        played.append(list(argv))
        assert Path(argv[-1]).read_bytes() == audio  # the file must exist WHILE playing
        return type("C", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(readycheck.subprocess, "run", _fake_run)

    assert readycheck.SystemProbe().speak("http://127.0.0.1:8090/", "ca", "No puc parlar") is True

    assert requested == [
        "http://127.0.0.1:8090/api/tts?text=No%20puc%20parlar&lang=ca"
    ]
    assert played[0][0] == "pw-play"
    assert not Path(played[0][-1]).exists()  # and be cleaned up afterwards


@pytest.mark.parametrize("failure", ["tts_503", "not_audio", "empty", "no_pw_play"])
def test_speaking_is_best_effort_and_never_raises(monkeypatch, failure):
    import httpx

    from emori_adapter.hostapp import readycheck

    class _Response:
        status_code = 503 if failure == "tts_503" else 200
        headers = {"content-type": "application/json" if failure == "not_audio" else "audio/wav"}
        content = b"" if failure == "empty" else b"RIFF"

    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: _Response())

    def _no_binary(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(readycheck.subprocess, "run", _no_binary)

    assert readycheck.SystemProbe().speak("http://127.0.0.1:8090", "en", "x") is False


def test_a_dead_hostapp_cannot_make_the_announcement_raise(monkeypatch):
    from emori_adapter.hostapp import readycheck

    # Port 9 (discard) is refused immediately; the point is that a TTS endpoint that is
    # itself down degrades to silence instead of a traceback that would hide the matrix.
    assert readycheck.SystemProbe(timeout_s=0.2).speak("http://127.0.0.1:9", "en", "x") is False


def test_main_speaks_the_failure_unless_it_is_told_not_to(monkeypatch, capsys):
    from emori_adapter.hostapp import readycheck

    spoken: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        readycheck, "collect",
        lambda probe: _healthy(sso=SsoResult(False)),
    )
    monkeypatch.setattr(
        readycheck.SystemProbe, "speak",
        lambda self, hostapp_url, lang, text: spoken.append((hostapp_url, lang, text)) or True,
    )

    assert readycheck.main([]) == 1
    assert [call[2] for call in spoken] == [SPOKEN_LINES["ca"][0]]

    assert readycheck.main(["--json", "--no-speak"]) == 1
    assert len(spoken) == 1  # unchanged: --no-speak keeps a scripted run silent
    assert json.loads(capsys.readouterr().out.splitlines()[-1])


def test_main_prints_the_matrix_and_returns_the_exit_code(monkeypatch, capsys):
    from emori_adapter.hostapp import readycheck

    monkeypatch.setattr(readycheck.SystemProbe, "speak",
                        lambda self, hostapp_url, lang, text: False)
    monkeypatch.setattr(
        readycheck, "collect",
        lambda probe: _healthy(motors=HttpResult(True, 200, {"mode": "disabled"})),
    )
    assert readycheck.main([]) == 1
    out = capsys.readouterr().out
    assert "motors" in out and "FAIL" in out

    monkeypatch.setattr(readycheck, "collect", lambda probe: _healthy())
    assert readycheck.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["name"] for row in payload} >= {"hostapp", "daemon", "gesture"}
    assert all(row["status"] == PASS for row in payload)


def test_readycheck_writes_nothing_and_has_no_record_flag(monkeypatch, tmp_path):
    """Read-only: the one write is the session_chain login Pokamind records. There is
    no flag that makes this tool write a file."""
    from emori_adapter.hostapp import readycheck

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(readycheck, "collect", lambda probe: _healthy())
    readycheck.main(["--json", "--no-speak"])

    assert list(tmp_path.iterdir()) == []
    with pytest.raises(SystemExit):
        readycheck.main(["--json", "--no-speak", "--record-menu-proof"])


def test_console_script_and_shell_entry_points_exist_and_are_lf_only():
    root = Path(__file__).resolve().parents[1]

    assert 'emori-readycheck = "emori_adapter.hostapp.readycheck:main"' in \
        root.joinpath("pyproject.toml").read_text("utf-8")
    for entry in ("scripts/readycheck.ps1", "deploy/robot/readycheck.sh"):
        assert "emori_adapter.hostapp.readycheck" in root.joinpath(entry).read_text("utf-8")
    # A CRLF shebang makes systemd/bash fail 203/EXEC on the CM4 (install.sh:20-22).
    assert b"\r" not in root.joinpath("deploy/robot/readycheck.sh").read_bytes()


# --- the daemon gate's hint names what serves :8000 on THIS robot ---------------------
# On a v2 robot the hint must never tell the operator to POST /api/daemon/start (a
# route the robot node's shim does not have) or to restart the fenced daemon unit.
_SHIM_STATUS = {"state": "running", "no_media": True, "backend": "reachy_mini_ros2"}


def _daemon_gate(**overrides) -> GateResult:
    return {r.name: r for r in evaluate(_healthy(**overrides))}["daemon"]


@pytest.mark.parametrize("overrides, restart", [
    ({"daemon": HttpResult(False), "v2_fence": True}, "systemctl restart reachy-mini-ros2"),
    ({"daemon": HttpResult(False), "ros2_flavor": "v2"}, "docker restart emori-ros2-v2"),
    ({"daemon": HttpResult(True, 200, dict(_SHIM_STATUS, state="stopped"))},
     "systemctl restart reachy-mini-ros2"),
], ids=["fence", "v2-container", "shim-not-running"])
def test_on_a_v2_robot_the_daemon_hint_restarts_the_robot_node(overrides, restart):
    gate = _daemon_gate(**overrides)

    assert gate.status == FAIL
    assert restart in gate.hint and "ros2_native/README.md" in gate.hint
    assert "/api/daemon/start" not in gate.hint
    assert "restart reachy-mini-daemon" not in gate.hint


@pytest.mark.parametrize("daemon", [
    HttpResult(False),
    HttpResult(True, 200, {"state": "stopped", "no_media": True}),
], ids=["silent", "pollen-not-running"])
def test_on_pollens_daemon_the_hint_still_starts_the_daemon(daemon):
    gate = _daemon_gate(daemon=daemon)

    assert gate.status == FAIL
    assert "/api/daemon/start?wake_up=true" in gate.hint
    assert "reachy-mini-ros2" not in gate.hint


def test_no_voice_delay_passes():
    assert _by_name(evaluate(_healthy()))["voice_delay"].code == "voice_delay_off"
    assert _by_name(evaluate(_healthy(voice_delay_ms="0")))["voice_delay"].status == PASS


def test_a_voice_delay_with_its_sink_up_passes():
    nodes = CommandResult(available=True, returncode=0, stdout='node.name = "emori_voice"\n')
    gate = _by_name(evaluate(_healthy(voice_delay_ms="150", voice_nodes=nodes)))["voice_delay"]
    assert gate.status == PASS and gate.code == "voice_delay_150ms"


def test_a_voice_delay_with_no_sink_warns_and_does_not_block_the_robot():
    nodes = CommandResult(available=True, returncode=0, stdout='node.name = "another"\n')
    gate = _by_name(evaluate(_healthy(voice_delay_ms="150", voice_nodes=nodes)))["voice_delay"]
    assert gate.status == WARN and gate.code == "voice_delay_sink_missing"
    gate = _by_name(evaluate(_healthy(voice_delay_ms="150")))["voice_delay"]
    assert gate.status == WARN and gate.code == "voice_delay_unreadable"
