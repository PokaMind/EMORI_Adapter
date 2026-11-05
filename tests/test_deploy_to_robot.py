"""The one-click deploy: `scripts/deploy_to_robot.py` (the computer half),
`deploy/robot/one-click-deploy.sh` (the robot half) and the two double-click launchers.

The robot half is RUN here, against stub `sudo`/`systemctl`/`curl`/`docker` on PATH,
because what matters about it is what it does and refuses to do on a live robot: it must
never restart anything when it refuses, never restart Pollen's daemon under a running
v2 node, and never delete a file it cannot put back.
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "deploy_to_robot.py"
_ROBOT_HALF = _ROOT / "deploy" / "robot" / "one-click-deploy.sh"
_MODULE_NAME = "deploy_to_robot_under_test"


@pytest.fixture(scope="module")
def deploy():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(_MODULE_NAME, None)


# --- the computer half: pure functions ------------------------------------------------

_IPCONFIG = """
Windows IP Configuration

Wireless LAN adapter Wi-Fi:
   Link-local IPv6 Address . . . . . : fe80::1c2d:3e4f:5a6b:7c8d%12
   IPv4 Address. . . . . . . . . . . : 192.168.1.23
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.1.1

Wireless LAN adapter Local Area Connection* 10:
   IPv4 Address. . . . . . . . . . . : 192.168.137.1
   Subnet Mask . . . . . . . . . . . : 255.255.255.0

Ethernet adapter Ethernet:
   Autoconfiguration IPv4 Address. . : 169.254.12.7
   Subnet Mask . . . . . . . . . . . : 255.255.0.0
"""

_IP_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
3: wlan0    inet 10.0.4.17/24 brd 10.0.4.255 scope global dynamic wlan0
4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0
"""


def test_local_addresses_are_read_from_any_os_listing(deploy):
    """Netmasks, loopback, link-local and broadcast addresses are not places a robot lives."""

    assert deploy.extract_ipv4s(_IPCONFIG) == ["192.168.1.23", "192.168.1.1", "192.168.137.1"]
    assert deploy.extract_ipv4s(_IP_ADDR) == ["10.0.4.17", "172.17.0.1"]


def test_the_windows_hotspot_is_scanned_right_after_the_main_network(deploy):
    """A robot joined to this PC's Mobile Hotspot lives on 192.168.137.0/24."""

    networks = deploy.networks_to_scan(
        ["10.0.4.17", "172.17.0.1", "192.168.137.1", "10.0.4.1", "192.168.137.1"])

    assert [str(n) for n in networks] == ["10.0.4.0/24", "192.168.137.0/24", "172.17.0.0/24"]
    assert len(deploy.networks_to_scan([f"10.0.{i}.1" for i in range(20)], limit=6)) == 6


def test_fill_env_sets_every_occurrence_and_never_ships_crlf(deploy):
    template = ("# comment\r\nPOKAMIND_BASE=http://localhost:4000\r\n"
                "#EMORI_ADMIN_PIN=commented\r\nEMORI_ROBOT_NAME=\r\n"
                "EMORI_ROBOT_NAME=\r\nOTHER=kept")

    text = deploy.fill_env(template, {"POKAMIND_BASE": "https://api.pokamind.com",
                                      "EMORI_ROBOT_NAME": "Reception",
                                      "EMORI_ADMIN_PIN": "123456"})

    assert "\r" not in text
    assert text.endswith("\n")
    assert "POKAMIND_BASE=https://api.pokamind.com\n" in text
    # dotenv keeps the LAST assignment, so a duplicate left empty would win
    assert text.count("EMORI_ROBOT_NAME=Reception\n") == 2
    assert "#EMORI_ADMIN_PIN=commented\n" in text
    assert text.endswith("OTHER=kept\nEMORI_ADMIN_PIN=123456\n")


def test_fill_env_with_no_values_only_normalises(deploy):
    assert deploy.fill_env("A=1\r\nB=2\r\n\r\n", {}) == "A=1\nB=2\n"


def test_the_real_env_example_takes_the_answers(deploy):
    template = (_ROOT / ".env.example").read_text(encoding="utf-8")
    keys = [key for key, *_ in deploy.ENV_QUESTIONS]

    text = deploy.fill_env(template, {key: f"value-{key}" for key in keys})

    for key in keys:
        assert f"\n{key}=value-{key}\n" in text
        # ...in place, not appended: the example documents each one where it sits
        assert f"{key}=" in template


def test_parse_kv_and_shell_word(deploy):
    assert deploy.parse_kv("a=1\nnoise\n b=x=y \n=bad\n") == {"a": "1", "b": "x=y"}
    assert deploy._shell_word("feature/x; rm -rf ~") == "feature/x__rm_-rf__"
    assert deploy._shell_word("") == "unknown"


def test_ssh_is_batch_mode_with_its_own_known_hosts(deploy):
    command = deploy.Robot("192.0.2.20", "pollen").ssh()

    assert command[0] == "ssh"
    assert command[-1] == "pollen@192.0.2.20"
    assert "BatchMode=yes" in command
    assert "StrictHostKeyChecking=accept-new" in command
    # a tilde path: ssh expands it, and a Windows profile path with a space never
    # reaches UserKnownHostsFile's whitespace-separated list
    assert "UserKnownHostsFile=~/.ssh/emori_known_hosts" in command
    assert "-t" in deploy.Robot("h", "u").ssh(tty=True)
    assert "BatchMode=yes" not in deploy.Robot("h", "u").ssh(batch=False)


# --- the computer half: telling a robot from everything else --------------------------


class _Face(http.server.BaseHTTPRequestHandler):
    routes: ClassVar[dict] = {}

    def do_GET(self):
        body = self.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _serve(routes: dict) -> tuple[http.server.HTTPServer, int]:
    handler = type("Face", (_Face,), {"routes": routes})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _closed_port() -> int:
    server, port = _serve({})
    server.shutdown()
    server.server_close()
    return port


@pytest.mark.parametrize(("status", "expected"), [
    ({"state": "running", "robot_name": "reachy_mini"}, "reachy_mini (Pollen daemon)"),
    ({"state": "running", "backend": "reachy_mini_ros2"}, "robot (v2 ROS node)"),
    ({"state": "running", "simulation_enabled": True}, None),
    ({"state": "running", "mockup_sim_enabled": True}, None),
    ({"unrelated": "json"}, None),
])
def test_the_daemon_face_identifies_a_real_robot_and_never_a_simulator(deploy, status, expected):
    server, port = _serve({"/api/daemon/status": status})
    closed = _closed_port()
    try:
        assert deploy.identify("127.0.0.1", ports=(port, closed, closed)) == expected
    finally:
        server.shutdown()


