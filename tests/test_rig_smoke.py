"""scripts/rig_smoke.py: the post-deploy smoke test's verdicts, with fakes for every edge.

Nothing here reaches Pokamind, the host app or the kiosk browser: the whoami call, the
host app's HTTP API and the kiosk's DevTools port are all fakes with a scripted clock.
What is pinned:

* each check's PASS / FAIL / SKIP logic, including the official end route;
* the no-secret output rule: a token, an SSO login or another credential never
  reaches a printed line, in text or JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from emori_adapter import robot_token
from emori_adapter.hostapp.pokamind_whoami import Whoami, WhoamiModule, WhoamiResult


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "rig_smoke.py"
_WRAPPER = _ROOT / "deploy" / "robot" / "rig-smoke.sh"
GOLDEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
SSO_SECRET = "eyJhbGciOiJFZERTQSJ9.SSOSECRETPAYLOADxxxxxxxxxxxxxxxxxxxxxxxx.sig"


def _load():
    spec = importlib.util.spec_from_file_location("rig_smoke", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rig_smoke"] = module  # dataclasses resolve string annotations through it
    spec.loader.exec_module(module)
    return module


S = _load()


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    async def asleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# roleplay
# --------------------------------------------------------------------------
class FakeRobot:
    """The host app + the kiosk page, driven by the scripted clock."""

    def __init__(self, clock, *, session_active=False, demo=False, first_event_after=4.0,
                 armed_after=9.0, completes_after=6.0, home_after=3.0, page_opens=True,
                 events=True, end_works=True, goes_home=True, kiosk_up=True,
                 auth_fails_after=None) -> None:
        self.clock = clock
        self.base_url = "http://127.0.0.1:8090"
        self.demo = demo
        self.first_event_after = first_event_after
        self.armed_after = armed_after
        self.completes_after = completes_after
        self.home_after = home_after
        self.page_opens = page_opens
        self.events = events
        self.end_works = end_works
        self.goes_home = goes_home
        self.kiosk_up = kiosk_up
        self.auth_fails_after = auth_fails_after
        self.session = {"session_id": "someone-else"} if session_active else None
        self.counters = {"sessions_started": 3, "utterances": 40}
        self.path = "/"
        self.opened_at = None
        self.end_at = None
        self.home_at = None
        self.posts: list[tuple[str, dict]] = []
        self.navigations: list[str] = []
        self.ends = 0
        self._applied: set[str] = set()

    # ---- the simulation
    def _once(self, name, due, apply):
        if due is not None and name not in self._applied and self.clock() >= due:
            self._applied.add(name)
            apply()

    def _tick(self):
        if self.opened_at is not None:
            start = self.opened_at
            self._once("open", start, self._open)
            if self.events:
                self._once("first", start + self.first_event_after,
                           lambda: self._bump("utterances"))
                self._once("armed", start + self.armed_after, lambda: self._bump("turn_armed"))
            if self.auth_fails_after is not None:
                self._once("auth", start + self.auth_fails_after, self._auth_failed)
        if self.end_at is not None and self.end_works:
            self._once("complete", self.end_at + self.completes_after, self._complete)
        if self.home_at is not None and self.goes_home:
            self._once("home", self.home_at + self.home_after, self._go_home)

    def _bump(self, name):
        self.counters[name] = self.counters.get(name, 0) + 1

    def _open(self):
        if self.page_opens:
            self.session = {"session_id": self.sid, "person_id": "robot", "lang": "ca",
                            "topic": "Escolta activa", "cycles": 0}
            self._bump("sessions_started")

    def _auth_failed(self):
        self.session = None
        self._bump("session_start_failed")
        self.home_at = self.clock()

    def _complete(self):
        self.session = None
        self._bump("completed")
        self.home_at = self.clock()

    def _go_home(self):
        self.path = "/"

    # ---- the host app
    def get_json(self, path):
        self._tick()
        if path == "/healthz":
            return 200, {"status": "ok", "demo": self.demo}
        if path == "/api/status":
            return 200, {"session": self.session, "counters": dict(self.counters)}
        if path == "/api/addon/status":
            return 200, {"mode": "idle"}
        if path == "/api/voice-menu":
            return 200, {"prompt": "...", "grammar": [],
                         "options": [{"key": "1", "name": "Escolta activa"},
                                     {"key": "2", "name": "Treball en equip"}]}
        raise AssertionError(path)

    def post_json(self, path, body):
        self._tick()
        self.posts.append((path, dict(body)))
        if path == "/api/voice-select":
            self.sid = "anon-123-abc"
            if self.demo:
                return 200, {"demo": True, "session_url": f"/session?demo=1&sid={self.sid}",
                             "matched": True, "key": "1"}
            return 200, {"demo": False, "anonymous": True, "matched": True, "exact": True,
                         "key": body["text"], "topic": "Escolta activa",
                         "session_url": f"/session?sso=https%3A%2F%2Fpokamind.test%2Fsso%3F"
                                        f"token%3D{SSO_SECRET}&sid={self.sid}",
                         "sso_url": f"https://pokamind.test/sso?token={SSO_SECRET}"}
        if path == "/api/session-start-failed":
            if self.session is not None and self.counters.get("utterances", 0) <= 40:
                self.session = None
                self._bump("session_start_failed")
                self.home_at = self.clock()
                return 202, {"aborted": True}
            return 202, {"aborted": False}
        raise AssertionError(path)

    # ---- the kiosk
    def current_path(self):
        self._tick()
        return self.path if self.kiosk_up else None

    def navigate(self, path):
        self.navigations.append(path)
        if not self.kiosk_up:
            return False
        if path.startswith("/session?"):
            self.path = "/session"
            self.opened_at = self.clock() + 0.5
        else:
            self.path = path
        return True

    def press_end(self):
        self._tick()
        self.ends += 1
        self.end_at = self.clock()
        return True


def _by_name(results):
    return {result.check: result for result in results}


def _roleplay(robot, clock, *, lang="ca", live_lang="ca", lang_explicit=False):
    return _by_name(S.check_roleplay(
        hostapp=robot, kiosk=robot, lang=lang, live_lang=live_lang,
        lang_explicit=lang_explicit, start_timeout_s=150, end_timeout_s=180,
        clock=clock, sleep=clock.sleep,
    ))


def test_roleplay_starts_the_first_module_and_ends_it_through_the_official_route():
    clock = Clock()
    robot = FakeRobot(clock)
    results = _roleplay(robot, clock)

    start, end = results["roleplay.start"], results["roleplay.end"]
    assert start.status == S.PASS, start.detail
    assert "option 1 'Escolta activa' (ca)" in start.detail
    assert "first Pokamind event: AI line" in start.detail
    assert "learner turn armed" in start.detail
    assert end.status == S.PASS, end.detail
    assert "Pokamind completed it" in end.detail and "kiosk back at /" in end.detail
    # The anonymous learner picks option 1 the way the landing page would, the kiosk is
    # sent to the session page the host app returned, and the end key is pressed ONCE.
    assert robot.posts == [("/api/voice-select", {"text": "1"})]
    assert robot.navigations[0].startswith("/session?sso=")
    assert robot.ends == 1


def test_roleplay_refuses_to_interrupt_an_active_session():
    clock = Clock()
    robot = FakeRobot(clock, session_active=True)
    results = _roleplay(robot, clock)
    assert results["roleplay.start"].status == S.SKIP
    assert "already active" in results["roleplay.start"].detail
    assert robot.posts == [] and robot.navigations == []
    # Nothing ran, so the report must not claim the robot spoke or moved.
    assert S.notes_for(list(results.values())) == []


def test_roleplay_skips_a_demo_host_app_and_a_kiosk_in_another_language():
    clock = Clock()
    assert _roleplay(FakeRobot(clock, demo=True), clock)["roleplay.start"].status == S.SKIP
    robot = FakeRobot(clock)
    results = _roleplay(robot, clock, lang="en", live_lang="ca", lang_explicit=True)
    assert results["roleplay.start"].status == S.SKIP
    assert "kiosk language is ca" in results["roleplay.start"].detail
    assert robot.posts == []


def test_roleplay_fails_without_a_kiosk_page():
    clock = Clock()
    results = _roleplay(FakeRobot(clock, kiosk_up=False), clock)
    assert results["roleplay.start"].status == S.FAIL
    assert "DevTools" in results["roleplay.start"].detail


def test_a_session_pokamind_never_starts_is_aborted_through_the_unstarted_route():
    clock = Clock()
    robot = FakeRobot(clock, events=False)
    results = _roleplay(robot, clock)
    assert results["roleplay.start"].status == S.FAIL
    assert "no Pokamind event" in results["roleplay.start"].detail
    end = results["roleplay.end"]
    assert end.status == S.PASS, end.detail
    assert "aborted via /api/session-start-failed" in end.detail
    assert ("/api/session-start-failed", {"reason": "rig_smoke_abort"}) in robot.posts
    assert robot.ends == 0


def test_a_failed_sso_login_fails_the_start_and_names_it():
    clock = Clock()
    robot = FakeRobot(clock, events=False, auth_fails_after=3.0)
    results = _roleplay(robot, clock)
    assert results["roleplay.start"].status == S.FAIL
    assert "session_start_failed" in results["roleplay.start"].detail
    assert results["roleplay.end"].status == S.SKIP
    assert robot.ends == 0


def test_a_session_that_will_not_end_fails_the_end_check():
    clock = Clock()
    results = _roleplay(FakeRobot(clock, end_works=False), clock)
    assert results["roleplay.start"].status == S.PASS
    assert results["roleplay.end"].status == S.FAIL
    assert "still active after 180 s" in results["roleplay.end"].detail


def test_a_kiosk_that_does_not_go_home_fails_and_is_sent_back():
    clock = Clock()
    robot = FakeRobot(clock, goes_home=False)
    results = _roleplay(robot, clock)
    assert results["roleplay.end"].status == S.FAIL
    assert "did not return to /" in results["roleplay.end"].detail
    assert robot.navigations[-1] == "/"


def test_a_page_that_never_opens_is_sent_home():
    clock = Clock()
    robot = FakeRobot(clock, page_opens=False)
    results = _roleplay(robot, clock)
    assert results["roleplay.start"].status == S.FAIL
    assert "did not open" in results["roleplay.start"].detail
    assert robot.navigations[-1] == "/"


# --------------------------------------------------------------------------
# the no-secret output rule
# --------------------------------------------------------------------------
@pytest.mark.parametrize("dangerous", [
    GOLDEN,
    f"Bearer {SSO_SECRET}",
    f"/session?sso=x&token={SSO_SECRET}",
    "access_token=AQ.Ab8RN6Kx-secret",
    "AIza" + "FAKE" * 12,
])
def test_safe_detail_strips_anything_credential_shaped(dangerous):
    cleaned = S.safe_detail(f"before {dangerous} after")
    assert "before" in cleaned and "after" in cleaned
    for fragment in re.split(r"[=&.?\s]", dangerous):
        if len(fragment) >= 12:
            assert fragment not in cleaned


def test_safe_detail_keeps_a_long_robot_id_and_bounds_the_line():
    robot_id = "demo-reception-ward-three-north-wing-0001x"
    assert robot_id in S.safe_detail(f"robot={robot_id}")
    assert len(S.safe_detail("x " * 500)) <= 240
    assert "\n" not in S.safe_detail("one\ntwo\x1b[31m")


# --------------------------------------------------------------------------
# exit code, CLI, wrapper
# --------------------------------------------------------------------------
def test_exit_code_is_1_on_any_failure_and_2_on_a_strict_skip():
    passed = S.Result("a", S.PASS, "")
    skipped = S.Result("b", S.SKIP, "")
    failed = S.Result("c", S.FAIL, "")
    assert S.exit_code([passed, skipped]) == 0
    assert S.exit_code([passed, skipped], strict=True) == 2
    assert S.exit_code([passed, skipped, failed], strict=True) == 1


def test_only_accepts_a_comma_list_of_known_checks():
    assert S.parse_only(None) == S.CHECKS
    assert S.parse_only("control, roleplay") == ("control", "roleplay")
    with pytest.raises(Exception):
        S.parse_only("dance")
    with pytest.raises(SystemExit):
        S.build_parser().parse_args(["--only", "dance"])


def test_the_text_report_is_one_line_per_check():
    results = [S.Result("control.config", S.PASS, "ok"), S.Result("roleplay.end", S.FAIL,
                                                                   "ended in 1.0 s")]
    lines = S.format_text(results, {"robot": "r", "credential": "none", "lang": "ca"},
                          []).splitlines()
    assert lines[0].startswith("rig-smoke  robot=r")
    assert lines[1].startswith("PASS  control.config")
    assert lines[2].startswith("FAIL  roleplay.end")


def _whoami_answer(code="ok"):
    who = Whoami(robot_id="test-robot-01", display_name="Demo", status="active",
                 credential_kind="fleet", key_fingerprint="fp", key_version=1,
                 org="example-org", partner="emori-robot", course_id="example-course",
                 languages=("ca",), pathways=(), identity_domains=(),
                 modules=(WhoamiModule("MOD0000000000000001a", "One", "P", "ca", True, None),),
                 features={})
    if code != "ok":
        return WhoamiResult(code=code, http_status=401)
    return WhoamiResult(ok=True, code="ok", http_status=200, whoami=who, checked_at=0.0)


def test_control_skips_without_a_token_and_names_the_fix():
    result = S.check_control(None, base_url="https://api.example.test", clock=Clock())

    assert (result.check, result.status) == ("control.whoami", S.SKIP)
    assert "Replace token" in result.detail


def test_control_passes_when_pokamind_names_this_robot(monkeypatch):
    seen = []
    monkeypatch.setattr(S, "fetch_whoami", lambda base, token: seen.append((base, token))
                        or _whoami_answer())

    result = S.check_control(robot_token.parse_token(GOLDEN), base_url="https://p.test",
                             clock=Clock())

    assert result.status == S.PASS, result.detail
    assert "languages=ca modules=1" in result.detail
    assert seen[0][0] == "https://p.test"
    assert GOLDEN not in result.detail


def test_control_fails_when_pokamind_refuses_the_credential(monkeypatch):
    monkeypatch.setattr(S, "fetch_whoami", lambda base, token: _whoami_answer("robot_revoked"))

    result = S.check_control(robot_token.parse_token(GOLDEN), base_url="https://p.test",
                             clock=Clock())

    assert result.status == S.FAIL
    assert GOLDEN not in result.detail


def test_run_on_a_tokenless_robot_skips_the_control_check_and_names_the_token(monkeypatch):
    monkeypatch.setattr(S._robot_token, "load_token", lambda: SimpleNamespace(token=None))
    monkeypatch.setattr(S, "load_settings", lambda: None)
    monkeypatch.setattr(S, "live_kiosk_language", lambda: "ca")
    args = S.build_parser().parse_args(["--only", "control", "--lang", "ca"])

    results, header = S.run(args)

    assert header == {"robot": "?", "credential": "none", "lang": "ca"}
    assert [(r.check, r.status) for r in results] == [("control.whoami", S.SKIP)]
    assert "no Pokamind token" in results[0].detail


def test_roleplay_skips_while_an_add_on_mode_is_running():
    clock = Clock()
    robot = FakeRobot(clock)
    get_json = robot.get_json
    robot.get_json = lambda path: ((200, {"mode": "busy"}) if path == "/api/addon/status"
                                   else get_json(path))

    results = _roleplay(robot, clock)

    assert results["roleplay.start"].status == S.SKIP
    assert "add-on mode is active" in results["roleplay.start"].detail
    assert robot.navigations == []


def test_roleplay_runs_when_no_add_on_answers():
    clock = Clock()
    robot = FakeRobot(clock)
    get_json = robot.get_json
    robot.get_json = lambda path: ((503, {"error_code": "addon_unavailable"})
                                   if path == "/api/addon/status" else get_json(path))

    results = _roleplay(robot, clock)

    assert results["roleplay.start"].status == S.PASS, results["roleplay.start"].detail
    assert results["roleplay.end"].status == S.PASS


def test_the_wrapper_runs_the_script_from_the_install_root():
    text = _WRAPPER.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\r" not in text
    assert 'cd "$ROOT"' in text
    assert '"$PY" "$ROOT/scripts/rig_smoke.py"' in text


def _bash():
    """A bash that runs scripts (Git's on Windows; the bare name may be a WSL relay)."""

    for candidate in (shutil.which("bash"), "C:/Program Files/Git/bin/bash.exe"):
        if candidate and Path(candidate).exists():
            try:
                done = subprocess.run([candidate, "-c", "echo ok"], capture_output=True,
                                      text=True, timeout=20, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if done.stdout.strip() == "ok":
                return candidate
    return None


_BASH = _bash()


def _stub_install(tmp_path, *, addon: bool):
    """An install root whose interpreter only prints which script it was asked to run."""

    root = tmp_path / "opt-emori"
    (root / "deploy" / "robot").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "deploy" / "robot" / "rig-smoke.sh").write_bytes(_WRAPPER.read_bytes())
    python = root / ".venv" / "bin" / "python"
    python.write_bytes(
        b'#!/bin/sh\necho "ran $(basename "$1") $2 $3"\n'
        b'case "$1" in *addon_smoke.py) exit "${ADDON_RC:-0}" ;; esac\nexit "${CORE_RC:-0}"\n')
    python.chmod(0o755)
    (root / "scripts" / "rig_smoke.py").write_text("", encoding="utf-8")
    if addon:
        scripts = root / "addons" / "example" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "addon_smoke.py").write_text("", encoding="utf-8")
    return root


