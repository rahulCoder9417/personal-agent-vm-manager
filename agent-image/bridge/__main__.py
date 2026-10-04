"""Container entrypoint: python -m bridge

1. load /data/.platform/platform.json (written by the vm-manager)
2. render $HERMES_HOME/config.yaml from it
3. start Hermes (`hermes gateway run`, API server on 127.0.0.1 only)
4. set up Claude Code (the official `claude` CLI, config dir on /data) as the second runtime
5. when Scout's browser is turned on for the user (platform.json "browser_enabled"): write the browser
   server's settings and start the meter that measures browser time
6. serve the Bridge HTTP API on 0.0.0.0:$BRIDGE_PORT

Each delivery goes to the runtime named in platform.json ("runtime": hermes | claude_code).
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import sys
import threading

from . import browser, crew
from .claude import RUNTIME_CLAUDE, RUNTIME_HERMES, ClaudeAuth, ClaudeRunner
from .core import Bridge, DeliveryStore
from .hermes import HermesClient, HermesProcess
from .mcp_client import PlatformMCP
from .platform import (HERMES_API_PORT, load_platform_file, render_gitconfig, render_hermes_config, write_gitconfig,
                       write_hermes_config)
from .runtime import PlatformReader, RuntimeRunner
from .server import BridgeHTTPServer

log = logging.getLogger("bridge")


def main() -> int:
    logging.basicConfig(level=os.environ.get("BRIDGE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = dict(os.environ)
    data = env.get("DATA_DIR", "/data")
    hermes_home = env.get("HERMES_HOME", os.path.join(data, "hermes"))
    env.setdefault("CLAUDE_CONFIG_DIR", os.path.join(data, "claude"))
    for d in (hermes_home, env.get("HOME", os.path.join(data, "home")), env.get("TMPDIR", os.path.join(data, "tmp")),
              os.path.join(data, "workspace"), env["CLAUDE_CONFIG_DIR"]):
        os.makedirs(d, mode=0o700, exist_ok=True)

    platform_path = env.get("PLATFORM_FILE", os.path.join(data, ".platform", "platform.json"))
    pf = load_platform_file(platform_path)
    write_hermes_config(render_hermes_config(pf, env, data), hermes_home)
    write_gitconfig(render_gitconfig(pf, env), data, env.get("HOME", os.path.join(data, "home")))
    if pf.timezone:
        env["TZ"] = pf.timezone

    # Scout's browser: on when the platform turned it on for this user and the image has it. Decided once,
    # here: the vm-manager re-creates the container when the switch changes. The settings file must be
    # there before Hermes starts the browser's server.
    with_browser = browser.enabled(pf, env)
    if pf.browser_enabled and not with_browser:
        log.warning("the browser is turned on for this user, but this image has none")
    meter = None
    if with_browser:
        browser.write_config(env, data)
        meter = browser.BrowserMeter(browser.browsers_dir(env))

    cmd = shlex.split(env.get("HERMES_CMD", "hermes gateway run"))
    hermes = HermesProcess(cmd, env, HERMES_API_PORT)
    hermes.start()
    client = HermesClient(f"http://127.0.0.1:{HERMES_API_PORT}", hermes.api_key,
                          approval_choice=env.get("BRIDGE_HERMES_APPROVAL", "deny"), with_browser=with_browser)
    reader = PlatformReader(platform_path, pf)
    claude_cmd = shlex.split(env.get("CLAUDE_CMD", "claude"))
    claude_auth = ClaudeAuth(claude_cmd, env)
    claude = ClaudeRunner(claude_cmd, env, data, reader.load, auth=claude_auth,
                          model=env.get("BRIDGE_CLAUDE_MODEL", ""),
                          max_turns=int(env.get("BRIDGE_CLAUDE_MAX_TURNS", "0") or 0),
                          crew_model=env.get("BRIDGE_CLAUDE_CREW_MODEL", ""),
                          crew_parallel=crew.max_parallel(env), with_browser=with_browser)
    runner = RuntimeRunner(reader.load, {RUNTIME_HERMES: client, RUNTIME_CLAUDE: claude})
    sender = PlatformMCP(env.get("PLATFORM_MCP_URL", "http://api:8080/mcp"), pf.mcp_token)
    store = DeliveryStore(os.path.join(data, ".platform", "deliveries.json"))
    bridge = Bridge(pf.bridge_secret, runner, sender, store,
                    turn_timeout=float(env.get("BRIDGE_TURN_TIMEOUT", "900")), browser=meter)
    if meter is not None:
        meter.busy = lambda: bridge.busy() > 0  # a browser left open with no turn running is closed
        meter.start()

    def health_extra() -> dict:
        return {"runtime": runner.runtime(), "claude_connected": claude_auth.status(),
                "claude_login_pending": claude_auth.login_pending(), "browser": with_browser}

    # /shutdown must answer within the vm-manager's SHUTDOWN_GRACE (20 s): 2 s + 15 s.
    stop_timeout = float(env.get("BRIDGE_HERMES_STOP_TIMEOUT", "15"))
    stop_lock = threading.Lock()
    stopped = threading.Event()

    def stop_hermes() -> None:
        with stop_lock:
            if not stopped.is_set():
                claude_auth.shutdown()
                bridge.shutdown(lambda: hermes.stop(stop_timeout), wait_turns=2.0)
                if meter is not None:
                    meter.stop()
                    meter.close(grace=1.0)
                stopped.set()

    port = int(env.get("BRIDGE_PORT", "8765"))
    server = BridgeHTTPServer(("0.0.0.0", port), bridge, stop_hermes, claude=claude_auth, health_extra=health_extra)

    def on_signal(signum, _frame) -> None:
        log.info("signal %s: stopping", signum)
        threading.Thread(target=lambda: (stop_hermes(), server.shutdown()), daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("bridge listening on :%d for user %s", port, pf.user_id)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
