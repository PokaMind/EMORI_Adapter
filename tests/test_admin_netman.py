"""The host Wi-Fi backend: the daemon's /wifi router, re-done over nmcli for v2.

On a v2 robot (no reachy-mini daemon; the ROS node's shim owns :8000) the daemon's
/wifi routes do not exist: a "scan" sent there answers 404 and the robot cannot be put
on a site Wi-Fi from the phone. These tests pin the replacement against the
daemon it replaces (``reachy_mini/daemon/app/routers/wifi_config.py``):

* the same modes, shapes, busy lock / 409, last error, connect-or-revert,
  forget-falls-back, boot fallback;
* exactly the argv it runs, with sudo only where root is needed, and the
  sudoers fragment granting exactly those -- no more, no less;
* the site's Wi-Fi password never in a log, a response, a kept error, or any
  argv other than the one the daemon's own nmcli package used.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path

import pytest

from emori_adapter.admin import netman, network, probes
from test_admin_service_fakes import FakeNM, base_env, install, terse

_ROOT = Path(__file__).resolve().parents[1]
_SUDOERS = _ROOT / "deploy" / "robot" / "sudoers-emori-admin"

PSK = "s3cret-Passphrase!"
SCAN = (
    (" ", "Example Wi-Fi", "AA:BB:CC:DD:EE:01", 54, "WPA2"),
    ("*", "Example Wi-Fi", "AA:BB:CC:DD:EE:02", 82, "WPA2"),   # same SSID, stronger AP
    (" ", "", "AA:BB:CC:DD:EE:03", 90, "WPA2"),              # hidden (terse form)
    (" ", "--", "AA:BB:CC:DD:EE:04", 88, "WPA2"),            # hidden (table form)
    (" ", "Lab:5GHz", "AA:BB:CC:DD:EE:05", 70, "WPA1 WPA2"),  # a colon in the name
    (" ", "Guest", "AA:BB:CC:DD:EE:06", 31, ""),
)


@pytest.fixture()
def nm(monkeypatch, tmp_path):
    base_env(monkeypatch, state_file=str(tmp_path / "state.json"))
    monkeypatch.setenv("EMORI_ADMIN_HOST_BACKEND", "host")
    fake = FakeNM(scan=SCAN)
    install(monkeypatch)
    monkeypatch.setattr(probes, "run", fake)
    return fake


def _ap(monkeypatch, ssid="emori-setup-3", password="test-ap-passphrase"):
    monkeypatch.setenv("EMORI_AP_SSID", ssid)
    monkeypatch.setenv("EMORI_AP_PASSWORD", password)


# --------------------------------------------------------------------------
# parsing real nmcli -t output
# --------------------------------------------------------------------------
def test_terse_fields_unescape_colons_and_backslashes():
    """Verified against NetworkManager 1.36: a profile named Caf:é\\Net prints as
    ``Caf\\:é\\\\Net`` and an inactive one has an EMPTY device in terse mode."""
    assert netman.split_terse("Caf\\:é\\\\Net:uuid-1:802-11-wireless:") == [
        "Caf:é\\Net", "uuid-1", "802-11-wireless", ""]
    assert netman.split_terse("*:Lab:AA\\:BB\\:CC:Infra") == ["*", "Lab", "AA:BB:CC", "Infra"]


def test_only_wifi_connections_are_read_and_both_empty_and_dashes_mean_inactive():
    text = "\n".join([
        terse("Wired connection 1", "u0", "802-3-ethernet", "eth0"),
        terse("Hotspot", "u1", "802-11-wireless", "wlan0"),
        terse("Lab: 2nd floor", "u2", "802-11-wireless", ""),
        terse("Old", "u3", "wifi", "--"),
        "garbage-without-fields",
    ])

    found = netman.parse_connections(text)

    assert [(c.name, c.active) for c in found] == [
        ("Hotspot", True), ("Lab: 2nd floor", False), ("Old", False)]


def test_a_scan_is_strongest_first_each_ssid_once_and_hidden_networks_left_out():
    rows = netman.parse_wifi_list("\n".join(
        terse(i, s, b, "Infra", "6", "2437 MHz", "130 Mbit/s", str(sig), sec)
        for i, s, b, sig, sec in SCAN) + "\nnot:enough:fields\n")

    assert ("Lab:5GHz", 70) in rows
    assert netman.ssids_by_signal(rows) == ["Example Wi-Fi", "Lab:5GHz", "Guest"]


def test_a_row_with_an_unreadable_signal_sorts_last_instead_of_failing_the_scan():
    rows = netman.parse_wifi_list(
        terse(" ", "Odd", "B", "Infra", "1", "2412 MHz", "54 Mbit/s", "??", "") + "\n" +
        terse(" ", "Fine", "C", "Infra", "1", "2412 MHz", "54 Mbit/s", "10", "") + "\n")

    assert netman.ssids_by_signal(rows) == ["Fine", "Odd"]


# --------------------------------------------------------------------------
# status: the daemon's WifiStatus
# --------------------------------------------------------------------------
def test_status_on_the_hotspot_is_the_daemons_shape(nm):
    nm.add_profile("Example Wi-Fi")
    nm.add_profile("Hotspot", hotspot=True)
    nm.active = "Hotspot"

    result = netman.request("GET", "/wifi/status")

    assert result.ok and result.status == 200
    assert result.payload == {"mode": "hotspot", "known_networks": ["Example Wi-Fi"],
                              "connected_network": "Hotspot"}


@pytest.mark.parametrize("active,mode", [("Example Wi-Fi", "wlan"), (None, "disconnected")])
def test_status_reports_wlan_and_disconnected(nm, active, mode):
    nm.add_profile("Example Wi-Fi")
    nm.active = active

    payload = netman.request("GET", "/wifi/status").payload

    assert payload["mode"] == mode
    assert payload["connected_network"] == active


def test_status_reads_need_no_sudo_and_run_in_a_fixed_utf8_c_locale(nm):
    netman.request("GET", "/wifi/status")

    assert nm.calls == [["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show"]]
    assert nm.envs[0]["LANG"] == "C.UTF-8" and nm.envs[0]["LC_ALL"] == "C.UTF-8"


def test_while_a_change_is_in_flight_the_mode_is_busy(nm):
    assert netman._busy.acquire(blocking=False)
    try:
        assert netman.request("GET", "/wifi/status").payload["mode"] == "busy"
    finally:
        netman._busy.release()


def test_no_nmcli_is_a_named_failure_not_daemon_down(nm):
    nm.missing = True

    result = netman.request("GET", "/wifi/status")
    body = network.status(with_internet=False)

    assert result.status == 500 and result.error == "nmcli_missing"
    assert body["ok"] is False and body["error"] == "nmcli_missing"


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------
def test_scan_is_rescan_then_the_daemons_own_list_both_as_root(nm):
    result = netman.request("POST", "/wifi/scan_and_list")

    assert result.payload == ["Example Wi-Fi", "Lab:5GHz", "Guest"]
    assert nm.root_calls() == [
        ["sudo", "-n", "nmcli", "device", "wifi", "rescan"],
        ["sudo", "-n", "nmcli", "-t", "-f",
         "IN-USE,SSID,BSSID,MODE,CHAN,FREQ,RATE,SIGNAL,SECURITY", "device", "wifi", "list"]]
    # The list waits for the scan the rescan started; a CM4 takes seconds.
    assert nm.timeouts[1] >= 20


def test_a_refused_rescan_still_lists_what_networkmanager_knows(nm):
    """NetworkManager refuses to scan while the robot IS the access point --
    exactly when an operator on its hotspot taps Scan."""
    nm.rescan_fails = [(1, "Error: Scanning not allowed while in AP mode.")]

    assert netman.request("POST", "/wifi/scan_and_list").payload == [
        "Example Wi-Fi", "Lab:5GHz", "Guest"]


def test_the_boot_scan_is_strict_like_the_daemons(nm):
    nm.rescan_fails = [(8, "")]

    with pytest.raises(netman.NmcliError, match="NetworkManager is not running"):
        netman.scan(strict=True)


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------
def test_joining_a_new_network_creates_it_exactly_as_the_daemon_did(nm):
    result = netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi",
                                                             "password": PSK})

    assert result.ok and result.status == 200 and result.payload is None  # accepted
    assert ["sudo", "-n", "nmcli", "device", "wifi", "connect", "Example Wi-Fi",
            "password", PSK] in nm.calls
    connect_index = nm.calls.index(["sudo", "-n", "nmcli", "device", "wifi", "connect",
                                    "Example Wi-Fi", "password", PSK])
    assert nm.timeouts[connect_index] > 90  # nmcli's own activation wait is 90 s
    assert nm.active == "Example Wi-Fi"
    assert netman.last_error() is None
    assert netman.request("GET", "/wifi/status").payload["mode"] == "wlan"


def test_an_open_network_is_joined_without_a_password_argument(nm):
    netman.request("POST", "/wifi/connect", params={"ssid": "Guest", "password": ""})

    assert ["sudo", "-n", "nmcli", "device", "wifi", "connect", "Guest"] in nm.calls


def test_a_saved_network_is_brought_up_and_the_new_password_is_not_used(nm):
    """The daemon's setup_wifi_connection: an existing profile is only `up`ed."""
    nm.add_profile("Example Wi-Fi", psk="old-passphrase")
    nm.add_profile("Hotspot", hotspot=True)
    nm.active = "Hotspot"

    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})

    assert nm.root_calls() == [["sudo", "-n", "nmcli", "connection", "up", "id",
                                "Example Wi-Fi"]]
    assert all(PSK not in arg for call in nm.calls for arg in call)


