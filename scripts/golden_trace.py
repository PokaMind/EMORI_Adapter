"""Record what a stack does, and compare two stacks to prove they behave the same.

    python scripts/golden_trace.py list
    python scripts/golden_trace.py record --label before --scenario full_session --runs 5
    python scripts/golden_trace.py compare --a before --b after
    python scripts/golden_trace.py spread --a before
    python scripts/golden_trace.py self-check

``record`` runs a scenario in the simulator unless ``--base-url`` names a running host.
Each simulated run gets a process, a working directory, a stub broker and a fake voice of
its own. On a robot the host is the recording launcher, in a maintenance window, with the
kiosk stopped so that nothing else drains the page's verbs. **There the head moves and the
speaker plays aloud**, and the head must be free and nobody touching it.

``compare`` exits non-zero on any violation and prints the first divergence per stream.
Runs live under ``--root`` (``artifacts/golden``) by label, and ``--a`` or ``--b`` may also be
a path. Tolerances come from ``deploy/hub/golden_tolerances.yaml``, then any
``addons/*/golden/tolerances.yaml``, then each ``--tolerance`` given, later ones winning.

``self-check`` proves the harness can see what it claims to: it records one full session
twice, requires those to agree, then breaks copies of them and requires each to be caught.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Optional
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))

from golden import compare as comparing  # noqa: E402
from golden import runs as stored  # noqa: E402
from golden.record import RobotHost, record_run  # noqa: E402
from golden.scenarios import addon_recorders, addon_tolerances, all_scenarios  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOLERANCE = ROOT / "deploy" / "hub" / "golden_tolerances.yaml"
DEFAULT_RUNS_ROOT = Path("artifacts/golden")
_SIM_RUN_TIMEOUT_S = 900
SELF_CHECK_SCENARIO = "full_session"


def _say(message: str) -> None:
    print(message, flush=True)


def _side(value: str, root: Path) -> Path:
    """A label under ``root``, or a directory if it is written as a path.

    A bare name is always a label: a directory of that name here must not stand in for it.
    """

    candidate = Path(value)
    is_path = candidate.is_absolute() or len(candidate.parts) > 1
    return candidate if is_path and candidate.is_dir() else root / value


def _tolerances(extra: Optional[list[str]]) -> dict:
    return comparing.load_tolerances([DEFAULT_TOLERANCE, *addon_tolerances(ROOT), *(extra or [])])


# ---------------------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------------------


def sim_run(scenario_name: str, run_dir: Path, *, label: str, fast: bool, lang: str) -> None:
    """One simulated run, in this process. See :func:`_spawn_sim_run`."""

    from golden.sim_host import BADGE, SECOND_BADGE, SimHost, apply_environment, prepare_workdir
    from golden.sim_host import sim_environment
    from golden.stub_orion import StubOrion

    scenario = all_scenarios(ROOT)[scenario_name]
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="golden-"))
    try:
        prepare_workdir(workdir)
        orion = StubOrion(run_dir / "fiware.jsonl")
        apply_environment(sim_environment(workdir, orion.start(), dict(scenario.env)))
        os.chdir(workdir)
        host = SimHost(run_dir=run_dir, workdir=workdir, extension_factory=scenario.extension,
                       orion=orion)
        track = _HostVoiceTrack(host, speed=2.0 if fast else 1.0)
        record_run(scenario, host=host, run_dir=run_dir, label=label, fast=fast, lang=lang,
                   badge=BADGE, second_badge=SECOND_BADGE, track=track, root=ROOT,
                   recorders=addon_recorders(ROOT))
    finally:
        os.chdir(ROOT)
        shutil.rmtree(workdir, ignore_errors=True)


class _HostVoiceTrack:
    """The host's voice exists only once it has started, and is a new one after a restart."""

    def __init__(self, host, *, speed: float) -> None:
        self._host = host
        self._speed = speed

    def play(self, *args, **kwargs) -> int:
        from golden.voice_track import SimTrack

        return SimTrack(self._host.voice, speed=self._speed).play(*args, **kwargs)