def _wrapper_run(root, *args, **env):
    return subprocess.run([_BASH, str(root / "deploy" / "robot" / "rig-smoke.sh"), *args],
                          capture_output=True, text=True, check=False,
                          env={**os.environ, **env})


def _wrapper_scripts(done):
    return [line.split()[1] for line in done.stdout.splitlines()]


@pytest.mark.skipif(_BASH is None, reason="needs a working bash")
def test_the_wrapper_runs_the_core_checks_alone_when_no_add_on_is_installed(tmp_path):
    root = _stub_install(tmp_path, addon=False)

    done = _wrapper_run(root, "--json")

    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["ran", "rig_smoke.py", "--json"]


@pytest.mark.skipif(_BASH is None, reason="needs a working bash")
def test_asking_for_the_add_ons_alone_with_none_installed_says_so(tmp_path):
    root = _stub_install(tmp_path, addon=False)

    plain = _wrapper_run(root, "--addons")
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout.split()[:2] == ["SKIP", "addons"]

    assert _wrapper_run(root, "--addons", "--strict").returncode == 2

    report = _wrapper_run(root, "--addons", "--json", "--strict")
    assert report.returncode == 2
    answer = json.loads(report.stdout)
    assert answer["exit_code"] == 2 and answer["results"][0]["status"] == "SKIP"


