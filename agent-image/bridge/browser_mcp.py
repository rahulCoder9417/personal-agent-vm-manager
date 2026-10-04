"""The guard in front of the browser's MCP server: what Scout may ask the browser to do.

The browser is driven by Microsoft's Playwright MCP server (bridge/browser.py says which version and how
it is started). That server is made for a developer's own machine: its documentation says it "is not a
security boundary". It offers tools that upload files from the container, run code in the server and write
files, and its own block on `file:` addresses is a guardrail that an address like `view-source:file:///…`
gets around (seen with version 0.0.83). So the engines never talk to it directly. They start this program
instead, as the MCP server named `scout_browser`; it starts the real server and passes messages between the two
(MCP over stdio: one JSON-RPC message per line), with three rules:

- Tools: only the tools named with --allow are listed, and a call to any other tool is refused here. The
  list is Scout's browser tools (crew.BROWSER_TOOLS).
- Addresses: an `url` argument must be a full http:// or https:// address (or about:blank). Every other
  scheme (file:, view-source:, chrome:, javascript:, data:, ftp:) is refused here, so the browser cannot be
  made to read a file in the container. Which hosts it may reach is the egress proxy's business.
- Files: a `filename` argument is dropped, so a tool returns its result instead of writing it to a file in
  the user's workspace.

A refused call is answered with a tool error the model can read, and is never sent on.

Usage: python -I browser_mcp.py --allow <tool>[,<tool>...] [--env NAME=VALUE]... -- <server command>...

It is run as a file, on its own (like lead_gate.py): standard library only, nothing imported from the
Bridge. It ends when its input ends (the engine is gone) or the server ends, and takes the server and its
browser with it. If it cannot start the server it exits with an error: there is then no browser, never an
unguarded one.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
from typing import Any, BinaryIO, Optional

TOOLS_CALL, TOOLS_LIST = "tools/call", "tools/list"
_HTTP = re.compile(r"^https?://[^\s/?#]", re.IGNORECASE)
_BLANK = "about:blank"

REFUSED_TOOL = ("This tool is not available. The browser tools you have are: {tools}.")
REFUSED_URL = ("The browser opens only full http:// or https:// addresses. This address was refused and was "
               "not opened; do not try it in another form.")


def allowed_url(url: Any) -> bool:
    """True for a full http(s) address and for about:blank. Anything else (another scheme, no scheme, not a
    string, an address with a line break or a space in it) is refused."""
    if not isinstance(url, str):
        return False
    url = url.strip()
    if url.lower() == _BLANK:
        return True
    return bool(_HTTP.match(url)) and not any(ch.isspace() or ord(ch) < 32 for ch in url)


def refusal(msg_id: Any, text: str) -> dict:
    """The answer to a refused tools/call: a tool error, which the model reads as the tool's result."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}], "isError": True}}


class Guard:
    """The rules, applied to one message at a time. No I/O here, so it can be tested on its own."""

    def __init__(self, allow: list[str]):
        self.allow = list(allow)
        self._lists: set[str] = set()  # ids of tools/list requests whose answers must be filtered
        self._lock = threading.Lock()

    def from_client(self, msg: Any) -> tuple[Optional[Any], Optional[dict]]:
        """(message to send on to the server, answer to give the client instead). Exactly one is set."""
        if not isinstance(msg, dict):
            return msg, None
        method = msg.get("method")
        if method == TOOLS_LIST and "id" in msg:
            with self._lock:
                self._lists.add(json.dumps(msg["id"]))
            return msg, None
        if method != TOOLS_CALL:
            return msg, None
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        name = params.get("name")
        if name not in self.allow:
            return None, refusal(msg.get("id"), REFUSED_TOOL.format(tools=", ".join(self.allow)))
        args = params.get("arguments")
        if isinstance(args, dict):
            if "url" in args and not allowed_url(args["url"]):
                return None, refusal(msg.get("id"), REFUSED_URL)
            if "filename" in args:
                args = {k: v for k, v in args.items() if k != "filename"}
                msg = {**msg, "params": {**params, "arguments": args}}
        elif args is not None:
            return None, refusal(msg.get("id"), "The tool's arguments must be an object.")
        return msg, None

    def from_server(self, msg: Any) -> Any:
        """The server's message as the client may see it: a tools/list answer has only the allowed tools."""
        if not isinstance(msg, dict) or "id" not in msg:
            return msg
        key = json.dumps(msg["id"])
        with self._lock:
            if key not in self._lists:
                return msg
            self._lists.discard(key)
        result = msg.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            tools = [t for t in result["tools"] if isinstance(t, dict) and t.get("name") in self.allow]
            msg = {**msg, "result": {**result, "tools": tools}}
        return msg


