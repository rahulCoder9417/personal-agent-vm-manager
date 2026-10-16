import os
import tempfile
import threading
import time
import unittest

from bridge import crew
from bridge.core import (ANSWER_RULES, APP_RULES, APPS_PAGE, CLAUDE_EFFORTS, FORMAT_PLAIN, FORMAT_WEB, IDENTITY, KINDS,
                         MEMORY_RULES, Bridge, DeliverRequest, DeliveryStore, Draft, Turn, TurnOptions, build_input,
                         build_instructions, event, format_rules)

from tests.fakes import CLAUDE_CODE_MODELS, FakeRunner, FakeSender

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


class DraftTest(unittest.TestCase):
    """core.Draft: the whole draft so far, at most one event per interval, nothing after close()."""

    def setUp(self):
        self.now = 100.0
        self.sent: list[str] = []
        self.draft = Draft(self.sent.append, interval=0.25, max_chars=20, clock=lambda: self.now)

    def test_whole_draft_throttled_with_the_latest_sent_when_the_interval_has_passed(self):
        self.draft("Hel")  # the first change goes out at once
        self.draft("lo")
        self.draft(" there")  # gathered: one event with the latest text, once the interval has passed
        self.assertEqual(self.sent, ["Hel"])
        self.now += 0.3
        self.draft(",")
        self.assertEqual(self.sent, ["Hel", "Hello there,"])

    def test_the_timer_sends_the_last_words_before_a_long_pause(self):
        sent = []
        done = threading.Event()
        draft = Draft(lambda t: (sent.append(t), done.set() if len(sent) == 2 else None), interval=0.05)
        draft("Let me check")
        draft(" your calendar.")  # nothing more comes (the lead waits on a tool): the timer sends it
        self.assertTrue(done.wait(2))
        self.assertEqual(sent, ["Let me check", "Let me check your calendar."])
        draft.close()

    def test_starting_over_clears_what_was_shown_and_nothing_is_sent_twice(self):
        self.draft(None)  # nothing was shown: nothing to clear
        self.assertEqual(self.sent, [])
        self.draft("Let me look")
        self.now += 1
        self.draft(None)
        self.now += 1
        self.draft("\n\nYou have two")  # a new piece's leading blank lines are not shown
        self.now += 1
        self.draft("")  # nothing added: nothing sent
        self.assertEqual(self.sent, ["Let me look", "", "You have two"])

    def test_a_long_draft_stops_growing(self):
        self.draft("x" * 15)
        self.now += 1
        self.draft("y" * 10)
        self.now += 1
        self.draft("z")
        self.assertEqual(self.sent, ["x" * 15, "x" * 15 + "y" * 5])

    def test_nothing_after_close(self):
        sent = []
        draft = Draft(sent.append, interval=0.05)
        draft("one")
        draft(" two")  # waits for the timer
        draft.close()
        draft(" three")
        time.sleep(0.15)
        self.assertEqual(sent, ["one"])


