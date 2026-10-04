"""Scout's browser: the server's settings and command (bridge/browser.py), the guard in front of the server
(bridge/browser_mcp.py), the meter, and the browser time the Bridge reports with a turn (bridge/core.py).

The guard runs here in front of a stand-in server (tests/fake_browser_server.py). RealBrowserTest runs the
real Playwright MCP server and a real Chromium, when the machine has them: set BROWSER_TEST_ROOT to a
directory laid out like the image's /opt/browser (node_modules/@playwright/mcp and ms-playwright).
"""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from bridge import browser, browser_check, browser_mcp, crew
from bridge.core import Bridge, DeliverRequest, DeliveryStore, browser_fields

from tests.fakes import FakeRunner, FakeSender

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_SERVER = os.path.join(HERE, "fake_browser_server.py")
SECRET = "s3cret-bridge"
# The tools of Playwright MCP 0.0.83 that Scout does not get.
WITHHELD = ("browser_resize", "browser_console_messages", "browser_emulate_media", "browser_evaluate",
            "browser_file_upload", "browser_drop", "browser_network_requests", "browser_network_request",
            "browser_run_code_unsafe", "browser_take_screenshot", "browser_drag")


def image_root(tmp):
    """A directory laid out like the image's /opt/browser."""
    root = os.path.join(tmp, "browser")
    os.makedirs(os.path.join(root, "node_modules", "@playwright", "mcp"))
    os.makedirs(os.path.join(root, "ms-playwright"))
    with open(os.path.join(root, "node_modules", "@playwright", "mcp", "cli.js"), "w") as fh:
        fh.write("// the browser's MCP server\n")
    return root


class Flag:
    def __init__(self, browser_enabled):
        self.browser_enabled = browser_enabled


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = image_root(self.tmp)
        self.env = {"BROWSER_ROOT": self.root, "BROWSER_NODE": "/usr/local/bin/node", "HOME": "/data/home",
                    "TMPDIR": "/data/tmp", "HTTPS_PROXY": "http://172.30.0.2:3128", "HTTP_PROXY": "http://other:1"}

    def test_on_only_when_turned_on_for_the_user_and_in_the_image(self):
        self.assertTrue(browser.installed(self.env))
        self.assertTrue(browser.enabled(Flag(True), self.env))
        self.assertFalse(browser.enabled(Flag(False), self.env))
        self.assertFalse(browser.enabled(object(), self.env), "an older platform.json has no switch")
        missing = {**self.env, "BROWSER_ROOT": os.path.join(self.tmp, "nothing")}
        self.assertFalse(browser.installed(missing))
        self.assertFalse(browser.enabled(Flag(True), missing))
        # The image's own place when nothing overrides it.
        self.assertEqual(browser.server_js({}), "/opt/browser/node_modules/@playwright/mcp/cli.js")
        self.assertEqual(browser.browsers_dir({}), "/opt/browser/ms-playwright")

    def test_the_servers_settings(self):
        self.assertEqual(browser.render_config(self.env, "/data"), {
            "browser": {
                "browserName": "chromium",
                # The profile (cookies) stays in the user's own volume.
                "userDataDir": "/data/browser/profile",
                "launchOptions": {
                    "headless": True,
                    # No Chromium sandbox: the container (gVisor) is the sandbox.
                    "chromiumSandbox": False,
                    "args": ["--disable-dev-shm-usage", "--disable-gpu", "--disk-cache-size=104857600",
                             "--disk-cache-dir=/data/browser/cache"],
                    # Everything through the egress proxy.
                    "proxy": {"server": "http://172.30.0.2:3128"},
                },
            },
            "outputDir": "/data/browser/output",
            "outputMaxSize": 20 * 1024 * 1024,
            "snapshot": {"mode": "none"},
            # Closed after two idle minutes.
            "timeouts": {"idle": 120000},
            "imageResponses": "omit",
            "webmcp": False,
            "codegen": "none",
            "allowUnrestrictedFileAccess": False,
        })
        self.assertEqual(browser.IDLE_SECONDS, 120)
        # Nothing the browser writes is outside /data (the root filesystem is read-only).
        config = json.dumps(browser.render_config(self.env, "/data"))
        for path in ("/tmp", "/dev/shm", "/opt", "/root", "/var"):
            self.assertNotIn(f'"{path}', config)
            self.assertNotIn(f"={path}", config)
        # Which proxy: HTTPS_PROXY first, then the other spellings; none, no proxy setting.
        self.assertEqual(browser.proxy_url({"http_proxy": "http://p:1"}), "http://p:1")
        self.assertEqual(browser.proxy_url({}), "")
        self.assertNotIn("proxy", browser.render_config({}, "/data")["browser"]["launchOptions"])

    def test_the_settings_file_is_private_and_the_directories_exist(self):
        data = os.path.join(self.tmp, "data")
        path = browser.write_config(self.env, data)
        self.assertEqual(path, os.path.join(data, ".platform", "browser-mcp.json"))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
        with open(path) as fh:
            self.assertEqual(json.load(fh), browser.render_config(self.env, data))
        for name in ("profile", "cache", "output"):
            self.assertTrue(os.path.isdir(os.path.join(data, "browser", name)), name)
        self.assertEqual(os.listdir(os.path.join(data, ".platform")), ["browser-mcp.json"], "no temp file left")
        browser.write_config(self.env, data)  # again: fine

    def test_the_command_the_engines_start(self):
        cmd = browser.server_command(self.env, "/data")
        self.assertEqual(cmd, [
            sys.executable, "-I", browser.GUARD,
            "--allow", "browser_navigate,browser_navigate_back,browser_snapshot,browser_find,browser_click,"
                       "browser_type,browser_fill_form,browser_select_option,browser_press_key,browser_hover,"
                       "browser_wait_for,browser_handle_dialog,browser_tabs,browser_close",
            "--env", f"PLAYWRIGHT_BROWSERS_PATH={self.root}/ms-playwright", "--env", "HOME=/data/home",
            "--env", "TMPDIR=/data/tmp",
            "--", "/usr/local/bin/node", f"{self.root}/node_modules/@playwright/mcp/cli.js",
            "--config", "/data/.platform/browser-mcp.json"])
        self.assertTrue(os.path.isfile(browser.GUARD))
        self.assertEqual(os.path.basename(browser.GUARD), "browser_mcp.py")
        # The guard reads it the same way: these tools, these variables, this server.
        allow, env, server = browser_mcp.parse_args(cmd[3:])
        self.assertEqual(tuple(allow), crew.BROWSER_TOOLS)
        self.assertEqual(env, {"PLAYWRIGHT_BROWSERS_PATH": f"{self.root}/ms-playwright", "HOME": "/data/home",
                               "TMPDIR": "/data/tmp"})
        self.assertEqual(server, cmd[cmd.index("--") + 1:])
        # The same command for both engines, in each one's own config shape.
        self.assertEqual(browser.claude_server(self.env, "/data"), {"type": "stdio", "command": cmd[0], "args": cmd[1:]})
        self.assertEqual(browser.hermes_server(self.env, "/data"),
                         {"command": cmd[0], "args": cmd[1:], "timeout": 120, "connect_timeout": 60, "enabled": True})
        # Without HOME and TMPDIR in the environment they are the image's.
        bare = browser.server_command({"BROWSER_ROOT": self.root, "BROWSER_NODE": "node"}, "/data")
        self.assertIn("HOME=/data/home", bare)
        self.assertIn("TMPDIR=/data/tmp", bare)


