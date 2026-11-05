"""The three publishable ROS 2 packages must build and run with no EMORI core present.

`hri_context_analyzer`, `hri_engagement_behaviors` and `emori_skill_facade` are the
packages we intend to release into the ROS 4 HRI ecosystem: Apache-2.0, `bloom`-ready,
installable by somebody who has never heard of Pokamind. `deploy/ros2/Dockerfile.standalone`
builds exactly those three plus `emori_msgs` on a plain Humble, with **no** `pip install`
of the adapter - so a single `from emori_adapter...` line anywhere inside them is an
`ImportError` at node start in that image.

That failure is quiet in the worst way. The package builds (colcon never imports the
node), `ros2 pkg list` shows it, and the only symptom is a node missing from
`ros2 node list` several seconds after launch, which reads exactly like a slow start.
It is also the one rule a routine refactor breaks without noticing: importing a helper
from the core is the obvious thing to do everywhere else in this repository.

The check is `ast`, not `grep`: the module docstrings discuss `emori_adapter` at length,
and prose about a package is not a dependency on it.

`emori_adapter_ros` is the deliberate exception and is asserted to be one. It is the
BRIDGE - it consumes the core's `Translator` and `AnalysisCycle` and turns them into
messages - so it ships in the full image only, and its import of the core is the thing
that makes the other three able to do without one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROS_WS = Path(__file__).resolve().parents[1] / "ros2_ws" / "src"
# The directory split: `open/` packages stand alone, `emori/` packages use the adapter core.
_OPEN = _ROS_WS / "open"
_EMORI = _ROS_WS / "emori"

_CORE = "emori_adapter"

# The packages that must stand alone.
_STANDALONE_PACKAGES = (
    "hri_context_analyzer",
    "hri_engagement_behaviors",
    "emori_skill_facade",
)

# The bridge, which is allowed - and required - to import the core.
_BRIDGE_PACKAGE = "emori_adapter_ros"


def _imports(source: Path) -> set[str]:
    """Every module name `source` imports, dotted, as written."""

    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _package_dir(package: str) -> Path:
    """The package's directory, in whichever half of the workspace it lives."""

    for half in (_OPEN, _EMORI):
        if (half / package).is_dir():
            return half / package
    return _OPEN / package  # absent: the callers' is_dir() checks report it


def _package_sources(package: str) -> list[Path]:
    """Every `.py` in the package, `setup.py` and launch files included."""

    return sorted(p for p in _package_dir(package).rglob("*.py")
                  if "__pycache__" not in p.parts)


def _core_imports(package: str) -> list[str]:
    offenders: list[str] = []
    for source in _package_sources(package):
        for module in sorted(_imports(source)):
            if module == _CORE or module.startswith(f"{_CORE}."):
                offenders.append(f"{source.relative_to(_ROS_WS)}: import {module}")
    return offenders


@pytest.mark.parametrize("package", _STANDALONE_PACKAGES)
def test_a_publishable_package_imports_nothing_from_the_adapter_core(package):
    """One `from emori_adapter...` is a node that dies at startup in the release image."""

    directory = _OPEN / package
    assert directory.is_dir(), f"{package} is not under ros2_ws/src/open/ - has it moved?"

    sources = _package_sources(package)
    assert sources, f"{package} contains no Python at all - has the package moved?"

    offenders = _core_imports(package)
    assert offenders == [], (
        f"{package} must build with no EMORI core installed, but imports it: {offenders}")


@pytest.mark.parametrize("package", ("reachy_mini_ros2", "reachy_mini_msgs"))
def test_the_native_robot_packages_import_nothing_from_the_adapter_core(package):
    """v2 puts the robot itself in the graph, and the robot is Pollen's, not EMORI's.

    `reachy_mini_ros2` owns Pollen's backend in-process, the motion arbiter and the REST
    compatibility shim; `reachy_mini_msgs` is its interface package. Neither may import
    `emori_adapter` - not because of a release decision, but because they are a *robot
    driver*: somebody with a Reachy Mini and no EMORI at all must be able to run them,
    and the arbiter's rules must not quietly depend on an adapter's opinion.

    The one package that legitimately bridges the two is `emori_skill_native` (the
    `ros_native` transport, which reuses the hostapp's own skill-to-intent mapping on
    purpose). It is a fourth package precisely so this line stays drawable, and the test
    below asserts that it is the exception rather than an oversight.
    """

    directory = _OPEN / package
    assert directory.is_dir(), f"{package} is not under ros2_ws/src/open/ - has it moved?"

    offenders = _core_imports(package)
    assert offenders == [], (
        f"{package} is a robot driver and must run with no EMORI core installed, but "
        f"imports it: {offenders}. The bridge is emori_skill_native.")


def test_nothing_in_the_open_half_imports_the_adapter_core():
    """`ros2_ws/src/open/` is the standalone half, so the rule holds for the directory.

    The two tests above name their packages; this one names none, so a package that
    lands in `open/` is held to the rule from its first commit rather than from the day
    somebody remembers to list it.
    """

    packages = sorted(d.name for d in _OPEN.iterdir() if (d / "package.xml").is_file())
    assert "hri_context_analyzer" in packages, "open/ is empty - has the workspace moved?"
    offenders = [line for package in packages for line in _core_imports(package)]
    assert offenders == [], f"the open half imports the adapter core: {offenders}"


