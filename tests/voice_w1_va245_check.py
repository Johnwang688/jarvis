"""VA-2/4/5 package W1 acceptance tests: typing plan, actions matching, the flow
(orchestrator-owned; the implementer may not edit this file).

Plan: docs/plans/2026-11-16-voice-anywhere-va245.md. The modules must import on
Linux: Windows-only imports happen inside the glue, never at import time.

Run: PYTHONPATH=. .venv/bin/python tests/voice_w1_va245_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))

import voiceactions  # noqa: E402
import voiceflow  # noqa: E402
import voicetype  # noqa: E402
from voicewire import LinkError  # noqa: E402


class Sanitize(unittest.TestCase):
    def test_controls_and_newlines(self):
        self.assertEqual(voicetype.sanitize("hello\nworld\x07\tagain\r\n"), "hello world again")
        self.assertEqual(voicetype.sanitize("a‮b​c"), "abc")   # bidi override, zero width
        self.assertEqual(voicetype.sanitize("  spaced  "), "spaced")


class Plan(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(voicetype.plan("hello world"), [("text", "hello world")])

    def test_send_it(self):
        self.assertEqual(voicetype.plan("send it"), [("key", "enter")])
        self.assertEqual(voicetype.plan("Send it."), [("key", "enter")])
        self.assertEqual(voicetype.plan("fix the tests send it"), [("text", "fix the tests"), ("key", "enter")])
        self.assertEqual(voicetype.plan("Fix the tests. Send it."), [("text", "Fix the tests."), ("key", "enter")])
        self.assertEqual(voicetype.plan("fix the tests, send it"), [("text", "fix the tests"), ("key", "enter")])

    def test_send_it_mid_sentence_is_text(self):
        self.assertEqual(voicetype.plan("send it to Bob tomorrow"), [("text", "send it to Bob tomorrow")])

    def test_new_line_and_scratch(self):
        self.assertEqual(voicetype.plan("new line"), [("key", "shift+enter")])
        self.assertEqual(voicetype.plan("New line."), [("key", "shift+enter")])
        self.assertEqual(voicetype.plan("scratch that"), [("scratch",)])
        self.assertEqual(voicetype.plan("Scratch that!"), [("scratch",)])

    def test_empty(self):
        self.assertEqual(voicetype.plan(""), [])
        self.assertEqual(voicetype.plan("  \n "), [])

    def test_newlines_never_become_keys(self):
        self.assertEqual(voicetype.plan("rm -rf build\nsend it"), [("text", "rm -rf build"), ("key", "enter")])
        self.assertEqual(voicetype.plan("one\ntwo"), [("text", "one two")])


class MightBeCommand(unittest.TestCase):
    def test_yes(self):
        for t in ("Jarvis open Slack", "jarvis, what time is it", "open slack", "Switch to Chrome",
                  "search black holes", "read this", "Launch notepad", "Google cats"):
            with self.subTest(t=t):
                self.assertTrue(voicetype.might_be_command(t))

    def test_no(self):
        for t in ("hello there", "the open door", "I will search later", "", "openness is good",
                  "what time is the meeting"):
            with self.subTest(t=t):
                self.assertFalse(voicetype.might_be_command(t))


APPS = [{"name": "Slack", "id": "s"}, {"name": "Visual Studio Code", "id": "v"},
        {"name": "Google Chrome", "id": "c"}, {"name": "Microsoft Edge", "id": "e"},
        {"name": "Notepad", "id": "n"}, {"name": "Spotify", "id": "p"}]


class Actions(unittest.TestCase):
    def test_match_app(self):
        m = voiceactions.match_app
        self.assertEqual(m("slack", APPS)["id"], "s")
        self.assertEqual(m("Slack.", APPS)["id"], "s")
        self.assertEqual(m("vs code", APPS)["id"], "v")
        self.assertEqual(m("vscode", APPS)["id"], "v")
        self.assertEqual(m("chrome", APPS)["id"], "c")
        self.assertEqual(m("edge", APPS)["id"], "e")
        self.assertEqual(m("notepad", APPS)["id"], "n")
        self.assertIsNone(m("zzz unknown", APPS))
        self.assertIsNone(m("", APPS))

    def test_match_window(self):
        wins = [(1, "Inbox - Gmail - Google Chrome", "chrome.exe"), (2, "Untitled - Notepad", "notepad.exe"),
                (3, "general | Dusk - Slack", "slack.exe")]
        self.assertEqual(voiceactions.match_window("chrome", wins), 1)
        self.assertEqual(voiceactions.match_window("notepad", wins), 2)
        self.assertEqual(voiceactions.match_window("Slack", wins), 3)
        self.assertIsNone(voiceactions.match_window("spotify", wins))

    def test_search_url(self):
        self.assertEqual(voiceactions.search_url("black holes & stars"),
                         "https://www.google.com/search?q=black+holes+%26+stars")


# -- the flow, with fakes ---------------------------------------------------------

class FakeClient:
    def __init__(self, text="hello there", intent=None, answer="Four.", dictate_error=None):
        self.text, self.intent, self.answer, self.dictate_error = text, intent, answer, dictate_error
        self.calls = []

    def dictate(self, wav, source="hotkey"):
        self.calls.append(("dictate", source))
        if self.dictate_error:
            raise self.dictate_error
        return {"text": self.text, "backend": "local"}

    def route(self, text, source):
        self.calls.append(("route", text, source))
        return self.intent or {"kind": "dictate", "text": text, "target": ""}

    def ask(self, text, on_delta=None):
        self.calls.append(("ask", text))
        for piece in ("Fo", "ur."):
            if on_delta:
                on_delta(piece)
        return self.answer

    def say(self, text):
        self.calls.append(("say", text))
        return b"WAV"


class FakeTyper:
    def __init__(self):
        self.runs = []

    def run(self, steps):
        self.runs.append(list(steps))


class FakeActions:
    def __init__(self, found=True, selection="selected words"):
        self.found, self.selection, self.calls = found, selection, []

    def open_app(self, target):
        self.calls.append(("open_app", target))
        return self.found

    def switch_app(self, target):
        self.calls.append(("switch_app", target))
        return self.found

    def search(self, query):
        self.calls.append(("search", query))
        return True

    def read_selection(self):
        self.calls.append(("read_selection",))
        return self.selection


class FakeUI:
    def __init__(self):
        self.states = []

    def set_state(self, state, text=None):
        self.states.append((state, text))

    def push_level(self, rms):
        pass


class FakePlayer:
    def __init__(self):
        self.played = []

    def play(self, wav):
        self.played.append(wav)

    def stop(self):
        pass


def flow(**kw):
    client = kw.pop("client")
    typer, actions, ui, player = FakeTyper(), kw.pop("actions", FakeActions()), FakeUI(), FakePlayer()
    return voiceflow.Flow(client, typer, actions, ui, player), client, typer, actions, ui, player


class Flow(unittest.TestCase):
    def test_hotkey_plain_dictation_types_without_routing(self):
        f, c, t, a, ui, p = flow(client=FakeClient("hello there"))
        f.on_clip(b"wav", "hotkey")
        self.assertEqual(c.calls, [("dictate", "hotkey")])
        self.assertEqual(t.runs, [[("text", "hello there")]])
        self.assertIn("transcribing", [s for s, _ in ui.states])

    def test_hotkey_send_it_types_and_enters(self):
        f, c, t, a, ui, p = flow(client=FakeClient("fix the tests send it"))
        f.on_clip(b"wav", "hotkey")
        self.assertEqual(t.runs, [[("text", "fix the tests"), ("key", "enter")]])
        self.assertNotIn("route", [x[0] for x in c.calls])

    def test_hotkey_command_routes_and_acts(self):
        intent = {"kind": "open_app", "text": "jarvis open slack", "target": "Slack"}
        f, c, t, a, ui, p = flow(client=FakeClient("jarvis open slack", intent=intent))
        f.on_clip(b"wav", "hotkey")
        self.assertIn(("route", "jarvis open slack", "hotkey"), c.calls)
        self.assertEqual(a.calls, [("open_app", "Slack")])
        self.assertEqual(t.runs, [])

    def test_hotkey_maybe_command_routed_back_to_dictation_types(self):
        f, c, t, a, ui, p = flow(client=FakeClient("open the report and check it"))
        f.on_clip(b"wav", "hotkey")
        self.assertIn(("route", "open the report and check it", "hotkey"), c.calls)
        self.assertEqual(t.runs, [[("text", "open the report and check it")]])

    def test_hotkey_question_asks_and_speaks(self):
        intent = {"kind": "ask", "text": "what is two plus two", "target": ""}
        f, c, t, a, ui, p = flow(client=FakeClient("jarvis what is two plus two", intent=intent))
        f.on_clip(b"wav", "hotkey")
        self.assertIn(("ask", "what is two plus two"), c.calls)
        self.assertIn(("say", "Four."), c.calls)
        self.assertEqual(p.played, [b"WAV"])
        self.assertIn("answering", [s for s, _ in ui.states])
        self.assertEqual(t.runs, [])

    def test_wake_always_routes_and_never_types(self):
        for kind in ("ask", "dictate"):
            with self.subTest(kind=kind):
                intent = {"kind": kind, "text": "what time is it", "target": ""}
                f, c, t, a, ui, p = flow(client=FakeClient("what time is it", intent=intent))
                f.on_clip(b"wav", "wake")
                self.assertEqual(c.calls[0], ("dictate", "wake"))
                self.assertIn(("route", "what time is it", "wake"), c.calls)
                self.assertIn(("ask", "what time is it"), c.calls)
                self.assertEqual(t.runs, [])

    def test_wake_actions_run(self):
        intent = {"kind": "switch_app", "text": "switch to chrome", "target": "chrome"}
        f, c, t, a, ui, p = flow(client=FakeClient("switch to chrome", intent=intent))
        f.on_clip(b"wav", "wake")
        self.assertEqual(a.calls, [("switch_app", "chrome")])
        self.assertEqual(t.runs, [])

    def test_wake_refused_voice_stops_there(self):
        f, c, t, a, ui, p = flow(client=FakeClient(dictate_error=LinkError("That didn't sound like you.")))
        f.on_clip(b"wav", "wake")
        self.assertEqual(c.calls, [("dictate", "wake")])
        self.assertEqual(ui.states[-1][0], "error")
        self.assertEqual((t.runs, a.calls, p.played), ([], [], []))

    def test_empty_transcript_does_nothing(self):
        for source in ("hotkey", "wake"):
            f, c, t, a, ui, p = flow(client=FakeClient(""))
            f.on_clip(b"wav", source)
            self.assertEqual(c.calls, [("dictate", source)])
            self.assertEqual((t.runs, a.calls), ([], []))

    def test_search_and_read_selection(self):
        f, c, t, a, ui, p = flow(client=FakeClient("search black holes",
                                                   intent={"kind": "search", "text": "search black holes", "target": "black holes"}))
        f.on_clip(b"wav", "hotkey")
        self.assertEqual(a.calls, [("search", "black holes")])
        f, c, t, a, ui, p = flow(client=FakeClient("read this", intent={"kind": "read_selection", "text": "read this", "target": ""}))
        f.on_clip(b"wav", "hotkey")
        self.assertEqual(a.calls, [("read_selection",)])
        self.assertIn(("say", "selected words"), c.calls)
        self.assertEqual(p.played, [b"WAV"])

    def test_app_not_found_is_an_error_naming_it(self):
        f, c, t, a, ui, p = flow(client=FakeClient("open zork", intent={"kind": "open_app", "text": "open zork", "target": "Zork"}),
                                 actions=FakeActions(found=False))
        f.on_clip(b"wav", "hotkey")
        self.assertEqual(ui.states[-1][0], "error")
        self.assertIn("Zork", ui.states[-1][1] or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
