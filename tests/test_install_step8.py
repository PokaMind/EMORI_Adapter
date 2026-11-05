"""``deploy/robot/install.sh`` step 8, RUN: the machine secrets a fresh robot gets.

The step is executed in bash against a scratch /opt/emori (``env_ensure`` and the whole
step, as written), with ``python3`` pointed at this interpreter and a stub ``hostname``:
what lands in ``.env``, what is never overwritten, and what is shown to the person at
the deploy -- once, at the very end.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

_INSTALL = Path(__file__).resolve().parents[1] / "deploy" / "robot" / "install.sh"


def _text() -> str:
    return _INSTALL.read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    text = _text()
    return text[text.index(start):text.index(end)]


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


class _Install:
    """install.sh's env_ensure + step 8 in a scratch install root."""

    def __init__(self, tmp_path, env_text: str, *, hostname: str = "reachy-mini",
                 token: str | None = None, dashboard_db: bool = False):
        bash = _bash()
        if bash is None:
            pytest.skip("no bash")
        self.bash = bash
        self.root = tmp_path / "emori"
        (self.root / "artifacts").mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text(env_text, encoding="utf-8", newline="\n")
        if token is not None:
            (self.root / "secrets").mkdir(exist_ok=True)
            (self.root / "secrets" / "pokamind_robot_token").write_text(
                token + "\n", encoding="ascii", newline="\n")
        if dashboard_db:
            (self.root / "artifacts" / "dashboard.db").write_bytes(b"SQLite format 3\x00")
        self.stubs = tmp_path / "stubs"
        self.stubs.mkdir(exist_ok=True)
        self._stub("hostname", f"echo {hostname}\n")
        self._stub("python3", f'exec "{Path(sys.executable).as_posix()}" "$@"\n')

    def _stub(self, name: str, body: str) -> None:
        path = self.stubs / name
        path.write_text("#!/bin/sh\n" + body, encoding="ascii", newline="\n")
        path.chmod(0o755)

    def run(self, after: str = "") -> subprocess.CompletedProcess:
        script = "\n".join([
            "set -euo pipefail",
            f"ROOT='{self.root.as_posix()}'",
            'cd "$ROOT"',
            _between("env_ensure() {", "# 0. Guard"),
            _between('echo "== [8/10] admin secrets in .env =="', "# 8b. THIS robot's"),
            after,
        ])
        # A file, not `bash -c`: a Windows command line does not carry a long script with
        # quotes and continuation lines through to bash intact.
        path = self.root.parent / "step8.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        env = {**os.environ, "PATH": f"{self.stubs}{os.pathsep}{os.environ['PATH']}"}
        result = subprocess.run([self.bash, path.as_posix()], capture_output=True,
                                encoding="utf-8", errors="replace", env=env, timeout=120,
                                check=False)
        result.env = self.env()
        return result

    def env(self) -> dict[str, str]:
        values = {}
        for line in (self.root / ".env").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key] = value
        return values


# --- the dashboard password ---------------------------------------------------------------


def test_a_fresh_robot_gets_a_dashboard_password_and_salt_shown_once_at_the_end(tmp_path):
    install = _Install(tmp_path, "EMORI_DASH_SALT=\nEMORI_DASH_PASSWORD=\nEMORI_ROBOT_ID=r-01\n")

    result = install.run(after='echo "== [9/10] systemd units =="')

    assert result.returncode == 0, result.stderr
    password = result.env["EMORI_DASH_PASSWORD"]
    assert re.fullmatch(r"[a-km-np-z2-9]{4}-[a-km-np-z2-9]{4}-[a-km-np-z2-9]{4}", password)
    assert re.fullmatch(r"[0-9a-f]{32}", result.env["EMORI_DASH_SALT"])
    # Shown once, and AFTER everything else the install printed.
    assert result.stdout.count(password) == 1
    tail = result.stdout[result.stdout.index("== [9/10]"):]
    assert "WRITE THIS DOWN" in tail and password in tail
    assert "http://<robot>:8090/dashboard" in tail
    # ...beside the hotspot this install generated too.
    assert result.env["EMORI_AP_PASSWORD"] in tail


