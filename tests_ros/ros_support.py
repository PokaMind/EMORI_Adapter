"""Plumbing for the tests that run against a real rclpy.

Nothing here imports ROS when this module loads: every helper takes the ``rclpy`` module
it should use, so collecting these tests on a machine without ROS costs nothing.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
HUB_PACKAGE = ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"

#: Set where the tests are meant to run (the hub image). A missing rclpy, or a missing
#: interface package, is then a failure and not a skip, so a broken image cannot read as a
#: green run of zero tests.
REQUIRE_ENV = "EMORI_REQUIRE_ROS"


def required() -> bool:
    return os.environ.get(REQUIRE_ENV, "") not in ("", "0")


def need_module(name: str) -> Any:
    """Import a module the test needs, or skip (fail, in the job)."""

    try:
        return importlib.import_module(name)
    except ImportError:
        if required():
            pytest.fail(f"{name} cannot be imported and {REQUIRE_ENV} is set")
        pytest.skip(f"{name} is not importable here")


def domain_id() -> int:
    """A DDS domain for this process, so two runs on one machine do not meet."""

    return (os.getpid() % 100) + 1


def child_environment() -> dict[str, str]:
    """The environment of a child process that imports the hub package and the core."""

    env = dict(os.environ)
    paths = [str(HUB_PACKAGE), str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["ROS_DOMAIN_ID"] = str(domain_id())
    return env


def hub_package_on_path() -> None:
    """Make ``emori_hub`` importable from the checkout when the image does not hold it."""

    try:
        import emori_hub  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(HUB_PACKAGE))


def wait_until(predicate: Callable[[], Any], timeout: float = 10.0, step: float = 0.02) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class Spinner:
    """One executor, spun on one thread of its own, until :meth:`stop`.

    It records what the executor raised, so a test can say that a late answer or a removed
    request did not break the loop.
    """

    def __init__(self, executor: Any, name: str, *, slice_s: float = 0.05) -> None:
        self.executor = executor
        self.errors: list[BaseException] = []
        self._slice = slice_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> "Spinner":
        self._thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> bool:
        self._stop.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.executor.spin_once(timeout_sec=self._slice)
            except Exception as error:  # noqa: BLE001 - the test reads these
                if self._stop.is_set():
                    return
                self.errors.append(error)
                time.sleep(self._slice)
