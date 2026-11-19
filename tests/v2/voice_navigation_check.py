"""Website command grammar, link compatibility and revocation without a desktop."""
import json
import threading
import unittest
from unittest.mock import Mock

from tests.v2.voice_link_va245_impl_check import Base
from jarvis.v2.voice_intent import route
from jarvis.v2.voice_link import parse_intent
from windows.voiceflow import Flow
from windows.voicetype import web_destination
from windows.voicewire import Client, LinkError


PHRASE = "Go to YouTube dot-com."
URL = "https://youtube.com"


class Navigation(unittest.TestCase):
    def test_complete_frames_and_canonical_destinations(self):
        for source, phrase, url in (
            ("hotkey", PHRASE, URL),
            ("hotkey", "Go to WWW . YouTube dot com!", "https://www.youtube.com"),
            ("hotkey", "Hey Jarvis, could you go to HTTP://YouTube.com/ please?", "http://youtube.com"),
            ("wake", "Let's go to youtube.com, please.", URL),
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(route(phrase, source), {"kind": "navigate", "text": phrase, "target": url})
                self.assertEqual(web_destination(url), url)

    def test_only_regular_origins_and_complete_commands(self):
        for destination in (
            "Chrome", "localhost", "desk.local", "foo.localhost", "foo.internal",
            "127.1", "192.168.1.1", "[::1]", "127.0.0.1.nip.io", "lvh.me",
            "file:///C:/Windows", "chrome://settings", "javascript:alert(1)",
            "C:\\Windows\\cmd.exe", "cmd.exe", "command.com", "notes.txt",
            "https://youtube.com@else.com", "youtube.com:8402", "youtube.com\\@else.com",
            "youtube.com/a", "youtube.com?x=1", "youtube.com#comments", "you%74ube.com",
            "youtube.com;calc", "you\u200btube.com", "you\ntube.com", "youtube.com..",
        ):
            with self.subTest(destination=destination):
                self.assertEqual(web_destination(destination), "")
                self.assertNotEqual(route("Go to " + destination, "hotkey")["kind"], "navigate")
        for prose in (
            "I usually go to youtube.com", "When I say go to youtube.com",
            "Go to youtube.com every day", "Go to youtube.com and send it",
            '"Go to youtube.com"', "Go to 'youtube.com'", "Type the words go to youtube.com",
        ):
            self.assertEqual(route(prose, "hotkey")["kind"], "dictate")
        self.assertEqual(route("Go to Paint.", "wake")["kind"], "switch_app")
        self.assertEqual(route("Search for youtube.com.", "wake")["kind"], "search")

    def test_daemon_validates_the_original_text_and_unmodified_target(self):
        self.assertEqual(parse_intent(route(PHRASE, "hotkey"), "hotkey", PHRASE)["target"], URL)
        for text, target in ((PHRASE, "https://else.com"), (PHRASE, URL + "\x00"),
                             ("Go to You\u200bTube.com", URL),
                             ("Type the words go to youtube.com", URL)):
            self.assertEqual(parse_intent({"kind": "navigate", "target": target}, "hotkey", text),
                             {"kind": "dictate", "target": "literal", "text": text.replace("\u200b", "")})

    def test_client_only_requests_navigation_after_feature_ping(self):
        client = Client("127.0.0.1", 1, b"")
        literal = {"kind": "dictate", "text": PHRASE, "target": "literal"}
        client._request = Mock(side_effect=[{"streaming": True}, {"intent": literal},
                                          {"web_navigation": True},
                                          {"intent": {"kind": "navigate", "text": PHRASE, "target": URL}},
                                          {"intent": {"kind": "navigate", "text": PHRASE,
                                                      "target": "https://else.com"}}])
        client.ping()
        self.assertEqual(client.route(PHRASE, "hotkey"), literal)
        self.assertNotIn("web_navigation", client._request.call_args.args[0])
        client.ping()
        self.assertEqual(client.route(PHRASE, "hotkey")["target"], URL)
        self.assertIs(client._request.call_args.args[0]["web_navigation"], True)
        with self.assertRaises(LinkError):
            client.route(PHRASE, "hotkey")
        self.assertFalse(client.web_navigation)

    def test_flow_does_not_type_or_ask_when_navigation_cannot_run(self):
        for source in ("hotkey", "wake"):
            for outcome in ("legacy", "routing_error", "missing_action", "failure", "cancelled"):
                with self.subTest(source=source, outcome=outcome):
                    client, typer, actions, ui, player = Mock(), Mock(), Mock(), Mock(), Mock()
                    player.cancelled = threading.Event()
                    client.route.return_value = {"kind": "navigate", "text": PHRASE,
                                                 "target": URL, "receipt": "test"}
                    if outcome == "legacy":
                        client.route.return_value = {"kind": "dictate" if source == "hotkey" else "ask"}
                    elif outcome == "routing_error":
                        client.route.side_effect = LinkError("The voice link disconnected.")
                    elif outcome == "missing_action":
                        actions.navigate = None
                    elif outcome == "cancelled":
                        def revoke(*args):
                            player.cancelled.set()
                            return True
                        actions.navigate.side_effect = revoke
                    else:
                        actions.navigate.return_value = False
                    Flow(client, typer, actions, ui, player).on_clip(None, source, transcript=PHRASE)
                    typer.run.assert_not_called()
                    client.ask.assert_not_called()
                    client.confirm_action.assert_not_called()
                    if outcome != "missing_action":
                        self.assertEqual(actions.navigate.call_count, int(outcome in ("failure", "cancelled")))


class NavigationRoute(Base):
    def test_old_helper_receives_legacy_kind_and_new_helper_confirms_private_receipt(self):
        self.start()
        client = self.client()
        for source in ("hotkey", "wake"):
            legacy = client._request({"op": "route", "source": source, "text": PHRASE, "receipt": True})
            self.assertEqual(legacy["intent"], {"kind": "dictate", "text": "", "target": "literal"})
            self.assertNotIn("receipt", legacy)
        client.ping()
        intent = client.route(PHRASE, "hotkey", with_receipt=True)
        self.assertEqual(intent["target"], URL)
        self.assertTrue(client.confirm_action(intent["receipt"]))
        self.assertFalse(client.confirm_action(intent["receipt"]))
        rows = self.d.stores.threads.read_log(self.chosen.id)
        self.assertEqual([row["data"]["text"] for row in rows if row["kind"] == "notice"],
                         ["Jarvis voice sent a website to the browser."])
        self.assertNotIn("youtube", json.dumps(rows).casefold())
        self.assertFalse(self.fast.messages)
