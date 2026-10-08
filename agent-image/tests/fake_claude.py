"""A fake `claude` CLI for the Bridge tests (mimics auth login/status/logout and -p stream-json).

State lives in $CLAUDE_CONFIG_DIR:
  .credentials.json   present = signed in (written by `auth login`, removed by `auth logout`)
  fake_mode           optional behaviour switch: limit | refusal | crash | crash_noisy | hang | max_turns |
                      login_hang | login_no_url |
                      crew (the lead starts specialists in the foreground) | crew_background |
                      crew_unreported (a background specialist whose end the stream never reports) |
                      crew_hang (a specialist starts, then the CLI hangs) |
                      web (the lead calls the web and browser tools itself, then Scout calls them; the
                      PreToolUse hooks of --settings are run for each call)
  fake_calls.jsonl    one line per -p run: argv, prompt (stdin), cwd, selected env, the MCP config, --agents,
                      the --settings file, and `history`: the messages of the session this run continues
  fake_sessions.json  known session ids -> the messages written into each, in order
  projects/<cwd>/<session id>.jsonl   a transcript file per session, like the real CLI's (one line per message)

Like the real CLI (2.1.283, checked against a fake API): every message is written into the session before the
model is called, a failed one included; `--resume <id> --fork-session` continues a copy of the session under a
new id (the init event names it) and leaves the original unchanged.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid

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
    agents_arg = flag(args, "--agents")
    try:
        agents = json.loads(agents_arg) if agents_arg else None
    except ValueError:
        # Like the real CLI: an invalid --agents value ends the run before the session starts.
        sys.stderr.write("Error: Invalid --agents configuration:\ninvalid JSON\n")
        return 1
    settings_path = flag(args, "--settings")
    settings = None
    if settings_path and os.path.exists(settings_path):
        with open(settings_path) as fh:
            settings = json.load(fh)
    call = {"argv": args, "prompt": prompt, "cwd": os.getcwd(),
            "env": {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "DISABLE_AUTOUPDATER",
                                                   "HTTPS_PROXY", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
                                                   "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS",
                                                   "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
                                                   "CLAUDE_CODE_DISABLE_AUTO_MEMORY")},
            "mcp": mcp, "mcp_mode": oct(os.stat(mcp_path).st_mode & 0o777) if mcp_path else None,
            "agents": agents, "settings": settings,
            "settings_mode": oct(os.stat(settings_path).st_mode & 0o777) if settings is not None else None}
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
    if m == "crash_noisy":
        # stderr that holds what must never reach the Bridge's log: a token, a key and the user's message.
        sys.stderr.write("connecting to http://api:8080/mcp with Authorization: Bearer tok-123\n")
        sys.stderr.write("oauth refresh used sk-ant-oat01-AbCdEfGhIjKlMnOp\n")
        sys.stderr.write(f"while handling the prompt: {prompt.strip()}\n")
        sys.stderr.write("\n" * 3)
        sys.stderr.write("TypeError: undefined is not an object (evaluating 'x.y')\n")
        sys.stderr.write("fatal: something broke\n")
        return 3
    if not os.path.exists(CREDS):
        out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login",
             "session_id": sid})
        return 1
    if resume and resume not in sessions:
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": f"No conversation found with session ID: {resume}", "session_id": resume})
        return 1
    history = list(sessions.get(sid, []))
    if resume and "--fork-session" in args:
        sid = str(uuid.uuid4())  # a copy of the session under a new id; the original is left as it was
    out({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-fake",
         "mcp_servers": [{"name": n, "status": "connected"} for n in ((mcp or {}).get("mcpServers") or {})]})
    sessions[sid] = history + [prompt]  # written before the model is called: a failed turn's message too
    with open(sessions_path, "w") as fh:
        json.dump(sessions, fh)
    project = os.path.join(CFG, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
    os.makedirs(project, exist_ok=True)
    with open(os.path.join(project, f"{sid}.jsonl"), "w") as fh:
        fh.writelines(json.dumps({"type": "user", "sessionId": sid, "message": p}) + "\n" for p in sessions[sid])
    with open(os.path.join(CFG, "fake_calls.jsonl")) as fh:
        lines = fh.readlines()
    lines[-1] = json.dumps({**json.loads(lines[-1]), "history": history, "session": sid}) + "\n"
    with open(os.path.join(CFG, "fake_calls.jsonl"), "w") as fh:
        fh.writelines(lines)
    if m == "hang":
        time.sleep(60)
        return 1
    if m == "limit":
        # As 2.1.283 reports the plan's limit (a 429 with the subscription's limit headers).
        out({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 3, "retry_delay_ms": 10,
             "error_status": 429, "error": "rate_limit", "session_id": sid})
        msg(sid, "assistant", [{"type": "text", "text": "You've hit your session limit · resets 3:45pm"}],
            error="rate_limit")
        out({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
             "api_error_status": 429, "stop_reason": "stop_sequence",
             "result": "You've hit your session limit · resets 3:45pm", "session_id": sid})
        return 1
    if m == "refusal":
        # As 2.1.283 reports a request Anthropic's safeguards refused, on the first model and on the fallback.
        out({"type": "system", "subtype": "informational", "level": "notice", "session_id": sid,
             "content": "Opus 5.5's safeguards stopped the response above · continuing once with that noted"})
        out({"type": "system", "subtype": "model_refusal_fallback", "trigger": "refusal", "direction": "retry",
             "scope": "session", "original_model": "claude-opus-5-5", "fallback_model": "claude-opus-5",
             "api_refusal_category": "cyber", "session_id": sid})
        out({"type": "system", "subtype": "model_refusal_no_fallback", "original_model": "claude-opus-5",
             "api_refusal_category": "cyber", "session_id": sid, "content": ""})
        text = ("API Error: Opus 5's safeguards flagged this message (https://www.anthropic.com/legal/aup). "
                "Try rephrasing the request in a new session or change your model.")
        out({"type": "assistant", "session_id": sid, "parent_tool_use_id": None, "error": "invalid_request",
             "message": {"model": "<synthetic>", "role": "assistant", "stop_reason": "refusal",
                         "stop_details": {"type": "refusal", "category": "cyber", "explanation": "blocked"},
                         "content": [{"type": "text", "text": text}]}})
        out({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
             "api_error_status": None, "stop_reason": "refusal", "num_turns": 2, "result": text,
             "session_id": sid})
        return 1
    if m == "max_turns":
        out({"type": "result", "subtype": "error_max_turns", "is_error": True, "session_id": sid})
        return 1
    if m == "crew_hang":
        msg(sid, "assistant", [agent_call("h1", "byte", "a long job")])
        time.sleep(60)
        return 1
    if m == "web":
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
             "result": "\n".join(the_web_tools(sid, settings))})
        return 0
    if m in CREW_MODES:
        CREW_MODES[m](sid)
        out({"type": "result", "subtype": "success", "is_error": False, "result": "the crew is done",
             "session_id": sid})
        return 0
    out({"type": "assistant", "session_id": sid, "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__platform__get_profile", "input": {}}]}})
    out({"type": "user", "session_id": sid, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
    out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "text", "text": "done"}]}})
    out({"type": "result", "subtype": "success", "is_error": False,
         "result": f"reply {len(sessions[sid])} to: {prompt.strip().splitlines()[-1] if prompt.strip() else ''}",
         "session_id": sid, "total_cost_usd": 0.01})
    return 0


def msg(sid, role, content, parent=None, **extra) -> None:
    """One `assistant` / `user` message; `parent` = the Agent tool call a sub-agent's message belongs to."""
    out({"type": role, "session_id": sid, "parent_tool_use_id": parent, "message": {"role": role, "content": content},
         **extra})


