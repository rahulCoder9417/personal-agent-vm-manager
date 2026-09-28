import json
import threading
import unittest
import urllib.error
import urllib.request

from bridge.core import Bridge, DeliveryStore
from bridge.server import BridgeHTTPServer

from tests.fakes import FakeRunner, FakeSender

SECRET = "s3cret-bridge"


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.sender = FakeSender()
        self.bridge = Bridge(SECRET, self.runner, self.sender, DeliveryStore(None), retry_delay=0)
        self.stops = []
        self.server = BridgeHTTPServer(("127.0.0.1", 0), self.bridge, lambda: self.stops.append(1))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, secret=SECRET):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if secret:
            r.add_header("X-Bridge-Secret", secret)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        try:
            resp = self.opener.open(r, timeout=5)
            return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def test_healthz(self):
        code, _, body = self.call("GET", "/healthz", secret=None)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"ok": True, "busy": 0})
        self.runner.is_healthy = False
        code, _, _ = self.call("GET", "/healthz", secret=None)
        self.assertEqual(code, 503)

    def test_deliver_requires_secret(self):
        body = {"delivery_id": "d1", "kind": "message", "text": "hello"}
        self.assertEqual(self.call("POST", "/deliver", body, secret=None)[0], 401)
        self.assertEqual(self.call("POST", "/deliver", body, secret="wrong")[0], 401)
        self.assertEqual(self.runner.calls, [])

    def test_deliver_streams_ndjson(self):
        code, headers, body = self.call("POST", "/deliver", {"delivery_id": "d1", "kind": "message",
                                                             "session_key": "web", "text": "hello"})
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/x-ndjson")
        lines = [json.loads(line) for line in body.decode().splitlines() if line.strip()]
        self.assertEqual([e["type"] for e in lines], ["accepted", "working", "turn_done"])
        for e in lines:
            self.assertEqual(set(e), {"type", "message", "platform_fault"})
        self.assertEqual(self.sender.sent, [("Hi! How can I help?", "web")])

    def test_deliver_dedupes(self):
        body = {"delivery_id": "d1", "kind": "message", "text": "hello"}
        self.call("POST", "/deliver", body)
        _, _, again = self.call("POST", "/deliver", body)
        lines = [json.loads(line) for line in again.decode().splitlines() if line.strip()]
        self.assertEqual([e["type"] for e in lines], ["accepted", "turn_done"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_deliver_bad_body(self):
        self.assertEqual(self.call("POST", "/deliver", {"kind": "message"})[0], 400)
        self.assertEqual(self.call("POST", "/deliver", {"delivery_id": "x", "kind": "bogus"})[0], 400)

    def test_shutdown(self):
        self.assertEqual(self.call("POST", "/shutdown", {}, secret="wrong")[0], 401)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.call("POST", "/shutdown", {})[0], 200)
        self.assertEqual(self.call("POST", "/shutdown", {})[0], 200)
        self.assertEqual(self.stops, [1], "shutdown runs once")

    def test_unknown_path(self):
        self.assertEqual(self.call("GET", "/nope", secret=None)[0], 404)


if __name__ == "__main__":
    unittest.main()
