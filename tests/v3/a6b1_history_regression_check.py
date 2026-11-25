"""Regressions for the persisted chat history and its ingest boundary."""

import unittest

from a6b1_acceptance_check import Base, Model, reply
from jarvis.v3.chat import MAX_TEXT, ChatService, ChatStorageError
from jarvis.v3.index import Index
from jarvis.v3.ingest.transcripts import ingest


class HistoryRegression(Base):
    def test_max_length_message_survives_reopening_the_chat(self):
        text = ("a" * (MAX_TEXT - len(" final words"))) + " final words"
        chat = self.service(Model(reply("Done.")))
        chat.send(text)
        self.assertEqual(self.wait_idle(chat), "idle")
        session_id = chat.snapshot()["session_id"]
        self.assertTrue(chat.close())

        reopened = self.service(Model())
        snapshot = reopened.snapshot()
        self.assertEqual(snapshot["session_id"], session_id)
        stored = snapshot["history"][0]["text"]
        self.assertEqual(len(stored), len(text))
        self.assertEqual(stored, text)

    def test_ingest_skips_a_chat_root_the_service_refuses_as_nonprivate(self):
        chat = self.service(Model(reply("A seismograph.")))
        chat.send("Name a scientific instrument.")
        self.assertEqual(self.wait_idle(chat), "idle")
        self.assertTrue(chat.close())
        root = self.v3 / "chats"
        root.chmod(0o755)
        with self.assertRaises(ChatStorageError):
            ChatService(root).snapshot()

        missing = self.tmp / "absent"
        with Index(self.v3 / "index.sqlite") as index:
            ingest(index, claude=missing, codex=missing, jarvis=missing, chats=root)
            self.assertEqual(index.recent(5), [])


if __name__ == "__main__":
    unittest.main()
