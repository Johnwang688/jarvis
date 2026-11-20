"""Independent voice hand-off regression probes using the isolated CU-3d fixture."""
from concurrent.futures import ThreadPoolExecutor
import contextvars
import socket
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, patch

from jarvis import runtime, tools
from jarvis.v2 import bridge, voice_link
from jarvis.v2.callers import Caller
from jarvis.v2.daemon import APIError
from jarvis.v2.desk import SelectedVoiceMessage
from jarvis.v2.desktop_driver import DRIVER_BRIEF
from jarvis.v2.provider import Event, EventKind, UserMessage
from jarvis.v2.tools import hud_window
from jarvis.v2.voice_chat import DRIVER_STILL_WORKING, NoVoiceTarget
import mcp_session_check as fixture
import voice_driver_handoff_check as handoff_fixture
from windows import voiceflow, voicewire


class Review(fixture.DaemonHarness):
    make_daemon = handoff_fixture.Handoff.make_daemon
    hold = handoff_fixture.Handoff.hold

    def setUp(self):
        super().setUp()
        self.driver, self.selected = self.chat(), self.chat()
        self.d.voice_chat.select(self.selected.id)
        self.link = self.d.voice_link
        self.link._feedback = Mock()
        self.link._feedback.record.return_value = "fixture-row"
        self.client = voicewire.Client(*self.link.address, handoff_fixture.KEY)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.ping()
        self.assertTrue(self.client.laya_routing)

    def grant(self, on=True):
        self.d.set_desktop_driving(self.driver.id, on)

    def helper(self, *, driving):
        self.client.streaming = False
        typer, actions = Mock(), Mock()
        flow = voiceflow.Flow(self.client, typer, actions, Mock(), Mock(), driving=lambda: driving)
        return flow, typer, actions

    @unittest.expectedFailure  # lead: documented edge case
    def test_stale_idle_helper_routes_explicit_assistant_literal_to_driver(self):
        self.grant()
        flow, typer, _ = self.helper(driving=False)
        text = "Jarvis, type the words hello"
        seen = []
        self.claude.script[self.driver.id] = lambda h, m: seen.append(m.text)
        with patch.object(self.link, "speaker", return_value=b"fake WAV"), \
             patch.object(self.link, "_laya_decide") as decide:
            flow.on_clip(None, "assistant", transcript=text)
        self.assertEqual((seen, typer.run.call_count), ([DRIVER_BRIEF + "\n\n" + text], 0))
        decide.assert_not_called()

    @unittest.expectedFailure  # lead: documented edge case
    def test_stale_idle_helper_does_not_execute_old_website_choice(self):
        self.grant()
        flow, _, actions = self.helper(driving=False)
        actions.navigate.return_value = True
        self.client._pending_site_choice = voiceflow._SiteChoice(
            self.client.session, ("https://example.test", "https://example.org"),
            None, None, time.monotonic() + 30)
        seen = []
        self.claude.script[self.driver.id] = lambda h, m: seen.append(m.text)
        with patch.object(self.link, "speaker", return_value=b"fake WAV"):
            flow.on_clip(None, "wake", transcript="first")
        self.assertEqual((seen, actions.navigate.call_count), ([DRIVER_BRIEF + "\n\nfirst"], 0))

    @unittest.expectedFailure  # lead: documented edge case
    def test_stale_driving_helper_does_not_ask_selected_chat_after_native_route(self):
        flow, _, actions = self.helper(driving=True)
        seen = []
        self.claude.script[self.selected.id] = lambda h, m: seen.append(m.text)
        url = "https://example.org"
        with patch.object(self.link, "_laya_decide", return_value={"kind": "navigate", "target": url}), \
             patch.object(self.link, "speaker", return_value=b"fake WAV"):
            flow.on_clip(None, "assistant", transcript=f"Go to {url}")
        self.assertEqual(seen, [], "a stale badge must not redirect a native action into another chat")

    def test_legacy_route_discards_fast_action_when_grant_starts_during_inference(self):
        def decide(*_):
            self.grant()
            return {"kind": "open_app", "target": "Notepad"}

        with patch.object(self.link, "route_model", side_effect=decide):
            intent = self.client.route("Jarvis, open Notepad", "hotkey", with_receipt=True)
        self.assertEqual((intent["kind"], "receipt" in intent), ("ask", False))

    def test_laya_outage_before_grant_does_not_divert_same_routed_ask(self):
        seen = []
        self.claude.script[self.driver.id] = lambda h, m: seen.append(m.text)
        text = "Open Notepad"
        with patch.object(self.link, "_laya_decide", return_value=None), \
             patch.object(self.link, "converser", return_value="conversation only"):
            intent = self.client.laya_route(text, "wake")
            self.assertEqual(intent["target"], "conversation")
            self.grant()
            answer = self.client.ask(text, turn=intent["turn"])
        self.assertEqual((answer, seen), ("done", [DRIVER_BRIEF + "\n\n" + text]))

    # lead: documented edge case (E26): a helper that has not yet heard driving
    # started can run a fast action it was already told to; the daemon refuses its receipt.
    @unittest.expectedFailure
    def test_grant_start_after_laya_route_prevents_local_action_and_notice(self):
        flow, _, actions = self.helper(driving=False)
        actions.navigate.return_value = True
        original_route = self.client.laya_route
        url = "https://example.org"
        seen = []
        text = f"Go to {url}"
        self.claude.script[self.driver.id] = lambda h, m: seen.append(m.text)

        def route(*args, **kwargs):
            intent = original_route(*args, **kwargs)
            self.grant()
            return intent

        with patch.object(self.link, "_laya_decide", return_value={"kind": "navigate", "target": url}), \
             patch.object(self.link, "speaker", return_value=b"fake WAV"), \
             patch.object(self.client, "laya_route", side_effect=route), \
             patch.object(self.d.voice_chat, "note_action", wraps=self.d.voice_chat.note_action) as notice:
            flow.on_clip(None, "assistant", transcript=text)
            self.assertEqual((seen, actions.navigate.call_count, notice.call_count),
                             ([DRIVER_BRIEF + "\n\n" + text], 0, 0))

    def test_unaddressed_hotkey_navigation_dictates_while_driving(self):
        self.grant()
        flow, typer, actions = self.helper(driving=True)
        url = "https://example.org"
        text = f"Go to {url}"
        with patch.object(self.link, "route_model", return_value={"kind": "navigate", "target": url}), \
             patch.object(self.client, "ask", wraps=self.client.ask) as ask:
            flow.on_clip(None, "hotkey", transcript=text)
        ask.assert_not_called()
        actions.navigate.assert_not_called()
        typer.run.assert_called_once_with([("text", text)])

    @unittest.expectedFailure  # lead: documented edge case
    def test_expired_driver_handoff_does_not_fall_back_to_selected_chat(self):
        self.grant()
        text = "Type the words hello into the active window."
        seen = []
        self.claude.script[self.selected.id] = lambda h, m: seen.append(m.text)
        intent = self.client.laya_route(text, "assistant", with_receipt=True)
        self.assertEqual(intent["kind"], "ask")
        self.assertNotIn("receipt", intent)
        self.grant(False)
        try:
            self.client.ask(text, turn=intent["turn"])
        except voicewire.VoiceQuestionError:
            pass
        self.assertEqual(seen, [], "an expired hand-off must not send a desktop command to the selected chat")

    def test_voice_handoff_queues_behind_an_off_desk_turn(self):
        entered, release = self.hold(self.driver)
        self.grant()
        self.d.deliver(self.driver.id, UserMessage("Current phone turn", via="phone"))
        self.assertTrue(entered.wait(2))
        seen = []
        self.claude.script[self.driver.id] = lambda h, m: seen.append(m.text)
        text = "List my windows"
        original_assign = self.d.voice_chat.assign
        assigned = threading.Event()

        def capture(message, thread, turn):
            original_assign(message, thread, turn)
            if isinstance(message, SelectedVoiceMessage):
                assigned.set()

        with patch.object(self.claude, "steer", create=True, return_value=True) as steer, \
             patch.object(self.d.voice_chat, "assign", side_effect=capture), \
             ThreadPoolExecutor() as pool:
            try:
                ask = pool.submit(self.client.ask, text)
                self.assertTrue(assigned.wait(2))
            finally:
                release.set()
            self.assertEqual(ask.result(2), "done")
        self.assertEqual(seen, [DRIVER_BRIEF + "\n\n" + text])
        steer.assert_not_called()

    def test_late_driver_reply_is_not_played_for_the_next_ask(self):
        self.grant()
        first_entered, second_delivered, release_first = (threading.Event() for _ in range(3))
        first_text, second_text = "First driver request", "Second driver request"
        first_reply, second_reply = "Late reply to the first request.", "Reply to the second request."
        voice = self.d.voice_chat
        original_deliver = self.d.deliver
        audio = []

        def send(handle, message):
            first = message.text.endswith(first_text)
            yield Event(EventKind.TURN_STARTED, handle.thread_id)
            if first:
                first_entered.set()
                release_first.wait(4)
            yield Event(EventKind.TEXT, handle.thread_id, {"text": first_reply if first else second_reply})
            yield Event(EventKind.TURN_FINISHED, handle.thread_id, {"stop": "end"})

        def deliver(thread_id, message):
            result = original_deliver(thread_id, message)
            if message.text == second_text:
                second_delivered.set()
            return result

        with patch.object(self.claude, "send", side_effect=send), \
             patch.object(self.claude, "steer", create=True, return_value=True), \
             patch.object(self.claude, "interrupt") as interrupt, \
             patch.object(self.d, "deliver", side_effect=deliver), \
             patch.object(self.link, "speaker", side_effect=str.encode), ThreadPoolExecutor() as pool:
            try:
                first = pool.submit(voice.ask, first_text, lambda _: None, timeout=0.2)
                self.assertTrue(first_entered.wait(2))
                self.assertEqual(first.result(2), DRIVER_STILL_WORKING)
                self.assertEqual((voice._message, voice._turn), (None, None))
                self.assertIsNotNone(self.d._sessions[self.driver.id].worker)
                interrupt.assert_not_called()
                second = pool.submit(self.client.ask_spoken, second_text, lambda _: None, audio.append)
                self.assertTrue(second_delivered.wait(2))
            finally:
                release_first.set()
            self.assertEqual((second.result(2), audio), (second_reply, [second_reply.encode()]))

    @unittest.expectedFailure
    def test_ask_becoming_driver_at_admission_keeps_spoken_timeout(self):
        entered, release = self.hold(self.driver)
        audio = []
        original_active = self.link._driver_active

        def snapshot():
            active = original_active()
            if not active:
                self.grant()
            return active

        with patch.object(self.link, "_driver_active", side_effect=snapshot), \
             patch.object(voice_link, "ASK_TIMEOUT_S", 0.2), \
             patch.object(self.link, "speaker", return_value=b"fake WAV"), ThreadPoolExecutor() as pool:
            answer = pool.submit(self.client.ask_spoken, "Work in the driver chat", lambda _: None, audio.append)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(answer.result(2), DRIVER_STILL_WORKING)
                self.assertEqual(audio, [b"fake WAV"])
            finally:
                release.set()

    @unittest.expectedFailure
    def test_driver_switch_during_target_resolution_does_not_deliver_to_former_driver(self):
        self.grant()
        original_subscribe = self.d.bus.subscribe
        sent = []
        self.claude.script[self.driver.id] = lambda h, m: sent.append(h.thread_id)

        def subscribe(filters):
            self.grant(False)
            self.d.set_desktop_driving(self.selected.id, True)
            return original_subscribe(filters)

        with patch.object(self.d.bus, "subscribe", side_effect=subscribe):
            try:
                self.d.voice_chat.ask("List my windows", lambda _: None, timeout=2)
            except NoVoiceTarget:
                pass
        self.assertEqual(sent, [])

    def test_disconnect_clears_playback_ownership_without_interrupting_driver(self):
        self.grant()
        voice = self.d.voice_chat
        entered, release = self.hold(self.driver)
        with patch.object(self.claude, "interrupt") as interrupt, ThreadPoolExecutor() as pool:
            ask = pool.submit(self.client.ask, "Keep running in the driver chat")
            try:
                self.assertTrue(entered.wait(2))
                self.client._socket.shutdown(socket.SHUT_RDWR)
                with self.assertRaises(voicewire.LinkError):
                    ask.result(2)
                self.assertTrue(voice._ask_lock.acquire(timeout=2))
                try:
                    self.assertEqual((voice._message, voice._turn), (None, None))
                    self.assertIsNotNone(self.d._sessions[self.driver.id].worker)
                    interrupt.assert_not_called()
                finally:
                    voice._ask_lock.release()
            finally:
                release.set()

    def test_idle_laya_preserves_native_receipt_and_feedback(self):
        url = "https://example.org"
        text = f"Go to {url}"
        with patch.object(self.link, "_laya_decide", return_value={"kind": "navigate", "target": url}), \
             patch.object(self.d.voice_chat, "note_action", wraps=self.d.voice_chat.note_action) as note:
            intent = self.client.laya_route(text, "assistant", with_receipt=True)
            self.assertTrue(self.client.confirm_action(intent["receipt"]))
            self.assertFalse(self.client.confirm_action(intent["receipt"]))
            self.client.route_outcome(intent["turn"], "confirmed")
            note.assert_called_once_with(self.selected.id, "navigate", "")
        self.link._feedback.record.assert_called_once()
        self.link._feedback.outcome.assert_called_once_with("fixture-row", "confirmed")

    def test_driver_grant_rejects_and_consumes_outstanding_native_receipt(self):
        url = "https://example.org"
        with patch.object(self.link, "_laya_decide", return_value={"kind": "navigate", "target": url}), \
             patch.object(self.d.voice_chat, "note_action", wraps=self.d.voice_chat.note_action) as note:
            intent = self.client.laya_route(f"Go to {url}", "assistant", with_receipt=True)
            self.grant()
            self.assertFalse(self.client.confirm_action(intent["receipt"]))
            self.grant(False)
            self.assertFalse(self.client.confirm_action(intent["receipt"]))
            note.assert_not_called()

    def assert_window_inaccessible(self, chat):
        session = self.d._sessions[chat.id]
        caller = Caller(chat.id, session, chat.provider.value, 0)
        with patch("jarvis.v2.callers.authenticate", return_value=(caller, session)), \
             patch.object(bridge, "_api_listener", return_value=True):
            _, listing = bridge.route(SimpleNamespace(command="GET"), self.d, ["mcp", "tools"], {})
        self.assertFalse({row["name"] for row in listing["tools"]} & hud_window.HUD_WINDOW_TOOLS)
        if chat.provider == fixture.FAST:
            with self.assertRaises(APIError):
                bridge.call(self.d, session, caller, "hud_window_list", {})
            return
        self.assertTrue(bridge.call(self.d, session, caller, "hud_window_list", {})["isError"])

    def test_window_gate_rejects_other_turn_sources_and_non_driver_chat(self):
        self.grant()
        with patch.object(hud_window.SESSION, "request") as request:
            cases = [(self.driver, UserMessage("ordinary", via=via))
                     for via in ("phone", "discord", "peer", "inbox", "system")]
            cases.extend([
                (self.driver, UserMessage("unpaired", via="voice", spoken=True)),
                (self.selected, SelectedVoiceMessage("paired, other chat")),
                (self.chat(fixture.FAST), SelectedVoiceMessage("paired, fast provider")),
            ])
            for chat, message in cases:
                with self.subTest(via=message.via, provider=chat.provider, thread=chat.id):
                    entered, release = self.hold(chat)
                    try:
                        self.d.deliver(chat.id, message)
                        self.assertTrue(entered.wait(2))
                        self.assert_window_inaccessible(chat)
                    finally:
                        release.set()
                    self.wait_idle(chat.id)
            request.assert_not_called()

    def test_window_gate_rechecks_socket_depth_steer_and_stop(self):
        self.grant()
        entered, release = self.hold(self.driver)
        self.d.deliver(self.driver.id, SelectedVoiceMessage("paired"))
        self.assertTrue(entered.wait(2))
        session = self.d._sessions[self.driver.id]
        caller = Caller(self.driver.id, session, self.driver.provider.value, 0)

        def child_call():
            runtime.bind(caller={"thread_id": self.driver.id, "provider": "claude", "turn_id": session.turn_id},
                         desk=session.desk.slot, depth=1, tool_names=hud_window.HUD_WINDOW_TOOLS)
            return tools.dispatch("hud_window_list", "{}")

        try:
            with patch.object(hud_window.SESSION, "request") as request, \
                 patch.object(self.claude, "steer", create=True, return_value=True):
                self.assertTrue(bridge.call(self.d, session, caller, "hud_window_list", {},
                                            request_alive=lambda: False)["isError"])
                self.assertIn("Refused:", contextvars.Context().run(child_call).text)
                steer = self.d.deliver(self.driver.id, UserMessage("untrusted steer", via="discord"))
                self.assertEqual(steer["mode"], "native")
                self.assert_window_inaccessible(self.driver)
                self.d.stop_desktop_driving("review fixture")
                self.assertTrue(session.cancelled.is_set())
                self.assert_window_inaccessible(self.driver)
                request.assert_not_called()
        finally:
            release.set()

    def test_window_gate_rejects_cancelled_turn_even_with_grant(self):
        self.grant()
        entered, release = self.hold(self.driver)
        self.d.deliver(self.driver.id, SelectedVoiceMessage("paired"))
        try:
            self.assertTrue(entered.wait(2))
            with patch.object(hud_window.SESSION, "request") as request:
                self.d.interrupt(self.driver.id)
                self.assertIsNotNone(self.d.desktop_driver.grant(self.driver.id))
                self.assert_window_inaccessible(self.driver)
                request.assert_not_called()
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
