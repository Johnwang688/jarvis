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
from windows import voice_agent, voiceactions, voiceflow, voicetype, voicewire
from windows.voiceactions import Actions, LaunchStatus
from windows.voiceflow import (ASSISTANT_UPDATE, CONVERSATION_ONLY, ROUTE_UNAVAILABLE, SITE_CHOSEN,
                               SITE_EXPIRED, SITE_FAILED, SITE_UNCLEAR, Flow)

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


    def test_only_whole_affirmative_forms_select(self):
        choices = ("https://nasa.gov", "https://noaa.gov")
        for reply, want in (("the second one, please", 1), ("Jarvis, first", 0), ("noaa.gov", 1),
                            ("noaa dot gov", 1), ("open noaa.gov please", 1), ("the noaa site", 1),
                            ("go with the first one", 0), ("https://nasa.gov", 0), ("www.nasa.gov", 0),
                            ("nasa", 0), ("cancel", "cancel"), ("neither", "cancel"),
                            ("don't open noaa.gov", "unclear"), ("Don’t open noaa.gov", "unclear"),
                            ("not the second one", "unclear"), ("no, the second one", "unclear"),
                            ("not nasa", "unclear"), ("noaa.gov noaa.gov", "unclear"),
                            ("the second one the second one", "unclear"), ("first or second", "unclear"),
                            ("nasa or noaa", "unclear"), ("open noaa.gov instead of nasa.gov", "unclear"),
                            ("I would rather you didn't open the noaa website at all today", "unclear"),
                            ("what time is it", None),
                            ("Talk me through the difference between nasa.gov and noaa.gov", None)):
            with self.subTest(reply=reply):
                self.assertEqual(voiceflow._site_reply(reply, choices), want)

    @patch(ROUTE)
    def test_negated_or_repeated_replies_never_navigate_or_route(self, route):
        for reply in ("don't open noaa.gov", "Jarvis, not the second one", "no, the second one",
                      "noaa.gov noaa.gov", "first or second"):
            with self.subTest(reply=reply):
                self.offer(route)
                route.reset_mock()
                # Were it routed, the daemon would ground this site from the words.
                route.return_value = decision("navigate", "https://noaa.gov")
                self.clip("assistant", reply)
                self.ui.set_state.assert_called_with("done", SITE_UNCLEAR)
                # The prompt is settled: a plain choice now changes nothing either.
                self.clip("assistant", "the second one")
                self.ui.set_state.assert_called_with("done", SITE_UNCLEAR)
                route.assert_not_called()
        self.actions.navigate.assert_not_called()
        self.assertEqual(self.notices(), [])
        self.assertTrue(all(t["outcomes"] == ["clarified", "cancelled"] for t in self.turns()))

    @patch(ROUTE)
    def test_negated_repeat_after_a_choice_stays_local(self, route):
        self.offer(route)
        self.clip("assistant", "the second one")
        self.actions.navigate.assert_called_once_with("https://noaa.gov")
        route.reset_mock()
        route.return_value = decision("navigate", "https://noaa.gov")
        for reply in ("don't open noaa.gov", "noaa.gov noaa.gov", "Jarvis, the second one"):
            self.clip("assistant", reply)
            self.ui.set_state.assert_called_with("done", SITE_CHOSEN)
        route.assert_not_called()
        self.actions.navigate.assert_called_once()
        self.assertEqual(len(self.notices()), 1)

    @patch(ROUTE)
    def test_failed_or_expired_handoff_repeat_reports_what_happened(self, route):
        self.offer(route)
        self.actions.navigate.return_value = False
        self.clip("assistant", "the second one")
        self.ui.set_state.assert_called_with("error", SITE_FAILED)
        self.actions.navigate.return_value = True
        route.reset_mock()
        self.clip("assistant", "the second one")
        self.ui.set_state.assert_called_with("error", SITE_FAILED)  # never "already chosen"
        self.assertNotIn(("done", SITE_CHOSEN), [c.args for c in self.ui.set_state.call_args_list])
        route.assert_not_called()
        self.actions.navigate.assert_called_once()
        self.assertEqual(self.notices(), [])
        turn, = self.turns()
        self.assertEqual(turn["outcomes"], ["clarified", "failed"])

        self.offer(route)  # an offer that lapses while its reply is checked
        now = [time.monotonic()]

        def lapse():
            now[0] += 20
            return True
        self.flow.can_act = lapse
        with patch.object(voiceflow, "time", SimpleNamespace(monotonic=lambda: now[0])):
            self.clip("assistant", "the first one")
            self.ui.set_state.assert_called_with("done", SITE_EXPIRED)
            self.flow.can_act = lambda: self.allowed
            self.clip("assistant", "the first one")
        self.ui.set_state.assert_called_with("done", SITE_EXPIRED)
        self.actions.navigate.assert_called_once()

    def network_agent(self, clips):
        agent = voice_agent.Agent.__new__(voice_agent.Agent)
        agent.done, agent.online = threading.Event(), threading.Event()
        agent.stop_requested, agent.profile_cancelled = threading.Event(), threading.Event()
        agent.recording, agent.flow_running = threading.Event(), threading.Event()
        agent.generation_lock, agent.generation, agent.wake = threading.Lock(), 1, None
        agent.overlay, agent.player = self.ui, self.player
        agent.host, agent.port, agent.key_path = "unused", 0, "unused"
        agent.user32 = SimpleNamespace(GetAsyncKeyState=lambda key: 0)
        agent.discard_clips = Mock()
        agent.clips = Mock(spec=queue.Queue)
        agent.clips.get.side_effect = clips(agent)
        return agent

    @patch(ROUTE)
    def test_reconnect_carries_website_recognition_without_authority(self, route):
        routed, made = [], []

        def words(wav, **_):
            if wav == b"drop":
                raise voicewire.LinkError("The voice link disconnected.")
            return {"text": wav.decode()}

        def connect(*_, **__):
            # Each reconnect is a new paired connection to the real daemon.
            client = self.client()
            self.enterContext(patch.object(client, "connect"))
            self.enterContext(patch.object(client, "dictate", side_effect=words))
            made.append(client)
            return client

        def clips(agent):
            route.return_value = TWO_SITES
            yield 1, EITHER.encode(), "assistant"
            self.assertIsInstance(made[0]._pending_site_choice, voiceflow._SiteChoice)
            route.reset_mock()
            route.side_effect = lambda *a, **k: routed.append(a) or decision("navigate", "https://noaa.gov")
            yield 1, b"drop", "assistant"  # the link fails with the offer active
            # Reconnected and negotiated: the new connection could route and act.
            self.assertEqual((len(made), made[-1].laya_routing), (2, True))
            for words in (b"the second one", b"don't open noaa.gov", b"noaa.gov"):
                yield 1, words, "assistant"
            agent.done.set()
            raise queue.Empty

        agent = self.network_agent(clips)
        with patch.object(voice_agent, "Client", side_effect=connect), \
                patch.object(voice_agent, "load_key", return_value=b"key"), \
                patch.object(voice_agent, "Typer", return_value=self.typer), \
                patch.object(voice_agent, "Actions", return_value=self.actions):
            agent.network()
        first, second = made
        self.assertIsNot(first, second)
        self.assertIsInstance(second._pending_site_choice, voiceflow._SiteReplyContext)
        self.assertEqual(second._pending_site_choice.choices, ("https://nasa.gov", "https://noaa.gov"))
        self.assertEqual(routed, [])
        route.assert_not_called()
        self.actions.navigate.assert_not_called()
        self.typer.run.assert_not_called()
        self.ui.set_state.assert_called_with("done", SITE_EXPIRED)
        self.assertEqual(self.notices(), [])

    def test_carry_drops_anything_with_authority(self):
        target = SimpleNamespace(_pending_site_choice=None, _pending_chrome_profile=None)
        active = voiceflow._SiteChoice("s", ("https://nasa.gov", "https://noaa.gov"), "receipt", "turn",
                                       time.monotonic() + 9)
        profile = voiceflow._ProfileChoice("s", ("Work",), "receipt", time.monotonic() + 9)
        Flow.carry_choices(target, (profile, active))
        self.assertEqual((target._pending_site_choice, target._pending_chrome_profile), (None, None))
        source = SimpleNamespace(_pending_site_choice=active, _pending_chrome_profile=profile, session="s")
        carried = Flow.retire_choices(source)
        Flow.carry_choices(target, carried)
        self.assertIsInstance(target._pending_site_choice, voiceflow._SiteReplyContext)
        self.assertIsInstance(target._pending_chrome_profile, voiceflow._ProfileReplyContext)


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
        self.actions.switch_app.assert_called_once_with("Calendar", exact=True)
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
        # Ctrl+D pressed to end a Right Ctrl toggle recording: the clip dictates.
        self.assertEqual(self.run_poll([(0, right, False), (100, set(), False),
                                        (600, left | {voice_agent.VK_D}, True), (800, set(), False)]),
                         [("dictation", 2)])
        # A Right Ctrl toggle ended by Right Ctrl stays an assistant command.
        self.assertEqual(self.run_poll([(0, right, False), (100, set(), False), (600, right, False),
                                        (800, set(), False)]), [("assistant", 2)])

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

    def poll_real_capture(self, frames, *, streamed):
        """The production poller and finish_capture, with only Win32 and audio faked."""
        a = self.bare()
        a.capture_mode, a.client, a.upload, a.capture_online = "hotkey", None, None, False
        a.desktop_key = Mock(hud=False)
        step = 0
        uploads = []

        def start_capture():
            a.recording.set()
            with a.capture_lock:
                a.capture_online, a.chunks, a.frames = True, [b"\1\0" * 100], 100
                if streamed:
                    a.upload = voicewire.Capture()
                    uploads.append(a.upload)
                    a.clips.put_nowait((a.generation, a.upload, "hotkey"))
        a.start_capture = start_capture

        def tick(_):
            nonlocal step
            step += 1
            if step >= len(frames):
                a.done.set()
        a.done.wait = tick
        a.desktop_key.poll.side_effect = lambda: frames[step][2]
        a.user32 = SimpleNamespace(GetAsyncKeyState=lambda k: 0x8000 if k in frames[step][1] else 0)
        with patch.object(voice_agent.time, "monotonic_ns", lambda: frames[step][0] * 1_000_000):
            a.poll()
        items = []
        while not a.clips.empty():
            items.append(a.clips.get_nowait())
        return items, uploads

    def test_hotkey_poller_dictates_across_chord_and_toggle_for_queued_and_streamed_captures(self):
        right = {voice_agent.VK_RCONTROL, voice_agent.VK_CONTROL}
        left = {voice_agent.VK_LCONTROL, voice_agent.VK_CONTROL}
        d = voice_agent.VK_D
        gestures = {
            "toggle ended by Ctrl+D": ([(0, right, False), (100, set(), False), (600, left | {d}, True),
                                       (800, set(), False)], "dictation"),
            "Right Ctrl joined by D": ([(0, right, False), (20, right | {d}, True), (1000, right | {d}, False),
                                       (1600, right, False), (2100, set(), False)], "dictation"),
            "Ctrl+D hold": ([(0, left, False), (20, left | {d}, True), (1000, left | {d}, False),
                             (1250, set(), False)], "dictation"),
            "Right Ctrl hold": ([(0, right, False), (900, set(), False), (1000, set(), False)], "assistant"),
        }
        for name, (frames, mode) in gestures.items():
            for streamed in (False, True):
                with self.subTest(gesture=name, streamed=streamed):
                    items, uploads = self.poll_real_capture(frames, streamed=streamed)
                    if streamed:
                        upload, = uploads
                        self.assertEqual([item[1] for item in items], [upload])
                        self.assertFalse(upload.cancelled)
                        self.assertEqual(upload.mode, mode)
                    else:
                        clip, = items
                        self.assertEqual(clip[2], mode)

    def test_laya_switch_grounds_the_app_never_a_title_mention(self):
        notepad = (1, "Chrome setup instructions - Notepad", r"C:\Windows\notepad.exe")
        console = (2, "Google Chrome", r"C:\Windows\System32\cmd.exe")
        folder = (3, "Chrome", r"C:\Windows\explorer.exe")
        chrome = (4, "New Tab - Google Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        word = (5, "Chrome notes - Word", r"C:\Office\WINWORD.EXE")
        calc = (6, "Calculator", r"C:\Windows\ApplicationFrameHost.exe")
        shown = [notepad, console, folder]
        desktop = SimpleNamespace(windows=lambda: list(shown), focus=Mock(return_value=True))
        actions = Actions(desktop=desktop, runner=Mock(returncode=1))
        actions.refreshed = time.monotonic()
        actions.apps = [{"name": "Google Chrome", "id": "c"}, {"name": "Word", "id": "w"},
                        {"name": "Calculator", "id": "k"}]
        for name in ("Chrome", "Google Chrome", "chrome"):
            self.assertFalse(actions.switch_app(name, exact=True), name)
        self.assertEqual(actions.route_names()[1], ())
        desktop.focus.assert_not_called()
        self.assertTrue(actions.switch_app("Chrome"))  # the unrouted legacy matcher is unchanged
        desktop.focus.reset_mock()
        shown[:] = [notepad, word, calc, chrome]
        self.assertEqual(actions.route_names()[1], ("Google Chrome", "Word", "Calculator", "chrome"))
        for name, hwnd in (("Chrome", 4), ("Word", 5), ("Calculator", 6)):
            desktop.focus.reset_mock()
            self.assertTrue(actions.switch_app(name, exact=True))
            desktop.focus.assert_called_once_with(hwnd)

    def test_switch_rechecks_the_guard_after_enumeration_before_focus(self):
        allowed = [True]

        def enumerate_windows():
            allowed[0] = False  # a newer capture arrives while windows are listed
            return [(4, "New Tab - Google Chrome", r"C:\Chrome\chrome.exe")]
        desktop = SimpleNamespace(windows=enumerate_windows, focus=Mock(return_value=True))
        actions = Actions(desktop=desktop, runner=Mock(returncode=1))
        for exact in (True, False):
            allowed[0] = True
            with voiceactions.action_scope(lambda: allowed[0]):
                self.assertFalse(actions.switch_app("Chrome", exact=exact))
        with voiceactions.action_scope(lambda: False):
            self.assertFalse(actions.switch_app("Chrome", exact=True))
        desktop.focus.assert_not_called()

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


class RouterLifecycle(Paired):
    """The daemon's lazily made Laya router never outlives VoiceLink.stop()."""

    def test_no_router_is_made_after_stop_returns(self):
        with patch("jarvis.v2.laya_service.LayaRouter") as router_class:
            self.link.stop()
            self.assertIsNone(self.link._laya_decide("assistant", "open youtube.com", (), ()))
            router_class.assert_not_called()
        self.assertIsNone(self.link._laya)

    def test_router_made_while_stop_runs_is_closed_and_not_replaced(self):
        entered, release, made = threading.Event(), threading.Event(), []

        def factory():
            entered.set()
            release.wait(5)
            router = Mock()
            router.route.return_value = decision("ask")
            made.append(router)
            return router
        self.link._laya_factory = factory
        routing = threading.Thread(target=self.link._laya_decide, args=("assistant", "hi", (), ()))
        routing.start()
        self.assertTrue(entered.wait(5))
        stopping = threading.Thread(target=self.link.stop)
        stopping.start()
        release.set()
        routing.join(5)
        stopping.join(5)
        self.assertFalse(routing.is_alive() or stopping.is_alive())
        router, = made
        router.close.assert_called_once_with()
        self.assertIsNone(self.link._laya)
        # A handler that reaches the router after stop gets none at all.
        self.assertIsNone(self.link._laya_decide("assistant", "hi", (), ()))
        self.assertEqual(len(made), 1)

    def test_route_in_flight_across_stop_cannot_restart_a_worker(self):
        started, release = threading.Event(), threading.Event()
        workers = []

        def classify(router, transcript):
            started.set()
            release.wait(5)
            return LayaRouterReal._classify(router, transcript)

        from jarvis.v2.laya_service import LayaRouter as LayaRouterReal
        with patch("jarvis.v2.laya_service._launch_worker", side_effect=lambda: workers.append(1) or Mock()), \
                patch.object(LayaRouterReal, "_classify", classify):
            routing = threading.Thread(target=self.link._laya_decide, args=("assistant", "what now", (), ()))
            routing.start()
            self.assertTrue(started.wait(5))
            self.link.stop()
            release.set()
            routing.join(5)
            self.assertFalse(routing.is_alive())
            self.assertIsNone(self.link._laya_decide("assistant", "what now", (), ()))
        self.assertEqual(workers, [])

    def test_restart_after_stop_routes_with_a_fresh_router(self):
        made = []

        def factory():
            router = Mock()
            router.route.return_value = decision("ask")
            made.append(router)
            return router
        self.link._laya_factory = factory
        self.assertEqual(self.link._laya_decide("assistant", "hi", (), ())["kind"], "ask")
        self.link.stop()
        made[0].close.assert_called_once_with()
        self.link.port = 0
        self.assertTrue(self.link.start())
        self.assertEqual(self.link._laya_decide("assistant", "hi", (), ())["kind"], "ask")
        self.assertEqual(len(made), 2)
        made[1].close.assert_not_called()
        # The restarted link serves Right Ctrl commands end to end.
        wire = self.client()
        wire.ping()
        self.assertTrue(wire.laya_routing)
        self.assertEqual(wire.laya_route("what is new", "assistant")["kind"], "ask")

    def test_concurrent_restart_keeps_the_new_router_open(self):
        # A start() that runs while stop() is still shutting sockets down must
        # keep its open router state: stop() detached its own router already.
        self.link._laya_factory = lambda: Mock(route=Mock(return_value=decision("ask")))
        self.assertEqual(self.link._laya_decide("assistant", "hi", (), ())["kind"], "ask")
        old_router = self.link._laya
        shutdown, restarted = voice_link._shutdown, []

        def restart_during_shutdown(sock):
            if not restarted:
                restarted.append(True)
                self.link.port = 0
                starting = threading.Thread(target=self.link.start)
                starting.start()
                starting.join(5)
                self.assertIsNotNone(self.link.address)
            shutdown(sock)
        with patch.object(voice_link, "_shutdown", side_effect=restart_during_shutdown):
            self.link.stop()
        old_router.close.assert_called_once_with()
        self.assertEqual(restarted, [True])
        self.assertIsNotNone(self.link.address)
        self.assertEqual(self.link._laya_decide("assistant", "hi", (), ())["kind"], "ask")
        self.assertIsNot(self.link._laya, old_router)
        self.link._laya.close.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
