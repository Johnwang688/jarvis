"""Laya Phase 2: negotiated input modes, local literal/clarification decisions,
grounded dispatch, conversation-only outage and observational feedback.

Real daemon and paired link on a temporary HOME with ephemeral ports. The Laya
router, actions, typer and player are fakes; no model, microphone or desktop.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.v2 import voice_link_va245_impl_check as fixture
from jarvis.v2 import laya_routing, voice_link
from jarvis.v2.route_feedback import RouteFeedback
from windows import voice_agent, voicetype, voicewire
from windows.voiceactions import Actions, LaunchStatus
from windows.voiceflow import (ASSISTANT_UPDATE, CONVERSATION_ONLY, ROUTE_UNAVAILABLE, SITE_EXPIRED,
                               Flow)

ROUTE = "jarvis.v2.laya_service.LayaRouter.route"
EITHER = "Show me either nasa.gov or noaa.gov"
TWO_SITES = {"kind": "clarify", "target": "", "scores": {"navigate": .55, "ask": .4, "clarify": .05}}


def decision(kind, target="", top=.99):
    return {"kind": kind, "target": target, "scores": {kind if kind != "unavailable" else "ask": top,
                                                       "clarify": round(1 - top, 2)}}


class Paired(fixture.Base):
    def setUp(self):
        super().setUp()
        self.start()
        self.wire = self.client()
        self.wire.ping()
        self.assertTrue(self.wire.laya_routing)
        self.actions, self.typer, self.ui, self.player = Mock(), Mock(), Mock(), Mock()
        self.player.cancelled = threading.Event()
        self.actions.navigate.return_value = True
        self.actions.route_names.return_value = ((), ())
        self.allowed = True
        self.link.speaker = lambda _: b"test WAV"
        self.flow = self.make_flow()
        self.feedback = RouteFeedback(self.d.stores.root)

    def make_flow(self, client=None):
        return Flow(client or self.wire, self.typer, self.actions, self.ui, self.player,
                    can_act=lambda: self.allowed)

    def clip(self, mode, text, flow=None):
        (flow or self.flow).on_clip(None, mode, transcript=text)

    def notices(self):
        return [r for r in self.d.stores.threads.read_log(self.chosen.id) if r["kind"] == "notice"]

    def turns(self):
        return self.feedback.recent()


class Parity(unittest.TestCase):
    def test_helper_literal_and_site_extraction_match_the_daemon_adapter(self):
        for text in ("Jarvis, type the words open nasa.gov", "type out: hello", 'dictate "send it"',
                     "please type literally go", "type one is common", "typewriter", "Type the",
                     "Hey Jarvis type the phrase new line"):
            self.assertEqual(voicetype.literal_payload(text), laya_routing._literal_payload(text), text)
        for text in ("Open boo uh youtube.com", "Yo, open youtube.Com for me", "go to YouTube dot com",
                     "youtube.comYo, open youtube.com", "nasa.govnasa.gov", "mail me at a@b.com",
                     EITHER, "open https://Example.org/ now", "visit 127.0.0.1", "open notes.txt"):
            sites = voicetype.spoken_websites(text)
            self.assertEqual(sites[0] if len(sites) == 1 else "", laya_routing._website(text), text)
        self.assertEqual(voicetype.spoken_websites(EITHER), ("https://nasa.gov", "https://noaa.gov"))


class Modes(Paired):
    @patch(ROUTE)
    def test_dictation_bypasses_router_receipts_prompts_and_choices(self, route):
        route.return_value = TWO_SITES
        self.clip("assistant", EITHER)
        self.actions.open_app.return_value = LaunchStatus.CHROME_PROFILE_REQUIRED
        self.actions.chrome_profile_options.return_value = ("Work",)
        route.return_value = decision("open_app", "Chrome")
        self.actions.route_names.return_value = (("Chrome",), ())
        self.clip("assistant", "Open Chrome")
        route.reset_mock()
        self.typer.reset_mock()
        before = len(self.turns())
        for words in ("Work.", "the second one", "open youtube.com", "Jarvis, open Chrome"):
            self.clip("dictation", words)
        self.assertEqual([c.args[0][0][1] for c in self.typer.run.call_args_list],
                         ["Work.", "the second one", "open youtube.com", "Jarvis, open Chrome"])
        for words, steps in (("send it", [("key", "enter")]), ("type the words send it", [("text", "send it")]),
                             ("I will send it", [("text", "I will send it")])):
            self.clip("dictation", words)
            self.assertEqual(self.typer.run.call_args.args[0], steps)
        route.assert_not_called()
        self.actions.navigate.assert_not_called()
        self.actions.choose_chrome_profile.assert_not_called()
        self.assertEqual((len(self.turns()), self.notices(), self.fast.messages), (before, [], []))

    @patch(ROUTE)
    def test_explicit_literal_on_both_jarvis_inputs_types_payload_only(self, route):
        for mode in ("assistant", "wake"):
            self.typer.reset_mock()
            self.clip(mode, "Jarvis, type the words open nasa.gov and send it")
            self.typer.run.assert_called_once_with([("text", "open nasa.gov and send it")])
        route.assert_not_called()
        self.assertEqual((self.turns(), self.notices()), ([], []))

    @patch(ROUTE, return_value=decision("navigate", "https://nasa.gov"))
    def test_router_sees_full_utterance_once_with_offered_names(self, route):
        self.actions.route_names.return_value = (("Google Chrome", "chrome"), ("Google Chrome",))
        text = "Uh, take me straight to nasa.gov"
        self.clip("wake", text)
        route.assert_called_once_with("wake", text, apps=("Google Chrome", "chrome"),
                                      windows=("Google Chrome",))
        self.actions.navigate.assert_called_once_with("https://nasa.gov")
        self.assertEqual([n["data"]["text"] for n in self.notices()],
                         ["Jarvis voice sent a website to the browser."])
        turn, = self.turns()
        self.assertEqual((turn["mode"], turn["kind"], turn["outcomes"]), ("wake", "navigate", ["confirmed"]))
        self.typer.run.assert_not_called()

    def test_unnegotiated_client_and_connection(self):
        legacy = self.client()
        legacy.request_laya = False
        legacy.ping(wake="ready")
        self.assertFalse(legacy.laya_routing)
        self.assertIn(voice_link.LAYA_UPDATE, self.link.agent_status()["detail"])
        # A Right Ctrl capture on a daemon that cannot tell it from dictation: no typing.
        with patch(ROUTE) as route:
            self.clip("assistant", "open youtube.com", flow=self.make_flow(legacy))
        route.assert_not_called()
        self.typer.run.assert_not_called()
        self.ui.set_state.assert_called_with("error", ASSISTANT_UPDATE)
        with self.assertRaises(voicewire.LinkError):
            legacy._request({"op": "route", "laya": True, "text": "open youtube.com", "source": "assistant",
                             "apps": [], "windows": [], "web_navigation": True})


class Choices(Paired):
    def offer(self, route, mode="assistant"):
        route.return_value = TWO_SITES
        self.clip(mode, EITHER)
        state = self.ui.set_state.call_args.args
        self.assertEqual(state[0], "profile_choice")
        self.assertIn("nasa.gov or noaa.gov", state[1])
        return self.wire._pending_site_choice

    @patch(ROUTE)
    def test_second_one_selects_once_and_confirms_once(self, route):
        self.offer(route)
        route.reset_mock()
        self.clip("assistant", "The second one, please.")
        self.clip("wake", "Jarvis, the second one")
        route.assert_not_called()
        self.actions.navigate.assert_called_once_with("https://noaa.gov")
        self.assertEqual(len(self.notices()), 1)
        turn, = self.turns()
        self.assertEqual((turn["kind"], turn["outcomes"]), ("clarify", ["clarified", "confirmed"]))
        self.typer.run.assert_not_called()

    @patch(ROUTE)
    def test_named_site_and_first(self, route):
        for reply, url in (("nasa", "https://nasa.gov"), ("noaa.gov", "https://noaa.gov"),
                           ("first", "https://nasa.gov")):
            with self.subTest(reply=reply):
                self.actions.navigate.reset_mock()
                self.offer(route)
                self.clip("assistant", reply)
                self.actions.navigate.assert_called_once_with(url)

    @patch(ROUTE)
    def test_expiry_cancel_stop_session_and_reconnect_remove_authority(self, route):
        pending = self.offer(route)
        self.wire._pending_site_choice = replace(pending, expires_at=time.monotonic() - .01)
        route.reset_mock()
        self.clip("assistant", "the second one")
        self.ui.set_state.assert_called_with("done", SITE_EXPIRED)
        route.assert_not_called()  # a late reply is recognized, never routed or acted on

        self.offer(route)
        self.clip("assistant", "Cancel")
        self.clip("assistant", "the second one")

        self.offer(route)
        Flow.retire_profile_choice(self.wire)  # what Stop, a cancelled capture or a link loss do
        self.clip("assistant", "the second one")

        self.offer(route)
        self.wire._pending_site_choice = replace(self.wire._pending_site_choice, session="old")
        self.clip("assistant", "the second one")

        self.offer(route)
        fresh = self.client()
        fresh.ping()
        route.return_value = decision("ask")
        self.clip("assistant", "the second one", flow=self.make_flow(fresh))
        self.actions.navigate.assert_not_called()
        self.assertEqual(self.notices(), [])

    @patch(ROUTE)
    def test_offer_expiring_while_reply_is_handled_never_navigates(self, route):
        from windows import voiceflow
        self.offer(route)
        now = [time.monotonic()]

        def clip_still_current():
            now[0] += 20  # the offer's deadline passes while this reply is checked
            return True
        self.flow.can_act = clip_still_current
        with patch.object(voiceflow, "time", SimpleNamespace(monotonic=lambda: now[0])):
            self.clip("assistant", "the second one")
        self.actions.navigate.assert_not_called()
        self.ui.set_state.assert_called_with("done", SITE_EXPIRED)

    @patch(ROUTE)
    def test_unrelated_speech_retires_the_offer(self, route):
        self.offer(route)
        route.return_value = decision("ask")
        self.clip("assistant", "what time is it")
        route.return_value = decision("clarify", top=.5)
        self.clip("assistant", "the second one")
        self.actions.navigate.assert_not_called()

    @patch(ROUTE)
    def test_superseded_offer_and_reply_never_navigate(self, route):
        self.offer(route)
        self.allowed = False
        self.clip("assistant", "the second one")
        self.allowed = True
        self.clip("assistant", "the second one")
        self.assertEqual(self.actions.navigate.call_count, 1)  # the active offer survives a stale clip


class Dispatch(Paired):
    @patch(ROUTE, return_value=decision("ask"))
    def test_comparison_answers_in_the_selected_chat(self, route):
        self.clip("assistant", "Talk me through the difference between nasa.gov and noaa.gov")
        self.assertEqual(len(self.fast.messages), 1)
        self.actions.navigate.assert_not_called()
        self.typer.run.assert_not_called()
        self.assertEqual(self.turns()[0]["outcomes"], ["answered"])

    def test_outage_answers_in_conversation_only(self):
        self.link.converser = Mock(return_value="Desktop actions are unavailable right now.")
        for failure in ({"side_effect": RuntimeError("worker died")},
                        {"return_value": {"kind": "unavailable", "target": "", "scores": {}}}):
            with self.subTest(failure=failure), patch(ROUTE, **failure):
                self.link.converser.reset_mock()
                self.clip("wake", "Open youtube.com")
                self.link.converser.assert_called_once()
                self.assertEqual(self.link.converser.call_args.args[0], "Open youtube.com")
                self.assertIn(("thinking", CONVERSATION_ONLY), [c.args for c in self.ui.set_state.call_args_list])
                self.assertIn("Laya", self.link.agent_status()["detail"])
        self.assertEqual(self.fast.messages, [])  # no chat turn, tools or task
        self.actions.navigate.assert_not_called()
        self.typer.run.assert_not_called()
        self.assertEqual([t["outcomes"] for t in self.turns()], [["unavailable", "answered"]] * 2)
        self.link.converser.side_effect = RuntimeError("no login")
        with patch(ROUTE, side_effect=TimeoutError()):
            self.clip("assistant", "what is the weather")
        self.ui.set_state.assert_called_with("error", voice_link.VOICE_DOWN)
        with patch(ROUTE, return_value=decision("ask")):
            self.clip("assistant", "what is the weather")
        self.assertNotIn("Laya", self.link.agent_status()["detail"])

    def test_daemon_downgrades_ungrounded_router_targets(self):
        self.actions.route_names.return_value = (("Paint",), ())
        for text, made in (("open youtube.com", decision("navigate", "https://evil.example.org")),
                           ("open Calculator", decision("open_app", "Calculator")),
                           ("open paint", decision("switch_app", "Paint")),
                           ("search for cats", decision("search", "dogs"))):
            with self.subTest(text=text), patch(ROUTE, return_value=made):
                got = self.wire.laya_route(text, "assistant", apps=("Paint",))
                self.assertEqual((got["kind"], got["target"]), ("clarify", ""))
                self.clip("assistant", text)
        for name in ("navigate", "open_app", "switch_app", "search"):
            getattr(self.actions, name).assert_not_called()
        self.assertEqual([t["outcomes"] for t in self.turns()], [["clarified"]] * 8)

    @patch(ROUTE, return_value=decision("navigate", "https://youtube.com"))
    def test_helper_rejects_a_faulty_daemon_decision(self, route):
        forged = ({"kind": "navigate", "text": "", "target": "https://evil.example.org"}, "navigate")
        with patch.object(voice_link.VoiceLink, "_laya_intent", return_value=forged):
            self.clip("assistant", "open youtube.com")
        self.assertIsInstance(self.flow.link_error, voicewire.LinkError)
        self.ui.set_state.assert_called_with("error", ROUTE_UNAVAILABLE)
        self.actions.navigate.assert_not_called()

    def test_open_switch_search_and_read_use_exact_offered_names(self):
        self.actions.route_names.return_value = (("Google Chrome", "Calendar"), ("Calendar",))
        self.actions.open_app.return_value = LaunchStatus.LAUNCHED
        self.actions.switch_app.return_value = self.actions.search.return_value = True
        self.actions.read_selection.return_value = "private selection sentinel"
        for text, made in (("open Google Chrome for me", decision("open_app", "Google Chrome")),
                           ("bring Calendar back", decision("switch_app", "Calendar")),
                           ("search the web for viola tuning", decision("search", "viola tuning")),
                           ("read this to me", decision("read_selection"))):
            with patch(ROUTE, return_value=made):
                self.clip("assistant", text)
        self.actions.open_app.assert_called_once_with("Google Chrome", exact=True)
        self.actions.switch_app.assert_called_once_with("Calendar")
        self.actions.search.assert_called_once_with("viola tuning")
        self.player.play.assert_called_once_with(b"test WAV")
        self.assertEqual(len(self.notices()), 4)
        self.assertTrue(all(t["outcomes"] == ["confirmed"] for t in self.turns()))
        self.assertNotIn("private selection sentinel", repr(self.turns()))

    def test_failures_and_supersession_never_confirm(self):
        self.actions.navigate.return_value = False
        with patch(ROUTE, return_value=decision("navigate", "https://youtube.com")):
            self.clip("assistant", "open youtube.com")
        self.actions.navigate.return_value = True

        def supersede(*args, **kwargs):
            self.allowed = False  # a newer capture arrived during inference
            return decision("navigate", "https://youtube.com")
        with patch(ROUTE, side_effect=supersede):
            self.clip("assistant", "open youtube.com")
        self.allowed = True
        self.assertEqual(self.actions.navigate.call_count, 1)
        self.assertEqual(self.notices(), [])
        self.assertEqual([t["outcomes"] for t in self.turns()], [["cancelled"], ["failed"]])

    def test_feedback_failure_cannot_change_dispatch(self):
        with patch.object(RouteFeedback, "record", side_effect=OSError("disk full")), \
                patch.object(RouteFeedback, "outcome", side_effect=OSError("disk full")), \
                patch(ROUTE, return_value=decision("navigate", "https://youtube.com")):
            self.clip("assistant", "Yo, open youtube.Com for me")
        self.actions.navigate.assert_called_once_with("https://youtube.com")
        self.assertEqual(len(self.notices()), 1)

    def test_transport_failure_never_types_or_acts(self):
        with patch.object(self.wire, "laya_route", side_effect=voicewire.LinkError("gone")):
            self.clip("assistant", "open youtube.com")
        self.ui.set_state.assert_called_with("error", ROUTE_UNAVAILABLE)
        self.typer.run.assert_not_called()
        self.actions.navigate.assert_not_called()


class Helper(unittest.TestCase):
    def run_poll(self, frames):
        recorder = object.__new__(voice_agent.Agent)
        recorder.generation, recorder.capture_mode = 0, "hotkey"
        recorder.next_generation, recorder.start_capture, recorder.show = Mock(), Mock(), Mock()
        recorder.player, recorder.user32, recorder.done = Mock(), Mock(), Mock()
        recorder.desktop_key = Mock(hud=False)
        step, events = 0, []

        def tick(_):
            nonlocal step
            step += 1

        def hotkey():
            return frames[step][2]

        def finish(cancel=False):
            events.append(("cancel" if cancel else recorder.capture_mode, step))
            recorder.capture_mode = "hotkey"
        recorder.finish_capture = finish
        recorder.done.is_set.side_effect = lambda: step >= len(frames)
        recorder.done.wait.side_effect = tick
        recorder.desktop_key.poll.side_effect = hotkey
        recorder.user32.GetAsyncKeyState.side_effect = lambda k: 0x8000 if k in frames[step][1] else 0
        with patch.object(voice_agent.time, "monotonic_ns", lambda: frames[step][0] * 1_000_000):
            recorder.poll()
        return events[:-1]

    def test_capture_identity_right_ctrl_ctrl_d_and_chord(self):
        right = {voice_agent.VK_RCONTROL, voice_agent.VK_CONTROL}
        left = {voice_agent.VK_LCONTROL, voice_agent.VK_CONTROL}
        self.assertEqual(self.run_poll([(0, right, False), (900, set(), False)]), [("assistant", 1)])
        self.assertEqual(self.run_poll([(0, left, False), (20, left | {voice_agent.VK_D}, True),
                                        (1000, left | {voice_agent.VK_D}, False), (1250, set(), False)]),
                         [("dictation", 3)])
        # Right Ctrl joined by D is one dictation clip.
        self.assertEqual(self.run_poll([(0, right, False), (20, right | {voice_agent.VK_D}, True),
                                        (1000, right | {voice_agent.VK_D}, False), (1600, right, False),
                                        (2100, set(), False)]), [("dictation", 3)])

    def bare(self):
        a = voice_agent.Agent.__new__(voice_agent.Agent)
        a.done, a.online = threading.Event(), threading.Event()
        a.recording, a.flow_running = threading.Event(), threading.Event()
        a.overlay, a.player = Mock(), voice_agent.Player()
        a.generation, a.generation_lock, a.capture_lock = 1, threading.Lock(), threading.Lock()
        a.capture, a.chunks, a.frames, a.audio_bad = None, [], 0, False
        a.clips = queue.Queue(maxsize=4)
        a.online.set()
        return a

    def test_mode_survives_queued_and_streamed_captures(self):
        a = self.bare()
        a.capture_online, a.chunks, a.capture_mode = True, [b"\1\0" * 100], "dictation"
        a.finish_capture()
        self.assertEqual(a.clips.get_nowait()[2], "dictation")
        a.capture_online, a.chunks = True, [b"\1\0" * 100]
        a.finish_capture()  # a direct caller keeps the legacy source
        self.assertEqual(a.clips.get_nowait()[2], "hotkey")
        upload = voicewire.Capture()
        a.upload, a.capture_online, a.frames, a.capture_mode = upload, True, 100, "assistant"
        a.finish_capture()
        self.assertEqual(upload.mode, "assistant")

    def test_route_names_offer_catalog_names_never_window_titles(self):
        desktop = SimpleNamespace(windows=lambda: [
            (1, "Secret plan - Word", r"C:\Office\WINWORD.EXE"),
            (2, "Inbox (3) - private@example.com - Google Chrome", r"C:\Chrome\chrome.exe")])
        actions = Actions(desktop=desktop, runner=Mock(returncode=1))
        actions.refreshed = time.monotonic()
        actions.apps = [{"name": "Google Chrome", "id": "c"}, {"name": "Word", "id": "w"},
                        {"name": "Paint", "id": "p"}, {"name": "Bad\u200bName", "id": "b"}]
        apps, open_names = actions.route_names()
        self.assertEqual(apps, ("Google Chrome", "Word", "Paint", "chrome"))
        self.assertEqual(open_names, ("Google Chrome", "Word", "chrome"))
        self.assertNotIn("Secret", repr((apps, open_names)))
        self.assertNotIn("private@", repr((apps, open_names)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
