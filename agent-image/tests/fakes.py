"""Test doubles: Hermes runner and Platform MCP sender."""

from __future__ import annotations

import threading
import time
from typing import Callable

from bridge.core import RunResult, TurnOptions

# Every model id the backend offers for Claude Code (its CLAUDE_MODELS, 2026-10-13), as it sends them in a delivery's
# `claude.model`: the current models, then the older ones Claude still offers. All are in the model table of the
# pinned Claude Code 2.1.296.
CLAUDE_CODE_MODELS = (
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1",
    "claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-4-6", "claude-opus-4-5", "claude-haiku-4-5",
)


def without_timings(ev: dict) -> dict:
    """An event without the stage times the Bridge adds to a turn's last event (timings.py)."""
    return {k: v for k, v in ev.items() if k != "timings"}


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
        # What the lead "writes" while it works: each item is passed to on_text (None starts the draft over).
        self.pieces: list[str | None] = []
        self.piece_delay = 0.0  # seconds between two pieces
        self._lock = threading.Lock()

    def healthy(self) -> bool:
        return self.is_healthy

    def run(self, text: str, instructions: str, session_key: str, idempotency_key: str,
            on_progress: Callable[[str], None], timeout: float, options: TurnOptions | None = None,
            on_text: Callable[[str | None], None] | None = None) -> RunResult:
        with self._lock:
            self.calls.append({"text": text, "instructions": instructions, "session_key": session_key,
                               "idempotency_key": idempotency_key, "options": options, "on_text": on_text})
        self.started.set()
        on_progress("using web_search")
        if on_text is not None:
            for piece in self.pieces:
                on_text(piece)
                if self.piece_delay:
                    time.sleep(self.piece_delay)
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
