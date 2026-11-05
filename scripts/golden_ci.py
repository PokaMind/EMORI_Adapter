"""CI's use of the committed golden recordings.

    python scripts/golden_ci.py status
    python scripts/golden_ci.py check [--require-recordings]

The goldens are runs recorded by ``scripts/golden_trace.py record`` and committed under
``tests/fixtures/golden/<scenario>/run-NNN/``, in the layout that command writes. ``check``
records the same scenarios again from this checkout, with the options each golden run's
``meta.json`` names (time compressed or not, language), and compares them with
``golden_trace.py compare --exact-only``: a CI machine says nothing about timing, so only
the structure of what the robot is asked to do is judged.

With no golden recorded, ``check`` says so and exits 0, unless ``--require-recordings`` is
given: CI passes it, so that goldens lost from the tree fail the job instead of passing it
without a comparison. A golden is never written by hand or
by this script: it comes from a recording someone looked at and decided to keep. A scenario
the harness does not know, or a golden run with no ``meta.json``, fails the check, because a
golden that cannot be compared must not read as a pass.

Exit: 0 compared and equal, or nothing recorded (without ``--require-recordings``) | 1 a
difference, a golden that cannot be used, or nothing recorded (with it).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from golden import runs as stored  # noqa: E402

DEFAULT_GOLDENS = ROOT / "tests" / "fixtures" / "golden"
DEFAULT_LABEL = "ci"
GOLDEN_TRACE = Path(__file__).resolve().parent / "golden_trace.py"


@dataclass(frozen=True)
class Golden:
    """One recorded scenario: how many runs, and how they were recorded."""

    scenario: str
    runs: int
    fast: bool
    lang: str


class GoldenError(Exception):
    """A golden that cannot be compared."""


def recorded(goldens: Path) -> list[Golden]:
    """The scenarios with at least one run under ``goldens``, with how each was recorded."""

    found = []
    for scenario in stored.scenarios_in(goldens):
        directories = sorted(path for path in (goldens / scenario).glob("run-*") if path.is_dir())
        metas = []
        for directory in directories:
            meta_path = directory / "meta.json"
            if not meta_path.is_file():
                raise GoldenError(f"{directory}: no meta.json")
            try:
                metas.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except ValueError as error:
                raise GoldenError(f"{meta_path}: {error}") from error
        for key in ("fast", "lang"):
            if len({repr(meta.get(key)) for meta in metas}) > 1:
                raise GoldenError(f"{scenario}: its runs disagree on {key}")
        first = metas[0]
        found.append(Golden(scenario=scenario, runs=len(directories),
                            fast=bool(first.get("fast", False)), lang=str(first.get("lang", "en"))))
    return found


def _known_scenarios() -> set[str]:
    from golden.scenarios import all_scenarios

    return set(all_scenarios(ROOT))


def _trace(root: Path, *arguments: str) -> int:
    command = [sys.executable, str(GOLDEN_TRACE), "--root", str(root), *arguments]
    return subprocess.run(command, check=False).returncode


def check(goldens: Path, root: Path, label: str, *,
          known: Optional[Callable[[], set[str]]] = None,
          trace: Callable[..., int] = _trace, require: bool = False) -> int:
    """Record each golden scenario again and compare. See the module docstring."""

    try:
        found = recorded(goldens)
    except GoldenError as error:
        print(f"golden: {error}")
        return 1
    if not found:
        print(f"golden: nothing is recorded under {goldens}; no comparison was made")
        return 1 if require else 0
    names = (known or _known_scenarios)()
    unknown = [golden.scenario for golden in found if golden.scenario not in names]
    if unknown:
        print(f"golden: the harness has no scenario named {', '.join(unknown)}")
        return 1
    failed = False
    for golden in found:
        print(f"golden: {golden.scenario}: {golden.runs} run(s), "
              f"{'compressed' if golden.fast else 'real time'}, lang {golden.lang}")
        record = ["record", "--label", label, "--scenario", golden.scenario,
                  "--runs", str(golden.runs), "--lang", golden.lang]
        if golden.fast:
            record.append("--fast")
        if trace(root, *record) != 0:
            print(f"golden: {golden.scenario}: the recording failed")
            failed = True
            continue
        compared = trace(root, "compare", "--a", str(goldens), "--b", label,
                         "--scenario", golden.scenario, "--exact-only")
        failed = failed or compared != 0
    return 1 if failed else 0


def status(goldens: Path) -> int:
    try:
        found = recorded(goldens)
    except GoldenError as error:
        print(f"golden: {error}")
        return 1
    if not found:
        print(f"golden: nothing is recorded under {goldens}")
    for golden in found:
        print(f"golden: {golden.scenario}: {golden.runs} run(s)")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("status", "check"))
    parser.add_argument("--goldens", type=Path, default=DEFAULT_GOLDENS)
    parser.add_argument("--root", type=Path, default=None,
                        help="where the new recordings go (a temporary directory by default)")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--require-recordings", action="store_true",
                        help="check: fail when no golden is recorded, instead of passing")
    args = parser.parse_args(argv)
    if args.command == "status":
        return status(args.goldens)
    if args.root is not None:
        return check(args.goldens, args.root, args.label, require=args.require_recordings)
    with tempfile.TemporaryDirectory(prefix="golden-ci-") as scratch:
        return check(args.goldens, Path(scratch), args.label, require=args.require_recordings)


if __name__ == "__main__":
    sys.exit(main())
