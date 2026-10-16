"""Claude Code runtime: the official, unmodified `claude` CLI on the user's own Claude plan.

Two parts:

- ClaudeRunner: one delivered message = one `claude -p --output-format stream-json` run.
  Each conversation of a chat (the session_key, and the `chat_epoch` in the delivery's metadata)
  keeps its own Claude Code session (SessionMap, stored in /data); a turn runs in a copy of it
  (--fork-session) that the conversation keeps only when the turn completed, so a failed message
  is never acted on later (ClaudeRunner.run). The user's "New chat" is a `chat_epoch` with no
  session yet: the turn starts one. An earlier `chat_epoch` is a conversation the user opened
  again: the turn goes on in that conversation's own session. The model and effort the user
  chose go to the CLI as --model and --effort. A failed
  run is logged without secrets or the user's message (_log_failure); a refusal by Anthropic or
  the user's plan or account, or of the model or effort the user chose, is never a platform fault
  (classify_failure).
  The Platform MCP is passed with --mcp-config (this container's bearer
  token, the same as Hermes gets). Permissions: dontAsk mode, so nothing ever waits for a prompt.
  Reading, searching and our MCP tools are on the allow list; commands and file changes are not:
  each one goes through Textcrew's gate hook (cc_gate.py), which lets it run, refuses it, or has
  the user asked first (an approval card; the turn ends; after the yes the same call runs once).
  A gate that crashes or times out leaves such a call refused. The credential and platform files
  are in the deny rules too (never_rules). Risky real-world actions in apps go through the
  platform's request_approval tool. The crew (crew.py) is
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
import glob
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

from . import browser, crew, labels
from .platform import PLATFORM_SERVER_NAME, PlatformFile, server_key

log = logging.getLogger("bridge.claude")

RUNTIME_HERMES = "hermes"
RUNTIME_CLAUDE = "claude_code"
RUNTIMES = (RUNTIME_HERMES, RUNTIME_CLAUDE)

# Env the CLI must never see: a platform key here would bypass the user's own plan.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")

AUTO_MEMORY_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"

# The lead's built-in tools, allowed without a prompt (everything else is denied in dontAsk mode). No web
# tool: the web is Scout's (crew.py). Bash, Edit, Write and NotebookEdit are not here: each call of theirs runs
# only when the gate hook says so (GATED_TOOLS, cc_gate.py), so a gate that fails refuses it.
DEFAULT_ALLOWED_TOOLS = ("Read", "Glob", "Grep", "TodoWrite")
LEAD_GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lead_gate.py")
LEAD_GATE_TIMEOUT = 10  # seconds; a hook that times out does not block the call
CC_GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc_gate.py")
# The tools the gate hook decides on: the writes it lets through (they are on no allow list), and the reads it
# checks for the platform's own files (they are allowed, and the deny rules hold those files too).
GATED_TOOLS = ("Bash", "Edit", "Write", "NotebookEdit", "Read", "Glob", "Grep")
CC_GATE_TIMEOUT = 30  # seconds: the gate may ask the backend (15 s at most); a timeout refuses the call

MSG_NOT_SIGNED_IN = "Connect your Claude plan again"
MSG_LIMIT = "Your Claude plan's usage limit was reached"
MSG_ACCOUNT = "Your Claude account can't be used right now"
MSG_MAX_TURNS = "the agent hit its step limit before finishing"
MSG_REFUSED = "Claude declined this request under Anthropic's usage policy"
MSG_CHOICE = "Claude Code could not use the model or thinking effort chosen in Settings"
MSG_FAILED = "Claude Code could not finish"

# The `error` of the CLI's own assistant message for a failed API call (Agent SDK, SDKAssistantMessageError:
# code.claude.com/docs/en/agent-sdk/typescript). The ones that are the user's plan or account, not ours. A
# refusal comes as `invalid_request` with stop_reason "refusal" (seen with 2.1.283) and is told apart by that.
_ERROR_REASONS = {"authentication_failed": "not_signed_in", "oauth_org_not_allowed": "account",
                  "account_on_hold": "account", "billing_error": "plan_limit", "rate_limit": "plan_limit"}
# A Claude Code session id (a UUID): the only thing ever put into a transcript path.
_SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# A conversation's number (`chat_epoch`) as a key of claude-sessions.json.
_EPOCH_KEY = re.compile(r"[0-9]{1,18}")

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
    reason: str = ""  # not_signed_in | plan_limit | account | refused | model_choice | max_turns | failed
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
    # The effort is the user's choice, passed per turn with --effort. This variable would win over that flag and
    # over a specialist's own level (code.claude.com/docs/en/model-config "Adjust effort level"; seen with 2.1.283:
    # CLAUDE_CODE_EFFORT_LEVEL=low and --effort xhigh sent effort "low"), so the CLI never gets it.
    out.pop(EFFORT_ENV, None)
    return out


def installed(cmd: list[str], env: dict[str, str]) -> bool:
    exe = cmd[0]
    if os.sep in exe:
        return os.path.isfile(exe) and os.access(exe, os.X_OK)
    return shutil.which(exe, path=env.get("PATH")) is not None


def _one_line(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit]


# --- what is logged about a failed run -----------------------------------------------------------------

_TOKEN_PATTERNS = re.compile(
    # name: value (the name is kept)
    r"(?i)(?P<key>\b(?:x-bridge-secret|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret"
    r"|password)[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s\"',;]+"
    r"|\bbearer\s+\S+"  # an Authorization header value on its own
    r"|\bsk-[a-z0-9][a-z0-9_-]{8,}")  # Anthropic / LiteLLM / OpenAI style keys
_QUOTE_WINDOW = 24  # a logged line that shares this many characters in a row with the user's message is left out
STDERR_LINES = 10


class Redactor:
    """Leaves the container's secrets and the user's message out of what the Bridge logs about a failed run.

    Secrets: the values the Bridge knows (the Platform MCP token, the Bridge secret, the LiteLLM key, the user's
    MCP server secrets) and anything shaped like a token. The user's message: a line that quotes it (any
    _QUOTE_WINDOW characters in a row, or the whole message when it is shorter) is replaced as a whole."""

    def __init__(self, secrets: list[str], message: str):
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)
        self.message = " ".join(str(message or "").split())
        self._windows: Optional[set[str]] = None  # built on first use: only a failed run logs anything

    def _quotes_message(self, norm: str) -> bool:
        msg = self.message
        if len(msg) < _QUOTE_WINDOW:
            return len(msg) >= 4 and msg in norm
        if self._windows is None:
            self._windows = {msg[i:i + _QUOTE_WINDOW] for i in range(len(msg) - _QUOTE_WINDOW + 1)}
        return any(norm[i:i + _QUOTE_WINDOW] in self._windows for i in range(max(len(norm) - _QUOTE_WINDOW + 1, 0)))

    def line(self, text: Any, limit: int = 300) -> str:
        norm = " ".join(str(text or "").split())
        if not norm:
            return ""
        if self._quotes_message(norm):
            return "[left out: quotes the user's message]"
        for s in self.secrets:
            norm = norm.replace(s, "[secret]")
        return _TOKEN_PATTERNS.sub(lambda m: (m.group("key") or "") + "[secret]", norm)[:limit]


def _api_error(ev: dict) -> Optional[dict]:
    """The CLI's own assistant message for a failed API call (it carries `error`), as a few fields."""
    if not ev.get("error"):
        return None
    message = ev.get("message") if isinstance(ev.get("message"), dict) else {}
    details = message.get("stop_details") if isinstance(message.get("stop_details"), dict) else {}
    texts = [b.get("text") for b in _blocks(ev, "text")]
    out = {"error": str(ev.get("error")), "stop_reason": message.get("stop_reason"), "text": texts[0] if texts else ""}
    if details.get("type") == "refusal":
        out["refusal_category"] = details.get("category")
    return out


