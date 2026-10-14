"""Where one delivery's time goes, as the Bridge sees it (README "Bridge contract", "Stage times").

The Bridge starts a TurnClock when it takes a delivery, the engine marks what happens during the run, and the
report goes out on the turn's last event (`turn_done` or `error`) as "timings". The backend stores it next to its
own stage times, so a person can see where a slow answer spent its time: the engine's start, the model's first
words, tools, the crew, sending the answer.

Every time is in whole milliseconds since the Bridge took the delivery, on the Bridge's own monotonic clock, so
the report does not depend on the container's wall clock. Nothing in it is text the user or the agent wrote:
only stage names, numbers, the engine's name and the crew members' ids.

    {"v": 1, "engine": "claude_code", "runs": 1,
     "marks": {"engine_start": 12, "engine_ready": 1840, "first_response": 3410, "first_thinking": 3420,
               "first_text": 6120, "first_tool": 4200, "engine_done": 9300, "answer_sent": 9380},
     "tools_ms": 640, "crew_ms": 0, "crew": [{"member": "scout", "start": 5000, "end": 61000}],
     "cli": {"duration_ms": 7400, "duration_api_ms": 6100, "num_turns": 2, "input_tokens": 12,
             "cache_read_input_tokens": 41000, "cache_creation_input_tokens": 900, "output_tokens": 310}}

Marks: engine_start (the CLI process is started / the Hermes run is asked for), engine_ready (Claude Code's `init`
event: the CLI is up and its tool servers answered / Hermes created the run), first_response (Claude Code: the
model's first message began; only with the answer streamed, i.e. web chat), first_thinking (Claude Code: the model
started to think; web chat only), first_text (the lead's first words), first_tool (the lead's first tool call),
engine_done (the engine's last run ended), answer_sent (the answer went through send_message). A mark that did
not happen is left out. tools_ms: the time at least one of the lead's own tool calls was running (overlapping calls
count once). crew_ms: the same for specialists. cli: the numbers Claude Code's `result` event gives about the run
(its own total, the time in API calls, the model round trips and the token counts), Claude Code only.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

VERSION = 1
MAX_CREW = 20  # crew runs listed in one report (the time of all of them is in crew_ms)
CLI_FIELDS = ("duration_ms", "duration_api_ms", "num_turns")
USAGE_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def _whole(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        return None
    return int(value)


class _Spans:
    """The time at least one of a set of things (tool calls, specialists) was running: overlaps count once."""

    def __init__(self, now: Callable[[], int]):
        self._now = now
        self._open: dict[str, int] = {}
        self._since: Optional[int] = None
        self.total = 0

    def start(self, key: str) -> None:
        if not self._open:
            self._since = self._now()
        self._open[key] = self._open.get(key, 0) + 1

    def end(self, key: str) -> bool:
        if key not in self._open:
            return False
        self._open[key] -= 1
        if self._open[key] <= 0:
            del self._open[key]
        if not self._open and self._since is not None:
            self.total += self._now() - self._since
            self._since = None
        return True

    def so_far(self) -> int:
        return self.total + (self._now() - self._since if self._open and self._since is not None else 0)


class TurnClock:
    """The stage times of one delivery (module doc). Safe to call from the engine's threads."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self.engine = ""
        self.runs = 0
        self._marks: dict[str, int] = {}
        self._tools = _Spans(self.now)
        self._crew_spans = _Spans(self.now)
        self._crew: dict[str, dict] = {}
        self._cli: dict[str, int] = {}

    def now(self) -> int:
        return max(round((self._clock() - self._t0) * 1000), 0)

    def mark(self, name: str, last: bool = False) -> None:
        """Note that `name` happened now: the first time only, or every time (the last one is kept) with last."""
        with self._lock:
            if last or name not in self._marks:
                self._marks[name] = self.now()

    def run_started(self, engine: str) -> None:
        """An engine run starts (a turn may take more than one: a lost session, the crew's follow-up rounds)."""
        with self._lock:
            self.engine = engine
            self.runs += 1
            self._marks.setdefault("engine_start", self.now())

    def tool_start(self, key: Any) -> None:
        with self._lock:
            self._tools.start(str(key or "?"))

    def tool_end(self, key: Any) -> None:
        with self._lock:
            self._tools.end(str(key or "?"))

    def crew_start(self, run_id: Any, member: str) -> None:
        with self._lock:
            key = str(run_id or "?")
            if key in self._crew:
                return
            self._crew_spans.start(key)
            self._crew[key] = {"member": member, "start": self.now()}

    def crew_end(self, run_id: Any) -> None:
        with self._lock:
            key = str(run_id or "?")
            run = self._crew.get(key)
            if run is None or "end" in run:
                return
            self._crew_spans.end(key)
            run["end"] = self.now()

    def cli_result(self, result: dict) -> None:
        """The numbers of Claude Code's `result` event (never its text)."""
        with self._lock:
            for name in CLI_FIELDS:
                value = _whole(result.get(name))
                if value is not None:
                    self._cli[name] = value
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            for name in USAGE_FIELDS:
                value = _whole(usage.get(name))
                if value is not None:
                    self._cli[name] = value

    def report(self) -> dict:
        """The report for the turn's last event."""
        with self._lock:
            out: dict[str, Any] = {"v": VERSION, "engine": self.engine, "runs": self.runs,
                                   "marks": dict(self._marks), "tools_ms": self._tools.so_far(),
                                   "crew_ms": self._crew_spans.so_far(),
                                   "crew": [dict(run) for run in list(self._crew.values())[:MAX_CREW]]}
            if self._cli:
                out["cli"] = dict(self._cli)
            return out
