"""Hermes Agent: the child process and a client for its local Runs API.

Hermes runs as `hermes gateway run` with only its API server enabled, bound to
127.0.0.1 and protected by a random per-boot key that never leaves this process
tree. One Hermes run = one delivered message (plus follow-up runs when the lead handed work
to its crew, see HermesClient.run). Hermes keeps one conversation per
`X-Hermes-Session-Key`: we pass the platform's session key (e.g. "telegram:42"), with a suffix
that tells the chat's conversations apart (Conversations).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from . import crew

log = logging.getLogger("bridge.hermes")

# A conversation's number (`chat_epoch`) as a key of hermes-conversations.json.
_EPOCH_KEY = re.compile(r"[0-9]{1,18}")

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}

# The crew on Hermes: follow-up turns after specialists that ran in the background.
MAX_CREW_ROUNDS = 3
CREW_FOLLOW_UP = ("[Crew update: the specialists you started have finished, and their reports are now in this "
                  "conversation. Use them to write the final answer for the user. Start another specialist only "
                  "if the job still needs one.]")
_DISPATCHED = '"status": "dispatched"'  # start of delegate_task's answer when the children run in the background
_CHILD_OUTCOMES = {"completed": crew.DONE, "failed": crew.FAILED, "error": crew.FAILED, "timeout": crew.FAILED,
                   "interrupted": crew.STOPPED}

# The notice Hermes writes into the answer's text when the model's stream broke during a tool call and it asks
# the model again: "\n\n⚠ Connection dropped mid tool-call; reconnecting…\n\n", then the words before the tool
# call come again (the pinned commit, agent/chat_completion_helpers.py). The draft starts over at it.
_RECONNECTING = "⚠ Connection dropped mid tool-call; reconnecting"


class DraftText:
    """The lead's words from the Runs API's `message.delta` events ({"delta": "..."}, one per piece the model
    wrote; api_server_runs.py `_text_cb` at the pinned commit), for the draft of the answer (core.Draft, the
    `on_text` hook).

    Hermes sends the deltas of the whole run in one stream: the words before a tool call, then (after a paragraph
    break it adds itself) those of the next model call. Its final answer (`output`) is the last model call's
    text only. So the draft starts over (on_text(None)) when a tool starts (`tool.started`, which also covers
    handing work to the crew), when a run starts (each follow-up round of the crew is a run of its own), and at
    the reconnect notice above: what came before it is written again after it, and the notice itself is not
    part of the answer. `message.interim` (the text beside a tool call, already streamed) adds nothing.
    Only the lead's deltas are in this stream: a specialist's run has none here."""

    def __init__(self, on_text):
        self.on_text = on_text

    def restart(self) -> None:
        self.on_text(None)

    def delta(self, text: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        if _RECONNECTING in text:
            self.restart()
            text = text.split(_RECONNECTING, 1)[1].lstrip("…. \n")
            if not text:
                return
        self.on_text(text)


def _child_id(ev: dict) -> str:
    return str(ev.get("subagent_id") or f"{ev.get('delegation_id') or ''}#{ev.get('task_index') or 0}")


class HermesError(Exception):
    """Hermes could not run the turn (not the user's fault)."""


class Conversations:
    """The X-Hermes-Session-Key of each conversation of each chat, persisted in /data.

    A chat is a session_key. Its conversations are numbered by the backend (`chat_epoch`: 0, then one more at
    each "New chat"), and each has its own Hermes conversation, so its own key. A key is the session key with a
    suffix: none for the chat's first key, then `#1`, `#2`, ... in the order the keys were handed out; a key
    Hermes has not seen starts a fresh session (the API server docs: "A request without any continuation starts
    a fresh session"). One record per chat:

        {"generation": n, "epoch": e, "epochs": {"<epoch>": suffix number, ...}}

    `generation` is the highest suffix handed out so far (a key is never handed out twice), `epoch` the
    conversation of the chat's last turn on this engine (a delivery that names no conversation goes on in it),
    and `epochs` the suffix of each conversation Hermes has run. A chat with no record has run conversation 0
    under the bare session key, or nothing.

    A turn of a conversation Hermes has no key for (the user started a new chat, or the conversation never ran
    on this engine) gets the next key, before the turn runs. A turn of a conversation that has a key goes on
    under it, whether it is the chat's latest or an earlier one the user opened again.

    A message whose turn failed is never acted on later. Hermes saves a turn to its session on every exit path, a
    failed one included (agent/session_persistence.py `_persist_session`: "Save to SQLite on any exit path";
    agent/turn_recovery.py persists before it returns a failed result, at the pinned commit), and the next run
    with the same key loads that session again (api_server_runs.py: a declared X-Hermes-Session-Key conversation
    loads its transcript). Its API has no documented way to take a turn back (`/undo` is a chat command of its
    messaging platforms, not of the API server). So after a failed turn that conversation gets the next key
    (failed): it starts afresh on Hermes, and the chat's other conversations keep theirs. What carries over: the
    platform's memory (remember / recall) and Hermes's session_search over earlier conversations.

    An older Bridge kept one key per chat: {"generation": n, "epoch": e}, and before that the bare number n
    (epoch 0). That key is the one of that epoch, so the record is read with "epochs": {"<e>": n} and the chat's
    current conversation goes on. The earlier conversations of such a chat have no key: the older Bridge did not
    keep which suffix was whose."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        rec = _chat_record(v)
                        if rec != _first_record():
                            self._data[k] = rec
            except (OSError, json.JSONDecodeError):
                log.warning("could not read %s; starting empty", path)

    def enter(self, session_key: str, epoch: Optional[int]) -> tuple[int, str, str]:
        """The conversation a turn runs in. `epoch` is the delivery's `chat_epoch`; None (the delivery names
        none) is the conversation of the chat's last turn. Returns (epoch, its X-Hermes-Session-Key, "same" |
        "new" | "reopened"): the conversation of the last turn, one that had no key and got the next one, or
        another one that has a key. Written to /data before the turn runs."""
        with self._lock:
            rec = self._data.get(session_key) or _first_record()
            if epoch is None:
                epoch = rec["epoch"]
            moved = "same"
            if str(epoch) not in rec["epochs"]:
                rec["generation"] += 1
                rec["epochs"][str(epoch)] = rec["generation"]
                moved = "new"
            elif epoch != rec["epoch"]:
                moved = "reopened"
            if moved != "same":
                rec["epoch"] = epoch
                self._data[session_key] = rec
                self._save()
            return epoch, _key(session_key, rec["epochs"][str(epoch)]), moved

    def key(self, session_key: str) -> str:
        """The key of the conversation of the chat's last turn."""
        with self._lock:
            rec = self._data.get(session_key) or _first_record()
            return _key(session_key, rec["epochs"][str(rec["epoch"])])

    def failed(self, session_key: str, epoch: Optional[int] = None) -> None:
        """A turn of this conversation failed (None: the one of the chat's last turn): its next turn starts a new
        Hermes conversation. The chat's other conversations keep theirs."""
        with self._lock:
            rec = self._data.setdefault(session_key, _first_record())
            rec["generation"] += 1
            rec["epochs"][str(rec["epoch"] if epoch is None else epoch)] = rec["generation"]
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh)
        os.replace(tmp, self.path)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _first_record() -> dict[str, Any]:
    """A chat Hermes has run under the bare session key only, or not at all: conversation 0, no suffix."""
    return {"generation": 0, "epoch": 0, "epochs": {"0": 0}}


