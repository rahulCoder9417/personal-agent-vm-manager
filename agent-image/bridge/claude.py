"""Claude Code runtime: the official, unmodified `claude` CLI on the user's own Claude plan.

Two parts:

- ClaudeRunner: one delivered message = one `claude -p --output-format stream-json` run.
  Each chat (session_key) keeps its own Claude Code session (session_key -> session id,
  stored in /data). The Platform MCP is passed with --mcp-config (this container's bearer
  token, the same as Hermes gets). Permissions: dontAsk mode with an allow list (file and
  shell work inside the sandbox, our MCP tools), so nothing ever waits for a prompt; risky
  real-world actions go through the platform's request_approval tool. The crew (crew.py) is
  passed as sub-agents with --agents; the lead starts one with the Agent tool, and a
  specialist cannot start another (code.claude.com/docs/en/sub-agents). The lead has no web
  tool: the web is Scout's, and a hook passed with --settings refuses the lead's own calls
  to Scout's web tools, the browser's among them (render_settings, lead_gate.py). When the
  browser is turned on for the user, its MCP server is in --mcp-config and its tools are
  in Scout's list (browser.py).

- ClaudeAuth: sign-in, status and sign-out through the CLI's own commands
  (`claude auth login | status | logout`). The CLI keeps its credentials in its own config
  dir (CLAUDE_CONFIG_DIR, on /data). This module never reads, copies, logs or returns them.
  The code the user pastes is only written to the waiting `claude auth login` process; it is
  never logged or stored.

Compliance (code.claude.com/docs/en/legal-and-compliance): the binary is run as published,
no auth method is removed or disabled, sign-in completes through Anthropic's own flow, and
usage is billed to the user's own plan.
"""

from __future__ import annotations

import fcntl
import hmac
import json
import logging
import os
import pty
import re
import secrets
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import browser, crew
from .platform import PLATFORM_SERVER_NAME, PlatformFile, server_key

log = logging.getLogger("bridge.claude")

RUNTIME_HERMES = "hermes"
RUNTIME_CLAUDE = "claude_code"
RUNTIMES = (RUNTIME_HERMES, RUNTIME_CLAUDE)

# Env the CLI must never see: a platform key here would bypass the user's own plan.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")

AUTO_MEMORY_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"

# The lead's built-in tools, allowed without a prompt (everything else is denied in dontAsk mode). No web
# tool: the web is Scout's (crew.py).
DEFAULT_ALLOWED_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit", "TodoWrite")
LEAD_GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lead_gate.py")
LEAD_GATE_TIMEOUT = 10  # seconds; a hook that times out does not block the call

MSG_NOT_SIGNED_IN = "Connect your Claude plan again"
MSG_LIMIT = "Your Claude plan's usage limit was reached"
MSG_ACCOUNT = "Your Claude account can't be used right now"
MSG_MAX_TURNS = "the agent hit its step limit before finishing"
MSG_FAILED = "Claude Code could not finish"

_AUTH_MARKERS = ("not logged in", "please run /login", "login expired", "oauth token revoked",
                 "oauth token has expired", "invalid api key", "authentication required", "sign in again",
                 "authentication_failed")
_ACCOUNT_MARKERS = ("account_on_hold", "oauth_org_not_allowed")
_LIMIT_MARKERS = ("usage limit", "you've hit your", "you have hit your", "spend limit", "shared budget",
                  "credit balance is too low", "usage credits", "rate limit", "rate_limit", "(429)",
                  "billing_error", "limit reached")
_NO_SESSION_MARKERS = ("no conversation found",)


class ClaudeError(Exception):
    """Claude Code crashed, timed out or is missing (a platform fault)."""


@dataclass
class RunResult:
    status: str  # completed | failed
    output: str
    error: str = ""
    reason: str = ""  # not_signed_in | plan_limit | account | max_turns | failed
    message: str = ""  # safe to show the user
    platform_fault: Optional[bool] = None


