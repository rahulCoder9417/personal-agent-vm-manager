"""A self-check for Scout's browser, to run inside a container: python -m bridge.browser_check [options]

It does what an engine does: starts the MCP server `scout_browser` exactly as the Bridge configures it (the guard
and, behind it, Playwright MCP with the Bridge's settings file), opens a page, reads it, checks that what
must be refused is refused, closes the browser, and checks that the meter saw it open and close. One line
per check, "ok" or "FAILED"; the exit code is 0 only when every check passed.

    --url URL        the page to open (default https://example.com/); must be reachable through the egress proxy
    --refused URL    an address the egress proxy must refuse (default http://169.254.169.254/, the cloud
                     metadata address); the check passes when the browser does not get a page from it
    --no-network     skip the two checks that need the network (opening --url, the --refused address)

It is a tool for the first run of a new image and for debugging, not part of a turn. Standard library only.
It uses the user's own browser profile in /data, so run it while no turn uses the browser.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Optional

from . import browser, crew


class StdioClient:
    """A minimal MCP client over a server's stdin/stdout (one JSON-RPC message per line)."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Optional[str] = None):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=cwd)
        self._id = 0
        self._lock = threading.Lock()

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 120) -> dict:
        with self._lock:
            self._id += 1
            want = self._id
            self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params or {}})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("the browser server closed its output")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == want and ("result" in msg or "error" in msg):
                    return msg
                if "method" in msg and "id" in msg:  # a request from the server (roots/list, ping, ...)
                    result = {"roots": []} if msg["method"] == "roots/list" else {}
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            raise TimeoutError(method)

    def initialize(self) -> dict:
        answer = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "browser-check", "version": "1"}}, timeout=60)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return answer.get("result") or {}

    def tool(self, name: str, arguments: Optional[dict] = None, timeout: float = 120) -> tuple[bool, str]:
        """(ok, text) of one tool call; ok is False for a tool error or a protocol error."""
        answer = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        if "error" in answer:
            return False, json.dumps(answer["error"])
        result = answer.get("result") or {}
        text = "\n".join(str(c.get("text", "")) for c in result.get("content") or [] if isinstance(c, dict))
        return not result.get("isError"), text

    def close(self, timeout: float = 20) -> Optional[int]:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
            return None
        finally:
            try:
                self.proc.stdout.close()
            except OSError:
                pass


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def run(env: dict[str, str], data_dir: str, url: str, refused: str, network: bool = True,
        out: Any = sys.stdout) -> bool:
    """Run the checks. Returns True when all passed."""
    failed: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        out.write(f"{'ok    ' if ok else 'FAILED'} {name}" + (f": {_short(detail)}" if detail else "") + "\n")
        out.flush()
        if not ok:
            failed.append(name)
        return ok

    if not check("the image has the browser server and a browser", browser.installed(env),
                 f"{browser.server_js(env)} and {browser.browsers_dir(env)}"):
        return False
    config = browser.write_config(env, data_dir)
    command = browser.server_command(env, data_dir)
    check("the settings file is written", os.path.isfile(config), config)
    meter = browser.BrowserMeter(browser.browsers_dir(env))
    if meter.is_open():
        check("no browser is open before the check", False, "a turn may be using it; run this again later")
        return False
    workdir = os.path.join(data_dir, "workspace")
    os.makedirs(workdir, mode=0o700, exist_ok=True)
    client = StdioClient(command, env, cwd=workdir)
    try:
        info = client.initialize()
        check("the server answers", bool(info.get("serverInfo")), json.dumps(info.get("serverInfo")))
        tools = [t.get("name") for t in (client.request("tools/list").get("result") or {}).get("tools") or []]
        check("it offers Scout's browser tools and no others", sorted(tools) == sorted(crew.BROWSER_TOOLS),
              ", ".join(sorted(set(tools) ^ set(crew.BROWSER_TOOLS))) or f"{len(tools)} tools")
        check("the browser is not started before the first call", not meter.sample())

        for name, arguments in (("browser_navigate", {"url": "file:///etc/passwd"}),
                                ("browser_navigate", {"url": "view-source:file://" + config}),
                                ("browser_tabs", {"action": "new", "url": "view-source:file:///etc/passwd"}),
                                ("browser_run_code_unsafe", {"code": "async (page) => 1"}),
                                ("browser_file_upload", {"paths": [config]})):
            ok, text = client.tool(name, arguments)
            check(f"refused: {name} {json.dumps(arguments)}", not ok and not meter.sample(), text)

        if network:
            ok, text = client.tool("browser_navigate", {"url": url})
            check(f"a page opens: {url}", ok, text)
        else:
            ok, text = client.tool("browser_navigate", {"url": "about:blank"})
            check("the browser starts (about:blank)", ok, text)
        opened = meter.sample()
        check("the meter sees the browser open", opened)
        if opened:
            argv = next((" ".join(a).split() for _, a in browser.list_processes()
                         if browser.browser_process(a, meter.prefix)[1]), [])
            flags = [a for a in argv if a.split("=")[0] in ("--headless", "--no-sandbox", "--disable-dev-shm-usage",
                                                            "--disable-gpu", "--user-data-dir", "--proxy-server",
                                                            "--proxy-bypass-list", "--disk-cache-dir")]
            check("Chromium runs headless, without its own sandbox, with its profile in /data",
                  "--headless" in flags and "--no-sandbox" in flags
                  and f"--user-data-dir={browser.data_paths(data_dir)['profile']}" in flags, " ".join(flags))
            missing = [f for f in browser.NO_BACKGROUND_FLAGS if f not in argv]
            check("no background networking and no component updates (" + ", ".join(browser.NO_BACKGROUND_FLAGS)
                  + ")", not missing, "missing: " + " ".join(missing) if missing else "")
            proxy = browser.proxy_url(env)
            if proxy:
                check("every request goes through the egress proxy, loopback included",
                      f"--proxy-server={proxy}" in flags and "--proxy-bypass-list=<-loopback>" in flags, proxy)
        ok, text = client.tool("browser_snapshot")
        check("the page can be read (browser_snapshot)", ok and "Snapshot" in text, text)
        if network:
            ok, text = client.tool("browser_navigate", {"url": refused})
            ok2, page = client.tool("browser_snapshot")
            check(f"the egress proxy refuses {refused}", (not ok) or "chrome-error" in page or "403" in page
                  or "ERR_" in text or "Access Denied" in page, text if not ok else page)
        time.sleep(1.1)
        ok, text = client.tool("browser_close")
        time.sleep(1.0)
        check("browser_close closes the browser", ok and not meter.sample(), text)
        seconds = meter.drain()
        check("the meter measured the time it was open", seconds >= 1, f"{seconds} s")
        check("nothing is counted twice", meter.drain() == 0)
    except Exception as exc:  # noqa: BLE001 - report it as a failed check
        check("the check ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        code = client.close()
        time.sleep(0.5)
        check("the server ends with its client and leaves no browser behind", code == 0 and not meter.is_open(),
              f"exit code {code}")
        meter.close(grace=1.0)
    out.write(("all checks passed" if not failed else f"{len(failed)} check(s) FAILED") + "\n")
    return not failed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bridge.browser_check", description="Self-check of Scout's browser.")
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--refused", default="http://169.254.169.254/")
    parser.add_argument("--no-network", action="store_true")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    data_dir = env.get("DATA_DIR", "/data")
    return 0 if run(env, data_dir, args.url, args.refused, network=not args.no_network) else 1


if __name__ == "__main__":
    sys.exit(main())
