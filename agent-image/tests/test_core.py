import os
import tempfile
import threading
import time
import unittest

from bridge.core import (Bridge, DeliverRequest, DeliveryStore, build_input, build_instructions)

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
        # The crew: who does what, small jobs stay with the lead, the cap, Patch after Byte.
        for part in ("You are Tex, the lead of a small crew", "research to Scout", "code changes to Byte",
                     "the review of a pull request or of Byte's work to Patch", "Do small jobs yourself",
                     "at most 3 specialists for one job",
                     "After Byte opens a pull request you may have Patch review it",
                     "when you hand a job to Scout, pass only what the research needs, never secrets, tokens or "
                     "private data"):
            self.assertIn(part, text)
        self.assertIn("through chat", build_instructions(req(channel="")))


if __name__ == "__main__":
    unittest.main()