def _chat_record(raw: Any) -> dict[str, Any]:
    """One chat's record of hermes-conversations.json as Conversations keeps it, from what is in the file: this
    layout, an older Bridge's {"generation", "epoch"} (one key, the one of that epoch), or the bare generation
    of the Bridge before that (epoch 0)."""
    if not isinstance(raw, dict):
        return {"generation": _count(raw), "epoch": 0, "epochs": {"0": _count(raw)}}
    generation, epoch = _count(raw.get("generation")), _count(raw.get("epoch"))
    if not isinstance(raw.get("epochs"), dict):
        return {"generation": generation, "epoch": epoch, "epochs": {str(epoch): generation}}
    epochs: dict[str, int] = {}
    for number, suffix in raw["epochs"].items():
        if isinstance(number, str) and _EPOCH_KEY.fullmatch(number) and isinstance(suffix, int) \
                and not isinstance(suffix, bool) and suffix >= 0:
            epochs[str(int(number))] = suffix
    # A key is never handed out twice: the next one comes after every suffix in the file.
    generation = max([generation, *epochs.values()])
    if str(epoch) not in epochs:  # the file does not say which key the last conversation has: it gets a new one
        generation += 1
        epochs[str(epoch)] = generation
    return {"generation": generation, "epoch": epoch, "epochs": epochs}


