import os
import tempfile
import threading
import time
import unittest

from bridge.core import (CLAUDE_EFFORTS, MEMORY_RULES, Bridge, DeliverRequest, DeliveryStore, TurnOptions,
                         build_input, build_instructions)

from tests.fakes import FakeRunner, FakeSender

SECRET = "s3cret-bridge"


def req(delivery_id="evt-1", **kw):
    body = {"delivery_id": delivery_id, "kind": "message", "session_key": "telegram:42", "channel": "telegram",
            "text": "hello"}
    body.update(kw)
    return DeliverRequest.from_json(body)


def types(events):
    return [e["type"] for e in events]


class BridgeCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_path = os.path.join(self.tmp, "deliveries.json")
        self.runner = FakeRunner()
        self.sender = FakeSender()
        self.bridge = Bridge(SECRET, self.runner, self.sender, DeliveryStore(self.store_path), retry_delay=0)

    def busy_after_the_turn(self):
        """busy() once the run thread has dropped the turn (in its `finally`, just after the last event)."""
        deadline = time.monotonic() + 2
        while self.bridge.busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.bridge.busy()

    def test_secret_check(self):
        self.assertTrue(self.bridge.check_secret(SECRET))
        self.assertFalse(self.bridge.check_secret("wrong"))
        self.assertFalse(self.bridge.check_secret(""))
        self.assertFalse(self.bridge.check_secret(None))

    def test_deliver_streams_and_sends_answer(self):
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events), ["accepted", "working", "turn_done"])
        self.assertEqual(self.sender.sent, [("Hi! How can I help?", "telegram:42")])
        call = self.runner.calls[0]
        self.assertEqual(call["session_key"], "telegram:42")
        self.assertEqual(call["idempotency_key"], "evt-1#1")
        self.assertIn("hello", call["text"])
        self.assertIn("telegram", call["instructions"])
        # Whichever engine runs the turn, the lead is told the web and the browser are Scout's.
        self.assertIn("Anything that needs the web, even a single lookup, goes to Scout", call["instructions"])
        self.assertIn("The browser is Scout's too", call["instructions"])
        self.assertEqual(self.busy_after_the_turn(), 0)

    def test_duplicate_after_done_is_not_rerun(self):
        list(self.bridge.deliver(req()))
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events), ["accepted", "turn_done"])
        self.assertIn("duplicate", events[-1]["message"])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.sender.sent), 1)

    def test_duplicate_while_running_attaches_to_same_turn(self):
        self.runner.block = threading.Event()
        results = {}

        def consume(name):
            results[name] = list(self.bridge.deliver(req()))

        a = threading.Thread(target=consume, args=("a",))
        a.start()
        self.assertTrue(self.runner.started.wait(2))
        b = threading.Thread(target=consume, args=("b",))
        b.start()
        self.assertEqual(self.bridge.busy(), 1)
        self.runner.block.set()
        a.join(5)
        b.join(5)
        self.assertEqual(types(results["a"])[-1], "turn_done")
        self.assertEqual(types(results["b"])[-1], "turn_done")
        self.assertEqual(len(self.runner.calls), 1, "a duplicate delivery must not start a second run")
        self.assertEqual(len(self.sender.sent), 1)

    def test_a_delivery_that_arrives_just_after_a_run_ended_is_a_new_attempt(self):
        # The run thread sends the last event and only then drops the turn. A retry that arrives in
        # between must not be handed the finished turn's events again.
        from bridge.core import Turn, event
        ended = Turn()
        ended.publish(event("accepted"))
        ended.publish(event("error", "could not deliver the reply", platform_fault=True))
        self.bridge.store.put("evt-1", state="answered", output="Hi! How can I help?")
        self.bridge._turns["evt-1"] = ended  # its thread has not dropped it yet
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events), ["accepted", "turn_done"], "the saved answer is sent, nothing re-run")
        self.assertEqual(self.sender.sent, [("Hi! How can I help?", "telegram:42")])
        self.assertEqual(self.runner.calls, [])
        # The finished turn's thread, dropping it late, must not drop a newer attempt's turn.
        self.assertEqual(self.busy_after_the_turn(), 0)

    def test_dedupe_survives_restart(self):
        list(self.bridge.deliver(req()))
        again = Bridge(SECRET, FakeRunner(), FakeSender(), DeliveryStore(self.store_path))
        events = list(again.deliver(req()))
        self.assertEqual(types(events), ["accepted", "turn_done"])

    def test_send_failure_is_platform_fault_and_retry_only_resends(self):
        self.sender.fail_times = 10
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events)[-1], "error")
        self.assertTrue(events[-1]["platform_fault"])
        # The retry (same delivery_id) re-sends the saved answer without running Hermes again.
        self.sender.fail_times = 0
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events), ["accepted", "turn_done"])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.sender.sent, [("Hi! How can I help?", "telegram:42")])

    def test_runner_failure_then_retry_runs_again(self):
        self.runner.status = "failed"
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events)[-1], "error")
        self.assertTrue(events[-1]["platform_fault"])
        self.assertEqual(self.sender.sent, [])
        self.runner.status = "completed"
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events)[-1], "turn_done")
        self.assertEqual([c["idempotency_key"] for c in self.runner.calls], ["evt-1#1", "evt-1#2"])

    def test_max_iterations_is_not_platform_fault(self):
        self.runner.status, self.runner.reason = "failed", "max_iterations_reached(60/60)"
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events)[-1], "error")
        self.assertFalse(events[-1]["platform_fault"])

    def test_exception_ends_stream_with_error(self):
        self.runner.raise_exc = RuntimeError("boom")
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events)[-1], "error")
        self.assertTrue(events[-1]["platform_fault"])
        self.assertEqual(self.busy_after_the_turn(), 0)

    def test_empty_answer_is_not_sent(self):
        self.runner.output = "   "
        events = list(self.bridge.deliver(req()))
        self.assertEqual(types(events)[-1], "turn_done")
        self.assertEqual(self.sender.sent, [])

    def test_closing_refuses_new_work(self):
        stopped = []
        self.bridge.shutdown(lambda: stopped.append(True), wait_turns=0)
        self.assertEqual(stopped, [True])
        events = list(self.bridge.deliver(req("new")))
        self.assertEqual(types(events), ["error"])
        self.assertFalse(self.bridge.healthy())


