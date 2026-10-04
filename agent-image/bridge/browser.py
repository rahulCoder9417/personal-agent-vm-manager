"""Scout's browser: a headless Chromium inside the user's own container (a feature of the Max plan).

Three parts:

- The server. The browser is driven through Microsoft's Playwright MCP server (`@playwright/mcp`, pinned in
  agent-image/browser/package.json) with the Chromium build that version of Playwright pins. Both live in
  the image under /opt/browser. The engines start it as an MCP server named `scout_browser` over stdio, behind
  the guard in browser_mcp.py (server_command). Claude Code starts it for each run and it ends with the
  run; Hermes starts it once and keeps it. The server opens the browser at the first tool call, not before.

- Its settings (render_config), written to /data/.platform/browser-mcp.json. What they are for:
    headless, no Chromium sandbox   the container itself is the sandbox (gVisor); Chromium's own sandbox
                                    needs privileges the container does not have
    --disable-dev-shm-usage         shared memory goes to TMPDIR (/data/tmp), not to the small /dev/shm
    --disable-gpu                   there is no GPU; one process less
    profile, cache, output in /data the root filesystem is read-only, and cookies stay in the user's own
                                    volume (/data/browser/profile)
    proxy = the egress proxy        every request, loopback included (Playwright adds
                                    `--proxy-bypass-list=<-loopback>`), goes through the egress proxy, which
                                    refuses internal addresses
    idle timeout 120 s              the server closes the browser after two minutes without a tool call and
                                    opens it again on the next one
    snapshots only when asked       Scout cannot read files, so it asks for the page with browser_snapshot
    no pictures, no page tools      image answers are left out; tools a page registers itself (WebMCP) are
                                    not passed on to the agent

- The meter (BrowserMeter). Browser time is charged to the user per minute, so the Bridge measures it: from
  the moment a browser process exists to the moment none does, found by looking at /proc once a second. At
  the end of a turn the Bridge closes the browser (when no other turn runs) and reports the seconds since
  the last report with the turn's last event. Nothing is reported when the browser never started. The
  measurement is made inside the user's own container by a process the agent could disturb: it is good
  enough to charge an honest user, and it cannot be used to charge another user.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import signal
import sys
import threading
import time
from typing import Any, Callable, Optional

from . import crew

log = logging.getLogger("bridge.browser")

SERVER_NAME = crew.BROWSER_SERVER
ROOT = "/opt/browser"  # the image: node_modules (the server) and ms-playwright (the browser); BROWSER_ROOT overrides
GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_mcp.py")
IDLE_SECONDS = 120  # the server closes the browser after this long without a tool call
OUTPUT_MAX_BYTES = 20 * 1024 * 1024  # old output files (page logs) are removed above this
CACHE_MAX_BYTES = 100 * 1024 * 1024  # Chromium's disk cache
# Chromium flags on top of what Playwright passes itself (see the module text).
CHROMIUM_FLAGS = ("--disable-dev-shm-usage", "--disable-gpu", f"--disk-cache-size={CACHE_MAX_BYTES}")
TOOL_TIMEOUT = 120  # seconds an engine waits for one browser tool call (a navigation may take 60)


def root(env: dict[str, str]) -> str:
    return env.get("BROWSER_ROOT") or ROOT


def server_js(env: dict[str, str]) -> str:
    return os.path.join(root(env), "node_modules", "@playwright", "mcp", "cli.js")


def browsers_dir(env: dict[str, str]) -> str:
    """Where Playwright's browsers are installed (its PLAYWRIGHT_BROWSERS_PATH)."""
    return os.path.join(root(env), "ms-playwright")


def installed(env: dict[str, str]) -> bool:
    """Whether this image has the browser server and a browser."""
    return os.path.isfile(server_js(env)) and os.path.isdir(browsers_dir(env))


def enabled(pf: Any, env: dict[str, str]) -> bool:
    """The browser is offered when the platform turned it on for this user (platform.json
    `browser_enabled`, a Max-plan feature) and the image has it."""
    return bool(getattr(pf, "browser_enabled", False)) and installed(env)


def data_paths(data_dir: str) -> dict[str, str]:
    base = os.path.join(data_dir, "browser")
    return {"profile": os.path.join(base, "profile"), "cache": os.path.join(base, "cache"),
            "output": os.path.join(base, "output"), "config": os.path.join(data_dir, ".platform", "browser-mcp.json")}


