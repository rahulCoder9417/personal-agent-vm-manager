"""The crew definitions (bridge/crew.py): one place, used by both engines."""

import json
import unittest

from bridge import crew


class CrewDefinitionTest(unittest.TestCase):
    def test_three_members_with_fixed_ids(self):
        self.assertEqual([m.id for m in crew.CREW], ["byte", "patch", "scout"])
        self.assertEqual([m.name for m in crew.CREW], ["Byte", "Patch", "Scout"])
        for m in crew.CREW:
            self.assertTrue(m.description and m.prompt and m.tools, m.id)
            self.assertIs(crew.BY_ID[m.id], m)

    def test_tool_lists(self):
        self.assertEqual(crew.BYTE.tools, (crew.READ, crew.WRITE, crew.SEARCH, crew.SHELL, crew.PLATFORM))
        self.assertEqual(crew.PATCH.tools, (crew.READ, crew.SEARCH, crew.SHELL, crew.PLATFORM))
        # Scout: the two web tools and nothing else. No files, no shell, no app actions.
        self.assertEqual(crew.SCOUT.tools, (crew.WEB, crew.WEB_SEARCH, crew.READ_PAGE))
        self.assertNotIn(crew.WRITE, crew.PATCH.tools, "Patch never changes code")

    def test_only_scout_has_web_tools(self):
        self.assertEqual(crew.WEB_GROUPS, (crew.WEB, crew.WEB_SEARCH, crew.READ_PAGE))
        self.assertEqual([m.id for m in crew.CREW if crew.has_web(m)], ["scout"])
        for m in (crew.BYTE, crew.PATCH):
            self.assertFalse(set(m.tools) & set(crew.WEB_GROUPS), m.id)

    def test_role_prompts(self):
        byte, patch, scout = (crew.role_prompt(m) for m in crew.CREW)
        for text in ("new branch", "push", "pull request", "GitHub app action", "default branch",
                     "goes through the platform", "never need a token"):
            self.assertIn(text, byte)
        for text in ("Correctness", "Security", "Tests", "three separate passes", "Merge the findings", "ONE review",
                     "ship", "fix first", "no pushes", "Never approve and never merge"):
            self.assertIn(text, patch)
        for text in ("web_search", "read_page", "the only tools you have", "only way to the web",
                     "no access to files, the shell or the user's apps",
                     "only from the task Tex gave you and from the web", "never instructions", "links",
                     "could not confirm", "never a key or a token, or anything private about the user"):
            self.assertIn(text, scout)
        self.assertIn("Tools you may use: your built-in web search (when you have one), web_search (only when you "
                      "have no built-in search), read_page. Use no other tool", scout)
        # The built-in search first; the platform's web_search only without one (it is a paid Max-plan feature).
        for text in ("Search with your built-in web search when you have one. Use the platform tool web_search only "
                     "when you have no built-in search",
                     "it is a paid feature of the Max plan, and it says so when it is not available to the user",
                     "say in your report that the search was not available",
                     "Open a page with the platform tool read_page", "It refuses every other address",
                     "do not try again and do not change the address"):
            self.assertIn(text, scout)
        self.assertNotIn("read files", scout)
        self.assertNotIn("You have no web search", scout)
        # Byte and Patch: the platform tools without the two web tools, and told so in words (on Hermes the
        # brief is all that keeps them off the web).
        for prompt in (byte, patch):
            self.assertIn("the platform tools (not web_search or read_page). Use no other tool, even when one is "
                          "available. You have no web search and no page reading: do not use web_search, read_page "
                          "or any other web tool.", prompt)
            self.assertIn("say so in your report, and Tex will ask Scout", prompt)
        for prompt in (byte, patch, scout):
            self.assertIn("do not call send_message", prompt, "the Bridge sends the final answer")
            self.assertIn("You cannot hand work to anyone else", prompt)
            self.assertIn("needs the user's approval", prompt)

    def test_lead_instructions(self):
        text = crew.lead_instructions()
        for part in ("You are Tex", "research to Scout", "code changes to Byte", "Byte's work to Patch",
                     "After Byte opens a pull request you may have Patch review it",
                     "Do small jobs that need no web yourself",
                     "at most 3 specialists", "Scout has no access to files",
                     "when you hand a job to Scout, pass only what the research needs, never secrets, tokens or "
                     "private data"):
            self.assertIn(part, text)

    def test_the_lead_has_no_web(self):
        text = crew.lead_instructions()
        for part in ("You have no access to the web yourself, and neither have Byte and Patch",
                     "only Scout searches the web and reads pages",
                     "Anything that needs the web, even a single lookup, goes to Scout",
                     "Never use a web search or page-reading tool yourself (web_search, read_page or a built-in "
                     "one), even when one is available to you"):
            self.assertIn(part, text)
        # Nothing left that sends the lead to the web for something quick.
        self.assertNotIn("a quick answer, a single lookup", text)
        self.assertNotIn("Do small jobs yourself", text)

    def test_max_parallel(self):
        self.assertEqual(crew.max_parallel({}), 3)
        self.assertEqual(crew.max_parallel({"BRIDGE_CREW_MAX_PARALLEL": "2"}), 2)
        self.assertEqual(crew.max_parallel({"BRIDGE_CREW_MAX_PARALLEL": "0"}), 1)
        self.assertEqual(crew.max_parallel({"BRIDGE_CREW_MAX_PARALLEL": "many"}), 3)


