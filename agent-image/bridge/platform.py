"""platform.json (written by the vm-manager into /data) -> Hermes config.yaml.

The Hermes config is rendered at every boot from the secrets file, so a new
agent image can change the config format without touching the vm-manager.
JSON is valid YAML, so config.yaml is written as JSON (no YAML dependency).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import browser, crew

HERMES_API_HOST = "127.0.0.1"
HERMES_API_PORT = 8642
PLATFORM_SERVER_NAME = "platform"


@dataclass
class MCPServer:
    id: str
    name: str
    url: str
    auth_header_name: str = ""
    auth_secret: str = ""


@dataclass
class PlatformFile:
    user_id: str
    mcp_token: str
    bridge_secret: str
    litellm_key: str
    timezone: str = ""
    mcp_servers: list[MCPServer] = field(default_factory=list)
    # Which agent answers: "hermes" (default) or "claude_code". Read again on every delivery.
    runtime: str = "hermes"
    # Scout's browser is turned on for this user (a Max-plan feature). The vm-manager writes it when it
    # wakes the container, with the container's memory limit and network rule, so the Bridge reads it once,
    # when it boots.
    browser_enabled: bool = False


def load_platform_file(path: str) -> PlatformFile:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    for key in ("user_id", "mcp_token", "bridge_secret", "litellm_key"):
        if not raw.get(key):
            raise ValueError(f"platform file: missing {key}")
    servers = [
        MCPServer(
            id=str(s.get("id", "")),
            name=str(s.get("name", "")),
            url=str(s.get("url", "")),
            auth_header_name=str(s.get("auth_header_name", "") or ""),
            auth_secret=str(s.get("auth_secret", "") or ""),
        )
        for s in raw.get("mcp_servers") or []
    ]
    return PlatformFile(
        user_id=raw["user_id"],
        mcp_token=raw["mcp_token"],
        bridge_secret=raw["bridge_secret"],
        litellm_key=raw["litellm_key"],
        timezone=raw.get("timezone") or "",
        mcp_servers=servers,
        runtime=str(raw.get("runtime") or "hermes"),
        browser_enabled=raw.get("browser_enabled") is True,
    )


def server_key(server: MCPServer) -> str:
    safe = re.sub(r"[^a-z0-9_]", "_", server.id.lower())[:40] or "server"
    return f"user_{safe}"


def render_hermes_config(pf: PlatformFile, env: dict[str, str], data_dir: str = "/data") -> dict[str, Any]:
    """Build the Hermes config dict.

    - model: LiteLLM (OpenAI-compatible) with this user's key; reached through the egress proxy
    - MCP: the Platform MCP (with this container's token; `send_message` hidden because the
      Bridge sends the final answer itself) + the user's own MCP servers + the browser's server when
      the browser is turned on for the user (bridge/browser.py; a stdio server Hermes starts itself)
    - Hermes's own chat connectors and cron off (only the local API server runs), and Hermes's own
      built-in browser tools off: the only browser is Scout's, behind the guard, and only when it is
      turned on for the user
    - delegation (the crew): children cannot delegate, BRIDGE_CREW_MAX_PARALLEL at a time (default 3)
    - memory, skills and sessions under HERMES_HOME (= /data/hermes)
    """
    base = env.get("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    servers: dict[str, Any] = {
        PLATFORM_SERVER_NAME: {
            "url": env.get("PLATFORM_MCP_URL", "http://api:8080/mcp"),
            "headers": {"Authorization": f"Bearer {pf.mcp_token}"},
            "timeout": 120,
            "connect_timeout": 30,
            "enabled": True,
            "tools": {"exclude": ["send_message"]},
        }
    }
    for s in pf.mcp_servers:
        if not s.url:
            continue
        entry: dict[str, Any] = {"url": s.url, "timeout": 120, "connect_timeout": 30, "enabled": True}
        if s.auth_header_name and s.auth_secret:
            entry["headers"] = {s.auth_header_name: s.auth_secret}
        servers[server_key(s)] = entry
    if browser.enabled(pf, env):
        servers[browser.SERVER_NAME] = browser.hermes_server(env, data_dir)

    cfg: dict[str, Any] = {
        "model": {
            "provider": "custom",
            "base_url": base,
            "api_key": pf.litellm_key,
            "default": env.get("LITELLM_MODEL", "agent-default"),
        },
        "mcp_servers": servers,
        # No Hermes-side scheduling or outbound messaging: the platform owns both.
        "cron": {"enabled": False},
        # Nor Hermes's built-in browser toolset (toolsets.py "browser": its own browser_* tools).
        "agent": {"disabled_toolsets": ["cronjob", "messaging", "browser"]},
        # The crew: the lead hands work over with delegate_task, one level deep, a few at a time.
        "delegation": crew.hermes_delegation(crew.max_parallel(env)),
        "terminal": {"backend": "local", "cwd": os.path.join(data_dir, "workspace"), "timeout": 180},
        "memory": {"memory_enabled": True, "user_profile_enabled": True},
        # The only "platform" Hermes runs: its local API server, loopback only.
        "gateway": {
            "api_server": {
                "enabled": True,
                "host": HERMES_API_HOST,
                "port": HERMES_API_PORT,
                "max_concurrent_runs": 4,
            }
        },
    }
    if pf.timezone:
        cfg["timezone"] = pf.timezone
    return cfg


def write_hermes_config(cfg: dict[str, Any], hermes_home: str) -> str:
    os.makedirs(hermes_home, mode=0o700, exist_ok=True)
    path = os.path.join(hermes_home, "config.yaml")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


GIT_USER_NAME = "Personal Agent"
GIT_USER_EMAIL = "agent@users.noreply.github.com"


def git_gateway_url(env: dict[str, str]) -> str:
    """The api's git gateway, next to the Platform MCP: http://api:8080/mcp -> http://api:8080/git/."""
    mcp = env.get("PLATFORM_MCP_URL", "http://api:8080/mcp").rstrip("/")
    base = mcp[: -len("/mcp")] if mcp.endswith("/mcp") else mcp
    return base + "/git/"


