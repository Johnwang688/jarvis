"""Regressions for cancellation after chat work has already started."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
import threading
import unittest
from unittest.mock import patch

from a6b1_acceptance_check import Base, reply
from jarvis import llm
from jarvis.v3.chat import MAX_INBOX, ChatBusy, ChatService
from jarvis.v3.skill_store import SkillStore


class StopRegression(Base):
    def start(self, first_result):
        entered, release = threading.Event(), threading.Event()
        seen = []

        def complete(_model, messages, **kwargs):
            seen.append(json.loads(json.dumps(messages)))
            if len(seen) == 1:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Fixture did not release completion")
                return first_result(kwargs["should_stop"])
            return reply("A new completion ran.")

        chat = self.service(complete)
        self.addCleanup(release.set)
        chat.send("Original question.")
        self.assertTrue(entered.wait(5))
        return chat, release, seen

    def join_turn(self, chat):
        worker = chat._worker
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(chat.snapshot()["state"], "idle")

    def test_stop_does_not_start_another_turn_from_pending_steering(self):
        def cancel(should_stop):
            self.assertTrue(should_stop())
            raise llm.Cancelled()

        chat, release, seen = self.start(cancel)
        self.assertTrue(chat.send("Still waiting to be steered.")["steered"])
        self.assertEqual(chat.stop()["state"], "stopping")
        release.set()
        self.join_turn(chat)

        self.assertEqual([event["kind"] for event in chat.events(0)["events"]],
                         ["user", "steer", "stopped"])
        self.assertEqual(len(seen), 1)

    def test_stopped_reply_is_absent_from_saved_history(self):
        late = "A reply that finished while Stop was being handled."
        chat, release, _ = self.start(lambda _should_stop: reply(late))
        self.assertEqual(chat.stop()["state"], "stopping")
        release.set()
        self.join_turn(chat)

        self.assertEqual([event["kind"] for event in chat.events(0)["events"]],
                         ["user", "stopped"])
        root = self.v3 / "chats"
        reopened = ChatService(root)
        self.addCleanup(reopened.close)
        snapshot = reopened.snapshot()
        self.assertEqual(snapshot["session_id"], chat.snapshot()["session_id"])
        self.assertEqual([(row["role"], row["text"]) for row in snapshot["history"]],
                         [("user", "Original question.")])
        self.assertNotIn(late, next(root.glob("*/messages.json")).read_text())

    def test_pending_steering_has_backpressure(self):
        chat, _, _ = self.start(lambda _should_stop: reply("Done."))
        for number in range(MAX_INBOX):
            self.assertTrue(chat.send(f"Waiting message {number}.")["steered"])
        with self.assertRaises(ChatBusy):
            chat.send("One more waiting message.")

    def test_close_honors_grace_while_a_read_tool_is_blocked(self):
        in_tool, release_tool = threading.Event(), threading.Event()

        def blocked_names(_store, *, readonly):
            self.assertTrue(readonly)
            in_tool.set()
            if not release_tool.wait(5):
                raise TimeoutError("Fixture did not release tool")
            return []

        chat, release_completion, _ = self.start(
            lambda _should_stop: reply(calls=[("skill_names", {})]))
        with patch.object(SkillStore, "names", blocked_names), ThreadPoolExecutor(1) as pool:
            release_completion.set()
            self.assertTrue(in_tool.wait(5))
            closing = pool.submit(chat.close, grace_s=0)
            try:
                self.assertFalse(closing.result(timeout=1))
            except FutureTimeout:
                self.fail("close waited for a blocked read beyond its grace")
            finally:
                release_tool.set()


if __name__ == "__main__":
    unittest.main()
