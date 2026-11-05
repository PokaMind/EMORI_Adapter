"""Deploy-to-Robot for robot #2..N: what a fresh robot needed a human for.

Deploying fresh robots back to back must do what a human would do by hand: check the
robot clock, ask Pokamind before installing a token, tell robots apart by more than their
IP address, and deploy a robot from outside its own Wi-Fi.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from tests.test_deploy_to_robot import _HEALTHY, _FakeRobot

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "deploy_to_robot.py"
_MODULE_NAME = "deploy_to_robot_fleet_under_test"


@pytest.fixture(scope="module")
def deploy():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(_MODULE_NAME, None)


# --- the robot's clock ---------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("1791000000.123456789\n", (1791000000.123456789, 0.0)),
    ("1791000000.N\n", (1791000000.5, 0.5)),      # a date without %N support
    ("1791000000.%N", (1791000000.5, 0.5)),
    ("1791000000", (1791000000.5, 0.5)),
    ("Mon Oct 12 2026", None),
    ("", None),
])
def test_the_robots_date_output_is_read_with_its_resolution(deploy, text, expected):
    result = deploy.parse_epoch(text)

    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_the_skew_is_bracketed_by_the_ssh_round_trip(deploy):
    # The robot said 1000 somewhere between 990 and 992 on this computer: ~9 s ahead.
    skew, uncertainty = deploy.clock_skew((1000.0, 0.0), 990.0, 992.0)

    assert (skew, uncertainty) == (9.0, 1.0)


def test_the_preflight_prints_the_robot_clock_against_this_computer(deploy, capsys):
    info = deploy.preflight(_FakeRobot(_HEALTHY + "ntp_sync=yes\n", offset=0.0))

    out = capsys.readouterr().out
    assert info["ntp_sync"] == "yes"
    assert "robot clock:" in out and "NTP synchronized yes" in out
    assert "!!" not in out


def test_a_robot_clock_far_off_this_computers_is_a_loud_warning(deploy, capsys):
    deploy.preflight(_FakeRobot(_HEALTHY + "ntp_sync=no\n", offset=-3600.0))

    out = capsys.readouterr().out
    assert "behind this computer" in out
    assert "!! the robot's clock is 3600 s behind" in out
    assert "sudo timedatectl set-ntp true" in out and "UDP 123" in out


def test_an_unsynchronized_clock_that_is_still_right_is_warned_about(deploy, capsys):
    deploy.preflight(_FakeRobot(_HEALTHY + "ntp_sync=no\n", offset=0.0))

    out = capsys.readouterr().out
    assert "!! NTP has not synchronized the robot's clock" in out
    assert "3600" not in out


def test_a_robot_that_cannot_tell_the_time_does_not_stop_the_deploy(deploy, capsys):
    info = deploy.preflight(_FakeRobot(_HEALTHY, offset=None))

    assert info["hostname"] == "reachy-mini"
    assert "could not compare it with this computer" in capsys.readouterr().out


def test_the_preflight_reads_ntp_without_changing_anything(deploy):
    assert "timedatectl show -p NTPSynchronized --value" in deploy.PREFLIGHT
    assert "set-ntp" not in deploy.PREFLIGHT


# --- deploying from another network: --port and --jump --------------------------------


def test_every_ssh_takes_the_port_and_the_jump_host(deploy):
    robot = deploy.Robot("reachy-01", "pollen", port=2222, jump="me@bastion.example.org")

    for command in (robot.ssh(), robot.ssh(batch=False), robot.ssh(tty=True)):
        assert command[-1] == "pollen@reachy-01"
        assert command[-5:-1] == ["-p", "2222", "-J", "me@bastion.example.org"]
    # ...and nothing extra on the usual route.
    plain = deploy.Robot("192.0.2.20", "pollen", port=22).ssh()
    assert "-p" not in plain and "-J" not in plain


def test_the_code_stream_and_the_robot_half_go_the_same_way(deploy, monkeypatch):
    robot = deploy.Robot("reachy-01", "pollen", port=2222, jump="me@bastion")
    seen = []

    class _Archive:
        stdout = type("Out", (), {"close": lambda self: None})()

        def wait(self):
            return 0

    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *a, **k: _Archive())

    def run(argv, **kwargs):
        seen.append(argv)
        return deploy.subprocess.CompletedProcess(argv, 0, b"shipped=12\n", b"")

    monkeypatch.setattr(deploy.subprocess, "run", run)

    deploy.ship(robot, addons=False)
    deploy.install(robot, {"commit": "0" * 40, "branch": "b"}, quick=True)
    robot.run("true")

    assert len(seen) == 3
    for argv in seen:
        assert ["-p", "2222", "-J", "me@bastion", "pollen@reachy-01"] == argv[
            argv.index("-p"):argv.index("pollen@reachy-01") + 1]


def test_a_changed_host_key_is_forgotten_under_the_name_ssh_filed_it(deploy):
    assert deploy.Robot("h", "u", port=2222).known_hosts_name == "[h]:2222"
    assert deploy.Robot("h", "u", port=22).known_hosts_name == "h"
    assert deploy.Robot("h", "u").known_hosts_name == "h"


@pytest.mark.parametrize("jump", ["me@bastion", "bastion.example.org:2200",
                                  "a@hop1,b@hop2:22", "me@[fd7a:115c::1]"])
def test_a_jump_host_is_what_ssh_j_takes(deploy, jump):
    assert deploy.parse_jump(jump) == jump


@pytest.mark.parametrize("jump", ["-oProxyCommand=sh", "me@host;rm -rf ~", "me@-host",
                                  "-x@host", "me@host evil", "", "me@host:port"])
def test_anything_else_is_refused_before_it_reaches_ssh(deploy, jump):
    with pytest.raises(deploy.argparse.ArgumentTypeError):
        deploy.parse_jump(jump)


@pytest.mark.parametrize("port, ok", [("22", True), ("2222", True), ("0", False),
                                      ("65536", False), ("ssh", False)])
def test_the_port_is_a_port(deploy, port, ok):
    if ok:
        assert deploy.parse_port(port) == int(port)
    else:
        with pytest.raises(deploy.argparse.ArgumentTypeError):
            deploy.parse_port(port)


def test_a_remote_robot_needs_its_address(deploy, capsys):
    assert deploy.main(["--jump", "me@bastion", "--no-browser"]) == 1

    assert "give --host too" in capsys.readouterr().out


def test_a_remote_deploy_ends_with_a_tunnel_not_a_plain_http_link(deploy, monkeypatch,
                                                                  capsys):
    monkeypatch.setattr(deploy, "describe_checkout", lambda: {
        "commit": "0" * 40, "branch": "test", "subject": "s", "dirty": "0"})
    monkeypatch.setattr(deploy, "_load_state", lambda: {})
    monkeypatch.setattr(deploy, "_save_state", lambda state: None)
    monkeypatch.setattr(deploy, "_has_keyboard", lambda: False)
    routes = []
    monkeypatch.setattr(deploy, "connect", lambda robot: routes.append(robot.route()))
    monkeypatch.setattr(deploy, "preflight", lambda robot: {
        "env": "yes", "token": "yes", "token_robot": "robot-01", "internet": "yes"})
    monkeypatch.setattr(deploy, "ship", lambda robot, **k: None)
    monkeypatch.setattr(deploy, "ensure_env", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "install", lambda *a: 0)
    monkeypatch.setattr(deploy, "plan_v2_image", lambda *a, **k: {"action": "none", "why": ""})
    monkeypatch.setattr(deploy, "stack_outcome", lambda robot: "")
    # Reaching the hub has its own tests (tests/test_hub_payload.py).
    monkeypatch.setattr(deploy, "finish_on_hub", lambda robot: ("", True, False))
    opened = []
    monkeypatch.setattr(deploy.webbrowser, "open", lambda url: opened.append(url))

    assert deploy.main(["--host", "reachy-01", "--port", "2222",
                        "--jump", "me@bastion", "--yes"]) == 0

    out = capsys.readouterr().out
    assert routes == [["-p", "2222", "-J", "me@bastion"]]
    assert "-N -L 8070:127.0.0.1:8070 -L 8090:127.0.0.1:8090 pollen@reachy-01" in out
    assert "http://localhost:8070" in out
    assert "http://reachy-01:8070" not in out
    assert "-p 2222 -J me@bastion pollen@reachy-01" in out  # the shell line
    assert opened == []


# --- a changed host key through --jump ------------------------------------------------

_CHANGED = """@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!
Offending ED25519 key in {file}:7
  remove with:
  ssh-keygen -f "{file}" -R "{host}"