def child_env(env: dict[str, str]) -> dict[str, str]:
    out = {k: v for k, v in env.items() if k not in STRIPPED_ENV}
    out.setdefault("CLAUDE_CONFIG_DIR", os.path.join(env.get("DATA_DIR", "/data"), "claude"))
    # Updates come with new images (read-only rootfs); telemetry off. No auth method is touched.
    out.setdefault("DISABLE_AUTOUPDATER", "1")
    out.setdefault("DISABLE_TELEMETRY", "1")
    out.setdefault("DISABLE_ERROR_REPORTING", "1")
    # claude.ai connectors would go through mcp-proxy.anthropic.com (not on the egress allowlist).
    out.setdefault("ENABLE_CLAUDEAI_MCP_SERVERS", "false")
    # One memory, the platform's (remember / recall / forget; core.MEMORY_RULES). Claude Code's own auto memory
    # (notes it writes under $CLAUDE_CONFIG_DIR/projects/<project>/memory/ and loads into every session) is off:
    # "Set to 1 to disable auto memory ... When disabled, Claude does not create or load auto memory files"
    # (code.claude.com/docs/en/env-vars; /memory "Enable or disable auto memory"; read by 2.1.283). Always set,
    # never taken from the container's env ("0" would force it on). Session transcripts, and so --resume, are
    # not part of it.
    out[AUTO_MEMORY_ENV] = "1"
    return out


def installed(cmd: list[str], env: dict[str, str]) -> bool:
    exe = cmd[0]
    if os.sep in exe:
        return os.path.isfile(exe) and os.access(exe, os.X_OK)
    return shutil.which(exe, path=env.get("PATH")) is not None


def _one_line(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit]


def classify_failure(text: str, retry_error: str = "", subtype: str = "") -> RunResult:
    """Turn a failed run (result text / last api_retry category / result subtype) into a RunResult."""
    low = f"{text} {retry_error}".lower()
    if subtype == "error_max_turns":
        return RunResult("failed", "", text, "max_turns", MSG_MAX_TURNS, platform_fault=False)
    if any(m in low for m in _AUTH_MARKERS):
        return RunResult("failed", "", text, "not_signed_in", MSG_NOT_SIGNED_IN, platform_fault=False)
    if any(m in low for m in _ACCOUNT_MARKERS):
        return RunResult("failed", "", text, "account", MSG_ACCOUNT, platform_fault=False)
    if any(m in low for m in _LIMIT_MARKERS):
        detail = _one_line(text)
        msg = f"{MSG_LIMIT}: {detail}" if detail and "limit" in detail.lower() else MSG_LIMIT
        return RunResult("failed", "", text, "plan_limit", msg, platform_fault=False)
    # Overload, server errors, anything unknown: not the user's doing; the turn is not charged.
    return RunResult("failed", "", text, "failed", MSG_FAILED, platform_fault=True)


class SessionMap:
    """session_key -> Claude Code session id, persisted in /data."""

    def __init__(self, path: Optional[str], max_entries: int = 2000):
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

    def get(self, session_key: str) -> Optional[str]:
        with self._lock:
            rec = self._data.get(session_key)
            return rec.get("session_id") if rec else None

    def set(self, session_key: str, session_id: str) -> None:
        with self._lock:
            self._data[session_key] = {"session_id": session_id, "at": time.time()}
            if len(self._data) > self.max_entries:
                oldest = sorted(self._data.items(), key=lambda kv: kv[1].get("at", 0))
                for k, _ in oldest[: len(self._data) - self.max_entries]:
                    del self._data[k]
            self._save()

    def drop(self, session_key: str) -> None:
        with self._lock:
            if self._data.pop(session_key, None) is not None:
                self._save()

    def _save(self) -> None:
        if not self.path:
            return
        _write_private(self.path, json.dumps(self._data))


