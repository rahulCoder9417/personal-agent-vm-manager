"""Test doubles: Hermes runner and Platform MCP sender."""

from __future__ import annotations

import threading
from typing import Callable

from bridge.core import RunResult, TurnOptions


class FakeRunner:
    def __init__(self, output: str = "Hi! How can I help?", status: str = "completed", reason: str = ""):
        self.output = output
        self.status = status
        self.reason = reason
        self.is_healthy = True
        self.calls: list[dict] = []
        self.block: threading.Event | None = None
        self.started = threading.Event()
        self.raise_exc: Exception | None = None
        self._lock = threading.Lock()

    def healthy(self) -> bool:
        return self.is_healthy

    def run(self, text: str, instructions: str, session_key: str, idempotency_key: str,
            on_progress: Callable[[str], None], timeout: float, options: TurnOptions | None = None) -> RunResult:
        with self._lock:
            self.calls.append({"text": text, "instructions": instructions, "session_key": session_key,
                               "idempotency_key": idempotency_key, "options": options})
        self.started.set()
        on_progress("using web_search")
        if self.block is not None:
            self.block.wait(5)
        if self.raise_exc:
            raise self.raise_exc
        return RunResult(status=self.status, output=self.output, reason=self.reason)


class FakeSender:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.sent: list[tuple[str, str]] = []

    def send_message(self, text: str, session_key: str) -> dict:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("platform mcp down")
        self.sent.append((text, session_key))
        return {"content": [{"type": "text", "text": "sent"}]}