def agent_call(call_id, agent, description, tool="Agent", **extra):
    return {"type": "tool_use", "id": call_id, "name": tool,
            "input": {"subagent_type": agent, "description": description, "prompt": "the task", **extra}}


def pre_tool_use(settings, sid, call_id, name, tool_input, agent=None):
    """Run the `PreToolUse` command hooks of --settings for one tool call, the way the docs describe
    (code.claude.com/docs/en/hooks): a matcher made of exact tool names separated by `|`; the command
    through `sh -c`, with the call as JSON on stdin; `agent_id` and `agent_type` in it only for a call from
    inside a sub-agent; exit code 2 blocks the call and stderr is the reason. Returns that reason, or None
    when the call may run."""
    event = {"session_id": sid, "cwd": os.getcwd(), "permission_mode": "dontAsk", "hook_event_name": "PreToolUse",
             "tool_name": name, "tool_input": tool_input, "tool_use_id": call_id}
    if agent:
        event.update(agent_id=f"agent-{call_id}", agent_type=agent)
    for group in ((settings or {}).get("hooks") or {}).get("PreToolUse") or []:
        matcher = group.get("matcher") or "*"
        if matcher != "*" and name not in [part.strip() for part in matcher.split("|")]:
            continue
        for hook in group.get("hooks") or []:
            if hook.get("type") != "command":
                continue
            done = subprocess.run(["sh", "-c", hook["command"]], input=json.dumps(event), capture_output=True,
                                  text=True, timeout=hook.get("timeout", 600))
            if done.returncode == 2:
                return done.stderr.strip()
    return None