def _spawn_sim_run(scenario: str, run_dir: Path, *, label: str, fast: bool, lang: str) -> int:
    env = dict(os.environ)
    source = str(ROOT / "src")
    env["PYTHONPATH"] = source + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    argv = [sys.executable, str(Path(__file__).resolve()), "_sim-run", "--scenario", scenario,
            "--run-dir", str(run_dir.resolve()), "--label", label, "--lang", lang]
    if fast:
        argv.append("--fast")
    return subprocess.run(argv, env=env, timeout=_SIM_RUN_TIMEOUT_S, check=False).returncode


def _next_run(directory: Path) -> Path:
    numbers = [int(path.name.split("-")[1]) for path in directory.glob("run-*")
               if path.name.split("-")[1].isdigit()]
    return directory / f"run-{max(numbers, default=0) + 1:03d}"


def cmd_record(args: argparse.Namespace) -> int:
    scenarios = all_scenarios(ROOT)
    names = list(scenarios) if args.scenario == "all" else [args.scenario]
    mode = "robot" if args.base_url else "sim"
    failures = 0
    for name in names:
        scenario = scenarios.get(name)
        if scenario is None:
            _say(f"no scenario named {name}; try: list")
            return 2
        if mode not in scenario.modes and not scenario.offline:
            if args.scenario == "all":
                _say(f"{name}: not applicable on {mode}")
                continue
            _say(f"{name} does not run on {mode}")
            return 2
        directory = args.root / args.label / name
        for _ in range(args.runs):
            run_dir = _next_run(directory)
            _say(f"{name}: {run_dir}")
            if mode == "sim":
                code = _spawn_sim_run(name, run_dir, label=args.label, fast=args.fast, lang=args.lang)
            else:
                code = _robot_run(args, scenario, run_dir)
            if code != 0:
                failures += 1
                continue
            for violation in comparing.self_check(stored.load_run(run_dir)):
                _say(f"  {violation}")
                failures += 1
    return 1 if failures else 0


def _robot_idle(base_url: str) -> Optional[str]:
    """Why a robot run must not start now, or None. A session is never interrupted."""

    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/status", timeout=5.0) as answer:
            status = json.load(answer)
    except (OSError, ValueError):
        return "the host does not answer /api/status"
    return "a session is running: never interrupt one" if status.get("session") else None


def _robot_run(args: argparse.Namespace, scenario, run_dir: Path) -> int:
    from golden.voice_track import RobotTrack, sync_run

    root = Path(args.host_root).resolve()
    host = RobotHost(base_url=args.base_url, root=root,
                     client_dir=Path(args.client_dir) if args.client_dir else None,
                     restart_command=args.restart_command.split() if args.restart_command else None,
                     admin_token=os.environ.get("EMORI_ADMIN_TOKEN", ""),
                     arbiter_url=args.arbiter_url, journal_unit=args.journal_unit)
    if not args.confirm_clear:
        _say("the head moves and the speaker plays aloud: confirm with --confirm-clear")
        return 2
    if not args.records_ok:
        _say("a robot run posts a synthetic completion under the badge it uses, which writes "
             "dashboard rows, results mail and FIWARE entities: use a test badge with mail and "
             "FIWARE off, and confirm with --records-ok")
        return 2
    idle = _robot_idle(args.base_url)
    if idle is not None:
        _say(idle)
        return 2
    if not args.badge:
        _say("name a badge on this host's roster with --badge")
        return 2
    if sync_run.Machine(root).unit_state(sync_run.KIOSK_UNIT).get("ActiveState") == "active":
        _say(f"{sync_run.KIOSK_UNIT} is running and would drain the page's verbs: stop it first")
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)
    track = RobotTrack(root=root, run_dir=run_dir, sink=args.voice_sink)
    try:
        record_run(scenario, host=host, run_dir=run_dir, label=args.label, lang=args.lang,
                   badge=args.badge, second_badge=args.second_badge, track=track, root=root,
                   recorders=addon_recorders(ROOT))
    except Exception as error:  # noqa: BLE001 - one failed run must not lose the others
        _say(f"  failed: {error}")
        return 1
    return 0


def cmd_sim_run(args: argparse.Namespace) -> int:
    try:
        sim_run(args.scenario, Path(args.run_dir), label=args.label, fast=args.fast, lang=args.lang)
    except Exception as error:  # noqa: BLE001 - the parent reports the run as failed
        _say(f"  failed: {type(error).__name__}: {error}")
        return 1
    return 0


