"""A stand-in NGSI-LD broker that records what it was asked to do.

Only the sequence of calls and the attributes matter for parity, so it answers what a broker
answers to a writer that is doing well and keeps nothing else. It writes ``fiware.jsonl``:
method, path, and the request's JSON, with a Normaliser taking the session out of the path.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Optional

from .normalise import Normaliser


class StubOrion:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._normaliser = Normaliser()
        self.calls: list[dict] = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def _record(self, method: str, path: str, body: Optional[object]) -> None:
        with self._lock:
            record = {"method": method, "path": self._normaliser.text(path),
                      "body": self._normaliser.value(body)}
            self.calls.append(record)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    def start(self) -> str:
        """Listen on a free loopback port. Returns the broker's URL."""

        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def _answer(self, status: int, payload: Optional[object] = None) -> None:
                body = json.dumps(payload).encode("utf-8") if payload is not None else b""
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _body(self) -> Optional[object]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return None
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw)
                except ValueError:
                    return None

            def do_GET(self) -> None:  # noqa: N802 - the http.server hook
                if self.path.startswith("/version"):
                    self._answer(200, {"orionld version": "stub"})
                elif "subscriptions" in self.path:
                    stub._record("GET", self.path, None)
                    self._answer(200, [])
                else:
                    stub._record("GET", self.path, None)
                    self._answer(404, {"title": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                body = self._body()
                stub._record("POST", self.path, body)
                self._answer(204 if "/attrs" in self.path else 201)

            def do_PATCH(self) -> None:  # noqa: N802
                body = self._body()
                stub._record("PATCH", self.path, body)
                self._answer(204)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="stub-orion",
                                        daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
