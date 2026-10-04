"""Bridge core: dedupe by delivery_id, run one Hermes turn per delivery, send the
final answer through the Platform MCP, and report NDJSON events.

Events (one JSON object per line):
    {"type": "accepted" | "working" | "turn_done" | "error", "message": str, "platform_fault": bool}

The last event of a turn (turn_done or error) also carries the browser time to charge, when Scout's
browser was open: "browser_seconds" (whole seconds since the Bridge's last report) and
"browser_report_id" (so the vm-manager stores a repeated report once). Both are left out when the
browser never started.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Protocol

from . import crew

log = logging.getLogger("bridge.core")

ACCEPTED, WORKING, TURN_DONE, ERROR = "accepted", "working", "turn_done", "error"
KINDS = {"message", "approval_answer", "automation", "app_event"}
MAX_TEXT = 100_000


def event(typ: str, message: str = "", platform_fault: bool = False, **extra: Any) -> dict:
    ev = {"type": typ, "message": message, "platform_fault": platform_fault}
    ev.update(extra)
    return ev


def browser_fields(rec: Optional[dict]) -> dict:
    """The browser report saved with a delivery, as the fields of its last event ({} when there is none)."""
    report = (rec or {}).get("browser")
    if not isinstance(report, dict):
        return {}
    seconds, report_id = report.get("browser_seconds"), report.get("browser_report_id")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0 or not isinstance(report_id, str):
        return {}
    return {"browser_seconds": seconds, "browser_report_id": report_id}


@dataclass
class Attachment:
    id: str = ""
    name: str = ""
    mime_type: str = ""
    url: str = ""


@dataclass
class DeliverRequest:
    delivery_id: str
    kind: str
    session_key: str
    channel: str = ""
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    approval_id: str = ""
    approved: bool = False
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: Any) -> "DeliverRequest":
        if not isinstance(raw, dict):
            raise ValueError("body must be a JSON object")
        did = raw.get("delivery_id")
        if not isinstance(did, str) or not did or len(did) > 200:
            raise ValueError("delivery_id is required (max 200 chars)")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        sk = raw.get("session_key") or "default"
        if not isinstance(sk, str) or len(sk) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in sk):
            raise ValueError("session_key must be <= 256 printable characters")
        text = raw.get("text") or ""
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError("text too long")
        atts = []
        for a in raw.get("attachments") or []:
            if not isinstance(a, dict):
                raise ValueError("bad attachment")
            atts.append(Attachment(**{k: str(a.get(k) or "") for k in ("id", "name", "mime_type", "url")}))
        meta = raw.get("metadata") or {}
        return cls(
            delivery_id=did, kind=kind, session_key=sk, channel=str(raw.get("channel") or ""), text=text,
            attachments=atts, approval_id=str(raw.get("approval_id") or ""), approved=bool(raw.get("approved")),
            metadata=meta if isinstance(meta, dict) else {"value": meta},
        )


def build_input(req: DeliverRequest) -> str:
    parts: list[str] = []
    if req.kind == "approval_answer":
        verdict = "APPROVED" if req.approved else "REJECTED"
        parts.append(f"[Approval {req.approval_id or '?'}: the user {verdict} the pending action. "
                     f"Continue accordingly.]")
    elif req.kind == "automation":
        parts.append("[Scheduled automation: carry out this saved instruction now.]")
    elif req.kind == "app_event":
        parts.append("[An event arrived from one of the user's connected apps.]")
    if req.text:
        parts.append(req.text)
    for a in req.attachments:
        parts.append(f"[Attachment: {a.name or a.id} ({a.mime_type or 'unknown type'}) {a.url}]")
    return "\n".join(parts) or "(empty message)"


def build_instructions(req: DeliverRequest) -> str:
    """The lead's instructions for one turn. Each engine adds how it hands work to a specialist
    (crew.CLAUDE_HANDOFF / crew.hermes_handoff)."""
    channel = req.channel or "chat"
    return (
        f"The user is talking to you through {channel}. Your final answer for this turn is delivered to "
        "them automatically, so do not try to send it yourself. Keep replies suited to a chat app. "
        "Before anything risky (sending to other people, deleting, paying, posting publicly) call "
        "request_approval and end your turn; the user's answer arrives as a later message. "
        "When the user's GitHub is connected, git works for their repositories: clone "
        "https://github.com/<owner>/<repo> into your workspace, commit on a new branch and push it, "
        "then open the pull request with the GitHub app action. Pushing to a repository's default "
        "branch is refused, so always work on a branch.\n\n"
        + crew.lead_instructions()
    )


@dataclass
class RunResult:
    status: str
    output: str
    error: str = ""
    reason: str = ""
    message: str = ""  # shown to the user on failure (default: "the agent could not finish (...)")
    platform_fault: Optional[bool] = None  # None: decided from reason


class Runner(Protocol):
    def healthy(self) -> bool: ...

    def run(self, text: str, instructions: str, session_key: str, idempotency_key: str,
            on_progress: Callable[[str], None], timeout: float) -> Any: ...


class Sender(Protocol):
    def send_message(self, text: str, session_key: str) -> Any: ...


class BrowserUsage(Protocol):
    """What the Bridge needs from the browser meter (browser.BrowserMeter)."""

    def end_turn(self, last: bool) -> int: ...


class DeliveryStore:
    """Finished deliveries, persisted in /data so a retry after a restart is not re-run.

    state "answered": Hermes produced the answer but sending it failed (a retry only re-sends).
    state "done": answer sent; a repeat delivery_id is acknowledged without running again.
    """

    def __init__(self, path: Optional[str], max_entries: int = 1000):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._data = loaded
            except (OSError, json.JSONDecodeError):
                log.warning("could not read %s; starting empty", path)

    def get(self, delivery_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._data.get(delivery_id)
            return dict(rec) if rec else None

    def put(self, delivery_id: str, **fields: Any) -> None:
        with self._lock:
            rec = self._data.get(delivery_id, {})
            rec.update(fields)
            rec["at"] = time.time()
            self._data[delivery_id] = rec
            if len(self._data) > self.max_entries:
                oldest = sorted(self._data.items(), key=lambda kv: kv[1].get("at", 0))
                for k, _ in oldest[: len(self._data) - self.max_entries]:
                    del self._data[k]
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh)
        os.replace(tmp, self.path)


class Turn:
    """Events of one in-progress delivery, fanned out to every subscriber."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.done = False
        self.cond = threading.Condition()

    def publish(self, ev: dict) -> None:
        with self.cond:
            self.events.append(ev)
            if ev["type"] in (TURN_DONE, ERROR):
                self.done = True
            self.cond.notify_all()

    def subscribe(self) -> Iterator[dict]:
        i = 0
        while True:
            with self.cond:
                while i >= len(self.events) and not self.done:
                    self.cond.wait()
                if i >= len(self.events):
                    return
                ev = self.events[i]
                i += 1
            yield ev
            if ev["type"] in (TURN_DONE, ERROR):
                return


