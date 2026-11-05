"""Installing an add-on for the hub container: deploy/hub/hub-addons.sh and addon_dist.py.

The script runs in bash against stubs for ``sudo`` and ``docker``. ``addon_dist.py`` runs for
real, and a child interpreter that only knows the directory proves the core's discovery finds
what it wrote. The add-on here is made up for the test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

# addon_dist.py reads a pyproject with tomllib. It runs on the robot, on a newer Python.
pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason="needs tomllib")

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "hub" / "hub-addons.sh"
_DIST = _ROOT / "deploy" / "hub" / "addon_dist.py"
_SHELL = shutil.which("bash")
_PYPROJECT = """
[project]
name = "Sample-Addon"
version = "1.2.3"

[project.entry-points."emori_adapter.extensions"]
sample = "sample_addon.extension:SampleExtension"

[tool.setuptools.packages.find]
where = ["src"]
"""
_LIB = Path("lib/python3.12/site-packages")

needs_shell = pytest.mark.skipif(_SHELL is None, reason="no shell")


def _write_addon(root: Path, name: str = "sample", *, pins: bool = True) -> Path:
    addon = root / "addons" / name
    package = addon / "src" / "sample_addon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "extension.py").write_text("class SampleExtension:\n    api_version = 1\n",
                                          encoding="utf-8")
    (addon / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    if pins:
        (addon / "deploy").mkdir()
        (addon / "deploy" / "hub-constraints.txt").write_text("thing==1.0\n", encoding="utf-8")
    return addon


# --- what addon_dist writes ------------------------------------------------------------------
def test_what_it_writes_is_found_and_imported_from_the_directory_alone(tmp_path):
    addon = _write_addon(tmp_path)
    site_dir = tmp_path / "site-packages"
    done = subprocess.run([sys.executable, str(_DIST), str(addon), str(site_dir)],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr

    probe = (
        "import json, site, sys\n"
        "from importlib import metadata\n"
        "site.addsitedir(sys.argv[1])\n"
        "entry = metadata.entry_points(group='emori_adapter.extensions')['sample']\n"
        "print(json.dumps({'name': entry.load().__name__, 'api': entry.load().api_version,"
        " 'version': metadata.version('sample-addon')}))\n")
    child = subprocess.run([sys.executable, "-c", probe, str(site_dir)], capture_output=True,
                           text=True, check=False, cwd=tmp_path)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == {"name": "SampleExtension", "api": 1, "version": "1.2.3"}


def test_the_path_file_points_at_the_add_ons_own_source(tmp_path):
    addon = _write_addon(tmp_path)
    site_dir = tmp_path / "site-packages"
    subprocess.run([sys.executable, str(_DIST), str(addon), str(site_dir)], check=True)
    line = (site_dir / "sample_addon.pth").read_text(encoding="utf-8").strip()
    assert Path(line) == (addon / "src").resolve()


def test_a_flat_add_on_points_at_its_own_folder(tmp_path):
    addon = tmp_path / "flat"
    addon.mkdir()
    (addon / "pyproject.toml").write_text('[project]\nname = "flat"\nversion = "1"\n',
                                          encoding="utf-8")
    site_dir = tmp_path / "site-packages"
    subprocess.run([sys.executable, str(_DIST), str(addon), str(site_dir)], check=True)
    assert Path((site_dir / "flat.pth").read_text(encoding="utf-8").strip()) == addon.resolve()


def test_no_arguments_is_a_usage_error():
    done = subprocess.run([sys.executable, str(_DIST)], capture_output=True, text=True, check=False)
    assert done.returncode == 2


# --- the script ------------------------------------------------------------------------------
class _Robot:
    """A checkout holding the real scripts, with stubs for sudo, docker and python3."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        (self.root / "deploy" / "hub").mkdir(parents=True)
        shutil.copy(_SCRIPT, self.root / "deploy" / "hub" / "hub-addons.sh")
        shutil.copy(_DIST, self.root / "deploy" / "hub" / "addon_dist.py")
        self.log = tmp_path / "calls.log"
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        self._stub(stubs / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
        self._stub(stubs / "docker", (
            f'echo "docker $*" >> "{self.log.as_posix()}"\n'
            'if [ "$1" = image ]; then exit "${STUB_NO_IMAGE:-0}"; fi\n'
            'exit "${STUB_RUN_EXIT:-0}"\n'))
        self._stub(stubs / "python3", f'exec "{Path(sys.executable).as_posix()}" "$@"\n')
        self.stubs = stubs

    @staticmethod
    def _stub(path: Path, body: str) -> None:
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def run(self, *args: str, **extra: str):
        env = {**os.environ, "PATH": f"{self.stubs.as_posix()}{os.pathsep}{os.environ['PATH']}",
               **extra}
        done = subprocess.run(
            [_SHELL, str((self.root / "deploy" / "hub" / "hub-addons.sh").as_posix()), *args],
            capture_output=True, text=True, check=False, env=env, timeout=60)
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return done, calls

    def docker_runs(self, calls: list[str]) -> list[str]:
        return [line for line in calls if line.startswith("docker run")]

    @property
    def site(self) -> Path:
        return self.root / "hub-site"


@needs_shell
def test_no_add_on_means_no_hub_site_and_no_container(tmp_path):
    robot = _Robot(tmp_path)
    robot.site.mkdir()
    (robot.site / "left-over").write_text("x", encoding="utf-8")
    done, calls = robot.run()
    assert done.returncode == 0, done.stderr
    assert "hub_addons=none" in done.stdout
    assert not robot.site.exists() and calls == []


@needs_shell
def test_the_packages_install_offline_in_a_throwaway_container(tmp_path):
    robot = _Robot(tmp_path)
    addon = _write_addon(robot.root)
    done, calls = robot.run()
    assert done.returncode == 0, done.stderr
    assert "hub_addons=installed" in done.stdout
    runs = robot.docker_runs(calls)
    assert len(runs) == 1
    for word in ("--rm", "--network none", "--user 1000:1000", "--no-index", "--no-deps",
                 "--no-cache-dir", "emori/hub:robot", "pip3 --python /opt/emori-hub/venv/bin/python"):
        assert word in runs[0], word
    assert "/deploy/hub-wheels" in runs[0] and "/deploy/hub-constraints.txt" in runs[0]
    assert "--target" in runs[0] and "--prefix" not in runs[0]
    assert (robot.site / _LIB / "sample_addon.pth").is_file()
    assert (robot.site / _LIB / "sample_addon-1.2.3.dist-info" / "entry_points.txt").is_file()
    assert addon.is_dir() and not (robot.root / "hub-site.new").exists()


@needs_shell
def test_an_add_on_with_no_pins_starts_no_container(tmp_path):
    robot = _Robot(tmp_path)
    _write_addon(robot.root, pins=False)
    done, calls = robot.run()
    assert done.returncode == 0, done.stderr
    assert robot.docker_runs(calls) == []
    assert (robot.site / _LIB / "sample_addon.pth").is_file()


@needs_shell
def test_an_up_to_date_hub_site_is_left_alone(tmp_path):
    robot = _Robot(tmp_path)
    _write_addon(robot.root)
    robot.run()
    robot.log.unlink()
    done, calls = robot.run()
    assert done.returncode == 0 and "hub_addons=current" in done.stdout
    assert calls == []


@needs_shell
def test_a_newer_pin_file_installs_again(tmp_path):
    robot = _Robot(tmp_path)
    addon = _write_addon(robot.root)
    robot.run()
    robot.log.unlink()
    pins = addon / "deploy" / "hub-constraints.txt"
    later = pins.stat().st_mtime + 60
    os.utime(pins, (later, later))
    done, calls = robot.run()
    assert "hub_addons=installed" in done.stdout and robot.docker_runs(calls)


@needs_shell
def test_force_installs_again(tmp_path):
    robot = _Robot(tmp_path)
    _write_addon(robot.root)
    robot.run()
    robot.log.unlink()
    done, calls = robot.run("--force")
    assert "hub_addons=installed" in done.stdout and robot.docker_runs(calls)


@needs_shell
def test_an_add_on_that_has_gone_stops_being_importable(tmp_path):
    robot = _Robot(tmp_path)
    _write_addon(robot.root, "one", pins=False)
    gone = _write_addon(robot.root, "two", pins=False)
    robot.run()
    assert (robot.site / ".addons" / "two").exists()
    shutil.rmtree(gone)
    done, _ = robot.run()
    assert "hub_addons=installed" in done.stdout
    assert not (robot.site / ".addons" / "two").exists()


@needs_shell
def test_a_failed_install_leaves_the_hub_with_what_it_had(tmp_path):
    robot = _Robot(tmp_path)
    addon = _write_addon(robot.root)
    robot.run()
    before = (robot.site / _LIB / "sample_addon.pth").read_text(encoding="utf-8")
    later = addon.stat().st_mtime + 60
    os.utime(addon / "pyproject.toml", (later, later))
    done, _ = robot.run(STUB_RUN_EXIT="1")
    assert done.returncode == 4 and "hub_addons=failed" in done.stdout
    assert (robot.site / _LIB / "sample_addon.pth").read_text(encoding="utf-8") == before
    assert not (robot.root / "hub-site.new").exists()


@needs_shell
def test_with_no_hub_image_it_says_so_and_changes_nothing(tmp_path):
    robot = _Robot(tmp_path)
    _write_addon(robot.root)
    done, calls = robot.run(STUB_NO_IMAGE="1")
    assert done.returncode == 5 and "hub_addons=no_image" in done.stdout
    assert robot.docker_runs(calls) == [] and not robot.site.exists()


@needs_shell
def test_an_unknown_option_is_a_usage_error(tmp_path):
    robot = _Robot(tmp_path)
    done, _ = robot.run("--nope")
    assert done.returncode == 2


def test_the_script_is_generic():
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "addons/*/pyproject.toml" in text
    assert b"\r" not in _SCRIPT.read_bytes()


def test_the_add_on_step_is_called_where_the_deploy_needs_it():
    refresh = (_ROOT / "deploy" / "hub" / "hub-refresh.sh").read_text(encoding="utf-8")
    stack = (_ROOT / "deploy" / "robot" / "hub-stack.sh").read_text(encoding="utf-8")
    assert "deploy/hub/hub-addons.sh" in refresh and "deploy/hub/hub-addons.sh" in stack