def test_the_live_network_is_left_alone(nm):
    nm.add_profile("Example Wi-Fi")
    nm.active = "Example Wi-Fi"

    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})

    assert nm.root_calls() == []


def test_a_failed_join_keeps_the_error_deletes_the_profile_and_brings_the_hotspot_back(nm):
    nm.add_profile("Hotspot", hotspot=True)
    nm.active = "Hotspot"
    nm.join_fails = (4, "Error: Connection activation failed: Secrets were required, "
                        "but not provided.")

    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})

    # The daemon's nmcli package raises "Connection activation failed" for exit 4,
    # and str(exception) is what /wifi/error has always shown.
    assert netman.request("GET", "/wifi/error").payload == {
        "error": "Connection activation failed"}
    assert "Example Wi-Fi" not in nm.profiles
    assert nm.root_calls()[-2:] == [
        ["sudo", "-n", "nmcli", "connection", "delete", "id", "Example Wi-Fi"],
        ["sudo", "-n", "nmcli", "connection", "up", "id", "Hotspot"]]
    assert nm.active == "Hotspot"
    assert netman.request("GET", "/wifi/status").payload["mode"] == "hotspot"


def test_a_failed_join_whose_profile_cannot_be_deleted_still_brings_the_hotspot_back(
        nm, monkeypatch):
    """The daemon's single try skipped the hotspot when the delete failed; on v2
    that revert is the only way back, so a failed delete must not cost it."""
    real = nm._root

    def delete_fails(argv, args):
        if args[:2] == ["connection", "delete"]:
            return probes.Cmd(ok=False, ran=True, returncode=7,
                              stderr="Error: Connection deletion failed.")
        return real(argv, args)

    monkeypatch.setattr(nm, "_root", delete_fails)
    nm.add_profile("Hotspot", hotspot=True)
    nm.active = "Hotspot"
    nm.join_fails = (4, "Error: Connection activation failed.")

    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})

    assert nm.root_calls()[-2:] == [
        ["sudo", "-n", "nmcli", "connection", "delete", "id", "Example Wi-Fi"],
        ["sudo", "-n", "nmcli", "connection", "up", "id", "Hotspot"]]
    assert nm.active == "Hotspot"
    # The operator is shown why the JOIN failed, not the cleanup's trouble.
    assert netman.last_error() == "Connection activation failed"


