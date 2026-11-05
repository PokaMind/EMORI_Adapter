"""Which physical robot this is: the CM4's serial number and its Wi-Fi MAC.

Every fresh Reachy Mini is hostname ``reachy-mini`` and its daemon says ``reachy_mini``,
so on a site with several new robots nothing on a screen said which unit a Pokamind
token was about to land on. The hardware says it: the Raspberry Pi serial
(``/proc/device-tree/serial-number``, else the ``Serial`` line of ``/proc/cpuinfo``)
and the MAC of the wireless interface. Neither is a secret.

``install.sh`` records them, with the robot id this install serves, in
``/opt/emori/ROBOT_IDENTITY``::

    serial=10000000a1b2c3d4
    wlan_mac=dc:a6:32:01:02:03
    robot_id=robot-demo-01
    recorded_at=2026-10-12T20:00:00Z

so a card that moved to another robot body is visible later (readycheck's ``identity``
gate, the admin page's System tab): the record says one board, the hardware another,
and the token on the card logs THIS body in as the robot the record names.

Standard library only; every reader fails soft to ``""``.

    python -m emori_adapter.robot_identity record --robot-id <id>    # install.sh
"""

from __future__ import annotations

import argparse
import datetime
import os
from pathlib import Path
import re
import sys
from typing import Optional

IDENTITY_FILE = "ROBOT_IDENTITY"
_SERIAL_RE = re.compile(r"[0-9a-f]{8,32}\Z")
_MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z")
_ROBOT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
_STAMP_RE = re.compile(r"[0-9TZ:+-]{1,32}\Z")
_FIELDS = {"serial": _SERIAL_RE, "wlan_mac": _MAC_RE, "robot_id": _ROBOT_ID_RE,
           "recorded_at": _STAMP_RE}
# An all-zero serial is what a board without one reports, not an identity.
_NO_SERIAL = re.compile(r"0+\Z")


def _serial(raw: str) -> str:
    value = re.sub(r"[^0-9a-f]", "", (raw or "").strip().lower())
    if not _SERIAL_RE.fullmatch(value) or _NO_SERIAL.fullmatch(value):
        return ""
    return value


def hardware_serial(device_tree: str = "/proc/device-tree/serial-number",
                    cpuinfo: str = "/proc/cpuinfo") -> str:
    """The board's serial number, lowercase hex, or ``""`` when it has none."""

    try:
        with open(device_tree, "rb") as handle:
            found = _serial(handle.read(64).replace(b"\0", b"").decode("ascii", "replace"))
        if found:
            return found
    except OSError:
        pass
    try:
        with open(cpuinfo, encoding="ascii", errors="replace") as handle:
            for line in handle:
                key, sep, value = line.partition(":")
                if sep and key.strip() == "Serial":
                    return _serial(value)
    except OSError:
        pass
    return ""


def wlan_mac(net: str = "/sys/class/net") -> str:
    """The MAC of the first wireless interface, or ``""``."""

    try:
        names = sorted(os.listdir(net))
    except OSError:
        return ""
    for name in names:
        if not os.path.isdir(os.path.join(net, name, "wireless")):
            continue
        try:
            with open(os.path.join(net, name, "address"), encoding="ascii") as handle:
                value = handle.read(64).strip().lower()
        except OSError:
            continue
        if _MAC_RE.fullmatch(value) and value != "00:00:00:00:00:00":
            return value
    return ""


def read_record(path: str | os.PathLike = IDENTITY_FILE) -> dict[str, str]:
    """The recorded identity: only known keys, only values of their own grammar."""

    record: dict[str, str] = {}
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            text = handle.read(4096)
    except OSError:
        return record
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        pattern = _FIELDS.get(key)
        if sep and pattern is not None and pattern.fullmatch(value.strip()):
            record[key] = value.strip()
    return record


def record(path: str | os.PathLike = IDENTITY_FILE, *, robot_id: str = "",
           serial: Optional[str] = None, mac: Optional[str] = None,
           now: Optional[datetime.datetime] = None) -> tuple[dict[str, str], dict[str, str]]:
    """Write this board's identity to ``path``; ``(previous, written)``."""

    previous = read_record(path)
    written = {
        "serial": hardware_serial() if serial is None else _serial(serial),
        "wlan_mac": wlan_mac() if mac is None else (mac if _MAC_RE.fullmatch(mac) else ""),
        "robot_id": robot_id if _ROBOT_ID_RE.fullmatch(robot_id or "") else "",
        "recorded_at": (now or datetime.datetime.now(datetime.timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }
    lines = [f"{key}={value}" for key, value in written.items() if value]
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return previous, written


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m emori_adapter.robot_identity")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("record", help=f"write {IDENTITY_FILE} (install.sh)")
    write.add_argument("--file", default=IDENTITY_FILE)
    write.add_argument("--robot-id", default="")
    commands.add_parser("show", help="print this board's identity")
    args = parser.parse_args(argv)
    if args.command == "show":
        print(f"serial={hardware_serial()}")
        print(f"wlan_mac={wlan_mac()}")
        return 0
    previous, written = record(args.file, robot_id=args.robot_id)
    was = previous.get("serial", "")
    if was and written["serial"] and was != written["serial"]:
        print(f"  !! this install was recorded on board {was}"
              f"{' (robot ' + previous['robot_id'] + ')' if previous.get('robot_id') else ''},"
              f" and now runs on board {written['serial']}: the card moved to another robot."
              " Recorded the new board.")
    print(f"  robot identity: serial {written['serial'] or 'unknown'}, Wi-Fi "
          f"{written['wlan_mac'] or 'unknown'}, robot id {written['robot_id'] or 'unset'}"
          f" ({args.file})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