def _write_private(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def render_mcp_config(pf: PlatformFile, env: dict[str, str], data_dir: str = "/data",
                      with_browser: bool = False) -> dict[str, Any]:
    """--mcp-config for Claude Code: the Platform MCP (container token) + the user's own servers, and the
    browser's server (stdio, started by the CLI for this run and ended with it) when the browser is turned
    on for the user."""
    servers: dict[str, Any] = {
        PLATFORM_SERVER_NAME: {
            "type": "http",
            "url": env.get("PLATFORM_MCP_URL", "http://api:8080/mcp"),
            "headers": {"Authorization": f"Bearer {pf.mcp_token}"},
        }
    }
    for s in pf.mcp_servers:
        if not s.url:
            continue
        entry: dict[str, Any] = {"type": "http", "url": s.url}
        if s.auth_header_name and s.auth_secret:
            entry["headers"] = {s.auth_header_name: s.auth_secret}
        servers[server_key(s)] = entry
    if with_browser:
        servers[browser.SERVER_NAME] = browser.claude_server(env, data_dir)
    return {"mcpServers": servers}


def render_settings() -> dict[str, Any]:
    """--settings for Claude Code: one `PreToolUse` hook that keeps the lead off Scout's web tools (the
    built-in search, the platform's `web_search` and `read_page`, and the browser tools). The hook is the
    same whether or not the browser is turned on for the user.

    The session has to keep those tools, because a deny rule would take them from Scout too (crew.py). The
    hook runs lead_gate.py before every call to one of them, in the lead and in a sub-agent alike. It
    blocks the lead's call and lets a sub-agent's through (code.claude.com/docs/en/hooks: hooks from
    settings run inside sub-agents too, and `agent_id` in the hook's input tells the two apart).

    The matcher is a list of exact tool names separated by `|` (the docs: a matcher of only letters,
    digits, `_`, `-`, spaces, `,` and `|` is such a list; with any other character it would be a regular
    expression). The command has no `args`, so the CLI runs it with `sh -c`: lead_gate.py as a file, with
    this interpreter in isolated mode (-I), so nothing from the working directory, the Bridge package or
    PYTHON* variables is imported.
    """
    guarded = crew.claude_lead_gate(PLATFORM_SERVER_NAME)
    command = shlex.join([sys.executable or "python3", "-I", LEAD_GATE, *guarded])
    return {"hooks": {"PreToolUse": [{
        "matcher": "|".join(guarded),
        "hooks": [{"type": "command", "command": command, "timeout": LEAD_GATE_TIMEOUT}],
    }]}}


class ClaudeRunner:
    """Runs one turn with `claude -p` (stream-json). Same interface as HermesClient.run."""

    def __init__(self, cmd: list[str], env: dict[str, str], data_dir: str,
                 load_platform: Callable[[], PlatformFile], auth: Optional["ClaudeAuth"] = None,
                 model: str = "", max_turns: int = 0, allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS,
                 crew_model: str = "", crew_parallel: int = crew.DEFAULT_MAX_PARALLEL, with_browser: bool = False):
        self.cmd = cmd
        self.env = child_env(env)
        # Sub-agents: the crew only, one level deep, a cap on how many run at once.
        self.env.update(crew.claude_env(crew_parallel))
        self.crew_model = crew_model  # "" = a specialist uses the lead's model
        self.with_browser = with_browser  # Scout's browser is turned on for this user (read at boot)
        self.base_env = env
        self.data_dir = data_dir
        self.workdir = os.path.join(data_dir, "workspace")
        self.mcp_path = os.path.join(data_dir, ".platform", "claude-mcp.json")
        self.settings_path = os.path.join(data_dir, ".platform", "claude-settings.json")
        self.sessions = SessionMap(os.path.join(data_dir, ".platform", "claude-sessions.json"))
        self.load_platform = load_platform
        self.auth = auth
        self.model = model
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_mu = threading.Lock()

    def healthy(self) -> bool:
        return installed(self.cmd, self.env)

    def _session_lock(self, session_key: str) -> threading.Lock:
        with self._locks_mu:
            lock = self._session_locks.get(session_key)
            if lock is None:
                lock = self._session_locks[session_key] = threading.Lock()
            return lock

    def _args(self, pf: PlatformFile, instructions: str, session_id: str, resume: bool) -> list[str]:
        # Permission rules are the session's: the lead's and every specialist's calls are checked against
        # them. So the allow list also names what only Scout uses (its built-in web search and, when the
        # browser is turned on for the user, the browser tools by name), and the deny list only what nobody
        # has: sending the answer (the Bridge does it) and fetching pages from the container. The lead is
        # kept off Scout's web tools by the hook in --settings (render_settings).
        # Each list is written the way the docs show it for that flag: commas for --allowedTools (headless
        # page), one argument per rule for --disallowedTools (CLI reference).
        allowed = list(self.allowed_tools) + crew.claude_session_allowed(self.with_browser) + \
            [f"mcp__{PLATFORM_SERVER_NAME}"] + \
            [f"mcp__{server_key(s)}" for s in pf.mcp_servers if s.url] + [crew.CLAUDE_AGENT_TOOL]
        args = [*self.cmd, "-p", "--output-format", "stream-json", "--verbose",
                "--mcp-config", self.mcp_path, "--strict-mcp-config",
                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                "--allowedTools", ",".join(allowed),
                "--disallowedTools", *crew.claude_session_denied(PLATFORM_SERVER_NAME),
                "--setting-sources", "user", "--settings", self.settings_path,
                # The crew as sub-agents of this (lead) session; each has its own tool list.
                "--agents", json.dumps(crew.claude_agents(PLATFORM_SERVER_NAME, self.crew_model, self.with_browser)),
                "--append-system-prompt", f"{instructions}\n\n{crew.CLAUDE_HANDOFF}"]
        if self.model:
            args += ["--model", self.model]
        if self.max_turns > 0:
            args += ["--max-turns", str(self.max_turns)]
        args += ["--resume", session_id] if resume else ["--session-id", session_id]
        return args

    def run(self, text: str, instructions: str, session_key: str, idempotency_key: str,
            on_progress: Callable[[str], None], timeout: float = 900) -> RunResult:
        if not installed(self.cmd, self.env):
            raise ClaudeError("claude code is not installed")
        pf = self.load_platform()
        os.makedirs(self.workdir, mode=0o700, exist_ok=True)
        os.makedirs(self.env["CLAUDE_CONFIG_DIR"], mode=0o700, exist_ok=True)
        if self.with_browser:
            browser.write_config(self.base_env, self.data_dir)
        _write_private(self.mcp_path, json.dumps(render_mcp_config(pf, self.base_env, self.data_dir,
                                                                   self.with_browser)))
        _write_private(self.settings_path, json.dumps(render_settings()))
        deadline = time.monotonic() + timeout
        with self._session_lock(session_key):
            known = self.sessions.get(session_key)
            result, seen = self._run_once(pf, text, instructions, known or str(uuid.uuid4()), bool(known),
                                          on_progress, deadline)
            if known and result.status != "completed" and \
                    any(m in result.error.lower() for m in _NO_SESSION_MARKERS):
                # The saved session is gone (e.g. volume restored): start a new one.
                log.warning("claude session for %s not found; starting a new one", session_key)
                self.sessions.drop(session_key)
                result, seen = self._run_once(pf, text, instructions, str(uuid.uuid4()), False,
                                              on_progress, deadline)
            if seen:
                self.sessions.set(session_key, seen)
        if self.auth is not None:
            if result.status == "completed":
                self.auth.set_connected(True)
            elif result.reason == "not_signed_in":
                self.auth.set_connected(False)
        return result

    def _run_once(self, pf: PlatformFile, text: str, instructions: str, session_id: str, resume: bool,
                  on_progress: Callable[[str], None], deadline: float) -> tuple[RunResult, Optional[str]]:
        args = self._args(pf, instructions, session_id, resume)
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=self.workdir, env=self.env, start_new_session=True)
        timed_out = threading.Event()

        def kill() -> None:
            timed_out.set()
            _killpg(proc)

        remaining = max(deadline - time.monotonic(), 0.1)
        timer = threading.Timer(remaining, kill)
        timer.daemon = True
        timer.start()
        stderr_tail: list[bytes] = []

        def feed() -> None:
            try:
                proc.stdin.write(text.encode())  # the prompt goes on stdin, not argv
                proc.stdin.close()
            except OSError:
                pass

        def drain_stderr() -> None:
            for chunk in iter(lambda: proc.stderr.read(4096), b""):
                stderr_tail.append(chunk)
                while sum(len(c) for c in stderr_tail) > 8192:
                    stderr_tail.pop(0)

        threads = [threading.Thread(target=feed, daemon=True), threading.Thread(target=drain_stderr, daemon=True)]
        for t in threads:
            t.start()
        seen: Optional[str] = None
        final: Optional[dict] = None
        retry_error = ""
        crew_events = CrewEvents(on_progress)
        try:
            for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                typ, sub = ev.get("type"), ev.get("subtype")
                if typ == "system" and sub == "init":
                    seen = ev.get("session_id") or seen
                elif typ == "system" and sub == "api_retry":
                    retry_error = str(ev.get("error") or "")
                    on_progress(f"Claude is retrying ({retry_error or 'error'})")
                elif typ == "system" and sub in ("task_started", "task_notification"):
                    crew_events.task(ev)
                elif typ == "assistant":
                    for block in _blocks(ev, "tool_use"):
                        if not crew_events.started(ev, block):
                            on_progress(f"using {_tool_label(block.get('name'))}")
                elif typ == "user":
                    for block in _blocks(ev, "tool_result"):
                        crew_events.result(ev, block)
                elif typ == "result":
                    final = ev
                    seen = ev.get("session_id") or seen
            proc.wait(timeout=max(deadline - time.monotonic(), 5))
        except subprocess.TimeoutExpired:
            kill()
        finally:
            timer.cancel()
            if proc.poll() is None:
                _killpg(proc)
                proc.wait(timeout=5)
            for t in threads:
                t.join(2)
            for f in (proc.stdout, proc.stderr):
                try:
                    f.close()
                except OSError:
                    pass
            crew_events.close(stopped=timed_out.is_set() or final is None)
        if timed_out.is_set():
            raise ClaudeError("claude code run timed out")
        if final is None:
            err = b"".join(stderr_tail).decode("utf-8", "replace")
            res = classify_failure(err, retry_error)
            if res.platform_fault:
                raise ClaudeError(f"claude exited with code {proc.returncode} without a result")
            return res, seen
        if final.get("subtype") == "success" and not final.get("is_error"):
            out = final.get("result")
            return RunResult("completed", out if isinstance(out, str) else json.dumps(out)), seen
        text_err = final.get("result") if isinstance(final.get("result"), str) else ""
        errors = final.get("errors")
        if isinstance(errors, list):
            text_err = " ".join([text_err, *[str(e) for e in errors]]).strip()
        return classify_failure(text_err, retry_error, str(final.get("subtype") or "")), seen


