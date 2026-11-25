"""A completed assistant reply survives in full when the chat reopens."""

import unittest

from a6b1_acceptance_check import Base, Model, reply
from jarvis.v3.chat import MAX_TEXT


class ReplyHistory(Base):
    def test_reopened_history_matches_a_long_reply_event(self):
        paragraph = "Here is a detailed example. "
        text = paragraph * (MAX_TEXT // len(paragraph) + 1) + "End of reply."
        self.assertGreater(len(text), MAX_TEXT)
        chat = self.service(Model(reply(text)))
        chat.send("Give a detailed example.")
        self.assertEqual(self.wait_idle(chat), "idle")
        (event,) = [row for row in chat.events(0)["events"] if row["kind"] == "reply"]
        self.assertEqual(event["text"], text)
        self.assertTrue(chat.close())

        snapshot = self.service(Model()).snapshot()
        self.assertEqual(snapshot["session_id"], chat.snapshot()["session_id"])
        row = snapshot["history"][-1]
        self.assertEqual(row["role"], "assistant")
        self.assertEqual(len(row["text"]), len(event["text"]))
        self.assertTrue(row["text"] == event["text"], "History changed the reply text")


if __name__ == "__main__":
    unittest.main()