def test_an_operators_dashboard_password_is_never_overwritten_or_shown(tmp_path):
    install = _Install(tmp_path, "EMORI_DASH_PASSWORD=chosen-by-ops\nEMORI_DASH_SALT=s\n"
                                 "EMORI_AP_SSID=EMORI-Setup\nEMORI_AP_PASSWORD=secret12\n"
                                 "EMORI_ADMIN_PIN=246810\n")

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert result.env["EMORI_DASH_PASSWORD"] == "chosen-by-ops"
    assert result.env["EMORI_DASH_SALT"] == "s"
    assert "WRITE THIS DOWN" not in result.stdout
    assert "chosen-by-ops" not in result.stdout and "secret12" not in result.stdout
    assert result.env["EMORI_ADMIN_PIN"] == "246810" and "246810" not in result.stdout


def test_a_robot_with_dashboard_data_never_gets_a_new_salt(tmp_path):
    """A new salt would turn every learner already in the dashboard into a new person."""

    install = _Install(tmp_path, "EMORI_DASH_SALT=\nEMORI_ROBOT_ID=ward-3\n",
                       dashboard_db=True)

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert result.env["EMORI_DASH_SALT"] == ""
    assert "EMORI_DASH_SALT is empty and this robot already has dashboard data" in result.stdout
    # The password is still generated: it keys nothing but the login.
    assert result.env["EMORI_DASH_PASSWORD"]


def test_the_credentials_are_shown_even_when_a_later_step_fails(tmp_path):
    install = _Install(tmp_path, "EMORI_DASH_PASSWORD=\n")

    result = install.run(after="false")

    assert result.returncode != 0
    assert "WRITE THIS DOWN" in result.stdout
    assert result.env["EMORI_DASH_PASSWORD"] in result.stdout


def test_the_dashboard_secrets_are_written_before_any_unit_starts():
    text = _text()

    assert text.index("env_ensure EMORI_DASH_PASSWORD") < text.index("enable --now emori-hostapp")
    assert text.index("env_ensure EMORI_DASH_SALT") < text.index("enable --now emori-hostapp")
    # ...and a re-run that mints one restarts the hostapp that missed it.
    block = _between("env_ensure EMORI_DASH_PASSWORD", "# 8b. THIS robot's")
    assert "SECRETS_WROTE=1" in block


# --- the recovery hotspot's name ----------------------------------------------------------

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def _token_for(robot_id: str) -> str:
    return GOLDEN_TOKEN.replace("test-robot-01", robot_id)


def _mark(install: _Install) -> str:
    return (install.root / "artifacts" / "ap-ssid.generated").read_text(encoding="utf-8").strip()


def test_the_tokens_robot_id_names_the_hotspot_first(tmp_path):
    install = _Install(tmp_path, "EMORI_ROBOT_ID=ward-3\nEMORI_AP_SSID=\n",
                       token=_token_for("reception-02"))

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert result.env["EMORI_AP_SSID"] == "emori-reception-02"
    assert _mark(install) == "emori-reception-02"
    assert GOLDEN_TOKEN.rsplit(".", 1)[1] not in result.stdout


def test_a_damaged_token_file_falls_back_to_the_robot_id(tmp_path):
    install = _Install(tmp_path, "EMORI_ROBOT_ID=ward-3\n")
    (install.root / "secrets").mkdir()
    (install.root / "secrets" / "pokamind_robot_token").write_text(
        "pkr1.not-a-token\n", encoding="ascii")

    assert install.run().env["EMORI_AP_SSID"] == "emori-ward-3"


def test_a_long_robot_id_keeps_its_distinguishing_end(tmp_path):
    first = _Install(tmp_path / "a", "", token=_token_for("example-site-main-reception-desk-unit-01"))
    second = _Install(tmp_path / "b", "", token=_token_for("example-site-main-reception-desk-unit-02"))

    one = first.run().env["EMORI_AP_SSID"]
    two = second.run().env["EMORI_AP_SSID"]

    assert one != two
    assert one.endswith("-unit-01") and two.endswith("-unit-02")
    assert one.startswith("emori-") and len(one.encode()) <= 32
    assert not one.startswith("emori--")


