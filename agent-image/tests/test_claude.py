"""Claude Code runtime tests against a fake `claude` executable (tests/fake_claude.py)."""

import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from bridge import crew
from bridge.claude import (MSG_NOT_SIGNED_IN, ClaudeAuth, ClaudeError, ClaudeRunner, CrewEvents, LoginNotFound,
                           find_authorize_url)
from bridge.core import Bridge, DeliverRequest, DeliveryStore
from bridge.platform import load_platform_file
from bridge.runtime import PlatformReader, RuntimeRunner
from bridge.server import BridgeHTTPServer

from tests.fakes import FakeRunner, FakeSender

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET = "s3cret-bridge"
GOOD = "good-code-123"


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
        self.env = {"PATH": self.bin + os.pathsep + os.environ.get("PATH", ""), "DATA_DIR": self.data,
                    "HOME": os.path.join(self.data, "home"), "CLAUDE_CONFIG_DIR": self.cfg,
                    "PLATFORM_MCP_URL": "http://api:8080/mcp", "HTTPS_PROXY": "http://proxy:3128",
                    "ANTHROPIC_API_KEY": "sk-platform-must-not-leak"}
        os.makedirs(self.env["HOME"])

    def write_platform(self, runtime):
        with open(self.platform_path, "w") as fh:
            json.dump({"version": 1, "user_id": "u1", "mcp_token": "tok-123", "bridge_secret": SECRET,
                       "litellm_key": "sk-1", "runtime": runtime,
                       "mcp_servers": [{"id": "Notes", "name": "notes", "url": "https://notes.example.org/mcp",
                                        "auth_header_name": "X-Api-Key", "auth_secret": "k1"}]}, fh)

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


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


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
        self.assertIn("using web_search", self.progress)
        call = self.e.calls()[0]
        argv = call["argv"]
        for a in ("-p", "--verbose", "--strict-mcp-config"):
            self.assertIn(a, argv)
        self.assertEqual(flag(argv, "--output-format"), "stream-json")
        self.assertEqual(flag(argv, "--permission-mode"), "dontAsk")
        self.assertEqual(flag(argv, "--permission-prompts"), "none")
        self.assertEqual(flag(argv, "--disallowedTools"), "mcp__platform__send_message")
        allowed = flag(argv, "--allowedTools").split(",")
        for t in ("Bash", "Read", "Edit", "Write", "mcp__platform", "mcp__user_notes"):
            self.assertIn(t, allowed)
        self.assertIn("telegram", flag(argv, "--append-system-prompt"))
        self.assertIsNotNone(flag(argv, "--session-id"))
        self.assertNotIn("what's up?", " ".join(argv), "the prompt goes on stdin, not argv")
        self.assertEqual(call["prompt"], "what's up?")
        self.assertEqual(call["cwd"], os.path.join(self.e.data, "workspace"))
        self.assertIsNone(call["env"]["ANTHROPIC_API_KEY"], "platform keys must never reach the CLI")
        self.assertEqual(call["env"]["CLAUDE_CONFIG_DIR"], self.e.cfg)
        self.assertEqual(call["env"]["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(call["env"]["HTTPS_PROXY"], "http://proxy:3128")
        # MCP config: Platform MCP with the container token + the user's own server; private file.
        servers = call["mcp"]["mcpServers"]
        self.assertEqual(servers["platform"], {"type": "http", "url": "http://api:8080/mcp",
                                               "headers": {"Authorization": "Bearer tok-123"}})
        self.assertEqual(servers["user_notes"]["headers"], {"X-Api-Key": "k1"})
        self.assertEqual(call["mcp_mode"], oct(0o600))
        self.assertTrue(self.auth.connected())

    def test_crew_is_passed_as_sub_agents(self):
        self.e.sign_in()
        self.run_turn()
        call = self.e.calls()[0]
        argv = call["argv"]
        agents = json.loads(flag(argv, "--agents"))
        self.assertEqual(agents, crew.claude_agents("platform"))
        self.assertEqual(list(agents), ["byte", "patch", "scout"])
        self.assertEqual(agents["scout"]["tools"],
                         ["Read", "Glob", "Grep", "mcp__platform__web_search", "mcp__platform__read_page"])
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
        self.assertEqual(flag(argv, "--disallowedTools"), "mcp__platform__send_message")

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
        self.assertEqual(flag(c[1]["argv"], "--resume"), first)
        self.assertIsNone(flag(c[1]["argv"], "--session-id"))
        self.assertIsNotNone(flag(c[2]["argv"], "--session-id"))
        self.assertNotEqual(flag(c[2]["argv"], "--session-id"), first)
        self.assertEqual(flag(c[3]["argv"], "--resume"), first)

    def test_lost_session_starts_a_new_one(self):
        self.e.sign_in()
        self.run_turn("one")
        os.remove(os.path.join(self.e.cfg, "fake_sessions.json"))
        res = self.run_turn("two")
        self.assertEqual(res.status, "completed")
        c = self.e.calls()
        self.assertIsNotNone(flag(c[1]["argv"], "--resume"))
        self.assertIsNotNone(flag(c[2]["argv"], "--session-id"))

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
