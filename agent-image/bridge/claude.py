"""Claude Code runtime: the official, unmodified `claude` CLI on the user's own Claude plan.

Two parts:

- ClaudeRunner: one delivered message = one `claude -p --output-format stream-json` run.
  Each chat (session_key) keeps its own Claude Code session (session_key -> session id,
  stored in /data). The Platform MCP is passed with --mcp-config (this container's bearer
  token, the same as Hermes gets). Permissions: dontAsk mode with an allow list (file and
  shell work inside the sandbox, our MCP tools), so nothing ever waits for a prompt; risky
  real-world actions go through the platform's request_approval tool.

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
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .platform import PLATFORM_SERVER_NAME, PlatformFile, server_key

log = logging.getLogger("bridge.claude")

RUNTIME_HERMES = "hermes"
RUNTIME_CLAUDE = "claude_code"
RUNTIMES = (RUNTIME_HERMES, RUNTIME_CLAUDE)

# Env the CLI must never see: a platform key here would bypass the user's own plan.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")

# Built-in tools allowed without a prompt (everything else is denied in dontAsk mode).
DEFAULT_ALLOWED_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit", "WebFetch", "WebSearch",
                         "TodoWrite")

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


def render_mcp_config(pf: PlatformFile, env: dict[str, str]) -> dict[str, Any]:
    """--mcp-config for Claude Code: the Platform MCP (container token) + the user's own servers."""
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
    return {"mcpServers": servers}


class ClaudeRunner:
    """Runs one turn with `claude -p` (stream-json). Same interface as HermesClient.run."""

    def __init__(self, cmd: list[str], env: dict[str, str], data_dir: str,
                 load_platform: Callable[[], PlatformFile], auth: Optional["ClaudeAuth"] = None,
                 model: str = "", max_turns: int = 0, allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS):
        self.cmd = cmd
        self.env = child_env(env)
        self.base_env = env
        self.data_dir = data_dir
        self.workdir = os.path.join(data_dir, "workspace")
        self.mcp_path = os.path.join(data_dir, ".platform", "claude-mcp.json")
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
        allowed = list(self.allowed_tools) + [f"mcp__{PLATFORM_SERVER_NAME}"] + \
            [f"mcp__{server_key(s)}" for s in pf.mcp_servers if s.url]
        args = [*self.cmd, "-p", "--output-format", "stream-json", "--verbose",
                "--mcp-config", self.mcp_path, "--strict-mcp-config",
                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                "--allowedTools", ",".join(allowed),
                # The Bridge sends the final answer itself.
                "--disallowedTools", f"mcp__{PLATFORM_SERVER_NAME}__send_message",
                "--setting-sources", "user",
                "--append-system-prompt", instructions]
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
        _write_private(self.mcp_path, json.dumps(render_mcp_config(pf, self.base_env)))
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
                elif typ == "assistant":
                    for block in (ev.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            on_progress(f"using {_tool_label(block.get('name'))}")
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
