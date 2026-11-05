"""The CI job ``ros2-hub`` and what it runs, checked as text, with no container runtime.

The job builds the hub image through the same Dockerfiles the robot uses and runs it beside a
motor node. Nothing here starts Docker; it holds the pieces to each other, so that a rename in
one place fails here and not as a red job twenty minutes in:

* the workflow calls every subcommand of ``scripts/ci/hub-ci.sh`` and only those, in the
  order the layers depend on one another, and always collects the logs and stops the
  containers;
* the motor layer carries the files and the install path ``motor-overlay.sh`` carries;
* the payload holds everything ``Dockerfile.hub`` and ``Dockerfile.hub-base`` copy or mount;
* the real-rclpy tests under ``tests_ros/`` import ROS only inside tests, so they collect and
  skip on a computer without it, and they run in the job with a missing rclpy as a failure;
* the hub image carries none of the test or recording code.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"
_SCRIPT = _ROOT / "scripts" / "ci" / "hub-ci.sh"
_OVERLAY = _ROOT / "deploy" / "robot" / "ros2_native" / "motor-overlay.sh"
_HUB_DIR = _ROOT / "deploy" / "hub"
_TESTS_ROS = _ROOT / "tests_ros"
_SHELL = shutil.which("bash") or shutil.which("sh")

#: In the order each depends on the one before it.
_BUILD_ORDER = ["motor-layer", "payload", "hub-base", "hub", "up", "smoke", "tests", "stop"]


def _jobs() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _steps(job: str) -> list[dict]:
    return _jobs()[job]["steps"]


def _script() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def _array(text: str, name: str) -> list[str]:
    match = re.search(rf"^{name}=\(([^)]*)\)", text, re.MULTILINE)
    assert match, f"no {name}=(...) in the script"
    return match.group(1).split()


def _variable(text: str, name: str) -> str:
    match = re.search(rf"^{name}=(\S+)", text, re.MULTILINE)
    assert match, f"no {name}= in the script"
    return match.group(1)


# --- the workflow ----------------------------------------------------------------------


def test_the_matrix_is_the_four_jobs():
    assert list(_jobs()) == ["test", "ros2-standalone", "ros2-graph", "ros2-hub"]


def test_the_python_suite_keeps_its_three_versions_and_lints_the_ros_tests():
    job = _jobs()["test"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.10", "3.12", "3.13"]
    runs = " ".join(str(step.get("run", "")) for step in job["steps"])
    assert "ruff check src tests tests_ros" in runs
    assert re.search(r"pytest tests(\s|$)", runs)
    assert "pytest tests_ros" in runs


def test_the_hub_job_runs_every_subcommand_in_dependency_order():
    runs = [str(step.get("run", "")) for step in _steps("ros2-hub")]
    called = [match.group(1) for run in runs
              for match in re.finditer(r"hub-ci\.sh (\S+)", run)]
    assert [name for name in called if name in _BUILD_ORDER] == _BUILD_ORDER
    # The base is built before anything is layered on it.
    base = next(index for index, run in enumerate(runs)
                if "deploy/ros2/Dockerfile.v2.robot" in run)
    layer = next(index for index, run in enumerate(runs) if "hub-ci.sh motor-layer" in run)
    assert base < layer


def test_the_hub_job_names_the_image_the_script_expects():
    run = next(str(step["run"]) for step in _steps("ros2-hub")
               if "Dockerfile.v2.robot" in str(step.get("run", "")))
    assert f"-t {_variable(_script(), 'BASE_IMAGE')}" in run


def test_the_hub_job_always_collects_logs_and_stops_the_containers():
    steps = {step.get("name", ""): step for step in _steps("ros2-hub")}
    last = [steps[name] for name in steps if name.startswith(("Container logs", "Stop"))]
    assert len(last) == 2
    for step in last:
        assert step["if"] == "always()"
    assert "hub-ci.sh logs" in last[0]["run"] and "hub-ci.sh down" in last[1]["run"]


def test_the_hub_job_builds_with_plain_docker_so_each_image_can_be_the_next_from():
    text = yaml.safe_dump(_steps("ros2-hub"))
    assert "setup-buildx-action" not in text and "buildx" not in text


def test_the_hub_job_runs_the_goldens_check_and_fails_without_recordings():
    runs = " ".join(str(step.get("run", "")) for step in _steps("ros2-hub"))
    assert "python scripts/golden_ci.py check --require-recordings" in runs


def test_the_workflow_runs_the_other_jobs_as_before():
    for job in ("ros2-standalone", "ros2-graph"):
        assert job in _jobs()


# --- the script ------------------------------------------------------------------------


def test_the_script_is_executable_in_the_index_and_has_unix_line_endings():
    assert b"\r" not in _SCRIPT.read_bytes()
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    listing = subprocess.run([git, "ls-files", "-s", "--", "scripts/ci/hub-ci.sh"], cwd=_ROOT,
                             capture_output=True, text=True, check=False)
    if listing.returncode != 0 or not listing.stdout.strip():
        pytest.skip("not a git checkout, or not added yet")
    # core.filemode is false on a Windows checkout, so the index is the only record.
    assert listing.stdout.split()[0] == "100755"


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_the_script_parses():
    done = subprocess.run([_SHELL, "-n", str(_SCRIPT)], capture_output=True, text=True,
                          check=False)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_an_unknown_subcommand_is_a_usage_error():
    done = subprocess.run([_SHELL, str(_SCRIPT), "nonsense"], capture_output=True, text=True,
                          check=False)
    assert done.returncode == 2 and "usage:" in done.stderr


def test_every_subcommand_the_workflow_calls_is_one_the_script_has():
    script = _script()
    calls = {match.group(1) for step in _steps("ros2-hub")
             for match in re.finditer(r"hub-ci\.sh (\S+)", str(step.get("run", "")))}
    assert calls
    for name in calls:
        assert re.search(rf"^\s+{re.escape(name)}\) ", script, re.MULTILINE), name


def test_a_node_list_with_no_daemon_waits_for_discovery():
    """With no daemon it knows only what it discovers in its own spin, half a second unless told,
    and then it lists nothing and the smoke step reads that as a missing node."""
    lines = [line for line in _script().splitlines() if "ros2 node list" in line]
    assert lines
    for line in lines:
        assert "--spin-time" in line, line


def test_the_ros_tests_run_without_the_pytest_plugins_ros_ships():
    """pip takes the current pytest, and launch_testing's hook stops that from starting at all."""
    script = _script()
    tests = script[script.index("tests() {"):script.index("stop_hub() {")]
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in tests


