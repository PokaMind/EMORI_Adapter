"""Passive recorder for a real session: samples the robot once a second, drives nothing.

Writes ``sys.jsonl`` with one line per sample: monotonic time, ``MemAvailable`` and the
proportional set size (PSS) of each watched process. Run it beside a normal session
(browser and camera running, a person using the robot) together with
``scripts/golden_host.py``, which records the client side.

    python scripts/golden_passive.py --out artifacts/golden/run1 --pid 1234 --seconds 600

Linux only; on other systems the fields that need ``/proc`` are null.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional


def read_mem_available_kb(path: str = "/proc/meminfo") -> Optional[int]:
    try:
        with open(path, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_pss_kb(pid: int, root: str = "/proc") -> Optional[int]:
    try:
        with open(f"{root}/{pid}/smaps_rollup", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def sample(pids: list[int], clock: Callable[[], float] = time.monotonic,
           mem: Callable[[], Optional[int]] = read_mem_available_kb,
           pss: Callable[[int], Optional[int]] = read_pss_kb) -> dict:
    return {
        "t": clock(),
        "mem_available_kb": mem(),
        "pss_kb": {str(p): pss(p) for p in pids},
    }


def record(out_dir: Path, pids: list[int], seconds: float, interval: float = 1.0,
           clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep,
           sampler: Callable[[list[int]], dict] = sample) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    deadline = clock() + seconds
    n = 0
    with (out_dir / "sys.jsonl").open("a", encoding="utf-8") as fh:
        while True:
            fh.write(json.dumps(sampler(pids), separators=(",", ":")) + "\n")
            fh.flush()
            n += 1
            if clock() + interval > deadline:
                break
            sleep(interval)
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pid", type=int, action="append", default=[],
                    help="process to report PSS for (repeatable)")
    ap.add_argument("--seconds", type=float, default=600.0)
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args(argv)
    try:
        record(args.out, args.pid, args.seconds, args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
