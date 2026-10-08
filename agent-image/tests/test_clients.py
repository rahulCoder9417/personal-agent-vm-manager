"""HTTP-level tests for the Platform MCP client and the Hermes Runs API client."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import crew
from bridge.core import MEMORY_RULES, DeliverRequest, build_instructions
from bridge.hermes import CREW_FOLLOW_UP, MAX_CREW_ROUNDS, HermesClient, HermesError, HermesProcess
from bridge.mcp_client import MCPError, PlatformMCP


class FakeMCP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    calls: list = []
    sse = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeMCP.calls.append((dict(self.headers), body))
        if self.headers.get("Authorization") != "Bearer tok":
            self.send_response(401)
            self.end_headers()
            return
        method = body.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "t"}}
        elif method == "tools/call":
            if self.headers.get("Mcp-Session-Id") != "sess-1":
                self.send_response(400)
                self.end_headers()
                return
            result = {"content": [{"type": "text", "text": "queued"}], "isError": False}
        else:
            result = {}
        msg = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "sess-1")
        if FakeMCP.sse and method == "tools/call":
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(f"event: message\ndata: {msg}\n\n".encode())
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg.encode())


DEFAULT_RUN = [
    ": open",
    'id: 1\ndata: {"event": "tool.started", "tool": "web_search", "preview": "x"}',
    'id: 2\ndata: {"event": "approval.request", "request_id": "r1"}',
    'id: 3\ndata: {"event": "run.completed", "output": "All done."}',
]


def frames(*events):
    """SSE frames for a run: one per event dict."""
    return [f"id: {i}\ndata: {json.dumps(ev)}" for i, ev in enumerate(events, 1)]


class FakeHermes(BaseHTTPRequestHandler):
    """Hermes's local API: POST /v1/runs starts run_1, run_2, ... (their events come from `runs`,
    in order); GET /health/detailed reports the background delegations from `health` (the last
    value repeats; None = an answer without that field)."""

    protocol_version = "HTTP/1.0"
    posted: list = []
    runs: list = [DEFAULT_RUN]
    health: list = [0]
    health_calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = FakeHermes
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        if self.path == "/health/detailed":
            active = cls.health[min(cls.health_calls, len(cls.health) - 1)]
            cls.health_calls += 1
            queues = {"status": "ok"} if active is None else {"status": "ok", "active_api_runs": 0,
                                                              "process_completions": 0, "active_delegations": active}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "readiness": {"checks": {"background_queues": queues}}})
                             .encode())
            return
        if self.path.startswith("/v1/runs/run_") and self.path.endswith("/events"):
            n = int(self.path.split("/")[3].split("_")[1])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for f in cls.runs[n - 1]:
                self.wfile.write((f + "\n\n").encode())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeHermes.posted.append((self.path, dict(self.headers), body))
        run_id = "run_%d" % sum(1 for p, _, _ in FakeHermes.posted if p == "/v1/runs")
        self.send_response(202 if self.path == "/v1/runs" else 200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"run_id": run_id, "status": "started"}).encode())


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class MCPClientTest(unittest.TestCase):
    def setUp(self):
        FakeMCP.calls = []
        self.srv, self.url = serve(FakeMCP)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _client(self, token="tok"):
        c = PlatformMCP(self.url + "/mcp", token)
        c._opener = __import__("urllib.request").request.build_opener(
            __import__("urllib.request").request.ProxyHandler({}))
        return c

    def test_send_message_json(self):
        res = self._client().send_message("hello there", "telegram:42")
        self.assertEqual(res["content"][0]["text"], "queued")
        methods = [b["method"] for _, b in FakeMCP.calls]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"])
        call = FakeMCP.calls[-1][1]
        self.assertEqual(call["params"], {"name": "send_message",
                                          "arguments": {"text": "hello there", "session_key": "telegram:42"}})

    def test_send_message_sse(self):
        FakeMCP.sse = True
        try:
            res = self._client().send_message("hi", "web")
        finally:
            FakeMCP.sse = False
        self.assertFalse(res["isError"])

    def test_bad_token(self):
        with self.assertRaises(MCPError):
            self._client("nope").send_message("hi", "web")


class HermesClientTest(unittest.TestCase):
    def setUp(self):
        FakeHermes.posted = []
        FakeHermes.runs = [DEFAULT_RUN]
        FakeHermes.health = [0]
        FakeHermes.health_calls = 0
        self.srv, self.url = serve(FakeHermes)
        self.progress = []

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def run_turn(self, timeout=10):
        c = HermesClient(self.url, "local-key", poll_interval=0.01)
        return c.run("hello", "be brief", "telegram:42", "evt-1#1", self.progress.append, timeout=timeout)

    def run_posts(self):
        return [(headers, body) for path, headers, body in FakeHermes.posted if path == "/v1/runs"]

    def test_run_to_completion(self):
        c = HermesClient(self.url, "local-key")
        self.assertTrue(c.healthy())
        res = c.run("hello", "be brief", "telegram:42", "evt-1#1", self.progress.append, timeout=10)
        self.assertEqual((res.status, res.output), ("completed", "All done."))
        self.assertEqual(self.progress, ["using web_search"])
        path, headers, body = FakeHermes.posted[0]
        self.assertEqual(path, "/v1/runs")
        self.assertEqual(headers["Authorization"], "Bearer local-key")
        self.assertEqual(headers["X-Hermes-Session-Key"], "telegram:42")
        self.assertEqual(headers["Idempotency-Key"], "evt-1#1")
        self.assertEqual(body["input"], "hello")
        # The lead's instructions, then how to hand work to the crew with delegate_task.
        self.assertTrue(body["instructions"].startswith("be brief\n\n"))
        self.assertTrue(body["instructions"].endswith(crew.hermes_handoff()))
        # Hermes's own approval prompt is resolved by policy (deny by default).
        self.assertEqual(FakeHermes.posted[1][0], "/v1/runs/run_1/approval")
        self.assertEqual(FakeHermes.posted[1][2], {"choice": "deny", "all": True})
        self.assertEqual(len(self.run_posts()), 1, "no follow-up turn without a hand-over")

    def test_the_hermes_lead_is_told_the_web_is_scouts(self):
        # Hermes cannot narrow tools per agent, so there the web rule is carried by the instructions only:
        # the lead's own part, and the briefs it copies into each task.
        c = HermesClient(self.url, "local-key")
        c.run("hello", build_instructions(DeliverRequest.from_json(
            {"delivery_id": "d1", "kind": "message", "channel": "telegram", "text": "hello"})),
            "telegram:42", "evt-1#1", self.progress.append, timeout=10)
        sent = self.run_posts()[0][1]["instructions"]
        for part in ("You have no access to the web yourself, and neither have Byte and Patch",
                     "Anything that needs the web, even a single lookup, goes to Scout",
                     "Never use a web search or page-reading tool yourself (web_search, read_page or a built-in "
                     "one), even when one is available to you",
                     "Use the platform tool web_search only when you have no built-in search"):
            self.assertIn(part, sent)
        self.assertEqual(sent.count("You have no web search, no page reading and no browser"), 2,
                         "Byte's and Patch's briefs")
        # One memory, the platform's: the Hermes lead is told so too (its own memory tool is off in config.yaml).
        self.assertIn(MEMORY_RULES, sent)

    def test_the_hermes_lead_is_told_the_browser_is_scouts(self):
        # The same for the browser: on Hermes the browser tools are in everyone's list, so the lead's
        # instructions and the three briefs are all that says who drives it and how.
        instructions = build_instructions(DeliverRequest.from_json(
            {"delivery_id": "d1", "kind": "message", "channel": "telegram", "text": "hello"}))
        lead = ("The browser is Scout's too: a job that needs a website to be opened, clicked through, filled in "
                "or scraped goes to Scout",
                "Never use a browser tool yourself, even when one is available to you",
                "signing in to a site is not available yet")
        FakeHermes.runs = [DEFAULT_RUN, DEFAULT_RUN]
        # The browser is turned on for this user: Scout's brief says how to use it, and to close it.
        c = HermesClient(self.url, "local-key", with_browser=True)
        c.run("hello", instructions, "telegram:42", "evt-1#1", self.progress.append, timeout=10)
        sent = self.run_posts()[0][1]["instructions"]
        for part in (*lead,
                     # Scout's brief.
                     "Use the cheapest tool that does the job, in this order: search, then read_page for a plain "
                     "page, then the browser",
                     "call browser_close as soon as you are done, also when the job failed",
                     "Never type a password, a one-time code, a card number or any other secret into a page",
                     "Never enter anything private about the user into a page, unless the task explicitly gives "
                     "it to you for that purpose",
                     # Byte's and Patch's briefs.
                     "do not use web_search, read_page, a browser tool or any other web tool"):
            self.assertIn(part, sent)
        self.assertNotIn("You have no browser here", sent)
        # Not turned on (the default): the lead is told the same, and Scout that it has no browser.
        c = HermesClient(self.url, "local-key")
        c.run("hello", instructions, "telegram:42", "evt-2#1", self.progress.append, timeout=10)
        sent = self.run_posts()[1][1]["instructions"]
        for part in (*lead, "You have no browser here: it is part of the Max plan and is not turned on for this user"):
            self.assertIn(part, sent)
        self.assertNotIn("browser_close", sent)

    def test_crew_in_the_background_gets_a_follow_up_turn(self):
        # Hermes runs the lead's specialists in the background: the first run ends with a status, the
        # reports land in the session later, and the client starts the next turn.
        FakeHermes.runs = [
            frames({"event": "tool.started", "tool": "delegate_task", "preview": "2 tasks"},
                   {"event": "subagent.start", "subagent_id": "sa-1", "task_index": 0, "task_count": 2,
                    "goal": "Scout: compare flight options to Goa", "preview": "Scout: compare flight options to Goa"},
                   {"event": "subagent.start", "subagent_id": "sa-2", "task_index": 1, "task_count": 2,
                    "goal": "Byte: fix the login bug"},
                   {"event": "tool.completed", "tool": "delegate_task", "duration": 0.1, "error": False,
                    "preview": json.dumps({"status": "dispatched", "mode": "background", "count": 2})},
                   {"event": "subagent.complete", "subagent_id": "sa-2", "status": "failed", "summary": "no repo"},
                   {"event": "run.completed", "output": "Scout and Byte are on it."}),
            frames({"event": "run.completed", "output": "Here is the answer."}),
        ]
        FakeHermes.health = [2, 1, 0, 0]
        res = self.run_turn()
        self.assertEqual((res.status, res.output), ("completed", "Here is the answer."))
        self.assertEqual(self.progress, ["using delegate_task", "Scout: compare flight options to Goa",
                                         "Byte: fix the login bug", "Byte: could not finish", "Scout: finished"])
        self.assertEqual(FakeHermes.health_calls, 4, "waited until Hermes reported no background work, twice")
        (h1, b1), (h2, b2) = self.run_posts()
        self.assertEqual(b2["input"], CREW_FOLLOW_UP)
        self.assertEqual(b2["instructions"], b1["instructions"])
        self.assertEqual(h2["X-Hermes-Session-Key"], "telegram:42", "the same conversation")
        self.assertEqual((h1["Idempotency-Key"], h2["Idempotency-Key"]), ("evt-1#1", "evt-1#1/crew1"))

    def test_crew_that_finishes_inside_the_run_needs_no_follow_up(self):
        FakeHermes.runs = [frames(
            {"event": "tool.started", "tool": "delegate_task"},
            {"event": "subagent.start", "subagent_id": "sa-1", "goal": "Patch: review pull request 12"},
            {"event": "subagent.start", "subagent_id": "sa-9", "goal": "look into something"},
            {"event": "subagent.complete", "subagent_id": "sa-1", "status": "completed"},
            {"event": "subagent.complete", "subagent_id": "sa-9", "status": "completed"},
            {"event": "tool.completed", "tool": "delegate_task", "preview": json.dumps({"results": []})},
            {"event": "run.completed", "output": "Ship it."})]
        res = self.run_turn()
        self.assertEqual(res.output, "Ship it.")
        # A child whose goal names no crew member gets no crew line.
        self.assertEqual(self.progress, ["using delegate_task", "Patch: review pull request 12", "Patch: done"])
        self.assertEqual(len(self.run_posts()), 1)
        self.assertEqual(FakeHermes.health_calls, 0)

    def test_unknown_background_state_keeps_the_leads_status(self):
        FakeHermes.runs = [frames(
            {"event": "subagent.start", "subagent_id": "sa-1", "goal": "Scout: look it up"},
            {"event": "run.completed", "output": "Scout is on it."})]
        FakeHermes.health = [None]
        res = self.run_turn()
        self.assertEqual(res.output, "Scout is on it.")
        self.assertEqual(len(self.run_posts()), 1)
        self.assertEqual(self.progress, ["Scout: look it up"])

    def test_crew_still_working_at_the_deadline(self):
        FakeHermes.runs = [frames(
            {"event": "subagent.start", "subagent_id": "sa-1", "goal": "Byte: a long job"},
            {"event": "run.completed", "output": "Byte is on it."})]
        FakeHermes.health = [1]
        with self.assertRaises(HermesError):
            self.run_turn(timeout=0.3)
        self.assertEqual(len(self.run_posts()), 1)

    def test_follow_up_turns_are_bounded(self):
        again = frames({"event": "subagent.start", "subagent_id": "sa-1", "goal": "Scout: once more"},
                       {"event": "run.completed", "output": "still working"})
        FakeHermes.runs = [again] * (MAX_CREW_ROUNDS + 1)
        res = self.run_turn()
        self.assertEqual(res.output, "still working")
        self.assertEqual(len(self.run_posts()), MAX_CREW_ROUNDS + 1)


class HermesProcessTest(unittest.TestCase):
    def test_the_gateway_installs_nothing_at_runtime(self):
        # The gateway is started with HERMES_DISABLE_LAZY_INSTALLS=1, next to security.allow_lazy_installs:
        # false in config.yaml, whatever the container's environment says; the API server settings as before.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "env.json")
        script = "import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], 'w'))"
        proc = HermesProcess([sys.executable, "-c", script, out], {"PATH": os.environ.get("PATH", ""),
                                                                    "HERMES_DISABLE_LAZY_INSTALLS": "0"}, 8642)
        proc.start()
        self.addCleanup(proc.stop, 1)
        deadline = time.monotonic() + 10
        while not os.path.exists(out) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)
        with open(out) as fh:
            env = json.load(fh)
        self.assertEqual(env["HERMES_DISABLE_LAZY_INSTALLS"], "1")
        self.assertEqual((env["API_SERVER_ENABLED"], env["API_SERVER_HOST"], env["API_SERVER_PORT"]),
                         ("true", "127.0.0.1", "8642"))
        self.assertEqual(env["API_SERVER_KEY"], proc.api_key)


if __name__ == "__main__":
    unittest.main()
