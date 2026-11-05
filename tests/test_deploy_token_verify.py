"""A Pokamind token is asked about ON the robot before it replaces the robot's own.

The admin page's Replace token always refused a token Pokamind would not confirm; the
deploy installed whatever it was given (``--token-file``, a paste, ``--replace-token``).
Now the deploy ships the candidate to a 0600 file and ``deploy/robot/install-token.sh``
asks the robot's own Python (``emori_adapter.hostapp.token_verify``, the same whoami)
before it moves it into place:

* verified -> installed; refused -> deleted, the old token stays, the deploy stops before
  anything is installed or restarted; Pokamind unreachable -> installed, loudly, on a
  robot with no token (a robot with no internet must still get its first token), and
  NEVER over a working token: that one stays, and the new one waits staged (exit 7);
* whenever a token replaces another, the old one is kept as ``<token file>.prev`` (0600);
* a first deploy has no venv before install.sh step 2, so the candidate is staged and
  install.sh asks once it has built one -- before the hotspot is named after the token's
  robot and before any unit starts with it.

The robot half is RUN here through bash, with the robot's Python stubbed.
"""

from __future__ import annotations

import base64
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import ClassVar

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "deploy_to_robot.py"
_INSTALL_TOKEN = _ROOT / "deploy" / "robot" / "install-token.sh"
_INSTALL = _ROOT / "deploy" / "robot" / "install.sh"
_ONE_CLICK = _ROOT / "deploy" / "robot" / "one-click-deploy.sh"
_MODULE_NAME = "deploy_to_robot_token_verify_under_test"

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
SEED = GOLDEN_TOKEN.rsplit(".", 1)[1]
# The token the robot already has: another key for the same robot, built here rather
# than written out (the golden vector is the one full token the repository holds).
OLD_TOKEN = "pkr1.test-robot-01." + base64.urlsafe_b64encode(
    bytes(range(32))[::-1]).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def deploy():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(_MODULE_NAME, None)


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


# The robot's Python, stubbed: `-c import ...` says the verifier is importable, and the
# verifier itself answers what the test asks for and records how it was called.
_FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-c" ]; then exit 0; fi
echo "$*" >> "$FAKE_LOG"
case "$FAKE_RC" in
  0) echo verdict=verified ;;
  2|4) echo verdict=refused ;;
  *) echo verdict=unverified ;;
