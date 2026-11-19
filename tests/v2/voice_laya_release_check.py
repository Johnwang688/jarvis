"""Exercise negotiated input modes and reviewed feedback through a paired voice link."""
import threading
import unittest
from unittest.mock import Mock, patch

from tests.v2 import voice_link_va245_impl_check as fixture
from jarvis.v2.route_feedback import RouteFeedback
from windows.voiceflow import Flow


class PairedRouting(fixture.Base):
    def setUp(self):
        super().setUp()
        self.start()
        self.wire = self.client()
        self.wire.ping()
        self.assertTrue(self.wire.laya_routing)
        self.actions, self.typer, player = Mock(), Mock(), Mock()
        player.cancelled = threading.Event()
        self.flow = Flow(self.wire, self.typer, self.actions, Mock(), player)
        self.feedback = RouteFeedback(self.d.stores.root)

    def clip(self, mode, text):
        self.flow.on_clip(None, mode, transcript=text)

    @patch("jarvis.v2.laya_service.LayaRouter.route")
    def test_only_assistant_turn_becomes_reviewable(self, route):
        text = "Open boo uh youtube.com"
        route.return_value = {"kind": "navigate", "target": "https://youtube.com",
                              "scores": {"navigate": .99, "open_app": .01}}
        self.actions.navigate.return_value = True
        self.clip("dictation", text)
        self.assertEqual(self.typer.run.call_args.args[0][0][:2], ("text", text))
        self.clip("wake", "Jarvis, type the words open youtube.com and send it")
        self.assertEqual(self.typer.run.call_args.args, ([("text", "open youtube.com and send it")],))
        self.assertFalse(self.feedback.recent())
        route.assert_not_called()

        self.typer.reset_mock()
        self.clip("assistant", text)
        self.actions.navigate.assert_called_once_with("https://youtube.com")
        route.assert_called_once()
        self.typer.run.assert_not_called()
        turn, = self.feedback.recent()
        self.assertEqual((turn["mode"], turn["text"], turn["kind"]), ("assistant", text, "navigate"))
        self.assertEqual(turn["scores"]["navigate"], .99)
        self.assertIn("confirmed", turn["outcomes"])
        self.assertEqual(list(self.feedback.examples()), [])
        self.assertTrue(self.feedback.correct(turn["id"], "navigate"))
        example, = self.feedback.examples()
        self.assertEqual((example["text"], example["label"]), (text, "navigate"))

    @patch("jarvis.v2.laya_service.LayaRouter.route")
    def test_failed_handoff_does_not_confirm(self, route):
        route.return_value = {"kind": "navigate", "target": "https://youtube.com",
                              "scores": {"navigate": .99}}
        self.actions.navigate.return_value = False
        self.clip("wake", "Open youtube.com")
        self.actions.navigate.assert_called_once()
        self.typer.run.assert_not_called()
        turn, = self.feedback.recent()
        self.assertNotIn("confirmed", turn["outcomes"])
        self.assertEqual(list(self.feedback.examples()), [])
        self.assertFalse(any(r["kind"] == "notice" for r in self.d.stores.threads.read_log(self.chosen.id)))


if __name__ == "__main__":
    unittest.main()