def proxy_url(env: dict[str, str]) -> str:
    """The egress proxy, from the container's environment (the vm-manager sets it)."""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if env.get(name):
            return env[name]
    return ""


def render_config(env: dict[str, str], data_dir: str = "/data") -> dict[str, Any]:
    """The Playwright MCP configuration file (its README, "Configuration file"). See the module text for
    what each setting is for."""
    paths = data_paths(data_dir)
    launch: dict[str, Any] = {
        "headless": True,
        "chromiumSandbox": False,
        "args": [*CHROMIUM_FLAGS, f"--disk-cache-dir={paths['cache']}"],
    }
    proxy = proxy_url(env)
    if proxy:
        launch["proxy"] = {"server": proxy}
    return {
        "browser": {"browserName": "chromium", "userDataDir": paths["profile"], "launchOptions": launch},
        "outputDir": paths["output"],
        "outputMaxSize": OUTPUT_MAX_BYTES,
        "snapshot": {"mode": "none"},
        "timeouts": {"idle": IDLE_SECONDS * 1000},
        "imageResponses": "omit",
        "webmcp": False,
        "codegen": "none",
        "allowUnrestrictedFileAccess": False,
    }


def write_config(env: dict[str, str], data_dir: str = "/data") -> str:
    """Write the server's configuration (0600) and make the browser's directories. Returns the file's path."""
    paths = data_paths(data_dir)
    for key in ("profile", "cache", "output"):
        os.makedirs(paths[key], mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(paths["config"]), mode=0o700, exist_ok=True)
    tmp = f"{paths['config']}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(render_config(env, data_dir), fh, indent=2)
        fh.write("\n")
    os.replace(tmp, paths["config"])
    return paths["config"]


def server_command(env: dict[str, str], data_dir: str = "/data") -> list[str]:
    """The command an engine starts as the MCP server `scout_browser`: the guard (browser_mcp.py, run as a file
    in isolated mode), which lets through Scout's browser tools and http(s) addresses only, and behind it
    the Playwright MCP server with the configuration file. What the server needs from the environment is
    passed on the command line, because an engine may start it with a reduced environment (Hermes does)."""
    node = env.get("BROWSER_NODE") or shutil.which("node", path=env.get("PATH")) or "node"
    home = env.get("HOME") or os.path.join(data_dir, "home")
    tmp = env.get("TMPDIR") or os.path.join(data_dir, "tmp")
    return [sys.executable or "python3", "-I", GUARD,
            "--allow", ",".join(crew.BROWSER_TOOLS),
            "--env", f"PLAYWRIGHT_BROWSERS_PATH={browsers_dir(env)}",
            "--env", f"HOME={home}",
            "--env", f"TMPDIR={tmp}",
            "--", node, server_js(env), "--config", data_paths(data_dir)["config"]]


def claude_server(env: dict[str, str], data_dir: str = "/data") -> dict[str, Any]:
    """The `scout_browser` entry of Claude Code's --mcp-config (a stdio server: code.claude.com/docs/en/mcp)."""
    cmd = server_command(env, data_dir)
    return {"type": "stdio", "command": cmd[0], "args": cmd[1:]}


def hermes_server(env: dict[str, str], data_dir: str = "/data") -> dict[str, Any]:
    """The `scout_browser` entry of Hermes's `mcp_servers` (a stdio server: `command` and `args`, as in its
    cli-config.yaml.example)."""
    cmd = server_command(env, data_dir)
    return {"command": cmd[0], "args": cmd[1:], "timeout": TOOL_TIMEOUT, "connect_timeout": 60, "enabled": True}


# --- the meter ----------------------------------------------------------------------------


def list_processes() -> list[tuple[int, list[str]]]:
    """(pid, argv) of every process in the container, from /proc."""
    out: list[tuple[int, list[str]]] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if raw:
            out.append((int(name), [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]))
    return out