# ---------------------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------------------


def _print_report(report: comparing.Report) -> None:
    if report.ok:
        _say(f"{report.scenario}: same")
    else:
        _say(f"{report.scenario}: {len(report.violations)} violations")
        for violation in report.first_per_stream():
            _say(f"  {violation}")
    for note in report.notes:
        _say(f"  note: {note}")


def cmd_compare(args: argparse.Namespace) -> int:
    side_a, side_b = _side(args.a, args.root), _side(args.b, args.root)
    tolerances = _tolerances(args.tolerance)
    names = ([args.scenario] if args.scenario
             else sorted(set(stored.scenarios_in(side_a)) | set(stored.scenarios_in(side_b))))
    if not names:
        _say(f"no runs under {side_a} or {side_b}")
        return 1
    failed = False
    for name in names:
        report = comparing.compare_scenario(name, stored.load_side(side_a, name),
                                            stored.load_side(side_b, name), tolerances,
                                            exact_only=args.exact_only)
        _print_report(report)
        failed = failed or not report.ok
    return 1 if failed else 0


def cmd_spread(args: argparse.Namespace) -> int:
    side = _side(args.a, args.root)
    tolerances = _tolerances(args.tolerance)
    failed = False
    for name in ([args.scenario] if args.scenario else stored.scenarios_in(side)):
        lines, violations = comparing.spread_report(name, stored.load_side(side, name), tolerances)
        _say(f"{name}:")
        for line in lines:
            _say(f"  {line}")
        for violation in violations:
            _say(f"  {violation}")
        failed = failed or bool(violations)
    return 1 if failed else 0


def cmd_list(args: argparse.Namespace) -> int:
    for name, scenario in all_scenarios(ROOT).items():
        modes = "offline" if scenario.offline else ",".join(scenario.modes)
        _say(f"{name:24} {modes:12} {scenario.description.splitlines()[0] if scenario.description else ''}")
    return 0


# ---------------------------------------------------------------------------------------
# self-check
# ---------------------------------------------------------------------------------------


def _break_copy(source: Path, target: Path, name: str) -> None:
    """Copy one run, making the one mistake ``name`` stands for."""

    shutil.copytree(source, target)
    client = stored.read_jsonl(target / "client.jsonl")
    if name == "lost_recorded_moves":
        client = [r for r in client if r.get("method") != "play_recorded_move"]
    elif name == "lost_gaze_pushes":
        client = [r for r in client if r.get("method") != "set_gaze"]
    elif name == "changed_duration":
        for record in client:
            intent = (record.get("args") or {}).get("intent")
            if intent and intent.get("kind") == "goto":
                intent["duration"] = float(intent["duration"]) + 0.25
                break
    elif name == "changed_answer":
        answers = stored.read_jsonl(target / "http.jsonl")
        for record in answers:
            if record.get("status") == 200:
                record["status"] = 500
                break
        stored.write_jsonl(target / "http.jsonl", answers)
    elif name == "slower_moves":
        # Every move a second later than it was, and in the same order.
        for record in client:
            if record.get("t_start") is not None and record.get("method") == "apply":
                record["t_start"] = float(record["t_start"]) + 1.0
    stored.write_jsonl(target / "client.jsonl", client)