def _tool_label(name: Any) -> str:
    name = str(name or "a tool")
    if name.startswith("mcp__"):
        return name.split("__")[-1]
    return name


def _blocks(ev: dict, typ: str) -> list[dict]:
    """The content blocks of one type in an `assistant` / `user` stream-json message."""
    content = (ev.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == typ]


class CrewEvents:
    """Crew progress lines from the stream-json events about sub-agents.

    The lead starts a specialist with an `Agent` tool call (input: subagent_type, description).
    A specialist that runs in the foreground is finished when that call's tool_result arrives. One
    that runs in the background (the default in `claude -p`) answers the call at once
    (`tool_use_result.status` = "async_launched") and is finished by a `system/task_notification`
    event; the CLI stays open until it is. Messages from inside a specialist carry
    `parent_tool_use_id`, so they are never taken for the lead's own calls.
    (code.claude.com/docs/en/headless "Follow subagent messages" and "Background tasks at exit";
    agent-sdk/typescript AgentInput, AgentOutput, SDKTaskStartedMessage, SDKTaskNotificationMessage.)
    """

    _OUTCOMES = {"completed": crew.DONE, "failed": crew.FAILED, "stopped": crew.STOPPED}

    def __init__(self, on_progress: Callable[[str], None]):
        self.progress = crew.Progress(on_progress)
        self._tasks: dict[str, str] = {}  # background task id -> the Agent tool call that started it

    def started(self, ev: dict, block: dict) -> bool:
        """True when this tool_use block is the lead starting a specialist (a start line was sent)."""
        if ev.get("parent_tool_use_id") or block.get("name") not in crew.CLAUDE_AGENT_TOOLS:
            return False
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        member = crew.BY_ID.get(str(args.get("subagent_type") or ""))
        if member is None or not block.get("id"):
            return False
        self.progress.start(block["id"], member, args.get("description"))
        return True

    def result(self, ev: dict, block: dict) -> None:
        call_id = block.get("tool_use_id")
        if ev.get("parent_tool_use_id") or not self.progress.is_open(call_id):
            return
        out = ev.get("tool_use_result")
        launched = isinstance(out, dict) and out.get("status") in ("async_launched", "remote_launched")
        if launched and not block.get("is_error"):
            # Still running in the background; task_notification says when it ends. `tool_use_id` is
            # optional on that event, so remember the ids this answer gives the run as well.
            for key in ("agentId", "taskId"):
                if out.get(key):
                    self._tasks.setdefault(str(out[key]), str(call_id))
            return
        self.progress.finish(call_id, crew.FAILED if block.get("is_error") else crew.DONE)

    def task(self, ev: dict) -> None:
        task_id, call_id = str(ev.get("task_id") or ""), ev.get("tool_use_id")
        if ev.get("subtype") == "task_started":
            if task_id and call_id:
                self._tasks[task_id] = str(call_id)
            return
        self.progress.finish(call_id or self._tasks.get(task_id),
                             self._OUTCOMES.get(str(ev.get("status") or ""), crew.FINISHED))

    def close(self, stopped: bool = False) -> None:
        """The run is over. A specialist whose end the stream did not report ended with it (the CLI
        waits for background sub-agents before it exits), or was stopped with it (timeout, crash)."""
        self.progress.finish_all(crew.STOPPED if stopped else crew.FINISHED)


