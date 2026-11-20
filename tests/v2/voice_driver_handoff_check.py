"""CU-3d voice hand-off with fake providers and ephemeral paired listeners."""
from concurrent.futures import ThreadPoolExecutor
import os
import socket
import threading
import time
import unittest
from unittest.mock import ANY, Mock, patch

import mcp_session_check as fixture
from jarvis import config, runtime
from jarvis.v2 import router, voice_link
from jarvis.v2.daemon import Daemon
from jarvis.v2.desk import SelectedVoiceMessage
from jarvis.v2.desktop_driver import DRIVER_BRIEF
from jarvis.v2.provider import UserMessage
from jarvis.v2.tools import hud_window
from jarvis.v2.voice_chat import DRIVER_STILL_WORKING, NoVoiceTarget
from windows import voiceflow, voicetype, voicewire

KEY = bytes(range(32))
WINDOW = "0000000000000001"
CASES = (
    ("open_app", "Open Notepad", "Notepad"),
    ("switch_app", "Switch to Notepad", "Notepad"),
    ("navigate", "Go to example.com", "https://example.com"),
    ("search", "Search for garden plants", "garden plants"),
    ("read_selection", "Read my selection", ""),
    ("clarify", "List my windows", ""),
    ("ask", "What's the time", ""),
    ("open_app", "Go to Notepad and type hello", "Notepad"),
)