def cmd_self_check(args: argparse.Namespace) -> int:
    tolerances = _tolerances(None)
    scratch = Path(tempfile.mkdtemp(prefix="golden-self-check-"))
    try:
        side = scratch / "a" / SELF_CHECK_SCENARIO
        with ThreadPoolExecutor(2) as pool:
            codes = list(pool.map(
                lambda index: _spawn_sim_run(SELF_CHECK_SCENARIO, side / f"run-{index:03d}",
                                             label="self-check", fast=True, lang="en"), (1, 2)))
        if any(codes):
            _say("self-check: a recording failed")
            return 1
        runs = stored.load_side(scratch / "a", SELF_CHECK_SCENARIO)
        same = comparing.compare_scenario(SELF_CHECK_SCENARIO, runs[:1], runs[1:], tolerances,
                                          exact_only=True, min_runs=1)
        _print_report(same)
        if not same.ok:
            return 1
        failed = False
        for mistake in ("lost_recorded_moves", "lost_gaze_pushes", "changed_duration",
                        "changed_answer"):
            broken = scratch / mistake / SELF_CHECK_SCENARIO / "run-001"
            _break_copy(side / "run-002", broken, mistake)
            report = comparing.compare_scenario(
                SELF_CHECK_SCENARIO, runs[:1], stored.load_side(scratch / mistake, SELF_CHECK_SCENARIO),
                tolerances, exact_only=True, min_runs=1)
            _say(f"self-check {mistake}: {'caught' if not report.ok else 'NOT CAUGHT'}")
            failed = failed or report.ok
        # Timing is compared separately from structure, so it gets a mistake of its own.
        broken = scratch / "slower_moves" / SELF_CHECK_SCENARIO / "run-001"
        _break_copy(side / "run-002", broken, "slower_moves")
        report = comparing.compare_scenario(
            SELF_CHECK_SCENARIO, runs[:1],
            stored.load_side(scratch / "slower_moves", SELF_CHECK_SCENARIO),
            tolerances, min_runs=1)
        timing = [v for v in report.violations if v.stream not in ("plan", "http")]
        _say(f"self-check slower_moves: {'caught' if timing else 'NOT CAUGHT'}")
        failed = failed or not timing
        return 1 if failed else 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_RUNS_ROOT, help="where runs are kept")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="the scenarios").set_defaults(func=cmd_list)

    record = commands.add_parser("record", help="record runs of a scenario")
    record.add_argument("--label", required=True)
    record.add_argument("--scenario", required=True, help="a name, or all")
    record.add_argument("--runs", type=int, default=1)
    record.add_argument("--lang", default="en")
    record.add_argument("--fast", action="store_true",
                        help="simulator only: compress waits; compare only with other --fast runs")
    record.add_argument("--base-url", help="a running host; omit for the simulator")
    record.add_argument("--host-root", default=".", help="the host's working directory")
    record.add_argument("--client-dir", help="the recording launcher's output directory")
    record.add_argument("--restart-command", help="how to restart the host, for host_restart")
    record.add_argument("--arbiter-url", default="", help="read the arbiter's trace from here")
    record.add_argument("--journal-unit", default="", help="read mic changes from this unit's journal")
    record.add_argument("--voice-sink", default="", help="play the voice into this sink")
    record.add_argument("--badge", default="", help="a badge on this host's roster")
    record.add_argument("--second-badge", default="")
    record.add_argument("--records-ok", action="store_true",
                        help="a robot run writes dashboard rows, mail and FIWARE entities under "
                             "its badge, which is a test one, with mail and FIWARE off")
    record.add_argument("--confirm-clear", action="store_true",
                        help="on a robot: the head is free and nobody is touching it")
    record.set_defaults(func=cmd_record)

    compare = commands.add_parser("compare", help="compare two labels")
    compare.add_argument("--a", required=True, help="reference: a label or a path")
    compare.add_argument("--b", required=True, help="under test: a label or a path")
    compare.add_argument("--scenario")
    compare.add_argument("--tolerance", action="append", help="another tolerance file")
    compare.add_argument("--exact-only", action="store_true",
                         help="skip timing: for machines that say nothing about it")
    compare.set_defaults(func=cmd_compare)

    spread = commands.add_parser("spread", help="how far one label's own runs stray")
    spread.add_argument("--a", required=True)
    spread.add_argument("--scenario")
    spread.add_argument("--tolerance", action="append")
    spread.set_defaults(func=cmd_spread)

    commands.add_parser("self-check", help="prove the harness catches what it claims to"
                        ).set_defaults(func=cmd_self_check)

    sim = commands.add_parser("_sim-run", help=argparse.SUPPRESS)
    sim.add_argument("--scenario", required=True)
    sim.add_argument("--run-dir", required=True)
    sim.add_argument("--label", default="")
    sim.add_argument("--lang", default="en")
    sim.add_argument("--fast", action="store_true")
    sim.set_defaults(func=cmd_sim_run)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