def _refuses_choice(text: str, error: str, status: Any, model: str, effort: str) -> bool:
    """Whether a failed API call refused the model or the effort the user chose for this turn. 2.1.283 against a
    fake API: a 404 for the model ends with the CLI's error code `model_not_found` and "There's an issue with the
    selected model (<id>). It may not exist or you may not have access to it. ..."; a 400 ends with `unknown` and
    "API Error: 400 <the API's message>", which names the model or the effort it is about."""
    low = text.lower()
    if model and (error == "model_not_found" or (status in (400, 404) and model.lower() in low)):
        return True
    return bool(effort) and status == 400 and "effort" in low


def classify_failure(text: str, retry_error: str = "", subtype: str = "", error: str = "",
                     refused: bool = False, status: Any = None, model: str = "", effort: str = "") -> RunResult:
    """Turn a failed run into a RunResult, from: the result text, the last api_retry category, the result
    subtype, the `error` of the CLI's last assistant message for a failed API call, whether Anthropic
    refused the request (a stop_reason of "refusal"), the API's status (`api_error_status` of the result) and the
    model and effort the user chose for this turn ("" when the turn runs on the defaults). Only a fault of the
    platform's own is platform_fault: that flag gives the user's credits back and tells them it was our fault."""
    low = f"{text} {retry_error}".lower()
    if subtype == "error_max_turns":
        return RunResult("failed", "", text, "max_turns", MSG_MAX_TURNS, platform_fault=False)
    if refused:
        # Anthropic's safeguards declined the request (the CLI already retried once on a fallback model).
        return RunResult("failed", "", text, "refused", MSG_REFUSED, platform_fault=False)
    if _refuses_choice(text, error, status, model, effort):
        # The user's plan or Anthropic does not take the model or effort the user chose: their choice, not ours.
        # The CLI's own message says which.
        detail = _one_line(text, 300)
        return RunResult("failed", "", text, "model_choice", f"{MSG_CHOICE}: {detail}" if detail else MSG_CHOICE,
                         platform_fault=False)
    reason = _ERROR_REASONS.get(error)
    if reason == "not_signed_in":
        return RunResult("failed", "", text, "not_signed_in", MSG_NOT_SIGNED_IN, platform_fault=False)
    if reason == "account":
        return RunResult("failed", "", text, "account", MSG_ACCOUNT, platform_fault=False)
    if reason == "plan_limit" and not any(m in low for m in _LIMIT_MARKERS):
        return RunResult("failed", "", text, "plan_limit", MSG_LIMIT, platform_fault=False)
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
    """The Claude Code session of each conversation of each chat, persisted in /data.

    A chat is a session_key. Its conversations are numbered by the backend (`chat_epoch`: 0, then one more at
    each "New chat"), and each conversation has its own session. One record per chat:

        {"epoch": n, "sessions": {"<epoch>": "<session id>", ...}, "at": time}

    `epoch` is the conversation of the chat's last turn on this engine; a delivery that names no conversation
    goes on in it. `sessions` has an entry for each conversation that has a session; one with no entry starts a
    new session with its next turn (a new chat, a conversation this engine never ran, a first turn that failed,
    a session that was lost).

    An older Bridge kept one session per chat: {"session_id": id or None, "epoch": n, "at": time}, and before
    that no epoch (0). That session is the one of that epoch, so the record is read as
    {"epoch": n, "sessions": {"<n>": id}} and the chat's current conversation goes on. The earlier conversations
    of such a chat have no entry: the older Bridge did not keep their session ids."""

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
                    self._data = {k: _chat_record(v) for k, v in loaded.items() if isinstance(v, dict)}
            except (OSError, json.JSONDecodeError):
                log.warning("could not read %s; starting empty", path)

    def epoch(self, session_key: str) -> int:
        """The conversation of the chat's last turn on this engine (0 for a chat it never ran)."""
        with self._lock:
            return (self._data.get(session_key) or {}).get("epoch", 0)

    def enter(self, session_key: str, epoch: Optional[int]) -> tuple[int, str]:
        """The conversation a turn runs in, and how the chat got there. `epoch` is the delivery's `chat_epoch`;
        None (the delivery names none) is the conversation of the chat's last turn. Returns (epoch, "same" |
        "new" | "reopened"): the conversation of the last turn, one with no session yet, or another one that has
        a session. Written to /data before the turn runs, so a new chat whose first turn fails stays the chat's
        conversation."""
        with self._lock:
            rec = self._data.get(session_key)
            current = rec["epoch"] if rec else 0
            if epoch is None or epoch == current:
                return current, "same"
            rec = rec or {"epoch": 0, "sessions": {}, "at": 0}
            self._put(session_key, {**rec, "epoch": epoch, "at": time.time()})
            return epoch, "reopened" if str(epoch) in rec["sessions"] else "new"

    def get(self, session_key: str, epoch: int) -> Optional[str]:
        with self._lock:
            rec = self._data.get(session_key)
            return rec["sessions"].get(str(epoch)) if rec else None

    def set(self, session_key: str, epoch: int, session_id: str) -> None:
        """The conversation's session is now `session_id` (a turn in it completed)."""
        with self._lock:
            rec = self._data.get(session_key) or {"epoch": epoch, "sessions": {}}
            self._put(session_key, {"epoch": rec["epoch"], "sessions": {**rec["sessions"], str(epoch): session_id},
                                    "at": time.time()})

    def drop(self, session_key: str, epoch: int) -> None:
        """The conversation has no session any more (its next turn starts one). The chat's other conversations
        keep theirs."""
        with self._lock:
            rec = self._data.get(session_key)
            if rec is not None and rec["sessions"].pop(str(epoch), None) is not None:
                self._save()

    def _put(self, session_key: str, rec: dict) -> None:
        self._data[session_key] = rec
        if len(self._data) > self.max_entries:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1].get("at", 0))
            for k, _ in oldest[: len(self._data) - self.max_entries]:
                del self._data[k]
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        _write_private(self.path, json.dumps(self._data))


