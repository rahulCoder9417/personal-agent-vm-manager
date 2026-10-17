"""A Claude Code `PreToolUse` hook: the lead's own calls to Scout's web tools are refused.

The web is Scout's alone (crew.py): the built-in search, the platform's `web_search` and `read_page`, and
the tools of the browser in the container. On Claude Code the lead and its sub-agents are one session, and a deny rule of the
session would take a tool from Scout too. So the session keeps these tools, and this hook refuses them
when the lead itself calls one.

How Claude Code runs it (code.claude.com/docs/en/hooks, read again on 2026-10-04):

- When: before a tool call whose name matches the hook's matcher, in the main conversation and inside a
  sub-agent alike ("Hook locations": hooks from settings files "also run inside subagents").
- Input: one JSON object on stdin ("Common input fields", "PreToolUse input"). `tool_name` is the tool.
  `agent_id` is "present only when the hook fires inside a subagent call. Use this to distinguish subagent
  hook calls from main-thread calls", so a call without it is the lead's. `agent_type` (the sub-agent's
  name) comes with it; it would also be there in a session started with `--agent`, which the Bridge never
  does, so either field marks a sub-agent here.
- Refusing ("Exit code 2", "PreToolUse decision control"): exit code 2 "blocks the tool call", whatever
  is on stdout, and "Claude sees the stderr message as the denial reason". So the gate prints nothing on
  stdout, writes the reason to stderr and exits 2.
- Passing: exit code 0 with nothing on stdout decides nothing, and the session's permission rules apply
  as usual.

Usage: python -I lead_gate.py <tool name>...   (the tools it guards; a call to any other tool passes)

It is run as a file, on its own: standard library only, nothing imported from the Bridge. Whatever it
cannot read passes, so a broken hook never takes the web from Scout; the lead's instructions still say
that the web and the browser are Scout's. The same goes for a hook the CLI cannot start or that runs into
its timeout: per the docs neither blocks the call.
"""

import json
import sys

REFUSAL = ("You have no access to the web or the browser. Hand this job to Scout now, without explaining to "
           "the user whose job it is and without asking them first: use the Agent tool with subagent_type "
           "scout and put what you need to know in the task, then report what Scout found.")
BLOCK, PASS = 2, 0  # the hook's exit codes: 2 blocks the tool call, 0 leaves it to the permission rules


def refuses(event: object, guarded: list[str]) -> bool:
    """True when this is the lead itself calling a guarded tool."""
    if not isinstance(event, dict) or event.get("tool_name") not in guarded:
        return False
    return not (event.get("agent_id") or event.get("agent_type"))  # there only inside a sub-agent


def main(argv: list[str]) -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return PASS
    if not refuses(event, argv):
        return PASS
    sys.stderr.write(REFUSAL + "\n")
    return BLOCK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