def test_a_later_install_renames_a_hotspot_it_named_itself(tmp_path):
    """Installed before the robot had a token (hostname name), then the token arrived."""

    install = _Install(tmp_path, "EMORI_ROBOT_ID=\n")
    assert install.run().env["EMORI_AP_SSID"] == "emori-reachy-mini"
    (install.root / "secrets").mkdir()
    (install.root / "secrets" / "pokamind_robot_token").write_text(
        _token_for("fake-robot-02") + "\n", encoding="ascii")

    result = install.run(after="echo SECRETS_WROTE=$SECRETS_WROTE")

    assert result.env["EMORI_AP_SSID"] == "emori-fake-robot-02"
    assert "renamed emori-fake-robot-02 (was emori-reachy-mini" in result.stdout
    assert "SECRETS_WROTE=1" in result.stdout  # emori-admin restarts and rebrands it
    assert "the recovery hotspot is now called emori-fake-robot-02" in result.stdout
    assert result.env["EMORI_AP_PASSWORD"] not in result.stdout  # the password stays put
    assert _mark(install) == "emori-fake-robot-02"


@pytest.mark.parametrize("old_name, env_text", [
    ("emori-reachy-mini", "EMORI_ROBOT_ID=\n"),                  # an install that predates the robot-id SSID: hostname
    ("emori-fake-robot-02", "EMORI_ROBOT_ID=fake-robot-02\n"),   # the robot id
    ("emori-robot", ""),                                          # the empty fallback
])
def test_installs_from_before_the_marker_recognise_their_own_names(tmp_path, old_name,
                                                                    env_text):
    install = _Install(tmp_path, env_text + f"EMORI_AP_SSID={old_name}\n",
                       token=_token_for("reception-01"))

    assert install.run().env["EMORI_AP_SSID"] == "emori-reception-01"


@pytest.mark.parametrize("chosen", ["EMORI-Setup", "Ward3 recovery"])
def test_an_operators_hotspot_name_is_never_touched(tmp_path, chosen):
    install = _Install(tmp_path, f"EMORI_AP_SSID={chosen}\nEMORI_AP_PASSWORD=secret12\n",
                       token=_token_for("reception-01"))

    result = install.run()

    assert result.env["EMORI_AP_SSID"] == chosen
    assert "renamed" not in result.stdout
    assert not (install.root / "artifacts" / "ap-ssid.generated").exists()


def test_a_name_the_operator_changed_after_the_install_is_theirs(tmp_path):
    install = _Install(tmp_path, "EMORI_ROBOT_ID=\n")
    install.run()  # emori-reachy-mini, remembered as generated
    env = (install.root / ".env").read_text(encoding="utf-8")
    (install.root / ".env").write_text(
        env.replace("EMORI_AP_SSID=emori-reachy-mini", "EMORI_AP_SSID=emori-ward3"),
        encoding="utf-8", newline="\n")

    assert install.run().env["EMORI_AP_SSID"] == "emori-ward3"


# --- duplicate EMORI_AP_SSID lines ----------------------------------------------------------


def _ssid_lines(install: _Install) -> list[str]:
    return [line for line in (install.root / ".env").read_text(encoding="utf-8").splitlines()
            if line.startswith("EMORI_AP_SSID=")]


def test_an_empty_duplicate_never_overwrites_an_operators_hotspot_name(tmp_path):
    """The current name is not read from the LAST line only, while the rename rewrites
    EVERY line: an empty duplicate after the operator's must not make it look unnamed."""

    install = _Install(tmp_path, "EMORI_AP_SSID=EMORI-Setup\nEMORI_AP_PASSWORD=secret12\n"
                                 "EMORI_AP_SSID=\n", token=_token_for("reception-01"))

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert _ssid_lines(install) == ["EMORI_AP_SSID=EMORI-Setup", "EMORI_AP_SSID="]
    assert "renamed" not in result.stdout
    assert "sets EMORI_AP_SSID 2 times" in result.stdout and "only the LAST" in result.stdout
    assert not (install.root / "artifacts" / "ap-ssid.generated").exists()


def test_an_operators_name_anywhere_among_generated_ones_is_theirs(tmp_path):
    install = _Install(tmp_path, "EMORI_AP_SSID=Ward3 recovery\n"
                                 "EMORI_AP_SSID=emori-reachy-mini\n",
                       token=_token_for("reception-01"))

    install.run()

    assert _ssid_lines(install) == ["EMORI_AP_SSID=Ward3 recovery",
                                    "EMORI_AP_SSID=emori-reachy-mini"]