def test_a_failed_join_on_a_robot_with_no_hotspot_profile_creates_ours(nm, monkeypatch):
    _ap(monkeypatch)
    nm.join_fails = (10, "Error: No network with SSID 'Example Wi-Fi' found.")

    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})

    assert nm.root_calls()[-1] == [
        "sudo", "-n", "nmcli", "device", "wifi", "hotspot", "con-name", "Hotspot",
        "ssid", "emori-setup-3", "password", "test-ap-passphrase"]
    assert netman.last_error() == "Connection, device, or access point does not exist"


@pytest.mark.parametrize("ssid,password", [("", ""), ("emori", "short")])
def test_without_a_usable_ap_identity_the_daemons_defaults_are_used(nm, monkeypatch,
                                                                    ssid, password):
    """Parity with v1: the daemon falls back to reachy-mini-ap / reachy-mini."""
    _ap(monkeypatch, ssid, password)
    nm.join_fails = (4, "")

    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})

    assert nm.root_calls()[-1][-4:] == ["ssid", "reachy-mini-ap", "password", "reachy-mini"]


def test_nmcli_saying_failed_with_exit_zero_is_still_a_failure(nm, monkeypatch):
    real = nm._root

    def lying(argv, args):
        if args[:3] == ["device", "wifi", "connect"]:
            nm.add_profile(args[3])
            return probes.Cmd(ok=True, ran=True,
                              stdout="Error: Connection activation failed: (7) Secrets\n")
        return real(argv, args)

    monkeypatch.setattr(nm, "_root", lying)
    nm.add_profile("Hotspot", hotspot=True)

    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})

    assert netman.last_error() == "Connection activation failed"
    assert nm.active == "Hotspot"


