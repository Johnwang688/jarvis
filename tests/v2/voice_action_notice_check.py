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
from windows.voiceactions import LaunchStatus
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

    def legacy_helper(self):
        # A helper that never requested Laya routing keeps the legacy wake path.
        self.wire.request_laya = False
        self.wire.ping()
        self.assertFalse(self.wire.laya_routing)

    def ask_local(self, text, source="wake"):
        self.flow.on_clip(None, source, transcript=text)

    def route(self, text, source="wake"):
        return self.wire.route(text, source, with_receipt=True)

    def notices(self, thread=None):
        return [r for r in self.d.stores.threads.read_log((thread or self.chosen).id) if r["kind"] == "notice"]

    def test_success_after_open_switch_search_and_completed_read(self):
        self.legacy_helper()
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
        self.assertNotIn("receipt", self.route("how do I open Chrome?", "hotkey"))
        self.assertNotIn("receipt", self.route("I am making a shopping list", "hotkey"))
        self.assertNotIn("receipt", self.route("Jarvis, how does Chrome work?", "hotkey"))
        self.assertEqual(self.notices(), [])

    def test_go_to_site_navigates_once_without_typing_the_command(self):
        self.legacy_helper()
        self.actions.navigate.return_value = True
        for phrase, source in (("Go to youtube.com", "hotkey"), ("Go to YouTube dot com.", "hotkey"),
                               ("Jarvis, go to youtube.com.", "hotkey"), ("Go to youtube.com.", "wake")):
            with self.subTest(phrase=phrase, source=source):
                self.actions.navigate.reset_mock()
                self.ask_local(phrase, source)
                self.actions.navigate.assert_called_once()
                self.assertEqual(self.actions.navigate.call_args.args[0].rstrip("/"), "https://youtube.com")
                self.typer.run.assert_not_called()
                self.actions.search.assert_not_called()

    def test_go_to_site_still_allows_explicit_dictation_and_ordinary_prose(self):
        for words, expected in (("Type the words go to youtube.com", "go to youtube.com"),
                                ("I usually go to youtube.com", "I usually go to youtube.com")):
            self.ask_local(words, "hotkey")
            self.assertEqual(self.typer.run.call_args.args[0][0][:2], ("text", expected))
            self.actions.navigate.assert_not_called()

    def test_held_shortcut_types_other_unaddressed_commands_and_preserves_edit_controls(self):
        phrases = ["how do I open Chrome?", "could you search for maps?", "she said Jarvis open Chrome",
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

    def test_profile_reply_finishes_original_launch_in_original_chat(self):
        self.actions.open_app.return_value = LaunchStatus.CHROME_PROFILE_REQUIRED
        self.actions.chrome_profile_options.return_value = ("Private profile name",)
        self.actions.choose_chrome_profile.return_value = LaunchStatus.LAUNCHED
        self.ask_local("Open Chrome.", "hotkey")
        self.assertEqual(self.notices(), [])
        other = self.d.open_thread(self.chosen.project_id, "chat", "fast", {})
        self.select(other.id)
        # The network worker constructs a new Flow for the next capture.
        followup = Flow(self.wire, self.typer, self.actions, self.ui, self.player)
        followup.on_clip(None, "hotkey", transcript="Private profile name.")
        self.actions.choose_chrome_profile.assert_called_once_with("Private profile name")
        self.assertEqual([n["data"]["text"] for n in self.notices()], ["Jarvis voice opened Chrome."])
        self.assertEqual(self.notices(other), [])
        self.assertEqual(self.fast.messages, [])
        self.typer.run.assert_not_called()

    def test_natural_and_repeated_profile_replies_stay_in_the_picker_conversation(self):
        from tests.voice_chrome_picker_check import Choose

        for reply, selected in [("Cancel, please.", False), ("I'd like Work.", True)]:
            with self.subTest(reply=reply):
                fixture = Choose()
                self.flow.actions = fixture.make()
                notices_before = len(self.notices())
                self.ask_local("Open Chrome.", "hotkey")
                self.ask_local(reply, "hotkey")
                if not selected:
                    self.ui.set_state.assert_called_with("done", "Chrome profile selection canceled.")
                self.ask_local("Work.", "hotkey")
                self.assertEqual(fixture.backend.invoked, ["Work"] if selected else [])
                self.assertEqual(len(self.notices()) - notices_before, int(selected))
                self.typer.run.assert_not_called()

    def start_work_profile_prompt(self):
        from tests.voice_chrome_picker_check import Choose

        fixture = Choose()
        self.flow.actions = fixture.make()
        self.ask_local("Open Chrome.", "hotkey")
        return fixture.backend

    def test_expired_prompt_recognizes_multiple_late_replies_inside_grace(self):
        from dataclasses import replace
        import time

        backend = self.start_work_profile_prompt()
        pending = self.wire._pending_chrome_profile
        self.wire._pending_chrome_profile = replace(pending, expires_at=time.monotonic() - 0.01)
        self.ask_local("Work.", "hotkey")
        self.ask_local("I'd like Work.", "hotkey")
        self.assertEqual(backend.invoked, [])
        self.assertEqual(self.notices(), [])
        self.typer.run.assert_not_called()

    def test_unrelated_dictation_during_grace_preserves_repeat_recognition(self):
        backend = self.start_work_profile_prompt()
        self.ask_local("Work.", "hotkey")
        self.ask_local("Add milk to the shopping list.", "hotkey")
        self.typer.run.assert_called_once()
        self.typer.run.reset_mock()
        self.ask_local("Work.", "hotkey")
        self.assertEqual(backend.invoked, ["Work"])
        self.assertEqual(len(self.notices()), 1)
        self.typer.run.assert_not_called()

    def test_grace_ends_at_original_expiry_even_if_transcription_delays_check(self):
        from dataclasses import replace
        import time

        backend = self.start_work_profile_prompt()
        pending = self.wire._pending_chrome_profile
        self.wire._pending_chrome_profile = replace(pending, expires_at=time.monotonic() - 11)
        self.ask_local("Work.", "hotkey")
        self.typer.run.assert_called_once()
        self.assertEqual(backend.invoked, [])
        self.assertEqual(self.notices(), [])

    def test_first_replacement_capture_sets_deadline_even_when_picker_returns_later(self):
        import queue
        from types import SimpleNamespace

        from tests.voice_chrome_picker_check import Choose
        from windows import voice_agent, voiceflow

        picker = Choose()
        actions = picker.make()
        clock = picker.clock
        clock.monotonic = clock
        agent = voice_agent.Agent.__new__(voice_agent.Agent)
        agent.done, agent.stop_requested, agent.profile_cancelled = (threading.Event() for _ in range(3))
        agent.recording, agent.flow_running = threading.Event(), threading.Event()
        agent.generation_lock = threading.Lock()
        agent.generation = 1
        agent.clips = Mock(spec=queue.Queue)
        agent.overlay, agent.player = self.ui, self.player
        agent.player.begin.side_effect = agent.player.cancelled.clear
        agent.host, agent.port, agent.key_path = "unused", 0, "unused"
        agent.user32 = SimpleNamespace(GetAsyncKeyState=lambda key: 0)
        typed = []
        self.typer.run.side_effect = lambda steps: typed.append((clock.now, steps[0][1]))

        def interrupt_lookup(_):
            clock.now = 104
            agent.player.cancelled.set()
            agent.next_generation()
            clock.now = 104.75
            agent.next_generation()
            clock.now = 105

        def clips():
            yield agent.generation, b"Open Chrome.", "hotkey"
            picker.backend.on_read = interrupt_lookup
            yield agent.next_generation(), b"Work.", "hotkey"
            self.assertEqual(clock.now, 105)
            clock.now = 113.99
            yield agent.generation, b"Work.", "hotkey"
            clock.now = 114
            yield agent.next_generation(), b"Work.", "hotkey"
            agent.done.set()
            raise queue.Empty

        agent.clips.get.side_effect = clips()
        for module in (voiceflow, voice_agent):
            self.enterContext(patch.object(module, "time", clock))
        for name, value in (("Client", self.wire), ("load_key", b"test-key"),
                            ("Actions", actions), ("Typer", self.typer)):
            self.enterContext(patch.object(voice_agent, name, return_value=value))
        self.enterContext(patch.object(self.wire, "connect"))
        self.enterContext(patch.object(self.wire, "dictate", side_effect=lambda wav, **kw: {"text": wav.decode()}))
        agent.network()

        self.assertEqual((picker.backend.invoked, self.notices()), ([], []))
        self.assertEqual(typed, [(114, "Work.")])

    def test_failed_profile_invoke_cannot_confirm_a_manual_profile_switch(self):
        from tests.voice_chrome_picker_check import Choose, PICKER, window
        from windows.chromepicker import PickerUnavailable

        fixture = Choose()
        actions = fixture.make()
        def manual_selection(_):
            fixture.backend.windows.pop(PICKER.hwnd)
            fixture.backend.windows[30] = window(30, aumid="Chrome")
            fixture.backend.fg = 30
            raise PickerUnavailable("invoke failed")
        fixture.backend.on_invoke = manual_selection
        flow = Flow(self.wire, self.typer, actions, self.ui, self.player)
        flow.on_clip(None, "hotkey", transcript="Open Chrome.")
        flow.on_clip(None, "hotkey", transcript="Work.")
        self.assertEqual(self.notices(), [])

    def test_selection_is_pinned_before_routing_finishes(self):
        self.legacy_helper()
        other = self.d.open_thread(self.chosen.project_id, "chat", "fast", {})
        route = self.link.route_model
        def switch_while_routing(text, source):
            self.select(other.id)
            return route(text, source)
        with patch.object(self.link, "route_model", side_effect=switch_while_routing):
            self.ask_local("open Chrome")
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.notices(other), [])

    def test_unaddressed_open_can_confirm_only_against_its_original_chat(self):
        other = self.d.open_thread(self.chosen.project_id, "chat", "fast", {})
        route = self.link.route_model
        def switch_while_routing(text, source):
            self.select(other.id)
            return route(text, source)
        with patch.object(self.link, "route_model", side_effect=switch_while_routing):
            got = self.route("Open Google Chrome for me.", "hotkey")
        self.assertEqual((got["kind"], got["target"]), ("open_app", "Google Chrome"))
        self.assertEqual(self.notices(), [])
        self.assertTrue(self.wire.confirm_action(got["receipt"]))
        self.assertFalse(self.wire.confirm_action(got["receipt"]))
        self.assertEqual([r["data"]["text"] for r in self.notices()], ["Jarvis voice opened Google Chrome."])
        self.assertEqual(self.notices(other), [])
        self.select(None)
        self.assertEqual(self.route("Open Google Chrome for me.", "hotkey"),
                         {"kind": "open_app", "text": "Open Google Chrome for me.", "target": "Google Chrome"})

    def test_unaddressed_launch_requires_exact_catalog_capability_and_matching_words(self):
        phrase = "Open Google Chrome for me."
        for flag in ({}, {"exact_open": False}, {"exact_open": 1}):
            response = self.wire._request({"op": "route", "source": "hotkey", "text": phrase,
                                           "receipt": True, **flag})
            self.assertEqual(response["intent"], {"kind": "dictate", "target": "literal", "text": phrase})
            self.assertNotIn("receipt", response)
        with patch.object(self.link, "route_model", return_value={"kind": "open_app", "target": "Paint"}):
            mismatched = self.route(phrase, "hotkey")
        self.assertEqual(mismatched, {"kind": "dictate", "target": "literal", "text": phrase})

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
        self.legacy_helper()
        self.wire.action_receipts = False
        self.ask_local("open Chrome")
        self.actions.open_app.assert_called_once_with("Chrome")
        self.assertEqual(self.notices(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