# Scout's web tools: the built-in search, the platform's two web tools, and the tools of the MCP server
# `scout_browser` (the browser inside the container).
BROWSER_TOOLS = ("browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_find", "browser_click",
                 "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key", "browser_hover",
                 "browser_wait_for", "browser_handle_dialog", "browser_tabs", "browser_close")
WEB_TOOLS = ("WebSearch", "mcp__platform__web_search", "mcp__platform__read_page",
             *(f"mcp__scout_browser__{name}" for name in BROWSER_TOOLS))


def the_web_tools(sid, settings) -> list:
    """The lead calls each web tool itself (the search, read_page, the browser tools) and another
    platform tool, then starts Scout, which calls each web tool. Every call goes through the PreToolUse
    hooks. Returns one line per call: who, the tool, and "ok" or "refused: <reason>"."""
    lines = []

    def call(who, call_id, name, parent=None):
        msg(sid, "assistant", [{"type": "tool_use", "id": call_id, "name": name, "input": {}}], parent=parent)
        reason = pre_tool_use(settings, sid, call_id, name, {}, agent=who if parent else None)
        result = {"type": "tool_result", "tool_use_id": call_id, "content": reason or "ok"}
        if reason:
            result["is_error"] = True
        msg(sid, "user", [result], parent=parent)
        lines.append(f"{who} {name}: " + (f"refused: {reason}" if reason else "ok"))

    for i, name in enumerate((*WEB_TOOLS, "mcp__platform__get_profile")):
        call("lead", f"l{i}", name)
    msg(sid, "assistant", [agent_call("a1", "scout", "looking it up")])
    msg(sid, "user", [{"type": "text", "text": "the task"}], parent="a1")
    for i, name in enumerate(WEB_TOOLS):
        call("scout", f"s{i}", name, parent="a1")
    msg(sid, "user", [{"type": "tool_result", "tool_use_id": "a1", "content": [{"type": "text", "text": "report"}]}],
        tool_use_result={"status": "completed", "agentId": "x1", "agentType": "scout", "prompt": "the task",
                         "content": [{"type": "text", "text": "report"}]})
    msg(sid, "assistant", [{"type": "text", "text": "done"}])
    return lines


