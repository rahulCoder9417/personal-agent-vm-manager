"""Hermes Agent: the child process and a client for its local Runs API.

Hermes runs as `hermes gateway run` with only its API server enabled, bound to
127.0.0.1 and protected by a random per-boot key that never leaves this process
tree. One Hermes run = one delivered message (plus follow-up runs when the lead handed work
to its crew, see HermesClient.run). Hermes keeps one conversation per
`X-Hermes-Session-Key` (we pass the platform's session key, e.g. "telegram:42").
"""

from __future__ import annotations

import json
import logging
import os
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

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}

# The crew on Hermes: follow-up turns after specialists that ran in the background.
MAX_CREW_ROUNDS = 3
CREW_FOLLOW_UP = ("[Crew update: the specialists you started have finished, and their reports are now in this "
                  "conversation. Use them to write the final answer for the user. Start another specialist only "
                  "if the job still needs one.]")
_DISPATCHED = '"status": "dispatched"'  # start of delegate_task's answer when the children run in the background
_CHILD_OUTCOMES = {"completed": crew.DONE, "failed": crew.FAILED, "error": crew.FAILED, "timeout": crew.FAILED,
                   "interrupted": crew.STOPPED}


def _child_id(ev: dict) -> str:
    return str(ev.get("subagent_id") or f"{ev.get('delegation_id') or ''}#{ev.get('task_index') or 0}")


class HermesError(Exception):
    """Hermes could not run the turn (not the user's fault)."""


@dataclass
class RunResult:
    status: str  # completed | failed | cancelled | interrupted
    output: str
    error: str = ""
    reason: str = ""  # Hermes turn_exit_reason, e.g. "max_iterations_reached(60/60)"


class HermesClient:
    """Minimal client for Hermes's local API server (no proxy: loopback only)."""

    def __init__(self, base_url: str, api_key: str, approval_choice: str = "deny", poll_interval: float = 2.0,
                 with_browser: bool = False):
        self.with_browser = with_browser  # Scout's browser is turned on for this user: the briefs say so
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
            on_progress, timeout: float = 900) -> RunResult:
        """Run one turn to the end. on_progress(message) is called for tool activity.

        The crew: on this API Hermes runs the lead's specialists in the background. The lead's run
        ends with a short status, the specialists' reports are stored in the session when they
        finish, and the client has to start the next turn. So after a run that handed work over,
        wait for the specialists and run again in the same session; the last run's answer is the
        turn's answer.
        """
        instructions = f"{instructions}\n\n{crew.hermes_handoff(self.with_browser)}"
        deadline = time.monotonic() + timeout
        progress = crew.Progress(on_progress)
        result, handed_over = self._run_once(text, instructions, session_key, idempotency_key, on_progress,
                                             progress, deadline)
        rounds = 0
        while result.status == "completed" and handed_over and rounds < MAX_CREW_ROUNDS:
            if not self._wait_for_crew(deadline):
                break  # Hermes does not say whether they are done: keep the lead's status as the answer
            progress.finish_all()
            rounds += 1
            result, handed_over = self._run_once(CREW_FOLLOW_UP, instructions, session_key,
                                                 f"{idempotency_key}/crew{rounds}", on_progress, progress, deadline)
        return result

    def _run_once(self, text: str, instructions: str, session_key: str, idempotency_key: str, on_progress,
                  progress: crew.Progress, deadline: float) -> tuple[RunResult, bool]:
        """One Hermes run. Returns (result, whether specialists are still working in the background)."""
        run_id = self.start_run(text, instructions, session_key, idempotency_key)
        children: set[str] = set()  # started and not yet finished
        detached = False
        try:
            for ev in self.events(run_id, timeout=max(deadline - time.monotonic(), 1)):
                name = ev.get("event", "")
                if name == "tool.started":
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