@pytest.mark.skipif(_BASH is None, reason="needs a working bash")
def test_the_wrapper_runs_each_installed_add_ons_smoke_test_after_the_core_checks(tmp_path):
    root = _stub_install(tmp_path, addon=True)

    both = _wrapper_run(root)
    assert both.returncode == 0, both.stderr
    assert _wrapper_scripts(both) == ["rig_smoke.py", "addon_smoke.py"]
    addons = _wrapper_run(root, "--addons")
    assert _wrapper_scripts(addons) == ["addon_smoke.py"]


@pytest.mark.skipif(_BASH is None, reason="needs a working bash")
@pytest.mark.parametrize("args, expected", [
    (("--only", "control"), ["ran rig_smoke.py --only control"]),
    (("--only=roleplay",), ["ran rig_smoke.py --only roleplay"]),
    (("--only", "other"), ["ran addon_smoke.py --only other"]),
    (("--only=other",), ["ran addon_smoke.py --only other"]),
    (("--only", "control,other"), ["ran rig_smoke.py --only control",
                                   "ran addon_smoke.py --only other"]),
    (("--addons", "--only", "other"), ["ran addon_smoke.py --only other"]),
])
def test_the_wrapper_routes_only_names_to_the_run_that_has_them(tmp_path, args, expected):
    root = _stub_install(tmp_path, addon=True)

    done = _wrapper_run(root, *args)

    assert done.returncode == 0, done.stderr
    assert [" ".join(line.split()) for line in done.stdout.splitlines()] == expected


