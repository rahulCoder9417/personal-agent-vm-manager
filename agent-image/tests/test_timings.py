"""Where a delivery's time goes, as the Bridge reports it on the turn's last event (bridge/timings.py)."""

import json
import os
import shutil
import tempfile
import threading
import unittest

from bridge.claude import ClaudeAuth, ClaudeRunner
from bridge.core import Bridge, DeliverRequest, DeliveryStore, RunResult, TurnOptions
from bridge.hermes import HermesClient
from bridge.timings import MAX_CREW, TurnClock
from tests.fakes import FakeSender
from tests.test_claude import Env
from tests.test_clients import FakeHermes, frames, serve

SECRET = "bridge-secret-0123456789"


class Ticks:
    """A clock that moves only when told to."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000


class TurnClockTest(unittest.TestCase):
    def test_marks_are_milliseconds_since_the_delivery_was_taken(self):
        ticks = Ticks()
        clock = TurnClock(ticks)
        ticks.advance(250)
        clock.run_started("claude_code")
        ticks.advance(1500)
        clock.mark("engine_ready")
        ticks.advance(500)
        clock.mark("engine_ready")  # the first time counts
        clock.mark("engine_done", last=True)
        ticks.advance(100)
        clock.mark("engine_done", last=True)  # ... or the last, when asked
        report = clock.report()
        self.assertEqual(report["v"], 1)
        self.assertEqual((report["engine"], report["runs"]), ("claude_code", 1))
        self.assertEqual(report["marks"], {"engine_start": 250, "engine_ready": 1750, "engine_done": 2350})

    def test_overlapping_tool_calls_count_once(self):
        ticks = Ticks()
        clock = TurnClock(ticks)
        clock.tool_start("a")
        ticks.advance(100)
        clock.tool_start("b")  # runs next to a
        ticks.advance(100)
        clock.tool_end("a")
        ticks.advance(100)
        clock.tool_end("b")
        clock.tool_end("never-started")  # ignored
        ticks.advance(1000)  # nothing runs
        clock.tool_start("c")
        ticks.advance(50)
        self.assertEqual(clock.report()["tools_ms"], 350, "300 for a and b together, 50 so far for c")

    def test_the_crew_runs_are_listed_with_their_member(self):
        ticks = Ticks()
        clock = TurnClock(ticks)
        ticks.advance(1000)
        clock.crew_start("call-1", "scout")
        clock.crew_start("call-1", "scout")  # the same run once
        ticks.advance(4000)
        clock.crew_end("call-1")
        clock.crew_end("call-1")
        report = clock.report()
        self.assertEqual(report["crew"], [{"member": "scout", "start": 1000, "end": 5000}])
        self.assertEqual(report["crew_ms"], 4000)
        for i in range(MAX_CREW + 5):
            clock.crew_start(f"x{i}", "byte")
        self.assertEqual(len(clock.report()["crew"]), MAX_CREW)

    def test_only_the_numbers_of_the_clis_result_are_kept(self):
        clock = TurnClock()
        clock.cli_result({"type": "result", "result": "the answer, never kept", "duration_ms": 7400,
                          "duration_api_ms": 6100.0, "num_turns": 2, "total_cost_usd": 0.4,
                          "usage": {"input_tokens": 12, "cache_read_input_tokens": 41000, "output_tokens": -1,
                                    "service_tier": "standard"}})
        self.assertEqual(clock.report()["cli"], {"duration_ms": 7400, "duration_api_ms": 6100, "num_turns": 2,
                                                 "input_tokens": 12, "cache_read_input_tokens": 41000})
        self.assertNotIn("cli", TurnClock().report(), "no CLI, no numbers")
        self.assertNotIn("the answer", json.dumps(clock.report()))


class MarkingRunner:
    """A runner that marks the clock it is given, like the engines do."""

    def __init__(self):
        self.options = None

    def healthy(self):
        return True

    def run(self, text, instructions, session_key, idempotency_key, on_progress, timeout, options=None,
            on_text=None):
        self.options = options
        options.clock.run_started("hermes")
        options.clock.mark("engine_ready")
        options.clock.mark("first_text")
        options.clock.mark("engine_done", last=True)
        return RunResult(status="completed", output="Hello!")


class BridgeTimingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DeliveryStore(os.path.join(self.tmp, "deliveries.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def deliver(self, bridge, did="d1"):
        return list(bridge.deliver(DeliverRequest(delivery_id=did, kind="message", session_key="web",
                                                  text="hlo", metadata={"chat_epoch": 0})))

    def test_the_last_event_says_where_the_time_went(self):
        runner, sender = MarkingRunner(), FakeSender()
        events = self.deliver(Bridge(SECRET, runner, sender, self.store, retry_delay=0))
        self.assertEqual([e["type"] for e in events], ["accepted", "turn_done"])
        self.assertNotIn("timings", events[0])
        report = events[-1]["timings"]
        self.assertEqual((report["engine"], report["runs"]), ("hermes", 1))
        marks = report["marks"]
        for name in ("engine_start", "engine_ready", "first_text", "engine_done", "answer_sent"):
            self.assertIn(name, marks)
        self.assertLessEqual(marks["engine_done"], marks["answer_sent"])
        self.assertNotIn("hlo", json.dumps(report), "no message text")
        self.assertNotIn("Hello", json.dumps(report), "no answer text")
        # The clock reached the engine without changing what the delivery asked for.
        self.assertEqual(runner.options, TurnOptions(chat_epoch=0))
        # A repeat of a finished delivery runs nothing, so it reports nothing.
        again = self.deliver(Bridge(SECRET, runner, sender, self.store, retry_delay=0))
        self.assertNotIn("timings", again[-1])

    def test_a_failed_turn_reports_too(self):
        class Failing(MarkingRunner):
            def run(self, *a, **kw):
                super().run(*a, **kw)
                return RunResult(status="failed", output="", reason="boom")

        events = self.deliver(Bridge(SECRET, Failing(), FakeSender(), self.store, retry_delay=0))
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("engine_done", events[-1]["timings"]["marks"])
        self.assertNotIn("answer_sent", events[-1]["timings"]["marks"])


class ClaudeTimingsTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.sign_in()
        self.runner = ClaudeRunner([self.e.cli], self.e.env, self.e.data, self.e.reader().load,
                                   auth=ClaudeAuth([self.e.cli], self.e.env))

    def run_turn(self, on_text=None):
        clock = TurnClock()
        res = self.runner.run(text="what's up?", instructions="hi", session_key="web", idempotency_key="d#1",
                              on_progress=lambda _: None, timeout=20, options=TurnOptions(clock=clock),
                              on_text=on_text)
        self.assertEqual(res.status, "completed")
        return clock.report()

    def test_a_web_chat_turn_shows_the_model_starting_to_think_and_write(self):
        report = self.run_turn(on_text=lambda _: None)  # web chat: the answer is streamed
        marks = report["marks"]
        self.assertEqual((report["engine"], report["runs"]), ("claude_code", 1))
        order = ["engine_start", "engine_ready", "first_response", "first_thinking", "first_text", "engine_done"]
        for name in order:
            self.assertIn(name, marks)
        self.assertEqual([marks[n] for n in order], sorted(marks[n] for n in order), "in the order they happen")
        self.assertIn("first_tool", marks)
        self.assertEqual(report["cli"], {"duration_ms": 7400, "duration_api_ms": 6100, "num_turns": 2,
                                         "input_tokens": 12, "cache_read_input_tokens": 41000,
                                         "cache_creation_input_tokens": 900, "output_tokens": 310})
        self.assertGreaterEqual(report["tools_ms"], 0)
        self.assertEqual(report["crew"], [])

    def test_a_chat_app_turn_has_the_leads_whole_messages_only(self):
        report = self.run_turn()
        marks = report["marks"]
        for name in ("engine_ready", "first_tool", "first_text", "engine_done"):
            self.assertIn(name, marks)
        self.assertNotIn("first_response", marks, "the API's stream events come only with the answer streamed")
        self.assertLessEqual(marks["first_tool"], marks["first_text"], "get_profile came before the words")

    def test_a_specialist_is_the_crews_time_and_the_leads_own_tool_is_the_tools(self):
        self.e.mode("crew_background")
        report = self.run_turn()
        self.assertEqual([run["member"] for run in report["crew"]], ["patch"])
        self.assertIn("end", report["crew"][0])
        self.assertGreaterEqual(report["crew_ms"], 0)
        self.assertIn("first_tool", report["marks"])


class HermesTimingsTest(unittest.TestCase):
    def setUp(self):
        FakeHermes.posted = []
        FakeHermes.health = [0]
        FakeHermes.health_calls = 0
        self.srv, self.url = serve(FakeHermes)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_a_hermes_turn_marks_its_run_words_tools_and_crew(self):
        FakeHermes.runs = [frames(
            {"event": "message.delta", "delta": "Let me look."},
            {"event": "tool.started", "tool": "web_search", "tool_call_id": "c1"},
            {"event": "tool.completed", "tool": "web_search", "tool_call_id": "c1"},
            {"event": "tool.started", "tool": "delegate_task"},
            {"event": "subagent.start", "subagent_id": "sa-1", "goal": "Scout: compare flights"},
            {"event": "subagent.complete", "subagent_id": "sa-1", "status": "completed"},
            {"event": "tool.completed", "tool": "delegate_task", "preview": "{}"},
            {"event": "run.completed", "output": "Done."})]
        clock = TurnClock()
        res = HermesClient(self.url, "k", poll_interval=0.01).run(
            "hi", "be brief", "web", "d#1", lambda _: None, timeout=10, options=TurnOptions(clock=clock))
        self.assertEqual(res.status, "completed")
        report = clock.report()
        self.assertEqual((report["engine"], report["runs"]), ("hermes", 1))
        for name in ("engine_start", "engine_ready", "first_text", "first_tool", "engine_done"):
            self.assertIn(name, report["marks"])
        self.assertEqual([run["member"] for run in report["crew"]], ["scout"])
        self.assertNotIn("cli", report)


if __name__ == "__main__":
    unittest.main()