def _write(out: BinaryIO, lock: threading.Lock, msg: Any) -> None:
    data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
    with lock:
        out.write(data)
        out.flush()


def _parse(line: bytes) -> tuple[bool, Any]:
    try:
        return True, json.loads(line)
    except ValueError:
        return False, None


def client_to_server(guard: Guard, src: BinaryIO, server_in: BinaryIO, client_out: BinaryIO,
                     out_lock: threading.Lock) -> None:
    """Pass the client's messages to the server, one per line, until the client's side ends. A line that is
    not JSON cannot be a tool call the server would run; it is dropped."""
    in_lock = threading.Lock()
    for line in iter(src.readline, b""):
        if not line.strip():
            continue
        ok, msg = _parse(line)
        if not ok:
            continue
        # A batch is taken apart: every message in it goes through the rules on its own.
        for one in (msg if isinstance(msg, list) else [msg]):
            forward, answer = guard.from_client(one)
            if answer is not None:
                _write(client_out, out_lock, answer)
            elif forward is not None:
                _write(server_in, in_lock, forward)


def server_to_client(guard: Guard, server_out: BinaryIO, client_out: BinaryIO, out_lock: threading.Lock) -> None:
    """Pass the server's messages to the client until the server's side ends."""
    for line in iter(server_out.readline, b""):
        if not line.strip():
            continue
        ok, msg = _parse(line)
        if not ok:
            continue  # the server's stdout carries messages only; anything else is noise
        if isinstance(msg, list):
            msg = [guard.from_server(one) for one in msg]
        else:
            msg = guard.from_server(msg)
        try:
            _write(client_out, out_lock, msg)
        except (BrokenPipeError, OSError):
            return


def parse_args(argv: list[str]) -> tuple[list[str], dict[str, str], list[str]]:
    """(allowed tools, environment additions, server command) from the command line."""
    if "--" not in argv:
        raise ValueError("usage: browser_mcp.py --allow <tools> [--env NAME=VALUE]... -- <server command>...")
    cut = argv.index("--")
    opts, cmd = argv[:cut], argv[cut + 1:]
    allow: list[str] = []
    env: dict[str, str] = {}
    i = 0
    while i < len(opts):
        opt = opts[i]
        if opt in ("--allow", "--env") and i + 1 < len(opts):
            value = opts[i + 1]
            if opt == "--allow":
                allow += [t.strip() for t in value.split(",") if t.strip()]
            else:
                name, sep, val = value.partition("=")
                if not sep or not name:
                    raise ValueError(f"--env needs NAME=VALUE, got {value!r}")
                env[name] = val
            i += 2
        else:
            raise ValueError(f"unknown option {opt!r}")
    if not allow:
        raise ValueError("--allow needs at least one tool")
    if not cmd:
        raise ValueError("no server command after --")
    return allow, env, cmd


def stop(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """End the server: its input is closed (it then closes the browser and exits); it is killed if it
    does not."""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(sig)
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def main(argv: list[str]) -> int:
    try:
        allow, extra_env, cmd = parse_args(argv)
    except ValueError as exc:
        sys.stderr.write(f"browser_mcp: {exc}\n")
        return 2
    env = dict(os.environ)
    env.update(extra_env)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    except OSError as exc:
        sys.stderr.write(f"browser_mcp: could not start the browser server: {exc}\n")
        return 1
    guard = Guard(allow)
    out_lock = threading.Lock()
    client_in, client_out = sys.stdin.buffer, sys.stdout.buffer
    done = threading.Event()

    def up() -> None:
        try:
            client_to_server(guard, client_in, proc.stdin, client_out, out_lock)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            done.set()

    def down() -> None:
        try:
            server_to_client(guard, proc.stdout, client_out, out_lock)
        finally:
            done.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: done.set())
    for target in (up, down):
        threading.Thread(target=target, daemon=True).start()
    done.wait()
    stop(proc)
    return proc.returncode if proc.returncode is not None and proc.returncode >= 0 else 0


if __name__ == "__main__":
    # Leave at once, without the interpreter's shutdown: a thread may still be waiting on the client's
    # input, and Python aborts when it has to tear down a stream another thread is reading. Everything
    # written was flushed when it was written, and the server has been ended.
    os._exit(main(sys.argv[1:]))
