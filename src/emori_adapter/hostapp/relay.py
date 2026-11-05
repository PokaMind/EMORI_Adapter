"""RelayHub: the ``/ws/relay`` broadcast with an actual producer.

Historically ``/ws/relay`` only re-broadcast between connected WebSocket clients and
NOTHING ever produced into it - the documented consumers (the ROS 2 live publisher,
``emori-run --source pokamind``, the recorder) read an always-empty socket. Now the
hostapp itself is the producer: ``/api/cycle`` publishes every stamped ``pokamind_*``
envelope through :meth:`RelayHub.publish` (thread-safe; the HTTP handlers run in
FastAPI's threadpool while the broadcast pump runs on the event loop).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

log = logging.getLogger("emori.hostapp.relay")


class RelayHub:
    def __init__(self, tap: Optional[Callable[[str], object]] = None) -> None:
        # Sees what the hostapp itself publishes, in order, on the caller's thread and
        # whether or not the broadcast is running. Must not block; a fault is logged and
        # goes no further. May be set after construction.
        self.tap = tap
        self._clients: set = set()
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pump_task: Optional[asyncio.Task] = None

    # --- lifecycle (called from the app's startup/shutdown hooks) -------------
    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=256)
        self._pump_task = self._loop.create_task(self._pump())

    async def shutdown(self) -> None:
        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a relay receiver cannot block app teardown
                pass
        self._clients.clear()
        self._queue = None
        self._loop = None

    # --- producer side (thread-safe) ------------------------------------------
    def publish(self, message: str, exclude=None) -> bool:
        """Queue a message for broadcast. Safe to call from any thread.

        ``exclude`` marks a frame a WebSocket client sent, re-broadcast to the others. Only
        what the hostapp produced goes to the tap.
        """
        if exclude is None and self.tap is not None:
            try:
                self.tap(message)
            except Exception:  # noqa: BLE001 - a tap must not fail the request publishing
                log.warning("relay tap failed", exc_info=True)
        if self._loop is None or self._queue is None:
            return False
        try:
            self._loop.call_soon_threadsafe(self._try_put, (message, exclude))
            return True
        except RuntimeError:  # loop already closed (shutdown)
            return False

    def _try_put(self, item) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            log.warning("relay queue full; dropping a message")

    # --- consumer side ----------------------------------------------------------
    def register(self, ws) -> None:
        self._clients.add(ws)

    def unregister(self, ws) -> None:
        self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def _pump(self) -> None:
        while True:
            message, exclude = await self._queue.get()
            for ws in list(self._clients):
                if ws is exclude:
                    continue
                try:
                    await ws.send_text(message)
                except Exception:  # noqa: BLE001 - dead client
                    self._clients.discard(ws)