class Bridge:
    def __init__(self, secret: str, runner: Runner, sender: Sender, store: DeliveryStore,
                 turn_timeout: float = 900, send_retries: int = 3, retry_delay: float = 1.0,
                 browser: Optional[BrowserUsage] = None):
        if not secret:
            raise ValueError("bridge secret is required")
        self._secret = secret.encode()
        self.runner = runner
        self.sender = sender
        self.store = store
        self.turn_timeout = turn_timeout
        self.send_retries = send_retries
        self.retry_delay = retry_delay
        self.browser = browser  # None: this container has no browser (it is not turned on for the user)
        self._turns: dict[str, Turn] = {}
        self._lock = threading.Lock()
        self.closing = False

    def check_secret(self, given: Optional[str]) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self._secret)

    def busy(self) -> int:
        with self._lock:
            return len(self._turns)

    def healthy(self) -> bool:
        return not self.closing and self.runner.healthy()

    def deliver(self, req: DeliverRequest) -> Iterator[dict]:
        with self._lock:
            rec = self.store.get(req.delivery_id)
            if rec and rec.get("state") == "done":
                duplicate = True
                turn = None
            else:
                duplicate = False
                turn = self._turns.get(req.delivery_id)
                if turn is None:
                    if self.closing:
                        turn = None
                    else:
                        turn = Turn()
                        self._turns[req.delivery_id] = turn
                        threading.Thread(target=self._run, args=(req, turn, rec), daemon=True,
                                         name=f"turn-{req.delivery_id[:16]}").start()
        if duplicate:
            yield event(ACCEPTED)
            # The browser report of the finished turn goes out again: if the first one was lost on the
            # way, this one is stored; if not, the vm-manager knows its id and drops it.
            yield event(TURN_DONE, "duplicate delivery; already handled", **browser_fields(rec))
            return
        if turn is None:
            yield event(ERROR, "the agent is shutting down", platform_fault=True)
            return
        yield from turn.subscribe()

    def _run(self, req: DeliverRequest, turn: Turn, rec: Optional[dict]) -> None:
        try:
            turn.publish(event(ACCEPTED))
            output: Optional[str] = rec.get("output") if rec and rec.get("state") == "answered" else None
            if output is None:
                attempts = int((rec or {}).get("attempts", 0)) + 1
                self.store.put(req.delivery_id, state="running", attempts=attempts)
                result = self.runner.run(
                    text=build_input(req),
                    instructions=build_instructions(req),
                    session_key=req.session_key,
                    idempotency_key=f"{req.delivery_id}#{attempts}",
                    on_progress=lambda msg: turn.publish(event(WORKING, msg)),
                    timeout=self.turn_timeout,
                )
                if result.status != "completed":
                    reason = getattr(result, "reason", "") or ""
                    fault = getattr(result, "platform_fault", None)
                    if fault is None:
                        # The agent did real work and hit its own limit: not a platform fault.
                        fault = not reason.startswith("max_iterations")
                    message = getattr(result, "message", "") or f"the agent could not finish ({result.status})"
                    self.store.put(req.delivery_id, state="failed")
                    self._end(req, turn, event(ERROR, message, platform_fault=fault))
                    return
                output = result.output or ""
                self.store.put(req.delivery_id, state="answered", output=output)
            if output.strip():
                if not self._send(output, req.session_key):
                    self._end(req, turn, event(ERROR, "could not deliver the reply", platform_fault=True))
                    return
            self.store.put(req.delivery_id, state="done", output=None)
            self._end(req, turn, event(TURN_DONE))
        except Exception as exc:  # noqa: BLE001 - every failure must end the stream
            log.exception("turn %s failed", req.delivery_id)
            self._end(req, turn, event(ERROR, f"agent error: {type(exc).__name__}", platform_fault=True))
        finally:
            with self._lock:
                self._turns.pop(req.delivery_id, None)

    def _end(self, req: DeliverRequest, turn: Turn, ev: dict) -> None:
        """Publish a turn's last event, with the browser time to charge (when the browser was open)."""
        try:
            ev.update(self._browser_report(req.delivery_id))
        except Exception:  # noqa: BLE001 - the turn must end whatever the meter does
            log.exception("browser report for %s failed", req.delivery_id)
        turn.publish(ev)

    def _browser_report(self, delivery_id: str) -> dict:
        """Close the browser (unless another turn is still running) and return what to report: the
        seconds it was open since the last report, under an id of its own. {} when it never started.
        The report is saved with the delivery, so a repeat of the delivery sends it again."""
        if self.browser is None:
            return {}
        with self._lock:
            last = len(self._turns) <= 1  # this turn is still in the table
        seconds = self.browser.end_turn(last)
        if seconds <= 0:
            return {}
        number = int((self.store.get(delivery_id) or {}).get("browser_reports", 0)) + 1
        report = {"browser_seconds": seconds, "browser_report_id": f"{delivery_id}#{number}"}
        self.store.put(delivery_id, browser_reports=number, browser=report)
        return report

    def _send(self, text: str, session_key: str) -> bool:
        for attempt in range(1, self.send_retries + 1):
            try:
                self.sender.send_message(text, session_key)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("send_message attempt %d failed: %s", attempt, exc)
                if attempt < self.send_retries:
                    time.sleep(self.retry_delay * attempt)
        return False

    def shutdown(self, stop_runner: Callable[[], None], wait_turns: float = 5.0) -> None:
        """Stop taking work, give running turns a moment, then stop Hermes (it saves to /data)."""
        self.closing = True
        deadline = time.monotonic() + wait_turns
        while self.busy() and time.monotonic() < deadline:
            time.sleep(0.1)
        stop_runner()
