"""The dev loopback proxy is a fence around ONE hop, not an open relay.

``scripts/dev/loopback_proxy.py`` makes a Docker Desktop container look like a loopback
peer to the hostapp's ``/api/skill`` intake. That is the whole point, and also the
whole risk: while it runs, whoever it forwards is loopback as far as the intake can
tell. These tests pin the two limits that keep it a dev tool -- only private-network
peers are forwarded, and it shuts itself down when nobody has used it for a while.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest

_PROXY = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "loopback_proxy.py"


def _load():
    spec = importlib.util.spec_from_file_location("emori_loopback_proxy_under_test", _PROXY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("host, allowed", [
    ("127.0.0.1", True),
    ("::1", True),
    ("192.168.65.1", True),     # Docker Desktop's gateway, the peer the hop exists for
    ("172.17.0.2", True),       # a bridge-network container
    ("10.0.0.7", True),
    ("169.254.10.10", True),
    ("8.8.8.8", False),
    ("1.1.1.1", False),
    ("2606:4700:4700::1111", False),
    ("not-an-address", False),
    ("", False),
    (None, False),
])
def test_only_private_network_peers_are_forwarded(host, allowed):
    assert _load().peer_allowed(host) is allowed


def test_the_proxy_shuts_itself_down_when_idle():
    """A rehearsal that forgot to stop the proxy must not leave the hop open."""

    proxy = _load()

    async def run() -> None:
        # max_idle_s well under the poll interval floor of 1 s: the first poll sees an
        # idle proxy and `serve` returns instead of running forever.
        await asyncio.wait_for(
            proxy.serve("127.0.0.1", 0, "127.0.0.1", 1, max_idle_s=0.2), timeout=5.0)

    asyncio.run(run())


def test_the_default_idle_limit_is_minutes_not_hours():
    proxy = _load()
    assert 0 < proxy.DEFAULT_MAX_IDLE_S <= 1800
