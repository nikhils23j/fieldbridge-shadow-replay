"""Actual localhost HTTP ingress and destination read API for the demo."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from .core import Bridge
from .fixtures import PROFILES


def make_server(bridge: Bridge, host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            parts = self.path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "source" or parts[2] != "events":
                self._json(404, {"error": "unknown endpoint"})
                return
            profile = parts[1]
            if profile not in PROFILES:
                self._json(404, {"error": "unknown profile"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 65536:
                    raise ValueError("invalid body size")
                event = json.loads(self.rfile.read(size))
                if event.get("profile") != profile or event.get("tenant") != profile:
                    self._json(403, {"error": "source identity does not match envelope"})
                    return
                outcome = bridge.ingest(event)
            except (ValueError, TypeError, KeyError) as exc:
                self._json(422, {"error": str(exc)})
                return
            self._json(202, {"outcome": outcome})

        def do_GET(self) -> None:
            if self.path == "/destination/state":
                self._json(200, {"state": bridge.state()})
            elif self.path == "/integration/status":
                self._json(200, {"mapping": bridge.mapping.name, "counts": bridge.counts()})
            else:
                self._json(404, {"error": "unknown endpoint"})

    return HTTPServer((host, port), Handler)


def serve_in_thread(bridge: Bridge) -> tuple[HTTPServer, Thread]:
    server = make_server(bridge)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