def test_emori_admin_identifies_a_robot_whose_daemon_is_down(deploy):
    admin, admin_port = _serve({"/healthz": {"status": "ok", "service": "emori-admin"}})
    other, other_port = _serve({"/healthz": {"status": "ok", "service": "something-else"}})
    closed = _closed_port()
    try:
        found = deploy.identify("127.0.0.1", ports=(closed, admin_port, closed))
        assert found == "robot (EMORI admin)"
        assert deploy.identify("127.0.0.1", ports=(closed, other_port, closed)) is None
        assert deploy.identify("127.0.0.1", ports=(closed, closed, closed)) is None
    finally:
        admin.shutdown()
        other.shutdown()


# --- the computer half: what the preflight refuses ------------------------------------


class _FakeRobot:
    """The preflight's two reads: the state script, then the clock (``offset`` seconds
    off this computer's; ``None`` = the robot cannot say)."""

    def __init__(self, stdout: str, returncode: int = 0, offset: float | None = 0.0):
        self.user = "pollen"
        self._result = subprocess.CompletedProcess([], returncode, stdout.encode(), b"boom")
        self._offset = offset

    def run(self, remote, **_):
        if remote == "date -u +%s.%N":
            if self._offset is None:
                return subprocess.CompletedProcess([], 1, b"", b"")
            return subprocess.CompletedProcess(
                [], 0, f"{time.time() + self._offset:.9f}\n".encode(), b"")
        assert remote == "bash -s"
        return self._result


_HEALTHY = ("hostname=reachy-mini\nenv=yes\nsudo=yes\nfree_mb=2800\n"
            "v2_fence=no\nv2_running=no\ninternet=yes\n")


def test_preflight_passes_a_healthy_robot(deploy):
    info = deploy.preflight(_FakeRobot(_HEALTHY))

    assert info["hostname"] == "reachy-mini"
    assert info["env"] == "yes"


def test_preflight_refuses_without_passwordless_sudo(deploy):
    with pytest.raises(deploy.DeployError, match="passwordless sudo"):
        deploy.preflight(_FakeRobot(_HEALTHY.replace("sudo=yes", "sudo=no")))


def test_preflight_refuses_v2_on_trial(deploy):
    """Running v2 node + no fence: install.sh would restart Pollen's daemon onto the bus."""

    on_trial = _HEALTHY.replace("v2_running=no", "v2_running=true")
    with pytest.raises(deploy.DeployError, match="fence"):
        deploy.preflight(_FakeRobot(on_trial))
    fenced = on_trial.replace("v2_fence=no", "v2_fence=yes")
    assert deploy.preflight(_FakeRobot(fenced))["v2_running"] == "true"


def test_the_preflight_script_is_read_only(deploy):
    """It runs before the person has confirmed anything."""

    for verb in ("systemctl", "rm ", "mv ", "tee ", "chown", "chmod", "docker run",
                 "docker compose", " > /", ">>"):
        assert verb not in deploy.PREFLIGHT


# --- the launchers ---------------------------------------------------------------------


def test_the_windows_launcher_is_crlf_and_runs_the_script():
    attributes = (_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.cmd     text eol=crlf" in attributes

    text = (_ROOT / "Deploy-to-Robot.cmd").read_text(encoding="ascii")
    assert r"scripts\deploy_to_robot.py %*" in text
    assert "pause" in text


def test_the_mac_and_linux_launcher_is_lf_executable_and_runs_the_script():
    path = _ROOT / "Deploy-to-Robot.command"
    raw = path.read_bytes()

    assert raw.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r\n" not in raw
    assert b'scripts/deploy_to_robot.py "$@"' in raw
    if os.name != "nt":
        assert os.access(path, os.X_OK)


# --- the robot half, run against stubs --------------------------------------------------

_STUBS = {
    # `sudo -n X` runs X; FAKE_NO_SUDO makes every sudo fail, as on a robot without NOPASSWD
    "sudo": '[ "$1" = "-n" ] && shift\n[ -n "${FAKE_NO_SUDO:-}" ] && exit 1\nexec "$@"\n',
    "systemctl": 'echo "systemctl $*" >> "$STUB_LOG"\n'
                 'if [ "$1" = is-enabled ] && [ -n "${FAKE_NOT_ENABLED:-}" ]; then exit 1; fi\n'
                 "exit 0\n",
    "curl": 'echo "curl $*" >> "$STUB_LOG"\n'
            'case "$*" in *pypi.org*) [ -n "${FAKE_ONLINE:-}" ] || exit 7 ;; esac\nexit 0\n',
    "docker": 'echo "docker $*" >> "$STUB_LOG"\n'
              'if [ -n "${FAKE_V2_RUNNING:-}" ]; then echo true; exit 0; fi\nexit 1\n',
    "sleep": "exit 0\n",
}


@pytest.fixture
def rig(tmp_path):
    if shutil.which("bash") is None or os.name == "nt":
        pytest.skip("the robot half is bash on Linux")
    root = tmp_path / "opt-emori"
    robot = root / "deploy" / "robot"
    robot.mkdir(parents=True)
    shutil.copy2(_ROBOT_HALF, robot / "one-click-deploy.sh")
    (robot / "install.sh").write_text(
        'echo "install.sh ran" >> "$STUB_LOG"\nexit "${FAKE_INSTALL_RC:-0}"\n')
    (robot / "readycheck.sh").write_text(
        'echo "readycheck $*" >> "$STUB_LOG"\n'
        'n=$(grep -c "^readycheck" "$STUB_LOG")\n'
        '[ -n "${FAKE_NOT_READY:-}" ] && exit 1\n'
        '[ -n "${FAKE_WARMUP:-}" ] && [ "$n" -eq 1 ] && exit 1\nexit 0\n')
    (robot / "ros2_native").mkdir()
    (robot / "ros2_native" / "v2-stack.sh").write_text(
        'echo "v2-stack $*" >> "$STUB_LOG"\nexit "${FAKE_STACK_RC:-0}"\n')
    (root / ".env").write_text("HOSTAPP_PORT=8090\n")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "src").mkdir()
    (root / "src" / "keep.py").write_text("keep\n")
    (root / "src" / "old.py").write_text("old\n")
    (root / "src" / "local.py").write_text("never shipped by any deploy\n")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "last-deploy.files").write_text("src/\nsrc/keep.py\nsrc/old.py\n")
    files = tmp_path / "files.txt"
    files.write_text("deploy/\ndeploy/robot/\ndeploy/robot/one-click-deploy.sh\nsrc/\nsrc/keep.py\n")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in _STUBS.items():
        stub = stubs / name
        stub.write_text("#!/bin/sh\n" + body)
        stub.chmod(0o755)
    log = tmp_path / "stub.log"
    log.touch()

    def run(*args, **env):
        environment = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
                       "STUB_LOG": str(log), "EMORI_V2_FENCE": str(tmp_path / "fence"), **env}
        result = subprocess.run(
            ["bash", str(robot / "one-click-deploy.sh"), "--files", str(files), *args],
            capture_output=True, text=True, env=environment, timeout=60, check=False)
        result.log = log.read_text()
        return result

    run.root = root
    run.fence = tmp_path / "fence"
    return run


