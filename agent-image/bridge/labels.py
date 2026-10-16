"""The lead's tool calls as progress lines in plain words, the same on both engines.

The web chat shows each `working` line while the turn runs, and keeps the turn's lines as its steps. Before
2026-10-16 the line was `using <tool name>`: "using Bash", "using run_app_action". Now it says what was done,
in the past tense: "Checked your inbox" for run_app_action with GMAIL_FETCH_EMAILS, "Looked at your apps" for
list_app_actions, "Asked for your OK" for request_approval. A call that is only the engine's own housekeeping
(ToolSearch, a hand-over the crew's own lines already show) gets no line at all.

The tool's name comes in the engine's own form:
- Claude Code: `mcp__<server>__<tool>` for an MCP server's tool, the plain name for a built-in one (`Bash`);
  the call's whole `input` is known (the stream-json tool_use block).
- Hermes: its `tool.started` event names the tool and gives a short `preview` of the call. An MCP tool's name is
  likely `mcp_<server>_<tool>` (the pinned Hermes source could not be checked when this was written), so the
  platform's tool names are matched after an `mcp_<server>_` prefix, and an app action's name (GMAIL_FETCH_EMAILS)
  is looked for in whatever the event carries.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import crew
from .platform import PLATFORM_SERVER_NAME

# The Platform MCP tools (the backend's src/modules/platform-mcp/tools/). An app action's tools are labelled from
# the action they name (action_label).
PLATFORM_LABELS = {
    "list_app_actions": "Looked at your apps",
    "request_approval": "Asked for your OK",
    "get_profile": "Looked at your profile",
    "send_message": "Sent you a message",
    "remember": "Saved it to memory",
    "recall": "Checked your memory",
    "forget": "Removed it from memory",
    "web_search": "Searched the web",
    "read_page": "Read a web page",
    "schedule_reminder": "Set a reminder",
    "create_automation": "Set up an automation",
    "list_automations": "Looked at your automations",
    "cancel_automation": "Cancelled an automation",
    "start_crew_job": "Started a crew job",
}
APP_TOOLS = ("run_app_action", "describe_app_action")
_PLATFORM_TOOLS = tuple(sorted((*PLATFORM_LABELS, *APP_TOOLS), key=len, reverse=True))

# The engines' own tools. Claude Code: Bash, Read, …; Hermes: terminal, read_file, …
BUILTIN_LABELS = {
    "Bash": "Ran a command",
    "BashOutput": "Checked on a command",
    "KillShell": "Stopped a command",
    "Read": "Read a file",
    "Write": "Wrote a file",
    "Edit": "Edited a file",
    "MultiEdit": "Edited a file",
    "NotebookEdit": "Edited a notebook",
    "Glob": "Searched your files",
    "Grep": "Searched your files",
    "LS": "Looked at your files",
    "WebSearch": "Searched the web",
    "WebFetch": "Read a web page",
    "TodoWrite": "Planned the steps",
    "Agent": "Asked a helper",
    "Task": "Asked a helper",
    "terminal": "Ran a command",
    "process": "Checked on a command",
    "execute_code": "Ran some code",
    "read_file": "Read a file",
    "write_file": "Wrote a file",
    "patch": "Edited a file",
    "search_files": "Searched your files",
    "web_extract": "Read a web page",
    "todo": "Planned the steps",
    "vision_analyze": "Looked at an image",
    "session_search": "Looked back through your chats",
}

# Calls that are only the engine's own housekeeping: no line. Claude Code's ToolSearch loads the tools it
# defers; Hermes's delegate_task is the hand-over to the crew, whose own lines ("Scout: …") say who is on it.
SILENT = frozenset({"ToolSearch", "ExitPlanMode", crew.HERMES_DELEGATE_TOOL})

# Scout's browser (crew.BROWSER_TOOLS).
BROWSER_LABELS = {
    "browser_navigate": "Opened a page in the browser",
    "browser_navigate_back": "Went back a page",
    "browser_snapshot": "Looked at the page",
    "browser_find": "Searched the page",
    "browser_click": "Clicked on the page",
    "browser_type": "Typed on the page",
    "browser_fill_form": "Filled in a form",
    "browser_select_option": "Picked an option on the page",
    "browser_press_key": "Pressed a key on the page",
    "browser_hover": "Looked at the page",
    "browser_wait_for": "Waited for the page",
    "browser_handle_dialog": "Answered a pop-up",
    "browser_tabs": "Switched tabs",
    "browser_close": "Closed the browser",
}

USER_TOOL = "Used one of your own tools"
UNKNOWN = "Used a tool"

# Apps by their Composio toolkit, the start of an action's name (GMAIL_SEND_EMAIL -> gmail).
APP_NAMES = {
    "gmail": "Gmail",
    "googlecalendar": "Google Calendar",
    "github": "GitHub",
    "googledrive": "Google Drive",
    "googlesheets": "Google Sheets",
    "googledocs": "Google Docs",
    "slack": "Slack",
    "notion": "Notion",
    "linear": "Linear",
    "outlook": "Outlook",
    "microsoft_teams": "Microsoft Teams",
    "onedrive": "OneDrive",
}

# Words of an action's name that say it only reads (Composio names an action with its verb first, mostly:
# GMAIL_FETCH_EMAILS, GITHUB_LIST_REPOSITORY_ISSUES; sometimes last: GOOGLECALENDAR_EVENTS_LIST).
_READ_WORDS = frozenset({"FETCH", "LIST", "GET", "SEARCH", "FIND", "READ", "RETRIEVE", "QUERY", "LOOKUP", "CHECK",
                         "DESCRIBE", "VIEW", "COUNT", "DOWNLOAD", "EXPORT"})
_CHANGE_WORDS = frozenset({"SEND", "CREATE", "UPDATE", "DELETE", "REMOVE", "ADD", "REPLY", "MOVE", "MERGE", "PATCH",
                           "POST", "PUT", "SET", "INSERT", "EDIT", "MODIFY", "TRASH", "ARCHIVE", "UPLOAD", "APPROVE",
                           "CLOSE", "INVITE", "QUICK", "STAR", "UNSTAR", "MARK", "FORWARD", "SHARE", "COPY"})

_ACTION = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")


def _toolkit(action: str) -> str:
    """The toolkit an action belongs to: the longest known one its name starts with, else its first word."""
    lower = action.lower()
    known = [t for t in APP_NAMES if lower.startswith(t + "_")]
    return max(known, key=len) if known else lower.split("_", 1)[0]


def app_name(action: str) -> str:
    toolkit = _toolkit(action)
    return APP_NAMES.get(toolkit) or toolkit.replace("_", " ").title()


def _reads(words: list[str]) -> bool:
    """True when the action only reads: its first verb-like word is a reading one."""
    for word in words:
        if word in _CHANGE_WORDS:
            return False
        if word in _READ_WORDS:
            return True
    return False


def _gmail(words: set[str], reads: bool) -> Optional[str]:
    if reads:
        if "ATTACHMENT" in words:
            return "Opened an attachment"
        if words & {"PEOPLE", "CONTACT", "CONTACTS"}:
            return "Looked up your contacts"
        if words & {"DRAFT", "DRAFTS"}:
            return "Looked at your drafts"
        if words & {"LABEL", "LABELS", "PROFILE"}:
            return "Looked in Gmail"
        if {"MESSAGE", "ID"} <= words:
            return "Read an email"
        return "Checked your inbox"
    if words & {"TRASH", "DELETE"}:
        return "Deleted an email"
    if words & {"SEND", "FORWARD"}:
        return "Sent an email"
    if "REPLY" in words:
        return "Replied to an email"
    if "DRAFT" in words:
        return "Drafted an email"
    if words & {"LABEL", "LABELS", "MODIFY", "MOVE", "ARCHIVE", "MARK"}:
        return "Sorted your email"
    return None


def _calendar(words: set[str], reads: bool) -> Optional[str]:
    if reads:
        return "Checked your calendar"
    if words & {"DELETE", "REMOVE"}:
        return "Removed an event from your calendar"
    if words & {"CREATE", "QUICK", "INSERT"}:
        return "Added an event to your calendar"
    if words & {"UPDATE", "PATCH", "MOVE"}:
        return "Changed an event in your calendar"
    return None


def _github(words: set[str], reads: bool) -> Optional[str]:
    if reads:
        return "Looked in GitHub"
    pull = {"PULL", "REQUEST"} <= words
    if pull and "MERGE" in words:
        return "Merged a pull request"
    if pull and "REVIEW" in words:
        return "Reviewed a pull request"
    if "COMMENT" in words:
        return "Commented in GitHub"
    if pull and "CREATE" in words:
        return "Opened a pull request"
    if {"ISSUE", "CREATE"} <= words:
        return "Opened an issue"
    return None


_BY_APP = {"gmail": _gmail, "googlecalendar": _calendar, "github": _github}


def action_label(action: str) -> str:
    """An app action in plain words: GMAIL_FETCH_EMAILS -> "Checked your inbox", NOTION_SEARCH -> "Looked in
    Notion", SLACK_SEND_MESSAGE -> "Used Slack"."""
    action = action.upper()
    toolkit = _toolkit(action)
    words = action[len(toolkit) + 1:].split("_") if action.lower().startswith(toolkit + "_") else action.split("_")
    reads = _reads(words)
    special = _BY_APP.get(toolkit)
    said = special(set(words), reads) if special else None
    if said:
        return said
    return f"Looked in {app_name(action)}" if reads else f"Used {app_name(action)}"


