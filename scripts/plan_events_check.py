#!/usr/bin/env python3
"""Does ``/emori/motion/plan_events`` say what the motion trace says, for every sentence?

    python3 scripts/plan_events_check.py record --seconds 120 --output events.jsonl
    python  scripts/plan_events_check.py compare events.jsonl artifacts/motion-trace/*.jsonl

``record`` runs where ``rclpy`` and the interfaces are, on the same ROS domain as the hub, with
``EMORI_MOTION_TRACE=1`` on the hub so the trace exists. ``compare`` needs neither, and prints each
disagreement, keyed by plan.

The family on the topic is the one performed, so a sentence nothing was planned for has none,
while the trace's ``performed`` keeps what the planner chose. Timing is not compared: the trace
has the moments, and the topic is published up to a tick after them.

Exit: 0 they agree | 1 they disagree, or a file holds nothing to compare | 2 usage
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Iterable

TOPIC = "/emori/motion/plan_events"
_FIELDS = ("plan_id", "seq", "family", "slug", "started", "ended", "outcome",
           "first_apply_delay_s", "family_authored", "family_source", "skip_reason")


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def disagreements(events: Iterable[dict], trace: Iterable[dict]) -> list[str]:
    """Every way the two differ, as sentences. Empty when they agree on every plan."""

    by_plan: dict[str, list[dict]] = {}
    for event in events:
        by_plan.setdefault(str(event.get("plan_id")), []).append(event)
    sentences: dict[str, dict] = {}
    batches: dict[str, dict] = {}
    for record in trace:
        plan = str(record.get("plan"))
        if record.get("s") == "sentence":
            sentences[plan] = record
        elif record.get("s") == "batch_end" and plan != "0":
            batches[plan] = record

    problems: list[str] = []
    for plan in sorted(set(sentences) - set(by_plan), key=_plan_order):
        problems.append(f"plan {plan}: in the trace and never on the topic")
    for plan in sorted(set(by_plan) - set(sentences), key=_plan_order):
        problems.append(f"plan {plan}: on the topic and not in the trace")

    for plan in sorted(set(sentences) & set(by_plan), key=_plan_order):
        sentence, steps = sentences[plan], by_plan[plan]
        planned = bool(sentence.get("planned"))
        wanted = {
            "seq": sentence.get("seq"),
            "family": (sentence.get("performed") or "") if planned else "",
            "family_authored": sentence.get("authored") or "",
            "slug": sentence.get("slug") or "",
        }
        for name, value in wanted.items():
            seen = {step.get(name) for step in steps}
            if seen != {value}:
                problems.append(f"plan {plan}: {name} is {sorted(map(str, seen))} on the topic "
                                f"and {value!r} in the trace")
        ended = [step for step in steps if step.get("ended")]
        if len(ended) != 1:
            problems.append(f"plan {plan}: {len(ended)} ending messages on the topic")
            continue
        batch = batches.get(plan)
        if batch is None:
            if planned:
                problems.append(f"plan {plan}: planned, and the trace has no batch_end")
            elif ended[0].get("outcome") != "skipped":
                problems.append(f"plan {plan}: not planned, and the topic says "
                                f"{ended[0].get('outcome')!r}")
            continue
        for name in ("outcome", "skip_reason"):
            if (ended[0].get(name) or "") != (batch.get(name) or ""):
                problems.append(f"plan {plan}: {name} is {ended[0].get(name)!r} on the topic and "
                                f"{batch.get(name)!r} in the trace")
    return problems


def _plan_order(plan: str) -> tuple[int, str]:
    return (int(plan), plan) if plan.isdigit() else (1 << 62, plan)


def compare(events_path: Path, trace_paths: Iterable[Path]) -> int:
    events = _jsonl(events_path)
    trace = [record for path in trace_paths for record in _jsonl(path)]
    sentences = sum(1 for record in trace if record.get("s") == "sentence")
    if not events or not sentences:
        print(f"!! nothing to compare: {len(events)} events, {sentences} sentences")
        return 1
    problems = disagreements(events, trace)
    for problem in problems:
        print(f"!! {problem}")
    print(f"{sentences} sentences, {len(events)} events, {len(problems)} disagreements")
    return 1 if problems else 0


def record(seconds: float, output: Path) -> int:
    import rclpy
    from emori_msgs.msg import MotionPlanEvent
    from rclpy.executors import SingleThreadedExecutor

    rclpy.init(args=[])
    node = rclpy.create_node("plan_events_check")
    count = 0
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        def on_event(message: Any) -> None:
            nonlocal count
            count += 1
            handle.write(json.dumps({name: getattr(message, name) for name in _FIELDS}) + "\n")
            handle.flush()

        node.create_subscription(MotionPlanEvent, TOPIC, on_event, 100)
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            executor.spin_once(timeout_sec=0.2)
    node.destroy_node()
    rclpy.try_shutdown()
    print(f"{count} events in {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    recording = commands.add_parser("record")
    recording.add_argument("--seconds", type=float, required=True)
    recording.add_argument("--output", type=Path, required=True)
    comparing = commands.add_parser("compare")
    comparing.add_argument("events", type=Path)
    comparing.add_argument("trace", type=Path, nargs="+",
                           help="a session's trace file, and any other a plan may have ended in")
    args = parser.parse_args(argv)
    if args.command == "record":
        return record(args.seconds, args.output)
    return compare(args.events, args.trace)


if __name__ == "__main__":
    sys.exit(main())
