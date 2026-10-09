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


HERMES_CONTEXT_LENGTH = 128_000
HERMES_MIN_CONTEXT_LENGTH = 64_000  # Hermes refuses a model with less (agent/model_metadata.py MINIMUM_CONTEXT_LENGTH)


def hermes_context_length(env: dict[str, str]) -> int:
    """`model.context_length` for Hermes: the total context window of the model behind LiteLLM's name.

    Given, not detected. Without it Hermes asks LiteLLM at every start: `GET /v1/models/agent-default`, which
    LiteLLM refuses to a user's key ("Only proxy admin can be used ...", 401, logged by LiteLLM as an error), then
    `/v1/models` and Ollama's `/api/show`, and falls back to 256,000 tokens ("Could not determine context length
    ... Set model.context_length in config.yaml to override"). The setting is documented in Hermes's
    cli-config.yaml.example ("Set manually only when auto-detection is wrong (e.g. ... a proxy that doesn't expose
    /v1/models)"), and an explicit value is returned before any probe (agent/model_metadata.py
    get_model_context_length, step 0, at the pinned commit).

    `agent-default` is an alias on each user's LiteLLM key: the cheap model on Free (Llama 3.3 70B, a 131,072-token
    window), Claude on Pro and Max, the user's own provider with their own key. 128,000 fits the smallest of them,
    so Hermes compresses a long chat before the cheap model's window is full (256,000 did not).
    BRIDGE_HERMES_CONTEXT_LENGTH overrides it (64,000 at least)."""
    raw = (env.get("BRIDGE_HERMES_CONTEXT_LENGTH") or "").strip()
    try:
        value = int(raw) if raw else HERMES_CONTEXT_LENGTH
    except ValueError:
        return HERMES_CONTEXT_LENGTH
    return value if value >= HERMES_MIN_CONTEXT_LENGTH else HERMES_CONTEXT_LENGTH


# What Hermes's system prompt says about where its answers appear, in place of its built-in hint for the API
# server. The Bridge reaches Hermes only through that API (`/v1/runs`), so every agent it builds has the platform
# `api_server` (gateway/platforms/api_server.py `_create_agent`), and the built-in hint for it asks for plain text
# always: "The rendering layer is unknown — assume plain text. No markdown formatting (no asterisks, bullets,
# headers, code fences)" (agent/prompt_builder.py PLATFORM_HINTS, at the pinned commit). That contradicts the
# lead's instructions for a turn from the web app, which ask for Markdown (core.FORMAT_WEB). The setting is
# Hermes's own: top-level `platform_hints.<platform>.replace` "substitutes the built-in hint entirely"
# (website/docs/developer-guide/prompt-assembly.md "Customizing platform hints"; agent/system_prompt.py
# `_resolve_platform_hint`).
#
# The hint is part of a session's stored system prompt, the same for every turn, so it cannot say Markdown or
# plain text itself: it points to the turn's instructions, which can (Hermes appends them to the system prompt of
# each request, `ephemeral_system_prompt`). The rest keeps what the built-in hint says and still holds here: the
# tone, and that a MEDIA: tag is not turned into a file on the runs endpoint.
# Hermes builds a session's prompt when the session starts and again when it compacts the history; in between it
# reuses the stored one (agent/conversation_loop.py `_restore_or_build_system_prompt`). So a conversation that
# started before this change keeps the built-in hint until then.
HERMES_API_HINT = (
    "You are answering through the platform: it delivers your final answer for a turn to the user, in the web app "
    "or in a messaging app. Which of the two it is, and how to format the answer for it, is said in the "
    "instructions of each turn: follow those. Treat this like a conversation, not a document. Keep responses "
    "brief and natural. Files: nothing here turns a MEDIA:/path tag into a file, it would reach the user as "
    "literal text with a path of this machine in it. So state the plain file path in your answer instead of a "
    "MEDIA: tag."
)


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
    - Hermes's own memory off: the user's memory is the platform's (remember / recall / forget)
    - Hermes's own hint about where its answers appear replaced: how to format an answer is said by the
      lead's instructions for each turn, by the delivery's channel (HERMES_API_HINT, core.format_rules)
    - no runtime installs and no model catalog: Hermes fetches nothing from GitHub, PyPI, Nous or
      OpenRouter when it starts; and the model's context size is given, so it does not probe LiteLLM for it
    - skills and sessions under HERMES_HOME (= /data/hermes)
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
            # The model's context size, given instead of detected (hermes_context_length).
            "context_length": hermes_context_length(env),
        },
        "mcp_servers": servers,
        # No Hermes-side scheduling or outbound messaging: the platform owns both.
        "cron": {"enabled": False},
        # Nor Hermes's built-in browser toolset (toolsets.py "browser": its own browser_* tools).
        "agent": {"disabled_toolsets": ["cronjob", "messaging", "browser"]},
        # The crew: the lead hands work over with delegate_task, one level deep, a few at a time.
        "delegation": crew.hermes_delegation(crew.max_parallel(env)),
        "terminal": {"backend": "local", "cwd": os.path.join(data_dir, "workspace"), "timeout": 180},
        # One memory, the platform's (remember / recall / forget; core.MEMORY_RULES). Hermes's own (MEMORY.md
        # and USER.md under HERMES_HOME/memories, its `memory` tool) is off: with both stores off no store is
        # built, nothing is loaded into the prompt and the tool is left out of the tool list (memory_tool.py
        # check_memory_requirements, agent_init._init_memory at the pinned commit). Sessions, chat history and
        # session_search are separate and unchanged.
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        # Nor does Hermes offer, on a first message, to build a profile of the user in that memory
        # (agent/onboarding.py: "off" never offers).
        "onboarding": {"profile_build": "off"},
        # Hermes's built-in hint for its API server asks for plain text without Markdown, always. How to format
        # an answer depends on where the user reads it and is said per turn (core.format_rules), so the hint is
        # replaced by one that points there (HERMES_API_HINT).
        "platform_hints": {"api_server": {"replace": HERMES_API_HINT}},
        # Hermes downloads and installs nothing at runtime: only what the image pins runs. Without this, the
        # gateway's start-up check (GatewayRunner._init_startup_checks -> tirith_security.ensure_installed) has
        # Hermes's package manager fetch a standalone Python, uv and the tirith command scanner from GitHub and
        # packages from PyPI into HERMES_HOME (about 480 MB, seen in a container with the open web). With it,
        # nothing is fetched: the shell still works (tirith fails open, its default), and a tool that would
        # need an install on first use is refused or left out (likely text_to_speech, whose default engine is
        # an extra this image does not install). Hermes's own Docker image documents the same switch. The Bridge also sets HERMES_DISABLE_LAZY_INSTALLS (hermes.HermesProcess).
        "security": {"allow_lazy_installs": False},
        # No model catalog: the gateway would fetch it 30 s after start and every 20 minutes
        # (hermes-agent.nousresearch.com, redirected to nousresearch.github.io; OpenRouter's model list;
        # Nous Portal's recommended models; hermes_cli/model_catalog.refresh_catalogs). It feeds the /model
        # picker only; the model here is fixed (LiteLLM, the name above), and the context size comes from
        # LiteLLM.
        "model_catalog": {"enabled": False},
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
