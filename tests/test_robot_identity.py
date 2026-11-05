"""Telling robots apart: every fresh Reachy Mini is hostname ``reachy-mini``.

The board's serial and Wi-Fi MAC say which physical robot answered. Deploy-to-Robot's
preflight prints them (with the robot's EMORI_ROBOT_ID and the token's robot) and names
them in its last question; install.sh records them in ``ROBOT_IDENTITY``; readycheck's
``identity`` gate and the admin System tab show them, and catch a card that moved into
another robot body.
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from emori_adapter import robot_identity
from emori_adapter.admin import system as admin_system
from emori_adapter.hostapp.readycheck import (
    PASS,
    SKIP,
    WARN,
    IdentityProbe,
    TokenProbe,
    collect,
    evaluate,
    identity_line,
)
from tests.test_readycheck import TOKEN, _by_name, _FakeProbe, _healthy

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL = _ROOT / "deploy" / "robot" / "install.sh"
SERIAL = "10000000a1b2c3d4"
MAC = "dc:a6:32:01:02:03"


# --- reading the hardware --------------------------------------------------------------


def test_the_serial_is_read_from_the_device_tree_first(tmp_path):
    tree = tmp_path / "serial-number"
    tree.write_bytes(SERIAL.encode() + b"\x00")
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("Serial\t\t: 10000000ffffffff\n", encoding="ascii")

    assert robot_identity.hardware_serial(str(tree), str(cpuinfo)) == SERIAL


def test_the_serial_falls_back_to_cpuinfo_and_never_invents_one(tmp_path):
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nHardware\t: BCM2835\nSerial\t\t: 10000000A1B2C3D4\n",
                       encoding="ascii")
    missing = str(tmp_path / "none")

    assert robot_identity.hardware_serial(missing, str(cpuinfo)) == SERIAL
    zeros = tmp_path / "zeros"
    zeros.write_text("Serial\t\t: 0000000000000000\n", encoding="ascii")
    assert robot_identity.hardware_serial(missing, str(zeros)) == ""   # no serial at all
    assert robot_identity.hardware_serial(missing, missing) == ""       # not a Pi


def test_the_wlan_mac_is_the_wireless_interfaces(tmp_path):
    for name, address, wireless in (("eth0", "aa:bb:cc:dd:ee:ff", False),
                                    ("lo", "00:00:00:00:00:00", False),
                                    ("wlan0", MAC.upper(), True)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "address").write_text(address + "\n", encoding="ascii")
        if wireless:
            (tmp_path / name / "wireless").mkdir()

    assert robot_identity.wlan_mac(str(tmp_path)) == MAC
    assert robot_identity.wlan_mac(str(tmp_path / "missing")) == ""


def test_the_record_keeps_only_values_of_their_own_grammar(tmp_path):
    path = tmp_path / "ROBOT_IDENTITY"
    path.write_text(f"serial={SERIAL}\nwlan_mac={MAC}\nrobot_id=robot-01\n"
                    "recorded_at=2026-10-12T20:00:00Z\nserial2=x\ntoken=pkr1.x.y\n"
                    "robot_id=$(rm -rf /)\n", encoding="ascii")

    assert robot_identity.read_record(path) == {
        "serial": SERIAL, "wlan_mac": MAC, "robot_id": "robot-01",
        "recorded_at": "2026-10-12T20:00:00Z"}
    assert robot_identity.read_record(tmp_path / "absent") == {}


def test_recording_returns_what_was_there_before(tmp_path):
    path = tmp_path / "ROBOT_IDENTITY"
    at = datetime.datetime(2026, 10, 12, 20, 0, tzinfo=datetime.timezone.utc)

    previous, written = robot_identity.record(path, robot_id="robot-01", serial=SERIAL,
                                              mac=MAC, now=at)
    assert previous == {}
    assert path.read_text(encoding="ascii") == (
        f"serial={SERIAL}\nwlan_mac={MAC}\nrobot_id=robot-01\nrecorded_at=2026-10-12T20:00:00Z\n")

    previous, _ = robot_identity.record(path, robot_id="Not A Slug!", serial="1000000099999999",
                                        mac="not-a-mac", now=at)
    assert previous["serial"] == SERIAL
    assert robot_identity.read_record(path) == {"serial": "1000000099999999",
                                                "recorded_at": "2026-10-12T20:00:00Z"}


def test_the_install_command_says_when_the_card_moved(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ROBOT_IDENTITY"
    path.write_text(f"serial={SERIAL}\nrobot_id=robot-01\n", encoding="ascii")
    monkeypatch.setattr(robot_identity, "hardware_serial", lambda *a: "1000000099999999")
    monkeypatch.setattr(robot_identity, "wlan_mac", lambda *a: MAC)

    assert robot_identity.main(["record", "--file", str(path), "--robot-id", "robot-01"]) == 0

    out = capsys.readouterr().out
    assert f"recorded on board {SERIAL} (robot robot-01)" in out
    assert "now runs on board 1000000099999999" in out
    assert robot_identity.read_record(path)["serial"] == "1000000099999999"


def test_install_sh_records_the_identity_after_the_token_and_before_the_units():
    text = _INSTALL.read_text(encoding="utf-8")
    step = text.index("== [8c/10] robot identity ==")

    assert text.index("== [8b/10] Pokamind robot token ==") < step
    assert step < text.index("enable --now emori-hostapp")
    assert "-m emori_adapter.robot_identity record --robot-id" in text[step:]


# --- readycheck ------------------------------------------------------------------------


def _identity(**fields):
    return _by_name(evaluate(_healthy(identity=IdentityProbe(**fields))))["identity"]


def test_the_recorded_board_running_is_a_pass():
    gate = _identity(serial=SERIAL, recorded_serial=SERIAL)

    assert (gate.status, gate.code) == (PASS, "identity_recorded")


def test_a_card_in_another_robot_body_is_a_warning_naming_both_boards():
    gate = _identity(serial="1000000099999999", recorded_serial=SERIAL,
                     recorded_robot_id="robot-01")

    assert (gate.status, gate.code) == (WARN, "identity_card_moved")
    assert SERIAL in gate.hint and "1000000099999999" in gate.hint
    assert "'test-robot-01'" in gate.hint  # the token this body now signs with


@pytest.mark.parametrize("fields, code", [
    ({"serial": SERIAL}, "identity_unrecorded"),             # installed before the record
    ({"recorded_serial": SERIAL}, "serial_unavailable"),     # not a Pi (a dev box)
])
def test_what_cannot_be_compared_is_skipped(fields, code):
    gate = _identity(**fields)

    assert (gate.status, gate.code) == (SKIP, code)


def test_the_text_report_starts_by_naming_the_robot():
    ev = _healthy(identity=IdentityProbe(serial=SERIAL, wlan_mac=MAC), token=TOKEN,
                  env_robot_id="ward-3")

    line = identity_line(ev)

    assert line == (f"robot: serial {SERIAL}, Wi-Fi {MAC}, token for 'test-robot-01', "
                    "EMORI_ROBOT_ID=ward-3")
    assert identity_line(_healthy(identity=IdentityProbe(), token=TokenProbe())) == (
        "robot: serial unknown")


class _IdentityProbe(_FakeProbe):
    def robot_identity(self):
        return IdentityProbe(serial=SERIAL, wlan_mac=MAC, recorded_serial=SERIAL)


def test_collect_asks_the_probe_and_degrades_without_it():
    assert collect(_IdentityProbe({})).identity.serial == SERIAL
    assert collect(_FakeProbe({})).identity == IdentityProbe()


# --- the admin page's System tab -------------------------------------------------------


def test_the_system_tab_shows_the_board_and_a_moved_card(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(robot_identity, "hardware_serial", lambda *a: SERIAL)
    monkeypatch.setattr(robot_identity, "wlan_mac", lambda *a: MAC)

    fields = admin_system.robot_identity_fields()
    assert fields == {"robot_serial": SERIAL, "robot_wlan_mac": MAC}

    (tmp_path / "ROBOT_IDENTITY").write_text("serial=1000000099999999\n", encoding="ascii")
    moved = admin_system.robot_identity_fields()
    assert moved["recorded_serial"].startswith("1000000099999999 (this card was installed")
    assert {"robot_serial", "robot_wlan_mac"} <= set(admin_system.info())


# --- Deploy-to-Robot's preflight -------------------------------------------------------


@pytest.fixture(scope="module")
def deploy():
    name = "deploy_to_robot_identity_under_test"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / "deploy_to_robot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():
        return None
    return found


def test_the_preflight_reads_the_board_without_changing_it(deploy, tmp_path):
    bash = _bash()
    if bash is None:
        pytest.skip("no bash")
    (tmp_path / "dt").write_bytes(b"10000000A1B2C3D4\x00")
    net = tmp_path / "net"
    (net / "eth0").mkdir(parents=True)
    (net / "eth0" / "address").write_text("aa:bb:cc:dd:ee:ff\n", encoding="ascii")
    (net / "wlan0" / "wireless").mkdir(parents=True)
    (net / "wlan0" / "address").write_text(MAC.upper() + "\n", encoding="ascii")
    (tmp_path / "ROBOT_IDENTITY").write_text("serial=1000000099999999\n", encoding="ascii")
    script = deploy.PREFLIGHT
    start = script.index("serial=$(")
    end = script.index("[ -d /opt/emori ]")
    block = (script[start:end]
             .replace("/proc/device-tree/serial-number", (tmp_path / "dt").as_posix())
             .replace("/sys/class/net", net.as_posix())
             .replace("/opt/emori/ROBOT_IDENTITY", (tmp_path / "ROBOT_IDENTITY").as_posix()))
    path = tmp_path / "identity.sh"
    path.write_text(block, encoding="ascii", newline="\n")

    result = subprocess.run([bash, path.as_posix()], capture_output=True, text=True,
                            timeout=60, check=False)

    assert result.returncode == 0, result.stderr
    assert deploy.parse_kv(result.stdout) == {
        "hw_serial": SERIAL, "wlan_mac": MAC, "recorded_serial": "1000000099999999"}


def test_the_preflight_prints_which_robot_answered(deploy, capsys):
    deploy.report_identity({"hw_serial": SERIAL, "wlan_mac": MAC, "env_robot_id": "robot-01",
                            "token": "yes", "token_robot": "robot-01",
                            "recorded_serial": SERIAL})

    out = capsys.readouterr().out
    assert f"robot hardware: serial {SERIAL}, Wi-Fi {MAC}" in out
    assert "EMORI_ROBOT_ID=robot-01; holds the token for 'robot-01'" in out
    assert "!!" not in out


def test_the_preflight_warns_about_a_card_from_another_board(deploy, capsys):
    deploy.report_identity({"hw_serial": SERIAL, "recorded_serial": "1000000099999999"})

    out = capsys.readouterr().out
    assert "installed on ANOTHER board (serial 1000000099999999)" in out
    assert "none yet (a fresh robot)" in out


def test_the_last_question_names_the_board_and_the_tokens_robot(deploy):
    token = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    info = {"hw_serial": SERIAL, "wlan_mac": MAC}

    question = deploy.deploy_question(info, token)

    assert SERIAL in question and MAC in question and "'test-robot-01'" in question
    # A fresh robot has no label yet (it gets one after this deploy): nothing to check.
    assert "label" not in question
    labelled = deploy.deploy_question(dict(info, env_robot_id="test-robot-01"), token)
    assert "Check its label" in labelled
    assert "Check its label" in deploy.deploy_question(dict(info, token_robot="old-01"),
                                                       token)
    assert token.rsplit(".", 1)[1] not in question
    assert SERIAL in deploy.deploy_question(info, None)
    assert deploy.deploy_question({}, None) == "  deploy now?"