def _key(session_key: str, generation: int) -> str:
    """The X-Hermes-Session-Key for a chat's suffix number (0: the session key itself)."""
    if not generation:
        return session_key
    suffix = f"#{generation}"
    if len(session_key) + len(suffix) <= 256:  # Hermes takes at most 256 characters
        return session_key + suffix
    return f"{session_key[:200]}~{hashlib.sha256(session_key.encode()).hexdigest()[:16]}{suffix}"


@dataclass
class RunResult:
    status: str  # completed | failed | cancelled | interrupted
    output: str
    error: str = ""
    reason: str = ""  # Hermes turn_exit_reason, e.g. "max_iterations_reached(60/60)"


class HermesClient:
    """Minimal client for Hermes's local API server (no proxy: loopback only)."""

    def __init__(self, base_url: str, api_key: str, approval_choice: str = "deny", poll_interval: float = 2.0,
                 with_browser: bool = False, conversations_path: Optional[str] = None):
        self.with_browser = with_browser  # Scout's browser is turned on for this user: the briefs say so
        self.conversations = Conversations(conversations_path)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.approval_choice = approval_choice
        self.poll_interval = poll_interval  # between checks on specialists that work in the background
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, method: str, path: str, body: Optional[dict] = None, headers: Optional[dict] = None,
             timeout: float = 30) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return self._opener.open(req, timeout=timeout)

    def healthy(self) -> bool:
        try:
            with self._req("GET", "/health", timeout=3) as resp:
                return resp.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def start_run(self, text: str, instructions: str, session_key: str, idempotency_key: str) -> str:
        headers = {"X-Hermes-Session-Key": session_key, "Idempotency-Key": idempotency_key}
        try:
            with self._req("POST", "/v1/runs", {"input": text, "instructions": instructions}, headers) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise HermesError(f"start run: HTTP {exc.code}") from exc
        except OSError as exc:
            raise HermesError(f"start run: {exc}") from exc
        run_id = payload.get("run_id")
        if not run_id:
            raise HermesError("start run: no run_id")
        return run_id

    def events(self, run_id: str, timeout: float) -> Iterator[dict]:
        """Yield SSE events (dicts with an "event" key) until a terminal one or the stream ends."""
        try:
            resp = self._req("GET", f"/v1/runs/{run_id}/events", headers={"Accept": "text/event-stream"},
                             timeout=timeout)
        except (OSError, urllib.error.URLError) as exc:
            raise HermesError(f"events: {exc}") from exc
        with resp:
            data_lines: list[str] = []
            event_name = ""
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line == "" and data_lines:
                    try:
                        ev = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        ev = {}
                    data_lines, name = [], event_name
                    event_name = ""
                    if not isinstance(ev, dict):
                        continue
                    ev.setdefault("event", name)
                    yield ev
                    if ev.get("event") in TERMINAL_EVENTS:
                        return

    def status(self, run_id: str) -> dict:
        with self._req("GET", f"/v1/runs/{run_id}") as resp:
            return json.loads(resp.read() or b"{}")

    def resolve_approval(self, run_id: str) -> None:
        """Hermes's own approval prompts (dangerous commands, untrusted MCP writes) have no
        user to answer them here; resolve them by policy. Risky real-world actions go through
        the platform's request_approval tool instead."""
        try:
            with self._req("POST", f"/v1/runs/{run_id}/approval", {"choice": self.approval_choice, "all": True}):
                pass
        except (OSError, urllib.error.URLError) as exc:
            log.warning("could not resolve hermes approval: %s", exc)

    def stop_run(self, run_id: str) -> None:
        try:
            with self._req("POST", f"/v1/runs/{run_id}/stop", {}):
                pass
        except (OSError, urllib.error.URLError):
            pass

    def run(self, text: str, instructions: str, session_key: str, idempotency_key: str,
            on_progress, timeout: float = 900, options: Any = None, on_text=None) -> RunResult:
        """Run one turn to the end. on_progress(message) is called for tool activity.

        The crew: on this API Hermes runs the lead's specialists in the background. The lead's run
        ends with a short status, the specialists' reports are stored in the session when they
        finish, and the client has to start the next turn. So after a run that handed work over,
        wait for the specialists and run again in the same session; the last run's answer is the
        turn's answer.

        `options` (core.TurnOptions): only `chat_epoch` is used here. It names the conversation of the chat this
        turn belongs to, and each has its own Hermes conversation (Conversations): a new chat starts one, an
        earlier conversation the user opened again goes on in its own. The Claude Code model and effort are not
        Hermes's.

        `on_text` (core.Draft; None = no draft for this turn): the lead's words as the model writes them (DraftText).
        """
        instructions = f"{instructions}\n\n{crew.hermes_handoff(self.with_browser)}"
        deadline = time.monotonic() + timeout
        progress = crew.Progress(on_progress)
        # The Hermes conversation of this turn's conversation of the chat; a new one for a new chat, and for a
        # conversation whose last turn failed (Conversations).
        epoch, key, moved = self.conversations.enter(session_key, getattr(options, "chat_epoch", None))
        if moved == "new":
            log.info("hermes: a new chat for this session (chat %d); it starts a new conversation", epoch)
        elif moved == "reopened":
            log.info("hermes: back in another chat of this session (chat %d); it goes on in that chat's own "
                     "conversation", epoch)
        started: list[str] = []
        draft = DraftText(on_text) if on_text is not None else None
        try:
            result, handed_over = self._run_once(text, instructions, key, idempotency_key, on_progress,
                                                 progress, deadline, started, draft)
            rounds = 0
            while result.status == "completed" and handed_over and rounds < MAX_CREW_ROUNDS:
                if not self._wait_for_crew(deadline):
                    break  # Hermes does not say whether they are done: keep the lead's status as the answer
                progress.finish_all()
                rounds += 1
                result, handed_over = self._run_once(CREW_FOLLOW_UP, instructions, key,
                                                     f"{idempotency_key}/crew{rounds}", on_progress, progress,
                                                     deadline, started, draft)
        except Exception:
            if started:  # Hermes has the turn's message in its session: never continue that conversation
                self._turn_failed(session_key, epoch, idempotency_key)
            raise
        if result.status != "completed":
            self._turn_failed(session_key, epoch, idempotency_key)
        return result

    def _turn_failed(self, session_key: str, epoch: int, idempotency_key: str) -> None:
        self.conversations.failed(session_key, epoch)
        log.info("hermes turn %s failed; the next turn of that chat (chat %d) starts a new conversation",
                 idempotency_key, epoch)

    def _run_once(self, text: str, instructions: str, session_key: str, idempotency_key: str, on_progress,
                  progress: crew.Progress, deadline: float,
                  started: Optional[list] = None, draft: Optional[DraftText] = None) -> tuple[RunResult, bool]:
        """One Hermes run. Returns (result, whether specialists are still working in the background).
        `session_key` is the conversation's X-Hermes-Session-Key; `started` gets the run id once Hermes has it;
        `draft` gets the lead's words (None: no draft for this turn)."""
        if draft is not None:
            draft.restart()  # a follow-up round writes a new answer
        run_id = self.start_run(text, instructions, session_key, idempotency_key)
        if started is not None:
            started.append(run_id)
        children: set[str] = set()  # started and not yet finished
        detached = False
        try:
            for ev in self.events(run_id, timeout=max(deadline - time.monotonic(), 1)):
                name = ev.get("event", "")
                if name == "message.delta":
                    if draft is not None:
                        draft.delta(ev.get("delta"))
                elif name == "tool.started":
                    if draft is not None:
                        draft.restart()
                    on_progress(f"using {ev.get('tool') or 'a tool'}")
                elif name == "tool.completed" and ev.get("tool") == crew.HERMES_DELEGATE_TOOL:
                    detached = detached or _DISPATCHED in str(ev.get("preview") or "")
                elif name == "subagent.start":
                    children.add(_child_id(ev))
                    member, what = crew.from_goal(ev.get("goal") or ev.get("preview"))
                    if member is not None:
                        progress.start(_child_id(ev), member, what)
                elif name == "subagent.complete":
                    children.discard(_child_id(ev))
                    progress.finish(_child_id(ev), _CHILD_OUTCOMES.get(str(ev.get("status") or ""), crew.FINISHED))
                elif name == "approval.request":
                    self.resolve_approval(run_id)
                elif name in TERMINAL_EVENTS:
                    return _result(name.split(".", 1)[1], ev), detached or bool(children)
        except HermesError:
            raise
        except OSError as exc:  # stream broke; fall back to polling
            log.warning("run %s event stream broke: %s", run_id, exc)
        # Stream ended without a terminal event: poll the run status.
        while time.monotonic() < deadline:
            try:
                st = self.status(run_id)
            except (OSError, urllib.error.URLError) as exc:
                raise HermesError(f"status: {exc}") from exc
            if st.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                return _result(st["status"], st), detached or bool(children)
            time.sleep(1)
        self.stop_run(run_id)
        raise HermesError("run did not finish in time")

    def background_work(self) -> Optional[int]:
        """Delegations Hermes still runs, plus finished ones it has not stored in their session yet
        (GET /health/detailed). None when Hermes does not say."""
        try:
            with self._req("GET", "/health/detailed", timeout=10) as resp:
                payload = json.loads(resp.read() or b"{}")
            queues = payload["readiness"]["checks"]["background_queues"]
            return int(queues["active_delegations"]) + int(queues.get("process_completions") or 0)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _wait_for_crew(self, deadline: float) -> bool:
        """Wait until the background specialists are done (two quiet checks in a row). False when
        Hermes does not report it; HermesError when they are still working at the deadline."""
        quiet = unknown = 0
        while time.monotonic() < deadline:
            work = self.background_work()
            if work is None:
                unknown += 1
                if unknown >= 3:
                    log.warning("hermes does not report its background delegations; not waiting for the crew")
                    return False
            else:
                unknown = 0
                quiet = quiet + 1 if work == 0 else 0
                if quiet >= 2:
                    return True
            time.sleep(self.poll_interval)
        raise HermesError("the specialists did not finish in time")


