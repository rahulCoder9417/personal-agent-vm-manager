"""The lead's tool calls as progress lines in plain words (bridge/labels.py), the same on both engines.

2026-10-15: the web chat showed "using Bash" and "using run_app_action" while Tex checked the founder's email. The
line now says what was done ("Checked your inbox"), never a tool's raw name."""

import json
import re
import unittest

from bridge import crew, labels
from bridge.labels import action_label, tool_label

RAW_NAME = re.compile(r"\busing\b|_|mcp|[a-z][A-Z]")


class ToolLabelTest(unittest.TestCase):
    def test_the_founders_examples_on_claude_code(self):
        # Claude Code names an MCP tool mcp__<server>__<tool> and gives the call's whole input.
        self.assertEqual(tool_label("mcp__platform__run_app_action",
                                    {"action": "GMAIL_FETCH_EMAILS", "arguments": {"max_results": 10}}),
                         "Checked your inbox")
        self.assertEqual(tool_label("mcp__platform__list_app_actions", {}), "Looked at your apps")
        self.assertEqual(tool_label("mcp__platform__request_approval",
                                    {"summary": "Send an email", "action": "GMAIL_SEND_EMAIL"}), "Asked for your OK")

    def test_the_same_on_hermes(self):
        # Hermes's tool.started event: the tool's name (an MCP tool most likely as mcp_<server>_<tool>) and a short
        # preview of the call, which may be the action, or JSON cut short.
        cut = json.dumps({"action": "GMAIL_FETCH_EMAILS", "arguments": {"query": "is:unread"}})[:30]
        for name in ("mcp_platform_run_app_action", "run_app_action", "mcp__platform__run_app_action"):
            self.assertEqual(tool_label(name, None, "GMAIL_FETCH_EMAILS"), "Checked your inbox", name)
            self.assertEqual(tool_label(name, None, cut), "Checked your inbox", name)
            self.assertEqual(tool_label(name, json.dumps({"action": "GMAIL_SEND_EMAIL"})), "Sent an email", name)
        self.assertEqual(tool_label("mcp_platform_list_app_actions", None, ""), "Looked at your apps")
        self.assertEqual(tool_label("mcp_platform_request_approval", None, "Send an email"), "Asked for your OK")
        # A server name Hermes may write differently still finds the platform's tool by its name.
        self.assertEqual(tool_label("mcp_textcrew_platform_get_profile"), "Looked at your profile")
        # The hand-over to the crew gets no line: the crew's own lines ("Scout: …") say who is on what.
        self.assertIsNone(tool_label(crew.HERMES_DELEGATE_TOOL, None, "2 tasks"))
        for name, label in (("terminal", "Ran a command"), ("read_file", "Read a file"), ("patch", "Edited a file"),
                            ("search_files", "Searched your files"), ("web_search", "Searched the web")):
            self.assertEqual(tool_label(name, None, "x"), label)

    def test_every_platform_tool_has_words(self):
        for tool, label in labels.PLATFORM_LABELS.items():
            self.assertEqual(tool_label(f"mcp__platform__{tool}", {}), label)
            self.assertEqual(tool_label(f"mcp_platform_{tool}", None, ""), label)
        self.assertEqual(tool_label("mcp__platform__describe_app_action", {"action": "GMAIL_SEND_EMAIL"}),
                         "Looked up how to use Gmail")
        self.assertEqual(tool_label("mcp__platform__describe_app_action", {}), "Looked up an app action")
        self.assertEqual(tool_label("mcp__platform__run_app_action", {}), "Used one of your apps")
        self.assertEqual(tool_label("mcp__platform__send_message", {"text": "hi"}), "Sent you a message")
        self.assertEqual(tool_label("mcp__platform__remember", {}), "Saved it to memory")
        self.assertEqual(tool_label("mcp__platform__start_crew_job", {}), "Started a crew job")

    def test_claude_codes_own_tools(self):
        for name, label in (("Bash", "Ran a command"), ("Read", "Read a file"), ("Write", "Wrote a file"),
                            ("Edit", "Edited a file"), ("MultiEdit", "Edited a file"), ("Glob", "Searched your files"),
                            ("Grep", "Searched your files"), ("WebSearch", "Searched the web"),
                            ("WebFetch", "Read a web page"), ("TodoWrite", "Planned the steps"),
                            ("Agent", "Asked a helper")):
            self.assertEqual(tool_label(name, {}), label)
        # Loading a deferred tool is the engine's housekeeping: no line at all.
        self.assertIsNone(tool_label("ToolSearch", {"query": "select:mcp__platform__run_app_action"}))

    def test_the_browser_and_the_users_own_servers(self):
        for tool in crew.BROWSER_TOOLS:
            for name in (f"mcp__{crew.BROWSER_SERVER}__{tool}", f"mcp_{crew.BROWSER_SERVER}_{tool}"):
                label = tool_label(name, {})
                self.assertTrue(label and not RAW_NAME.search(label), (name, label))
        self.assertEqual(tool_label("mcp__scout_browser__browser_navigate", {"url": "https://x"}),
                         "Opened a page in the browser")
        self.assertEqual(tool_label("mcp__user_srv_1__lookup", {}), "Used one of your own tools")
        self.assertEqual(tool_label("mcp_user_srv_1_lookup", None, "x"), "Used one of your own tools")

    def test_never_a_raw_name(self):
        for name in ("SomethingNew", "mcp__other__thing", "mcp_other_thing", "", None):
            self.assertEqual(tool_label(name, {}), "Used a tool", name)