esac
echo "code=$FAKE_CODE"
echo "robot_id=test-robot-01"
if [ "$FAKE_RC" = 0 ]; then echo "org=emori-robot"; fi
echo "reason=stubbed answer"
exit "$FAKE_RC"
"""


class _Robot:
    """A fake /opt/emori with install-token.sh in it, run through bash."""

    def __init__(self, tmp_path, env_text="EMORI_ROBOT_ID=\n", token=None):
        bash = _bash()
        if bash is None:
            pytest.skip("no bash")
        self.bash = bash
        self.root = tmp_path / "emori"
        (self.root / "deploy" / "robot").mkdir(parents=True)
        shutil.copy2(_INSTALL_TOKEN, self.root / "deploy" / "robot" / "install-token.sh")
        (self.root / ".env").write_text(env_text, encoding="utf-8", newline="\n")
        self.secrets = self.root / "secrets"
        self.token_file = self.secrets / "pokamind_robot_token"
        self.candidate = self.secrets / ".pokamind_robot_token.candidate"
        if token is not None:
            self.secrets.mkdir()
            self.token_file.write_text(token + "\n", encoding="ascii", newline="\n")
            # Installed long before anything this test stages.
            os.utime(self.token_file, (time.time() - 3600,) * 2)
        self.python = tmp_path / "fake-python"
        self.python.write_text(_FAKE_PYTHON, encoding="ascii", newline="\n")
        self.python.chmod(0o755)
        self.log = tmp_path / "verify.log"

    def run(self, *args, stdin=None, rc=0, code="ok", python=True):
        env = {**os.environ, "FAKE_LOG": self.log.as_posix(), "FAKE_RC": str(rc),
               "FAKE_CODE": code,
               "EMORI_TOKEN_VERIFY_PYTHON": (self.python.as_posix() if python
                                             else "/nonexistent/python")}
        result = subprocess.run(
            [self.bash, (self.root / "deploy" / "robot" / "install-token.sh").as_posix(),
             *args], input=stdin, capture_output=True, env=env, timeout=60, check=False)
        out = result.stdout.decode("utf-8", "replace")
        err = result.stderr.decode("utf-8", "replace")
        assert SEED not in out and SEED not in err
        result.kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        result.err = err
        return result

    def verdict_file(self) -> str:
        return (self.root / "artifacts" / "last-token-verdict.env").read_text(encoding="utf-8")

    def env(self) -> str:
        return (self.root / ".env").read_text(encoding="utf-8")


def _token(text: str = GOLDEN_TOKEN) -> bytes:
    return (text + "\n").encode("ascii")


# --- the robot half -------------------------------------------------------------------


def test_a_verified_token_is_installed_private_and_names_the_robot(tmp_path):
    robot = _Robot(tmp_path)

    result = robot.run("--stdin", stdin=_token())

    assert result.returncode == 0, result.err
    assert result.kv["token_verdict"] == "verified"
    assert result.kv["token_installed"] == "test-robot-01"
    assert result.kv["token_org"] == "emori-robot"
    assert robot.token_file.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    assert not robot.candidate.exists()
    assert "EMORI_ROBOT_ID=test-robot-01\n" in robot.env()
    # The robot's Python was asked about the CANDIDATE, never about the installed file.
    assert "--token-file secrets/.pokamind_robot_token.candidate" in robot.log.read_text()
    assert "verdict=verified" in robot.verdict_file() and SEED not in robot.verdict_file()
    if os.name == "posix":
        assert stat.S_IMODE(robot.token_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(robot.secrets.stat().st_mode) == 0o700


@pytest.mark.parametrize("code", ["robot_unknown", "robot_revoked", "whoami_auth_failed",
                                  "robot_id_mismatch"])
def test_a_refused_token_never_replaces_the_working_one(tmp_path, code):
    robot = _Robot(tmp_path, env_text="EMORI_ROBOT_ID=test-robot-01\n", token=OLD_TOKEN)

    result = robot.run("--stdin", stdin=_token(), rc=2, code=code)

    assert result.returncode == 5
    assert result.kv["token_verdict"] == "refused" and result.kv["token_code"] == code
    assert "REFUSED" in result.err and "NOT installed" in result.err
    assert robot.token_file.read_text(encoding="ascii") == OLD_TOKEN + "\n"
    assert sorted(p.name for p in robot.secrets.iterdir()) == ["pokamind_robot_token"]
    assert "verdict=refused" in robot.verdict_file()


def test_pokamind_unreachable_still_installs_a_first_token_loudly(tmp_path):
    robot = _Robot(tmp_path)

    result = robot.run("--stdin", stdin=_token(), rc=3, code="whoami_unreachable")

    assert result.returncode == 0
    assert result.kv["token_verdict"] == "unverified"
    assert "WITHOUT Pokamind's confirmation" in result.err
    assert robot.token_file.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"


@pytest.mark.parametrize("code", ["whoami_timeout", "whoami_server_error",
                                  "whoami_rate_limited", "whoami_unavailable",
                                  "whoami_unreachable"])
def test_an_unverified_token_never_replaces_a_working_one(tmp_path, code):
    """A Pokamind hiccup (timeout, 5xx, 429) must not swap a working credential for one
    nobody has checked: the old token stays, the new one waits staged."""

    robot = _Robot(tmp_path, env_text="EMORI_ROBOT_ID=test-robot-01\n", token=OLD_TOKEN)

    result = robot.run("--stdin", stdin=_token(), rc=3, code=code)

    assert result.returncode == 7, result.err
    assert result.kv["token_verdict"] == "unverified" and result.kv["token_code"] == code
    assert (result.kv["token_staged"], result.kv["token_kept"]) == ("test-robot-01",) * 2
    assert "token_installed" not in result.kv
    assert "KEEPS its working token" in result.err and "Replace token" in result.err
    assert robot.token_file.read_text(encoding="ascii") == OLD_TOKEN + "\n"
    assert robot.candidate.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    assert not (robot.secrets / "pokamind_robot_token.prev").exists()
    assert "verdict=staged" in robot.verdict_file()
    if os.name == "posix":
        assert stat.S_IMODE(robot.candidate.stat().st_mode) == 0o600


def test_a_kept_back_token_is_installed_once_pokamind_confirms_it(tmp_path):
    robot = _Robot(tmp_path, env_text="EMORI_ROBOT_ID=test-robot-01\n", token=OLD_TOKEN)
    assert robot.run("--stdin", stdin=_token(), rc=3, code="whoami_timeout").returncode == 7
    assert robot.run(rc=3, code="whoami_timeout").returncode == 7  # install.sh, still down

    installed = robot.run()  # the next deploy: Pokamind answers

    assert installed.returncode == 0, installed.err
    assert robot.token_file.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    assert not robot.candidate.exists()
    # ...and the token it replaced is kept for a roll-back, private.
    previous = robot.secrets / "pokamind_robot_token.prev"
    assert previous.read_text(encoding="ascii") == OLD_TOKEN + "\n"
    assert installed.kv["token_previous"] == "secrets/pokamind_robot_token.prev"
    if os.name == "posix":
        assert stat.S_IMODE(previous.stat().st_mode) == 0o600


def test_every_replacement_keeps_the_replaced_token_as_prev(tmp_path):
    robot = _Robot(tmp_path, env_text="EMORI_ROBOT_ID=test-robot-01\n", token=OLD_TOKEN)

    result = robot.run("--stdin", stdin=_token())

    assert result.returncode == 0, result.err
    assert robot.token_file.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    assert (robot.secrets / "pokamind_robot_token.prev").read_text(
        encoding="ascii") == OLD_TOKEN + "\n"
    assert sorted(p.name for p in robot.secrets.iterdir()) == [
        "pokamind_robot_token", "pokamind_robot_token.prev"]
    # The same token again replaces nothing, and keeps the real previous one.
    assert robot.run("--stdin", stdin=_token()).returncode == 0
    assert (robot.secrets / "pokamind_robot_token.prev").read_text(
        encoding="ascii") == OLD_TOKEN + "\n"


def test_a_damaged_token_file_is_no_working_token_to_keep(tmp_path):
    robot = _Robot(tmp_path, token="pkr1.not-a-token")

    result = robot.run("--stdin", stdin=_token(), rc=3, code="whoami_unreachable")

    assert result.returncode == 0, result.err
    assert robot.token_file.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    assert "WITHOUT Pokamind's confirmation" in result.err


def test_a_token_installed_after_one_was_staged_wins(tmp_path):
    """The admin page's Replace token (asked live) is a newer decision than a token the
    deploy left staged; the next install must not put the staged one over it."""

    robot = _Robot(tmp_path, env_text="EMORI_ROBOT_ID=test-robot-01\n", token=OLD_TOKEN)
    assert robot.run("--stdin", stdin=_token(), rc=3, code="whoami_timeout").returncode == 7
    newer = OLD_TOKEN[:-4] + "AAAA"
    robot.token_file.write_text(newer + "\n", encoding="ascii")
    os.utime(robot.token_file, (time.time() + 60,) * 2)

    result = robot.run()

    assert result.returncode == 4
    assert result.kv["token_verdict"] == "superseded"
    assert robot.token_file.read_text(encoding="ascii") == newer + "\n"
    assert not robot.candidate.exists()
    assert not robot.log.exists() or robot.log.read_text().count("--token-file") == 1
    assert "verdict=superseded" in robot.verdict_file()


def test_install_sh_says_a_kept_back_token_stays_staged():
    text = _INSTALL.read_text(encoding="utf-8")
    block = text[text.index('echo "== [2b/10] staged Pokamind token =="'):
                 text.index("# 3. Voice + STT models")]

    assert '7) echo "  !! Pokamind could not be asked about the staged token: NOT installed' \
        in block


def test_a_robot_without_python_yet_keeps_the_token_staged_for_install_sh(tmp_path):
    robot = _Robot(tmp_path)

    staged = robot.run("--stdin", "--defer", "--allow-robot-id-change", stdin=_token(),
                       python=False)

    assert staged.returncode == 6
    assert staged.kv["token_verdict"] == "deferred"
    assert not robot.token_file.exists()
    assert robot.candidate.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    if os.name == "posix":
        assert stat.S_IMODE(robot.candidate.stat().st_mode) == 0o600
    # install.sh, once the venv exists: the staged token is asked about and installed,
    # and the human's "install it anyway" travelled with it.
    installed = robot.run()
    assert installed.returncode == 0, installed.err
    assert robot.token_file.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    assert not robot.candidate.exists()
    assert "--allow-robot-id-change" in robot.log.read_text()
    assert not list(robot.secrets.glob("*.allow-robot-id-change"))


def test_without_defer_a_python_less_robot_installs_unverified(tmp_path):
    robot = _Robot(tmp_path)

    result = robot.run("--stdin", stdin=_token(), python=False)

    assert result.returncode == 0
    assert (result.kv["token_verdict"], result.kv["token_code"]) == (
        "unverified", "verifier_unavailable")


def test_nothing_staged_is_silent_and_garbage_is_never_staged(tmp_path):
    robot = _Robot(tmp_path)

    nothing = robot.run()
    assert (nothing.returncode, nothing.stdout) == (4, b"")
    assert robot.run("--stdin", stdin=b"not a token\n").returncode == 3
    assert list(robot.secrets.iterdir()) == []
    assert robot.run("--stdin", stdin=_token() + _token()).returncode == 3
    assert list(robot.secrets.iterdir()) == []


def test_an_operators_robot_id_is_never_overwritten(tmp_path):
    robot = _Robot(tmp_path, env_text="EMORI_ROBOT_ID=robot-01\nPOKAMIND_BASE=x")

    result = robot.run("--stdin", "--allow-robot-id-change", stdin=_token())

    assert result.returncode == 0
    assert "robot_id_set" not in result.kv
    assert robot.env().startswith("EMORI_ROBOT_ID=robot-01\n")


def test_a_robot_id_is_appended_to_an_env_without_a_final_newline(tmp_path):
    robot = _Robot(tmp_path, env_text="POKAMIND_BASE=x")

    assert robot.run("--stdin", stdin=_token()).returncode == 0
    assert robot.env() == "POKAMIND_BASE=x\nEMORI_ROBOT_ID=test-robot-01\n"


def test_the_staged_token_is_asked_about_after_the_venv_and_before_the_hotspot_and_units():
    text = _INSTALL.read_text(encoding="utf-8")
    staged = text.index("bash deploy/robot/install-token.sh")

    assert text.index('.venv/bin/pip install -e ".[') < staged
    assert staged < text.index("env_ensure EMORI_AP_SSID")
    assert staged < text.index("== [8b/10] Pokamind robot token ==")
    assert staged < text.index("enable --now emori-hostapp")


def test_a_quick_deploy_asks_about_a_staged_token_before_the_restart():
    text = _ONE_CLICK.read_text(encoding="utf-8")

    assert text.index("bash deploy/robot/install-token.sh") < text.index(
        "sudo -n systemctl try-restart emori-hostapp emori-admin")


def test_the_script_is_lf_bash():
    raw = _INSTALL_TOKEN.read_bytes()

    assert raw.startswith(b"#!/usr/bin/env bash\n") and b"\r\n" not in raw
    bash = _bash()
    if bash:
        assert subprocess.run([bash, "-n", str(_INSTALL_TOKEN)], check=False).returncode == 0


# --- the computer half ----------------------------------------------------------------


class _TokenRobot:
    def __init__(self, returncode=0, stdout=b""):
        self.calls = []
        self._result = subprocess.CompletedProcess([], returncode, stdout, b"refused")

    def run(self, remote, *, stdin=None, **_):
        self.calls.append((remote, stdin))
        return self._result


def test_a_verified_token_is_reported_with_its_org(deploy, capsys):
    robot = _TokenRobot(stdout=b"token_robot=test-robot-01\ntoken_verdict=verified\n"
                               b"token_code=ok\ntoken_org=emori-robot\n"
                               b"token_installed=test-robot-01\n")

    assert deploy.install_token(robot, GOLDEN_TOKEN)["token_verdict"] == "verified"
    printed = capsys.readouterr().out
    assert "Pokamind knows robot 'test-robot-01', org emori-robot" in printed
    assert SEED not in printed


def test_an_unanswered_question_is_a_loud_warning_not_a_stop(deploy, capsys):
    robot = _TokenRobot(stdout=b"token_verdict=unverified\ntoken_code=whoami_unreachable\n"
                               b"token_installed=test-robot-01\n")

    deploy.install_token(robot, GOLDEN_TOKEN)

    printed = capsys.readouterr().out
    assert "!! Pokamind could not be asked about this token (whoami_unreachable)" in printed
    assert "Test connection" in printed


def test_a_refusal_stops_the_deploy_and_says_the_old_token_stays(deploy):
    robot = _TokenRobot(returncode=5, stdout=b"token_verdict=refused\n"
                                             b"token_code=robot_revoked\n")

    with pytest.raises(deploy.DeployError) as stopped:
        deploy.install_token(robot, GOLDEN_TOKEN, previous="test-robot-01")

    message = str(stopped.value)
    assert "REFUSED" in message and "robot_revoked" in message
    assert "keeps its token for 'test-robot-01'" in message
    assert "nothing was installed or restarted" in message
    assert SEED not in message


def test_a_token_kept_back_is_loud_but_does_not_stop_the_deploy(deploy, capsys):
    robot = _TokenRobot(returncode=7, stdout=b"token_robot=test-robot-01\n"
                                             b"token_verdict=unverified\n"
                                             b"token_code=whoami_timeout\n"
                                             b"token_staged=test-robot-01\n"
                                             b"token_kept=test-robot-01\n")

    out = deploy.install_token(robot, GOLDEN_TOKEN, previous="test-robot-01")

    assert out["token_staged"] == "test-robot-01"
    printed = capsys.readouterr().out
    assert "!! Pokamind could not be asked about the new token" in printed
    assert "it was NOT installed" in printed
    assert "KEEPS its working token for 'test-robot-01'" in printed
    assert "Replace token" in printed and SEED not in printed
    for verdict, expected in (("staged", "NOT installed - Pokamind could not be asked"),
                              ("superseded", "was dropped - a newer token")):
        line = deploy.staged_token_outcome(_TokenRobot(stdout=(
            f"verdict={verdict}\ncode=whoami_timeout\nrobot=test-robot-01\n").encode()))
        assert expected in line, line


def test_main_reports_what_install_sh_did_with_a_kept_back_token(deploy, monkeypatch,
                                                                tmp_path, capsys):
    path = tmp_path / "token.txt"
    path.write_text(GOLDEN_TOKEN, encoding="ascii")
    order = _main_with(deploy, monkeypatch, token_rc=7,
                       token_out=b"token_verdict=unverified\ntoken_code=whoami_timeout\n"
                                 b"token_staged=test-robot-01\ntoken_kept=test-robot-01\n")
    monkeypatch.setattr(deploy, "staged_token_outcome",
                        lambda robot: order.append("outcome") or "Pokamind token: verified")

    assert deploy.main(["--host", "robot", "--replace-token", "--token-file", str(path),
                        "--no-browser", "--yes"]) == 0
    assert order[-4:] == ["token", "install", "outcome", "warnings"]


def test_a_human_yes_to_another_robot_id_travels_to_the_robot(deploy):
    robot = _TokenRobot(stdout=b"token_verdict=unverified\ntoken_code=whoami_unreachable\n")

    deploy.install_token(robot, GOLDEN_TOKEN, allow_robot_id_change=True)

    (remote, _stdin), = robot.calls
    assert remote.endswith("--stdin --defer --allow-robot-id-change")


def test_a_deferred_token_is_reported_after_the_install(deploy, capsys):
    robot = _TokenRobot(returncode=6, stdout=b"token_verdict=deferred\n")

    assert deploy.install_token(robot, GOLDEN_TOKEN)["token_verdict"] == "deferred"
    assert "staged on the robot" in capsys.readouterr().out

    for verdict, expected in (("verified", "verified and installed"),
                              ("refused", "REFUSED by Pokamind (robot_unknown)"),
                              ("unverified", "WITHOUT Pokamind's confirmation"),
                              ("", "still staged")):
        answer = (f"verdict={verdict}\ncode=robot_unknown\nrobot=test-robot-01\n"
                  if verdict else "")
        line = deploy.staged_token_outcome(_TokenRobot(stdout=answer.encode()))
        assert expected in line, line


def test_without_a_keyboard_a_token_for_another_robot_says_it_needs_a_human(deploy, tmp_path,
                                                                            capsys):
    path = tmp_path / "token.txt"
    path.write_text(GOLDEN_TOKEN, encoding="ascii")
    args = type("Args", (), {"token_file": path, "replace_token": True, "no_token": False})()

    with pytest.raises(deploy.DeployError, match="robot id mismatch"):
        deploy.choose_token(args, {"token": "yes", "token_robot": "ward-3"}, interactive=False)
    assert "needs a human yes at a keyboard" in capsys.readouterr().out


def _main_with(deploy, monkeypatch, *, token_rc, token_out):
    """main() against a robot whose every step is recorded, not run."""

    order = []
    monkeypatch.setattr(deploy, "describe_checkout", lambda: {
        "commit": "0" * 40, "branch": "test", "subject": "s", "dirty": "0"})
    monkeypatch.setattr(deploy, "_load_state", lambda: {})
    monkeypatch.setattr(deploy, "_save_state", lambda state: None)
    monkeypatch.setattr(deploy, "connect", lambda robot: order.append("connect"))
    monkeypatch.setattr(deploy, "preflight", lambda robot: {
        "env": "yes", "token": "yes", "token_robot": "test-robot-01",
        "env_robot_id": "test-robot-01", "internet": "yes"})
    monkeypatch.setattr(deploy, "_has_keyboard", lambda: False)
    monkeypatch.setattr(deploy, "ship", lambda robot, **k: order.append("ship"))
    # Every question first (prepare_env), the .env only after the code (upload_env).
    monkeypatch.setattr(deploy, "prepare_env", lambda *a, **k: order.append("questions"))
    monkeypatch.setattr(deploy, "upload_env", lambda *a, **k: order.append("env"))
    monkeypatch.setattr(deploy, "install", lambda *a: order.append("install") or 0)
    monkeypatch.setattr(deploy, "readycheck_warnings",
                        lambda robot: order.append("warnings") or [])
    # The v2 image is planned and shipped by its own tests (tests/test_v2_stack.py).
    monkeypatch.setattr(deploy, "plan_v2_image",
                        lambda *a, **k: {"action": "none", "why": ""})
    monkeypatch.setattr(deploy, "stack_outcome", lambda robot: "")
    # Reaching the hub has its own tests (tests/test_hub_payload.py).
    monkeypatch.setattr(deploy, "finish_on_hub", lambda robot: ("", True, False))

    def run(self, remote, *, stdin=None, **_):
        order.append("token" if "install-token.sh" in remote else remote)
        return subprocess.CompletedProcess([], token_rc, token_out, b"")

    monkeypatch.setattr(deploy.Robot, "run", run)
    return order


def test_main_verifies_the_token_after_the_code_and_before_the_install(deploy, monkeypatch,
                                                                       tmp_path):
    path = tmp_path / "token.txt"
    path.write_text(GOLDEN_TOKEN, encoding="ascii")
    order = _main_with(deploy, monkeypatch, token_rc=0,
                       token_out=b"token_verdict=verified\ntoken_installed=test-robot-01\n")

    assert deploy.main(["--host", "robot", "--replace-token", "--token-file", str(path),
                        "--no-browser", "--yes"]) == 0
    assert order == ["connect", "questions", "ship", "env", "token", "install", "warnings"]


def test_main_with_a_refused_token_installs_and_restarts_nothing(deploy, monkeypatch,
                                                                 tmp_path, capsys):
    path = tmp_path / "token.txt"
    path.write_text(GOLDEN_TOKEN, encoding="ascii")
    order = _main_with(deploy, monkeypatch, token_rc=5,
                       token_out=b"token_verdict=refused\ntoken_code=whoami_key_mismatch\n")

    assert deploy.main(["--host", "robot", "--replace-token", "--token-file", str(path),
                        "--no-browser", "--yes"]) == 1
    assert "install" not in order
    out = capsys.readouterr().out
    assert "DEPLOY STOPPED" in out and "whoami_key_mismatch" in out and SEED not in out


def test_main_reports_what_install_sh_did_with_a_staged_token(deploy, monkeypatch, tmp_path,
                                                              capsys):
    path = tmp_path / "token.txt"
    path.write_text(GOLDEN_TOKEN, encoding="ascii")
    order = _main_with(deploy, monkeypatch, token_rc=6, token_out=b"token_verdict=deferred\n")
    monkeypatch.setattr(deploy, "staged_token_outcome",
                        lambda robot: order.append("outcome") or "Pokamind token: verified")

    assert deploy.main(["--host", "robot", "--replace-token", "--token-file", str(path),
                        "--no-browser", "--yes"]) == 0
    assert order[-3:] == ["install", "outcome", "warnings"]
    assert "Pokamind token: verified" in capsys.readouterr().out


# --- end to end: install-token.sh -> the REAL verifier -> a local stand-in Pokamind --------


class _Whoami(http.server.BaseHTTPRequestHandler):
    status = 200
    body: ClassVar[dict] = {}
    seen: ClassVar[list] = []

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Authorization", "")[:7],
                          self.headers.get("X-EMORI-Contract")))
        payload = json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("status, body, rc, verdict, installed", [
    (200, None, 0, "verified", GOLDEN_TOKEN),
    (401, {"error_code": "robot_revoked"}, 5, "refused", OLD_TOKEN),
    # Pokamind could not answer: the robot keeps the working token it has.
    (503, {"error_code": "robot_control_unavailable"}, 7, "unverified", OLD_TOKEN),
], ids=["verified", "refused", "unverified"])  # never a token in a test id
def test_the_real_verifier_asks_pokamind_with_the_candidate(tmp_path, status, body, rc,
                                                            verdict, installed):
    """The shell script and the Python verifier agree on arguments, output and exit
    codes: one real whoami, signed with the candidate, against a loopback stand-in.

    install-token.sh always passes --write-scope: a VERIFIED token also leaves this
    robot's Pokamind scope behind (configs/site/pokamind_scope.json), before the token
    is moved into place; any other verdict writes nothing."""

    from emori_adapter import site_settings
    from tests.test_readycheck import whoami

    # The wire body: what to_dict() reports minus the robot's own parse notes.
    wire = {k: v for k, v in whoami().whoami.to_dict().items() if k != "ignored_keys"}
    answer = wire if body is None else body
    handler = type("Whoami", (_Whoami,), {"status": status, "body": answer, "seen": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    robot = _Robot(tmp_path, token=OLD_TOKEN)
    # The robot's Python, pointed at THIS fake install root: the scope must land in it,
    # never in the checkout the test runs from.
    python = tmp_path / "robot-python"
    python.write_text('#!/bin/sh\nexec "%s" "$@" --root "%s"\n' % (
        Path(sys.executable).as_posix(), robot.root.as_posix()), encoding="ascii",
        newline="\n")
    python.chmod(0o755)
    checkout_scope = _ROOT / site_settings.SCOPE_FILE
    checkout_before = checkout_scope.read_bytes() if checkout_scope.exists() else None
    try:
        env = {**os.environ,
               "EMORI_TOKEN_VERIFY_PYTHON": python.as_posix(),
               "PYTHONPATH": str(_ROOT / "src") + os.pathsep + str(_ROOT),
               # Pinned in the environment, which a .env never overrides: the verifier
               # can only ever reach the stand-in.
               "POKAMIND_BASE": f"http://127.0.0.1:{server.server_address[1]}",
               "EMORI_ROBOT_ID": ""}
        result = subprocess.run(
            [robot.bash, (robot.root / "deploy" / "robot" / "install-token.sh").as_posix(),
             "--stdin"], input=_token(), capture_output=True, env=env, timeout=120,
            check=False)
    finally:
        server.shutdown()
        server.server_close()
    out = result.stdout.decode("utf-8", "replace")
    assert SEED not in out and SEED not in result.stderr.decode("utf-8", "replace")
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    assert result.returncode == rc, result.stderr
    assert kv["token_verdict"] == verdict
    assert handler.seen == [("/api/robot/whoami", "Bearer ", "2")]
    assert robot.token_file.read_text(encoding="ascii") == installed + "\n"
    scope = robot.root / site_settings.SCOPE_FILE
    if verdict == "verified":
        snapshot = json.loads(scope.read_text(encoding="utf-8"))
        assert (snapshot["robot_id"], snapshot["whoami"]["org"]) == (
            "test-robot-01", "example-org")
        # Relayed to the deploy: the scope reached the disk before the token did.
        assert kv["token_scope"] == "written"
        verdict_file = (robot.root / "artifacts" / "last-token-verdict.env").read_text()
        assert "scope=written" in verdict_file.splitlines()
        if os.name == "posix":
            assert stat.S_IMODE(scope.stat().st_mode) == 0o600
    else:
        assert not scope.exists()
    assert (checkout_scope.read_bytes() if checkout_scope.exists() else None) == (
        checkout_before)


def test_install_token_sh_asks_the_verifier_to_write_the_scope():
    text = _INSTALL_TOKEN.read_text(encoding="utf-8")

    assert '-m emori_adapter.hostapp.token_verify --token-file "$CAND" --write-scope' in text


def test_a_new_candidate_clears_the_verdict_about_the_last_one(tmp_path):
    """The deploy reads the verdict after install.sh; an old one must not answer for a
    token install.sh never reached."""

    robot = _Robot(tmp_path)
    assert robot.run("--stdin", stdin=_token(), rc=2, code="robot_unknown").returncode == 5
    assert "verdict=refused" in robot.verdict_file()

    assert robot.run("--stdin", "--defer", stdin=_token(), python=False).returncode == 6

    assert not (robot.root / "artifacts" / "last-token-verdict.env").exists()


def test_a_scope_that_could_not_be_written_is_said_on_the_last_screen(deploy, capsys):
    verified = (b"token_verdict=verified\ntoken_code=ok\ntoken_installed=test-robot-01\n"
                b"token_scope=failed\n")
    deploy.install_token(_TokenRobot(stdout=verified), GOLDEN_TOKEN)
    assert "could not be written" in capsys.readouterr().out

    line = deploy.staged_token_outcome(_TokenRobot(
        stdout=b"verdict=verified\ncode=ok\nrobot=test-robot-01\nscope=failed\n"))
    assert "verified and installed" in line and "Sync now" in line
    plain = deploy.staged_token_outcome(_TokenRobot(
        stdout=b"verdict=verified\ncode=ok\nrobot=test-robot-01\nscope=written\n"))
    assert plain == "Pokamind token: verified and installed for robot 'test-robot-01'"