def test_a_second_change_while_one_is_running_is_409_and_runs_nothing(nm):
    assert netman._busy.acquire(blocking=False)
    try:
        for path, params in (("/wifi/connect", {"ssid": "Example Wi-Fi", "password": PSK}),
                             ("/wifi/setup_hotspot", None)):
            result = netman.request("POST", path, params=params)
            assert result.status == 409 and result.error == "http_409"
            assert result.payload == {"detail": "Another operation is in progress."}
    finally:
        netman._busy.release()
    assert nm.root_calls() == []


def test_the_change_really_runs_in_a_thread_and_releases_the_lock(nm, monkeypatch):
    """With the real spawner: 200 at once, busy while it runs, free afterwards."""
    import threading

    gate = threading.Event()
    real = nm._root

    def slow(argv, args):
        gate.wait(5)
        return real(argv, args)

    monkeypatch.setattr(nm, "_root", slow)
    monkeypatch.setattr(netman, "_spawn", netman._start_thread)

    result = netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi",
                                                             "password": PSK})
    assert result.status == 200
    assert netman.request("GET", "/wifi/status").payload["mode"] == "busy"
    assert netman.request("POST", "/wifi/connect",
                          params={"ssid": "x", "password": PSK}).status == 409
    gate.set()
    for thread in threading.enumerate():
        if thread.name == "admin-wifi":
            thread.join(5)
    assert not netman.busy()
    assert nm.active == "Example Wi-Fi"


