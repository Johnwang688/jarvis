"""Paired Windows action confirmations through the real voice link, HUD selection and chat log."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import voice_link_va245_impl_check as fixture
from jarvis.v2 import chat_reads, projects, voice_link
from windows.voiceflow import Flow


class ConfirmedAction(fixture.Base):
    def setUp(self):
        super().setUp()
        self.start()
        self.wire = self.client()
        self.wire.ping()
        self.actions, self.typer, self.ui, self.player = Mock(), Mock(), Mock(), Mock()
        self.player.cancelled = threading.Event()
        self.actions.open_app.return_value = self.actions.switch_app.return_value = True
        self.actions.search.return_value = True
        self.actions.read_selection.return_value = "private selection sentinel"
        self.link.speaker = lambda _: b"test WAV"
        self.flow = Flow(self.wire, self.typer, self.actions, self.ui, self.player)

    def ask_local(self, text, source="wake"):
        self.flow.on_clip(None, source, transcript=text)

    def route(self, text, source="wake"):
        return self.wire.route(text, source, with_receipt=True)

    def notices(self, thread=None):
        return [r for r in self.d.stores.threads.read_log((thread or self.chosen).id) if r["kind"] == "notice"]

    def test_success_after_open_switch_search_and_completed_read(self):
        self.actions.open_app.side_effect = lambda _: not self.notices()
        self.ask_local("open Chrome")
        self.ask_local("Hey Jarvis, switch to Calendar", "hotkey")
        self.ask_local("search for private query sentinel")
        self.ask_local("read the selection")
        self.assertEqual([r["data"]["text"] for r in self.notices()], [
            "Jarvis voice opened Chrome.", "Jarvis voice switched to Calendar.",
            "Jarvis voice opened a web search.", "Jarvis voice read the selected text aloud."])
        self.actions.open_app.assert_called_once_with("Chrome")
        self.actions.switch_app.assert_called_once_with("Calendar")
        self.actions.search.assert_called_once_with("private query sentinel")
        self.actions.read_selection.assert_called_once_with()
        self.player.play.assert_called_once_with(b"test WAV")
        rows = self.d.stores.threads.read_log(self.chosen.id)
        self.assertFalse(any(r["kind"] in ("user", "turn_started", "text") for r in rows))
        self.assertNotIn("private", json.dumps(rows))
        self.assertIn("Jarvis voice opened Chrome.", chat_reads.read_tail(self.d.stores, self.chosen))
        self.assertEqual(self.fast.messages, [])

    def test_failure_literal_ordinary_dictation_or_cancelled_playback_never_add_a_notice(self):
        self.actions.open_app.return_value = False
        self.actions.switch_app.return_value = False
        self.actions.search.return_value = False
        self.ask_local("open Chrome")
        self.ask_local("switch to Calendar")
        self.ask_local("search for something")
        self.player.cancelled.set()
        self.ask_local("read the selection")
        self.actions.read_selection.return_value = None
        self.ask_local("read the selection")
        self.ask_local("type the words Jarvis open Chrome", "hotkey")
        self.typer.run.assert_called_once_with([("text", "Jarvis open Chrome")])
        self.assertNotIn("receipt", self.route("open Chrome", "hotkey"))
        self.assertNotIn("receipt", self.route("I am making a shopping list", "hotkey"))
        self.assertNotIn("receipt", self.route("Jarvis, how does Chrome work?", "hotkey"))
        self.assertEqual(self.notices(), [])

    def test_held_shortcut_types_unaddressed_commands_and_preserves_edit_controls(self):
        phrases = ["open Chrome", "could you search for maps?", "she said Jarvis open Chrome",
                   "would you please switch to Teams?", "Jarvis's shortcut opens the app"]
        for words in phrases:
            self.ask_local(words, "hotkey")
        inserts = [call.args[0][0][:2] for call in self.typer.run.call_args_list]
        self.assertEqual(inserts, [("text", words) for words in phrases])
        for words, expected in (("scratch that", [("scratch",)]), ("send it", [("key", "enter")]),
                                ("new line", [("key", "shift+enter")]),
                                ("type the words send it", [("text", "send it")])):
            self.ask_local(words, "hotkey")
            self.assertEqual(self.typer.run.call_args.args[0], expected)
        self.assertEqual(self.actions.mock_calls, [])
        self.assertEqual(self.fast.messages, [])
        self.assertEqual(self.notices(), [])

    def test_confirmed_once_before_expiry_and_in_original_active_chat(self):
        ticket = self.route("Jarvis open Chrome", "hotkey")["receipt"]
        self.assertEqual(self.notices(), [])
        other = self.d.open_thread(self.chosen.project_id, "chat", "fast", {})
        self.select(other.id)
        self.assertTrue(self.wire.confirm_action(ticket))
        self.assertFalse(self.wire.confirm_action(ticket))
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.notices(other), [])

        superseded = self.route("open Calendar")["receipt"]
        current = self.route("open Paint")["receipt"]
        self.assertFalse(self.wire.confirm_action(superseded))
        self.assertTrue(self.wire.confirm_action(current))
        with patch.object(voice_link, "ACTION_RECEIPT_S", -1):
            expired = self.route("open Teams")["receipt"]
        self.assertFalse(self.wire.confirm_action(expired))
        self.assertEqual([r["data"]["text"] for r in self.notices(other)], ["Jarvis voice opened Paint."])

    def test_selection_is_pinned_before_routing_finishes(self):
        other = self.d.open_thread(self.chosen.project_id, "chat", "fast", {})
        route = self.link.route_model
        def switch_while_routing(text, source):
            self.select(other.id)
            return route(text, source)
        with patch.object(self.link, "route_model", side_effect=switch_while_routing):
            self.ask_local("open Chrome")
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.notices(other), [])

    def test_scope_is_paired_connection_and_active_owner_selected_chat(self):
        ticket = self.route("open Chrome")["receipt"]
        replacement = self.client()
        replacement.ping()
        self.assertFalse(replacement.confirm_action(ticket))
        ticket = replacement.route("open Paint", "wake", with_receipt=True)["receipt"]
        projects.archive_thread(self.d, self.chosen.id)
        self.assertFalse(replacement.confirm_action(ticket))
        self.assertNotIn("receipt", replacement.route("open Chrome", "wake", with_receipt=True))
        self.assertEqual(self.notices(), [])

    def test_legacy_client_and_legacy_server_skip_receipts(self):
        # Older clients request the unextended route; newer clients only opt in
        # when the server advertised support during ping.
        self.assertEqual(set(self.wire.route("open Chrome", "wake")), {"kind", "text", "target"})
        self.wire.action_receipts = False
        self.ask_local("open Chrome")
        self.actions.open_app.assert_called_once_with("Chrome")
        self.assertEqual(self.notices(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
