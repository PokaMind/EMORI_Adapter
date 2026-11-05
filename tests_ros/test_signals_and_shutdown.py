"""How the hub takes signals and lets go of ROS, against a real rclpy.

``main.py`` installs its own SIGTERM and SIGINT handlers and then calls
``rclpy.init(signal_handler_options=SignalHandlerOptions.NO)``, so that uvicorn's graceful
stop is what ends the process and ROS goes last. That only holds if rclpy leaves the
handlers alone. The children below are separate processes because a signal handler is a
property of a process.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from typing import Any

import pytest

import ros_support

CHILD_S = 60

_NO_HANDLERS = """
    import os, signal, sys, time
    import rclpy
    from rclpy.signals import SignalHandlerOptions

    noted = []
    signal.signal(signal.SIGTERM, lambda signum, frame: noted.append(signum))
    signal.signal(signal.SIGINT, lambda signum, frame: noted.append(signum))
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    os.kill(os.getpid(), signal.SIGTERM)
    os.kill(os.getpid(), signal.SIGINT)
    deadline = time.monotonic() + 5
    while len(noted) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sorted(noted) == sorted([signal.SIGTERM, signal.SIGINT]), noted
    assert rclpy.ok(), "rclpy shut its context down on a signal it was told to leave alone"
    rclpy.try_shutdown()
    assert not rclpy.ok()
    print("handlers left alone")
"""

_HUB_BINDINGS = """
    from emori_hub import main as hub

    bindings = hub.load_hub_bindings()
    assert bindings.fatal, "no exception types for a context that went away"
    bindings.init(hub.ROS_ARGS)
    assert bindings.ok()
    executor = bindings.multi_threaded_executor(num_threads=hub.EXECUTOR_THREADS)
    executor.shutdown()
    bindings.try_shutdown()
    assert not bindings.ok()
    bindings.try_shutdown()
    print("bindings ok")
"""


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True,
                          text=True, timeout=CHILD_S, env=ros_support.child_environment(),
                          check=False)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_signal_handler_options_no_leaves_the_process_handlers_alone(rclpy: Any) -> None:
    done = _run(_NO_HANDLERS)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "handlers left alone" in done.stdout


def test_the_hub_bindings_start_and_stop_a_context(rclpy: Any, reachy_srv: Any) -> None:
    """``load_hub_bindings`` imports every service type the link needs, then inits and stops."""

    done = _run(_HUB_BINDINGS)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "bindings ok" in done.stdout


def test_try_shutdown_may_be_called_again_and_before_init(rclpy: Any) -> None:
    from rclpy.context import Context

    never_started = Context()
    rclpy.try_shutdown(context=never_started)

    started = Context()
    rclpy.init(args=[], context=started, domain_id=ros_support.domain_id())
    assert rclpy.ok(context=started)
    rclpy.try_shutdown(context=started)
    assert not rclpy.ok(context=started)
    rclpy.try_shutdown(context=started)
    rclpy.try_shutdown(context=started)


def test_shutdown_is_not_idempotent_which_is_why_the_hub_uses_try_shutdown(rclpy: Any) -> None:
    from rclpy.context import Context

    context = Context()
    rclpy.init(args=[], context=context, domain_id=ros_support.domain_id())
    rclpy.shutdown(context=context)
    with pytest.raises(RuntimeError):
        rclpy.shutdown(context=context)