def render_gitconfig(pf: PlatformFile, env: dict[str, str]) -> str:
    """Git config that sends github.com through the platform's git gateway.

    The container never holds a GitHub token: git talks to the gateway with this container's
    own token, and the api adds the user's GitHub credentials on its side.
    """
    gateway = git_gateway_url(env)
    return (
        "# Written by the platform at every start. Do not edit: changes are overwritten.\n"
        f'[url "{gateway}"]\n'
        "\tinsteadOf = https://github.com/\n"
        "\tinsteadOf = http://github.com/\n"
        "\tinsteadOf = git@github.com:\n"
        "\tinsteadOf = ssh://git@github.com/\n"
        f'[http "{gateway}"]\n'
        f"\textraHeader = Authorization: Bearer {pf.mcp_token}\n"
        "[user]\n"
        f"\tname = {GIT_USER_NAME}\n"
        f"\temail = {GIT_USER_EMAIL}\n"
        "[init]\n"
        "\tdefaultBranch = main\n"
    )


def write_gitconfig(content: str, data_dir: str, home: str) -> str:
    """Writes the managed config to /data/.platform/gitconfig and includes it from ~/.gitconfig.

    The include goes first, so anything the agent sets itself in ~/.gitconfig (e.g. user.name)
    still wins.
    """
    path = os.path.join(data_dir, ".platform", "gitconfig")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)

    os.makedirs(home, mode=0o700, exist_ok=True)
    user_path = os.path.join(home, ".gitconfig")
    include = f"[include]\n\tpath = {path}\n"
    try:
        with open(user_path, encoding="utf-8") as fh:
            current = fh.read()
    except FileNotFoundError:
        current = ""
    if f"path = {path}" not in current:
        with open(user_path, "w", encoding="utf-8") as fh:
            fh.write(include + current)
    return path