class GuardRulesTest(unittest.TestCase):
    """The three rules of browser_mcp.py, on single messages."""

    def setUp(self):
        self.guard = browser_mcp.Guard(list(crew.BROWSER_TOOLS))

    def call(self, name, arguments=None, msg_id=7):
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name}}
        if arguments is not None:
            msg["params"]["arguments"] = arguments
        return self.guard.from_client(msg)

    def assert_refused(self, name, arguments, word):
        forward, answer = self.call(name, arguments)
        self.assertIsNone(forward, f"{name} {arguments} must not reach the server")
        self.assertEqual((answer["jsonrpc"], answer["id"], answer["result"]["isError"]), ("2.0", 7, True))
        (part,) = answer["result"]["content"]
        self.assertEqual(part["type"], "text")
        self.assertIn(word, part["text"])

    def test_only_scouts_tools(self):
        for name in crew.BROWSER_TOOLS:
            forward, answer = self.call(name, {})
            self.assertIsNone(answer, name)
            self.assertEqual(forward["params"]["name"], name)
        for name in (*WITHHELD, "browser_install", "browser_pdf_save", "anything", "", None, 7):
            self.assert_refused(name, {}, "This tool is not available")
        # The refusal names what there is.
        _, answer = self.call("browser_run_code_unsafe", {"code": "x"})
        self.assertIn("browser_navigate, browser_navigate_back", answer["result"]["content"][0]["text"])

    def test_only_web_addresses(self):
        for url in ("https://example.com/", "http://example.com", "HTTPS://Example.com/a?b=c#d",
                    "http://127.0.0.1:8765/healthz", "about:blank", " https://example.com/ "):
            forward, answer = self.call("browser_navigate", {"url": url})
            self.assertIsNone(answer, url)  # which hosts it may reach is the egress proxy's business
            self.assertEqual(forward["params"]["arguments"]["url"], url)
        for url in ("file:///data/.platform/platform.json", "FILE:///etc/passwd",
                    # Playwright MCP 0.0.83 lets this one through to the file; the guard does not.
                    "view-source:file:///data/claude/.credentials.json", "view-source:https://example.com/",
                    "chrome://version", "chrome-extension://x/y", "javascript:alert(1)", "data:text/html,<h1>x</h1>",
                    "blob:https://example.com/1", "ftp://example.com/", "ws://example.com/", "about:config",
                    "example.com", "//example.com/", "localhost:8080", "/etc/passwd", "", "http://", "https:///x",
                    "http://exa mple.com/", "https://example.com/\nfile:///etc/passwd", None, 7, ["https://a"],
                    {"href": "https://a"}):
            self.assert_refused("browser_navigate", {"url": url}, "only full http:// or https:// addresses")
            self.assert_refused("browser_tabs", {"action": "new", "url": url}, "only full http:// or https://")
            self.assertFalse(browser_mcp.allowed_url(url), url)
        # A tool without an address is not affected.
        self.assertIsNone(self.call("browser_tabs", {"action": "list"})[1])

    def test_nothing_is_written_to_a_file(self):
        forward, answer = self.call("browser_snapshot", {"filename": "../../home/.bashrc", "depth": 3})
        self.assertIsNone(answer)
        self.assertEqual(forward["params"], {"name": "browser_snapshot", "arguments": {"depth": 3}})
        forward, _ = self.call("browser_find", {"text": "price", "filename": "x.txt"})
        self.assertEqual(forward["params"]["arguments"], {"text": "price"})
        # A call without arguments, and one whose arguments are not an object.
        self.assertEqual(self.call("browser_close")[0]["params"], {"name": "browser_close"})
        self.assert_refused("browser_click", ["e3"], "must be an object")

    def test_other_messages_pass(self):
        for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 9, "result": {"roots": []}},  # the client's answer to the server
                    "text", 3, None):
            self.assertEqual(self.guard.from_client(msg), (msg, None))

    def test_the_tool_list_is_filtered(self):
        listing = {"jsonrpc": "2.0", "id": "a1", "method": "tools/list"}
        self.assertEqual(self.guard.from_client(listing), (listing, None))
        tools = [{"name": n, "description": n} for n in ("browser_run_code_unsafe", "browser_navigate",
                                                          "browser_file_upload", "browser_close", "extra")]
        answer = self.guard.from_server({"jsonrpc": "2.0", "id": "a1", "result": {"tools": tools, "nextCursor": "c"}})
        self.assertEqual([t["name"] for t in answer["result"]["tools"]], ["browser_navigate", "browser_close"])
        self.assertEqual(answer["result"]["nextCursor"], "c")
        # Only the answer to a tools/list: another answer with the same shape, or the same id later, is not touched.
        other = {"jsonrpc": "2.0", "id": "a1", "result": {"tools": tools}}
        self.assertEqual(self.guard.from_server(other), other)
        for msg in ({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
                    {"jsonrpc": "2.0", "id": 5, "result": {"content": []}}, [1], "x"):
            self.assertEqual(self.guard.from_server(msg), msg)
        # An error answer to tools/list passes as it is.
        self.guard.from_client({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        err = {"jsonrpc": "2.0", "id": 3, "error": {"code": -1, "message": "no"}}
        self.assertEqual(self.guard.from_server(err), err)

    def test_command_line(self):
        self.assertEqual(browser_mcp.parse_args(["--allow", "a, b", "--allow", "c", "--env", "K=v=w", "--", "node", "x"]),
                         (["a", "b", "c"], {"K": "v=w"}, ["node", "x"]))
        for bad in ([], ["--allow", "a"], ["--allow", "a", "--"], ["--", "node"], ["--env", "K", "--", "node"],
                    ["--allow", "a", "--nope", "--", "node"], ["--allow", " , ", "--", "node"]):
            with self.assertRaises(ValueError, msg=bad):
                browser_mcp.parse_args(bad)

    def test_it_runs_on_its_own(self):
        # Run as a file in isolated mode: only the standard library, nothing from the Bridge package.
        with open(browser.GUARD, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("from .", source)
        self.assertNotIn("import bridge", source)
        imports = sorted({line.split()[1] for line in source.splitlines() if line.startswith(("import ", "from "))})
        self.assertEqual(imports, ["__future__", "json", "os", "re", "signal", "subprocess", "sys", "threading",
                                   "typing"])


class Client:
    """A minimal MCP client over the guard's stdin/stdout."""

    def __init__(self, argv, env):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env)

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def read(self):
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def ask(self, msg):
        """Send a request and return its answer, with whatever else arrived before it."""
        self.send(msg)
        others = []
        while True:
            got = self.read()
            if got is None or (isinstance(got, dict) and got.get("id") == msg["id"]):
                return got, others
            others.append(got)

    def end(self):
        self.proc.stdin.close()
        return self.proc.wait(15)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


class GuardProcessTest(unittest.TestCase):
    """browser_mcp.py started the way an engine starts it, in front of the stand-in server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "server.log")
        self.env = {"PATH": os.environ.get("PATH", ""), "FAKE_BROWSER_LOG": self.log}

    def start(self, *server_args, allow=crew.BROWSER_TOOLS):
        argv = [sys.executable, "-I", browser.GUARD, "--allow", ",".join(allow), "--env", "EXTRA=from-the-command-line",
                "--env", "TMPDIR=/data/tmp", "--", sys.executable, FAKE_SERVER, *server_args]
        client = Client(argv, self.env)
        self.addCleanup(client.kill)
        return client

    def server_saw(self):
        with open(self.log) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def calls_the_server_saw(self):
        return [m["params"] for m in self.server_saw() if isinstance(m, dict) and m.get("method") == "tools/call"]

    def test_a_session(self):
        c = self.start()
        init, _ = c.ask({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "1"}}})
        self.assertEqual(init["result"]["serverInfo"], {"name": "Playwright", "version": "fake"})
        c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # The list: the server has 25 tools; the client sees Scout's 14, in the server's order.
        listing, _ = c.ask({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in listing["result"]["tools"]]
        self.assertEqual(sorted(names), sorted(crew.BROWSER_TOOLS))
        self.assertEqual(len(names), 14)
        for name in WITHHELD:
            self.assertNotIn(name, names)
        # An allowed call reaches the server and its answer comes back, with what the server sends besides.
        answer, others = c.ask({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "browser_navigate", "arguments": {"url": "https://example.com/"}}})
        self.assertEqual(json.loads(answer["result"]["content"][0]["text"]),
                         {"ran": "browser_navigate", "arguments": {"url": "https://example.com/"}})
        self.assertNotIn("isError", answer["result"])
        # `filename` is dropped on the way.
        answer, others2 = c.ask({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                 "params": {"name": "browser_snapshot", "arguments": {"filename": "a.yml", "depth": 2}}})
        self.assertEqual(json.loads(answer["result"]["content"][0]["text"]),
                         {"ran": "browser_snapshot", "arguments": {"depth": 2}})
        self.assertIn({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "x"}},
                      others + others2, "the server's own messages pass")
        # Refused here, never sent on: another tool, a file address, the address 0.0.83 lets through.
        for msg_id, params in ((5, {"name": "browser_run_code_unsafe", "arguments": {"code": "process.exit()"}}),
                               (6, {"name": "browser_file_upload", "arguments": {"paths": ["/data/.platform/platform.json"]}}),
                               (7, {"name": "browser_navigate", "arguments": {"url": "file:///etc/passwd"}}),
                               (8, {"name": "browser_navigate",
                                    "arguments": {"url": "view-source:file:///data/.platform/platform.json"}}),
                               (9, {"name": "browser_tabs", "arguments": {"action": "new", "url": "chrome://version"}})):
            answer, _ = c.ask({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params})
            self.assertTrue(answer["result"]["isError"], params)
            self.assertNotIn("ran", answer["result"]["content"][0]["text"])
        # A batch is taken apart: the allowed call runs, the other is refused.
        c.send([{"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "browser_evaluate"}},
                {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "browser_close"}}])
        got = {}
        while not {10, 11} <= set(got):
            msg = c.read()
            if isinstance(msg, dict) and "id" in msg:
                got[msg["id"]] = msg
        self.assertTrue(got[10]["result"]["isError"])
        self.assertEqual(json.loads(got[11]["result"]["content"][0]["text"])["ran"], "browser_close")
        # Lines that are not JSON are dropped; the session goes on.
        c.proc.stdin.write(b"not json\n\n")
        answer, _ = c.ask({"jsonrpc": "2.0", "id": 12, "method": "ping"})
        self.assertEqual(answer["error"]["message"], "unknown method")
        # The client goes away: the guard ends the server and exits cleanly.
        self.assertEqual(c.end(), 0)
        self.assertEqual(self.calls_the_server_saw(), [
            {"name": "browser_navigate", "arguments": {"url": "https://example.com/"}},
            {"name": "browser_snapshot", "arguments": {"depth": 2}},
            {"name": "browser_close"}])
        saw = self.server_saw()
        self.assertEqual(saw[-1], "exit", "the server's input was closed")
        # The server got what the command line said it needs, on top of the guard's own environment.
        self.assertEqual(saw[0]["start"]["env"], {"PLAYWRIGHT_BROWSERS_PATH": None, "HOME": None,
                                                  "TMPDIR": "/data/tmp", "EXTRA": "from-the-command-line"})

    def test_the_guard_ends_when_the_server_ends(self):
        c = self.start("--exit-after", "1")
        answer, _ = c.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(len(answer["result"]["tools"]), 14)
        self.assertEqual(c.proc.wait(15), 3, "the server's exit code")
        self.assertIsNone(c.read())

    def test_a_signal_ends_both(self):
        c = self.start()
        c.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        c.proc.send_signal(signal.SIGTERM)
        self.assertEqual(c.proc.wait(15), 0)
        deadline = time.monotonic() + 5
        while self.server_saw()[-1] != "exit" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.server_saw()[-1], "exit")

    def test_no_server_means_no_browser(self):
        # The guard never runs without its rules, and when the server cannot start there is nothing to talk to.
        argv = [sys.executable, "-I", browser.GUARD, "--allow", "browser_navigate", "--",
                os.path.join(self.tmp, "no-such-node"), "cli.js"]
        done = subprocess.run(argv, input=b"", capture_output=True, timeout=20)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, b"")
        self.assertIn(b"could not start the browser server", done.stderr)
        done = subprocess.run([sys.executable, "-I", browser.GUARD, "--", sys.executable, FAKE_SERVER], input=b"",
                              capture_output=True, timeout=20)
        self.assertEqual(done.returncode, 2, "no --allow: refuse to start")


BROWSERS = "/opt/browser/ms-playwright"
CHROME = f"{BROWSERS}/chromium-1247/chrome-linux64/chrome"


class Machine:
    """The processes of a container, the clock and the signals, as the meter sees them."""

    def __init__(self):
        self.now = 1000.0
        self.procs = {1: ["/usr/bin/docker-init", "--", "python", "-m", "bridge"],
                      20: ["python", "-m", "bridge"],
                      30: ["node", "/opt/browser/node_modules/@playwright/mcp/cli.js", "--config", "/data/x.json"],
                      31: [sys.executable, "-I", browser.GUARD, "--allow", "browser_navigate", "--", "node"]}
        self.signals = []
        self.dies_on = {signal.SIGTERM: True}

    def open_browser(self):
        # Chromium rewrites its command line as one string; a helper carries --type=.
        self.procs[100] = [f"{CHROME} --headless --no-sandbox --user-data-dir=/data/browser/profile about:blank"]
        self.procs[101] = [f"{CHROME} --type=renderer --user-data-dir=/data/browser/profile"]
        self.procs[102] = [f"{BROWSERS}/chromium-1247/chrome-linux64/chrome_crashpad_handler", "--monitor-self"]

    def close_browser(self):
        for pid in (100, 101, 102):
            self.procs.pop(pid, None)

    def processes(self):
        return [(pid, list(argv)) for pid, argv in self.procs.items()]

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if pid not in self.procs:
            raise ProcessLookupError
        if sig == signal.SIGKILL:
            del self.procs[pid]
        elif self.dies_on.get(sig) and pid == 100:
            self.close_browser()  # the main process takes its helpers with it

    def meter(self, **kw):
        return browser.BrowserMeter(BROWSERS, processes=self.processes, kill=self.kill, clock=lambda: self.now, **kw)


class MeterTest(unittest.TestCase):
    def setUp(self):
        self.m = Machine()
        self.meter = self.m.meter()

    def test_which_processes_are_a_browser(self):
        prefix = BROWSERS + "/"
        for argv, want in (([CHROME, "--headless"], (True, True)),
                           ([f"{CHROME} --headless --user-data-dir=/data/browser/profile"], (True, True)),
                           ([CHROME, "--type=renderer"], (True, False)),
                           ([f"{CHROME} --type=gpu-process --x"], (True, False)),
                           ([f"{BROWSERS}/chromium-1247/chrome-linux64/chrome_crashpad_handler --monitor-self"], (True, False)),
                           (["node", f"{BROWSERS}/../node_modules/@playwright/mcp/cli.js"], (False, False)),
                           (["/usr/bin/chromium", "--headless"], (False, False)),
                           ([sys.executable, "-I", browser.GUARD, "--env", f"PLAYWRIGHT_BROWSERS_PATH={BROWSERS}"], (False, False)),
                           ([], (False, False)), ([""], (False, False))):
            self.assertEqual(browser.browser_process(argv, prefix), want, argv)

    def test_nothing_is_counted_when_the_browser_never_started(self):
        for _ in range(5):
            self.assertFalse(self.meter.sample())
            self.m.now += 60
        self.assertEqual(self.meter.drain(), 0)
        self.assertEqual(self.meter.end_turn(last=True), 0)
        self.assertEqual(self.m.signals, [], "nothing to close")

    def test_from_start_to_close(self):
        self.meter.sample()
        self.m.now += 10  # ten seconds of a turn without the browser
        self.m.open_browser()
        self.assertTrue(self.meter.sample())
        self.m.now += 73
        self.assertTrue(self.meter.sample())
        self.m.close_browser()
        self.assertFalse(self.meter.sample())
        self.m.now += 500  # closed: this is not browser time
        self.assertEqual(self.meter.drain(), 73)
        self.assertEqual(self.meter.drain(), 0, "each second is reported once")

    def test_two_sessions_and_a_report_in_between(self):
        self.m.open_browser()
        self.meter.sample()
        self.m.now += 30
        self.m.close_browser()
        self.meter.sample()
        self.m.now += 100
        self.m.open_browser()
        self.meter.sample()
        self.m.now += 20
        # A report while the browser is open: what has passed so far; it keeps counting from here.
        self.assertEqual(self.meter.drain(), 50)
        self.m.now += 15
        self.m.close_browser()
        self.meter.sample()
        self.assertEqual(self.meter.drain(), 15)
        self.assertEqual(self.meter.drain(), 0)

    def test_parts_of_a_second_are_carried_over(self):
        total = 0
        for _ in range(4):
            self.m.open_browser()
            self.meter.sample()
            self.m.now += 0.6
            self.m.close_browser()
            self.meter.sample()
            total += self.meter.drain()
        self.assertEqual(total, 2, "4 x 0.6 s = 2.4 s: two whole seconds reported, the rest kept")
        self.m.open_browser()
        self.meter.sample()
        self.m.now += 0.6
        self.m.close_browser()
        self.meter.sample()
        self.assertEqual(self.meter.drain(), 1, "0.4 s kept + 0.6 s")

    def test_close_asks_the_browser_to_end(self):
        self.m.open_browser()
        self.meter.sample()
        self.m.now += 5
        self.meter.close(grace=1)
        self.assertEqual(self.m.signals, [(100, signal.SIGTERM)], "the main process, not its helpers")
        self.assertFalse(self.meter.is_open())
        self.assertEqual(self.meter.drain(), 5)
        # The MCP server and the guard are not browsers: never signalled.
        self.assertIn(30, self.m.procs)
        self.assertIn(31, self.m.procs)

    def test_close_kills_what_does_not_end(self):
        self.m.dies_on = {}
        self.m.open_browser()
        self.meter.sample()
        self.m.now += 2
        self.meter.close(grace=0.2)
        self.assertEqual(self.m.signals[0], (100, signal.SIGTERM))
        self.assertEqual(sorted(self.m.signals[1:]), [(100, signal.SIGKILL), (101, signal.SIGKILL), (102, signal.SIGKILL)])
        self.assertFalse(self.meter.is_open())
        self.assertEqual(self.meter.drain(), 2)

    def test_the_end_of_a_turn(self):
        # The last turn running closes the browser and reports all of it.
        self.m.open_browser()
        self.meter.sample()
        self.m.now += 40
        self.assertEqual(self.meter.end_turn(last=True), 40)
        self.assertFalse(self.meter.is_open())
        # Another turn is still running: the browser stays, and this turn reports the time so far.
        self.m.open_browser()
        self.meter.sample()
        self.m.now += 12
        self.m.signals.clear()
        self.assertEqual(self.meter.end_turn(last=False), 12)
        self.assertEqual(self.m.signals, [])
        self.assertTrue(self.meter.is_open())
        self.m.now += 8
        self.assertEqual(self.meter.end_turn(last=True), 8, "the other turn reports the rest, not the same seconds")

    def test_a_browser_left_open_with_no_turn_running_is_closed(self):
        busy = [True]
        meter = self.m.meter(busy=lambda: busy[0], idle_close=15)
        self.m.open_browser()
        for _ in range(30):  # a turn is running: left alone however long
            meter.tick()
            self.m.now += 1
        self.assertEqual(self.m.signals, [])
        busy[0] = False
        for _ in range(15):
            meter.tick()
            self.m.now += 1
        self.assertEqual(self.m.signals, [], "not before 15 seconds without a turn")
        meter.tick()
        self.assertEqual(self.m.signals, [(100, signal.SIGTERM)])
        self.assertFalse(meter.is_open())
        self.assertEqual(meter.drain(), 45, "the time is still reported, with the next turn")
        # A turn that starts in between resets the wait.
        self.m.signals.clear()
        self.m.open_browser()
        for is_busy in (False, False, True, False, False):
            busy[0] = is_busy
            meter.tick()
            self.m.now += 10
        self.assertEqual(self.m.signals, [])

    def test_the_watcher_thread(self):
        meter = self.m.meter(poll=0.01)
        meter.start()
        meter.start()  # once
        self.m.open_browser()
        deadline = time.monotonic() + 2
        while meter._open_since is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(meter._open_since, "the watcher saw the browser open")
        meter.stop()

    def test_real_processes(self):
        # list_processes reads this machine's /proc: this test's own process is in it, with its arguments.
        mine = dict(browser.list_processes()).get(os.getpid())
        self.assertIsNotNone(mine)
        self.assertEqual(os.path.basename(mine[0]), os.path.basename(sys.executable))
        self.assertFalse(browser.BrowserMeter(BROWSERS).is_open())


class FakeMeter:
    def __init__(self, *seconds):
        self.seconds = list(seconds)
        self.calls = []
        self.fail = False

    def end_turn(self, last):
        self.calls.append(last)
        if self.fail:
            raise RuntimeError("the meter broke")
        return self.seconds.pop(0) if self.seconds else 0


def req(delivery_id="evt-1", **kw):
    body = {"delivery_id": delivery_id, "kind": "message", "session_key": "telegram:42", "channel": "telegram",
            "text": "hello"}
    body.update(kw)
    return DeliverRequest.from_json(body)


class BrowserTimeWithTheTurnTest(unittest.TestCase):
    """The Bridge reports browser time with the last event of a turn (bridge/core.py)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_path = os.path.join(self.tmp, "deliveries.json")
        self.runner, self.sender = FakeRunner(), FakeSender()

    def bridge(self, meter):
        return Bridge(SECRET, self.runner, self.sender, DeliveryStore(self.store_path), retry_delay=0, browser=meter)

    def test_reported_on_the_last_event(self):
        meter = FakeMeter(73)
        events = list(self.bridge(meter).deliver(req()))
        self.assertEqual([e["type"] for e in events], ["accepted", "working", "turn_done"])
        self.assertEqual(events[-1], {"type": "turn_done", "message": "", "platform_fault": False,
                                      "browser_seconds": 73, "browser_report_id": "evt-1#1"})
        for ev in events[:-1]:
            self.assertNotIn("browser_seconds", ev)
        self.assertEqual(meter.calls, [True], "the only turn running: the browser is closed")
        # As the vm-manager reads the line.
        self.assertEqual(json.loads(json.dumps(events[-1]))["browser_seconds"], 73)

    def test_nothing_when_the_browser_never_started(self):
        events = list(self.bridge(FakeMeter(0)).deliver(req()))
        self.assertEqual(events[-1], {"type": "turn_done", "message": "", "platform_fault": False})
        # And a container without the browser has no meter at all.
        events = list(self.bridge(None).deliver(req("evt-2")))
        self.assertEqual(events[-1], {"type": "turn_done", "message": "", "platform_fault": False})

    def test_a_failed_turn_reports_what_it_used(self):
        self.runner.status = "failed"
        bridge = self.bridge(FakeMeter(20, 15))
        events = list(bridge.deliver(req()))
        self.assertEqual((events[-1]["type"], events[-1]["platform_fault"]), ("error", True))
        self.assertEqual((events[-1]["browser_seconds"], events[-1]["browser_report_id"]), (20, "evt-1#1"))
        # The retry runs again and reports its own time under a new id.
        self.runner.status = "completed"
        events = list(bridge.deliver(req()))
        self.assertEqual(events[-1]["type"], "turn_done")
        self.assertEqual((events[-1]["browser_seconds"], events[-1]["browser_report_id"]), (15, "evt-1#2"))

    def test_a_run_that_raises_still_reports(self):
        self.runner.raise_exc = RuntimeError("boom")
        events = list(self.bridge(FakeMeter(9)).deliver(req()))
        self.assertEqual((events[-1]["type"], events[-1]["browser_seconds"], events[-1]["browser_report_id"]),
                         ("error", 9, "evt-1#1"))

    def test_a_reply_that_could_not_be_sent(self):
        self.sender.fail_times = 10
        bridge = self.bridge(FakeMeter(30, 0))
        events = list(bridge.deliver(req()))
        self.assertEqual((events[-1]["type"], events[-1]["browser_seconds"]), ("error", 30))
        # The retry only sends the saved answer: no run, no browser, nothing new to report.
        self.sender.fail_times = 0
        events = list(bridge.deliver(req()))
        self.assertEqual(events[-1], {"type": "turn_done", "message": "", "platform_fault": False})
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_repeated_delivery_sends_the_same_report_again(self):
        bridge = self.bridge(FakeMeter(73))
        first = list(bridge.deliver(req()))[-1]
        again = list(bridge.deliver(req()))[-1]
        self.assertIn("duplicate", again["message"])
        self.assertEqual((again["browser_seconds"], again["browser_report_id"]),
                         (first["browser_seconds"], first["browser_report_id"]))
        self.assertEqual(len(self.runner.calls), 1)
        # After a restart of the Bridge too (the report is saved with the delivery in /data).
        restarted = list(self.bridge(FakeMeter()).deliver(req()))[-1]
        self.assertEqual((restarted["browser_seconds"], restarted["browser_report_id"]), (73, "evt-1#1"))
        # A delivery that used no browser has nothing to repeat.
        list(bridge.deliver(req("evt-2")))
        self.assertNotIn("browser_seconds", list(bridge.deliver(req("evt-2")))[-1])

    def test_the_browser_stays_open_while_another_turn_runs(self):
        meter = FakeMeter(12, 8)
        bridge = self.bridge(meter)
        self.runner.block = threading.Event()
        results = {}

        def consume(name, delivery_id, session):
            results[name] = list(bridge.deliver(req(delivery_id, session_key=session)))

        a = threading.Thread(target=consume, args=("a", "evt-a", "telegram:1"))
        b = threading.Thread(target=consume, args=("b", "evt-b", "web"))
        a.start()
        b.start()
        deadline = time.monotonic() + 2
        while bridge.busy() < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(bridge.busy(), 2)
        self.runner.block.set()
        a.join(5)
        b.join(5)
        # The turn that ended first did not close the browser under the other; the last one did.
        self.assertEqual(sorted(meter.calls), [False, True])
        reports = sorted((r[-1]["browser_seconds"], r[-1]["browser_report_id"]) for r in results.values())
        self.assertEqual([r[0] for r in reports], [8, 12], "each second reported by one turn only")
        self.assertEqual(len({r[1] for r in reports}), 2)

    def test_a_broken_meter_does_not_break_the_turn(self):
        meter = FakeMeter()
        meter.fail = True
        events = list(self.bridge(meter).deliver(req()))
        self.assertEqual(events[-1], {"type": "turn_done", "message": "", "platform_fault": False})
        self.assertEqual(self.sender.sent, [("Hi! How can I help?", "telegram:42")])

    def test_saved_report_fields(self):
        self.assertEqual(browser_fields({"browser": {"browser_seconds": 5, "browser_report_id": "d#1"}}),
                         {"browser_seconds": 5, "browser_report_id": "d#1"})
        for rec in (None, {}, {"browser": None}, {"browser": {"browser_seconds": 0, "browser_report_id": "d#1"}},
                    {"browser": {"browser_seconds": "5", "browser_report_id": "d#1"}},
                    {"browser": {"browser_seconds": True, "browser_report_id": "d#1"}},
                    {"browser": {"browser_seconds": 5}}, {"browser": [5, "d#1"]}):
            self.assertEqual(browser_fields(rec), {}, rec)


REAL_ROOT = os.environ.get("BROWSER_TEST_ROOT", "")


@unittest.skipUnless(REAL_ROOT and shutil.which("node") and browser.installed({"BROWSER_ROOT": REAL_ROOT}),
                     "set BROWSER_TEST_ROOT to a directory with node_modules/@playwright/mcp and ms-playwright")
class RealBrowserTest(unittest.TestCase):
    """The real Playwright MCP server and a real Chromium, through the guard, measured by the real meter:
    the self-check an operator runs in a container (bridge/browser_check.py), without the network."""

    def test_self_check(self):
        # A short directory: Playwright keeps a socket under TMPDIR, and a socket's path is limited.
        data = tempfile.mkdtemp(prefix="b", dir="/tmp")
        self.addCleanup(shutil.rmtree, data, True)
        for name in ("home", "tmp"):
            os.makedirs(os.path.join(data, name))
        env = {"PATH": os.environ.get("PATH", ""), "BROWSER_ROOT": REAL_ROOT, "HOME": os.path.join(data, "home"),
               "TMPDIR": os.path.join(data, "tmp")}
        out = io.StringIO()
        ok = browser_check.run(env, data, url="about:blank", refused="", network=False, out=out)
        self.assertTrue(ok, out.getvalue())
        report = out.getvalue()
        for line in ("ok     it offers Scout's browser tools and no others",
                     "ok     the browser is not started before the first call",
                     'ok     refused: browser_navigate {"url": "file:///etc/passwd"}',
                     "ok     refused: browser_navigate {\"url\": \"view-source:file://",
                     "ok     refused: browser_run_code_unsafe",
                     "ok     the meter sees the browser open",
                     "ok     Chromium runs headless, without its own sandbox, with its profile in /data",
                     "ok     the page can be read (browser_snapshot)",
                     "ok     browser_close closes the browser",
                     "ok     the meter measured the time it was open",
                     "ok     the server ends with its client and leaves no browser behind",
                     "all checks passed"):
            self.assertIn(line, report)
        self.assertNotIn("FAILED", report)
        # The profile is in the data directory, and nothing was written into the workspace.
        self.assertTrue(os.listdir(os.path.join(data, "browser", "profile")))
        self.assertEqual(os.listdir(os.path.join(data, "workspace")), [])


if __name__ == "__main__":
    unittest.main()