def test_the_robot_half_is_lf_bash():
    raw = _ROBOT_HALF.read_bytes()

    assert raw.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r\n" not in raw
    if shutil.which("bash") and os.name != "nt":
        assert subprocess.run(["bash", "-n", str(_ROBOT_HALF)], check=False).returncode == 0


def test_no_passwordless_sudo_refuses_and_restarts_nothing(rig):
    result = rig("--quick", FAKE_NO_SUDO="1")

    assert result.returncode == 3
    assert "passwordless sudo" in result.stdout
    assert "systemctl" not in result.log
    assert "install.sh" not in result.log


def test_no_env_refuses(rig):
    (rig.root / ".env").unlink()

    result = rig("--quick")

    assert result.returncode == 3
    assert ".env" in result.stdout


def test_the_ros2_stack_is_put_in_place_after_the_install_and_before_readycheck(rig):
    result = rig("--stack", "v1", FAKE_ONLINE="1")

    assert result.returncode == 0, result.stdout
    lines = result.log.splitlines()
    first_readycheck = next(i for i, line in enumerate(lines) if line.startswith("readycheck"))
    assert lines.index("install.sh ran") < lines.index("v2-stack apply") < first_readycheck
    assert "EMORI_ROS2_STACK=v1" in (rig.root / ".env").read_text()


def test_a_stack_that_is_not_v1_or_v2_stops_before_anything_runs(rig):
    result = rig("--stack", "v3", FAKE_ONLINE="1")

    assert result.returncode == 2
    assert "install.sh" not in result.log and "systemctl" not in result.log
    assert "systemctl" not in result.log


def test_v2_on_trial_refuses_before_install_can_restart_the_daemon(rig):
    result = rig(FAKE_V2_RUNNING="1", FAKE_ONLINE="1")

    assert result.returncode == 3
    assert "fence" in result.stdout
    assert "install.sh ran" not in result.log


