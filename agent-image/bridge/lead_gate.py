"""A Claude Code `PreToolUse` hook: the lead's own calls to Scout's web tools are refused.

The web is Scout's alone (crew.py). On Claude Code the lead and its sub-agents are one session, and a
deny rule of the session would take a tool from Scout too. So the session keeps the web tools, and this
hook refuses them when the lead itself calls one.

How Claude Code runs it (code.claude.com/docs/en/hooks): before a tool call whose name matches the hook's
matcher, with the call as JSON on stdin. `agent_id` is in that JSON "only when the hook fires inside a
subagent call", so a call without it is the lead's. `agent_type` (the sub-agent's name) comes with it; it
would also be there in a session started with `--agent`, which the Bridge never does, so either field
marks a sub-agent here. Exit code 2 blocks the call and shows stderr to the model. Exit code 0 decides
nothing: the session's permission rules apply as usual.

Usage: python -I lead_gate.py <tool name>...   (the tools it guards; a call to any other tool passes)

It is run as a file, on its own: standard library only, nothing imported from the Bridge. Whatever it
cannot read passes, so a broken hook never takes the web from Scout; the lead's instructions still say
that the web is Scout's.
"""

import json
import sys

REFUSAL = ("You have no access to the web. Hand this to Scout: use the Agent tool with subagent_type scout and "
           "put what you need to know in the task.")


def refuses(event: object, guarded: list[str]) -> bool:
    """True when this is the lead itself calling a guarded tool."""
    if not isinstance(event, dict) or event.get("tool_name") not in guarded:
        return False
    return not (event.get("agent_id") or event.get("agent_type"))  # there only inside a sub-agent


def main(argv: list[str]) -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not refuses(event, argv):
        return 0
    sys.stderr.write(REFUSAL + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
