"""HTTP server for the vm-manager (sandbox network, port BRIDGE_PORT).

GET  /healthz                no secret; 200 {"ok": true, "busy": n, "runtime": ..., "claude_connected": bool,
                             "claude_login_pending": bool} when the current runtime answers, else 503
POST /deliver                X-Bridge-Secret; JSON body; 200 NDJSON stream of events
POST /shutdown               X-Bridge-Secret; stops Hermes (saves to /data); 200 when done
POST /claude/login/start     X-Bridge-Secret; starts `claude auth login`; 200 {"login_id","authorize_url","expires_at"}
POST /claude/login/complete  X-Bridge-Secret; {"login_id","code"}; 200 {"connected","message"}; 409 unknown/expired
POST /claude/logout          X-Bridge-Secret; `claude auth logout`; 200 {"ok": true, "claude_connected": false}

Request bodies are never logged (the sign-in code passes through here).
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .claude import ClaudeAuth, LoginError, LoginNotFound
from .core import Bridge, DeliverRequest

log = logging.getLogger("bridge.server")

SECRET_HEADER = "X-Bridge-Secret"
MAX_BODY = 1 << 20
MAX_CODE = 4096
POST_PATHS = ("/deliver", "/shutdown", "/claude/login/start", "/claude/login/complete", "/claude/logout")


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
        body: dict[str, Any] = {"ok": False, "busy": bridge.busy()}
        try:
            body.update(self.server.health_extra())
        except Exception:  # noqa: BLE001 - health must always answer
            log.exception("health extras failed")
        if bridge.healthy():
            body["ok"] = True
            self._json(200, body)
        else:
            self._json(503, body)

    def do_POST(self) -> None:  # noqa: N802
        bridge = self.server.bridge
        if self.path not in POST_PATHS:
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
        if self.path.startswith("/claude/"):
            self._claude(raw)
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

    def _claude(self, raw: bytes) -> None:
        auth = self.server.claude
        if auth is None or not auth.installed():
            self._json(503, {"error": "claude code is not installed"})
            return
        if self.server.bridge.closing:
            self._json(503, {"error": "the agent is shutting down"})
            return
        try:
            if self.path == "/claude/login/start":
                self._json(200, auth.start_login())
            elif self.path == "/claude/logout":
                auth.logout()
                self._json(200, {"ok": True, "claude_connected": auth.connected()})
            else:
                self._complete(auth, raw)
        except LoginError as exc:
            log.warning("claude sign-in: %s", exc)
            self._json(502, {"error": str(exc)})

    def _complete(self, auth: ClaudeAuth, raw: bytes) -> None:
        try:
            body = json.loads(raw or b"null")
        except (ValueError, json.JSONDecodeError):
            body = None
        login_id = body.get("login_id") if isinstance(body, dict) else None
        code = body.get("code") if isinstance(body, dict) else None
        if isinstance(code, str):
            code = code.strip()
        if not isinstance(login_id, str) or not login_id or len(login_id) > 200 or \
                not isinstance(code, str) or not code or len(code) > MAX_CODE or \
                any(ord(c) < 32 or ord(c) == 127 for c in code):
            self._json(400, {"error": "login_id and a one-line code are required"})
            return
        try:
            self._json(200, auth.complete_login(login_id, code))
        except LoginNotFound:
            self._json(409, {"error": "unknown or expired sign-in; start again"})


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], bridge: Bridge, on_shutdown: Callable[[], None],
                 claude: Optional[ClaudeAuth] = None, health_extra: Optional[Callable[[], dict]] = None):
        super().__init__(addr, Handler)
        self.bridge = bridge
        self.claude = claude
        self.health_extra = health_extra or (lambda: {})
        self._on_shutdown = on_shutdown
        self._shutdown_once = threading.Lock()
        self._shut = False

    def on_shutdown(self) -> None:
        with self._shutdown_once:
            if self._shut:
                return
            self._shut = True
            self._on_shutdown()
