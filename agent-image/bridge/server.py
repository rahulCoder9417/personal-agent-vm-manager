"""HTTP server for the vm-manager (sandbox network, port BRIDGE_PORT).

GET  /healthz   no secret; 200 {"ok": true, "busy": n} when Hermes answers, else 503
POST /deliver   X-Bridge-Secret; JSON body; 200 NDJSON stream of events
POST /shutdown  X-Bridge-Secret; stops Hermes (saves to /data); 200 when done
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .core import Bridge, DeliverRequest

log = logging.getLogger("bridge.server")

SECRET_HEADER = "X-Bridge-Secret"
MAX_BODY = 1 << 20


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: the NDJSON body is delimited by closing the connection (no chunking needed).
    protocol_version = "HTTP/1.0"
    server: "BridgeHTTPServer"
    server_version = "bridge"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:  # route to logging, no secrets in paths
        log.debug("%s %s", self.address_string(), fmt % args)

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self._json(404, {"error": "not found"})
            return
        bridge = self.server.bridge
        if bridge.healthy():
            self._json(200, {"ok": True, "busy": bridge.busy()})
        else:
            self._json(503, {"ok": False, "busy": bridge.busy()})

    def do_POST(self) -> None:  # noqa: N802
        bridge = self.server.bridge
        if self.path not in ("/deliver", "/shutdown"):
            self._json(404, {"error": "not found"})
            return
        if not bridge.check_secret(self.headers.get(SECRET_HEADER)):
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._json(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b""
        if self.path == "/shutdown":
            self.server.on_shutdown()
            self._json(200, {"ok": True})
            return
        try:
            req = DeliverRequest.from_json(json.loads(raw or b"null"))
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        client_gone = False
        for ev in bridge.deliver(req):
            if client_gone:
                continue  # keep draining: the turn runs to the end regardless
            try:
                self.wfile.write(json.dumps(ev).encode() + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                client_gone = True
                log.warning("vm-manager disconnected during delivery %s", req.delivery_id)


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], bridge: Bridge, on_shutdown: Callable[[], None]):
        super().__init__(addr, Handler)
        self.bridge = bridge
        self._on_shutdown = on_shutdown
        self._shutdown_once = threading.Lock()
        self._shut = False

    def on_shutdown(self) -> None:
        with self._shutdown_once:
            if self._shut:
                return
            self._shut = True
            self._on_shutdown()