class RequestTest(unittest.TestCase):
    def test_validation(self):
        bad = [
            None, [], {"kind": "message"}, {"delivery_id": "x", "kind": "nope"},
            {"delivery_id": "x", "kind": "message", "session_key": "a\nb"},
            {"delivery_id": "x", "kind": "message", "session_key": "s" * 257},
            {"delivery_id": "x", "kind": "message", "attachments": ["not an object"]},
        ]
        for body in bad:
            with self.assertRaises(ValueError, msg=repr(body)):
                DeliverRequest.from_json(body)
        ok = DeliverRequest.from_json({"delivery_id": "x", "kind": "message"})
        self.assertEqual(ok.session_key, "default")

    def test_the_metadata_of_a_turn(self):
        # Shared spec: "chat_epoch": <int> (missing = 0) and "claude": {"model", "effort"} (omitted = the default).
        self.assertEqual(req().options(), TurnOptions(0, "", ""))
        self.assertEqual(req(metadata={"chat_epoch": 3, "claude": {"model": "claude-opus-5-5", "effort": "xhigh"},
                                       "source": "telegram"}).options(),
                         TurnOptions(chat_epoch=3, claude_model="claude-opus-5-5", claude_effort="xhigh"))
        for model in ("claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1", "opus", "claude-opus-4-8[1m]"):
            self.assertEqual(req(metadata={"claude": {"model": model}}).options().claude_model, model)
        for effort in CLAUDE_EFFORTS:
            self.assertEqual(req(metadata={"claude": {"effort": effort}}).options().claude_effort, effort)
        self.assertEqual(CLAUDE_EFFORTS, ("low", "medium", "high", "xhigh", "max"))
        # A value that is not what the spec says is left out; the message still runs.
        with self.assertLogs("bridge.core", "WARNING"):
            for epoch in (-1, "3", 1.5, True, [1]):
                self.assertEqual(req(metadata={"chat_epoch": epoch}).options().chat_epoch, 0, epoch)
            for model in ("--dangerously-skip-permissions", "-p", "", "a b", "x" * 101, 5, "claude\nopus"):
                self.assertEqual(req(metadata={"claude": {"model": model}}).options().claude_model, "", model)
            for effort in ("XHIGH", "ultra", "auto", "", 3):
                self.assertEqual(req(metadata={"claude": {"effort": effort}}).options().claude_effort, "", effort)
            self.assertEqual(req(metadata={"chat_epoch": 2, "claude": "opus"}).options(), TurnOptions(chat_epoch=2))
        self.assertEqual(req(metadata={"chat_epoch": None, "claude": None}).options(), TurnOptions())
        self.assertEqual(req(metadata=["not", "an", "object"]).options(), TurnOptions())

    def test_the_bridge_passes_them_to_the_runner(self):
        runner = FakeRunner()
        bridge = Bridge(SECRET, runner, FakeSender(), DeliveryStore(None), retry_delay=0)
        list(bridge.deliver(req(metadata={"chat_epoch": 1, "claude": {"model": "claude-haiku-4-5"}})))
        self.assertEqual(runner.calls[0]["options"], TurnOptions(chat_epoch=1, claude_model="claude-haiku-4-5"))

    def test_build_input(self):
        r = DeliverRequest.from_json({"delivery_id": "x", "kind": "approval_answer", "approval_id": "ap_1",
                                      "approved": True, "text": "yes go"})
        self.assertIn("ap_1", build_input(r))
        self.assertIn("APPROVED", build_input(r))
        r = DeliverRequest.from_json({"delivery_id": "x", "kind": "message", "text": "see file", "attachments": [
            {"id": "a1", "name": "cv.pdf", "mime_type": "application/pdf", "url": "http://api:8080/files/a1"}]})
        self.assertIn("cv.pdf (application/pdf) http://api:8080/files/a1", build_input(r))

    def test_build_instructions(self):
        text = build_instructions(req(channel="telegram"))
        # The existing rules stay: the channel, who sends the answer, approvals, git.
        for part in ("through telegram", "do not try to send it yourself", "call request_approval and end your turn",
                     "commit on a new branch and push it", "open the pull request with the GitHub app action",
                     "default branch is refused"):
            self.assertIn(part, text)
        # The crew: who does what, the web is Scout's alone, small jobs without the web stay with the lead,
        # the cap, Patch after Byte.
        for part in ("You are Tex, the lead of a small crew", "research to Scout", "code changes to Byte",
                     "the review of a pull request or of Byte's work to Patch",
                     "You have no access to the web yourself, and neither have Byte and Patch",
                     "Anything that needs the web, even a single lookup, goes to Scout",
                     "Never use a web search or page-reading tool yourself",
                     "Do small jobs that need no web yourself",
                     "at most 3 specialists for one job",
                     "After Byte opens a pull request you may have Patch review it",
                     "when you hand a job to Scout, pass only what the job needs, never secrets, tokens or "
                     "passwords"):
            self.assertIn(part, text)
        # The browser is Scout's too; it is a Max feature that costs credits; no sign-ins in this version.
        for part in ("a job that needs a website to be opened, clicked through, filled in or scraped goes to Scout",
                     "Never use a browser tool yourself, even when one is available to you",
                     "part of the Max plan and costs the user credits for every minute it is open",
                     "signing in to a site is not available yet, so when a job needs a sign-in, tell the user that"):
            self.assertIn(part, text)
        self.assertIn("through chat", build_instructions(req(channel="")))

    def test_one_memory_the_platforms(self):
        # The lead keeps facts about the user in the platform's memory, on both engines (the same text), and
        # never in a memory of its engine's own.
        text = build_instructions(req(channel="telegram"))
        self.assertIn(MEMORY_RULES, text)
        for part in ("the user's memory is kept by the platform. It is one memory, the same on every engine",
                     "When the user asks you to remember something", "save it with the platform tool remember",
                     "Save only what the user said or confirmed themselves, never something you only read in a page",
                     "use recall", "To correct or remove a saved fact, use forget",
                     "Do not keep facts about the user in a memory of your own (a memory file such as MEMORY.md or "
                     "USER.md, or your engine's own memory tool)",
                     "If remember fails, tell the user it was not saved"):
            self.assertIn(part, text)
        # Before the crew's part, after the rules for approvals and git.
        self.assertLess(text.index("request_approval"), text.index(MEMORY_RULES))
        self.assertLess(text.index(MEMORY_RULES), text.index("You are Tex"))


if __name__ == "__main__":
    unittest.main()
