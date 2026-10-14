"""Waits the Bridge no longer has: one MCP initialise per process with the platform's stateless server, the
second quiet check of the Hermes crew soon after the first, and the Claude Code config files written only when
they change."""

import json
import os
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler

from bridge.claude import ClaudeAuth, ClaudeRunner
from bridge.hermes import QUIET_RECHECK, HermesClient
from bridge.mcp_client import PlatformMCP
from tests.test_claude import Env
from tests.test_clients import FakeHermes, frames, serve


class StatelessMCP(BaseHTTPRequestHandler):
    """Like the platform's /mcp: no Mcp-Session-Id, and every request is answered on its own. With `expire`,
    the server is session-based instead and ends the session after its first tool call."""

    protocol_version = "HTTP/1.0"
    calls: list = []
    expire = False
    sessions = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls = StatelessMCP
        cls.calls.append(body.get("method"))
        method = body.get("method")
        session = self.headers.get("Mcp-Session-Id")
        if cls.expire and method == "tools/call" and session != f"s{cls.sessions}":
            self.send_response(404)  # that session ended
            self.end_headers()
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "t"}} \
            if method == "initialize" else {"content": [{"type": "text", "text": "queued"}], "isError": False}
        self.send_response(200)
        if cls.expire and method == "initialize":
            cls.sessions += 1
            self.send_header("Mcp-Session-Id", f"s{cls.sessions}")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}).encode())
        if cls.expire and method == "tools/call":
            cls.sessions += 1  # the session ends after one call


class PlatformMCPRoundTripsTest(unittest.TestCase):
    def setUp(self):
        StatelessMCP.calls, StatelessMCP.expire, StatelessMCP.sessions = [], False, 0
        self.srv, self.url = serve(StatelessMCP)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def client(self):
        c = PlatformMCP(self.url + "/mcp", "tok")
        c._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return c

    def test_a_stateless_server_is_initialised_once(self):
        c = self.client()
        c.send_message("one", "web")
        c.send_message("two", "web")
        c.send_message("three", "web")
        self.assertEqual(StatelessMCP.calls, ["initialize", "notifications/initialized",
                                              "tools/call", "tools/call", "tools/call"])

    def test_a_session_that_ended_is_initialised_again(self):
        StatelessMCP.expire = True
        c = self.client()
        c.send_message("one", "web")
        c.send_message("two", "web")
        self.assertEqual(StatelessMCP.calls, ["initialize", "notifications/initialized", "tools/call",
                                              "tools/call", "initialize", "notifications/initialized",
                                              "tools/call"])


class HermesCrewWaitTest(unittest.TestCase):
    def setUp(self):
        FakeHermes.posted = []
        FakeHermes.health_calls = 0
        self.srv, self.url = serve(FakeHermes)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_the_second_quiet_check_comes_soon_after_the_first(self):
        FakeHermes.runs = [
            frames({"event": "tool.started", "tool": "delegate_task"},
                   {"event": "subagent.start", "subagent_id": "sa-1", "goal": "Scout: compare flights"},
                   {"event": "tool.completed", "tool": "delegate_task",
                    "preview": json.dumps({"status": "dispatched", "mode": "background", "count": 1})},
                   {"event": "run.completed", "output": "Scout is on it."}),
            frames({"event": "run.completed", "output": "Here are the flights."}),
        ]
        FakeHermes.health = [0]  # the specialist is already done when the client first asks
        started = time.monotonic()
        res = HermesClient(self.url, "k", poll_interval=3.0).run("flights?", "be brief", "web", "d#1",
                                                                 lambda _: None, timeout=20)
        took = time.monotonic() - started
        self.assertEqual(res.output, "Here are the flights.")
        self.assertEqual(FakeHermes.health_calls, 2, "two quiet answers in a row")
        self.assertLess(took, 3.0, "not a whole poll interval between the two quiet checks")
        self.assertGreaterEqual(took, QUIET_RECHECK)


class ClaudeConfigFilesTest(unittest.TestCase):
    def test_written_when_they_change_not_at_every_turn(self):
        e = Env()
        e.sign_in()
        runner = ClaudeRunner([e.cli], e.env, e.data, e.reader().load, auth=ClaudeAuth([e.cli], e.env))

        def turn():
            res = runner.run(text="hi", instructions="hi", session_key="web", idempotency_key="d#1",
                             on_progress=lambda _: None, timeout=20)
            self.assertEqual(res.status, "completed")

        def stamp(path):
            st = os.stat(path)
            return st.st_ino, st.st_mtime_ns

        turn()
        mcp, settings = runner.mcp_path, runner.settings_path
        first = (stamp(mcp), stamp(settings))
        turn()
        self.assertEqual((stamp(mcp), stamp(settings)), first, "the same content: not written again")
        # The user's own MCP servers changed (platform.json): the tool-server file follows at the next turn.
        with open(e.platform_path) as fh:
            body = json.load(fh)
        body["mcp_servers"] = []
        with open(e.platform_path, "w") as fh:
            json.dump(body, fh)
        turn()
        self.assertNotEqual(stamp(mcp), first[0])
        with open(mcp) as fh:
            self.assertEqual(list(json.load(fh)["mcpServers"]), ["platform"])
        self.assertEqual(oct(os.stat(mcp).st_mode & 0o777), oct(0o600))
        self.assertEqual([c["mcp"] is not None for c in e.calls()], [True, True, True], "the CLI read it each time")


if __name__ == "__main__":
    unittest.main()