def test_a_crashing_worker_never_leaves_the_lock_held(nm, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(netman, "setup_connection", boom)

    netman.request("POST", "/wifi/setup_hotspot")

    assert not netman.busy()
    assert netman.last_error() == "Unexpected error (RuntimeError)"


# --------------------------------------------------------------------------
# forget
# --------------------------------------------------------------------------
def test_the_hotspot_cannot_be_forgotten(nm):
    nm.add_profile("Hotspot", hotspot=True)

    result = netman.request("POST", "/wifi/forget", params={"ssid": "Hotspot"})

    assert result.status == 400
    assert nm.root_calls() == []


def test_forgetting_an_unknown_network_is_the_daemons_404(nm):
    from emori_adapter.admin import host_backend

    result = netman.request("POST", "/wifi/forget", params={"ssid": "Nope"})

    assert result.status == 404
    assert result.payload == {"detail": "Network 'Nope' not found in saved networks."}
    # ...and that is an ANSWER, not a missing router.
    assert host_backend.route_missing(result) is False


def test_forgetting_a_saved_network_does_not_touch_the_live_one(nm):
    nm.add_profile("Example Wi-Fi")
    nm.add_profile("Old")
    nm.active = "Example Wi-Fi"

    assert netman.request("POST", "/wifi/forget", params={"ssid": "Old"}).status == 200

    assert nm.root_calls() == [["sudo", "-n", "nmcli", "connection", "delete", "id", "Old"]]
    assert nm.active == "Example Wi-Fi"


def test_forgetting_the_live_network_falls_back_to_the_hotspot(nm):
    nm.add_profile("Example Wi-Fi")
    nm.add_profile("Hotspot", hotspot=True)
    nm.active = "Example Wi-Fi"

    netman.request("POST", "/wifi/forget", params={"ssid": "Example Wi-Fi"})

    assert nm.root_calls() == [
        ["sudo", "-n", "nmcli", "connection", "delete", "id", "Example Wi-Fi"],
        ["sudo", "-n", "nmcli", "connection", "up", "id", "Hotspot"]]
    assert nm.active == "Hotspot"


# --------------------------------------------------------------------------
# hotspot + error routes
# --------------------------------------------------------------------------
def test_setup_hotspot_reuses_the_existing_profile_whatever_is_passed(nm):
    nm.add_profile("Hotspot", hotspot=True)

    netman.request("POST", "/wifi/setup_hotspot",
                   params={"ssid": "other", "password": "other-passphrase"})

    assert nm.root_calls() == [["sudo", "-n", "nmcli", "connection", "up", "id", "Hotspot"]]


def test_setup_hotspot_creates_the_profile_with_the_identity_it_was_given(nm):
    netman.request("POST", "/wifi/setup_hotspot",
                   params={"ssid": "emori-setup-3", "password": "test-ap-passphrase"})

    assert nm.profiles["Hotspot"]["ssid"] == "emori-setup-3"
    assert nm.active == "Hotspot"


def test_a_failed_hotspot_is_kept_as_the_last_error(nm):
    nm.add_profile("Hotspot", hotspot=True)
    nm.up_fails["Hotspot"] = (4, "Error: Connection activation failed")

    netman.request("POST", "/wifi/setup_hotspot")

    assert netman.last_error() == "Connection activation failed"


def test_reset_error_and_unknown_routes(nm):
    netman._set_error("x")

    assert netman.request("POST", "/wifi/reset_error").payload == {"status": "ok"}
    assert netman.last_error() is None
    assert netman.request("POST", "/wifi/forget_everything").payload == {"detail": "Not Found"}


# --------------------------------------------------------------------------
# boot: the daemon's ensure_wifi_on_startup
# --------------------------------------------------------------------------
def test_a_robot_with_no_wifi_at_boot_comes_up_on_its_hotspot(nm, monkeypatch):
    _ap(monkeypatch)
    nm.add_profile("Old site")  # known, but not in range: nothing joined it

    outcome = netman.ensure_wifi_on_startup(sleep=lambda s: None)

    assert outcome == "hotspot"
    assert nm.active == "Hotspot"
    assert nm.profiles["Hotspot"]["ssid"] == "emori-setup-3"


def test_a_robot_on_its_site_wifi_at_boot_is_left_there(nm):
    nm.add_profile("Example Wi-Fi")
    nm.active = "Example Wi-Fi"

    assert netman.ensure_wifi_on_startup(sleep=lambda s: None) == "wlan"
    assert [c for c in nm.root_calls() if "up" in c or "hotspot" in c] == []


def test_the_boot_check_retries_while_the_radio_is_not_ready(nm):
    nm.rescan_fails = [(1, "Error: No Wi-Fi device found."),
                       (8, "")]
    sleeps = []

    assert netman.ensure_wifi_on_startup(sleep=sleeps.append) == "hotspot"
    assert sleeps == [3.0, 3.0] + [10.0] * 4  # two retries, then the 40 s confirmation


def test_the_boot_check_gives_up_after_five_tries(nm, caplog):
    nm.rescan_fails = [(8, "")] * 5
    sleeps = []

    with caplog.at_level(logging.WARNING, logger="emori.admin.netman"):
        assert netman.ensure_wifi_on_startup(sleep=sleeps.append) == "failed"

    assert sleeps == [3.0] * 4
    assert "attempt 5/5" in caplog.text
    assert nm.active is None


def test_the_boot_check_raises_the_hotspot_only_after_40_s_of_no_wifi(nm):
    nm.add_profile("Hotspot", hotspot=True)
    reads = []

    def sleep(seconds):
        reads.append((seconds, nm.active))

    assert netman.ensure_wifi_on_startup(sleep=sleep) == "hotspot"
    assert reads == [(10.0, None)] * 4  # nothing raised while it was still looking
    assert nm.active == "Hotspot"


def test_a_start_during_a_short_wifi_drop_leaves_the_robot_on_its_site_network(nm):
    """emori-admin restarted (deploy, Services tab) while NetworkManager was
    between reconnect attempts: the robot must not end up on its hotspot."""
    nm.add_profile("Hotspot", hotspot=True)
    nm.add_profile("Example Wi-Fi")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            nm.active = "Example Wi-Fi"  # NetworkManager got back on by itself

    assert netman.ensure_wifi_on_startup(sleep=sleep) == "wlan"
    assert nm.active == "Example Wi-Fi"
    assert [c for c in nm.root_calls() if "up" in c or "hotspot" in c] == []


def test_an_operator_starting_a_change_during_the_boot_wait_wins(nm):
    nm.add_profile("Hotspot", hotspot=True)

    def sleep(seconds):
        netman._busy.acquire(blocking=False)

    try:
        assert netman.ensure_wifi_on_startup(sleep=sleep) == "busy"
    finally:
        netman._busy.release()
    assert [c for c in nm.root_calls() if "up" in c or "hotspot" in c] == []


def test_the_boot_check_never_races_an_operator(nm):
    assert netman._busy.acquire(blocking=False)
    try:
        assert netman.ensure_wifi_on_startup(sleep=lambda s: None) == "busy"
    finally:
        netman._busy.release()
    assert [c for c in nm.root_calls() if "hotspot" in c or "up" in c] == []


# --------------------------------------------------------------------------
# the password goes nowhere it should not
# --------------------------------------------------------------------------
def test_the_password_is_never_logged_kept_or_returned(nm, caplog):
    """nmcli quoting the key back in an error must not carry it any further."""
    nm.add_profile("Hotspot", hotspot=True)
    nm.join_fails = (1, "Error: 802-11-wireless-security.psk: property is invalid: '%s'"
                     % PSK)

    with caplog.at_level(logging.DEBUG):
        accepted = netman.request("POST", "/wifi/connect",
                                  params={"ssid": "Example Wi-Fi", "password": PSK})
        error = network.wifi_error()
        status = network.status(with_internet=False)

    assert PSK not in caplog.text
    assert PSK not in repr(accepted) and PSK not in repr(error) and PSK not in repr(status)
    assert PSK not in (netman.last_error() or "")
    assert "***" in netman.last_error()
    # In argv only where the daemon's own nmcli package put it -- the one call the
    # sudoers fragment keeps out of sudo's log.
    carrying = [call for call in nm.calls if any(PSK in arg for arg in call)]
    assert carrying == [["sudo", "-n", "nmcli", "device", "wifi", "connect", "Example Wi-Fi",
                         "password", PSK]]
    assert all(_matches(call, _aliases()["EMORI_ADMIN_WIFI_SECRET"]) for call in carrying)


def test_the_api_never_echoes_the_password_on_the_host_backend(nm, monkeypatch):
    from starlette.testclient import TestClient

    from emori_adapter.admin import server
    from test_admin_service_fakes import PIN

    nm.join_fails = (1, "Error: bad key '%s'" % PSK)
    client = TestClient(server.create_app(background=False))
    assert client.post("/api/login", json={"pin": PIN}).status_code == 204

    bodies = [client.post("/api/network/connect",
                          json={"ssid": "Example Wi-Fi", "password": PSK}).text,
              client.get("/api/network/error").text,
              client.get("/api/network/status").text]

    assert all(PSK not in body for body in bodies)
    assert "***" in bodies[1]


# --------------------------------------------------------------------------
# sudoers: the fragment grants exactly what the code runs
# --------------------------------------------------------------------------
def _logical_lines(text: str) -> list[str]:
    lines, buffer = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buffer and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\") and not line.endswith("\\\\"):
            buffer += line[:-1] + " "
            continue
        lines.append(buffer + line)
        buffer = ""
    return lines


def _aliases() -> dict[str, list[str]]:
    aliases = {}
    for line in _logical_lines(_SUDOERS.read_text(encoding="utf-8")):
        if not line.startswith("Cmnd_Alias "):
            continue
        name, _, body = line[len("Cmnd_Alias "):].partition("=")
        commands = [re.sub(r"\s+", " ", c).strip().replace("\\,", ",")
                    for c in re.split(r"(?<!\\),", body)]
        aliases[name.strip()] = [c for c in commands if c]
    return aliases


def _matches(argv: list[str], grants: list[str]) -> bool:
    """sudo's rule: same binary, and the args string fnmatch'ed (``*`` crosses spaces)."""
    assert argv[:3] == ["sudo", "-n", "nmcli"], argv
    args = " ".join(argv[3:])
    for grant in grants:
        binary, _, pattern = grant.partition(" ")
        if binary == "/usr/bin/nmcli" and fnmatch.fnmatchcase(args, pattern):
            return True
    return False


def _exercise_every_root_path(nm, monkeypatch) -> list[list[str]]:
    _ap(monkeypatch)
    netman.request("POST", "/wifi/scan_and_list")
    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})
    netman.request("POST", "/wifi/connect", params={"ssid": "Guest", "password": ""})
    nm.add_profile("Saved: 2")
    netman.request("POST", "/wifi/connect", params={"ssid": "Saved: 2", "password": PSK})
    nm.join_fails = (4, "")
    netman.request("POST", "/wifi/connect", params={"ssid": "Bad", "password": PSK})
    nm.join_fails = None
    netman.request("POST", "/wifi/connect", params={"ssid": "Example Wi-Fi", "password": PSK})
    netman.request("POST", "/wifi/forget", params={"ssid": "Example Wi-Fi"})
    del nm.profiles["Hotspot"]
    netman.request("POST", "/wifi/setup_hotspot")
    return nm.root_calls()