def test_a_rename_rewrites_exactly_one_line(tmp_path):
    install = _Install(tmp_path, "EMORI_AP_SSID=emori-reachy-mini\nEMORI_ROBOT_ID=\n"
                                 "EMORI_AP_SSID=\n", token=_token_for("reception-01"))

    result = install.run(after="echo SECRETS_WROTE=$SECRETS_WROTE")

    assert _ssid_lines(install) == ["EMORI_AP_SSID=emori-reception-01"]
    assert (install.root / ".env").read_text(encoding="utf-8").startswith(
        "EMORI_AP_SSID=emori-reception-01\nEMORI_ROBOT_ID=\n")
    assert "renamed emori-reception-01 (was emori-reachy-mini" in result.stdout
    assert "SECRETS_WROTE=1" in result.stdout


def test_duplicates_of_the_generated_name_are_folded_into_one(tmp_path):
    install = _Install(tmp_path, "EMORI_AP_SSID=emori-reception-01\nEMORI_AP_SSID=\n",
                       token=_token_for("reception-01"))
    (install.root / "artifacts" / "ap-ssid.generated").write_text(
        "emori-reception-01\n", encoding="utf-8")

    result = install.run(after="echo SECRETS_WROTE=$SECRETS_WROTE")

    assert _ssid_lines(install) == ["EMORI_AP_SSID=emori-reception-01"]
    assert "renamed" not in result.stdout
    assert "SECRETS_WROTE=1" in result.stdout  # the name in force was the empty one


def test_a_fresh_key_set_twice_empty_is_written_once(tmp_path):
    install = _Install(tmp_path, "EMORI_AP_SSID=\nEMORI_DASH_PASSWORD=\nEMORI_AP_SSID=\n"
                                 "EMORI_DASH_PASSWORD=\n")

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert _ssid_lines(install) == ["EMORI_AP_SSID=emori-reachy-mini"]
    env = (install.root / ".env").read_text(encoding="utf-8")
    assert env.count("EMORI_DASH_PASSWORD=") == 1
    assert env.startswith("EMORI_AP_SSID=emori-reachy-mini\nEMORI_DASH_PASSWORD=")


# --- values nobody types: the admin PIN, the hardware profile, the ROS lane's secrets ----


def test_a_fresh_robot_gets_an_admin_pin_shown_once_at_the_end(tmp_path):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=\nEMORI_ROBOT_ID=r-01\n")

    result = install.run(after='echo "== [9/10] systemd units =="')

    assert result.returncode == 0, result.stderr
    pin = result.env["EMORI_ADMIN_PIN"]
    assert re.fullmatch(r"[0-9]{6}", pin)
    env = (install.root / ".env").read_text(encoding="utf-8")
    assert env.count("EMORI_ADMIN_PIN=") == 1          # the empty line was replaced
    assert result.stdout.count(pin) == 1
    tail = result.stdout[result.stdout.index("== [9/10]"):]
    assert "WRITE THIS DOWN" in tail and "admin page PIN" in tail and pin in tail


def test_under_the_one_click_deploy_the_block_is_also_kept_for_after_readycheck(tmp_path):
    """one-click-deploy.sh runs readycheck AFTER install.sh; the block it shows again
    then is the one install.sh wrote (0600) to EMORI_CREDENTIALS_FILE."""

    install = _Install(tmp_path, "EMORI_ADMIN_PIN=" + chr(10) + "EMORI_ROBOT_ID=r-01" + chr(10))
    kept = install.root / "artifacts" / ".new-credentials"

    result = install.run(after=f'export EMORI_CREDENTIALS_FILE="{kept.as_posix()}"')

    assert result.returncode == 0, result.stderr
    pin = result.env["EMORI_ADMIN_PIN"]
    text = kept.read_text(encoding="utf-8")
    assert "WRITE THIS DOWN" in text and pin in text
    assert "WRITE THIS DOWN" in result.stdout        # still printed as install.sh ends
    if os.name == "posix":
        assert (kept.stat().st_mode & 0o777) == 0o600


def test_nothing_new_writes_no_credentials_file(tmp_path):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=246810" + chr(10) + "EMORI_AP_SSID=site-ap"
                       + chr(10) + "EMORI_AP_PASSWORD=chosen-pass" + chr(10)
                       + "EMORI_DASH_PASSWORD=chosen-dash" + chr(10))
    kept = install.root / "artifacts" / ".new-credentials"

    result = install.run(after=f'export EMORI_CREDENTIALS_FILE="{kept.as_posix()}"')

    assert result.returncode == 0, result.stderr
    assert not kept.exists()