class ActionLabelTest(unittest.TestCase):
    def test_gmail(self):
        for action, label in (("GMAIL_FETCH_EMAILS", "Checked your inbox"),
                              ("GMAIL_LIST_THREADS", "Checked your inbox"),
                              ("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", "Read an email"),
                              ("GMAIL_GET_ATTACHMENT", "Opened an attachment"),
                              ("GMAIL_SEARCH_PEOPLE", "Looked up your contacts"),
                              ("GMAIL_LIST_DRAFTS", "Looked at your drafts"),
                              ("GMAIL_SEND_EMAIL", "Sent an email"),
                              ("GMAIL_SEND_DRAFT", "Sent an email"),
                              ("GMAIL_REPLY_TO_THREAD", "Replied to an email"),
                              ("GMAIL_CREATE_EMAIL_DRAFT", "Drafted an email"),
                              ("GMAIL_DELETE_DRAFT", "Deleted an email"),
                              ("GMAIL_MOVE_TO_TRASH", "Deleted an email"),
                              ("GMAIL_ADD_LABEL_TO_EMAIL", "Sorted your email"),
                              ("GMAIL_MARK_AS_READ", "Sorted your email")):
            self.assertEqual(action_label(action), label, action)

    def test_google_calendar(self):
        for action, label in (("GOOGLECALENDAR_EVENTS_LIST", "Checked your calendar"),
                              ("GOOGLECALENDAR_FIND_EVENT", "Checked your calendar"),
                              ("GOOGLECALENDAR_FIND_FREE_SLOTS", "Checked your calendar"),
                              ("GOOGLECALENDAR_CREATE_EVENT", "Added an event to your calendar"),
                              ("GOOGLECALENDAR_QUICK_ADD", "Added an event to your calendar"),
                              ("GOOGLECALENDAR_UPDATE_EVENT", "Changed an event in your calendar"),
                              ("GOOGLECALENDAR_DELETE_EVENT", "Removed an event from your calendar")):
            self.assertEqual(action_label(action), label, action)

    def test_github(self):
        for action, label in (("GITHUB_LIST_REPOSITORY_ISSUES", "Looked in GitHub"),
                              ("GITHUB_GET_A_PULL_REQUEST", "Looked in GitHub"),
                              ("GITHUB_CREATE_A_PULL_REQUEST", "Opened a pull request"),
                              ("GITHUB_CREATE_AN_ISSUE", "Opened an issue"),
                              ("GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST", "Reviewed a pull request"),
                              ("GITHUB_CREATE_AN_ISSUE_COMMENT", "Commented in GitHub"),
                              ("GITHUB_MERGE_A_PULL_REQUEST", "Merged a pull request")):
            self.assertEqual(action_label(action), label, action)

    def test_any_other_app(self):
        for action, label in (("NOTION_SEARCH_NOTION_PAGE", "Looked in Notion"),
                              ("SLACK_SEND_MESSAGE", "Used Slack"),
                              ("GOOGLEDRIVE_FIND_FILE", "Looked in Google Drive"),
                              ("GOOGLESHEETS_BATCH_UPDATE", "Used Google Sheets"),
                              ("MICROSOFT_TEAMS_LIST_CHATS", "Looked in Microsoft Teams"),
                              ("ONEDRIVE_UPLOAD_FILE", "Used OneDrive"),
                              ("TRELLO_GET_BOARDS", "Looked in Trello"),
                              ("gmail_fetch_emails", "Checked your inbox")):
            self.assertEqual(action_label(action), label, action)


if __name__ == "__main__":
    unittest.main()
