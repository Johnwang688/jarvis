from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from chat_check import Model, reply
from jarvis import config, llm
from jarvis.v3.chat import ChatService


class ListenerReview(unittest.TestCase):
    def start(self, *steps):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(
            config, V3_DIR=root, V3_POLICY_FILE=root / "no-policy.json",
            GOOGLE_TOKEN_PATH=root / "no-google-token.json",
        ))
        live = self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("live model")))
        self.addCleanup(live.assert_not_called)
        chat = ChatService(root / "chats", completion=Model(*steps))
        self.addCleanup(chat.close)
        return chat

    def test_slow_listener_does_not_block_typed_stream(self):
        entered, release, stream_finished = Event(), Event(), Event()

        def stream(on_delta, **kwargs):
            on_delta("A fixture answer.")
            stream_finished.set()
            return reply("A fixture answer.")

        chat = self.start(stream)
        self.addCleanup(release.set)

        def listener(event):
            if event["kind"] == "delta":
                entered.set()
                release.wait(5)

        self.addCleanup(chat.subscribe(listener))
        sender = Thread(target=chat.send, args=("Typed request.",), daemon=True)
        sender.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(stream_finished.wait(1), "The listener blocked the completion stream.")
        finally:
            release.set()
            sender.join(5)

    def test_close_stops_delivery_to_later_listeners(self):
        model_entered, release_model = Event(), Event()
        listener_entered, release_listener = Event(), Event()
        callbacks = []

        def stream(**kwargs):
            model_entered.set()
            release_model.wait(5)
            return reply("A fixture answer.")

        chat = self.start(stream, reply("A second fixture answer."))

        def listener(event):
            if event["kind"] == "steer":
                listener_entered.set()
                release_listener.wait(5)

        self.addCleanup(chat.subscribe(listener))
        self.addCleanup(chat.subscribe(lambda event: callbacks.append(event["kind"])))
        chat.send("Typed request.")
        sender = Thread(target=chat.send, args=("Typed follow-up.",), daemon=True)
        try:
            self.assertTrue(model_entered.wait(5))
            sender.start()
            self.assertTrue(listener_entered.wait(5))
            release_model.set()
            self.assertTrue(chat.close(grace_s=2))
            closed_count = len(callbacks)
        finally:
            release_model.set()
            release_listener.set()
            if sender.ident is not None:
                sender.join(5)
        self.assertFalse(sender.is_alive())
        self.assertEqual(callbacks[closed_count:], [])


if __name__ == "__main__":
    unittest.main()