def test_every_root_command_the_code_runs_is_granted_by_the_fragment(nm, monkeypatch):
    grants = _aliases()
    allowed = grants["EMORI_ADMIN_WIFI"] + grants["EMORI_ADMIN_WIFI_SECRET"]

    calls = _exercise_every_root_path(nm, monkeypatch)

    assert nm.unknown_root == []
    assert calls and all(_matches(call, allowed) for call in calls), [
        call for call in calls if not _matches(call, allowed)]


def test_the_fragment_grants_nothing_the_code_does_not_run(nm, monkeypatch):
    grants = _aliases()
    calls = _exercise_every_root_path(nm, monkeypatch)
    # The rebrand's modify is exercised by test_admin_service_workers (plain sudo).
    unused = [g for g in grants["EMORI_ADMIN_WIFI"] + grants["EMORI_ADMIN_WIFI_SECRET"]
              if g != "/usr/bin/nmcli connection modify Hotspot *"
              and not any(_matches(call, [g]) for call in calls)]

    assert unused == []


def test_every_secret_carrying_call_is_in_the_unlogged_alias(nm, monkeypatch):
    grants = _aliases()
    calls = _exercise_every_root_path(nm, monkeypatch)
    secrets = (PSK, "test-ap-passphrase")
    carrying = [c for c in calls if any(s in arg for s in secrets for arg in c)]

    assert carrying  # the session did join with a key and create our AP
    for call in carrying:
        assert _matches(call, grants["EMORI_ADMIN_WIFI_SECRET"]), call
        # ...and NOT also grantable through the logged alias: sudo would then
        # still match it there, but the Defaults line is per command, so the
        # point is that no logged grant was written with a secret slot in it.
        assert not _matches(call, grants["EMORI_ADMIN_WIFI"]), call