Host key for {host} has changed and you have requested strict checking.
Host key verification failed.
"""


def _connect(deploy, monkeypatch, robot, errors):
    """connect() against ssh answering ``errors`` in turn, then success; what it did."""

    answers = iter(errors)
    seen = {"keygen": [], "asked": []}

    def run(remote, **_):
        error = next(answers, None)
        if error is None:
            return deploy.subprocess.CompletedProcess([], 0, b"", b"")
        return deploy.subprocess.CompletedProcess([], 255, b"", error.encode())

    monkeypatch.setattr(robot, "run", run)
    monkeypatch.setattr(deploy.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(deploy, "confirm",
                        lambda prompt, default=True: seen["asked"].append(prompt) or True)
    monkeypatch.setattr(deploy.subprocess, "run",
                        lambda argv, **k: seen["keygen"].append(argv)
                        or deploy.subprocess.CompletedProcess(argv, 0, b"", b""))
    return seen


def test_a_jump_hosts_changed_key_never_removes_the_robots(deploy, monkeypatch, capsys):
    robot = deploy.Robot("reachy-01", "pollen", port=2222, jump="me@bastion.example.org")
    seen = _connect(deploy, monkeypatch, robot, [_CHANGED.format(
        file="/home/me/.ssh/known_hosts", host="bastion.example.org")])

    with pytest.raises(deploy.DeployError, match="on the way to the robot"):
        deploy.connect(robot)

    assert seen["keygen"] == [] and seen["asked"] == []
    out = capsys.readouterr().out
    assert "through the jump host me@bastion.example.org" in out
    assert "may be the JUMP HOST's key, not the robot's (bastion.example.org)" in out
    assert "presents a different SSH host key" not in out


def test_through_a_jump_an_unnamed_key_failure_is_not_blamed_on_the_robot(deploy,
                                                                         monkeypatch):
    robot = deploy.Robot("reachy-01", "pollen", jump="me@bastion")
    seen = _connect(deploy, monkeypatch, robot, ["Host key verification failed.\r\n"])

    with pytest.raises(deploy.DeployError):
        deploy.connect(robot)
    assert seen["keygen"] == []


def test_through_a_jump_the_robots_own_changed_key_is_still_offered(deploy, monkeypatch):
    robot = deploy.Robot("reachy-01", "pollen", port=2222, jump="me@bastion")
    seen = _connect(deploy, monkeypatch, robot, [_CHANGED.format(
        file="C:/Home/me/.ssh/emori_known_hosts", host="[reachy-01]:2222")])

    deploy.connect(robot)

    assert len(seen["asked"]) == 1
    (keygen,) = seen["keygen"]
    assert keygen[:3] == ["ssh-keygen", "-R", "[reachy-01]:2222"]


def test_on_the_robots_own_network_a_changed_key_is_offered_as_before(deploy, monkeypatch):
    robot = deploy.Robot("192.0.2.20", "pollen")
    seen = _connect(deploy, monkeypatch, robot, ["Host key verification failed.\n"])

    deploy.connect(robot)

    assert [argv[:3] for argv in seen["keygen"]] == [["ssh-keygen", "-R", "192.0.2.20"]]


@pytest.mark.parametrize("host", ["bastion.example.org", "[reachy-01]:2222"])
def test_the_host_an_ssh_key_complaint_is_about(deploy, host):
    assert deploy.host_key_hosts(_CHANGED.format(file="/k", host=host)) == {host}
    assert deploy.host_key_hosts("Host key verification failed.") == set()