def _result(status: str, payload: dict) -> RunResult:
    output = payload.get("output") or ""
    if not isinstance(output, str):
        output = json.dumps(output)
    err = payload.get("error") or ""
    reason = payload.get("turn_exit_reason") or ""
    return RunResult(status=status, output=output, error=str(err), reason=str(reason))


class HermesProcess:
    """Supervises `hermes gateway run` (restarts it if it dies, until stop())."""

    def __init__(self, cmd: list[str], env: dict[str, str], port: int):
        self.cmd = cmd
        self.port = port
        self.api_key = secrets.token_urlsafe(32)
        self.env = dict(env)
        self.env.update({
            "API_SERVER_ENABLED": "true",
            "API_SERVER_HOST": "127.0.0.1",
            "API_SERVER_PORT": str(port),
            "API_SERVER_KEY": self.api_key,
            "HERMES_GATEWAY_NO_SUPERVISE": "1",
            # Hermes installs nothing at runtime (also security.allow_lazy_installs: false in config.yaml, see
            # platform.render_hermes_config). Read before the config file (pm/install.py lazy_installs_allowed)
            # and inherited by Hermes's own child processes; unlike config.yaml, which Hermes reads again on every
            # check and the agent can write, the running gateway's environment cannot be changed from a turn.
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
        })
        self._proc: Optional[subprocess.Popen] = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._spawn()
        self._thread = threading.Thread(target=self._watch, name="hermes-supervisor", daemon=True)
        self._thread.start()

    def _spawn(self) -> None:
        with self._lock:
            log.info("starting hermes: %s", " ".join(self.cmd))
            self._proc = subprocess.Popen(self.cmd, env=self.env, start_new_session=True)

    def _watch(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            proc = self._proc
            if proc is not None and proc.poll() is not None and not self._stopping.is_set():
                log.error("hermes exited with %s; restarting in %.0fs", proc.returncode, backoff)
                if self._stopping.wait(backoff):
                    return
                backoff = min(backoff * 2, 30)
                self._spawn()
            elif proc is not None and proc.poll() is None:
                backoff = 1.0
            self._stopping.wait(1)

    def stop(self, timeout: float) -> None:
        """SIGTERM Hermes (it drains and saves its sessions to /data), SIGKILL after timeout."""
        self._stopping.set()
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("hermes did not exit in %.0fs; killing", timeout)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