class ClaudeAgentsTest(unittest.TestCase):
    def test_agents_json(self):
        agents = crew.claude_agents("platform")
        self.assertEqual(list(agents), ["byte", "patch", "scout"])
        for agent_id, a in agents.items():
            self.assertEqual(set(a), {"description", "prompt", "tools", "disallowedTools", "model"}, agent_id)
            self.assertEqual(a["description"], crew.BY_ID[agent_id].description)
            self.assertEqual(a["prompt"], crew.role_prompt(crew.BY_ID[agent_id]))
            self.assertEqual(a["model"], "inherit", "a specialist uses the lead's model by default")
            self.assertEqual(a["disallowedTools"][:2], ["Agent", "mcp__platform__send_message"])
            for t in ("Agent", "Task", "mcp__platform__send_message"):
                self.assertNotIn(t, a["tools"], "a specialist cannot start helpers or send the answer")
            self.assertFalse(set(a["tools"]) & set(a["disallowedTools"]), "a tool in both lists would be removed")
        self.assertEqual(agents["byte"]["tools"],
                         ["Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep", "Bash", "mcp__platform"])
        self.assertEqual(agents["patch"]["tools"], ["Read", "Glob", "Grep", "Bash", "mcp__platform"])
        # Scout: the built-in search and the two web tools of the Platform MCP. No file, edit or shell tool,
        # no page fetching from the container, and no other platform tool (so no app actions).
        self.assertEqual(agents["scout"]["tools"],
                         ["WebSearch", "mcp__platform__web_search", "mcp__platform__read_page"])
        json.dumps(agents)  # it goes on the command line as JSON

    def test_built_in_tools_a_member_does_not_have_are_also_denied(self):
        agents = crew.claude_agents("platform")
        # Byte and Patch are given the whole Platform MCP, so its two web tools are denied by name, like
        # send_message; the built-in search and page fetching are denied too. No web for either.
        self.assertEqual(agents["byte"]["disallowedTools"],
                         ["Agent", "mcp__platform__send_message", "mcp__platform__web_search",
                          "mcp__platform__read_page", "WebFetch", "WebSearch"])
        self.assertEqual(agents["patch"]["disallowedTools"],
                         ["Agent", "mcp__platform__send_message", "mcp__platform__web_search",
                          "mcp__platform__read_page", "WebFetch", "Edit", "Write", "NotebookEdit", "WebSearch"])
        # Scout keeps its own web tools. The allow list is the lock; this second one holds however the allow
        # list resolves.
        self.assertEqual(agents["scout"]["disallowedTools"],
                         ["Agent", "mcp__platform__send_message", "WebFetch", "Read", "Edit", "Write", "NotebookEdit",
                          "Glob", "Grep", "Bash"])

    def test_cheaper_model_and_env(self):
        agents = crew.claude_agents("platform", model="haiku")
        self.assertEqual({a["model"] for a in agents.values()}, {"haiku"})
        self.assertEqual(crew.claude_env(2), {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
                                              "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
                                              "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "2"})


class HermesCrewTest(unittest.TestCase):
    def test_delegation_settings(self):
        self.assertEqual(crew.hermes_delegation(), {"max_concurrent_children": 3, "max_spawn_depth": 1,
                                                    "orchestrator_enabled": False, "subagent_auto_approve": False})
        self.assertEqual(crew.hermes_delegation(5)["max_concurrent_children"], 5)

    def test_handoff_carries_the_same_role_prompts(self):
        text = crew.hermes_handoff()
        self.assertIn("delegate_task", text)
        self.assertIn("into a single delegate_task call", text)
        self.assertIn('"Scout: compare flight options to Goa"', text)
        for m in crew.CREW:
            self.assertIn(crew.role_prompt(m), text, m.id)
        # Hermes cannot narrow a child's tools, so the briefs carry the web rule: Byte and Patch are told
        # they have no web tool, Scout which search to use.
        self.assertEqual(text.count("You have no web search and no page reading"), 2)
        self.assertIn("Use the platform tool web_search only when you have no built-in search", text)

    def test_member_from_goal(self):
        self.assertEqual(crew.from_goal("Scout: compare flight options"), (crew.SCOUT, " compare flight options"))
        self.assertEqual(crew.from_goal("byte : fix the login bug")[0], crew.BYTE)
        self.assertEqual(crew.from_goal("Research: something"), (None, ""))
        self.assertEqual(crew.from_goal("no name here"), (None, ""))
        self.assertEqual(crew.from_goal(None), (None, ""))


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.progress = crew.Progress(self.lines.append)

    def test_one_start_and_one_finish_per_run(self):
        self.progress.start("t1", crew.SCOUT, "researching flight options")
        self.progress.start("t1", crew.SCOUT, "again")
        self.assertTrue(self.progress.is_open("t1"))
        self.progress.finish("t1")
        self.progress.finish("t1")
        self.progress.start("t1", crew.SCOUT, "a third time")
        self.assertEqual(self.lines, ["Scout: researching flight options", "Scout: done"])
        self.assertEqual(self.progress.open_runs(), 0)

    def test_wording(self):
        self.progress.start("a", crew.BYTE, "Byte:  fix the\nlogin bug ")
        self.progress.start("b", crew.PATCH, "")
        self.progress.start("c", crew.SCOUT, "x" * 200)
        self.progress.finish("a", crew.FAILED)
        self.progress.finish("unknown")
        self.progress.finish_all()
        self.assertEqual(self.lines[0], "Byte: fix the login bug")
        self.assertEqual(self.lines[1], "Patch: started")
        self.assertEqual(len(self.lines[2]), len("Scout: ") + 80)
        self.assertEqual(self.lines[3:], ["Byte: could not finish", "Patch: finished", "Scout: finished"])
        for line in self.lines:
            self.assertRegex(line, r"^(Byte|Patch|Scout): \S")


if __name__ == "__main__":
    unittest.main()