class Handoff(fixture.DaemonHarness):
    def make_daemon(self, stores=None, providers=None):
        self.stack.enter_context(patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "1"}))
        self.stack.enter_context(patch.object(config, "VOICE_LINK_PORT", 0))
        key = self.root / "voice.key"
        key.write_text(KEY.hex() + "\n")
        key.chmod(0o600)
        self.stack.enter_context(patch.object(config, "voice_link_key", return_value=key))
        daemon = Daemon(self.stores, {fixture.CLAUDE: self.claude, fixture.CODEX: self.codex,
                                     fixture.FAST: self.fast},
                        None, 0, face_port=0, workshop_port=0, remote_host=None)
        daemon.start()
        self.addCleanup(daemon.stop)
        self.assertFalse({daemon.port, daemon.face_port, daemon.workshop_port,
                          daemon.voice_link.port} & {8402, 8403, 8404, 8405, 8409})
        return daemon

    def setUp(self):
        super().setUp()
        self.driver, self.selected = self.chat(), self.chat()
        self.d.voice_chat.select(self.selected.id)
        self.link = self.d.voice_link
        self.link._feedback = Mock()
        self.link._feedback.record.return_value = "synthetic-row"
        self.client = voicewire.Client(*self.link.address, KEY)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.ping()
        self.assertTrue(self.client.laya_routing)

    def grant(self, on=True):
        # Set the fixture's grant in-process. No owner HTTP routes are needed.
        self.d.set_desktop_driving(self.driver.id, on)

    def test_laya_and_legacy_routes_skip_models_only_for_addressed_speech(self):
        for kind, words, target in CASES:
            for laya_route, mode in ((True, "assistant"), (True, "wake"),
                                     (False, "wake"), (False, "hotkey")):
                with self.subTest(kind=kind, laya=laya_route, mode=mode):
                    text = "Jarvis, " + words if mode == "hotkey" else words
                    decision = {"kind": kind, "target": target}
                    with patch.object(self.link, "_laya_decide", return_value=decision) as laya, \
                         patch.object(self.link, "route_model", return_value=decision) as legacy:
                        def route():
                            if laya_route:
                                return self.client.laya_route(text, mode, apps=["Notepad"],
                                                              windows=["Notepad"], with_receipt=True)
                            return self.client.route(text, mode, with_receipt=True)

                        self.grant(False)
                        baseline = route()
                        # The legacy wire never supported clarify.
                        self.assertEqual(baseline["kind"], "ask" if not laya_route
                                         and kind == "clarify" else kind)
                        self.client.ping()  # Wait for the after-reply feedback callback.
                        self.link._feedback.reset_mock()
                        self.grant()
                        routed = route()
                        self.assertEqual(routed["kind"], "ask")
                        self.assertEqual(routed["text"], text)
                        self.assertNotIn("receipt", routed)
                        self.client.ping()
                        self.link._feedback.record.assert_not_called()
                        self.assertEqual(laya.call_count, int(laya_route))
                        self.assertEqual(legacy.call_count, int(not laya_route))

        self.grant(False)
        with patch.object(self.link, "route_model", return_value={"kind": "open_app", "target": "Notepad"}) as model:
            self.assertEqual(self.client.route("Open Notepad", "wake")["kind"], "open_app")
            self.assertEqual(self.client.route("Open Notepad", "hotkey")["kind"], "open_app")
            self.assertEqual(self.client.route("A sentence for my notes.", "hotkey")["kind"], "dictate")
            self.grant()
            self.assertEqual(self.client.route("A sentence for my notes.", "hotkey"), {
                "kind": "dictate", "text": "A sentence for my notes.", "target": "literal"})
            self.assertEqual(self.client.route("Open Notepad", "hotkey"), {
                "kind": "dictate", "text": "Open Notepad", "target": "literal"})
            self.assertEqual(model.call_count, 5)

    def test_driver_overrides_selected_and_brief_follows_each_spoken_turn(self):
        seen = []
        for chat in (self.driver, self.selected):
            self.fake(chat.provider).script[chat.id] = lambda h, m: seen.append((h.thread_id, m))
        self.grant()
        for text in ("List my windows", "Read the window title"):
            intent = self.client.laya_route(text, "assistant")
            self.assertEqual(self.client.ask(text, turn=intent["turn"]), "done")
            self.wait_idle(self.driver.id)
        self.assertEqual([(ident, msg.text) for ident, msg in seen],
                         [(self.driver.id, DRIVER_BRIEF + "\n\n" + text)
                          for text in ("List my windows", "Read the window title")])
        self.assertTrue(all(isinstance(msg, SelectedVoiceMessage) for _, msg in seen))
        intent = self.client.laya_route("Use the normal chat", "wake")
        self.grant(False)
        self.assertEqual(self.client.ask(intent["text"], turn=intent["turn"]), "done")
        self.assertEqual((seen[-1][0], seen[-1][1].text), (self.selected.id, "Use the normal chat"))

    def test_driver_still_requires_an_eligible_chat(self):
        self.grant()
        original = self.d.voice_chat._eligible_thread
        with patch.object(self.d.voice_chat, "_eligible_thread",
                          side_effect=lambda ident: None if ident == self.driver.id else original(ident)):
            with self.assertRaises(NoVoiceTarget):
                self.d.voice_chat.ask("List my windows", lambda _: None, timeout=1)

    def test_laya_outage_before_grant_does_not_divert_driver_ask(self):
        converser = Mock(return_value="conversation only")
        with patch.object(self.link, "_laya_decide", return_value=None), \
             patch.object(self.link, "converser", converser):
            self.assertEqual(self.client.laya_route("Open Notepad", "wake")["target"], "conversation")
            self.grant()
            intent = self.client.route("Open Notepad", "wake")
            self.assertEqual(self.client.ask(intent["text"]), "done")
        converser.assert_not_called()

    def test_grant_enabled_during_laya_inference_discards_fast_decision(self):
        def route(*_):
            self.grant()
            return {"kind": "open_app", "target": "Notepad"}

        with patch.object(self.link, "_laya_decide", side_effect=route):
            intent = self.client.laya_route("Open Notepad", "wake", apps=["Notepad"], with_receipt=True)
        self.assertEqual(intent["kind"], "ask")
        self.assertNotIn("receipt", intent)

    def hold(self, chat):
        entered, release = threading.Event(), threading.Event()

        def send(_handle, _message):
            entered.set()
            release.wait(5)

        self.fake(chat.provider).script[chat.id] = send
        self.addCleanup(release.set)
        return entered, release

    def session_tool(self, chat, name=None, arguments=None):
        token = fixture.token_of(self.run_brief(chat))
        if name is None:
            status, response = fixture.http(self.d.port, "GET", "/mcp/tools", token=token)
            self.assertEqual(status, 200, response)
            return {row["name"] for row in response["tools"]} & hud_window.HUD_WINDOW_TOOLS
        status, response = fixture.http(self.d.port, "POST", "/mcp/call", token=token,
                                        body={"name": name, "arguments": arguments or {}})
        self.assertEqual(status, 200, response)
        return response

    def test_only_current_spoken_driver_can_use_window_tools(self):
        self.grant()
        ref = "0123456789abcdef-e1"
        safe = {"name": "Open", "role": "button"}
        fake_windows = {"windows": [{"window": WINDOW, "app": "notepad.exe",
                                     "title": "Notes", "access": "drivable"}]}
        snapshot = {"snapshot": "Open button", "refs": {ref: safe}}
        click = {"window": WINDOW, "ref": ref, "purpose": "Open the selected note.", "risky": "no"}
        with patch.object(hud_window.SESSION, "request",
                          side_effect=[fake_windows, {"image_b64": "YWJj"}, snapshot, {}]) as request, \
             patch.object(router, "cli_images", return_value=True), \
             patch.object(self.claude, "steer", create=True, return_value=True):
            with ThreadPoolExecutor() as pool:
                for chat in (self.driver, self.selected):
                    entered, release = self.hold(chat)
                    if chat == self.driver:
                        ask = pool.submit(self.client.ask, "Can you see Notepad?")
                    else:
                        self.d.deliver(chat.id, SelectedVoiceMessage("Can you see Notepad?"))
                    try:
                        self.assertTrue(entered.wait(2))
                        session = self.d._sessions[chat.id]
                        self.assertFalse(session.desk.slot["present"])
                        self.assertTrue(session.desk.slot["hud_desktop"])
                        with runtime.turn_scope(desk=session.desk.slot):
                            self.assertFalse(runtime.at_desk())
                            self.assertFalse(runtime.desk_images())
                        if chat == self.selected:
                            self.assertFalse(self.session_tool(chat))
                            self.assertEqual(self.session_tool(chat, "hud_window_list")["text"], hud_window.OFF)
                            continue
                        self.assertEqual(self.session_tool(chat), hud_window.HUD_WINDOW_TOOLS)
                        self.assertIn("notepad.exe", self.session_tool(chat, "hud_window_list")["text"])
                        self.assertEqual(self.session_tool(chat, "hud_window_screenshot",
                                                           {"window": WINDOW})["image"]["data"], "YWJj")
                        self.assertEqual(self.session_tool(chat, "hud_window_snapshot",
                                                           {"window": WINDOW})["text"], "Open button")
                        self.assertEqual(self.session_tool(chat, "hud_window_click", click)["text"],
                                         hud_window.COMPLETED)
                        result = self.d.deliver(chat.id, UserMessage("Other surface", via="discord"))
                        self.assertEqual(result["mode"], "native")
                        self.assertFalse(session.desk.slot["hud_desktop"])
                        self.assertFalse(self.session_tool(chat))
                        self.assertEqual(self.session_tool(chat, "hud_window_list")["text"], hud_window.UNAVAILABLE)
                        self.assertEqual(self.session_tool(chat, "hud_window_click", click)["text"],
                                         hud_window.UNAVAILABLE)
                    finally:
                        release.set()
                    self.assertEqual(ask.result(2), "done")
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ["windows", "window_screenshot", "window_snapshot", "window_click"])

    def test_expiring_or_disconnecting_voice_leaves_driver_turn_running(self):
        self.grant()
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect):
                entered, release = self.hold(self.driver)
                audio = []
                with patch.object(self.claude, "interrupt") as interrupt, \
                     patch.object(self.link, "speaker", return_value=b"fake WAV") as speaker, \
                     patch.object(voice_link, "ASK_TIMEOUT_S", 2 if disconnect else 0.2), \
                     ThreadPoolExecutor() as pool:
                    ask = pool.submit(self.client.ask_spoken, "Work on Notepad", lambda _: None, audio.append)
                    try:
                        self.assertTrue(entered.wait(2))
                        if disconnect:
                            self.client._socket.shutdown(socket.SHUT_RDWR)
                            with self.assertRaises(voicewire.LinkError):
                                ask.result(2)
                        else:
                            self.assertEqual(ask.result(2), DRIVER_STILL_WORKING)
                            self.assertIn(b"fake WAV", audio)
                            speaker.assert_called_once_with(DRIVER_STILL_WORKING)
                        session = self.d._sessions[self.driver.id]
                        self.assertFalse(session.cancelled.is_set())
                        self.assertIsNotNone(session.worker)
                        interrupt.assert_not_called()
                    finally:
                        release.set()
                self.wait_idle(self.driver.id)