def _epoch(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _chat_record(raw: dict) -> dict:
    """One chat's record of claude-sessions.json as SessionMap keeps it, from what is in the file: this layout,
    or an older Bridge's {"session_id", "epoch", "at"} (one session, the one of that epoch)."""
    epoch = _epoch(raw.get("epoch"))
    sessions: dict[str, str] = {}
    if isinstance(raw.get("sessions"), dict):
        for number, session_id in raw["sessions"].items():
            if isinstance(number, str) and _EPOCH_KEY.fullmatch(number) and isinstance(session_id, str) and session_id:
                sessions[str(int(number))] = session_id
    elif isinstance(raw.get("session_id"), str) and raw["session_id"]:
        sessions[str(epoch)] = raw["session_id"]
    at = raw.get("at")
    return {"epoch": epoch, "sessions": sessions,
            "at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else 0}


def _write_if_changed(path: str, content: str) -> bool:
    """Write a private file only when its content changes (False: it already held exactly this). The CLI's
    tool-server and settings files are the same turn after turn, so most turns write nothing."""
    try:
        st = os.stat(path)
        if st.st_mode & 0o777 == 0o600:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == content:
                    return False
    except (OSError, UnicodeDecodeError):
        pass
    _write_private(path, content)
    return True


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


@dataclass(frozen=True)
class GateConfig:
    """What the gate hook is told on its command line (nothing secret: it reads the token from the platform file)."""

    data_dir: str = "/data"
    workspace: str = "/data/workspace"
    gate_url: str = "http://api:8080/mcp/gate"
    platform_file: str = "/data/.platform/platform.json"
    tmp: tuple[str, ...] = ("/data/tmp", "/tmp")
    home: str = "/data/home"
    browser: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str], data_dir: str, browser: bool) -> "GateConfig":
        mcp = env.get("PLATFORM_MCP_URL", "http://api:8080/mcp").rstrip("/")
        tmp = tuple(dict.fromkeys(t for t in (env.get("TMPDIR") or os.path.join(data_dir, "tmp"), "/tmp") if t))
        return cls(data_dir=data_dir, workspace=os.path.join(data_dir, "workspace"), gate_url=f"{mcp}/gate",
                   platform_file=env.get("PLATFORM_FILE") or os.path.join(data_dir, ".platform", "platform.json"),
                   tmp=tmp, home=env.get("HOME") or os.path.join(data_dir, "home"), browser=browser)

    def argv(self) -> list[str]:
        out = ["--data", self.data_dir, "--workspace", self.workspace, "--gate-url", self.gate_url,
               "--platform-file", self.platform_file, "--home", self.home]
        for t in self.tmp:
            out += ["--tmp", t]
        return out + (["--browser"] if self.browser else [])