def test_an_operators_pin_is_never_overwritten_in_env_or_on_the_admin_page(tmp_path):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=135790\n")
    result = install.run()
    assert result.env["EMORI_ADMIN_PIN"] == "135790"
    assert "135790" not in result.stdout

    # Changed on the admin page: secrets/admin.env holds it, and it outranks .env.
    changed = _Install(tmp_path / "b", "EMORI_ADMIN_PIN=\n")
    (changed.root / "secrets").mkdir()
    (changed.root / "secrets" / "admin.env").write_text("EMORI_ADMIN_PIN=975310\n",
                                                        encoding="ascii")
    result = changed.run()
    assert result.returncode == 0, result.stderr
    assert result.env["EMORI_ADMIN_PIN"] == ""
    assert "admin page PIN" not in result.stdout and "975310" not in result.stdout


def test_credentials_saved_on_the_admin_page_are_neither_regenerated_nor_shown(tmp_path):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=246810\nEMORI_DASH_SALT=s\n")
    (install.root / "secrets").mkdir()
    (install.root / "secrets" / "admin.env").write_text(
        "EMORI_DASH_PASSWORD=phone-chosen-1\nEMORI_AP_SSID=Ward3 recovery\n"
        "EMORI_AP_PASSWORD=phone-chosen-2\n", encoding="ascii")

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert "EMORI_DASH_PASSWORD" not in result.env
    assert "EMORI_AP_SSID" not in result.env and "EMORI_AP_PASSWORD" not in result.env
    assert "WRITE THIS DOWN" not in result.stdout
    assert "phone-chosen" not in result.stdout


def test_the_hardware_profile_and_the_skill_key_are_generated_once(tmp_path):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=246810\n")

    first = install.run()

    assert first.returncode == 0, first.stderr
    assert first.env["EMORI_HARDWARE_PROFILE"] == "reachy-mini-wireless"
    key = first.env["EMORI_SKILL_KEY"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", key)
    assert key not in first.stdout     # never shown

    second = install.run()
    assert second.env["EMORI_SKILL_KEY"] == key


@pytest.mark.parametrize("env_text", [
    "EMORI_ADMIN_PIN=246810\n",
    "EMORI_ADMIN_PIN=246810\n#EMORI_ROS_ID_SALT=\n",
    "EMORI_ADMIN_PIN=246810\nEMORI_ROS_ID_SALT=\n",
])
def test_the_ros_id_salt_is_never_generated(tmp_path, env_text):
    """Unset, the ROS lane's pseudonyms change at every restart (privacy.Privacy). A
    fixed salt makes them linkable across restarts: a person's decision, never the
    installer's -- also on a robot in service whose ROS lane is running."""

    install = _Install(tmp_path, env_text)

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert not result.env.get("EMORI_ROS_ID_SALT")
    assert "env_ensure EMORI_ROS_ID_SALT" not in _text()


def test_an_operators_profile_and_ros_secrets_are_kept(tmp_path):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=246810\nEMORI_HARDWARE_PROFILE=other-kit\n"
                                 "EMORI_SKILL_KEY=chosen-key\nEMORI_ROS_ID_SALT=chosen-salt\n")

    result = install.run()

    assert result.env["EMORI_HARDWARE_PROFILE"] == "other-kit"
    assert result.env["EMORI_SKILL_KEY"] == "chosen-key"
    assert result.env["EMORI_ROS_ID_SALT"] == "chosen-salt"


# --- the old shipped session-start deadline ---------------------------------------------
# Every .env written from the template before the hardware profile carried the deadline
# holds 45000 / 45. That explicit value outranks the profile, so a robot deployed before it
# would stay on 45 s through any number of redeploys, which keep the robot's own .env.


@pytest.mark.parametrize("profile_line", ["", "EMORI_HARDWARE_PROFILE=reachy-mini-wireless\n"])
def test_a_reachy_mini_loses_the_old_shipped_deadline_to_its_profile(tmp_path, profile_line):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=246810\n" + profile_line
                       + "EMORI_SESSION_START_MS=45000\nEMORI_SESSION_START_S=45\n")

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert result.env["EMORI_SESSION_START_MS"] == ""
    assert result.env["EMORI_SESSION_START_S"] == ""
    assert "emptied EMORI_SESSION_START_MS=45000" in result.stdout
    assert "emptied EMORI_SESSION_START_S=45" in result.stdout
    # ...which the settings layer then fills from the profile.
    from emori_adapter import site_settings

    env = dict(result.env, EMORI_SITE_AUTO="0")
    sources = site_settings.apply(env, root=tmp_path / "no-site")
    assert (env["EMORI_SESSION_START_MS"], sources["EMORI_SESSION_START_MS"]) \
        == ("120000", "hardware")
    assert (env["EMORI_SESSION_START_S"], sources["EMORI_SESSION_START_S"]) \
        == ("130", "hardware")

    again = install.run()
    assert "emptied" not in again.stdout


