"""The four pure gaze modules must load on a machine with no ROS. Enforced, not hoped.

The gaze package is deliberately split: ``vision``, ``tracking``, ``geometry`` and
``controller`` are pure and host-testable, and ``component`` is the one file that
imports ``rclpy`` and ``hri_msgs``. Two things can quietly destroy that split, and both
are one line:

* an ``__init__.py`` that re-exports ``GazeComponent`` for convenience - which makes
  ``import reachy_mini_ros2.gaze.tracking`` pull in ``rclpy``, so every host test of
  the selection rules fails at import on a box that has never had ROS;
* a ``from ..conversions import ...`` in ``controller.py`` - which cannot resolve at
  all when the module is loaded BY PATH, and by-path loading is how these tests reach
  ``ros2_ws`` without putting it on ``sys.path`` (``tests/test_reachy_video.py``:9-10).

Neither failure is subtle once it happens. Both are invisible in review.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys

import pytest

_PACKAGE = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
            / "reachy_mini_ros2" / "gaze")
_PURE = ("vision", "tracking", "geometry", "controller")

#: Everything the pure four are allowed to need. `rclpy`, `hri_msgs`, `gi` and
#: `reachy_mini` are the four that would end host testing, and none of them is here.
_ALLOWED = {
    "__future__", "dataclasses", "typing", "math", "numpy", "scipy", "onnxruntime",
    "importlib", "pathlib", "sys",
}


class _Blocked(ImportError):
    pass


class _BlockingFinder:
    """A meta-path finder that refuses the modules a dev box does not have.

    Stronger than checking `sys.modules`: it fails the import at the moment it is
    attempted, so a module that reaches for `rclpy` inside a function body during
    import-time evaluation is caught too.
    """

    def __init__(self, blocked: tuple[str, ...]) -> None:
        self.blocked = blocked

    def find_module(self, fullname, path=None):        # pragma: no cover - legacy hook
        return None

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in self.blocked:
            raise _Blocked(f"{fullname} is blocked: this module must load without it")
        return None


@pytest.fixture
def no_ros():
    finder = _BlockingFinder(("rclpy", "hri_msgs", "gi", "reachy_mini",
                              "reachy_mini_msgs"))
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)


def _source(name: str) -> str:
    return (_PACKAGE / f"{name}.py").read_text(encoding="utf-8")


# --- 1. the package init ---------------------------------------------------------

def test_the_package_init_is_empty():
    """Empty, deliberately. An `__init__` that imported `component` for convenience
    would make `rclpy` a hard dependency of every module in the package."""

    source = (_PACKAGE / "__init__.py").read_text(encoding="utf-8")
    assert source.strip() == ""
    assert not [node for node in ast.parse(source).body
                if isinstance(node, (ast.Import, ast.ImportFrom))]


# --- 2. no relative imports ------------------------------------------------------

@pytest.mark.parametrize("name", _PURE)
def test_no_relative_imports_anywhere_in_the_pure_modules(name):
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"gaze/{name}.py uses a relative import, which cannot resolve when the "
                "module is loaded by path")


@pytest.mark.parametrize("name", _PURE)
def test_the_pure_modules_import_only_what_a_dev_box_has(name):
    top_level: set[str] = set()
    for node in ast.parse(_source(name)).body:
        if isinstance(node, ast.Import):
            top_level |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert top_level <= _ALLOWED, f"gaze/{name}.py imports {top_level - _ALLOWED}"


# --- 3. they actually load, with ROS blocked -------------------------------------

@pytest.mark.parametrize("name", _PURE)
def test_each_pure_module_loads_with_ros_blocked(no_ros, name):
    assert "rclpy" not in sys.modules
    module_name = f"reachy_mini_gaze_import_probe_{name}"
    spec = importlib.util.spec_from_file_location(module_name, _PACKAGE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    assert "rclpy" not in sys.modules


def test_the_controller_still_finds_the_conversions_module(no_ros):
    """The fallback path: `conversions.py` is loaded by FILE when the package is not
    installed, so `matrix_to_rotation_rpy` is still the one place the three pose
    representations meet rather than a second copy of its gimbal-lock guard."""

    spec = importlib.util.spec_from_file_location(
        "reachy_mini_gaze_controller_conversion_probe", _PACKAGE / "controller.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert callable(module._matrix_to_rotation_rpy)
        assert callable(module._xyzrpy_to_matrix)
        assert module._matrix_to_rotation_rpy(module._xyzrpy_to_matrix(
            0, 0, 0, 0.1, -0.2, 0.3)) == pytest.approx((0.1, -0.2, 0.3))
    finally:
        sys.modules.pop(spec.name, None)


# --- 4. the component is the one file that may need ROS --------------------------

def test_the_component_is_the_only_module_that_imports_ros():
    """Named here so the split is documented by a test rather than by a convention
    somebody has to remember."""

    imports: set[str] = set()
    for node in ast.parse(_source("component")).body:
        if isinstance(node, ast.Import):
            imports |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert "rclpy" in imports
    assert "hri_msgs" in imports