def crew_foreground(sid) -> None:
    """The lead starts Scout, then Byte (which fails) next to a sub-agent that is not a crew member."""
    msg(sid, "assistant", [agent_call("a1", "scout", "researching flight options")])
    out({"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": "a1", "session_id": sid,
         "description": "researching flight options", "task_type": "local_agent", "is_backgrounded": False})
    msg(sid, "user", [{"type": "text", "text": "the task"}], parent="a1")
    # Scout's own work: its tool calls show up with parent_tool_use_id set.
    msg(sid, "assistant", [{"type": "tool_use", "id": "s1", "name": "mcp__platform__web_search", "input": {}}],
        parent="a1")
    msg(sid, "user", [{"type": "tool_result", "tool_use_id": "s1", "content": "results"}], parent="a1")
    msg(sid, "user", [{"type": "tool_result", "tool_use_id": "a1", "content": [{"type": "text", "text": "report"}]}],
        tool_use_result={"status": "completed", "agentId": "x1", "agentType": "scout", "prompt": "the task",
                         "content": [{"type": "text", "text": "report"}]})
    msg(sid, "assistant", [agent_call("a2", "byte", "Byte: fixing the login bug"),
                           agent_call("a3", "general-purpose", "something else")])
    msg(sid, "user", [{"type": "tool_result", "tool_use_id": "a2", "is_error": True,
                       "content": "Agent terminated early due to an API error"}])
    msg(sid, "user", [{"type": "tool_result", "tool_use_id": "a3", "is_error": True,
                       "content": "Agent type 'general-purpose' not found"}])
    msg(sid, "assistant", [{"type": "text", "text": "the crew is done"}])


def crew_background(sid) -> None:
    """The lead starts Patch in the background (old tool name `Task`); it ends with a task_notification."""
    msg(sid, "assistant", [agent_call("b1", "patch", "reviewing pull request 12", tool="Task",
                                      run_in_background=True)])
    out({"type": "system", "subtype": "task_started", "task_id": "t9", "tool_use_id": "b1", "session_id": sid,
         "description": "reviewing pull request 12", "task_type": "local_agent", "is_backgrounded": True})
    msg(sid, "user", [{"type": "tool_result", "tool_use_id": "b1", "content": "Async agent launched"}],
        tool_use_result={"status": "async_launched", "agentId": "x2", "description": "reviewing pull request 12",
                         "prompt": "the task", "outputFile": "/data/tmp/x2.output"})
    msg(sid, "assistant", [{"type": "tool_use", "id": "l1", "name": "Read", "input": {}}])  # the lead carries on
    msg(sid, "user", [{"type": "tool_result", "tool_use_id": "l1", "content": "file"}])
    # A background shell task of the lead must not be taken for a specialist.
    out({"type": "system", "subtype": "task_notification", "task_id": "t0", "tool_use_id": "l1",
         "status": "completed", "output_file": "", "summary": "", "session_id": sid})
    out({"type": "system", "subtype": "task_notification", "task_id": "t9", "status": "completed",
         "output_file": "/data/tmp/x2.output", "summary": "review posted", "session_id": sid})
    msg(sid, "assistant", [{"type": "text", "text": "the crew is done"}])


def crew_unreported(sid) -> None:
    """Two background specialists. Scout's notification names it only by the id its launch answer gave
    (no task_started, no tool_use_id); Byte's never comes: the run just ends."""
    msg(sid, "assistant", [agent_call("u1", "scout", "reading 4 pages"), agent_call("u2", "byte", "fixing a typo")])
    for call_id, agent_id in (("u1", "x7"), ("u2", "x8")):
        msg(sid, "user", [{"type": "tool_result", "tool_use_id": call_id, "content": "Async agent launched"}],
            tool_use_result={"status": "async_launched", "agentId": agent_id, "description": "d", "prompt": "the task",
                             "outputFile": f"/data/tmp/{agent_id}.output"})
    out({"type": "system", "subtype": "task_notification", "task_id": "x7", "status": "completed",
         "output_file": "/data/tmp/x7.output", "summary": "", "session_id": sid})
    msg(sid, "assistant", [{"type": "text", "text": "the crew is done"}])


CREW_MODES = {"crew": crew_foreground, "crew_background": crew_background, "crew_unreported": crew_unreported}


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