def test_a_fenced_v2_rig_deploys(rig):
    rig.fence.touch()

    result = rig(FAKE_V2_RUNNING="1", FAKE_ONLINE="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "install.sh ran" in result.log
    assert "v2 (fenced)" in result.stdout


def test_online_runs_install_sh_and_records_the_deploy(rig):
    result = rig("--commit", "abc123", "--branch", "master", "--from", "laptop", FAKE_ONLINE="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "install.sh ran" in result.log
    assert "readycheck --no-speak" in result.log
    record = (rig.root / "artifacts" / "last-deploy.env").read_text()
    assert "commit=abc123\n" in record
    assert "branch=master\n" in record
    assert "mode=full\n" in record
    assert (rig.root / "artifacts" / "last-full-install.pyproject.sha256").is_file()


def test_the_generated_credentials_are_shown_again_after_readycheck_then_deleted(rig):
    robot = rig.root / "deploy" / "robot"
    (robot / "install.sh").write_text(
        'echo "install.sh ran" >> "$STUB_LOG"\n'
        'printf "WRITE THIS DOWN pin 135790\n" > "$EMORI_CREDENTIALS_FILE"\n'
        'exit 0\n')

    result = rig(FAKE_ONLINE="1")

    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert out.rindex("WRITE THIS DOWN pin 135790") > out.index("== readycheck ==")
    assert not (rig.root / "artifacts" / ".new-credentials").exists()


def test_the_readycheck_warnings_are_recorded_by_gate_name(rig):
    robot = rig.root / "deploy" / "robot"
    (robot / "readycheck.sh").write_text(
        'echo "readycheck $*" >> "$STUB_LOG"\n'
        'printf "PASS 3  FAIL 0  SKIP 0  WARN 2\n  ~ voice_menu: a hint\n'
        '  ~ gaze: another\n"\n'
        'exit 0\n')

    result = rig(FAKE_ONLINE="1")

    assert result.returncode == 0, result.stdout + result.stderr
    summary = (rig.root / "artifacts" / "last-deploy-readycheck.env").read_text()
    assert "rc=0\n" in summary and "warn_gates=gaze,voice_menu\n" in summary
    assert not (rig.root / "artifacts" / ".last-deploy-readycheck.txt").exists()


def test_offline_becomes_quick_and_never_runs_install_sh(rig):
    result = rig()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUICK deploy instead" in result.stdout
    assert "install.sh ran" not in result.log
    assert "systemctl try-restart emori-hostapp emori-admin" in result.log
    assert "mode=quick\n" in (rig.root / "artifacts" / "last-deploy.env").read_text()


def test_quick_on_a_robot_without_emori_refuses(rig):
    result = rig("--quick", FAKE_NOT_ENABLED="1")

    assert result.returncode == 3
    assert "try-restart" not in result.log


def test_quick_warns_when_dependencies_changed_since_the_last_full_install(rig):
    (rig.root / "artifacts" / "last-full-install.pyproject.sha256").write_text("stale\n")

    result = rig("--quick")

    assert "pyproject.toml changed" in result.stdout


def test_install_sh_failing_is_its_own_exit_code(rig):
    result = rig(FAKE_ONLINE="1", FAKE_INSTALL_RC="5")

    assert result.returncode == 4
    assert "install.sh failed (exit 5)" in result.stdout
    assert "readycheck" not in result.log


def test_files_the_last_deploy_shipped_and_this_one_did_not_are_moved_not_deleted(rig):
    result = rig("--quick")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (rig.root / "src" / "old.py").exists()
    moved = list((rig.root / "artifacts" / "superseded").glob("*/src/old.py"))
    assert len(moved) == 1 and moved[0].read_text() == "old\n"
    # shipped this time, or never shipped by any deploy: both stay exactly where they are
    assert (rig.root / "src" / "keep.py").exists()
    assert (rig.root / "src" / "local.py").exists()
    assert "src/old.py" not in (rig.root / "artifacts" / "last-deploy.files").read_text()


def test_readycheck_gets_one_warm_up_retry(rig):
    result = rig("--quick", FAKE_WARMUP="1")

    assert result.returncode == 0
    assert result.log.count("readycheck --no-speak") == 2


def test_a_robot_that_stays_unready_says_so(rig):
    result = rig("--quick", FAKE_NOT_READY="1")

    assert result.returncode == 1
    assert result.log.count("readycheck --no-speak") == 2


# --- THIS robot's Pokamind token -----------------------------------------------------------

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
SEED = GOLDEN_TOKEN.rsplit(".", 1)[1]


def test_a_token_is_validated_locally_with_the_robots_own_grammar(deploy):
    assert deploy.parse_robot_token(f" {GOLDEN_TOKEN}\n") == ("test-robot-01", GOLDEN_TOKEN)
    for bad in ("", "pkr1.test-robot-01", GOLDEN_TOKEN[:-1], GOLDEN_TOKEN + "A",
                GOLDEN_TOKEN[:-1] + "9",            # non-canonical spare bits
                GOLDEN_TOKEN.replace("test-robot-01", "Test-Robot-01"),
                GOLDEN_TOKEN.replace("pkr1", "pkr2"),
                GOLDEN_TOKEN[:30] + " " + GOLDEN_TOKEN[30:]):
        with pytest.raises(deploy.DeployError):
            deploy.parse_robot_token(bad)
    # ...and agrees with the robot's own parser.
    from emori_adapter import robot_token
    assert robot_token.parse_token(GOLDEN_TOKEN).robot_id == "test-robot-01"


def test_only_the_masked_token_is_ever_printed(deploy):
    assert deploy.mask_token(GOLDEN_TOKEN) == "pkr1.test-robot-01...dHh8"
    assert SEED not in deploy.mask_token(GOLDEN_TOKEN)


def _args(*flags):
    import argparse

    namespace = argparse.Namespace(token_file=None, replace_token=False, no_token=False)
    for flag in flags:
        setattr(namespace, flag, True)
    return namespace


def test_a_robot_that_has_a_token_keeps_it_without_a_question(deploy, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: pytest.fail("asked"))

    token = deploy.choose_token(_args(), {"token": "yes", "token_robot": "robot-01"},
                                interactive=True)

    assert token is None
    assert "robot-01" in capsys.readouterr().out


def test_the_token_is_asked_as_a_secret_and_a_bad_paste_is_asked_again(deploy, monkeypatch,
                                                                       capsys):
    answers = iter([GOLDEN_TOKEN[:-1] + "9", GOLDEN_TOKEN])  # one character mangled
    prompts = []

    def ask(prompt, default="", secret=False):
        prompts.append((prompt, secret))
        return next(answers)

    monkeypatch.setattr(deploy, "ask", ask)

    token = deploy.choose_token(_args(), {"token": "no"}, interactive=True)

    assert token == GOLDEN_TOKEN
    assert len(prompts) == 2 and all(secret for _prompt, secret in prompts)
    out = capsys.readouterr().out
    assert "damaged" in out and "test-robot-01" in out and SEED not in out


def test_an_empty_answer_or_no_keyboard_deploys_without_a_token(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: "")
    assert deploy.choose_token(_args(), {"token": "no"}, interactive=True) is None
    assert deploy.choose_token(_args(), {"token": "no"}, interactive=False) is None
    assert deploy.choose_token(_args("no_token"), {"token": "no"}, interactive=True) is None


def test_a_token_file_is_read_and_replace_token_asks_even_when_one_exists(deploy, tmp_path,
                                                                          monkeypatch):
    path = tmp_path / "token.txt"
    path.write_text(GOLDEN_TOKEN + "\n", encoding="ascii")
    args = _args()
    args.token_file = path
    assert deploy.choose_token(args, {"token": "no"}, interactive=False) == GOLDEN_TOKEN

    monkeypatch.setattr(deploy, "ask", lambda *a, **k: GOLDEN_TOKEN)
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: True)
    assert deploy.choose_token(_args("replace_token"),
                               {"token": "yes", "token_robot": "test-robot-01"},
                               interactive=True) == GOLDEN_TOKEN


@pytest.mark.parametrize("info", [
    {"token": "no", "env_robot_id": "ward-3"},
    {"token": "yes", "token_robot": "ward-3"},
])
def test_a_token_for_another_robot_needs_a_human_yes(deploy, monkeypatch, info):
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: GOLDEN_TOKEN)
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: False)

    with pytest.raises(deploy.DeployError, match="robot id mismatch"):
        deploy.choose_token(_args("replace_token"), info, interactive=True)
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: True)
    assert deploy.choose_token(_args("replace_token"), info, interactive=True) == GOLDEN_TOKEN


def test_without_a_keyboard_a_mismatched_token_file_always_stops(deploy, tmp_path):
    path = tmp_path / "token.txt"
    path.write_text(GOLDEN_TOKEN, encoding="ascii")
    args = _args()
    args.token_file = path

    with pytest.raises(deploy.DeployError, match="robot id mismatch"):
        deploy.choose_token(args, {"token": "no", "env_robot_id": "ward-3"}, interactive=False)


def test_a_matching_emori_robot_id_is_no_conflict(deploy, monkeypatch):
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: GOLDEN_TOKEN)
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: pytest.fail("asked"))

    assert deploy.choose_token(_args(), {"token": "no", "env_robot_id": "Test-Robot-01"},
                               interactive=True) == GOLDEN_TOKEN


class _TokenRobot:
    def __init__(self, returncode=0,
                 stdout=b"token_installed=test-robot-01\nrobot_id_set=test-robot-01\n"):
        self.calls = []
        self._result = subprocess.CompletedProcess([], returncode, stdout, b"refused")

    def run(self, remote, *, stdin=None, **_):
        self.calls.append((remote, stdin))
        return self._result