def test_the_last_assignment_is_the_one_judged_and_one_line_is_left(tmp_path):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=246810\n"
                                 "EMORI_HARDWARE_PROFILE=reachy-mini-wireless\n"
                                 "EMORI_SESSION_START_MS=60000\nEMORI_SESSION_START_MS=45000\n"
                                 'EMORI_SESSION_START_S="45"\n')

    result = install.run()

    assert result.returncode == 0, result.stderr
    lines = (install.root / ".env").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("EMORI_SESSION_START_")] \
        == ["EMORI_SESSION_START_MS=", "EMORI_SESSION_START_S="]


@pytest.mark.parametrize("env_text", [
    # An operator's own deadline, on a Reachy Mini.
    "EMORI_HARDWARE_PROFILE=reachy-mini-wireless\n"
    "EMORI_SESSION_START_MS=60000\nEMORI_SESSION_START_S=70\n",
    "EMORI_HARDWARE_PROFILE=reachy-mini-wireless\n"
    "EMORI_SESSION_START_MS=450000\nEMORI_SESSION_START_S=450\n",
    # Any other hardware has no profile deadline to fall back to: 45 s is its value.
    "EMORI_HARDWARE_PROFILE=other-kit\nEMORI_SESSION_START_MS=45000\nEMORI_SESSION_START_S=45\n",
])
def test_any_other_deadline_or_hardware_keeps_its_line(tmp_path, env_text):
    install = _Install(tmp_path, "EMORI_ADMIN_PIN=246810\n" + env_text)
    before = {key: value for key, value in install.env().items()
              if key.startswith("EMORI_SESSION_START_")}

    result = install.run()

    assert result.returncode == 0, result.stderr
    assert {key: value for key, value in result.env.items()
            if key.startswith("EMORI_SESSION_START_")} == before
    assert "emptied" not in result.stdout


def test_the_generated_values_are_written_before_any_unit_starts():
    text = _text()
    start = text.index("enable --now emori-hostapp")
    for key in ("EMORI_ADMIN_PIN", "EMORI_HARDWARE_PROFILE", "EMORI_SKILL_KEY"):
        assert text.index(key) < start, key
    # The old "will not invent one" warning is gone with the reason for it.
    assert "will not invent" not in text


def test_both_units_load_the_settings_files_after_env_in_precedence_order():
    root = _INSTALL.parents[2]
    expected = {
        "emori-hostapp.service": [
            "EnvironmentFile=-/opt/emori/.env",
            "EnvironmentFile=-/opt/emori/configs/site/settings.env",
            "EnvironmentFile=-/opt/emori/secrets/site.env",
            "EnvironmentFile=-/opt/emori/secrets/admin.env",
        ],
        # emori-admin holds no robot credential: .env (the robot token may be there on
        # a dev box, and the host app's secrets are) and secrets/site.env are loaded by
        # the process itself, without those keys (site_settings.load_admin_environment).
        "emori-admin.service": [
            "EnvironmentFile=-/opt/emori/configs/site/settings.env",
            "EnvironmentFile=-/opt/emori/secrets/admin.env",
        ],
    }
    for unit, files in expected.items():
        lines = (root / "deploy" / "robot" / unit).read_text(encoding="utf-8").splitlines()
        found = [line for line in lines if line.startswith("EnvironmentFile=")]
        assert found == files, unit


def test_install_creates_the_site_directories_without_touching_their_contents():
    block = _between('echo "== [4/10] configs =="', "# 5. Camera loopbacks")

    assert "mkdir -p configs/site/auto secrets" in block
    assert "chmod 700 secrets" in block and "chmod 755 configs/site configs/site/auto" in block
    for forbidden in ("rm -rf configs/site", "rm -rf secrets", "rm -f configs/site",
                      "> configs/site", "cp configs/prod"):
        assert forbidden not in block, forbidden
