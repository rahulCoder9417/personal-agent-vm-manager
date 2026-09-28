"""Minimal MCP (streamable HTTP) client for the Platform MCP's send_message tool.

The Bridge, not Hermes, sends each turn's final answer to the user. It calls
the Platform MCP with this container's bearer token, through the egress proxy
(urllib honours HTTP(S)_PROXY / NO_PROXY from the environment).
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


class PlatformMCP:
    def __init__(self, url: str, token: str, timeout: float = 30):
        self.url = url
        self.token = token
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._session: Optional[str] = None
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener()  # env proxies apply

    def _post(self, payload: dict, expect_response: bool = True) -> Optional[dict]:
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        if self._session:
            req.add_header("Mcp-Session-Id", self._session)
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and self._session:
                self._session = None  # session expired: caller re-initialises
            raise MCPError(f"HTTP {exc.code}") from exc
        except OSError as exc:
            raise MCPError(str(exc)) from exc
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session = sid
            if not expect_response:
                return None
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            return _last_sse_message(body, payload.get("id"))
        if not body.strip():
            return None
        return json.loads(body)

    def _initialize(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "personal-agent-bridge", "version": "0.1.0"},
            },
        }
        resp = self._post(msg)
        if not resp or "error" in resp:
            raise MCPError(f"initialize failed: {resp}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        with self._lock:
            for attempt in (1, 2):
                if self._session is None:
                    self._initialize()
                msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}
                try:
                    resp = self._post(msg)
                except MCPError:
                    if attempt == 1 and self._session is None:
                        continue  # session expired; initialise again once
                    raise
                if not resp:
                    raise MCPError("empty response")
                if "error" in resp:
                    raise MCPError(f"{name}: {resp['error']}")
                result = resp.get("result") or {}
                if result.get("isError"):
                    raise MCPError(f"{name} returned an error: {result.get('content')}")
                return result
        raise MCPError("unreachable")

    def send_message(self, text: str, session_key: str) -> dict:
        return self.call_tool("send_message", {"text": text, "session_key": session_key})


def _last_sse_message(body: str, want_id: Any) -> Optional[dict]:
    found = None
    for block in body.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if not data:
            continue
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and (want_id is None or msg.get("id") == want_id):
            found = msg
    return found
