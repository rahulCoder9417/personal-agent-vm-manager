"""Claude Code runtime tests against a fake `claude` executable (tests/fake_claude.py)."""

import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from bridge import browser, crew
from bridge.claude import (LEAD_GATE, MSG_NOT_SIGNED_IN, MSG_REFUSED, ClaudeAuth, ClaudeError, ClaudeRunner,
                           CrewEvents, LoginNotFound, Redactor, classify_failure, find_authorize_url, render_settings)
from bridge.core import MEMORY_RULES, Bridge, DeliverRequest, DeliveryStore
from bridge.lead_gate import BLOCK, PASS, REFUSAL
from bridge.platform import load_platform_file
from bridge.runtime import PlatformReader, RuntimeRunner
from bridge.server import BridgeHTTPServer

from tests.fakes import FakeRunner, FakeSender

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET = "s3cret-bridge"
GOOD = "good-code-123"
# Scout's web tools as Claude Code names them: the built-in search, the platform's two web tools, and the
# tools of the MCP server `scout_browser` (the browser inside the container). Written out here on purpose: the
# tests pin the names.
WEB_TOOLS = ["WebSearch", "mcp__platform__web_search", "mcp__platform__read_page",
             "mcp__scout_browser__browser_navigate", "mcp__scout_browser__browser_navigate_back",
             "mcp__scout_browser__browser_snapshot", "mcp__scout_browser__browser_find", "mcp__scout_browser__browser_click",
             "mcp__scout_browser__browser_type", "mcp__scout_browser__browser_fill_form", "mcp__scout_browser__browser_select_option",
             "mcp__scout_browser__browser_press_key", "mcp__scout_browser__browser_hover", "mcp__scout_browser__browser_wait_for",
             "mcp__scout_browser__browser_handle_dialog", "mcp__scout_browser__browser_tabs", "mcp__scout_browser__browser_close"]
BROWSER_TOOLS = WEB_TOOLS[3:]


def make_fake_cli(bin_dir: str) -> str:
    path = os.path.join(bin_dir, "claude")
    with open(path, "w") as fh:
        fh.write(f"#!/bin/sh\nexec '{sys.executable}' '{os.path.join(HERE, 'fake_claude.py')}' \"$@\"\n")
    os.chmod(path, 0o755)
    return path