def never_rules(data_dir: str = "/data") -> list[str]:
    """Deny rules for the session's --disallowedTools: the platform's own and the credential files, for reading
    and for changing. They hold whatever the gate hook does (Claude Code also applies the Read rules to read-only
    shell commands such as cat). `//` starts an absolute path in a rule (code.claude.com/docs/en/permissions).
    Claude Code's saved tool output, under its config folder's projects/, stays readable."""
    d = "/" + data_dir.rstrip("/")
    claude = f"{d}/claude"
    rules = []
    for folder in (f"{d}/.platform", f"{d}/hermes"):
        rules += [f"Read({folder}/**)", f"Edit({folder}/**)"]
    rules += [f"Read({claude}/.credentials.json)", f"Read({claude}/.claude.json)", f"Read({claude}/*.json)",
              f"Read({claude}/backups/**)", f"Edit({claude}/**)", "Read(//proc/*/environ)"]
    return rules


def render_settings(gate: Optional[GateConfig] = None) -> dict[str, Any]:
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
    gate = gate or GateConfig()
    return {"hooks": {"PreToolUse": [{
        "matcher": "|".join(guarded),
        "hooks": [{"type": "command", "command": command, "timeout": LEAD_GATE_TIMEOUT}],
    }, {
        # Textcrew's gate (cc_gate.py): every command and file change, in the lead and in a sub-agent alike.
        "matcher": "|".join(GATED_TOOLS),
        "hooks": [{"type": "command", "command": shlex.join([sys.executable or "python3", "-I", CC_GATE,
                                                             *gate.argv()]),
                   "timeout": CC_GATE_TIMEOUT}],
    }]}}


