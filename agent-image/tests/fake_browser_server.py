"""A stand-in for the Playwright MCP server (stdio: one JSON-RPC message per line), for the guard's tests.

It lists the tools the real server lists (version 0.0.83: 25 of them) and answers every tools/call with the
name and arguments it received. Each message it gets is appended to $FAKE_BROWSER_LOG as one JSON line, so a
test can see exactly what the guard let through; "exit" is written there when its input ends.

Usage: fake_browser_server.py [--exit-after N]   (N: end by itself after N messages)
"""

import json
import os
import sys

TOOLS = ["browser_close", "browser_resize", "browser_console_messages", "browser_handle_dialog",
         "browser_emulate_media", "browser_evaluate", "browser_file_upload", "browser_drop", "browser_find",
         "browser_fill_form", "browser_press_key", "browser_type", "browser_navigate", "browser_navigate_back",
         "browser_network_requests", "browser_network_request", "browser_run_code_unsafe",
         "browser_take_screenshot", "browser_snapshot", "browser_click", "browser_drag", "browser_hover",
         "browser_select_option", "browser_tabs", "browser_wait_for"]


def log(entry) -> None:
    path = os.environ.get("FAKE_BROWSER_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def send(msg) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> int:
    limit = int(sys.argv[2]) if sys.argv[1:2] == ["--exit-after"] else 0
    log({"start": {"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in ("PLAYWRIGHT_BROWSERS_PATH", "HOME",
                                                                                 "TMPDIR", "EXTRA")}}})
    seen = 0
    for line in sys.stdin:
        msg = json.loads(line)
        log(msg)
        seen += 1
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "Playwright", "version": "fake"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": name, "description": name, "inputSchema": {"type": "object"}} for name in TOOLS]}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            text = json.dumps({"ran": params.get("name"), "arguments": params.get("arguments")})
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}]}})
            # A notification and a request of the server's own, between two answers: both must pass.
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "x"}})
        elif msg_id is not None and method:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "unknown method"}})
        if limit and seen >= limit:
            log("gone")
            return 3
    log("exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