class StreamTextTest(unittest.TestCase):
    """A delivery whose metadata has "stream_text": true gets the lead's draft as text_delta events."""

    def setUp(self):
        self.runner = FakeRunner(output="Hello there")
        self.sender = FakeSender()
        self.bridge = Bridge(SECRET, self.runner, self.sender, DeliveryStore(None), retry_delay=0, draft_interval=0)

    def test_drafts_come_before_the_answer_is_sent_and_the_turn_ends(self):
        self.runner.pieces = ["Let me check.", None, "Hello", " there"]
        self.runner.piece_delay = 0.05  # the listener keeps up, so it sees each draft
        events = list(self.bridge.deliver(req(channel="web", session_key="web", metadata={"stream_text": True})))
        self.assertEqual(types(events), ["accepted", "working", "text_delta", "text_delta", "text_delta", "text_delta",
                                         "turn_done"])
        self.assertEqual([e["message"] for e in events if e["type"] == "text_delta"],
                         ["Let me check.", "", "Hello", "Hello there"])
        self.assertFalse(any(e["platform_fault"] for e in events))
        self.assertEqual(self.sender.sent, [("Hello there", "web")], "the answer still goes through send_message")

    def test_no_drafts_unless_asked(self):
        self.runner.pieces = ["Hello"]
        for meta in ({}, {"stream_text": "true"}, {"stream_text": 1}, {"stream_text": False}):
            with self.subTest(meta=meta):
                events = list(self.bridge.deliver(req(delivery_id=f"evt-{len(self.runner.calls)}", metadata=meta)))
                self.assertEqual(types(events), ["accepted", "working", "turn_done"])
                self.assertIsNone(self.runner.calls[-1]["on_text"])
        self.assertTrue(req(metadata={"stream_text": True}).options().stream_text)
        self.assertFalse(req().options().stream_text)

    def test_a_failed_turn_ends_with_its_error_after_the_drafts(self):
        self.runner.pieces = ["Half an ans"]
        self.runner.status = "failed"
        events = list(self.bridge.deliver(req(metadata={"stream_text": True})))
        self.assertEqual(types(events), ["accepted", "working", "text_delta", "error"])
        self.assertEqual(self.sender.sent, [])

    def test_a_delivery_that_attaches_again_gets_only_the_latest_draft(self):
        self.runner.pieces = ["One", " two", " three"]
        self.runner.piece_delay = 0.05
        self.runner.block = threading.Event()
        first = []
        t = threading.Thread(target=lambda: first.extend(self.bridge.deliver(req(metadata={"stream_text": True}))))
        t.start()
        self.assertTrue(self.runner.started.wait(2))
        deadline = time.monotonic() + 2
        while len(self.bridge._turns["evt-1"].events) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        again = self.bridge.deliver(req(metadata={"stream_text": True}))
        replayed = [next(again) for _ in range(3)]
        self.assertEqual([(e["type"], e["message"]) for e in replayed],
                         [("accepted", ""), ("working", "using web_search"), ("text_delta", "One two three")])
        self.runner.block.set()
        self.assertEqual(types(list(again)), ["turn_done"])
        t.join(5)
        self.assertEqual([e["message"] for e in first if e["type"] == "text_delta"],
                         ["One", "One two", "One two three"], "the first listener got every draft live")
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_listener_that_falls_behind_skips_the_drafts_a_later_one_replaced(self):
        self.runner.pieces = ["One", " two", " three"]
        events = list(self.bridge.deliver(req(metadata={"stream_text": True})))
        drafts = [e["message"] for e in events if e["type"] == "text_delta"]
        self.assertEqual(drafts[-1], "One two three")
        self.assertLessEqual(len(drafts), 3)

    def test_replay_keeps_an_empty_latest_draft(self):
        turn = Turn()
        for ev in (event("accepted"), event("text_delta", "Let me look"), event("working", "using gmail"),
                   event("text_delta", ""), event("turn_done")):
            turn.publish(ev)
        self.assertEqual([(e["type"], e["message"]) for e in turn.subscribe()],
                         [("accepted", ""), ("working", "using gmail"), ("text_delta", ""), ("turn_done", "")])


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
        # Shared spec: "chat_epoch": <int> (the conversation of the chat; missing = none named, the turn goes on in
        # the chat's last one) and "claude": {"model", "effort"} (omitted = the default).
        self.assertEqual(req().options(), TurnOptions(None, "", ""))
        self.assertEqual(req(metadata={"chat_epoch": 0}).options().chat_epoch, 0, "conversation 0 is a conversation")
        self.assertEqual(req(metadata={"chat_epoch": 3, "claude": {"model": "claude-opus-5-5", "effort": "xhigh"},
                                       "source": "telegram"}).options(),
                         TurnOptions(chat_epoch=3, claude_model="claude-opus-5-5", claude_effort="xhigh"))
        for model in ("opus", "claude-opus-4-8[1m]", *CLAUDE_CODE_MODELS):
            self.assertEqual(req(metadata={"claude": {"model": model}}).options().claude_model, model)
        for effort in CLAUDE_EFFORTS:
            self.assertEqual(req(metadata={"claude": {"effort": effort}}).options().claude_effort, effort)
        self.assertEqual(CLAUDE_EFFORTS, ("low", "medium", "high", "xhigh", "max"))
        # A value that is not what the spec says is left out; the message still runs.
        with self.assertLogs("bridge.core", "WARNING"):
            for epoch in (-1, "3", 1.5, True, [1]):
                self.assertIsNone(req(metadata={"chat_epoch": epoch}).options().chat_epoch, epoch)
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

    def test_formatting_by_channel(self):
        # Shared spec of 2026-10-09: the web app draws the agent's text as Markdown, a messaging app shows it as
        # typed. So the lead is told which of the two this turn's answer is for, with the reason.
        web = build_instructions(req(channel="web", session_key="web"))
        self.assertIn(FORMAT_WEB, web)
        for part in ("the user reads your final answer in the web app, which shows Markdown as formatted text",
                     "use Markdown where it helps the reader", "a list for steps or for separate points",
                     "a table for a comparison", "a fenced code block with the language named for code or commands",
                     "short headings only in a long answer", "A short answer stays a short plain sentence",
                     "Do not write raw HTML and do not embed images", "give an image as a link"):
            self.assertIn(part, web)
        # Nothing in the web instructions forbids Markdown or asks for plain text.
        self.assertNotIn(FORMAT_PLAIN, web)
        for part in ("plain text only", "Use no Markdown", "no # headings", "no table pipes", "Do not make a table",
                     "stray characters"):
            self.assertNotIn(part, web)
        # Every other value asks for plain text: the messaging apps the backend knows, no channel at all, and a
        # value the Bridge has never heard of.
        for channel in ("telegram", "whatsapp", "imessage", "", "sms"):
            text = build_instructions(req(channel=channel))
            self.assertIn(FORMAT_PLAIN, text, channel)
            self.assertNotIn(FORMAT_WEB, text, channel)
            for part in ("the user reads your final answer in a messaging app, which shows every character as you "
                         "typed it", "Write plain text only, the way a person writes a text message",
                         "Use no Markdown: no ** or _ around words, no # headings, no backticks or code fences, no "
                         "table pipes", "Keep paragraphs short",
                         'For a list, start each line with "- " or with "1. ", "2. " and so on',
                         "Do not make a table: give one line per item",
                         "Write a link as the bare address, not as [text](address)"):
                self.assertIn(part, text, channel)
            self.assertNotIn("use Markdown where it helps", text, channel)
        # A delivery with no channel field is the empty channel.
        bare = DeliverRequest.from_json({"delivery_id": "x", "kind": "message"})
        self.assertEqual(bare.channel, "")
        self.assertIn(FORMAT_PLAIN, build_instructions(bare))
        self.assertEqual((format_rules("web"), format_rules("telegram"), format_rules("")),
                         (FORMAT_WEB, FORMAT_PLAIN, FORMAT_PLAIN))
        # Both texts give the reason, and set aside what the engine itself says about where its output appears
        # (Claude Code: a terminal that renders Markdown; Hermes: an API with an unknown display).
        for text in (FORMAT_WEB, FORMAT_PLAIN):
            self.assertTrue(text.startswith("Formatting: the user reads your final answer in "))
            self.assertIn("Other guidance you have about where your output appears", text)
            self.assertTrue(text.endswith("does not apply to this answer."))

    def test_formatting_goes_by_the_channel_for_every_kind_of_delivery(self):
        # An approval's answer, an automation and an app event carry the channel of the chat their answer goes
        # to (the backend: the approval's or the automation's session), so they are formatted for it as well.
        for kind in sorted(KINDS):
            self.assertIn(FORMAT_WEB, build_instructions(req(kind=kind, channel="web", session_key="web")), kind)
            self.assertIn(FORMAT_PLAIN, build_instructions(req(kind=kind, channel="telegram")), kind)

    def test_the_formatting_rules_are_the_only_new_part(self):
        # The instructions as they were before the formatting rules: with the rules taken out, the text is that
        # again, for every channel. The rules are a paragraph of their own, after the app and answer rules and
        # before the memory rules.
        for channel, shown in (("web", "web"), ("telegram", "telegram"), ("whatsapp", "whatsapp"), ("", "chat")):
            before = (
                f"{IDENTITY}\n\n"
                f"The user is talking to you through {shown}. Your final answer for this turn is delivered to "
                "them automatically, so do not try to send it yourself. Keep replies suited to a chat app. "
                "Before anything risky (sending to other people, deleting, paying, posting publicly) call "
                "request_approval and end your turn; the user's answer arrives as a later message. "
                "When the user's GitHub is connected, git works for their repositories: clone "
                "https://github.com/<owner>/<repo> into your workspace, commit on a new branch and push it, "
                "then open the pull request with the GitHub app action. Pushing to a repository's default "
                "branch is refused, so always work on a branch.\n\n"
                f"{APP_RULES}\n\n"
                f"{ANSWER_RULES}\n\n"
                f"{MEMORY_RULES}\n\n"
                + crew.lead_instructions()
            )
            text = build_instructions(req(channel=channel))
            rules = format_rules(channel) + "\n\n"
            self.assertEqual(text.count(rules), 1, channel)
            self.assertEqual(text.replace(rules, ""), before, channel)
            self.assertIn(f"so always work on a branch.\n\n{APP_RULES}\n\n{ANSWER_RULES}\n\n" + rules + MEMORY_RULES,
                          text, channel)

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

    def test_the_lead_knows_who_it_is(self):
        # 2026-10-13: on Claude Code the lead sent the founder to "your Claude settings" to connect Google Calendar.
        # The lead's instructions now open with who it is (the user's agent in Textcrew), where apps are connected
        # (Textcrew's Apps page), how to see them (the platform tools) and what to say when one is missing. The
        # same text for every channel, every kind of delivery and a crew job, so both engines get it.
        for part in ("Who you are: you are Tex, the user's own personal agent in Textcrew",
                     "The user connects their apps", f"in Textcrew, on the Apps page: {APPS_PAGE}",
                     "get_profile lists the connected apps", "list_app_actions lists what you can do in them and "
                     "says which apps are still connecting",
                     "If an app the user says is connected is not there, tell them it may still be finishing, or that "
                     "it needs to be connected again on the Apps page",
                     "Never send the user to the settings or connectors of Claude or of any other product",
                     "Never present yourself as Claude or Claude Code",
                     "do not mention the engine, the model or the company behind you unless the user asks",
                     "Where other guidance you have names you differently, this takes its place"):
            self.assertIn(part, IDENTITY)
        self.assertEqual(APPS_PAGE, "https://app.textcrew.in/apps")
        for channel in ("web", "telegram", "whatsapp", "imessage", ""):
            for kind in sorted(KINDS):
                text = build_instructions(req(kind=kind, channel=channel))
                self.assertTrue(text.startswith(IDENTITY + "\n\n"), (channel, kind))
                self.assertEqual(text.count(IDENTITY), 1, (channel, kind))
        job = DeliverRequest.from_json({"delivery_id": "j", "kind": "message", "session_key": "web",
                                        "channel": "web", "text": "[Crew job crj_1] Review acme/web#42.",
                                        "metadata": {"crew_job": {"id": "crj_1", "member": "patch"}}})
        self.assertTrue(build_instructions(job, "/data/workspace/jobs/crj_1").startswith(IDENTITY))
        # Short: one paragraph, and nothing in the instructions sends the user to Claude's own settings.
        self.assertNotIn("\n", IDENTITY)
        self.assertLess(len(IDENTITY), 1100)
        for gone in ("Claude settings", "your Claude", "claude.ai"):
            self.assertNotIn(gone, build_instructions(req(channel="web")))


if __name__ == "__main__":
    unittest.main()