def test_the_token_travels_on_stdin_never_on_the_command_line(deploy, capsys):
    robot = _TokenRobot()

    deploy.install_token(robot, GOLDEN_TOKEN)

    (remote, stdin), = robot.calls
    assert SEED not in remote and "pkr1.test" not in remote
    assert stdin == (GOLDEN_TOKEN + "\n").encode("ascii")
    # ...to the script the code shipped just before, which asks Pokamind first.
    assert remote == "bash /opt/emori/deploy/robot/install-token.sh --stdin --defer"
    out = capsys.readouterr().out
    assert "EMORI_ROBOT_ID set to 'test-robot-01'" in out and SEED not in out


def test_a_robot_that_refuses_the_token_stops_the_deploy(deploy):
    with pytest.raises(deploy.DeployError, match="Pokamind token"):
        deploy.install_token(_TokenRobot(returncode=3), GOLDEN_TOKEN)


def test_no_credential_but_the_token_is_ever_asked(deploy, monkeypatch, tmp_path):
    asked = []

    def ask(prompt, default="", secret=False):
        asked.append(prompt)
        return "123456" if "PIN" in prompt else default

    monkeypatch.setattr(deploy, "ask", ask)
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(deploy, "_has_keyboard", lambda: True)
    monkeypatch.setattr(deploy, "LOCAL_ENV", tmp_path / "robot.local.env")

    text = deploy._env_from_questions()
    assert not any("secret" in prompt.lower() for prompt in asked)
    # The PIN only: the Pokamind server is the template's public default.
    assert len(asked) == 1 and "PIN" in asked[0]
    assert "\nEMORI_ADMIN_PIN=123456\n" in text
    assert "\nPOKAMIND_BASE=https://api.pokamind.com\n" in text
    for retired in deploy.RETIRED_ENV_KEYS:
        assert not [line for line in text.splitlines() if line.startswith(retired + "=")]
    saved = (tmp_path / "robot.local.env").read_text(encoding="utf-8")
    assert SEED not in saved
    assert not [line for line in saved.splitlines()
                if line.startswith("POKAMIND_ROBOT_TOKEN=") and line.split("=", 1)[1]]


def test_an_empty_pin_answer_generates_one_for_this_robot_only(deploy, monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "ask", lambda prompt, default="", secret=False: default)
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(deploy, "_has_keyboard", lambda: True)
    monkeypatch.setattr(deploy, "LOCAL_ENV", tmp_path / "robot.local.env")
    generated = {}

    text = deploy._env_from_questions(generated=generated)

    pin = generated["EMORI_ADMIN_PIN"]
    assert re.fullmatch(r"[0-9]{6}", pin)
    assert f"\nEMORI_ADMIN_PIN={pin}\n" in text
    # The answers file is for the NEXT robot: it never carries this robot's PIN.
    saved = (tmp_path / "robot.local.env").read_text(encoding="utf-8")
    assert f"EMORI_ADMIN_PIN={pin}" not in saved and "\nEMORI_ADMIN_PIN=\n" in saved
    # A typed PIN is kept, and nothing is generated.
    monkeypatch.setattr(deploy, "ask",
                        lambda prompt, default="", secret=False:
                        "654321" if "PIN" in prompt else default)
    again = {}
    assert "\nEMORI_ADMIN_PIN=654321\n" in deploy._env_from_questions(generated=again)
    assert again == {}


def test_the_generated_pin_reaches_the_final_summary_only(deploy, monkeypatch, capsys,
                                                         tmp_path):
    monkeypatch.setattr(deploy, "ask", lambda prompt, default="", secret=False: default)
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(deploy, "LOCAL_ENV", tmp_path / "absent.env")
    robot = _TokenRobot()

    generated = deploy.ensure_env(robot, {"env": "no"}, None)

    pin = generated["EMORI_ADMIN_PIN"]
    (_remote, stdin), = robot.calls
    assert f"EMORI_ADMIN_PIN={pin}".encode() in stdin
    assert pin not in capsys.readouterr().out        # not while the deploy is still running
    assert deploy.ensure_env(robot, {"env": "yes"}, None) == {}
    source = _SCRIPT.read_text(encoding="utf-8")
    summary = source[source.index('say(VERDICTS.get(rc'):]
    assert 'generated.get("EMORI_ADMIN_PIN")' in summary


def test_fill_env_never_reintroduces_a_retired_key(deploy):
    text = deploy.fill_env("A=1\n", {"EMORI_PARTNER_SECRET": "x", "EMORI_ROBOT_SECRET": "y",
                                     "EMORI_PARTNER_NAME": "z", "EMORI_ADMIN_PIN": "123456"})

    assert text == "A=1\nEMORI_ADMIN_PIN=123456\n"


def test_the_default_server_is_the_settings_layers_default(deploy):
    from emori_adapter import site_settings

    assert deploy.DEFAULT_POKAMIND_BASE == site_settings.DEFAULT_POKAMIND_BASE
    # Not asked any more: the template carries the public default, and another origin
    # is set with --env (or on the admin page, with the PIN).
    assert {key: default for key, _prompt, default, _secret in deploy.ENV_QUESTIONS} \
        == {"EMORI_ADMIN_PIN": ""}
    template = (deploy.REPO / ".env.example").read_text(encoding="utf-8")
    assert f"\nPOKAMIND_BASE={site_settings.DEFAULT_POKAMIND_BASE}\n" in template.replace("\r", "")


def test_without_a_keyboard_the_env_takes_every_default_and_asks_nothing(deploy,
                                                                          monkeypatch,
                                                                          tmp_path):
    """A scripted deploy (CI, --yes, a pipe) with no robot.local.env and no --env still
    gives the robot a complete .env -- before anything is sent -- instead of stopping
    after the code was copied."""

    monkeypatch.setattr(deploy, "_has_keyboard", lambda: False)
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: pytest.fail("asked"))
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: pytest.fail("asked"))
    monkeypatch.setattr(deploy, "LOCAL_ENV", tmp_path / "robot.local.env")
    generated = {}

    text = deploy.prepare_env({"env": "no"}, None, generated=generated)

    pin = generated["EMORI_ADMIN_PIN"]
    assert re.fullmatch(r"[0-9]{6}", pin) and f"\nEMORI_ADMIN_PIN={pin}\n" in text
    assert not (tmp_path / "robot.local.env").exists()
    assert deploy.prepare_env({"env": "yes"}, None, generated={}) is None