def _action_in(*sources: Any) -> str:
    """The app action a call names: the `action` of its input, or a name like GMAIL_FETCH_EMAILS anywhere in what
    the engine says of the call. Empty when none."""
    for src in sources:
        if isinstance(src, str) and src.strip().startswith("{"):
            try:
                src = json.loads(src)
            except ValueError:
                pass
        if isinstance(src, dict):
            named = src.get("action")
            if isinstance(named, str) and _ACTION.fullmatch(named.strip().upper()):
                return named.strip().upper()
            src = json.dumps(src, ensure_ascii=False)
        if isinstance(src, str):
            found = _ACTION.search(src)
            if found:
                return found.group(1)
    return ""


def _split(name: str) -> tuple[str, str]:
    """(server, tool) of an MCP tool's name, or ("", name) for an engine's own tool.

    Claude Code: `mcp__<server>__<tool>`. Hermes (likely): `mcp_<server>_<tool>`, where the server may have an
    underscore itself (scout_browser, user_<id>), so the known servers and the platform's tool names decide."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        return (parts[1], parts[-1]) if len(parts) >= 3 else ("", parts[-1])
    if name.startswith("mcp_"):
        rest = name[4:]
        for server in (PLATFORM_SERVER_NAME, crew.BROWSER_SERVER):
            if rest.startswith(server + "_"):
                return server, rest[len(server) + 1:]
        if rest.startswith("user_"):
            return "user", rest
        for tool in _PLATFORM_TOOLS:
            if rest.endswith("_" + tool):
                return PLATFORM_SERVER_NAME, tool
        return "other", rest
    return "", name


def tool_label(name: Any, args: Any = None, preview: Any = None) -> Optional[str]:
    """The progress line for one of the lead's tool calls, or None when the call gets no line.

    `args`: the call's input when the engine gives it (Claude Code), `preview`: the engine's short text about the
    call (Hermes). Never the raw name of a tool: an unknown one is "Used a tool"."""
    name = str(name or "").strip()
    if not name:
        return UNKNOWN
    server, tool = _split(name)
    if tool in SILENT:
        return None
    if server == crew.BROWSER_SERVER or (server == "" and tool.startswith("browser_")):
        return BROWSER_LABELS.get(tool, "Used the browser")
    if server.startswith("user"):
        return USER_TOOL
    if server in ("", PLATFORM_SERVER_NAME):
        if tool == "run_app_action":
            action = _action_in(args, preview)
            return action_label(action) if action else "Used one of your apps"
        if tool == "describe_app_action":
            action = _action_in(args, preview)
            return f"Looked up how to use {app_name(action)}" if action else "Looked up an app action"
        if tool in PLATFORM_LABELS:
            return PLATFORM_LABELS[tool]
    if server == "":
        return BUILTIN_LABELS.get(tool, UNKNOWN)
    return UNKNOWN