# The lead's model and effort when the user left both on "Default" in Settings and no BRIDGE_CLAUDE_MODEL is set:
# Sonnet 5.5 at low effort, a quick first word for chat. Without them the CLI picks for itself: Opus 5.5 at medium
# effort (2.1.301 against a fake API), which once thought for 14 s before its first word. An effort the user chose
# still applies, on this model. Byte and Patch then keep their own (crew.CLAUDE_CREW_DEFAULTS).
CHAT_DEFAULT_MODEL = "claude-sonnet-5-5"
CHAT_DEFAULT_EFFORT = "low"


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
        # The gate's rules depend on the browser: with it on, the container can reach any website.
        self.gate = GateConfig.from_env(env, data_dir, with_browser)
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

    def _args(self, pf: PlatformFile, instructions: str, session_id: str, resume: bool, model: str = "",
              effort: str = "", stream_text: bool = False, crew_defaults: bool = False) -> list[str]:
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
                "--disallowedTools", *crew.claude_session_denied(PLATFORM_SERVER_NAME), *never_rules(self.data_dir),
                "--setting-sources", "user", "--settings", self.settings_path,
                # The crew as sub-agents of this (lead) session; each has its own tool list.
                "--agents", json.dumps(crew.claude_agents(PLATFORM_SERVER_NAME, self.crew_model, self.with_browser,
                                                          own_defaults=crew_defaults)),
                "--append-system-prompt", f"{instructions}\n\n{crew.CLAUDE_HANDOFF}"]
        # The model and effort: the user's choice for this turn, else the Bridge's default (BRIDGE_CLAUDE_MODEL, or
        # CHAT_DEFAULT_MODEL at CHAT_DEFAULT_EFFORT). 2.1.283 and 2.1.296 (`claude --help`): "--model <model> ... an alias for the latest model
        # (e.g. 'fable', 'opus', or 'sonnet') or a model's full name"; "--effort <level> Effort level for the current
        # session (low, medium, high, xhigh, max)". Both apply to the lead; a specialist whose definition says
        # `inherit` runs on the lead's model at the session's effort (crew.claude_agents).
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        if stream_text:
            # The answer as the model writes it: `stream_event` lines with the API's own stream events (CLI
            # reference: "--include-partial-messages  Include partial message chunks as they arrive (only works
            # with --print and --output-format=stream-json)"; the Agent SDK's SDKPartialAssistantMessage). Only
            # for a turn whose draft is shown (core.TurnOptions.stream_text); see LeadText.
            args.append("--include-partial-messages")
        if self.max_turns > 0:
            args += ["--max-turns", str(self.max_turns)]
        # A turn on an existing chat continues a copy of its session ("--fork-session: When resuming, create a
        # new session ID instead of reusing the original", CLI reference; "Fork ... creates a new session that
        # starts with a copy of the original's history. The original stays unchanged", agent-sdk/sessions).
        # run() keeps the copy only when the turn completes. 2.1.283 has the flag (`claude --help`).
        args += ["--resume", session_id, "--fork-session"] if resume else ["--session-id", session_id]
        return args

    def run(self, text: str, instructions: str, session_key: str, idempotency_key: str,
            on_progress: Callable[[str], None], timeout: float = 900, options: Any = None,
            on_text: Optional[Callable[[Optional[str]], None]] = None) -> RunResult:
        """One turn. A message whose turn failed is never acted on later: the CLI writes every message into
        the session before it calls the model, a failed one included, and on --resume the model sees it again
        (on 2026-10-08 three messages refused for the plan's limit were all carried out 90 minutes later, with
        the next message). So the turn runs in a copy of the chat's session (--fork-session), and the chat
        moves on to that copy only when the turn completed. After a failure the chat keeps the session from
        before it; the copy with the failed message is never resumed (the CLI deletes it after its retention
        period, 30 days). A first turn that fails leaves the chat with no session, so the next one starts a
        new session. The copy a completed turn replaced is removed (see _drop_transcript).

        All of that is per conversation. `options` (core.TurnOptions, from the delivery's metadata): `chat_epoch`
        names the conversation of the chat this turn belongs to, and each has its own session (SessionMap). One
        with no session yet (the user started a new chat) starts a new session, not resumed. One that has a
        session is continued in a copy of it, whether it is the chat's latest or an earlier one the user opened
        again. A failed turn leaves that conversation where it was and touches no other. The Bridge removes no
        session of another conversation, so an earlier one can be opened again later.
        `claude_model` / `claude_effort` are passed as --model / --effort; the model wins over the Bridge's
        default (BRIDGE_CLAUDE_MODEL, else CHAT_DEFAULT_MODEL, and then CHAT_DEFAULT_EFFORT unless one was chosen).

        `on_text` (core.Draft; None = no draft for this turn): the lead's words as the model writes them, from the
        CLI's partial messages (LeadText). A run that starts again (a new session, below) starts the draft over."""
        if not installed(self.cmd, self.env):
            raise ClaudeError("claude code is not installed")
        chat_epoch = getattr(options, "chat_epoch", None)
        model = getattr(options, "claude_model", "") or ""
        effort = getattr(options, "claude_effort", "") or ""
        choice = {"model": model, "effort": effort}  # the user's own choice, for classify_failure
        chat_default = not model and not self.model  # CHAT_DEFAULT_MODEL and, unless chosen, CHAT_DEFAULT_EFFORT
        model = model or self.model or CHAT_DEFAULT_MODEL
        effort = effort or (CHAT_DEFAULT_EFFORT if chat_default else "")
        pf = self.load_platform()
        os.makedirs(self.workdir, mode=0o700, exist_ok=True)
        os.makedirs(self.env["CLAUDE_CONFIG_DIR"], mode=0o700, exist_ok=True)
        if self.with_browser:
            browser.write_config(self.base_env, self.data_dir)
        # Read by the CLI at its start: written when what they hold changed (a new token, the user's MCP servers),
        # not at every turn.
        _write_if_changed(self.mcp_path, json.dumps(render_mcp_config(pf, self.base_env, self.data_dir,
                                                                      self.with_browser)))
        _write_if_changed(self.settings_path, json.dumps(render_settings(self.gate)))
        deadline = time.monotonic() + timeout
        redact = Redactor([pf.mcp_token, pf.bridge_secret, pf.litellm_key, *(s.auth_secret for s in pf.mcp_servers)],
                          text)
        # A crew job's turn: its mark in the CLI's environment, inherited by everything the CLI starts (jobs.py).
        job_env = dict(getattr(options, "job_env", ()) or ())
        how = {"model": model, "effort": effort, "choice": choice, "on_text": on_text, "job_env": job_env,
               "clock": getattr(options, "clock", None), "crew_defaults": chat_default}
        with self._session_lock(session_key):
            # Which conversation of the chat this turn is in. The platform's memory is not part of a conversation.
            epoch, moved = self.sessions.enter(session_key, chat_epoch)
            known = self.sessions.get(session_key, epoch)
            if moved == "new":
                # The user started a new chat (or this engine never ran this conversation): the turn starts a new
                # session. The sessions of the chat's other conversations stay as they are.
                log.info("claude turn %s: a new chat (chat %d); it starts a new session", idempotency_key, epoch)
            elif moved == "reopened":
                log.info("claude turn %s: back in another chat (chat %d); it goes on in that chat's own session",
                         idempotency_key, epoch)
            result, seen = self._run_once(pf, text, instructions, known or str(uuid.uuid4()), bool(known),
                                          on_progress, deadline, idempotency_key, redact, **how)
            if known and result.status != "completed" and \
                    any(m in result.error.lower() for m in _NO_SESSION_MARKERS):
                # The conversation's saved session is gone (e.g. volume restored, or removed by the CLI's own
                # clean-up of old transcripts, 30 days by default): start a new one for it.
                log.warning("claude session for %s (chat %d) not found; starting a new one", session_key, epoch)
                self.sessions.drop(session_key, epoch)
                known = None
                result, seen = self._run_once(pf, text, instructions, str(uuid.uuid4()), False,
                                              on_progress, deadline, idempotency_key, redact, **how)
            if result.status == "completed" and seen:
                self.sessions.set(session_key, epoch, seen)
                if known and seen != known:
                    self._drop_transcript(known, seen)
            elif known:
                log.info("claude turn %s failed; the chat goes on from its session before that turn", idempotency_key)
        if self.auth is not None:
            if result.status == "completed":
                self.auth.set_connected(True)
            elif result.reason == "not_signed_in":
                self.auth.set_connected(False)
        return result

    def _transcript(self, session_id: str) -> Optional[str]:
        """The transcript file of a session: $CLAUDE_CONFIG_DIR/projects/<project>/<session id>.jsonl (the sessions
        page, "Where transcripts are stored"). None unless exactly one is found. Its content is never read."""
        if not _SESSION_ID.fullmatch(session_id or ""):
            return None
        projects = glob.escape(os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects"))
        found = glob.glob(os.path.join(projects, "*", f"{session_id}.jsonl"))
        return found[0] if len(found) == 1 else None

    def _drop_transcript(self, old: str, new: str) -> None:
        """Remove the transcript a completed turn replaced: its copy (the fork) holds all of it, so without this
        every turn would leave a full copy of the chat behind until the CLI's 30-day retention. Only the
        `<id>.jsonl` file, and only when the copy is next to it; the session's folder (sub-agent transcripts,
        saved tool output the copy may point to) stays. Checked with 2.1.283: a fork resumes with the
        original's file gone."""
        old_path, new_path = self._transcript(old), self._transcript(new)
        if not old_path or not new_path or os.path.dirname(old_path) != os.path.dirname(new_path):
            return
        try:
            os.remove(old_path)
        except OSError as exc:
            log.warning("could not remove a replaced claude transcript: %s", type(exc).__name__)

    def _log_failure(self, redact: "Redactor", delivery: str, reason: str, fault: Optional[bool],
                     returncode: Optional[int], final: Optional[dict], api_error: Optional[dict], refusal: str,
                     retry: dict, stderr: bytes, model: str = "", effort: str = "") -> None:
        """One log line per failed run, for `docker logs`: the exit code, how the run ended, the CLI's last error
        message, the last API retry, a refusal's category, the last lines of stderr, and the model and effort the
        run was given ("" = the default). The container's secrets and the user's message are left out (Redactor);
        so is any answer text."""
        info: dict[str, Any] = {"delivery": delivery, "reason": reason, "platform_fault": fault,
                                "exit_code": returncode, "model": model, "effort": effort}
        if final is not None:
            info["result"] = {k: final.get(k) for k in ("subtype", "is_error", "terminal_reason", "api_error_status",
                                                        "stop_reason", "num_turns") if k in final}
            errors = final.get("errors")
            if isinstance(errors, list) and errors:
                info["result"]["errors"] = [redact.line(e, 200) for e in errors[:3]]
            if api_error is None and final.get("is_error") and isinstance(final.get("result"), str):
                info["result"]["text"] = redact.line(final["result"], 300)
        else:
            info["result"] = None
        if api_error is not None:
            info["last_error"] = {**api_error, "text": redact.line(api_error.get("text"), 300)}
        if refusal:
            info["refusal_category"] = refusal
        if retry:
            info["last_retry"] = retry
        lines = [ln for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()][-STDERR_LINES:]
        info["stderr"] = [redact.line(ln) for ln in lines]
        log.warning("claude run failed: %s", json.dumps(info, ensure_ascii=False))

    def _run_once(self, pf: PlatformFile, text: str, instructions: str, session_id: str, resume: bool,
                  on_progress: Callable[[str], None], deadline: float, delivery: str = "",
                  redact: Optional["Redactor"] = None, model: str = "", effort: str = "",
                  choice: Optional[dict] = None,
                  on_text: Optional[Callable[[Optional[str]], None]] = None,
                  job_env: Optional[dict] = None, clock: Any = None,
                  crew_defaults: bool = False) -> tuple[RunResult, Optional[str]]:
        """One CLI run. `model` / `effort` go on the command line; `choice` is what of them the user chose
        ({"model", "effort"}), so a refusal of it is not a platform fault. `on_text`: the draft (LeadText).
        `job_env`: a crew job's mark, added to the CLI's environment. `clock`: the turn's stage times
        (timings.TurnClock; ClaudeMarks), or None."""
        redact = redact or Redactor([], text)
        choice = choice or {}
        marks = ClaudeMarks(clock)
        args = self._args(pf, instructions, session_id, resume, model, effort, stream_text=on_text is not None,
                          crew_defaults=crew_defaults)
        marks.run_started()
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=self.workdir, env={**self.env, **job_env} if job_env else self.env,
                                start_new_session=True)
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
        retry: dict = {}
        api_error: Optional[dict] = None  # the CLI's last own message for a failed API call (the lead's)
        refusal = ""  # the category of a refusal the CLI gave up on (system/model_refusal_no_fallback)
        crew_events = CrewEvents(on_progress, clock)
        lead_text = LeadText(on_text, crew_busy=lambda: crew_events.progress.open_runs() > 0) \
            if on_text is not None else None
        if lead_text is not None:
            lead_text.restart()  # a run that starts again (a new session) starts the draft over
        try:
            for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                typ, sub = ev.get("type"), ev.get("subtype")
                marks.feed(ev)
                if typ == "system" and sub == "init":
                    seen = ev.get("session_id") or seen
                elif typ == "system" and sub == "api_retry":
                    retry_error = str(ev.get("error") or "")
                    retry = {k: ev.get(k) for k in ("error", "error_status", "attempt", "max_retries")}
                    on_progress(f"Claude is retrying ({retry_error or 'error'})")
                elif typ == "system" and sub == "model_refusal_no_fallback":
                    refusal = str(ev.get("api_refusal_category") or "unknown")
                elif typ == "system" and sub in ("task_started", "task_notification"):
                    crew_events.task(ev)
                elif typ == "stream_event":
                    if lead_text is not None and not ev.get("parent_tool_use_id"):
                        lead_text.feed(ev.get("event"))
                elif typ == "assistant":
                    if not ev.get("parent_tool_use_id"):
                        api_error = _api_error(ev) or api_error
                    for block in _blocks(ev, "tool_use"):
                        if lead_text is not None and not ev.get("parent_tool_use_id"):
                            lead_text.restart()  # the lead went on to a tool: what it wrote before is not the answer
                        if not crew_events.started(ev, block):
                            label = labels.tool_label(block.get("name"), block.get("input"))
                            if label:  # None: the engine's own housekeeping (ToolSearch), no line
                                on_progress(label)
                            marks.tool_started(ev, block)
                elif typ == "user":
                    for block in _blocks(ev, "tool_result"):
                        crew_events.result(ev, block)
                        marks.tool_ended(ev, block)
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
        stderr = b"".join(stderr_tail)

        def failed(reason: str, fault: Optional[bool]) -> None:
            self._log_failure(redact, delivery, reason, fault, proc.returncode, final, api_error, refusal, retry,
                              stderr, model, effort)

        if timed_out.is_set():
            failed("timeout", True)
            raise ClaudeError("claude code run timed out")
        refused = bool(refusal) or (final or {}).get("stop_reason") == "refusal" or \
            (api_error or {}).get("stop_reason") == "refusal"
        error = (api_error or {}).get("error", "")
        if final is None:
            res = classify_failure(stderr.decode("utf-8", "replace"), retry_error, error=error, refused=refused)
            failed(res.reason, res.platform_fault)
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
        res = classify_failure(text_err, retry_error, str(final.get("subtype") or ""), error=error, refused=refused,
                               status=final.get("api_error_status"), model=choice.get("model", ""),
                               effort=choice.get("effort", ""))
        failed(res.reason, res.platform_fault)
        return res, seen