def test_yes_takes_every_default_even_with_a_keyboard(deploy, monkeypatch, tmp_path):
    """--yes is a scripted deploy: it never stops at the PIN question, keyboard or not."""

    monkeypatch.setattr(deploy, "_has_keyboard", lambda: True)
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: pytest.fail("asked"))
    monkeypatch.setattr(deploy, "confirm", lambda *a, **k: pytest.fail("asked"))
    monkeypatch.setattr(deploy, "LOCAL_ENV", tmp_path / "robot.local.env")
    generated = {}

    text = deploy.prepare_env({"env": "no"}, None, generated=generated, ask_questions=False)

    pin = generated["EMORI_ADMIN_PIN"]
    assert re.fullmatch(r"[0-9]{6}", pin) and f"\nEMORI_ADMIN_PIN={pin}\n" in text


@pytest.mark.skipif(os.name != "nt", reason="the NUL-is-a-tty quirk is Windows only")
def test_a_nul_stdin_on_windows_is_no_keyboard(deploy, monkeypatch):
    """Windows calls NUL a tty; `< /dev/null` under Git Bash must not hang at getpass."""

    with open(os.devnull, encoding="utf-8") as nul:
        monkeypatch.setattr(sys, "stdin", nul)
        assert deploy._has_keyboard() is False


def test_the_hint_for_a_missing_keyboard_names_the_env_option(deploy):
    assert "--env" in deploy.NO_KEYBOARD_HINT


def test_the_readycheck_warnings_are_read_back_by_name_only(deploy):
    class _Robot:
        def run(self, remote, **_):
            assert "last-deploy-readycheck.env" in remote
            return subprocess.CompletedProcess(
                [], 0, b"rc=0\nwarn_gates=voice_menu,gaze,bad gate,x\n", b"")

    assert deploy.readycheck_warnings(_Robot()) == ["voice_menu", "gaze", "x"]

    class _Nothing:
        def run(self, remote, **_):
            return subprocess.CompletedProcess([], 0, b"", b"")

    assert deploy.readycheck_warnings(_Nothing()) == []


def test_an_empty_token_answer_points_at_the_admin_page_not_a_shared_secret(deploy,
                                                                           monkeypatch,
                                                                           capsys):
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: "")

    assert deploy.choose_token(_args(), {"token": "no"}, interactive=True) is None

    out = capsys.readouterr().out
    assert "admin page" in out and "shared secret" not in out


def test_a_shared_env_file_carrying_a_token_is_refused_before_anything_is_sent(deploy,
                                                                               tmp_path):
    shared = tmp_path / "robot.local.env"
    shared.write_text("POKAMIND_BASE=https://api.pokamind.com\n"
                      f"POKAMIND_ROBOT_TOKEN={GOLDEN_TOKEN}\n", encoding="utf-8")
    robot = _TokenRobot()

    with pytest.raises(deploy.DeployError, match="ONE robot"):
        deploy.ensure_env(robot, {"env": "no"}, shared)
    assert robot.calls == []
    # A commented or empty line is documentation, not a token.
    shared.write_text("#POKAMIND_ROBOT_TOKEN=\nPOKAMIND_ROBOT_TOKEN=\n", encoding="utf-8")
    deploy.ensure_env(robot, {"env": "no"}, shared)
    assert len(robot.calls) == 1


def test_the_preflight_reports_the_token_robot_and_emori_robot_id(deploy):
    info = deploy.preflight(_FakeRobot(_HEALTHY + "token=yes\ntoken_robot=robot-01\n"
                                       "env_robot_id=robot-01\n"))

    assert (info["token"], info["token_robot"], info["env_robot_id"]) == (
        "yes", "robot-01", "robot-01")
    # It may name the robot id the token is for, never print the token file itself.
    assert "cat " not in deploy.PREFLIGHT
    assert deploy.TOKEN_PEEK in deploy.PREFLIGHT
    assert "cut -s -d. -f2" in deploy.TOKEN_PEEK
    # No retired credential is looked at at all.
    assert not [key for key in deploy.RETIRED_ENV_KEYS if key in deploy.PREFLIGHT]


def test_the_preflight_says_when_the_token_file_is_damaged(deploy, capsys):
    info = deploy.preflight(_FakeRobot(_HEALTHY + "emori_dir=yes\ntoken=invalid\n"))

    assert info["token"] == "invalid"
    assert "DAMAGED" in capsys.readouterr().out


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


def _peek(deploy, tmp_path, content: str | None, env_text: str = "") -> dict[str, str]:
    bash = _bash()
    if bash is None:
        pytest.skip("no bash")
    (tmp_path / ".env").write_text(env_text, encoding="utf-8", newline="\n")
    if content is not None:
        (tmp_path / "secrets").mkdir(exist_ok=True)
        (tmp_path / "secrets" / "pokamind_robot_token").write_bytes(content.encode("ascii"))
    script = deploy.TOKEN_PEEK.replace("root=/opt/emori", f"root='{tmp_path.as_posix()}'")
    result = subprocess.run([bash, "-c", script], capture_output=True, timeout=60,
                            check=False)
    assert result.returncode == 0, result.stderr
    out = result.stdout.decode("ascii")
    assert SEED not in out and SEED not in result.stderr.decode("ascii", "replace")
    return deploy.parse_kv(out)


@pytest.mark.parametrize("content", [
    GOLDEN_TOKEN + "\n",
    GOLDEN_TOKEN + "\r\n",           # saved by a Windows editor
    "  " + GOLDEN_TOKEN + " \n\n",   # surrounding whitespace, as the robot forgives
])
def test_the_token_peek_names_the_robot_of_a_valid_token(deploy, tmp_path, content):
    assert _peek(deploy, tmp_path, content) == {"token": "yes",
                                                "token_robot": "test-robot-01"}


@pytest.mark.parametrize("content", [
    SEED + "\n",                                 # a hand-edited file holding just the seed
    "key " + SEED + "\n",
    GOLDEN_TOKEN + "\n" + SEED + "\n",           # two lines: the robot refuses it
    "pkr1.Test_Robot." + SEED + "\n",            # not the token grammar
])
def test_the_token_peek_never_echoes_a_damaged_file(deploy, tmp_path, content):
    assert _peek(deploy, tmp_path, content) == {"token": "invalid"}


def test_the_token_peek_reports_none_and_follows_the_configured_file(deploy, tmp_path):
    assert _peek(deploy, tmp_path, None) == {"token": "no"}
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "t").write_text(GOLDEN_TOKEN, encoding="ascii")
    assert _peek(deploy, tmp_path, None, "EMORI_POKAMIND_TOKEN_FILE=keys/t\n") == {
        "token": "yes", "token_robot": "test-robot-01"}