def test_the_motor_layer_carries_what_the_robots_overlay_carries():
    script, overlay = _script(), _OVERLAY.read_text(encoding="utf-8")
    assert _array(script, "MOTOR_FILES") == _array(overlay, "FILES")
    assert _variable(script, "MOTOR_SITE") == _variable(overlay, "SITE")
    assert _variable(script, "MOTOR_SOURCE") == _variable(overlay, "SOURCE")
    assert _variable(script, "MOTOR_IMAGE") == _variable(overlay, "IMAGE")
    assert _variable(script, "BASE_IMAGE") == _variable(overlay, "BASE")
    for name in _array(script, "MOTOR_FILES"):
        assert (_ROOT / _variable(script, "MOTOR_SOURCE") / name).is_file(), name


def test_the_hub_layer_is_laid_over_the_package_the_image_builds():
    script = _script()
    source = _ROOT / _variable(script, "HUB_SOURCE")
    assert (source / "main.py").is_file()
    site = _variable(script, "HUB_SITE")
    assert site.startswith(f"{source.name}/lib/python3.")
    # The directory under the install tree is the package's own name.
    assert site.endswith(f"site-packages/{source.name}")


def test_the_payload_holds_what_the_dockerfiles_copy_and_mount():
    script = _script()
    image = (_HUB_DIR / "Dockerfile.hub").read_text(encoding="utf-8")
    base = (_HUB_DIR / "Dockerfile.hub-base").read_text(encoding="utf-8")
    copied = re.findall(r"^COPY (?:--\S+ )*(hub/\S+?)/? ", image, re.MULTILINE)
    assert copied
    for path in copied:
        name = path.removeprefix("hub/")
        if name == "install-delta":
            assert "hub/install-delta" in script
        else:
            assert name in script, f"the payload step does not copy {name}"
    for source in re.findall(r"source=(\w+),", base):
        assert f'"$context/{source}"' in script or f"$context/{source}" in script, source
    assert "Dockerfile.hub " in script and "Dockerfile.hub-base" in script
    # The package closure and the wheels come from the files the robot's build reads.
    assert "deploy/hub/hub-packages.txt" in script
    assert "deploy/hub/constraints-hub.txt" in script


def test_the_hub_image_is_run_as_the_robot_runs_it():
    script = _script()
    for fragment in ("--cap-drop ALL", "--security-opt no-new-privileges:true", "--ipc private",
                     "--ulimit core=0:0", "--init", "/etc/emori/fastdds_peers.xml",
                     "EMORI_ROBOT_TRANSPORT=ros"):
        assert fragment in script, fragment
    stop = int(_variable(script, "STOP_BUDGET_S"))
    compose = yaml.safe_load((_HUB_DIR / "docker-compose.hub.robot.yml").read_text(
        encoding="utf-8"))
    assert f"{stop}s" == compose["services"]["hub"]["stop_grace_period"]


