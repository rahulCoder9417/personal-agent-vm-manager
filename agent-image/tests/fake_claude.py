"""A fake `claude` CLI for the Bridge tests (mimics auth login/status/logout and -p stream-json).

State lives in $CLAUDE_CONFIG_DIR:
  .credentials.json   present = signed in (written by `auth login`, removed by `auth logout`)
  fake_mode           optional behaviour switch: limit | crash | hang | max_turns | login_hang | login_no_url
  fake_calls.jsonl    one line per -p run: argv, prompt (stdin), cwd, selected env, the MCP config
  fake_sessions.json  known session ids -> number of turns
"""

import json
import os
import sys
import time

CFG = os.environ.get("CLAUDE_CONFIG_DIR", "")
CREDS = os.path.join(CFG, ".credentials.json")
GOOD_CODE = os.environ.get("FAKE_CLAUDE_GOOD_CODE", "good-code-123")
URL = ("https://claude.ai/oauth/authorize?code=true&client_id=fake&response_type=code"
       "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=xyz")
LOCAL_URL = ("https://claude.ai/oauth/authorize?client_id=fake&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback"
             "&state=abc")


def mode() -> str:
    try:
        with open(os.path.join(CFG, "fake_mode")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def out(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def auth(args) -> int:
    sub = args[0] if args else ""
    if sub == "status":
        ok = os.path.exists(CREDS)
        print(json.dumps({"loggedIn": ok, "authMethod": "claude.ai" if ok else "none"}))
        return 0 if ok else 1
    if sub == "logout":
        if os.path.exists(CREDS):
            os.remove(CREDS)
        print("Successfully logged out from your Anthropic account.")
        return 0
    if sub == "login":
        m = mode()
        if m == "login_no_url":
            time.sleep(60)
            return 1
        # Colours and an OSC 8 hyperlink like a real TUI; the local-callback URL first.
        sys.stdout.write("\x1b[1mWelcome to Claude Code\x1b[0m\r\n")
        sys.stdout.write(f"Opening browser: \x1b]8;;{LOCAL_URL}\x07sign in\x1b]8;;\x07\r\n")
        sys.stdout.write("Browser didn't open? Use the url below to sign in:\r\n\r\n")
        sys.stdout.write(f"\x1b[36m{URL}\x1b[39m\r\n\r\n")
        sys.stdout.write("Paste code here if prompted > ")
        sys.stdout.flush()
        code = sys.stdin.readline().strip()
        if m == "login_hang":
            time.sleep(60)
            return 1
        if code != GOOD_CODE:
            sys.stdout.write("\r\nOAuth error: Invalid code. Please try again.\r\n")
            sys.stdout.flush()
            return 1
        os.makedirs(CFG, exist_ok=True)
        with open(CREDS, "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": "fake-token-never-read"}}, fh)
        os.chmod(CREDS, 0o600)
        sys.stdout.write("\r\nLogin successful. Press Enter to continue\r\n")
        sys.stdout.flush()
        sys.stdin.readline()
        return 0
    print(f"unknown auth command {sub}", file=sys.stderr)
    return 2


def flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def print_mode(args) -> int:
    prompt = sys.stdin.read()
    mcp_path = flag(args, "--mcp-config")
    mcp = None
    if mcp_path and os.path.exists(mcp_path):
        with open(mcp_path) as fh:
            mcp = json.load(fh)
    call = {"argv": args, "prompt": prompt, "cwd": os.getcwd(),
            "env": {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "DISABLE_AUTOUPDATER",
                                                   "HTTPS_PROXY")},
            "mcp": mcp, "mcp_mode": oct(os.stat(mcp_path).st_mode & 0o777) if mcp_path else None}
    with open(os.path.join(CFG, "fake_calls.jsonl"), "a") as fh:
        fh.write(json.dumps(call) + "\n")

    sessions_path = os.path.join(CFG, "fake_sessions.json")
    sessions = {}
    if os.path.exists(sessions_path):
        with open(sessions_path) as fh:
            sessions = json.load(fh)
    resume = flag(args, "--resume")
    sid = resume or flag(args, "--session-id")
    m = mode()
    if m == "crash":
        sys.stderr.write("fatal: something broke\n")
        return 3
    if m == "hang":
        time.sleep(60)
        return 1
    if not os.path.exists(CREDS):
        out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login",
             "session_id": sid})
        return 1
    if resume and resume not in sessions:
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": f"No conversation found with session ID: {resume}", "session_id": resume})
        return 1
    out({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-fake",
         "mcp_servers": [{"name": n, "status": "connected"} for n in ((mcp or {}).get("mcpServers") or {})]})
    sessions[sid] = sessions.get(sid, 0) + 1
    with open(sessions_path, "w") as fh:
        json.dump(sessions, fh)
    if m == "limit":
        out({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 3, "retry_delay_ms": 10,
             "error_status": 429, "error": "rate_limit", "session_id": sid})
        out({"type": "result", "subtype": "success", "is_error": True,
             "result": "You've hit your session limit · resets 3:45pm", "session_id": sid})
        return 1
    if m == "max_turns":
        out({"type": "result", "subtype": "error_max_turns", "is_error": True, "session_id": sid})
        return 1
    out({"type": "assistant", "session_id": sid, "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__platform__web_search", "input": {"q": "x"}}]}})
    out({"type": "user", "session_id": sid, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
    out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "text", "text": "done"}]}})
    out({"type": "result", "subtype": "success", "is_error": False,
         "result": f"reply {sessions[sid]} to: {prompt.strip().splitlines()[-1] if prompt.strip() else ''}",
         "session_id": sid, "total_cost_usd": 0.01})
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["auth"]:
        return auth(args[1:])
    if "-p" in args or "--print" in args:
        return print_mode(args)
    if args[:1] in (["--version"], ["-v"]):
        print("2.1.283 (Claude Code)")
        return 0
    print("interactive mode is not supported by the fake", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