def test_sudo_does_not_log_the_secret_carrying_calls_and_pollen_is_granted_them():
    text = _SUDOERS.read_text(encoding="utf-8")
    logical = _logical_lines(text)

    assert "Defaults!EMORI_ADMIN_WIFI_SECRET !log_allowed" in logical
    grant = [line for line in logical if line.startswith("pollen ")]
    assert len(grant) == 1
    for alias in ("EMORI_ADMIN_WIFI", "EMORI_ADMIN_WIFI_SECRET", "EMORI_ADMIN_HOTSPOT"):
        assert re.search(r"\b%s\b" % alias, grant[0]), alias
    # The commas inside the -f list must be escaped, or sudoers splits the
    # command in two and the list is never granted.
    assert "IN-USE\\,SSID\\,BSSID" in text
    assert b"\r\n" not in _SUDOERS.read_bytes()


def test_the_privilege_check_asks_about_exactly_the_grants_the_code_uses(nm, monkeypatch):
    """`sudo -n -l` on these argv passes on a robot with the fragment installed, and a
    grant the code needs cannot be left out of the check."""

    grants = _aliases()
    allowed = grants["EMORI_ADMIN_WIFI"] + grants["EMORI_ADMIN_WIFI_SECRET"]
    asked = netman.privileged_argvs()
    calls = _exercise_every_root_path(nm, monkeypatch)

    def grant_of(argv):
        return next(g for g in allowed if _matches(argv, [g]))

    assert all(_matches(argv, allowed) for argv in asked), asked
    assert {grant_of(argv) for argv in asked} == {grant_of(call) for call in calls}
    for argv in asked:
        assert netman.list_argv(argv) == ["sudo", "-n", "-l", *argv[2:]]


def test_reads_never_go_through_sudo(nm, monkeypatch):
    _exercise_every_root_path(nm, monkeypatch)

    reads = [c for c in nm.calls if c[:1] == ["nmcli"]]
    assert reads and all("connection" in c and "show" in c for c in reads)
    assert all(c[:3] == ["sudo", "-n", "nmcli"] for c in nm.calls if c[0] == "sudo")
