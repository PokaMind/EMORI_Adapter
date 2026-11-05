"""``deploy/robot/emori-hostapp.service`` bounds a stop at 12 s, and the shutdown fits in it.

systemd's default 90 s only ever waited on a wedged audio stream: the host app's own
teardown joins every worker with a timeout and finalizes nothing on SIGTERM (a session
left active is closed as ``interrupted`` by the next start). 90 s also outlasts the 30 s
emori-admin gives a restart from the phone. A robot in service runs the same 12 s.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

_REPO = Path(__file__).resolve().parents[1]
_UNIT = _REPO / "deploy" / "robot" / "emori-hostapp.service"
_SERVER = _REPO / "src" / "emori_adapter" / "hostapp" / "server.py"


def _stop_timeout_s() -> float:
    service = _UNIT.read_text(encoding="utf-8").split("[Service]", 1)[1].split("[Install]")[0]
    found = re.findall(r"^TimeoutStopSec=([0-9]+)$", service, re.M)
    assert len(found) == 1, "emori-hostapp.service must set TimeoutStopSec once, in [Service]"
    return float(found[0])


def _join_budget(nodes, multiplier: int = 1) -> float:
    """Every literal ``timeout=``/``timeout_s=`` a teardown passes, times the loops around it."""

    total = 0.0
    for node in nodes:
        if isinstance(node, (ast.For, ast.AsyncFor)):
            times = len(node.iter.elts) if isinstance(node.iter, (ast.Tuple, ast.List)) else 1
            total += _join_budget(node.body, multiplier * times)
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                for keyword in child.keywords:
                    if (keyword.arg in ("timeout", "timeout_s")
                            and isinstance(keyword.value, ast.Constant)):
                        total += float(keyword.value.value) * multiplier
            total += _join_budget([child], multiplier) if not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else 0.0
    return total


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    assert found, f"{path.name} has no {name}()"
    return found[0]


def test_the_hostapp_unit_bounds_a_stop_at_twelve_seconds():
    assert _stop_timeout_s() == 12.0


def test_the_bounded_teardown_fits_inside_the_stop_timeout():
    lifespan = _function(_SERVER, "lifespan")
    teardown = [node for node in ast.walk(lifespan) if isinstance(node, ast.Try)
                and node.finalbody]
    assert teardown, "lifespan() no longer tears down in a finally block"
    budget = _join_budget(teardown[0].finalbody)

    # scope sync 1 + four tickers x 1 + driver 1 + player 2.
    assert budget == 8.0
    # Room left for uvicorn to close its connections first. A new join that eats it has
    # to raise TimeoutStopSec with it, or a healthy stop gets SIGKILLed.
    assert budget <= _stop_timeout_s() - 3.0