def _killpg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# --- sign-in -----------------------------------------------------------------------------

_ANSI_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_OSC8 = re.compile(rb"\x1b\]8;[^;]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_URL = re.compile(rb"https://[^\s\x00-\x1f\x7f\"'<>]+")
_AUTH_HOSTS = ("claude.ai", "claude.com", "platform.claude.com", "console.anthropic.com", "anthropic.com")
_SUCCESS = re.compile(rb"login successful|logged in successfully|successfully logged in", re.I)
_CODE_PROMPT = re.compile(rb"paste (the )?code", re.I)


class LoginError(Exception):
    """The sign-in could not be started (CLI missing, no URL)."""


class LoginNotFound(Exception):
    """No pending sign-in with this id (unknown, expired or already used)."""


def find_authorize_url(buf: bytes) -> Optional[str]:
    """Anthropic's authorize URL from the CLI's terminal output (prefers the code-paste flow)."""
    return _find_urls(buf)[0]


def _find_urls(buf: bytes) -> tuple[Optional[str], bool]:
    """(best URL, whether it is the code-paste flow). A URL whose redirect goes to localhost
    only works with a browser on this machine, so it is a fallback."""
    candidates = [m.group(1) for m in _OSC8.finditer(buf)]
    plain = _ANSI_CSI.sub(b"", _ANSI_OSC.sub(b"", buf))
    candidates += [m.group(0) for m in _URL.finditer(plain)]
    fallback: Optional[str] = None
    for raw in candidates:
        url = raw.decode("utf-8", "replace").rstrip(".,;)")
        try:
            u = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        host = (u.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in _AUTH_HOSTS):
            continue
        if "oauth" not in u.path.lower() and "authorize" not in u.path.lower():
            continue
        redirect = (urllib.parse.parse_qs(u.query).get("redirect_uri") or [""])[0].lower()
        if "localhost" in redirect or "127.0.0.1" in redirect:
            fallback = fallback or url
            continue
        return url, True
    return fallback, False


@dataclass
class _Pending:
    login_id: str
    proc: subprocess.Popen
    master: int
    expires_at: float  # unix seconds
    buf: bytearray
    cond: threading.Condition
    eof: bool = False


class ClaudeAuth:
    """Sign-in / status / sign-out, all done by the official CLI itself."""

    def __init__(self, cmd: list[str], env: dict[str, str], login_ttl: float = 600, url_timeout: float = 30,
                 complete_timeout: float = 60, status_max_age: float = 300, clock: Callable[[], float] = time.time):
        self.cmd = cmd
        self.env = child_env(env)
        self.login_ttl = login_ttl
        self.url_timeout = url_timeout
        self.complete_timeout = complete_timeout
        self.status_max_age = status_max_age
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: Optional[_Pending] = None
        self._connected: Optional[bool] = None
        self._checked_at = 0.0
        self._refreshing = False

    def installed(self) -> bool:
        return installed(self.cmd, self.env)

    # status

    def set_connected(self, value: bool) -> None:
        self._connected = value
        self._checked_at = time.monotonic()

    def refresh(self) -> bool:
        """Ask the CLI (`claude auth status`, exit 0 = signed in). Never reads the credentials."""
        ok = False
        if self.installed():
            try:
                r = subprocess.run([*self.cmd, "auth", "status"], env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                                   cwd=self.env.get("HOME") or None)
                ok = r.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                log.warning("claude auth status did not answer")
                return bool(self._connected)
        self.set_connected(ok)
        return ok

    def connected(self) -> bool:
        """Last known status (False while unknown); see status()."""
        return bool(self.status())

    def status(self) -> Optional[bool]:
        """Last known status, None until the CLI has answered once. Refreshes in the background
        when stale, so /healthz never waits on the CLI."""
        stale = self._connected is None or time.monotonic() - self._checked_at > self.status_max_age
        if stale and not self._refreshing and self.installed():
            self._refreshing = True

            def bg() -> None:
                try:
                    self.refresh()
                finally:
                    self._refreshing = False

            threading.Thread(target=bg, daemon=True, name="claude-status").start()
        return self._connected

    def login_pending(self) -> bool:
        with self._lock:
            p = self._pending
            return p is not None and p.proc.poll() is None and self.clock() < p.expires_at

    # sign-in

    def start_login(self) -> dict:
        """Start `claude auth login`; return {login_id, authorize_url, expires_at (unix ms)}.
        One pending sign-in per container: a new start replaces the old one."""
        if not self.installed():
            raise LoginError("claude code is not installed")
        with self._lock:
            self._cancel_locked()
            os.makedirs(self.env["CLAUDE_CONFIG_DIR"], mode=0o700, exist_ok=True)
            master, slave = pty.openpty()
            # Wide terminal so the long URL is printed on one line.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
            env = dict(self.env)
            env.setdefault("TERM", "xterm-256color")
            env["BROWSER"] = "true"  # no browser in the container; the CLI prints the URL
            try:
                proc = subprocess.Popen([*self.cmd, "auth", "login"], stdin=slave, stdout=slave, stderr=slave,
                                        env=env, cwd=self.env.get("HOME") or None, start_new_session=True,
                                        close_fds=True)
            except OSError as exc:
                os.close(master)
                os.close(slave)
                raise LoginError(f"could not start claude: {exc}") from exc
            os.close(slave)
            p = _Pending(login_id=secrets.token_urlsafe(18), proc=proc, master=master,
                         expires_at=self.clock() + self.login_ttl, buf=bytearray(), cond=threading.Condition())
            threading.Thread(target=self._read, args=(p,), daemon=True, name="claude-login-pty").start()
            url = self._wait_url(p)
            if url is None:
                self._close(p)
                raise LoginError("the Claude sign-in did not show a link")
            self._pending = p
            timer = threading.Timer(self.login_ttl, self._expire, args=(p.login_id,))
            timer.daemon = True
            timer.start()
        log.info("claude sign-in started (login %s…)", p.login_id[:6])
        return {"login_id": p.login_id, "authorize_url": url, "expires_at": int(p.expires_at * 1000)}

    def complete_login(self, login_id: str, code: str) -> dict:
        """Type the code into the waiting CLI, wait for it, then ask the CLI whether it is signed in."""
        with self._lock:
            p = self._pending
            if p is None or not hmac.compare_digest(p.login_id.encode(), login_id.encode()) or \
                    self.clock() >= p.expires_at or p.proc.poll() is not None:
                raise LoginNotFound()
            self._pending = None  # one attempt per sign-in
        try:
            with p.cond:
                mark = len(p.buf)
            os.write(p.master, code.encode() + b"\r")
            del code
            deadline = time.monotonic() + self.complete_timeout
            with p.cond:
                while not p.eof and not _SUCCESS.search(bytes(p.buf[mark:])) and time.monotonic() < deadline:
                    p.cond.wait(0.2)
                success_seen = bool(_SUCCESS.search(bytes(p.buf[mark:])))
            if success_seen:
                try:
                    os.write(p.master, b"\r")  # "press Enter to continue"
                except OSError:
                    pass
                try:
                    p.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            self._close(p)
        connected = self.refresh()
        log.info("claude sign-in finished (login %s…): connected=%s", login_id[:6], connected)
        if connected:
            return {"connected": True, "message": "Claude plan connected"}
        return {"connected": False,
                "message": "Sign-in did not complete. The code may be wrong or expired; start again to get a new link."}

    def logout(self) -> None:
        """`claude auth logout` (the CLI removes its own credentials from /data)."""
        if not self.installed():
            raise LoginError("claude code is not installed")
        with self._lock:
            self._cancel_locked()
        try:
            subprocess.run([*self.cmd, "auth", "logout"], env=self.env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                           cwd=self.env.get("HOME") or None)
        except subprocess.TimeoutExpired:
            log.warning("claude auth logout did not finish in time")
        self.refresh()
        log.info("claude signed out: connected=%s", self._connected)

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_locked()

    # internals

    def _read(self, p: _Pending) -> None:
        while True:
            try:
                chunk = os.read(p.master, 4096)
            except OSError:
                chunk = b""
            with p.cond:
                if not chunk:
                    p.eof = True
                    p.cond.notify_all()
                    return
                p.buf += chunk
                if len(p.buf) > 256 * 1024:
                    del p.buf[: len(p.buf) - 256 * 1024]
                p.cond.notify_all()

    def _wait_url(self, p: _Pending) -> Optional[str]:
        """Wait for the code-paste URL; a localhost-redirect URL is used only if nothing better
        shows up by the time the CLI asks for the code (or the wait ends)."""
        deadline = time.monotonic() + self.url_timeout
        with p.cond:
            while True:
                buf = bytes(p.buf)
                url, code_flow = _find_urls(buf)
                if code_flow or p.eof or time.monotonic() >= deadline or (url and _CODE_PROMPT.search(buf)):
                    return url
                p.cond.wait(0.2)

    def _expire(self, login_id: str) -> None:
        with self._lock:
            p = self._pending
            if p is not None and p.login_id == login_id:
                self._pending = None
                self._close(p)
                log.info("claude sign-in expired (login %s…)", login_id[:6])

    def _cancel_locked(self) -> None:
        if self._pending is not None:
            self._close(self._pending)
            self._pending = None

    @staticmethod
    def _close(p: _Pending) -> None:
        if p.proc.poll() is None:
            _killpg(p.proc)
            try:
                p.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.close(p.master)
        except OSError:
            pass
        with p.cond:
            p.buf.clear()  # the terminal echoed the code; drop it
