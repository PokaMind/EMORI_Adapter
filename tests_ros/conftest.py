"""Tests against a real rclpy, run only where ROS is installed (the CI job ``ros2-hub``).

Everything here skips when ``rclpy`` cannot be imported, so a bare ``pytest tests_ros`` on a
development computer collects and skips. In the job, ``EMORI_REQUIRE_ROS=1`` turns that skip
into a failure.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

import pytest

import ros_support

# rclpy is imported only by the tests that need it, after this: discovery stays on this
# computer, which is what the job's private domain relies on.
os.environ.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
os.environ.setdefault("RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
# Off by default on Jazzy, where a message then takes a field of the wrong type or size without a
# word. These tests are where that is meant to be caught; a robot leaves it off for the cost.
os.environ.setdefault("ROS_PYTHON_CHECK_FIELDS", "1")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "ros: needs a real rclpy; skipped where it is absent")


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ours = [item for item in items if str(item.path).startswith(here)]
    for item in ours:
        item.add_marker(pytest.mark.ros)
    if _importable("rclpy"):
        return
    if ros_support.required():
        raise pytest.UsageError(
            f"{ros_support.REQUIRE_ENV} is set but rclpy cannot be imported: "
            "run these tests inside the hub image")
    skip = pytest.mark.skip(reason="rclpy is not importable here")
    for item in ours:
        item.add_marker(skip)


@pytest.fixture
def rclpy() -> Any:
    return ros_support.need_module("rclpy")


@pytest.fixture
def reachy_srv() -> Any:
    ros_support.need_module("rclpy")
    return ros_support.need_module("reachy_mini_msgs.srv")


@pytest.fixture
def ros_context(rclpy: Any):
    """A context of its own on this process's domain, shut down afterwards."""

    from rclpy.context import Context
    from rclpy.signals import SignalHandlerOptions

    context = Context()
    rclpy.init(args=[], context=context, domain_id=ros_support.domain_id(),
               signal_handler_options=SignalHandlerOptions.NO)
    try:
        yield context
    finally:
        rclpy.try_shutdown(context=context)