@pytest.mark.skipif(_BASH is None, reason="needs a working bash")
def test_the_wrapper_leaves_an_unknown_name_to_the_core_when_no_add_on_is_installed(tmp_path):
    root = _stub_install(tmp_path, addon=False)

    done = _wrapper_run(root, "--only", "other")

    assert " ".join(done.stdout.split()) == "ran rig_smoke.py --only other"


@pytest.mark.skipif(_BASH is None, reason="needs a working bash")
@pytest.mark.parametrize("args", [("--json",), ("--only", "control,other", "--json")])
def test_the_wrapper_refuses_json_that_would_be_two_documents(tmp_path, args):
    root = _stub_install(tmp_path, addon=True)

    done = _wrapper_run(root, *args)

    assert done.returncode == 2 and done.stdout == ""
    assert "--json" in done.stderr


@pytest.mark.skipif(_BASH is None, reason="needs a working bash")
@pytest.mark.parametrize("args", [("--only", "control", "--json"), ("--addons", "--json")])
def test_the_wrapper_keeps_json_for_a_single_run(tmp_path, args):
    root = _stub_install(tmp_path, addon=True)

    done = _wrapper_run(root, *args)

    assert done.returncode == 0, done.stderr
    assert len(done.stdout.splitlines()) == 1


@pytest.mark.skipif(_BASH is None, reason="needs a working bash")
@pytest.mark.parametrize("core, addon, expected", [
    ("0", "0", 0), ("1", "0", 1), ("0", "1", 1), ("0", "2", 2), ("2", "0", 2),
    ("1", "2", 1), ("2", "1", 1),
])
def test_the_wrappers_exit_code_puts_a_fail_above_a_skip(tmp_path, core, addon, expected):
    root = _stub_install(tmp_path, addon=True)

    assert _wrapper_run(root, CORE_RC=core, ADDON_RC=addon).returncode == expected


