"""A dumb TCP forwarder that makes a Docker Desktop container look like loopback.

``POST /api/skill`` refuses anything whose client host is not 127.0.0.1, and that
refusal is load-bearing: on the robot the ROS container is host-networked, so a genuine
facade request genuinely arrives from the machine itself, and everything else on the
venue LAN is turned away before the body is even read.

Docker Desktop on Windows cannot host-network. A container there reaches the host
through ``host.docker.internal``, i.e. the gateway address -- which is exactly what the
intake is built to refuse. There are two ways to rehearse ``EMORI_ROS_MOTION_OWNER=ros``
on this box, and only one of them is honest:

* widen the intake (trust the Docker bridge subnet, or add a header that bypasses the
  check) -- which means the thing under test is no longer the thing that ships;
* insert a loopback hop -- this file. It listens on every interface, and forwards every
  byte to 127.0.0.1, so the hostapp sees a connection whose peer IS the loopback
  address. The production gate is untouched and still has to pass.

The second is what the rehearsal uses. This is a DEV tool: it authenticates nothing and
must never run on a robot, where the container is host-networked and needs no hop.

    python scripts/dev/loopback_proxy.py --listen 8096 --target 8095
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import sys

log = logging.getLogger("emori.dev.loopback_proxy")

# The forwarding buffer. Skill requests are a few hundred bytes; this only ever matters
# for the response body, and 64 KiB is one comfortable read either way.
CHUNK_BYTES = 65536
# How long to wait for the upstream hostapp to accept a connection before giving up on
# one client. Longer than any healthy /api/skill round trip, short enough that a dead
# hostapp shows up as refused connections rather than as a hung rehearsal.
CONNECT_TIMEOUT_S = 5.0
# A forgotten proxy is an open hop; twenty minutes covers the longest rehearsal with
# margin and is short enough that "I started it this morning" is not a thing.
DEFAULT_MAX_IDLE_S = 1200.0


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction until EOF, then half-close so the peer sees the EOF too."""

    try:
        while True:
            chunk = await reader.read(CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - teardown races are not failures
            pass


def peer_allowed(host: str | None) -> bool:
    """Only a container on this machine may use the hop.

    The proxy exists to make ONE thing look like loopback: the Docker Desktop container,
    which arrives from a private gateway address. A peer on a public address is not that
    container, and forwarding it would turn a dev convenience into an open relay that
    neutralises the intake's loopback gate for the whole LAN. Private ranges are still
    accepted (the Docker gateway is one of them), so this is a fence, not a lock, and
    the docstring above already says never to run it on a robot.
    """

    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def make_handler(target_host: str, target_port: int, touch=None):
    """Build the per-connection handler for ``asyncio.start_server``."""

    async def handle(client_reader: asyncio.StreamReader,
                     client_writer: asyncio.StreamWriter) -> None:
        peer = client_writer.get_extra_info("peername")
        if touch is not None:
            touch()
        if not peer_allowed(peer[0] if peer else None):
            log.warning("refusing non-private peer %s", peer)
            client_writer.close()
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port),
                timeout=CONNECT_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("upstream %s:%s unreachable for %s (%s)",
                        target_host, target_port, peer, e)
            client_writer.close()
            return
        # Both directions concurrently: the hostapp answers while the client may still
        # be writing, and a strictly sequential copy would deadlock on a keep-alive.
        await asyncio.gather(
            _pump(client_reader, up_writer),
            _pump(up_reader, client_writer),
        )

    return handle


async def serve(listen_host: str, listen_port: int,
                target_host: str, target_port: int,
                max_idle_s: float = DEFAULT_MAX_IDLE_S) -> None:
    loop = asyncio.get_running_loop()
    last_activity = loop.time()

    def touch() -> None:
        nonlocal last_activity
        last_activity = loop.time()

    server = await asyncio.start_server(
        make_handler(target_host, target_port, touch), listen_host, listen_port)
    log.warning("DEV loopback proxy %s:%s -> %s:%s: every private-network peer is "
                "forwarded as loopback; stops by itself after %.0f s without a client",
                listen_host, listen_port, target_host, target_port, max_idle_s)
    async with server:
        # A rehearsal that forgot to stop the proxy must not leave the hop open all
        # afternoon: with no client for `max_idle_s` the server closes itself.
        while True:
            await asyncio.sleep(min(30.0, max(1.0, max_idle_s / 4)))
            if max_idle_s > 0 and loop.time() - last_activity > max_idle_s:
                log.warning("no client for %.0f s; shutting the proxy down", max_idle_s)
                return


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--listen", type=int, default=8096,
                    help="port to accept container traffic on (default: 8096)")
    ap.add_argument("--listen-host", default="0.0.0.0",
                    help="interface to listen on (default: every one)")
    ap.add_argument("--target", type=int, default=8095,
                    help="hostapp port to forward to on 127.0.0.1 (default: 8095)")
    ap.add_argument("--target-host", default="127.0.0.1",
                    help="forward target; keep it loopback or the hop is pointless")
    ap.add_argument("--max-idle-s", type=float, default=DEFAULT_MAX_IDLE_S,
                    help="exit after this long with no client (0 = never; "
                         f"default: {DEFAULT_MAX_IDLE_S:.0f})")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(serve(args.listen_host, args.listen, args.target_host, args.target,
                          max_idle_s=args.max_idle_s))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