class Helper(unittest.TestCase):
    def test_pure_addressing_and_dictation(self):
        for source, text, addressed in (
            ("assistant", "Go to Notepad and type hello", True),
            ("wake", "List my windows", True),
            ("hotkey", "Hey Jarvis, open Notepad", True),
            ("hotkey", "Open Notepad", False),
            ("hotkey", "Jarvison", False),
            ("dictation", "Jarvis, open Notepad", False),
        ):
            with self.subTest(source=source, text=text):
                self.assertEqual(voicetype.driver_handoff(True, source, text), addressed)
                self.assertFalse(voicetype.driver_handoff(False, source, text))

    def test_helper_retires_shortcuts_and_routes_full_addressed_speech(self):
        client = Mock(streaming=False, laya_routing=True, action_receipts=False, session="paired")
        typer, actions = Mock(), Mock()
        flow = voiceflow.Flow(client, typer, actions, Mock(), Mock(), driving=lambda: True)
        client.ask.return_value = "done"
        for source, text in (("assistant", "Jarvis, type the words hello"),
                             ("wake", "Go to example.com")):
            client.laya_route.return_value = {"kind": "ask", "target": "", "text": text, "turn": "turn"}
            flow.on_clip(None, source, transcript=text)
            client.laya_route.assert_called_with(text, source, apps=(), windows=(), with_receipt=False)
            client.ask.assert_called_with(text, on_delta=ANY, turn="turn")
        client._pending_site_choice = voiceflow._SiteChoice(
            "paired", ("https://example.com", "https://example.org"), None, "old", time.monotonic() + 5)
        client.laya_route.return_value = {"kind": "ask", "target": "", "text": "first", "turn": "turn"}
        flow.on_clip(None, "wake", transcript="first")
        client.laya_route.assert_called_with("first", "wake", apps=(), windows=(), with_receipt=False)
        client.ask.assert_called_with("first", on_delta=ANY, turn="turn")
        client.route.return_value = {"kind": "navigate", "text": "Go to example.com",
                                     "target": "https://example.com"}
        flow.on_clip(None, "hotkey", transcript="Jarvis, go to example.com")
        client.ask.assert_called_with("Jarvis, go to example.com", on_delta=ANY)
        client.route.return_value = {"kind": "open_app", "text": "Open Notepad", "target": "Notepad"}
        flow.on_clip(None, "hotkey", transcript="Open Notepad")
        typer.run.assert_called_once_with([("text", "Open Notepad")])
        client.reset_mock()
        flow.on_clip(None, "dictation", transcript="Jarvis, open Notepad")
        client.route.assert_not_called()
        client.laya_route.assert_not_called()
        client.ask.assert_not_called()
        self.assertFalse(actions.method_calls)


if __name__ == "__main__":
    unittest.main()
