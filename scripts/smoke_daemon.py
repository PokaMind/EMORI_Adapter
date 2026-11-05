"""Smoke-test the Reachy Mini daemon over REST — the production transport.

The deployed robot is driven exclusively through the daemon's REST API
(HttpRobotClient); this smoke exercises exactly that surface, so what passes
here is what runs on the CM4.

Prerequisite: the daemon must already be running, e.g.::

    .venv-reachy\\Scripts\\reachy-mini-daemon.exe --sim --no-media

Then (any Python with the adapter installed)::

    python scripts/smoke_daemon.py [--base-url http://127.0.0.1:8000]

It checks /api/daemon/status, plays wake_up, runs a gesture from every intent
family the behaviour engine emits (goto, antennas, look_at sweep), and prints
SMOKE_OK on success.
"""

from __future__ import annotations

import argparse
import sys
import time

from emori_adapter.behavior import repertoire as R
from emori_adapter.robot.client import HttpRobotClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    client = HttpRobotClient(args.base_url)
    if not client.health():
        print(f"FAIL: daemon not running at {args.base_url} "
              "(start it: reachy-mini-daemon --sim --no-media)")
        return 1
    print(f"daemon OK at {args.base_url}")

    client.wake_up()
    time.sleep(1.5)

    # Screenless control needs the back-drivable head: set gravity_compensation and
    # confirm /api/state/full reports a head_pose (the gesture sensor reads it).
    client.set_motor_mode("gravity_compensation")
    state = client.read_state()
    if isinstance(state, dict) and isinstance(state.get("head_pose"), dict):
        print("  gravity_compensation set; head_pose readable")
    else:
        print("  WARN: head_pose not readable after set_motor_mode (sim may not expose it)")

    sequences = [
        ("nod", R.nod(reason="smoke: nod")),
        ("curious tilt", [R.head_tilt(reason="smoke: tilt")]),
        ("antenna wiggle", R.antenna_wiggle(reason="smoke: wiggle")),
        ("gaze sweep", [R.gaze_to(-18.0, reason="smoke: gaze left"),
                        R.gaze_to(18.0, reason="smoke: gaze right"),
                        R.gaze_to(0.0, reason="smoke: gaze centre")]),
        ("talking gesture", R.talking_gesture(beats=3, reason="smoke: talking")),
        ("greeting", R.greeting_gesture(reason="smoke: greeting")),
        ("calm settle", R.calm_settle(reason="smoke: de-escalation")),
        ("rest", [R.neutral_gaze(reason="smoke: rest")]),
    ]
    for name, intents in sequences:
        print(f"  -> {name}")
        for intent in intents:
            client.apply(intent)
            time.sleep(min(1.2, max(0.15, float(intent.duration or 0.3))))

    client.close()
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