def test_the_services_the_smoke_looks_for_are_the_ones_the_link_names():
    sys.path.insert(0, str(_ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"))
    from emori_hub import robot_link

    script = _script()
    listed = re.search(r"for name in ((?:[a-z_]+|\s|\\)+); do\s+grep -qx \"/reachy_mini/",
                       script)
    assert listed, "the smoke has no list of services"
    names = listed.group(1).replace("\\", " ").split()
    assert set(names) == set(robot_link.SERVICE_NAMES) and len(names) == len(set(names))


def test_the_hub_image_carries_no_test_or_recording_code():
    image = (_HUB_DIR / "Dockerfile.hub").read_text(encoding="utf-8")
    for word in ("tests", "tests_ros", "golden", "scripts/"):
        assert word not in image, word


# --- tests_ros -------------------------------------------------------------------------

_ROS_MODULES = ("rclpy", "std_srvs", "std_msgs", "reachy_mini_msgs", "launch", "launch_ros")


def _module_files() -> list[Path]:
    return sorted(path for path in _TESTS_ROS.glob("*.py") if path.name != "link_scenario.py")


@pytest.mark.parametrize("path", _module_files(), ids=lambda path: path.name)
def test_the_ros_tests_import_ros_only_inside_functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            assert name.split(".")[0] not in _ROS_MODULES, \
                f"{path.name} imports {name} at module level, so it cannot be collected"


def test_the_ros_tests_name_what_they_assume():
    """One test per assumption the hub makes of rclpy and nothing but fakes had checked."""

    text = "\n".join(path.read_text(encoding="utf-8") for path in _TESTS_ROS.glob("test_*.py"))
    for fragment in ("call_async", "service_is_ready", "remove_pending_request",
                     "SignalHandlerOptions.NO", "MultiThreadedExecutor", "try_shutdown",
                     "executor.shutdown", "antennas", "float64"):
        assert fragment in text, fragment


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"loaded_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_required_run_without_rclpy_is_a_failure_and_not_a_skip(monkeypatch):
    support = _load(_TESTS_ROS / "ros_support.py")
    monkeypatch.delenv(support.REQUIRE_ENV, raising=False)
    assert support.required() is False
    for value in ("1", "yes"):
        monkeypatch.setenv(support.REQUIRE_ENV, value)
        assert support.required() is True
    monkeypatch.setenv(support.REQUIRE_ENV, "0")
    assert support.required() is False
    with pytest.raises(pytest.skip.Exception):
        support.need_module("no_such_module_for_this_test")
    monkeypatch.setenv(support.REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        support.need_module("no_such_module_for_this_test")


def _rclpy_importable() -> bool:
    return importlib.util.find_spec("rclpy") is not None


@pytest.mark.skipif(_rclpy_importable(), reason="rclpy is here, so the tests would run")
def test_without_rclpy_the_ros_tests_collect_and_skip():
    done = subprocess.run([sys.executable, "-m", "pytest", "tests_ros", "-rs",
                           "-p", "no:cacheprovider"], cwd=_ROOT, capture_output=True,
                          text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "rclpy is not importable here" in done.stdout
    assert " passed" not in done.stdout and " failed" not in done.stdout


@pytest.mark.skipif(_rclpy_importable(), reason="rclpy is here, so the tests would run")
def test_without_rclpy_a_required_run_stops_with_a_reason():
    env = {"EMORI_REQUIRE_ROS": "1", **_clean_environment()}
    done = subprocess.run([sys.executable, "-m", "pytest", "tests_ros",
                           "-p", "no:cacheprovider"], cwd=_ROOT, capture_output=True,
                          text=True, env=env, check=False)
    assert done.returncode != 0
    assert "rclpy cannot be imported" in done.stdout + done.stderr


def _clean_environment() -> dict[str, str]:
    import os

    return {key: value for key, value in os.environ.items() if key != "EMORI_REQUIRE_ROS"}


# --- what the new files must not say ---------------------------------------------------

#: Planning identifiers, dates, and the words of a development log.
_FORBIDDEN = re.compile(
    r"\b(?:T\d{2,3}|S-\d{2}|SP-\d{2}|SG-\d{1,2}|DM-\d{2}|OPS?\d{1,2}|N\d{2}|R\d{1,2}"
    r"|D\d{1,2}|E\d{1,2}[a-z]?)\b"
    r"|\b20\d\d-\d\d-\d\d\b"
    r"|(?i:verified live|proven on|root cause|the rig\b|the reference robot)")

_NEW_FILES = [_SCRIPT, _ROOT / "scripts" / "golden_ci.py", _TESTS_ROS / "README.md",
              _ROOT / "tests" / "fixtures" / "golden" / "README.md",
              *sorted(_TESTS_ROS.glob("*.py"))]


@pytest.mark.parametrize("path", [path for path in _NEW_FILES if path.exists()],
                         ids=lambda path: path.name)
def test_the_new_files_carry_no_plan_identifier_date_or_log_phrase(path):
    hits = [(number, match.group(0)) for number, line in
            enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            for match in [_FORBIDDEN.search(line)] if match]
    assert not hits, f"{path.name}: {hits}"