def _core_half_modules() -> set[str]:
    """The top-level Python modules the `emori/` half provides, the core included.

    Read off the tree rather than listed, so a fifth `emori/` package is covered the
    day it lands: every package's own name, plus every importable directory directly
    inside it (for ament_python the two are the same name, but nothing enforces that).
    """

    modules = {_CORE}
    for package in _EMORI.iterdir():
        if not (package / "package.xml").is_file():
            continue
        modules.add(package.name)
        modules |= {child.name for child in package.iterdir()
                    if (child / "__init__.py").is_file()}
    return modules


def _imports_of(package_dir: Path, forbidden: set[str]) -> list[str]:
    """Every import under `package_dir` whose top-level module is in `forbidden`."""

    offenders: list[str] = []
    for source in sorted(p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts):
        for module in sorted(_imports(source)):
            if module.split(".")[0] in forbidden:
                where = source.relative_to(package_dir.parent).as_posix()
                offenders.append(f"{where}: import {module}")
    return offenders


def test_nothing_in_the_open_half_imports_an_emori_ros_package():
    """`emori_adapter` is not the only forbidden module: the `emori/` packages are too.

    `import emori_skill_native` or `from fiware_bridge import ...` in an open node is
    the same broken promise as importing the core - the node cannot run where only the
    open half is installed - and the core-only check above would wave it through. The
    dependency may point the other way (`emori_skill_native` registers itself with the
    facade through an entry point), never this way.
    """

    forbidden = _core_half_modules()
    assert {"emori_adapter_ros", "fiware_bridge", "emori_skill_native"} <= forbidden, (
        f"ros2_ws/src/emori/ no longer provides the expected modules: {sorted(forbidden)}")
    packages = sorted(d for d in _OPEN.iterdir() if (d / "package.xml").is_file())
    offenders = [line for package in packages for line in _imports_of(package, forbidden)]
    assert offenders == [], f"the open half imports the emori/ half: {offenders}"


def test_the_emori_half_import_check_sees_a_planted_import(tmp_path):
    """The check above is only worth its green if it can go red."""

    node = tmp_path / "pkg" / "pkg" / "node.py"
    node.parent.mkdir(parents=True)
    node.write_text('"""Mentions fiware_bridge in prose, which is fine."""\n'
                    "import os\n"
                    "from emori_skill_native.transport import NativeTransport\n",
                    encoding="utf-8")
    offenders = _imports_of(tmp_path / "pkg", _core_half_modules())
    assert offenders == ["pkg/pkg/node.py: import emori_skill_native.transport"]


def test_the_native_transport_is_the_bridge_v2_adds_and_is_not_publishable():
    """`emori_skill_native` is allowed - required - to import the core, like the bridge.

    It exists to turn a bounded skill request into `GoTo` goals using
    `skill_to_intents`, the SAME mapping the hostapp's intake uses, so the robot
    performs the same gesture whichever side asked for it. If it ever stopped importing
    the core it would be re-deriving that mapping, and the two sides would drift apart
    exactly where nobody is watching.
    """

    package = "emori_skill_native"
    assert (_EMORI / package).is_dir(), f"{package} is not under ros2_ws/src/emori/"

    assert package not in _STANDALONE_PACKAGES, (
        f"{package} imports the adapter core by design and cannot be published as a "
        "standalone ROS 4 HRI package; it must not join the release set")
    offenders = _core_imports(package)
    assert offenders, (f"{package} no longer imports {_CORE}: either the transport moved "
                       "or it is re-deriving the hostapp's skill mapping instead of "
                       "sharing it")


def test_the_bridge_is_the_one_package_that_may_import_the_core():
    """The exception, asserted so it stays a decision rather than an oversight.

    `emori_adapter_ros` exists precisely to turn the core's `AnalysisCycle` into
    `emori_msgs`; if it ever stopped importing the core, the three packages above would
    have nothing to be standalone *from* and this whole file would be vacuously green.
    """

    offenders = _core_imports(_BRIDGE_PACKAGE)
    assert offenders, (f"{_BRIDGE_PACKAGE} no longer imports {_CORE}: either the bridge "
                       "moved or the graph now has no source for its messages")


def test_the_standalone_packages_are_the_ones_the_release_names():
    """A fourth package quietly joining the release set must reach this list too."""

    present = {name for name in _STANDALONE_PACKAGES if (_OPEN / name).is_dir()}
    assert "hri_context_analyzer" in present
    assert _BRIDGE_PACKAGE not in _STANDALONE_PACKAGES
    # v2 adds three packages and none of them belongs here: the two robot ones are
    # Pollen's driver (published on their own terms, if ever) and `emori_skill_native`
    # is EMORI-specific by construction. Listing them would make this file assert the
    # opposite of what the two tests above assert about each of them.
    for v2_package in ("reachy_mini_ros2", "reachy_mini_msgs", "emori_skill_native"):
        assert v2_package not in _STANDALONE_PACKAGES
