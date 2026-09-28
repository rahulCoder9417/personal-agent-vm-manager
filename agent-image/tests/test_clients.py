"""HTTP-level tests for the Platform MCP client and the Hermes Runs API client."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge.hermes import HermesClient
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


class FakeHermes(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    posted: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        if self.path == "/v1/runs/run_1/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            frames = [
                ": open",
                'id: 1\ndata: {"event": "tool.started", "tool": "web_search", "preview": "x"}',
                'id: 2\ndata: {"event": "approval.request", "request_id": "r1"}',
                'id: 3\ndata: {"event": "run.completed", "output": "All done."}',
            ]
            for f in frames:
                self.wfile.write((f + "\n\n").encode())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeHermes.posted.append((self.path, dict(self.headers), body))
        self.send_response(202 if self.path == "/v1/runs" else 200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"run_id": "run_1", "status": "started"}')


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
        self.srv, self.url = serve(FakeHermes)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_run_to_completion(self):
        c = HermesClient(self.url, "local-key")
        self.assertTrue(c.healthy())
        progress = []
        res = c.run("hello", "be brief", "telegram:42", "evt-1#1", progress.append, timeout=10)
        self.assertEqual((res.status, res.output), ("completed", "All done."))
        self.assertEqual(progress, ["using web_search"])
        path, headers, body = FakeHermes.posted[0]
        self.assertEqual(path, "/v1/runs")
        self.assertEqual(headers["Authorization"], "Bearer local-key")
        self.assertEqual(headers["X-Hermes-Session-Key"], "telegram:42")
        self.assertEqual(headers["Idempotency-Key"], "evt-1#1")
        self.assertEqual(body, {"input": "hello", "instructions": "be brief"})
        # Hermes's own approval prompt is resolved by policy (deny by default).
        self.assertEqual(FakeHermes.posted[1][0], "/v1/runs/run_1/approval")
        self.assertEqual(FakeHermes.posted[1][2], {"choice": "deny", "all": True})


if __name__ == "__main__":
    unittest.main()
