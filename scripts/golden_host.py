"""Run the production hostapp with every robot-client call recorded.

The hostapp builds its own ``HttpRobotClient`` unless one is injected, so recording needs
no product code: this launcher injects a :class:`TracingRobotClient` around it and starts
uvicorn with the same arguments ``emori_adapter.hostapp.server.main`` uses.

    python scripts/golden_host.py --out artifacts/golden/run1

writes ``<out>/client.jsonl``. Run it in place of ``emori-hostapp`` (stop that unit
first), from the same working directory and with the same environment. For a transient
unit with the service's user, slice and ``EnvironmentFile=`` lines, use
``scripts/golden_in_slice.sh``.

With ``--hub`` it runs the hub's own entry point instead, which picks its own transport and
takes the recording client through its wrapper hook, so the hub's normal entry carries no
harness code. Run it as the hub container's command.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def build_traced_app(out_dir: Path, base_url: str | None = None):
    """Return ``(app, sink)``: the hostapp with a tracing client writing to ``out_dir``."""
    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import HttpRobotClient
    from emori_adapter.robot.tracing import TraceSink, TracingRobotClient

    sink = TraceSink(out_dir / "client.jsonl")
    client = TracingRobotClient(HttpRobotClient(base_url or server.REACHY_BASE_URL), sink)
    return server._build_app({"robot_client": client}), sink


def run_traced_hub(out_dir: Path) -> int:
    """Run the hub with a tracing client around its own; returns its exit code."""
    from emori_hub import main as hub

    sinks = []

    def wrap(client):
        # Here and not above: the hub loads its environment before anything of the core.
        from emori_adapter.robot.tracing import TraceSink, TracingRobotClient

        sinks.append(TraceSink(out_dir / "client.jsonl"))
        return TracingRobotClient(client, sinks[-1])

    try:
        return hub.main(wrap_client=wrap)
    finally:
        for sink in sinks:
            sink.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path, help="directory for client.jsonl")
    ap.add_argument("--base-url", default=None, help="daemon URL (default: REACHY_BASE_URL)")
    ap.add_argument("--hub", action="store_true",
                    help="run the hub's entry point; --base-url does not apply")
    args = ap.parse_args(argv)

    if args.hub:
        return run_traced_hub(args.out)

    from emori_adapter.logging_setup import access_log_enabled, setup_journal_logging

    setup_journal_logging()
    import uvicorn

    app, sink = build_traced_app(args.out, args.base_url)
    host = os.environ.get("HOSTAPP_HOST", "0.0.0.0")
    port = int(os.environ.get("HOSTAPP_PORT", "8080"))
    ws_impl = os.environ.get("HOSTAPP_WS", "wsproto")
    try:
        uvicorn.run(app, host=host, port=port, ws=ws_impl, access_log=access_log_enabled())
    finally:
        sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