def browser_process(argv: list[str], prefix: "str | tuple[str, ...]") -> tuple[bool, bool]:
    """(is a process of a browser from `prefix`, is a browser's main process).

    Chromium rewrites its command line in /proc as one string with spaces, so the words are taken from the
    whole line, whichever way it is split. A helper process carries `--type=…`; the crash handler is not a
    browser. `prefix` is the browser directory with a trailing slash, or several spellings of it."""
    words = " ".join(argv).split()
    if not words or not words[0].startswith(prefix):
        return False, False
    helper = any(word.startswith("--type=") for word in words[1:])
    return True, not helper and "crashpad" not in os.path.basename(words[0])


class BrowserMeter:
    """Measures how long a browser is open in this container, and closes it.

    A browser is a process started from the image's browser directory (browser_process). It is "open"
    while one of them is a browser's main process. The time between two reports is kept in `_pending`
    (closed sessions) plus the session that is still open.
    """

    def __init__(self, browsers: str, poll: float = 1.0, idle_close: float = 15.0,
                 processes: Callable[[], list[tuple[int, list[str]]]] = list_processes,
                 kill: Callable[[int, int], None] = os.kill, clock: Callable[[], float] = time.monotonic,
                 busy: Optional[Callable[[], bool]] = None):
        # The directory as given and with links resolved: the path a process shows in /proc may be either
        # (seen under gVisor: the resolved one).
        self.prefix = tuple(dict.fromkeys(path.rstrip("/") + "/" for path in (browsers, os.path.realpath(browsers))))
        self.poll = poll
        self.idle_close = idle_close  # a browser left open while no turn runs is closed after this long
        self._processes = processes
        self._kill = kill
        self._clock = clock
        self.busy = busy  # whether a turn is in progress (set by the Bridge)
        self._lock = threading.Lock()
        self._open_since: Optional[float] = None
        self._pending = 0.0
        self._unattended_since: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # what is running

    def _browser_pids(self) -> tuple[list[int], list[int]]:
        """(main browser processes, every process of a browser)."""
        main: list[int] = []
        every: list[int] = []
        for pid, argv in self._processes():
            ours, is_main = browser_process(argv, self.prefix)
            if ours:
                every.append(pid)
            if is_main:
                main.append(pid)
        return main, every

    def is_open(self) -> bool:
        return bool(self._browser_pids()[0])

    # measuring

    def sample(self) -> bool:
        """Look once: note when the browser opened or closed. Returns whether it is open."""
        open_now = self.is_open()
        with self._lock:
            now = self._clock()
            if open_now and self._open_since is None:
                self._open_since = now
                log.info("browser opened")
            elif not open_now and self._open_since is not None:
                seconds = now - self._open_since
                self._pending += seconds
                self._open_since = None
                log.info("browser closed after %.0fs", seconds)
        return open_now

    def drain(self) -> int:
        """Whole seconds the browser was open since the last call. A browser that is still open keeps
        counting from now; a part of a second is carried over to the next call, so no second is reported
        twice and none is lost."""
        with self._lock:
            now = self._clock()
            total = self._pending
            if self._open_since is not None:
                total += now - self._open_since
                self._open_since = now
            seconds = int(total)
            self._pending = total - seconds
            return seconds

    # closing

    def close(self, grace: float = 3.0) -> None:
        """Close the browser: ask its main process to end, then kill what is left. The MCP server notices
        and opens a new browser on the next tool call."""
        main, every = self._browser_pids()
        if not every:
            self.sample()
            return
        for pid in main or every:
            self._signal(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not self._browser_pids()[1]:
                break
            time.sleep(0.1)
        for pid in self._browser_pids()[1]:
            self._signal(pid, signal.SIGKILL)
        self.sample()

    def _signal(self, pid: int, sig: int) -> None:
        try:
            self._kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def end_turn(self, last: bool) -> int:
        """A turn is over. `last`: no other turn is running, so the browser is closed (a turn never
        leaves one open). Returns the seconds to report with this turn."""
        if last:
            self.close()
        else:
            self.sample()
        return self.drain()

    # the watcher

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="browser-meter", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        """One round of the watcher: look, and close a browser that is open while no turn runs."""
        open_now = self.sample()
        if not open_now or self.busy is None or self.busy():
            self._unattended_since = None
            return
        now = self._clock()
        if self._unattended_since is None:
            self._unattended_since = now
        elif now - self._unattended_since >= self.idle_close:
            log.warning("browser open with no turn running; closing it")
            self._unattended_since = None
            self.close()

    def _watch(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the watcher must keep running
                log.exception("browser meter")
