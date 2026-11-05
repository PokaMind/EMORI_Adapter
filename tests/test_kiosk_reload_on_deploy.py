"""The kiosk browser must reload when the hostapp is redeployed.

Config the page needs -- the verbal menu's grammar, its confidence gate, the capture
thresholds -- is substituted into the HTML by ``server._menu_tuning`` and friends and read
ONCE, at page load. A running Chromium therefore keeps whatever values it was served on
the day it started.

Without a reload, a menu gate fixed server-side and verified with ``curl`` (which
fetches a FRESH page) leaves the kiosk rejecting every spoken digit using the old copy it
still has in memory: the journal says the server heard 'one' at conf 0.500 and the robot
says "Sorry, I didn't catch that" in the same second -- the fix is live everywhere except
the one process that matters.

``PartOf`` is what closes that gap: systemd restarts this unit whenever the hostapp is
restarted, and a deploy is exactly a hostapp restart.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIOSK_UNIT = ROOT / "deploy" / "robot" / "kiosk.service"


def _unit() -> str:
    return KIOSK_UNIT.read_text(encoding="utf-8")


def test_kiosk_restarts_with_the_hostapp() -> None:
    # After=/Wants= only order and pull in the dependency at START. Neither propagates a
    # RESTART, which is the case that leaves a stale page serving stale config.
    assert "PartOf=emori-hostapp.service" in _unit()


def test_kiosk_still_starts_after_the_hostapp() -> None:
    # PartOf carries no ordering: without After= the browser can load before the hostapp
    # answers and land on an error page instead of the badge screen.
    unit = _unit()
    assert "After=emori-hostapp.service" in unit
    assert "Wants=emori-hostapp.service" in unit