class CaptureLogs:
    """Captures every log record (all loggers, DEBUG) as formatted text."""

    def __enter__(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s %(args)s"))
        root = logging.getLogger()
        self.old = root.level
        root.addHandler(self.handler)
        root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.removeHandler(self.handler)
        root.setLevel(self.old)

    @property
    def text(self) -> str:
        return self.stream.getvalue()


class Env:
    """A fake /data with platform.json and the fake CLI on PATH."""

    def __init__(self, runtime="claude_code"):
        self.root = tempfile.mkdtemp()
        self.data = os.path.join(self.root, "data")
        self.bin = os.path.join(self.root, "bin")
        os.makedirs(os.path.join(self.data, ".platform"))
        os.makedirs(self.bin)
        self.cli = make_fake_cli(self.bin)
        self.cfg = os.path.join(self.data, "claude")
        os.makedirs(self.cfg)
        self.platform_path = os.path.join(self.data, ".platform", "platform.json")
        self.write_platform(runtime)
        # What the image has under /opt/browser: the browser's MCP server and the browser itself.
        self.browser_root = os.path.join(self.root, "browser")
        os.makedirs(os.path.join(self.browser_root, "node_modules", "@playwright", "mcp"))
        os.makedirs(os.path.join(self.browser_root, "ms-playwright"))
        with open(os.path.join(self.browser_root, "node_modules", "@playwright", "mcp", "cli.js"), "w") as fh:
            fh.write("// the browser's MCP server\n")
        self.env = {"PATH": self.bin + os.pathsep + os.environ.get("PATH", ""), "DATA_DIR": self.data,
                    "HOME": os.path.join(self.data, "home"), "CLAUDE_CONFIG_DIR": self.cfg,
                    "PLATFORM_MCP_URL": "http://api:8080/mcp", "HTTPS_PROXY": "http://proxy:3128",
                    "ANTHROPIC_API_KEY": "sk-platform-must-not-leak", "BROWSER_ROOT": self.browser_root,
                    "BROWSER_NODE": "/usr/local/bin/node", "TMPDIR": os.path.join(self.data, "tmp")}
        os.makedirs(self.env["HOME"])

    def write_platform(self, runtime, browser_enabled=False):
        body = {"version": 1, "user_id": "u1", "mcp_token": "tok-123", "bridge_secret": SECRET,
                "litellm_key": "sk-1", "runtime": runtime,
                "mcp_servers": [{"id": "Notes", "name": "notes", "url": "https://notes.example.org/mcp",
                                 "auth_header_name": "X-Api-Key", "auth_secret": "k1"}]}
        if browser_enabled:
            body["browser_enabled"] = True
        with open(self.platform_path, "w") as fh:
            json.dump(body, fh)

    def mode(self, m):
        with open(os.path.join(self.cfg, "fake_mode"), "w") as fh:
            fh.write(m)

    def sign_in(self):
        with open(os.path.join(self.cfg, ".credentials.json"), "w") as fh:
            fh.write("{}")

    def calls(self):
        p = os.path.join(self.cfg, "fake_calls.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def reader(self):
        return PlatformReader(self.platform_path, load_platform_file(self.platform_path))


def failure_logs(text):
    """The JSON of every "claude run failed" log line."""
    return [json.JSONDecoder().raw_decode(ln.split("claude run failed: ", 1)[1])[0]
            for ln in text.splitlines() if "claude run failed: " in ln]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def flags(argv, name):
    """Every value of a flag that takes a list: the arguments up to the next flag."""
    values = []
    for arg in argv[argv.index(name) + 1:] if name in argv else []:
        if arg.startswith("--"):
            break
        values.append(arg)
    return values


class ClaudeRunnerTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.auth = ClaudeAuth([self.e.cli], self.e.env)
        self.runner = ClaudeRunner([self.e.cli], self.e.env, self.e.data, self.e.reader().load, auth=self.auth)
        self.progress = []

    def run_turn(self, text="hello", session_key="telegram:42", timeout=20):
        return self.runner.run(text=text, instructions="The user is talking to you through telegram.",
                               session_key=session_key, idempotency_key="d#1", on_progress=self.progress.append,
                               timeout=timeout)

    def test_stream_json_run_and_cli_arguments(self):
        self.e.sign_in()
        res = self.run_turn("what's up?")
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.output, "reply 1 to: what's up?")
        self.assertIn("using get_profile", self.progress)
        call = self.e.calls()[0]
        argv = call["argv"]
        for a in ("-p", "--verbose", "--strict-mcp-config"):
            self.assertIn(a, argv)
        self.assertEqual(flag(argv, "--output-format"), "stream-json")
        self.assertEqual(flag(argv, "--permission-mode"), "dontAsk")
        self.assertEqual(flag(argv, "--permission-prompts"), "none")
        # The session's permission rules (the lead and the specialists share them). Denied: what nobody has.
        # Allowed: the lead's tools, plus WebSearch, which only Scout uses (see the hook below).
        self.assertEqual(flags(argv, "--disallowedTools"), ["mcp__platform__send_message", "WebFetch"])
        self.assertEqual(flag(argv, "--allowedTools"),
                         "Bash,Read,Edit,Write,Glob,Grep,NotebookEdit,TodoWrite,WebSearch,mcp__platform,"
                         "mcp__user_notes,Agent")
        self.assertEqual(flag(argv, "--setting-sources"), "user")
        # The hook that refuses the lead's own calls to Scout's web tools: a private settings file.
        self.assertEqual(flag(argv, "--settings"), os.path.join(self.e.data, ".platform", "claude-settings.json"))
        self.assertEqual(call["settings"], render_settings())
        self.assertEqual(call["settings_mode"], oct(0o600))
        self.assertIn("telegram", flag(argv, "--append-system-prompt"))
        self.assertIsNotNone(flag(argv, "--session-id"))
        self.assertNotIn("what's up?", " ".join(argv), "the prompt goes on stdin, not argv")
        self.assertEqual(call["prompt"], "what's up?")
        self.assertEqual(call["cwd"], os.path.join(self.e.data, "workspace"))
        self.assertIsNone(call["env"]["ANTHROPIC_API_KEY"], "platform keys must never reach the CLI")
        self.assertEqual(call["env"]["CLAUDE_CONFIG_DIR"], self.e.cfg)
        self.assertEqual(call["env"]["DISABLE_AUTOUPDATER"], "1")
        # One memory, the platform's: Claude Code's own auto memory is off.
        self.assertEqual(call["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(call["env"]["HTTPS_PROXY"], "http://proxy:3128")
        # MCP config: Platform MCP with the container token + the user's own server; private file.
        servers = call["mcp"]["mcpServers"]
        self.assertEqual(servers["platform"], {"type": "http", "url": "http://api:8080/mcp",
                                               "headers": {"Authorization": "Bearer tok-123"}})
        self.assertEqual(servers["user_notes"]["headers"], {"X-Api-Key": "k1"})
        self.assertEqual(call["mcp_mode"], oct(0o600))
        self.assertTrue(self.auth.connected())
        # The browser is not turned on for this user: no browser server, no browser tool allowed, no
        # settings file for it.
        self.assertEqual(sorted(servers), ["platform", "user_notes"])
        self.assertNotIn("browser", flag(argv, "--allowedTools"))
        self.assertFalse(os.path.exists(os.path.join(self.e.data, ".platform", "browser-mcp.json")))

    def test_the_browser_when_it_is_turned_on_for_the_user(self):
        self.e.sign_in()
        runner = ClaudeRunner([self.e.cli], self.e.env, self.e.data, self.e.reader().load, with_browser=True)
        res = runner.run(text="book it", instructions="i", session_key="s", idempotency_key="k",
                         on_progress=self.progress.append, timeout=20)
        self.assertEqual(res.status, "completed")
        call = self.e.calls()[0]
        argv = call["argv"]
        # The MCP config has a third server, `scout_browser`: a stdio server the CLI starts for this run. It is
        # the guard (browser_mcp.py as a file, isolated mode) with Scout's browser tools, and behind it the
        # Playwright MCP server with its settings file.
        servers = call["mcp"]["mcpServers"]
        self.assertEqual(sorted(servers), ["platform", "scout_browser", "user_notes"])
        config_path = os.path.join(self.e.data, ".platform", "browser-mcp.json")
        self.assertEqual(servers["scout_browser"], {
            "type": "stdio", "command": sys.executable,
            "args": ["-I", browser.GUARD, "--allow", ",".join(crew.BROWSER_TOOLS),
                     "--env", "PLAYWRIGHT_BROWSERS_PATH=" + os.path.join(self.e.browser_root, "ms-playwright"),
                     "--env", "HOME=" + self.e.env["HOME"], "--env", "TMPDIR=" + self.e.env["TMPDIR"],
                     "--", "/usr/local/bin/node",
                     os.path.join(self.e.browser_root, "node_modules", "@playwright", "mcp", "cli.js"),
                     "--config", config_path]})
        self.assertEqual(servers["scout_browser"], browser.claude_server(self.e.env, self.e.data))
        # No secret of the container is on that command line.
        for secret in ("tok-123", SECRET, "sk-1", "sk-platform-must-not-leak"):
            self.assertNotIn(secret, json.dumps(servers["scout_browser"]))
        # Its settings file was written before the run, private, with the settings the browser needs here.
        with open(config_path) as fh:
            config = json.load(fh)
        self.assertEqual(config, browser.render_config(self.e.env, self.e.data))
        self.assertEqual(oct(os.stat(config_path).st_mode & 0o777), oct(0o600))
        # The session allows Scout's browser tools one by one (never the whole server), next to what it
        # allowed before; nothing more is denied, because a session deny would take a tool from Scout too.
        self.assertEqual(flag(argv, "--allowedTools"),
                         "Bash,Read,Edit,Write,Glob,Grep,NotebookEdit,TodoWrite,WebSearch,"
                         + ",".join(BROWSER_TOOLS) + ",mcp__platform,mcp__user_notes,Agent")
        self.assertNotIn("mcp__scout_browser", flag(argv, "--allowedTools").split(","))
        self.assertEqual(flags(argv, "--disallowedTools"), ["mcp__platform__send_message", "WebFetch"])
        # Scout has the browser tools, and its brief says how to use them; Byte and Patch have them denied.
        agents = json.loads(flag(argv, "--agents"))
        self.assertEqual(agents, crew.claude_agents("platform", browser=True))
        self.assertEqual(agents["scout"]["tools"], WEB_TOOLS)
        self.assertIn("call browser_close as soon as you are done", agents["scout"]["prompt"])
        for member in ("byte", "patch"):
            self.assertEqual([t for t in agents[member]["tools"] if "browser" in t], [])
            for tool in BROWSER_TOOLS:
                self.assertIn(tool, agents[member]["disallowedTools"])
        # The lead's gate is the same hook as without the browser, and it guards the browser tools.
        self.assertEqual(call["settings"], render_settings())
        self.assertEqual(call["settings"]["hooks"]["PreToolUse"][0]["matcher"], "|".join(WEB_TOOLS))
        self.assertIn("--strict-mcp-config", argv)

    def test_crew_is_passed_as_sub_agents(self):
        self.e.sign_in()
        self.run_turn()
        call = self.e.calls()[0]
        argv = call["argv"]
        agents = json.loads(flag(argv, "--agents"))
        self.assertEqual(agents, crew.claude_agents("platform"))
        self.assertEqual(list(agents), ["byte", "patch", "scout"])
        # Scout: the built-in search and the two platform web tools (the browser is not turned on for this
        # user, so its tools are not in the list); the built-in file, edit and shell tools and page
        # fetching are denied as well.
        self.assertEqual(agents["scout"]["tools"], WEB_TOOLS[:3])
        self.assertIn("You have no browser here", agents["scout"]["prompt"])
        for t in ("Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Bash", "WebFetch"):
            self.assertIn(t, agents["scout"]["disallowedTools"])
        for a in agents.values():
            self.assertEqual(a["model"], "inherit")
            self.assertNotIn("Agent", a["tools"], "a specialist cannot start helpers")
            self.assertIn("Agent", a["disallowedTools"])
            self.assertFalse([t for t in a["tools"] if t.startswith("mcp__user_")], "platform tools only")
            self.assertNotIn("permissionMode", a, "specialists run in the lead's permission mode")
        # Only the lead can start a specialist, one level deep, at most 3 at once, no built-in agents.
        self.assertIn("Agent", flag(argv, "--allowedTools").split(","))
        self.assertEqual(call["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"], "1")
        self.assertEqual(call["env"]["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"], "1")
        self.assertEqual(call["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "3")
        self.assertIn("subagent_type byte, patch or scout", flag(argv, "--append-system-prompt"))
        # The rest of the command line is as before.
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(flag(argv, "--permission-mode"), "dontAsk")
        # None of Scout's tools is denied for the session: a session deny would take it from Scout too.
        # A specialist's calls are checked against the session's rules. So nothing Scout uses is denied for
        # the session, and its built-in search and the Platform MCP are allowed for it.
        self.assertFalse(set(flags(argv, "--disallowedTools")) & set(agents["scout"]["tools"]))
        for t in ("WebSearch", "mcp__platform"):
            self.assertIn(t, flag(argv, "--allowedTools").split(","))

    def test_the_lead_has_no_web_tool(self):
        # The fake CLI runs the PreToolUse hooks of --settings for every call, as the docs describe them.
        self.e.sign_in()
        self.e.mode("web")
        res = self.run_turn()
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.output.splitlines(), [
            # The lead's own calls to a web tool or a browser tool are refused; its other tools work.
            *[f"lead {tool}: refused: {REFUSAL}" for tool in WEB_TOOLS],
            "lead mcp__platform__get_profile: ok",
            # The same calls from inside Scout go through.
            *[f"scout {tool}: ok" for tool in WEB_TOOLS],
        ])
        self.assertEqual(len(res.output.splitlines()), 17 + 1 + 17)
        # Page fetching from the container is nobody's: denied for the whole session, lead and specialists.
        argv = self.e.calls()[0]["argv"]
        self.assertIn("WebFetch", flags(argv, "--disallowedTools"))
        self.assertNotIn("WebFetch", flag(argv, "--allowedTools").split(","))

    def test_crew_settings(self):
        self.e.sign_in()
        runner = ClaudeRunner([self.e.cli], self.e.env, self.e.data, self.e.reader().load, model="opus",
                              crew_model="haiku", crew_parallel=2)
        runner.run(text="x", instructions="i", session_key="s", idempotency_key="k", on_progress=lambda m: None,
                   timeout=20)
        call = self.e.calls()[0]
        self.assertEqual(flag(call["argv"], "--model"), "opus")
        self.assertEqual({a["model"] for a in call["agents"].values()}, {"haiku"})
        self.assertEqual(call["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "2")

    def test_crew_progress_lines(self):
        self.e.sign_in()
        self.e.mode("crew")
        res = self.run_turn()
        self.assertEqual(res.output, "the crew is done")
        self.assertEqual(self.progress, [
            "Scout: researching flight options",  # the lead starts Scout
            "using web_search",  # Scout's own tool call: the progress line it always was
            "Scout: done",
            "Byte: fixing the login bug",
            "using Agent",  # a sub-agent that is not a crew member gets no crew line
            "Byte: could not finish",
        ])

    def test_crew_progress_lines_for_a_background_specialist(self):
        self.e.sign_in()
        self.e.mode("crew_background")
        self.run_turn()
        # "done" comes with the task notification, not when the Agent call returns "launched".
        self.assertEqual(self.progress, ["Patch: reviewing pull request 12", "using Read", "Patch: done"])

    def test_every_specialist_that_started_gets_a_finish_line(self):
        self.e.sign_in()
        self.e.mode("crew_unreported")
        res = self.run_turn()
        self.assertEqual(res.status, "completed")
        # Scout's notification names it only by the id its launch answer gave. Byte's never comes:
        # the CLI waits for background sub-agents before it exits, so Byte ended with the run.
        self.assertEqual(self.progress, ["Scout: reading 4 pages", "Byte: fixing a typo", "Scout: done",
                                         "Byte: finished"])

    def test_a_specialist_is_stopped_with_a_run_that_times_out(self):
        self.e.sign_in()
        self.e.mode("crew_hang")
        with self.assertRaises(ClaudeError):
            self.run_turn(timeout=1)
        self.assertEqual(self.progress, ["Byte: a long job", "Byte: stopped"])

    def test_per_session_resume(self):
        self.e.sign_in()
        self.run_turn("one", "telegram:42")
        self.run_turn("two", "telegram:42")
        self.run_turn("other chat", "web")
        # A new runner (Bridge restart) resumes from the map in /data.
        again = ClaudeRunner([self.e.cli], self.e.env, self.e.data, self.e.reader().load)
        res = again.run(text="three", instructions="x", session_key="telegram:42", idempotency_key="k",
                        on_progress=lambda m: None, timeout=20)
        self.assertEqual(res.output, "reply 3 to: three")
        c = self.e.calls()
        first = flag(c[0]["argv"], "--session-id")
        # Each turn on an existing chat continues a copy of its session; the chat moves on to that copy.
        self.assertEqual(flag(c[1]["argv"], "--resume"), first)
        self.assertIn("--fork-session", c[1]["argv"])
        self.assertIsNone(flag(c[1]["argv"], "--session-id"))
        self.assertNotEqual(c[1]["session"], first)
        self.assertIsNotNone(flag(c[2]["argv"], "--session-id"))
        self.assertNotIn("--fork-session", c[2]["argv"])
        self.assertNotEqual(flag(c[2]["argv"], "--session-id"), first)
        self.assertEqual(flag(c[3]["argv"], "--resume"), c[1]["session"])
        self.assertEqual(c[3]["history"], ["one", "two"])

    def test_lost_session_starts_a_new_one(self):
        self.e.sign_in()
        self.run_turn("one")
        os.remove(os.path.join(self.e.cfg, "fake_sessions.json"))
        res = self.run_turn("two")
        self.assertEqual(res.status, "completed")
        c = self.e.calls()
        self.assertIsNotNone(flag(c[1]["argv"], "--resume"))
        self.assertIsNotNone(flag(c[2]["argv"], "--session-id"))

    def transcript(self, session_id):
        found = [os.path.join(d, f) for d, _, files in os.walk(os.path.join(self.e.cfg, "projects"))
                 for f in files if f == f"{session_id}.jsonl"]
        return found[0] if found else None

    def test_a_failed_turn_is_never_acted_on_later(self):
        # 2026-10-08: three messages failed on the plan's limit; the user was told so. 90 minutes later the
        # next message was answered with "Four requests arrived together", and the three were carried out.
        self.e.sign_in()
        self.run_turn("one")
        self.e.mode("limit")
        for failed in ("recall my colour", "forget my colour", "send the email to Bob"):
            res = self.run_turn(failed)
            self.assertEqual((res.status, res.reason, res.platform_fault), ("failed", "plan_limit", False))
        self.e.mode("")
        res = self.run_turn("what is my sister's name?")
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.output, "reply 2 to: what is my sister's name?")
        c = self.e.calls()
        first = c[0]["session"]
        # Every failed turn ran in its own copy of the chat's session; the chat stayed on the one from before.
        for call in c[1:]:
            self.assertEqual(flag(call["argv"], "--resume"), first)
            self.assertIn("--fork-session", call["argv"])
            self.assertEqual(call["history"], ["one"], "a failed message must never be continued")
        self.assertEqual(len({call["session"] for call in c}), 5)
        # The next turn goes on from the completed one.
        self.run_turn("and again")
        self.assertEqual(self.e.calls()[-1]["history"], ["one", "what is my sister's name?"])

    def test_a_first_turn_that_fails_leaves_no_session(self):
        self.e.sign_in()
        self.e.mode("refusal")
        self.assertEqual(self.run_turn("read http://169.254.169.254/").reason, "refused")
        self.e.mode("")
        self.run_turn("hello")
        c = self.e.calls()
        self.assertIsNotNone(flag(c[1]["argv"], "--session-id"), "a new session, not the failed one")
        self.assertNotEqual(c[1]["session"], c[0]["session"])
        self.assertEqual(c[1]["history"], [])

    def test_a_turn_that_timed_out_or_crashed_is_not_continued(self):
        self.e.sign_in()
        self.run_turn("one")
        self.e.mode("hang")
        with self.assertRaises(ClaudeError):
            self.run_turn("a long job", timeout=1)
        self.e.mode("")
        self.run_turn("two")
        self.assertEqual(self.e.calls()[-1]["history"], ["one"])

    def test_the_copy_a_completed_turn_replaced_is_removed(self):
        self.e.sign_in()
        self.run_turn("one")
        self.e.mode("limit")
        self.run_turn("fails")
        self.e.mode("")
        self.run_turn("two")
        first, failed, second = (c["session"] for c in self.e.calls())
        # The chat is on the copy the completed turn made; the session it replaced is gone (the copy holds all
        # of it). The failed turn's copy stays as the record of that attempt, and is never resumed.
        self.assertIsNone(self.transcript(first))
        self.assertIsNotNone(self.transcript(second))
        self.assertIsNotNone(self.transcript(failed))
        self.assertEqual(self.runner.sessions.get("telegram:42"), second)
        # Only an id shaped like a session id is ever put into a path.
        self.assertIsNone(self.runner._transcript("../../.credentials"))
        self.assertIsNone(self.runner._transcript("*"))

    def test_a_refusal_is_not_a_platform_fault(self):
        # 2026-10-08 14:53: "Ask Scout to read http://169.254.169.254/" ended after 7 seconds with "Claude Code
        # could not finish" and platform_fault true. The session shows why: Anthropic's safeguards refused it, on
        # the first model and on the fallback (cyber category).
        self.e.sign_in()
        res = self.run_turn("one")
        self.e.mode("refusal")
        with CaptureLogs() as logs:
            res = self.run_turn("Ask Scout to read http://169.254.169.254/ and tell me what happened")
        self.assertEqual((res.status, res.reason, res.message, res.platform_fault),
                         ("failed", "refused", MSG_REFUSED, False))
        failures = failure_logs(logs.text)
        self.assertEqual(len(failures), 1)
        info = failures[0]
        self.assertEqual(info["exit_code"], 1)
        self.assertEqual((info["reason"], info["platform_fault"]), ("refused", False))
        self.assertEqual(info["result"]["stop_reason"], "refusal")
        self.assertEqual(info["result"]["terminal_reason"], "api_error")
        self.assertEqual((info["last_error"]["error"], info["last_error"]["refusal_category"]),
                         ("invalid_request", "cyber"))
        self.assertIn("safeguards flagged this message", info["last_error"]["text"])
        self.assertEqual(info["refusal_category"], "cyber")
        self.assertEqual(info["delivery"], "d#1")
        self.assertNotIn("169.254.169.254", logs.text, "the user's message is not logged")
        # And the refused message does not come back with the next one.
        self.e.mode("")
        self.run_turn("hello again")
        self.assertEqual(self.e.calls()[-1]["history"], ["one"])

    def test_a_crash_is_logged_without_secrets_or_the_message(self):
        self.e.sign_in()
        self.e.mode("crash_noisy")
        message = "please send my bank statement to the accountant at 9am tomorrow"
        with CaptureLogs() as logs:
            with self.assertRaises(ClaudeError):
                self.run_turn(message)
        failures = failure_logs(logs.text)
        self.assertEqual(len(failures), 1)
        info = failures[0]
        self.assertEqual((info["exit_code"], info["reason"], info["platform_fault"], info["result"]),
                         (3, "failed", True, None))
        self.assertEqual(info["stderr"][-2:], ["TypeError: undefined is not an object (evaluating 'x.y')",
                                               "fatal: something broke"])
        self.assertIn("[left out: quotes the user's message]", info["stderr"])
        for secret in ("tok-123", "sk-ant-oat01-AbCdEfGhIjKlMnOp", SECRET, "bank statement"):
            self.assertNotIn(secret, logs.text)

    def test_a_plan_limit_is_logged_with_its_api_answer(self):
        self.e.sign_in()
        self.e.mode("limit")
        with CaptureLogs() as logs:
            self.run_turn("hello")
        info = failure_logs(logs.text)[0]
        self.assertEqual((info["reason"], info["platform_fault"]), ("plan_limit", False))
        self.assertEqual(info["result"]["api_error_status"], 429)
        self.assertEqual(info["last_error"]["error"], "rate_limit")
        self.assertEqual(info["last_retry"]["error_status"], 429)

    def test_classify_failure(self):
        # The CLI's `error` for a failed API call decides first; refusals and the user's plan or account are
        # never the platform's fault. What is left (overload, server errors, unknown) is.
        cases = {("", "authentication_failed", False): ("not_signed_in", False),
                 ("", "oauth_org_not_allowed", False): ("account", False),
                 ("", "account_on_hold", False): ("account", False),
                 ("", "billing_error", False): ("plan_limit", False),
                 ("API Error: Request rejected (429)", "rate_limit", False): ("plan_limit", False),
                 ("API Error: blocked", "invalid_request", True): ("refused", False),
                 ("API Error: 529 Overloaded", "overloaded", False): ("failed", True),
                 ("API Error: 500", "server_error", False): ("failed", True),
                 ("API Error: 400 bad request", "invalid_request", False): ("failed", True),
                 ("something", "", False): ("failed", True)}
        for (text, error, refused), want in cases.items():
            res = classify_failure(text, error=error, refused=refused)
            self.assertEqual((res.reason, res.platform_fault), want, (text, error, refused))
        self.assertEqual(classify_failure("x", subtype="error_max_turns", refused=True).reason, "max_turns")

    def test_the_redactor(self):
        r = Redactor(["tok-123", "k1", ""], "send the quarterly report to Jane Doe at noon please")
        self.assertEqual(r.line("Authorization: Bearer abc.def"), "Authorization: [secret]")
        self.assertEqual(r.line("used tok-123 for it"), "used [secret] for it")
        self.assertEqual(r.line("key=sk-ant-api03-xyzxyzxyzxyz"), "key=[secret]")
        self.assertEqual(r.line('{"access_token": "abc123", "x": 1}'), '{"access_token": "[secret]", "x": 1}')
        self.assertEqual(r.line("echo: quarterly report to Jane Doe at"), "[left out: quotes the user's message]")
        self.assertEqual(r.line("plain error line"), "plain error line")
        self.assertEqual(Redactor([], "send it").line("about to send it now"), "[left out: quotes the user's message]")
        self.assertEqual(len(r.line("x" * 1000)), 300)

    def test_not_signed_in(self):
        self.auth.set_connected(True)
        res = self.run_turn()
        self.assertEqual((res.status, res.reason, res.message, res.platform_fault),
                         ("failed", "not_signed_in", MSG_NOT_SIGNED_IN, False))
        self.assertFalse(self.auth.connected())

    def test_plan_limit(self):
        self.e.sign_in()
        self.e.mode("limit")
        res = self.run_turn()
        self.assertEqual(res.reason, "plan_limit")
        self.assertFalse(res.platform_fault)
        self.assertIn("usage limit", res.message)
        self.assertIn("session limit", res.message)
        self.assertTrue(any("retrying" in p for p in self.progress))

    def test_max_turns_is_not_platform_fault(self):
        self.e.sign_in()
        self.e.mode("max_turns")
        res = self.run_turn()
        self.assertEqual(res.reason, "max_turns")
        self.assertFalse(res.platform_fault)

    def test_crash_raises(self):
        self.e.sign_in()
        self.e.mode("crash")
        with self.assertRaises(ClaudeError):
            self.run_turn()

    def test_timeout_kills_and_raises(self):
        self.e.sign_in()
        self.e.mode("hang")
        start = time.monotonic()
        with self.assertRaises(ClaudeError):
            self.run_turn(timeout=1)
        self.assertLess(time.monotonic() - start, 10)

    def test_missing_cli(self):
        r = ClaudeRunner([os.path.join(self.e.bin, "nope")], self.e.env, self.e.data, self.e.reader().load)
        self.assertFalse(r.healthy())
        with self.assertRaises(ClaudeError):
            r.run(text="x", instructions="", session_key="s", idempotency_key="k", on_progress=print, timeout=5)


def hook_input(tool, tool_input=None, sub_agent=None):
    """The JSON Claude Code gives a `PreToolUse` command hook on stdin, as the hooks page documents it
    (code.claude.com/docs/en/hooks, "Common input fields" and "PreToolUse input"; read 2026-10-04).

    The common fields, then `tool_name`, `tool_input` and `tool_use_id`; `mcp_server` for an MCP tool.
    `agent_id` and `agent_type` are there only for a call from inside a sub-agent, so the lead's call
    (`sub_agent=None`) has neither key.
    """
    event = {"session_id": "abc123", "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
             "transcript_path": "/data/claude/projects/-data-workspace/abc123.jsonl", "cwd": "/data/workspace",
             "permission_mode": "dontAsk", "effort": {"level": "high"}, "hook_event_name": "PreToolUse",
             "tool_name": tool, "tool_input": {"query": "flights to Goa"} if tool_input is None else tool_input,
             "tool_use_id": "toolu_01ABC123"}
    if tool.startswith("mcp__"):
        event["mcp_server"] = {"name": tool.split("__")[1], "source": "user"}  # the gate does not read it
    if sub_agent:
        event.update(agent_id="agent-abc123", agent_type=sub_agent)
    return event


def matcher_names(matcher):
    """The tool names a hook matcher stands for, by the hooks page ("Matcher patterns"): a matcher made only
    of letters, digits, `_`, `-`, spaces, `,` and `|` is a list of exact names separated by `|` or `,`.
    Anything else would be a regular expression, which the Bridge never writes: None."""
    if not re.fullmatch(r"[A-Za-z0-9_\- ,|]+", matcher):
        return None
    return [name.strip() for name in re.split(r"[|,]", matcher)]


class LeadGateTest(unittest.TestCase):
    """bridge/lead_gate.py, the PreToolUse hook, run the way Claude Code runs it.

    From the hooks page (code.claude.com/docs/en/hooks, read 2026-10-04): a hook group runs for a tool call
    when its matcher names the tool; a command hook without `args` is passed to `sh -c`, with the hook input
    as JSON on stdin; exit code 2 "blocks the tool call" and "Claude sees the stderr message as the denial
    reason", whatever is on stdout; exit code 0 with nothing on stdout decides nothing, and the normal
    permission flow applies. These tests check the gate against that contract. They cannot show that the
    real CLI keeps to it: that needs a live turn.
    """

    GUARDED = WEB_TOOLS
    # Tools the lead keeps, and names that only look like a guarded one.
    OTHERS = ("Bash", "Read", "Agent", "TodoWrite", "mcp__platform__get_profile", "mcp__platform__run_app_action",
              "mcp__platform__request_approval", "mcp__user_notes__browser_navigate", "mcp__scout_browser",
              "mcp__browser__browser_navigate",
              "mcp__platform__browser_navigate", "mcp__scout_browser__browser_navigate_forward", "browser_navigate",
              "WebSearchPlus")

    def gate(self, stdin, guarded=None):
        """The gate run directly, as (exit code, stdout, stderr)."""
        done = subprocess.run([sys.executable, "-I", LEAD_GATE, *(self.GUARDED if guarded is None else guarded)],
                              input=stdin, capture_output=True, text=True, timeout=20)
        return done.returncode, done.stdout, done.stderr

    def hooks(self, event):
        """What the CLI does before one tool call: every `PreToolUse` hook of the settings file whose matcher
        names the tool is run through `sh -c` with the event on stdin. One (exit code, stdout, stderr) per
        hook that ran."""
        ran = []
        for group in render_settings()["hooks"]["PreToolUse"]:
            if event["tool_name"] not in matcher_names(group["matcher"]):
                continue
            for hook in group["hooks"]:
                self.assertEqual((hook["type"], "args" in hook), ("command", False), "shell form")
                done = subprocess.run(["sh", "-c", hook["command"]], input=json.dumps(event), capture_output=True,
                                      text=True, timeout=hook["timeout"])
                ran.append((done.returncode, done.stdout, done.stderr))
        return ran

    def test_the_settings_run_the_gate_for_scouts_web_tools_only(self):
        self.assertEqual(crew.claude_lead_gate("platform"), self.GUARDED)
        self.assertEqual(crew.claude_session_allowed(), ["WebSearch"])
        self.assertEqual(crew.claude_session_denied("platform"), ["mcp__platform__send_message", "WebFetch"])
        (group,) = render_settings()["hooks"]["PreToolUse"]
        # Only letters, digits, "_" and "|": Claude Code reads such a matcher as a list of exact tool names.
        self.assertEqual(group["matcher"],
                         "WebSearch|mcp__platform__web_search|mcp__platform__read_page|"
                         "mcp__scout_browser__browser_navigate|mcp__scout_browser__browser_navigate_back|"
                         "mcp__scout_browser__browser_snapshot|mcp__scout_browser__browser_find|mcp__scout_browser__browser_click|"
                         "mcp__scout_browser__browser_type|mcp__scout_browser__browser_fill_form|"
                         "mcp__scout_browser__browser_select_option|mcp__scout_browser__browser_press_key|"
                         "mcp__scout_browser__browser_hover|mcp__scout_browser__browser_wait_for|"
                         "mcp__scout_browser__browser_handle_dialog|mcp__scout_browser__browser_tabs|"
                         "mcp__scout_browser__browser_close")
        self.assertRegex(group["matcher"], r"^[A-Za-z0-9_|]+$")
        self.assertEqual(matcher_names(group["matcher"]), self.GUARDED)
        (hook,) = group["hooks"]
        self.assertEqual(set(hook), {"type", "command", "timeout"})
        self.assertEqual(hook["type"], "command")
        self.assertEqual(hook["timeout"], 10)
        # One shell command: this interpreter in isolated mode, the gate as a file, the tools it guards.
        self.assertTrue(os.path.isfile(LEAD_GATE))
        self.assertEqual(shlex.split(hook["command"]), [sys.executable, "-I", LEAD_GATE, *self.GUARDED])

    def test_the_gate_guards_what_scout_has_and_nothing_else(self):
        # The matcher, the names the gate is given and Scout's own tool list (with the browser turned on)
        # are the same seventeen names, so the lead is refused exactly the tools Scout is given.
        (group,) = render_settings()["hooks"]["PreToolUse"]
        command = shlex.split(group["hooks"][0]["command"])
        scout = crew.claude_agents("platform", browser=True)["scout"]["tools"]
        self.assertEqual(matcher_names(group["matcher"]), scout)
        self.assertEqual(command[3:], scout)
        self.assertEqual(len(scout), 17)
        self.assertEqual(scout[3:], BROWSER_TOOLS)
        self.assertEqual(scout[3:], [f"mcp__scout_browser__{name}" for name in crew.BROWSER_TOOLS])
        # Without the browser Scout has fewer tools; the gate still guards all of them.
        self.assertEqual(crew.claude_agents("platform")["scout"]["tools"], scout[:3])

    def test_the_leads_call_to_each_of_scouts_web_tools_is_refused(self):
        # The lead's call has no agent_id. Exit code 2 blocks the call; stderr is the reason the lead is
        # shown; stdout stays empty, so no JSON output competes with it.
        self.assertEqual((BLOCK, PASS), (2, 0))
        for tool in self.GUARDED:
            event = hook_input(tool)
            self.assertNotIn("agent_id", event)
            self.assertNotIn("agent_type", event)
            self.assertEqual(self.hooks(event), [(2, "", REFUSAL + "\n")], tool)
        for word in ("Scout", "subagent_type scout", "the web or the browser"):
            self.assertIn(word, REFUSAL)

    def test_a_sub_agents_call_to_each_of_scouts_web_tools_passes(self):
        # Inside a sub-agent the input carries agent_id and agent_type. Exit code 0 and no output: the gate
        # decides nothing, and the session's permission rules and the sub-agent's own tool list apply.
        for tool in self.GUARDED:
            event = hook_input(tool, sub_agent="scout")
            self.assertEqual((event["agent_id"], event["agent_type"]), ("agent-abc123", "scout"))
            self.assertEqual(self.hooks(event), [(0, "", "")], tool)

    def test_the_gate_does_not_decide_which_sub_agent(self):
        # The gate only tells the lead from a sub-agent. Byte and Patch are kept off these tools by their
        # own `disallowedTools`, not by the hook.
        self.assertEqual(self.hooks(hook_input("mcp__scout_browser__browser_navigate", sub_agent="byte")), [(0, "", "")])
        for member in ("byte", "patch"):
            denied = crew.claude_agents("platform")[member]["disallowedTools"]
            for tool in self.GUARDED:
                self.assertIn(tool, denied, member)

    def test_either_field_marks_a_sub_agent(self):
        # The Bridge never starts the session with --agent, so agent_type alone is a sub-agent too.
        for tool in ("WebSearch", "mcp__scout_browser__browser_close"):
            event = hook_input(tool)
            self.assertEqual(self.hooks({**event, "agent_id": "agent-1"}), [(0, "", "")])
            self.assertEqual(self.hooks({**event, "agent_type": "scout"}), [(0, "", "")])
            for empty in ("", None):
                (done,) = self.hooks({**event, "agent_id": empty, "agent_type": empty})
                self.assertEqual(done, (2, "", REFUSAL + "\n"), "an empty field is the lead")

    def test_other_tools_pass(self):
        for tool in self.OTHERS:
            # The matcher does not name the tool, so the CLI does not run the gate at all,
            self.assertEqual(self.hooks(hook_input(tool)), [], tool)
            self.assertEqual(self.hooks(hook_input(tool, sub_agent="byte")), [], tool)
            # and the gate itself lets it pass if it were run: the lead's call and a sub-agent's.
            self.assertEqual(self.gate(json.dumps(hook_input(tool))), (0, "", ""), tool)
            self.assertEqual(self.gate(json.dumps(hook_input(tool, sub_agent="byte"))), (0, "", ""), tool)

    def test_what_it_cannot_read_passes(self):
        # A broken hook must never take the web from Scout: nothing is decided, the session's rules apply.
        for stdin in ("", "not json", "[1, 2]", '"WebSearch"', "{}", '{"tool_name": 7}',
                      '{"tool_name": ["mcp__scout_browser__browser_navigate"]}'):
            self.assertEqual(self.gate(stdin), (0, "", ""), stdin)
        self.assertEqual(self.gate(json.dumps(hook_input("WebSearch")), guarded=[]), (0, "", ""))

    def test_it_runs_on_its_own(self):
        # Run as a file in isolated mode: only the standard library, nothing from the Bridge package.
        with open(LEAD_GATE, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("from .", source)
        self.assertNotIn("import bridge", source)
        imports = [line.split()[1] for line in source.splitlines() if line.startswith(("import ", "from "))]
        self.assertEqual(imports, ["json", "sys"])


class CrewEventsTest(unittest.TestCase):
    """stream-json events about sub-agents -> crew progress lines (no CLI needed)."""

    def setUp(self):
        self.lines = []
        self.events = CrewEvents(self.lines.append)

    def start(self, call_id, agent, parent=None, tool="Agent"):
        block = {"type": "tool_use", "id": call_id, "name": tool,
                 "input": {"subagent_type": agent, "description": f"job {call_id}"}}
        return self.events.started({"type": "assistant", "parent_tool_use_id": parent}, block)

    def test_only_the_leads_calls_to_crew_members_count(self):
        self.assertTrue(self.start("a1", "scout"))
        self.assertFalse(self.start("a2", "scout", parent="a1"), "a call from inside a specialist")
        self.assertFalse(self.start("a3", "Explore"), "not a crew member")
        self.assertFalse(self.start("a4", "byte", tool="Bash"), "not the Agent tool")
        self.assertEqual(self.lines, ["Scout: job a1"])

    def test_background_outcomes(self):
        for call_id, agent, status in (("a1", "scout", "failed"), ("a2", "byte", "stopped"), ("a3", "patch", "new")):
            self.start(call_id, agent)
            self.events.task({"subtype": "task_started", "task_id": "t" + call_id, "tool_use_id": call_id})
            self.events.result({"type": "user", "tool_use_result": {"status": "async_launched"}},
                               {"type": "tool_result", "tool_use_id": call_id})
            self.events.task({"subtype": "task_notification", "task_id": "t" + call_id, "status": status})
        self.assertEqual(self.lines[1::2], ["Scout: could not finish", "Byte: stopped", "Patch: finished"])

    def test_a_launch_that_fails_is_a_finish(self):
        self.start("a1", "byte")
        self.events.result({"type": "user", "tool_use_result": {"status": "async_launched"}},
                           {"type": "tool_result", "tool_use_id": "a1", "is_error": True})
        self.events.task({"subtype": "task_notification", "tool_use_id": "a1", "status": "completed"})
        self.assertEqual(self.lines, ["Byte: job a1", "Byte: could not finish"])

    def test_at_most_one_start_and_one_finish_line_per_run(self):
        self.start("a1", "scout")
        self.start("a1", "scout")  # the same call seen twice
        self.events.result({"type": "user", "tool_use_result": {"status": "completed"}},
                           {"type": "tool_result", "tool_use_id": "a1"})
        self.events.task({"subtype": "task_notification", "tool_use_id": "a1", "status": "completed"})
        self.events.close()
        self.assertEqual(self.lines, ["Scout: job a1", "Scout: done"])

    def test_close_finishes_what_is_still_open(self):
        self.start("a1", "scout")
        self.start("a2", "patch")
        self.events.close()
        self.events.close(stopped=True)
        self.assertEqual(self.lines, ["Scout: job a1", "Patch: job a2", "Scout: finished", "Patch: finished"])


def types(events):
    return [e["type"] for e in events]


class BridgeWithRuntimesTest(unittest.TestCase):
    """The Bridge core with the runtime switch: same NDJSON contract, dedupe and send_message path."""

    def setUp(self):
        self.e = Env(runtime="hermes")
        self.hermes = FakeRunner(output="hermes says hi")
        reader = self.e.reader()
        self.claude = ClaudeRunner([self.e.cli], self.e.env, self.e.data, reader.load)
        self.runtime = RuntimeRunner(reader.load, {"hermes": self.hermes, "claude_code": self.claude})
        self.sender = FakeSender()
        self.bridge = Bridge(SECRET, self.runtime, self.sender,
                             DeliveryStore(os.path.join(self.e.data, ".platform", "deliveries.json")), retry_delay=0)

    def deliver(self, did, text="hello"):
        return list(self.bridge.deliver(DeliverRequest.from_json(
            {"delivery_id": did, "kind": "message", "session_key": "telegram:42", "channel": "telegram",
             "text": text})))

    def test_runtime_switch_without_restart(self):
        self.e.sign_in()
        self.assertEqual(types(self.deliver("d1")), ["accepted", "working", "turn_done"])
        self.assertEqual(self.sender.sent[-1], ("hermes says hi", "telegram:42"))
        self.e.write_platform("claude_code")  # SetAgentRuntime rewrites platform.json
        self.assertEqual(types(self.deliver("d2", "hi claude")), ["accepted", "working", "turn_done"])
        self.assertEqual(self.sender.sent[-1], ("reply 1 to: hi claude", "telegram:42"))
        self.assertEqual(len(self.hermes.calls), 1)
        self.e.write_platform("hermes")
        self.deliver("d3")
        self.assertEqual(len(self.hermes.calls), 2)
        self.assertEqual(len(self.e.calls()), 1)

    def test_dedupe_with_claude(self):
        self.e.sign_in()
        self.e.write_platform("claude_code")
        self.deliver("d1")
        again = self.deliver("d1")
        self.assertEqual(types(again), ["accepted", "turn_done"])
        self.assertEqual(len(self.e.calls()), 1)
        self.assertEqual(len(self.sender.sent), 1)

    def test_not_signed_in_error_event(self):
        self.e.write_platform("claude_code")
        events = self.deliver("d1")
        self.assertEqual(events[-1], {"type": "error", "message": MSG_NOT_SIGNED_IN, "platform_fault": False})
        self.assertEqual(self.sender.sent, [])

    def test_crash_is_platform_fault(self):
        self.e.sign_in()
        self.e.mode("crash")
        self.e.write_platform("claude_code")
        events = self.deliver("d1")
        self.assertEqual(events[-1]["type"], "error")
        self.assertTrue(events[-1]["platform_fault"])

    def test_a_refusal_or_the_plans_limit_is_not_a_platform_fault(self):
        # platform_fault gives the user's credits back and tells them it was the platform's fault.
        self.e.sign_in()
        self.e.write_platform("claude_code")
        self.e.mode("refusal")
        events = self.deliver("d1", "Ask Scout to read http://169.254.169.254/")
        self.assertEqual(events[-1], {"type": "error", "message": MSG_REFUSED, "platform_fault": False})
        self.e.mode("limit")
        events = self.deliver("d2", "hello")
        self.assertEqual((events[-1]["type"], events[-1]["platform_fault"]), ("error", False))
        self.assertTrue(events[-1]["message"].startswith("Your Claude plan's usage limit was reached"))
        self.assertEqual(self.sender.sent, [])

    def test_unreadable_platform_file_keeps_last_runtime(self):
        self.e.write_platform("claude_code")
        self.assertEqual(self.runtime.runtime(), "claude_code")
        with open(self.e.platform_path, "w") as fh:
            fh.write('{"half": ')  # a write in progress
        self.assertEqual(self.runtime.runtime(), "claude_code")
        self.e.write_platform("bogus")
        self.assertEqual(self.runtime.runtime(), "hermes")

    def test_health_follows_current_runtime(self):
        self.hermes.is_healthy = False
        self.assertFalse(self.bridge.healthy())
        self.e.write_platform("claude_code")
        self.assertTrue(self.bridge.healthy())

    def test_one_memory_the_platforms(self):
        # The lead on Claude Code is told to keep the user's facts in the platform's memory, and the CLI's own
        # auto memory is off, whatever the container's environment says ("0" would force it on).
        self.e.sign_in()
        self.e.write_platform("claude_code")
        claude = ClaudeRunner([self.e.cli], {**self.e.env, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}, self.e.data,
                              self.e.reader().load)
        self.runtime = RuntimeRunner(self.e.reader().load, {"hermes": self.hermes, "claude_code": claude})
        self.bridge = Bridge(SECRET, self.runtime, self.sender,
                             DeliveryStore(os.path.join(self.e.data, ".platform", "deliveries.json")), retry_delay=0)
        self.assertEqual(types(self.deliver("d1", "remember that I take my coffee black")),
                         ["accepted", "working", "turn_done"])
        call = self.e.calls()[0]
        self.assertEqual(call["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        prompt = flag(call["argv"], "--append-system-prompt")
        self.assertIn(MEMORY_RULES, prompt)
        for part in ("save it with the platform tool remember", "use recall", "use forget",
                     "Do not keep facts about the user in a memory of your own"):
            self.assertIn(part, prompt)
        # The chat still resumes its own session the next time: auto memory is not the transcript.
        self.deliver("d2", "and what do I drink?")
        self.assertEqual(flag(self.e.calls()[1]["argv"], "--resume"), flag(call["argv"], "--session-id"))


class ClaudeAuthTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.auth = ClaudeAuth([self.e.cli], self.e.env, url_timeout=10, complete_timeout=10)

    def tearDown(self):
        self.auth.shutdown()

    def test_login_start_and_complete(self):
        with CaptureLogs() as logs:
            start = self.auth.start_login()
            self.assertTrue(start["authorize_url"].startswith("https://claude.ai/oauth/authorize?code=true"))
            self.assertIn("platform.claude.com", start["authorize_url"], "the code-paste URL, not localhost")
            self.assertAlmostEqual(start["expires_at"] / 1000, time.time() + 600, delta=30)
            self.assertTrue(self.auth.login_pending())
            res = self.auth.complete_login(start["login_id"], GOOD)
        self.assertEqual(res, {"connected": True, "message": "Claude plan connected"})
        self.assertTrue(self.auth.connected())
        self.assertFalse(self.auth.login_pending())
        self.assertTrue(os.path.exists(os.path.join(self.e.cfg, ".credentials.json")))
        self.assertNotIn(GOOD, logs.text)
        self.assertNotIn("fake-token-never-read", logs.text)
        with self.assertRaises(LoginNotFound):
            self.auth.complete_login(start["login_id"], GOOD)  # one attempt per sign-in

    def test_wrong_code(self):
        start = self.auth.start_login()
        with CaptureLogs() as logs:
            res = self.auth.complete_login(start["login_id"], "wrong-code-999")
        self.assertFalse(res["connected"])
        self.assertIn("start again", res["message"])
        self.assertNotIn("wrong-code-999", res["message"] + logs.text)
        self.assertFalse(self.auth.login_pending())

    def test_unknown_and_expired(self):
        with self.assertRaises(LoginNotFound):
            self.auth.complete_login("nope", GOOD)
        short = ClaudeAuth([self.e.cli], self.e.env, login_ttl=0.5, url_timeout=10)
        start = short.start_login()
        time.sleep(1.0)
        self.assertFalse(short.login_pending())
        with self.assertRaises(LoginNotFound):
            short.complete_login(start["login_id"], GOOD)

    def test_complete_timeout(self):
        self.e.mode("login_hang")
        auth = ClaudeAuth([self.e.cli], self.e.env, url_timeout=10, complete_timeout=1)
        start = auth.start_login()
        t = time.monotonic()
        res = auth.complete_login(start["login_id"], GOOD)
        self.assertFalse(res["connected"])
        self.assertLess(time.monotonic() - t, 10)

    def test_no_url_is_an_error(self):
        from bridge.claude import LoginError
        self.e.mode("login_no_url")
        auth = ClaudeAuth([self.e.cli], self.e.env, url_timeout=0.5)
        with self.assertRaises(LoginError):
            auth.start_login()
        self.assertFalse(auth.login_pending())

    def test_new_start_replaces_pending(self):
        first = self.auth.start_login()
        second = self.auth.start_login()
        self.assertNotEqual(first["login_id"], second["login_id"])
        with self.assertRaises(LoginNotFound):
            self.auth.complete_login(first["login_id"], GOOD)
        self.assertTrue(self.auth.complete_login(second["login_id"], GOOD)["connected"])

    def test_logout(self):
        self.e.sign_in()
        self.assertTrue(self.auth.refresh())
        self.auth.logout()
        self.assertFalse(self.auth.connected())
        self.assertFalse(os.path.exists(os.path.join(self.e.cfg, ".credentials.json")))

    def test_status_unknown_until_the_cli_answers(self):
        auth = ClaudeAuth([self.e.cli], self.e.env)
        self.e.sign_in()
        first = auth.status()  # starts a background `claude auth status`
        self.assertIsNone(first, "unknown is reported as null, not false")
        deadline = time.monotonic() + 10
        while auth.status() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(auth.status())

    def test_status_uses_the_cli(self):
        self.assertFalse(self.auth.refresh())
        self.e.sign_in()
        self.assertTrue(self.auth.refresh())


class AuthorizeURLTest(unittest.TestCase):
    def test_prefers_code_paste_flow_and_known_hosts(self):
        buf = (b"\x1b]8;;https://claude.ai/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1%2Fcb\x07x\x1b]8;;\x07"
               b" see https://evil.example.com/oauth/authorize?x=1 and \x1b[36mhttps://claude.com/cai/oauth/authorize"
               b"?redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback\x1b[0m\r\n")
        self.assertEqual(find_authorize_url(buf),
                         "https://claude.com/cai/oauth/authorize?redirect_uri="
                         "https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback")
        self.assertEqual(find_authorize_url(b"https://claude.ai/oauth/authorize?redirect_uri=http://localhost:1"),
                         "https://claude.ai/oauth/authorize?redirect_uri=http://localhost:1")
        self.assertIsNone(find_authorize_url(b"https://evil.example.com/oauth/authorize"))
        self.assertIsNone(find_authorize_url(b"nothing here"))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.auth = ClaudeAuth([self.e.cli], self.e.env, url_timeout=10, complete_timeout=10)
        self.bridge = Bridge(SECRET, FakeRunner(), FakeSender(), DeliveryStore(None), retry_delay=0)
        self.server = BridgeHTTPServer(("127.0.0.1", 0), self.bridge, lambda: None, claude=self.auth,
                                       health_extra=lambda: {"runtime": "claude_code",
                                                             "claude_connected": self.auth.connected(),
                                                             "claude_login_pending": self.auth.login_pending()})
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.auth.shutdown()
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, secret=SECRET):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if secret:
            r.add_header("X-Bridge-Secret", secret)
        try:
            resp = self.opener.open(r, timeout=30)
            return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def test_sign_in_over_http(self):
        self.assertEqual(self.call("POST", "/claude/login/start", {}, secret="wrong")[0], 401)
        self.assertEqual(self.call("POST", "/claude/logout", {}, secret=None)[0], 401)
        with CaptureLogs() as logs:
            code, start = self.call("POST", "/claude/login/start", {})
            self.assertEqual(code, 200)
            self.assertEqual(set(start), {"login_id", "authorize_url", "expires_at"})
            _, health = self.call("GET", "/healthz", secret=None)
            self.assertTrue(health["claude_login_pending"])
            self.assertEqual(self.call("POST", "/claude/login/complete", {"login_id": "nope", "code": GOOD})[0], 409)
            self.assertEqual(self.call("POST", "/claude/login/complete",
                                       {"login_id": start["login_id"], "code": "a\nb"})[0], 400)
            self.assertEqual(self.call("POST", "/claude/login/complete", {"login_id": start["login_id"]})[0], 400)
            code, res = self.call("POST", "/claude/login/complete", {"login_id": start["login_id"], "code": GOOD})
            self.assertEqual((code, res), (200, {"connected": True, "message": "Claude plan connected"}))
            _, health = self.call("GET", "/healthz", secret=None)
            self.assertEqual((health["claude_connected"], health["claude_login_pending"], health["runtime"]),
                             (True, False, "claude_code"))
            code, res = self.call("POST", "/claude/logout", {})
            self.assertEqual((code, res), (200, {"ok": True, "claude_connected": False}))
        self.assertNotIn(GOOD, logs.text)

    def test_not_installed(self):
        os.remove(self.e.cli)
        self.assertEqual(self.call("POST", "/claude/login/start", {})[0], 503)


if __name__ == "__main__":
    unittest.main()