def test_a_damaged_token_file_is_asked_for_again_never_kept(deploy, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: GOLDEN_TOKEN)

    assert deploy.choose_token(_args(), {"token": "invalid"}, interactive=True) == GOLDEN_TOKEN
    assert deploy.choose_token(_args(), {"token": "invalid"}, interactive=False) is None
    assert "DAMAGED" in capsys.readouterr().out


def test_installing_a_token_says_nothing_about_a_retired_credential(deploy, monkeypatch,
                                                                     capsys):
    monkeypatch.setattr(deploy, "ask", lambda *a, **k: GOLDEN_TOKEN)

    assert deploy.choose_token(_args(), {"token": "no"}, interactive=True) == GOLDEN_TOKEN
    out = capsys.readouterr().out

    assert "token for robot 'test-robot-01'" in out
    assert "shared-secret" not in out and "secret" not in out.lower()


# The robot half of the token install (deploy/robot/install-token.sh) is run in
# tests/test_deploy_fleet.py, with Pokamind's answer stubbed.


# --- the robot half: what the System tab reads -----------------------------------------------


def test_the_robot_half_writes_the_deployed_commit_the_system_tab_reads():
    text = _ROBOT_HALF.read_text(encoding="utf-8")

    assert "> DEPLOYED_COMMIT.txt" in text
    assert '"$COMMIT" "$BRANCH"' in text
    # ...and the System tab reads exactly that file.
    system = (_ROOT / "src" / "emori_adapter" / "admin" / "system.py").read_text(
        encoding="utf-8")
    assert '"DEPLOYED_COMMIT.txt"' in system


def test_the_deployed_commit_is_written_before_anything_restarts():
    """emori-admin reads DEPLOYED_COMMIT.txt once, at start, to report which commit it
    runs (readycheck admin_stale). Written after the restart, a fresh emori-admin would
    report the previous deploy and be called stale."""

    text = _ROBOT_HALF.read_text(encoding="utf-8")
    write = text.index("> DEPLOYED_COMMIT.txt")

    assert text.count("> DEPLOYED_COMMIT.txt") == 1
    assert write < text.index("bash deploy/robot/install.sh")
    assert write < text.index("systemctl try-restart")


def test_a_deploy_records_the_commit_for_the_system_tab(rig):
    result = rig("--quick", "--commit", "abc123", "--branch", "master")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (rig.root / "DEPLOYED_COMMIT.txt").read_text() == "abc123 (master)\n"


# --- no keyboard: a sentence, never a traceback --------------------------------------


class _ClosedConsole:
    """A console that still says isatty() but whose input has ended (EOF)."""

    def isatty(self):
        return True

    def readline(self, *args):
        return ""


class _ClosedFile:
    def isatty(self):
        raise ValueError("I/O operation on closed file")


def test_a_closed_keyboard_stops_with_a_message_naming_host(deploy, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _ClosedConsole())
    monkeypatch.setattr(deploy, "_is_console", lambda stream: True)  # a real (Windows) console

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)

    with pytest.raises(deploy.DeployError) as stopped:
        deploy.ask("  type the robot's IP address or name (empty = give up)")

    message = str(stopped.value)
    assert "--host" in message and "keyboard input is closed" in message
    assert stopped.value.__cause__ is None and stopped.value.__suppress_context__


def test_a_closed_stdin_file_is_no_keyboard_not_a_crash(deploy, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _ClosedFile())

    with pytest.raises(deploy.DeployError, match="--host"):
        deploy.ask("  which one? (number)", "1")
    assert deploy._has_keyboard() is False


def test_main_ends_in_deploy_stopped_when_it_cannot_ask_for_the_robot(deploy, monkeypatch,
                                                                     capsys):
    """A double-click launch can have no usable keyboard: the window could not ask,
    the search found no robot, and the question for its address ended in an EOFError
    traceback. Now it is DEPLOY STOPPED with what to type instead."""
    monkeypatch.setattr(sys, "stdin", _ClosedConsole())
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))
    monkeypatch.setattr(deploy, "describe_checkout", lambda: {
        "commit": "0" * 40, "branch": "test", "subject": "s", "dirty": "0"})
    monkeypatch.setattr(deploy, "_load_state", lambda: {})
    monkeypatch.setattr(deploy, "_resolves", lambda host: False)
    monkeypatch.setattr(deploy, "local_ipv4s", lambda: [])
    monkeypatch.setattr(deploy, "scan", lambda networks, own: [])

    assert deploy.main(["--no-browser"]) == 1

    out = capsys.readouterr().out
    assert "DEPLOY STOPPED" in out and "--host" in out
    assert "Traceback" not in out


# --- optional add-ons in this checkout -------------------------------------------------


class _SshRobot:
    host = "reachy-mini.local"

    def ssh(self, **_):
        return ["ssh", "robot"]


def _record_ssh(monkeypatch, deploy, *, stdout: bytes = b"shipped=3\n"):
    calls = []

    def _run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    monkeypatch.setattr(deploy.subprocess, "run", _run)
    return calls


