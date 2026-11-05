"""Replay a recorded session through the live stack — 'exactly having the robot'.

Reads a JSONL recording (artifacts/recordings/<session>.jsonl from a real run, or
tests/fixtures/live_session.jsonl) and POSTs each envelope to the hostapp's
/api/cycle, honouring the original inter-cycle timing — the hostapp drives the
simulated robot, FIWARE, the /ws/relay fan-out (ROS 2), and the completion flow
just as if the kiosk browser were relaying a live Pokamind session.

    python scripts/replay_session.py tests/fixtures/live_session.jsonl
    python scripts/replay_session.py recording.jsonl --fast        # no sleeps
    python scripts/replay_session.py recording.jsonl --base-url http://localhost:8095
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

import httpx


def _ts(envelope: dict) -> float | None:
    raw = envelope.get("timestamp")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("recording", help="JSONL recording to replay")
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--fast", action="store_true", help="skip inter-cycle sleeps")
    ap.add_argument("--max-gap", type=float, default=5.0,
                    help="cap on the sleep between envelopes (seconds)")
    args = ap.parse_args()

    with open(args.recording, encoding="utf-8") as fh:
        envelopes = [json.loads(line) for line in fh if line.strip()]
    if not envelopes:
        print("recording is empty")
        return 1

    client = httpx.Client(base_url=args.base_url, timeout=10.0)
    try:
        client.get("/healthz")
    except httpx.HTTPError as e:
        print(f"hostapp not reachable at {args.base_url}: {e}")
        return 1

    print(f"replaying {len(envelopes)} envelopes against {args.base_url}")
    prev_ts: float | None = None
    for i, env in enumerate(envelopes):
        ts = _ts(env)
        if not args.fast and prev_ts is not None and ts is not None and ts > prev_ts:
            time.sleep(min(args.max_gap, ts - prev_ts))
        if ts is not None:
            prev_ts = ts
        try:
            r = client.post("/api/cycle", json=env)
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"  [{i:03d}] {env.get('type')}: ERROR {e}")
            continue
        if body.get("completed"):
            print(f"  [{i:03d}] session completed -> summary: {body.get('summary', '')[:80]}...")
        elif body.get("level"):
            print(f"  [{i:03d}] {env.get('type')} -> {body['level']}"
                  + (f" ({body['reasons'][0]})" if body.get("reasons") else ""))
        elif body.get("spoke") is not None:
            print(f"  [{i:03d}] utterance -> {'spoke' if body['spoke'] else 'deduped'}")

    status = client.get("/api/status").json()
    print(f"\ndone. counters: {status.get('counters')}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
