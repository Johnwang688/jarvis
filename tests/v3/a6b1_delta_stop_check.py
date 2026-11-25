"""Stop clears waiting messages even when the provider failed first."""

import threading
import unittest

from a6b1_acceptance_check import Base, Model, reply


class StopAfterError(Base):
    def test_stop_drops_queued_steers_after_a_provider_error(self):
        entered, release = threading.Event(), threading.Event()

        def fail(_messages):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Provider fixture was not released")
            raise RuntimeError("Fake provider failed")

        model = Model(fail, reply("Fresh reply."))
        chat = self.service(model)
        self.addCleanup(release.set)
        chat.send("First question.")
        self.assertTrue(entered.wait(5))
        stale = ["First waiting steer.", "Second waiting steer."]
        for text in stale:
            self.assertTrue(chat.send(text)["steered"])
        release.set()
        self.assertEqual(self.wait_idle(chat), "error")

        chat.stop()
        self.assertEqual(len(model.seen), 1)
        self.assertFalse(chat.send("Fresh question.")["steered"])
        self.assertEqual(self.wait_idle(chat), "idle")
        self.assertEqual(len(model.seen), 2)
        users = [row["content"] for row in model.seen[-1]["messages"]
                 if row["role"] == "user"]
        self.assertEqual(users[-1], "Fresh question.")
        history = [row["text"] for row in chat.snapshot()["history"]]
        for text in stale:
            self.assertNotIn(text, history)
        stopped = [row["dropped"] for row in chat.events(0)["events"]
                   if row["kind"] == "stopped"]
        self.assertEqual(stopped, [len(stale)])


if __name__ == "__main__":
    unittest.main()
