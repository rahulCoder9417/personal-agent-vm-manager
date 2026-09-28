"""Container entrypoint: python -m bridge

1. load /data/.platform/platform.json (written by the vm-manager)
2. render $HERMES_HOME/config.yaml from it
3. start Hermes (`hermes gateway run`, API server on 127.0.0.1 only)
4. serve the Bridge HTTP API on 0.0.0.0:$BRIDGE_PORT
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import sys
import threading

from .core import Bridge, DeliveryStore
from .hermes import HermesClient, HermesProcess
from .mcp_client import PlatformMCP
from .platform import HERMES_API_PORT, load_platform_file, render_hermes_config, write_hermes_config
from .server import BridgeHTTPServer

log = logging.getLogger("bridge")


def main() -> int:
    logging.basicConfig(level=os.environ.get("BRIDGE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = dict(os.environ)
    data = env.get("DATA_DIR", "/data")
    hermes_home = env.get("HERMES_HOME", os.path.join(data, "hermes"))
    for d in (hermes_home, env.get("HOME", os.path.join(data, "home")), env.get("TMPDIR", os.path.join(data, "tmp")),
              os.path.join(data, "workspace")):
        os.makedirs(d, mode=0o700, exist_ok=True)

    pf = load_platform_file(env.get("PLATFORM_FILE", os.path.join(data, ".platform", "platform.json")))
    write_hermes_config(render_hermes_config(pf, env, data), hermes_home)
    if pf.timezone:
        env["TZ"] = pf.timezone

    cmd = shlex.split(env.get("HERMES_CMD", "hermes gateway run"))
    hermes = HermesProcess(cmd, env, HERMES_API_PORT)
    hermes.start()
    client = HermesClient(f"http://127.0.0.1:{HERMES_API_PORT}", hermes.api_key,
                          approval_choice=env.get("BRIDGE_HERMES_APPROVAL", "deny"))
    sender = PlatformMCP(env.get("PLATFORM_MCP_URL", "http://api:8080/mcp"), pf.mcp_token)
    store = DeliveryStore(os.path.join(data, ".platform", "deliveries.json"))
    bridge = Bridge(pf.bridge_secret, client, sender, store,
                    turn_timeout=float(env.get("BRIDGE_TURN_TIMEOUT", "900")))

    # /shutdown must answer within the vm-manager's SHUTDOWN_GRACE (20 s): 2 s + 15 s.
    stop_timeout = float(env.get("BRIDGE_HERMES_STOP_TIMEOUT", "15"))
    stop_lock = threading.Lock()
    stopped = threading.Event()

    def stop_hermes() -> None:
        with stop_lock:
            if not stopped.is_set():
                bridge.shutdown(lambda: hermes.stop(stop_timeout), wait_turns=2.0)
                stopped.set()

    port = int(env.get("BRIDGE_PORT", "8765"))
    server = BridgeHTTPServer(("0.0.0.0", port), bridge, stop_hermes)

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