def _fixture_addon(repo: Path, name: str = "example") -> Path:
    addon = repo / "addons" / name
    (addon / "src" / name / "__pycache__").mkdir(parents=True)
    (addon / "tests").mkdir()
    (addon / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (addon / "src" / name / "__init__.py").write_text("", encoding="utf-8")
    (addon / "src" / name / "__pycache__" / "x.pyc").write_bytes(b"cache")
    (addon / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (addon / "run.sh").write_bytes(b"echo hi\r\n")
    return addon


def test_deploy_finds_and_ships_nothing_when_there_is_no_addons_folder(
        deploy, monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "REPO", tmp_path)
    calls = _record_ssh(monkeypatch, deploy)

    assert deploy.find_addons() == []
    deploy.ship_addons(_SshRobot())

    assert calls == []


def test_deploy_ignores_folders_under_addons_that_are_not_add_ons(deploy, monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "REPO", tmp_path)
    (tmp_path / "addons" / "notes").mkdir(parents=True)
    (tmp_path / "addons" / "stray.txt").write_text("x", encoding="utf-8")

    assert deploy.find_addons() == []


def test_deploy_ships_a_fixture_add_on_without_its_tests_and_caches(
        deploy, monkeypatch, tmp_path):
    import tarfile

    monkeypatch.setattr(deploy, "REPO", tmp_path)
    _fixture_addon(tmp_path)
    calls = _record_ssh(monkeypatch, deploy)

    assert [path.name for path in deploy.find_addons()] == ["example"]
    deploy.ship_addons(_SshRobot())

    assert len(calls) == 1
    argv, payload = calls[0]
    assert argv[:2] == ["ssh", "robot"] and "$D/example" in argv[-1] and "/addons" in argv[-1]
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = set(archive.getnames())
        assert archive.extractfile("run.sh").read() == b"echo hi\n"
    assert {"pyproject.toml", "src/example/__init__.py", "run.sh"} <= names
    assert not [name for name in names if "tests" in name or "__pycache__" in name]


def test_no_addons_flag_ships_the_core_only(deploy, monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "REPO", tmp_path)
    _fixture_addon(tmp_path)
    shipped = []
    monkeypatch.setattr(deploy, "ship_addons", lambda robot: shipped.append(robot))

    class _Archive:
        stdout = io.BytesIO()

        def wait(self):
            return 0

    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *a, **k: _Archive())
    monkeypatch.setattr(deploy.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(
        argv, 0, b"shipped=1\n", b""))

    deploy.ship(_SshRobot(), addons=False)
    assert shipped == []
    deploy.ship(_SshRobot())
    assert len(shipped) == 1


def test_remove_addons_asks_the_robot_to_uninstall_and_delete_them(deploy, monkeypatch):
    calls = _record_ssh(monkeypatch, deploy, stdout=b"removed=example\n")

    deploy.remove_addons(_SshRobot())

    assert len(calls) == 1
    remote = calls[0][0][-1]
    assert "uninstall -y" in remote and 'rm -rf "$D"' in remote and "/addons" in remote


def test_a_failed_add_on_removal_is_reported_and_does_not_stop_the_deploy(
        deploy, monkeypatch, capsys):
    _record_ssh(monkeypatch, deploy, stdout=b"failed=example\nremoved=example\n")

    deploy.remove_addons(_SshRobot())

    assert "not fully removed" in capsys.readouterr().out


@pytest.mark.skipif(_bash() is None, reason="needs a working bash")
def test_a_failed_uninstall_is_never_reported_as_removed_and_a_nameless_one_is_skipped(
        deploy, monkeypatch, tmp_path, capsys):
    """The remote command, RUN: pip refuses one package, and another project has no name."""

    root = tmp_path / "emori"
    for name, text in (("good", 'name = "good"\n'), ("stuck", 'name = "stuck"\n'),
                       ("nameless", "[project]\n")):
        (root / "addons" / name).mkdir(parents=True)
        (root / "addons" / name / "pyproject.toml").write_text(text, encoding="utf-8")
    pip = root / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text('#!/bin/sh\ncase "$*" in *stuck*) exit 1 ;; esac\nexit 0\n',
                   encoding="ascii", newline="\n")
    pip.chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    sudo = stubs / "sudo"
    sudo.write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding="ascii", newline="\n")
    sudo.chmod(0o755)
    monkeypatch.setattr(deploy, "REMOTE_ROOT", root.as_posix())
    real_run = subprocess.run
    seen = {}

    def _run(argv, **kwargs):
        seen["answer"] = real_run(
            [_bash(), "-c", argv[-1]], capture_output=True, check=False,
            env={**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"})
        return seen["answer"]

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    deploy.remove_addons(_SshRobot())

    lines = seen["answer"].stdout.decode().split()
    assert sorted(lines) == ["failed=stuck", "removed=good"]
    out = capsys.readouterr().out
    assert "not fully removed (stuck)" in out and "removed good" in out
    assert "settings they saved stay in" in out
    assert not (root / "addons").exists()


def test_quick_replaces_a_hostapp_unit_that_differs_from_the_shipped_one(rig, tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "emori-hostapp.service").write_text("ExecStartPre=/gone.sh\n")
    (rig.root / "deploy" / "robot" / "emori-hostapp.service").write_text("[Service]\n")

    result = rig("--quick", EMORI_SYSTEMD_DIR=str(units))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (units / "emori-hostapp.service").read_text() == "[Service]\n"
    lines = result.log.splitlines()
    assert lines.index("systemctl daemon-reload") < next(
        i for i, line in enumerate(lines) if "try-restart" in line)


def test_an_identical_or_absent_hostapp_unit_is_left_alone(rig, tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    shipped = rig.root / "deploy" / "robot" / "emori-hostapp.service"
    shipped.write_text("[Service]\n")

    assert "daemon-reload" not in rig("--quick", EMORI_SYSTEMD_DIR=str(units)).log
    assert not (units / "emori-hostapp.service").exists()

    (units / "emori-hostapp.service").write_text("[Service]\n")
    assert "daemon-reload" not in rig("--quick", EMORI_SYSTEMD_DIR=str(units)).log


def _quick_addon(rig, *, pip_body: str) -> None:
    addon = rig.root / "addons" / "example"
    addon.mkdir(parents=True)
    (addon / "pyproject.toml").write_text('[project]\nname = "example-addon"\n')
    pip = rig.root / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text('#!/bin/sh\necho "pip $*" >> "$STUB_LOG"\n' + pip_body)
    pip.chmod(0o755)


def test_quick_installs_an_add_on_the_venv_lacks_without_the_network(rig):
    _quick_addon(rig, pip_body='[ "$1" = show ] && exit 1\nexit 0\n')

    result = rig("--quick")

    assert "pip show -q example-addon" in result.log
    assert ("pip install --no-index --no-deps --no-build-isolation -q -e addons/example"
            in result.log)
    assert "installed add-on example" in result.stdout


def test_quick_says_loudly_when_an_add_on_cannot_be_installed(rig):
    _quick_addon(rig, pip_body="exit 1\n")

    result = rig("--quick")

    assert result.returncode == 0
    assert "ADD-ON example IS NOT INSTALLED" in result.stdout


def test_quick_leaves_an_installed_add_on_alone(rig):
    _quick_addon(rig, pip_body="exit 0\n")

    result = rig("--quick")

    assert "pip show" in result.log and "pip install" not in result.log


def test_quick_counts_an_add_ons_pyproject_as_a_dependency_change(rig):
    mark = rig.root / "artifacts" / "last-full-install.pyproject.sha256"
    assert rig(FAKE_ONLINE="1").returncode == 0
    assert mark.is_file()
    assert "pyproject.toml changed" not in rig("--quick").stdout

    _quick_addon(rig, pip_body="exit 0\n")

    assert "pyproject.toml changed" in rig("--quick").stdout
