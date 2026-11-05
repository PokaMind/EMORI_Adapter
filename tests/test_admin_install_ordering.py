"""Static contracts on the admin-service part of ``deploy/robot/install.sh``.

Ordering inside an installer is normally not a testable property, and normally
it does not matter.  Here it decides whether the feature works at all: systemd
reads ``EnvironmentFile`` once, at unit start, ``enable --now`` is a no-op on an
already-running unit, and nothing in either process re-reads ``.env`` — so a
secret generated AFTER ``systemctl enable --now emori-hostapp`` is a secret the
hostapp does not have.  That is exactly what shipped: every fresh install came
up with a tokenless hostapp, the phone's Language tab answered "hostapp admin
disabled" for ever, and the one restart the ops guide named was the other unit.

The install itself cannot be exercised here (it is root, apt and systemd on an
aarch64 CM4), so these are text-level contracts on the two properties that
silently broke: WHEN the secrets are written, and that appending them cannot
corrupt what is already in ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "robot" / "install.sh"


def _text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def _at(needle: str) -> int:
    text = _text()
    index = text.find(needle)
    assert index >= 0, "install.sh no longer contains %r" % needle
    return index


def test_the_admin_secrets_are_written_before_any_unit_is_started():
    """Both processes read EMORI_ADMIN_TOKEN from the env systemd gave them."""

    token = _at("env_ensure EMORI_ADMIN_TOKEN")
    salt = _at("env_ensure EMORI_ADMIN_SALT")
    hostapp_start = _at("enable --now emori-hostapp")
    admin_start = _at("enable --now emori-admin")

    assert token < hostapp_start
    assert token < admin_start
    assert salt < admin_start


def test_a_re_run_restarts_the_units_that_missed_a_freshly_minted_secret():
    """`enable --now` does not restart an already-active unit, so a re-run that
    mints a secret has to say so itself."""

    text = _text()

    assert "try-restart emori-hostapp emori-admin" in text
    assert _at("SECRETS_WROTE=1") < _at("try-restart emori-hostapp emori-admin")


def test_every_install_restarts_emori_admin_and_the_hostapp_onto_the_new_code():
    """`enable --now` is a no-op on a running unit, so a full deploy that restarted only
    when a secret was minted would leave the OLD emori-admin serving the phone. The
    restart is unconditional, at column 0 -- inside no `if` -- and comes
    after the code is installed and the units are copied."""

    text = _text()
    line = "\nsudo systemctl try-restart emori-hostapp emori-admin\n"

    assert line in text
    restart = text.index(line)
    assert _at('.venv/bin/pip install -e') < restart
    assert _at("enable --now emori-admin") < restart


def test_the_env_append_cannot_fuse_with_a_file_lacking_a_final_newline():
    """A .env whose last line lost its newline would otherwise silently mangle
    the LAST secret in the file (the admin token, the dashboard salt...)."""

    text = _text()

    assert 'if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi' in text
    # ...and a present-but-empty line is replaced, never joined by a second one -- and
    # replaced ONCE: later duplicates go (tests/test_install_step8.py runs it).
    assert 'sed -i -e "0,/^$1=/{" -e "s|^$1=.*|$1=$2|" -e "b" -e "}" -e "/^$1=/d" .env' \
        in text


def test_the_hotspot_identity_is_provisioned_not_left_at_the_vendor_default():
    """The watchdog refuses to arm without it (network.watchdog_ap_ready), and
    an operator who is never told the credentials cannot use the AP either."""

    text = _text()

    assert "env_ensure EMORI_AP_SSID" in text
    assert "env_ensure EMORI_AP_PASSWORD" in text
    assert "WRITE THIS DOWN" in text


def test_visudo_is_resolved_by_path_and_by_usr_sbin():
    """Debian builds a non-root PATH without /usr/sbin, so a bare `visudo` exits
    127 and the sudoers hardening is skipped on every robot -- while the message
    blames a syntax error in a file that is perfectly valid."""

    text = _text()

    assert 'VISUDO="$(command -v visudo || echo /usr/sbin/visudo)"' in text
    assert "visudo not found" in text


def test_the_script_is_lf_only_and_syntax_clean():
    raw = _SCRIPT.read_bytes()

    # A CRLF shebang is systemd 203/EXEC on the robot.
    assert b"\r\n" not in raw
    assert raw.startswith(b"#!/usr/bin/env bash")


# --- step 8b: THIS robot's Pokamind token -----------------------------------------------------


def test_the_token_step_runs_before_any_unit_starts_and_can_trigger_the_restart():
    """The hostapp binds its credential at start, like EMORI_ADMIN_TOKEN."""

    token_step = _at("== [8b/10] Pokamind robot token ==")

    assert _at("env_ensure EMORI_ADMIN_SALT") < token_step < _at("enable --now emori-hostapp")
    assert token_step < _at("try-restart emori-hostapp emori-admin")


def test_the_token_step_never_prints_or_copies_the_token():
    text = _text()
    step = text[_at("== [8b/10]"):_at("# 9. systemd units")]

    assert 'cat "$TOKEN_FILE"' not in step
    assert "cp " not in step
    # It may say WHICH robot the token is for: the robot_id field only, of a line that
    # passed the token grammar, with -s (a plain cut echoes a line that has no ".").
    assert "cut -s -d. -f2" in step
    assert 'cut -d. -f2 "$TOKEN_FILE"' not in step


def _step_8b() -> str:
    text = _text()
    return text[_at("== [8b/10]") - len('echo "'):_at("# 9. systemd units")]


def _run_step(tmp_path, env_text: str, token: str | None):
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():
        pytest.skip("no bash")
    root = tmp_path / "emori"
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(env_text, encoding="utf-8", newline="\n")
    if token is not None:
        (root / "secrets").mkdir(exist_ok=True)
        (root / "secrets" / "pokamind_robot_token").write_text(token + "\n", encoding="ascii")
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "sudo").write_text("#!/bin/sh\nexit 0\n", encoding="ascii", newline="\n")
    (stubs / "sudo").chmod(0o755)
    script = ("set -euo pipefail\nSECRETS_WROTE=0\ncd '%s'\n%s\necho SECRETS_WROTE=$SECRETS_WROTE\n"
              % (root.as_posix(), _step_8b()))
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run([bash, "-c", script], capture_output=True, text=True, env=env,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return root, result.stdout


_GOLDEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def test_a_new_token_is_made_private_and_restarts_the_hostapp_once(tmp_path):
    root, first = _run_step(tmp_path, "EMORI_ROBOT_ID=\n", _GOLDEN)

    assert "token for robot 'test-robot-01'" in first
    assert _GOLDEN.rsplit(".", 1)[1] not in first
    assert "SECRETS_WROTE=1" in first
    if os.name == "posix":
        assert stat.S_IMODE((root / "secrets").stat().st_mode) == 0o700
        token_file = root / "secrets" / "pokamind_robot_token"
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    # A re-run with the same token restarts nothing.
    _, again = _run_step(tmp_path, "EMORI_ROBOT_ID=\n", _GOLDEN)
    assert "SECRETS_WROTE=0" in again


@pytest.mark.parametrize("content", [
    _GOLDEN.rsplit(".", 1)[1],                 # a hand-edited file holding just the seed
    "seed: " + _GOLDEN.rsplit(".", 1)[1],      # ...or a note in front of it
    _GOLDEN + "\n" + _GOLDEN.rsplit(".", 1)[1],  # a second line the robot would refuse
])
def test_a_damaged_token_file_is_reported_without_echoing_any_of_it(tmp_path, content):
    _, out = _run_step(tmp_path, "EMORI_ROBOT_ID=\n", content)

    assert _GOLDEN.rsplit(".", 1)[1] not in out
    assert "not one pkr1 token line" in out
    assert "token for robot" not in out


def test_a_robot_without_its_token_is_told_loudly_with_the_fix(tmp_path):
    _, out = _run_step(tmp_path, "EMORI_ROBOT_ID=\n", None)

    assert "NO Pokamind token" in out and "DEMO" in out
    assert "Replace token" in out and "Deploy-to-Robot" in out
    assert "docs/deploy-a-new-robot.md" in out
    assert "SECRETS_WROTE=0" in out


def test_a_retired_shared_secret_in_env_changes_nothing_and_is_never_echoed(tmp_path):
    _, plain = _run_step(tmp_path / "plain", "EMORI_ROBOT_ID=\n", None)
    _, old = _run_step(tmp_path / "old", "EMORI_PARTNER_SECRET=s3cret\n", None)

    assert old == plain  # the same "no token" verdict: the old secret is not a credential
    assert "s3cret" not in old


# --- step 8: the recovery hotspot's SSID names THIS robot ------------------------------------


def _ap_ssid(tmp_path, env_text: str, hostname: str = "reachy-mini") -> str:
    """Run install.sh's AP-SSID lines against ``env_text`` with a stub ``hostname``."""

    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():
        pytest.skip("no bash")
    text = _text()
    block = text[_at('AP_TOKEN_FILE="$(grep'):_at('AP_WROTE=0')]
    root = tmp_path / "emori"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(env_text, encoding="utf-8", newline="\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "hostname").write_text("#!/bin/sh\necho %s\n" % hostname, encoding="ascii",
                                    newline="\n")
    (stubs / "hostname").chmod(0o755)
    script = "set -euo pipefail\ncd '%s'\n%s\necho \"$AP_SSID_GEN\"\n" % (root.as_posix(), block)
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run([bash, "-c", script], capture_output=True, text=True, env=env,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_two_robots_with_the_stock_hostname_get_two_different_hotspots(tmp_path):
    """Every Reachy Mini is hostname "reachy-mini": the robot id must name the AP."""

    first = _ap_ssid(tmp_path / "a", "EMORI_ROBOT_ID=reception-desk-01\n")
    second = _ap_ssid(tmp_path / "b", "EMORI_ROBOT_ID=reception-desk-02\n")

    assert first == "emori-reception-desk-01"
    assert second == "emori-reception-desk-02"


def test_a_robot_without_an_id_still_falls_back_to_its_hostname(tmp_path):
    assert _ap_ssid(tmp_path, "EMORI_ROBOT_ID=\n") == "emori-reachy-mini"
    assert _ap_ssid(tmp_path / "none", "EMORI_ROBOT_NAME=\n", hostname="Lab 3") \
        == "emori-lab-3"


def test_the_hotspot_ssid_never_exceeds_the_32_byte_limit(tmp_path):
    ssid = _ap_ssid(tmp_path, "EMORI_ROBOT_ID=" + "a" * 63 + "\n")

    assert ssid.startswith("emori-") and len(ssid) == 32