def test_the_wrapper_names_exactly_the_core_checks():
    assert f"{'|'.join(S.CHECKS)})" in _WRAPPER.read_text(encoding="utf-8")


def test_an_option_of_another_run_is_ignored_with_a_note(monkeypatch, capsys):
    monkeypatch.setattr(S, "run", lambda args: ([], {}))

    assert S.main(["--only", "control", "--other", "1"]) == 0
    assert "--other 1" in capsys.readouterr().err


# --------------------------------------------------------------------------
# the two loopback clients, against real sockets
# --------------------------------------------------------------------------
class _JsonHandler(BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, *args):  # keep the test output quiet
        pass

    def _reply(self):
        status, body = self.routes.get((self.command, self.path), (404, {"error": "nope"}))
        if callable(body):
            length = int(self.headers.get("content-length") or 0)
            body = body(json.loads(self.rfile.read(length) or b"{}"))
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _reply
    do_POST = _reply


@contextlib.contextmanager
def _http(routes):
    handler = type("Handler", (_JsonHandler,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_the_host_app_client_speaks_json_and_reports_status_codes():
    routes = {
        ("GET", "/api/status"): (200, {"session": None, "counters": {"cycles": 2}}),
        ("POST", "/api/voice-select"): (200, lambda body: {"echo": body}),
        ("GET", "/api/addon/status"): (503, {"error_code": "addon_unavailable"}),
    }
    with _http(routes) as port:
        client = S.HostappHttp(f"http://127.0.0.1:{port}")
        assert client.get_json("/api/status") == (200, {"session": None,
                                                        "counters": {"cycles": 2}})
        assert client.post_json("/api/voice-select", {"text": "1"}) == (
            200, {"echo": {"text": "1"}})
        assert client.get_json("/api/addon/status") == (
            503, {"error_code": "addon_unavailable"})
    assert S.HostappHttp(f"http://127.0.0.1:{port}").get_json("/healthz") == (0, None)


def test_the_kiosk_client_finds_the_page_navigates_it_and_presses_end():
    from websockets.sync.server import serve

    calls = []

    def cdp(socket):
        for raw in socket:
            message = json.loads(raw)
            calls.append(message)
            # A CDP socket interleaves events with replies; the client must skip them.
            socket.send(json.dumps({"method": "Page.frameStartedLoading", "params": {}}))
            if message["method"] == "Runtime.evaluate":
                socket.send(json.dumps({"id": message["id"], "result": {
                    "result": {"type": "boolean", "value": True}}}))
            else:
                socket.send(json.dumps({"id": message["id"], "result": {"frameId": "F"}}))

    with serve(cdp, "127.0.0.1", 0) as ws_server:
        ws_port = ws_server.socket.getsockname()[1]
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        hostapp_port = 8090
        targets = [
            {"type": "iframe", "url": "https://pokamind.test/session",
             "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/X"},
            {"type": "page", "url": f"http://localhost:{hostapp_port}/session?sso={SSO_SECRET}"
                                    "&sid=abc",
             "webSocketDebuggerUrl": f"ws://127.0.0.1:{ws_port}/devtools/page/K"},
        ]
        with _http({("GET", "/json/list"): (200, targets)}) as cdp_port:
            kiosk = S.KioskCdp(cdp_port, hostapp_port, timeout_s=3.0)
            assert kiosk.current_path() == "/session"
            assert kiosk.navigate("/") is True
            assert kiosk.press_end() is True
        ws_server.shutdown()
    navigate, evaluate = calls
    assert navigate["method"] == "Page.navigate"
    assert navigate["params"] == {"url": f"http://localhost:{hostapp_port}/"}
    assert evaluate["method"] == "Runtime.evaluate"
    assert "key:'e'" in evaluate["params"]["expression"]
    # No kiosk page at all: a closed DevTools port reads as "no kiosk", never a crash.
    assert S.KioskCdp(1, hostapp_port, timeout_s=0.5).current_path() is None