def _blocks(ev: dict, typ: str) -> list[dict]:
    """The content blocks of one type in an `assistant` / `user` stream-json message."""
    content = (ev.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == typ]


class LeadText:
    """The lead's words as the model writes them, for the draft of the answer (core.Draft, the `on_text` hook).

    With --include-partial-messages the CLI writes the API's own stream events as `stream_event` lines:
    {"type": "stream_event", "event": {...}, "parent_tool_use_id": null, ...} (the Agent SDK's
    SDKPartialAssistantMessage). The caller passes only the lead's (no `parent_tool_use_id`; a specialist's
    text is not sent by the CLI anyway unless asked to). Of the events, only the text of `text` blocks is
    taken (`content_block_delta` with a `text_delta`): thinking, tool input and anything else never are.

    The draft starts over (on_text(None)):
    - at `message_start`: a new message of the lead (after a tool's result, or a retry of the API call, or the
      CLI trying the fallback model after a refusal). The final answer (`result`) is the text of the lead's
      last message, so the draft is always the message being written;
    - when the lead starts a tool call (`content_block_start` of a tool block, or a tool_use in a whole
      `assistant` message): the words before it were the lead saying what it is about to do;
    - when a run starts (restart()), so a second run of the turn (a new session) does not add to the first;
    - at the end of a message (`message_stop`) while specialists the lead started still work in the background
      (`crew_busy`): those words were a status ("Scout is on it"), the answer comes after the crew reports, and
      meanwhile the web app shows the crew's progress lines instead.
    Two text blocks in one message are kept apart by a blank line."""

    _TOOL_BLOCKS = ("tool_use", "server_tool_use", "mcp_tool_use")

    def __init__(self, on_text: Callable[[Optional[str]], None], crew_busy: Callable[[], bool] = lambda: False):
        self.on_text = on_text
        self.crew_busy = crew_busy
        self._wrote = False  # the message being written has text in the draft

    def restart(self) -> None:
        self._wrote = False
        self.on_text(None)

    def feed(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        typ = raw.get("type")
        if typ == "message_start" or (typ == "message_stop" and self.crew_busy()):
            self.restart()
        elif typ == "content_block_start":
            block = raw.get("content_block") if isinstance(raw.get("content_block"), dict) else {}
            if block.get("type") in self._TOOL_BLOCKS:
                self.restart()
            elif block.get("type") == "text":
                if self._wrote:
                    self.on_text("\n\n")
                self._add(block.get("text"))
        elif typ == "content_block_delta":
            delta = raw.get("delta") if isinstance(raw.get("delta"), dict) else {}
            if delta.get("type") == "text_delta":
                self._add(delta.get("text"))

    def _add(self, text: Any) -> None:
        if isinstance(text, str) and text:
            self._wrote = True
            self.on_text(text)


class ClaudeMarks:
    """The turn's stage times (timings.TurnClock) from the CLI's stream-json events. Only the lead's events count
    (a line with a `parent_tool_use_id` is a specialist's); the crew's runs are marked by CrewEvents.

    engine_ready: the `system/init` event (the CLI is up and has asked its tool servers for their tools).
    first_response, first_thinking: the model's first message began, and it began to think; these come from the
    API's own stream events, which the CLI writes only with --include-partial-messages (web chat turns).
    first_text, first_tool: the lead's first words and first tool call, from the stream events when there are
    any, else from the lead's whole messages. engine_done: the `result` event, whose numbers are kept too."""

    _TOOL_BLOCKS = ("tool_use", "server_tool_use", "mcp_tool_use")

    def __init__(self, clock: Any = None):
        self.clock = clock

    def run_started(self) -> None:
        if self.clock is not None:
            self.clock.run_started(RUNTIME_CLAUDE)

    def feed(self, ev: dict) -> None:
        clock = self.clock
        if clock is None or ev.get("parent_tool_use_id"):
            return
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            clock.mark("engine_ready", last=True)  # a run that starts again (a new session) is ready again
        elif typ == "stream_event":
            raw = ev.get("event") if isinstance(ev.get("event"), dict) else {}
            kind = raw.get("type")
            if kind == "message_start":
                clock.mark("first_response")
            elif kind == "content_block_start":
                block = raw.get("content_block") if isinstance(raw.get("content_block"), dict) else {}
                if block.get("type") in ("thinking", "redacted_thinking"):
                    clock.mark("first_thinking")
                elif block.get("type") in self._TOOL_BLOCKS:
                    clock.mark("first_tool")
                elif block.get("type") == "text" and block.get("text"):
                    clock.mark("first_text")
            elif kind == "content_block_delta":
                delta = raw.get("delta") if isinstance(raw.get("delta"), dict) else {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    clock.mark("first_text")
        elif typ == "assistant":
            if any(b.get("text") for b in _blocks(ev, "text")):
                clock.mark("first_text")
            if _blocks(ev, "tool_use"):
                clock.mark("first_tool")
        elif typ == "result":
            clock.mark("engine_done", last=True)
            clock.cli_result(ev)

    def tool_started(self, ev: dict, block: dict) -> None:
        """The lead called a tool of its own (not a specialist: CrewEvents took those)."""
        if self.clock is not None and not ev.get("parent_tool_use_id"):
            self.clock.tool_start(block.get("id"))

    def tool_ended(self, ev: dict, block: dict) -> None:
        if self.clock is not None and not ev.get("parent_tool_use_id"):
            self.clock.tool_end(block.get("tool_use_id"))


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

    def __init__(self, on_progress: Callable[[str], None], clock: Any = None):
        self.progress = crew.Progress(on_progress, clock)
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
