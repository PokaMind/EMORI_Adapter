"""The robot's own Pokamind token is its ONLY credential.

It signs every learner login. The guard below walks every Python file the robot or an
operator runs and refuses:

* any use of a retired credential: a ``robot_secret`` setting, a shared-secret minter
  or verifier (``mint_robot_assertion``, ``mint_partner_jwt``, ``verify_partner_jwt``),
  or ``EMORI_ROBOT_SECRET`` / ``EMORI_PARTNER_SECRET`` read from an environment;
* any reference to ``PokamindControlClient``: the control-plane client belongs to an
  optional add-on, never to the kiosk, the scripts or the deploy kit.

A tool that needs a learner login signs it with ``jwt_sso.mint_robot_sso_jwt`` and this
robot's token.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SCANNED_TREES = ("src", "scripts", "deploy", "ros2_ws")
_SKIPPED_DIRS = frozenset(
    {"__pycache__", ".pixi", ".venv", "site-packages", "node_modules", "build", "install",
     "log"}
)

# Names that exist only for a retired shared-secret credential.
_RETIRED_NAMES = frozenset({"robot_secret", "mint_robot_assertion", "mint_partner_jwt",
                            "verify_partner_jwt"})
_RETIRED_KEYS = frozenset({"EMORI_ROBOT_SECRET", "EMORI_PARTNER_SECRET"})


def _python_files() -> list[Path]:
    files = []
    for tree in _SCANNED_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            if not _SKIPPED_DIRS.intersection(path.relative_to(ROOT).parts):
                files.append(path)
    return files


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """ids of every node inside an annotation: naming the type there builds nothing."""

    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                roots.append(node.returns)
            arguments = node.args
            for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                        arguments.vararg, arguments.kwarg):
                if arg is not None and arg.annotation is not None:
                    roots.append(arg.annotation)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(child) for root in roots for child in ast.walk(root)}


def _names(node: ast.AST) -> str | None:
    """The identifier a Name/Attribute load refers to, else None."""

    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        return node.attr
    return None


def _constant_args(call: ast.Call) -> list[str]:
    return [arg.value for arg in call.args
            if isinstance(arg, ast.Constant) and type(arg.value) is str]


def credential_findings(source: str, rel: str) -> list[str]:
    """Every credential use in ``source`` (a file at repo-relative ``rel``) the rules refuse."""

    tree = ast.parse(source, filename=rel)
    annotations = _annotation_nodes(tree)
    findings: list[str] = []

    def visit(node: ast.AST) -> None:
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        name = _names(node)
        if name == "PokamindControlClient" and id(node) not in annotations:
            findings.append(f"{where}: PokamindControlClient in the public tree")
        if name in _RETIRED_NAMES:
            findings.append(f"{where}: retired shared-secret credential {name}")
        if isinstance(node, ast.Call):
            literals = _constant_args(node)
            if (isinstance(node.func, ast.Name) and node.func.id == "getattr"
                    and "robot_secret" in literals):
                findings.append(f"{where}: getattr(..., 'robot_secret')")
            for key in sorted(_RETIRED_KEYS.intersection(literals)):
                findings.append(f"{where}: retired credential {key} read")
        if isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and key.value in _RETIRED_KEYS:
                findings.append(f"{where}: retired credential {key.value} read")
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return findings


def test_no_module_builds_a_control_client_or_reads_a_credential():
    files = _python_files()
    assert any(path.name == "rig_smoke.py" for path in files)  # scripts are scanned

    findings = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        findings.extend(credential_findings(path.read_text(encoding="utf-8"), rel))

    assert findings == []


@pytest.mark.parametrize("snippet", [
    "client = pokamind.PokamindControlClient(url, rid, secret)\n",
    "PokamindControlClient(url, robot_id, None, robot_token=token)\n",
    "secret = getattr(settings, 'robot_secret')\n",
    "secret = os.environ['EMORI_ROBOT_SECRET']\n",
    "secret = os.environ.get('EMORI_ROBOT_SECRET', '')\n",
    "assertion = mint_robot_assertion(robot_id, secret)\n",
    "token = mint_partner_jwt(os.getenv('EMORI_PARTNER_SECRET'), **claims)\n",
    "claims = verify_partner_jwt(token, secret)\n",
    "secret = settings.robot_secret\n",
])
def test_the_guard_bites_on_a_new_tool(snippet):
    assert credential_findings(snippet, "scripts/some_new_tool.py")


def test_naming_the_type_in_an_annotation_is_not_a_construction():
    snippet = ("def use(client: PokamindControlClient) -> PokamindControlClient:\n"
               "    return client\n"
               "holder: PokamindControlClient | None = None\n")

    assert credential_findings(snippet, "src/emori_adapter/hostapp/elsewhere.py") == []


def test_no_shell_or_powershell_tool_hand_signs_a_robot_credential():
    """Shell tools embed Python too: a hand-rolled ``jwt.encode`` is the same regression.

    No shell or PowerShell tool under ``scripts/`` or ``deploy/`` may sign a robot
    assertion itself; every tool goes through the one builder.
    """

    allowed: set[str] = set()
    offenders = []
    for tree in ("scripts", "deploy"):
        for path in sorted((ROOT / tree).rglob("*")):
            if (not path.is_file() or path.suffix not in {".ps1", ".sh", ".cmd", ".bash"}
                    or _SKIPPED_DIRS.intersection(path.relative_to(ROOT).parts)):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if "jwt.encode(" in path.read_text(encoding="utf-8", errors="replace") \
                    and rel not in allowed:
                offenders.append(rel)

    assert offenders == []
