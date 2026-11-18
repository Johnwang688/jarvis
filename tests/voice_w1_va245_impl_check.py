"""W1 implementation checks: pure dependencies and mocked Windows boundaries.

Implementation notes (2026-11-16): windll shares SendInput declarations between
backends; clipboard restoration needs a real, non-activating owner window; and
the VA-1 verifier still embeds a headless recorder without the W2 overlay.
"""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy  # Keep audio's import outside patch.dict(sys.modules) capture fakes.

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows import voiceactions as actions, voiceflow, voicetype, voice_agent as agentmod
from windows.voicewire import LinkError, NotJarvis, VoiceQuestionError, wav_bytes


class Clock:
    def __init__(self):
        self.now = 100.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Input:
    def __init__(self):
        self.hwnd, self.calls = 12, []
    def foreground(self):
        return self.hwnd
    def text(self, text):
        self.calls.append(("text", text))
    def key(self, key):
        self.calls.append(("key", key))


class Typing(unittest.TestCase):
    def setUp(self):
        self.clock, self.input = Clock(), Input()
        self.typer = voicetype.Typer(self.input, self.clock)

    def test_scratch_once_same_hwnd_before_deadline(self):
        self.typer.run(voicetype.plan("abc"))
        self.clock.now += 59.99
        self.typer.run(voicetype.plan("scratch that"))
        self.typer.run(voicetype.plan("scratch that"))
        self.assertEqual(self.input.calls, [("text", "abc")] + [("key", "backspace")] * 3)

    def test_scratch_at_deadline_or_other_window_is_noop(self):
        for elapsed, hwnd in ((60, 12), (61, 12), (1, 44), (-1, 12)):
            with self.subTest(elapsed=elapsed, hwnd=hwnd):
                self.input.hwnd = 12
                self.typer.run([("text", "abc")])
                self.clock.now += elapsed
                self.input.hwnd = hwnd
                before = list(self.input.calls)
                self.typer.run([("scratch",)])
                self.assertEqual(self.input.calls, before)

    def test_scratch_latest_insert_and_hud_allowed(self):
        # The backend gives the HUD the same hwnd treatment as any app.
        self.typer.run([("text", "first"), ("text", "hi")])
        self.typer.run([("scratch",)])
        self.assertEqual(self.input.calls[-2:], [("key", "backspace")] * 2)

    def test_enter_clears_scratch_until_more_text_is_typed(self):
        for key in ("enter", "shift+enter"):
            with self.subTest(key=key):
                self.typer.run([("text", "old"), ("key", key), ("scratch",)])
                self.assertEqual(self.input.calls[-2:], [("text", "old"), ("key", key)])
                self.typer.run([("text", "new"), ("scratch",)])
                self.assertEqual(self.input.calls[-4:],
                                 [("text", "new")] + [("key", "backspace")] * 3)

    def test_failed_enter_also_invalidates_scratch(self):
        self.typer.run([("text", "old")])
        self.input.key = Mock(side_effect=OSError("Partial input"))
        with self.assertRaises(OSError):
            self.typer.run([("key", "enter")])
        self.typer.run([("scratch",)])
        self.input.key.assert_called_once_with("enter")

    def test_scratch_stops_when_focus_changes_mid_delete(self):
        self.typer.run([("text", "abc")])
        def key(name):
            self.input.calls.append(("key", name))
            self.input.hwnd = 99
        self.input.key = key
        self.typer.run([("scratch",)])
        self.assertEqual(self.input.calls, [("text", "abc"), ("key", "backspace")])

    def test_failed_insert_does_not_leave_old_scratch(self):
        self.typer.run([("text", "old")])
        self.input.text = Mock(side_effect=OSError())
        with self.assertRaises(OSError):
            self.typer.run([("text", "new")])
        self.typer.run([("scratch",)])
        self.assertEqual(self.input.calls, [("text", "old")])

    def test_unicode_sendinput_uses_surrogate_units_and_keyup(self):
        backend = voicetype.WindowsInput.__new__(voicetype.WindowsInput)
        backend._send = Mock()
        backend.text("A😀")
        self.assertEqual(backend._send.call_args.args[0],
                         [(0, 65, 4), (0, 65, 6), (0, 0xD83D, 4), (0, 0xD83D, 6),
                          (0, 0xDE00, 4), (0, 0xDE00, 6)])
        backend.key("shift+enter")
        self.assertEqual(backend._send.call_args.args[0],
                         [(0x10, 0, 0), (0x0D, 0, 0), (0x0D, 0, 2), (0x10, 0, 2)])

    def test_typer_and_clipboard_input_backends_can_share_win32(self):
        calls = []
        @ctypes.CFUNCTYPE(ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int)
        def send(count, data, size):
            calls.append(count)
            return count
        user32 = SimpleNamespace(SendInput=send, GetForegroundWindow=Mock())
        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            first, second = voicetype.WindowsInput(), voicetype.WindowsInput()
            first.text("hello")
            second.key("ctrl+c")
            first.key("enter")
        self.assertEqual(calls, [10, 4, 2])

    def test_partial_key_failure_releases_modifier(self):
        backend = voicetype.WindowsInput.__new__(voicetype.WindowsInput)
        backend._send = Mock(side_effect=[OSError("Input refused"), None])
        with self.assertRaises(OSError):
            backend.key("shift+enter")
        self.assertEqual(backend._send.call_args.args[0], [(0x0D, 0, 2), (0x10, 0, 2)])

    def test_grammar_preserves_sentence_punctuation(self):
        for punctuation in ".!?":
            self.assertEqual(voicetype.plan(f"Hi{punctuation}, SEND IT!!!"),
                             [("text", f"Hi{punctuation}"), ("key", "enter")])
        for text in ("scratch that later", "a new line here", "send it to Bob", "resend it"):
            self.assertEqual(voicetype.plan(text), [("text", text)])
        self.assertEqual(voicetype.plan("NEW, LINE!!!"), [("key", "shift+enter")])
        self.assertEqual(voicetype.plan("NEW_LINE!"), [("key", "shift+enter")])
        self.assertEqual(voicetype.plan("Hello,\u00a0send it!"), [("text", "Hello"), ("key", "enter")])
        self.assertEqual(voicetype.plan("\x00\u202e\u200b"), [])
        self.assertEqual(voicetype.plan("a\r\nb\tc\x01d"), [("text", "a b cd")])

    def test_local_precheck_whole_words(self):
        for word in ("jarvis", "open", "launch", "start", "switch", "search", "google", "read"):
            self.assertTrue(voicetype.might_be_command(word.upper() + ": do it"))
            self.assertFalse(voicetype.might_be_command(word + "ish do it"))

    def test_all_unicode_line_boundaries_become_spaces(self):
        for separator in ("\r", "\n", "\t", "\v", "\f", "\x1c", "\x1d", "\x1e",
                          "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=ascii(separator)):
                self.assertEqual(voicetype.plan("alpha" + separator + "beta"),
                                 [("text", "alpha beta")])

    def test_command_marks_and_symbols_remain_dictation(self):
        for command in ("send it", "hello send it", "new line", "scratch that"):
            for suffix in ("\u0301", "\u0338", "\u20dd", "\u0301!", "😀", "+"):
                text = command + suffix
                with self.subTest(text=ascii(text)):
                    self.assertEqual(voicetype.plan(text), [("text", text)])
        for text in ("new\u0301 line", "scratch\u20dd that", "send\u0301 it",
                     "resend it", "hello😀send it"):
            with self.subTest(text=ascii(text)):
                self.assertEqual(voicetype.plan(text), [("text", text)])


APPS = [{"name": "Visual Studio Code", "id": "code-id"},
        {"name": "Google Chrome", "id": "chrome-id"},
        {"name": "Microsoft Edge", "id": "edge-id"},
        {"name": "Notepad", "id": "note-id"}]


class Matching(unittest.TestCase):
    def test_aliases_prefix_words_and_typo(self):
        for name in ("vs code", "VS-Code!", "vscode", "code"):
            self.assertEqual(actions.match_app(name, APPS), APPS[0])
        self.assertEqual(actions.match_app("chrome", APPS), APPS[1])
        self.assertEqual(actions.match_app("EDGE.", APPS), APPS[2])
        self.assertEqual(actions.match_app("note", APPS), APPS[3])
        self.assertEqual(actions.match_app("studio", APPS), APPS[0])
        self.assertEqual(actions.match_app("notpad", APPS), APPS[3])

    def test_recognizer_sound_alikes_x_and_cs(self):
        # Live 2026-11-17: Parakeet heard "open Codex" as "Open codecs."
        codex = {"name": "Codex", "id": "codex-id"}
        self.assertEqual(actions.match_app("codecs", APPS + [codex]), codex)
        self.assertEqual(actions.match_app("codecs", [{"name": "Codecs Pack", "id": "p"}, codex])["id"], "p")
        self.assertIsNone(actions.match_app("codecs", APPS))

    def test_exact_precedes_prefix_and_threshold_is_inclusive(self):
        entries = [{"name": "Notepad++", "id": "plus"}, APPS[-1]]
        # Punctuation normalization makes these equal, so use a distinct prefix.
        entries[0]["name"] = "Notepad Pro"
        self.assertEqual(actions.match_app("notepad", entries), APPS[-1])
        self.assertEqual(actions.match_app("abce", [{"name": "abcd", "id": "x"}])["id"], "x")
        for target in ("abxy", "!!!", "", "rundll32.exe && do anything"):
            self.assertIsNone(actions.match_app(target, [{"name": "abcd", "id": "x"}]))

    def test_short_targets_require_exact_names(self):
        entries = [{"name": "Random Utility", "id": "r"},
                   {"name": "An Editor", "id": "e"}]
        for target in ("r", "ra", "R!", "a n", "an"):
            with self.subTest(target=target):
                self.assertIsNone(actions.match_app(target, entries))
        for name in ("R", "An"):
            exact = {"name": name, "id": "exact"}
            self.assertEqual(actions.match_app(name, entries + [exact]), exact)

    def test_windows_match_exe_stem_first_and_title_whole_words(self):
        wins = [(1, "Chrome release notes", "editor.exe"),
                (2, "Browser", r"C:\Program Files\Google\Chrome\chrome.exe"),
                (3, "project — Visual Studio Code", "Code.exe")]
        self.assertEqual(actions.match_window("google chrome", wins), 2)
        self.assertEqual(actions.match_window("vscode", wins), 3)
        self.assertEqual(actions.match_window("release notes", wins), 1)
        self.assertIsNone(actions.match_window("chrom", wins))
        self.assertIsNone(actions.match_window("", wins))
        self.assertIsNone(actions.match_window("code", [(4, "Decoder", "decoder.exe")]))

    def test_search_encodes_query_in_fixed_url(self):
        self.assertEqual(actions.search_url('a & x/#?=雪'),
                         'https://www.google.com/search?q=a+%26+x%2F%23%3F%3D%E9%9B%AA')


class Clipboard:
    def __init__(self, selection="chosen", changed=True, fail=False):
        self.value, self.sequence = "previous", 1
        self.selection, self.changed, self.fail = selection, changed, fail
        self.restored = []
    def clipboard_text(self):
        if self.fail and self.sequence == 2:
            raise OSError("Read failed")
        return self.value
    def clipboard_sequence(self):
        return self.sequence
    def copy(self):
        if self.changed:
            self.value, self.sequence = self.selection, 2
    def set_clipboard_text(self, text):
        self.value = text
        self.restored.append(text)


class ActionGlue(unittest.TestCase):
    def make(self, desktop=None, runner=None):
        self.clock = Clock()
        self.launcher, self.opener = Mock(), Mock()
        self.runner = runner or Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(
            [{"Name": app["name"], "AppID": app["id"]} for app in APPS])))
        self.desktop = desktop or Clipboard()
        return actions.Actions(self.desktop, self.clock, self.clock.sleep, self.runner,
                               self.launcher, self.opener)

    def test_index_cached_ten_minutes_launch_uses_only_indexed_id(self):
        a = self.make()
        self.assertEqual(self.runner.call_count, 1)
        self.assertTrue(a.open_app("vs code"))
        self.assertEqual(self.launcher.call_args.args[0], ["explorer.exe", "shell:AppsFolder\\code-id"])
        self.assertNotIn("shell", self.launcher.call_args.kwargs)
        self.assertEqual(self.runner.call_args.args[0],
                         ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json"])
        self.assertFalse(a.open_app("powershell -Command arbitrary"))
        self.assertEqual(self.launcher.call_count, 1)
        self.clock.now += 599.9
        a.refresh_apps()
        self.assertEqual(self.runner.call_count, 1)
        self.clock.now += 0.1
        a.refresh_apps()
        self.assertEqual(self.runner.call_count, 2)

    def test_exact_launch_accepts_display_or_alias_but_no_fuzzy_or_ambiguous_match(self):
        a = self.make()
        for name in ("Google Chrome", "CHROME"):
            self.assertTrue(a.open_app(name, exact=True))
            self.launcher.assert_called_once_with(
                ["explorer.exe", "shell:AppsFolder\\chrome-id"],
                creationflags=getattr(actions.subprocess, "CREATE_NO_WINDOW", 0))
            self.launcher.reset_mock()
        for name in ("Chrom", "Chrome Canary", "Notpad", "Visual", "Chrome.exe --incognito"):
            self.assertFalse(a.open_app(name, exact=True))
        for unsafe in ("PowerShell (Administrator)", "Elevated Windows Terminal", "C:/Windows/System32/cmd.exe"):
            a.apps.append({"name": unsafe, "id": "unsafe-id"})
            self.assertFalse(a.open_app(unsafe, exact=True))
        a.apps.append({"name": "Chrome", "id": "different-id"})
        self.assertFalse(a.open_app("chrome", exact=True))
        a.apps.append({"name": "GOOGLE CHROME", "id": "duplicate-name-id"})
        self.assertFalse(a.open_app("Google Chrome", exact=True))
        self.launcher.assert_not_called()
        self.assertEqual(self.runner.call_count, 1)

    def test_failed_refresh_keeps_cache_and_is_throttled(self):
        a = self.make()
        self.runner.side_effect = OSError()
        self.clock.now += 600
        self.assertTrue(a.open_app("notepad"))
        self.assertTrue(a.open_app("notepad"))
        self.assertEqual(self.runner.call_count, 2)

    def test_short_target_never_launches_a_prefix_match(self):
        a = self.make()
        for target in ("v", "vi", "g", "m", "no"):
            self.assertFalse(a.open_app(target))
        self.launcher.assert_not_called()
        self.assertTrue(a.open_app("edge"))
        self.launcher.assert_called_once_with(
            ["explorer.exe", "shell:AppsFolder\\edge-id"],
            creationflags=getattr(actions.subprocess, "CREATE_NO_WINDOW", 0))

    def test_singleton_and_malformed_index(self):
        for payload, count in (({"Name": "Solo", "AppID": "s"}, 1), (None, 0),
                               ([{}, {"Name": 123, "AppID": "x"}], 0)):
            with self.subTest(payload=payload):
                a = self.make(runner=Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))))
                self.assertEqual(len(a.apps), count)

    def test_search_and_switch(self):
        a = self.make()
        a.search("cats & dogs")
        self.opener.assert_called_once_with(actions.search_url("cats & dogs"))
        self.desktop.windows = Mock(return_value=[(1, "Notes", "notepad.exe")])
        self.desktop.focus = Mock(return_value=True)
        self.assertTrue(a.switch_app("notepad"))
        self.assertFalse(a.switch_app("chrome"))
        self.desktop.focus.assert_called_once_with(1)

    def test_read_selection_caps_and_restores(self):
        a = self.make(Clipboard("x" * 5000))
        self.assertEqual(a.read_selection(), "x" * 4000)
        self.assertEqual(self.desktop.restored, ["previous"])

    def test_same_text_copy_detected_by_sequence(self):
        a = self.make(Clipboard("previous"))
        self.assertEqual(a.read_selection(), "previous")
        self.assertEqual(self.desktop.restored, ["previous"])

    def test_selection_timeout_never_reads_old_clipboard(self):
        a = self.make(Clipboard(changed=False))
        self.assertIsNone(a.read_selection())
        self.assertGreaterEqual(self.clock.now, 100.3)
        self.assertLess(self.clock.now, 100.32)
        self.assertEqual(self.desktop.restored, ["previous"])

    def test_selection_errors_still_restore(self):
        a = self.make(Clipboard(fail=True))
        with self.assertRaises(OSError):
            a.read_selection()
        self.assertEqual(self.desktop.restored, ["previous"])


class FlowErrors(unittest.TestCase):
    def make(self, kind="ask"):
        self.client = Mock()
        self.client.dictate.return_value = {"text": "Jarvis do this"}
        self.client.route.return_value = {"kind": kind, "text": "question", "target": "Target"}
        self.client.ask.return_value, self.client.say.return_value = "answer", b"audio"
        self.typer, self.actions, self.ui, self.player = Mock(), Mock(), Mock(), Mock()
        return voiceflow.Flow(self.client, self.typer, self.actions, self.ui, self.player)

    def test_windows_does_not_override_classified_mentions_or_literal_dictation(self):
        for source, spoken, payload in (
                ("wake", "Open Chrome was what she said.", "Open Chrome was what she said."),
                ("hotkey", "Type the words send it", "send it"),
                ("hotkey", "Type the words open Google Chrome for me", "open Google Chrome for me"),
                ("hotkey", 'She said "scratch that"', 'She said "scratch that"')):
            with self.subTest(source=source, spoken=spoken):
                flow = self.make()
                self.client.dictate.return_value = {"text": spoken}
                self.client.route.return_value = {"kind": "dictate", "text": payload, "target": "literal"}
                flow.on_clip(b"fixture", source)
                self.client.route.assert_called_once_with(spoken, source)
                self.actions.open_app.assert_not_called()
                self.client.ask.assert_not_called()
                self.client.clean.assert_not_called()
                if source == "hotkey":
                    self.typer.run.assert_called_once_with([("text", payload)])
                else:
                    self.typer.run.assert_not_called()

    def test_shortcut_exact_launch_acknowledges_once_without_typing(self):
        spoken = "Open Google Chrome for me."
        flow = self.make("open_app")
        self.client.route.return_value = {"kind": "open_app", "text": spoken,
                                         "target": "google chrome", "receipt": "ticket"}
        self.actions.open_app.return_value = True
        self.client.confirm_action.side_effect = LinkError("Lost acknowledgement")
        flow.on_clip(None, "hotkey", transcript=spoken)
        self.actions.open_app.assert_called_once_with("Google Chrome", exact=True)
        self.client.confirm_action.assert_called_once_with("ticket")
        self.typer.run.assert_not_called()
        self.ui.set_state.assert_called_with("done", "Opened Google Chrome.")
        self.assertIsNotNone(flow.link_error)

    def test_shortcut_exact_miss_types_original_words_without_edit_or_receipt(self):
        flow = self.make("open_app")
        spoken = "Open Chrome Canary for me!"
        self.client.route.return_value = {"kind": "open_app", "text": "modified",
                                         "target": "Chrome Canary", "receipt": "ticket"}
        self.actions.open_app.return_value = False
        flow.on_clip(None, "hotkey", transcript=spoken)
        self.actions.open_app.assert_called_once_with("Chrome Canary", exact=True)
        self.typer.run.assert_called_once_with([("text", spoken)])
        self.client.clean.assert_not_called()
        self.client.confirm_action.assert_not_called()

    def test_shortcut_os_launch_failure_shows_error_without_typing_or_receipt(self):
        flow = self.make("open_app")
        self.client.route.return_value = {"kind": "open_app", "text": "open Chrome", "target": "Chrome", "receipt": "ticket"}
        self.actions.open_app.side_effect = OSError("launcher unavailable")
        flow.on_clip(None, "hotkey", transcript="Open Chrome.")
        self.actions.open_app.assert_called_once_with("Chrome", exact=True)
        self.ui.set_state.assert_called_with("error", "The voice action could not be completed.")
        self.typer.run.assert_not_called()
        self.client.confirm_action.assert_not_called()

    def test_unaddressed_prose_and_mismatched_daemon_target_stay_literal(self):
        for spoken in ("open enrollment send it", "Open Google Chrome was what she said.",
                       "I use open source software", "Open Chrome on my phone.",
                       "She said “open Chrome”", "Open Notepad."):
            with self.subTest(spoken=spoken):
                flow = self.make("open_app")
                self.client.route.return_value = {"kind": "open_app", "text": "different words",
                                                 "target": "Chrome", "receipt": "ticket"}
                flow.on_clip(None, "hotkey", transcript=spoken)
                self.actions.open_app.assert_not_called()
                self.typer.run.assert_called_once_with([("text", spoken)])
                self.client.confirm_action.assert_not_called()

    def test_linkerror_outside_routing_surfaces_exact_text(self):
        for stage in ("dictate", "ask", "say"):
            with self.subTest(stage=stage):
                f = self.make()
                error = LinkError("Link failure " + stage)
                getattr(self.client, stage).side_effect = error
                f.on_clip(b"wav", "wake")
                self.ui.set_state.assert_called_with("error", str(error))
                self.assertIs(f.link_error, error)
                self.typer.run.assert_not_called()
                self.player.play.assert_not_called()

    def test_selecting_a_chat_after_a_refused_question_keeps_the_microphone(self):
        f = self.make()
        error = VoiceQuestionError("Open a chat in the HUD, then ask again.")
        self.client.ask.side_effect = error
        f.on_clip(b"wav", "wake")
        self.ui.set_state.assert_called_with("error", str(error))
        self.assertIsNone(f.link_error)
        self.player.play.assert_not_called()

    def test_route_errors_use_original_words_and_source_fallback(self):
        for error_type in (LinkError, OSError, RuntimeError, ValueError):
            for source in ("hotkey", "wake"):
                with self.subTest(error=error_type.__name__, source=source):
                    f = self.make("open_app")
                    text = "open the report,\nsend it!"
                    self.client.dictate.return_value = {"text": text}
                    error = error_type("Route failed")
                    self.client.route.side_effect = error
                    f.on_clip(b"wav", source)
                    self.client.route.assert_called_once_with(text, source)
                    self.actions.open_app.assert_not_called()
                    if source == "hotkey":
                        self.typer.run.assert_called_once_with(
                            [("text", text)])
                        self.client.ask.assert_not_called()
                        self.player.play.assert_not_called()
                    else:
                        self.typer.run.assert_not_called()
                        self.client.ask.assert_called_once_with(text, on_delta=unittest.mock.ANY)
                        self.player.play.assert_called_once_with(b"audio")
                    self.assertIs(f.link_error, error if error_type is LinkError else None)
                    self.ui.set_state.assert_called_with("done")

    def test_route_link_error_is_cleared_before_next_clip(self):
        f = self.make()
        self.client.route.side_effect = LinkError("Route failed")
        f.on_clip(b"wav", "hotkey")
        self.client.route.side_effect = None
        self.client.route.return_value = {"kind": "dictate", "text": "plain dictation", "target": ""}
        self.client.dictate.return_value = {"text": "plain dictation"}
        f.on_clip(b"wav", "hotkey")
        self.assertIsNone(f.link_error)
        self.typer.run.assert_called_with([("text", "plain dictation")])

    def test_selection_missing_and_switch_failure(self):
        f = self.make("read_selection")
        self.actions.read_selection.return_value = None
        f.on_clip(b"wav", "wake")
        self.ui.set_state.assert_called_with("error", "No selected text to read.")
        self.client.say.assert_not_called()
        f = self.make("switch_app")
        self.actions.switch_app.return_value = False
        f.on_clip(b"wav", "wake")
        self.ui.set_state.assert_called_with("error", "Could not find Target.")

    def test_os_error_does_not_expose_exception_paths(self):
        f = self.make("open_app")
        self.actions.open_app.side_effect = OSError("private path /secret")
        f.on_clip(b"wav", "wake")
        self.ui.set_state.assert_called_with("error", "The voice action could not be completed.")

    def test_deltas_accumulate_and_playback_finishes_before_done(self):
        f = self.make()
        def ask(text, on_delta):
            on_delta("An")
            on_delta("swer")
            return "Answer"
        self.client.ask.side_effect = ask
        self.player.play.side_effect = lambda wav: self.ui.set_state.assert_called_with("answering", "Answer")
        f.on_clip(b"wav", "wake")
        self.assertIn(unittest.mock.call("answering", "Answer"), self.ui.set_state.call_args_list)
        self.ui.set_state.assert_called_with("done")
        self.player.play.assert_called_once_with(b"audio")

    def test_whitespace_empty_and_search_text_fallback(self):
        f = self.make()
        self.client.dictate.return_value = {"text": " \n\x00 "}
        f.on_clip(b"wav", "hotkey")
        self.client.route.assert_not_called()
        self.ui.set_state.assert_called_with("done", "No speech heard")
        f = self.make("search")
        self.client.route.return_value["target"] = ""
        f.on_clip(b"wav", "wake")
        self.actions.search.assert_called_once_with("question")


class AgentGlue(unittest.TestCase):
    def bare(self):
        a = agentmod.Agent.__new__(agentmod.Agent)
        a.done, a.online = threading.Event(), threading.Event()
        a.recording, a.flow_running = threading.Event(), threading.Event()
        a.overlay, a.player = Mock(), agentmod.Player()
        a.generation, a.generation_lock = 1, threading.Lock()
        a.capture_lock = threading.Lock()
        a.capture, a.chunks, a.frames, a.audio_bad = None, [], 0, False
        a.clips = queue.Queue(maxsize=4)
        a.host, a.port, a.key_path = "unused", 0, "unused"
        a.user32 = SimpleNamespace(GetAsyncKeyState=lambda key: 0)
        return a

    def test_busy_covers_capture_player_and_flow(self):
        a = self.bare()
        self.assertFalse(a.busy())
        for event in (a.recording, a.player.playing, a.flow_running):
            event.set()
            self.assertTrue(a.busy())
            event.clear()

    def test_wake_skips_busy_and_offline_and_passes_source_without_a_foreground_check(self):
        a = self.bare()
        a.recording.set()
        a.on_wake(b"wav")
        a.recording.clear()
        a.on_wake(b"offline")
        self.assertTrue(a.clips.empty())
        a.online.set()
        a.on_wake(b"wav")
        self.assertEqual(a.clips.get_nowait(), (3, b"wav", "wake"))
        a.overlay.reset_mock()
        a.wake_state("listening")
        a.overlay.set_state.assert_called_once_with("listening", None)

    def test_offline_hotkey_clip_is_discarded(self):
        a = self.bare()
        a.chunks = [b"\0\0" * 100]
        a.recording.set()
        a.finish_capture()
        self.assertTrue(a.clips.empty())
        self.assertFalse(a.recording.is_set())

    def test_capture_remembers_offline_interval_and_next_capture_recovers(self):
        from array import array
        for starts_online, disconnect in ((False, False), (True, True), (True, False)):
            with self.subTest(starts_online=starts_online, disconnect=disconnect):
                a = self.bare()
                if starts_online:
                    a.online.set()
                stream = Mock()
                sd = SimpleNamespace(
                    query_hostapis=lambda: [{"name": "WASAPI", "default_input_device": 0}],
                    WasapiSettings=Mock(), InputStream=Mock(return_value=stream))
                with patch.dict(sys.modules, {"sounddevice": sd}):
                    a.start_capture()
                    a.audio(array("h", [100] * 160), 160, None, False)
                    if disconnect:
                        a.online.clear()
                        a.discard_clips("Jarvis isn't running")
                    a.online.set()
                    a.finish_capture()
                    if not starts_online or disconnect:
                        self.assertTrue(a.clips.empty())
                        a.overlay.set_state.assert_called_with("error", "Jarvis isn't running")
                    else:
                        self.assertEqual(a.clips.get_nowait()[2], "hotkey")
                    # Invalidation belongs to one capture, not every future clip.
                    a.start_capture()
                    a.audio(array("h", [100] * 160), 160, None, False)
                    a.finish_capture()
                generation, wav, source = a.clips.get_nowait()
                self.assertEqual((generation, source), (1, "hotkey"))
                self.assertEqual(wav, wav_bytes(array("h", [100] * 160).tobytes()))
                self.assertFalse(a.recording.is_set())
                self.assertEqual(stream.stop.call_count, 2)
                self.assertEqual(stream.close.call_count, 2)

    def test_capture_invalidated_while_packaging_wav_is_discarded(self):
        a = self.bare()
        a.online.set()
        a.capture_online = True
        a.chunks = [b"\0\0" * 100]
        def encode(pcm):
            a.online.clear()
            a.discard_clips("Jarvis isn't running")
            a.online.set()
            return wav_bytes(pcm)
        with patch.object(agentmod, "wav_bytes", side_effect=encode):
            a.finish_capture()
        self.assertTrue(a.clips.empty())
        a.overlay.set_state.assert_called_with("error", "Jarvis isn't running")

    def test_network_runs_both_sources_sequentially_on_same_client(self):
        a = self.bare()
        client = Mock()
        client.dictate.return_value = {"text": "hello"}
        client.route.side_effect = lambda text, source: {"kind": "ask" if source == "wake" else "dictate",
                                                         "text": text, "target": ""}
        client.ask.return_value, client.say.return_value = "answer", wav_bytes(b"\0\0")
        typer, action = Mock(), Mock()
        def dictate(wav, source):
            self.assertTrue(a.flow_running.is_set())
            if source == "hotkey":
                a.enqueue(b"second", "wake", 1)
            else:
                a.done.set()
            return {"text": "hello"}
        client.dictate.side_effect = dictate
        a.player.play = Mock()
        real_set = a.online.set
        def connected():
            real_set()
            a.enqueue(b"first", "hotkey", 1)
        with patch.object(agentmod, "Client", return_value=client), \
             patch.object(agentmod, "load_key", return_value=b"key"), \
             patch.object(agentmod, "Typer", return_value=typer), \
             patch.object(agentmod, "Actions", return_value=action), \
             patch.object(a.online, "set", side_effect=connected):
            a.network()
        self.assertEqual(client.dictate.call_args_list,
                         [unittest.mock.call(b"first", source="hotkey"),
                          unittest.mock.call(b"second", source="wake")])
        typer.run.assert_called_once_with([("text", "hello")])
        client.ask.assert_called_once()
        self.assertFalse(a.flow_running.is_set())
        self.assertFalse(a.online.is_set())
        client.close.assert_called_once()
        action.close.assert_called_once()

    def test_second_hotkey_during_first_decode_preserves_both_dictations(self):
        agent = self.bare()
        client, typer = Mock(), Mock()
        client.streaming = True
        first, second = agentmod.Capture(), agentmod.Capture()
        def connected():
            agent.clips.put((1, first, "hotkey"))
        def transcript(capture):
            if capture is first:
                agent.generation = 2
                agent.clips.put((2, second, "hotkey"))
                return {"text": "first phrase"}
            return {"text": "second phrase"}

        def typed(steps):
            if steps == [("text", "second phrase")]:
                agent.done.set()
        typer.run.side_effect = typed

        timeout = threading.Timer(2, agent.done.set)
        timeout.start()
        self.addCleanup(timeout.cancel)
        client.route.side_effect = lambda text, source: {"kind": "dictate", "text": text, "target": ""}
        client.dictate_capture.side_effect = transcript
        with patch.object(agentmod, "Client", return_value=client), \
                patch.object(agentmod, "load_key", return_value=b"key"), \
                patch.object(agentmod, "Typer", return_value=typer), \
                patch.object(agentmod, "Actions"), patch.object(agent.online, "set", side_effect=connected):
            agent.network()
        self.assertEqual(typer.run.call_args_list,
                         [unittest.mock.call([("text", "first phrase")]),
                          unittest.mock.call([("text", "second phrase")])])

    def test_reconnect_backoff_and_offline_queue_discard(self):
        a = self.bare()
        a.clips.put((1, b"stale", "wake"))
        client = Mock()
        client.connect.side_effect = NotJarvis("That wasn't Jarvis — refused")
        delays = []
        def wait(delay):
            delays.append(delay)
            if len(delays) == 3:
                a.done.set()
        with patch.object(agentmod, "Client", return_value=client), \
             patch.object(agentmod, "load_key", return_value=b"key"), \
             patch.object(agentmod, "Typer"), patch.object(agentmod, "Actions"), \
             patch.object(a.done, "wait", side_effect=wait):
            a.network()
        self.assertEqual(delays, [1.0, 2.0, 4.0])
        self.assertTrue(a.clips.empty())
        client.dictate.assert_not_called()

    def test_barge_in_suppresses_pending_tts_until_next_flow(self):
        player = agentmod.Player()
        player.begin()
        player.stop()
        player.play(b"invalid wav would fail without cancellation")
        self.assertFalse(player.playing.is_set())
        player.begin()
        self.assertFalse(player.cancelled.is_set())
        player.stream = Mock()
        player.stop()
        player.stream.abort.assert_called_once()

    def test_playback_stops_active_stream_and_clears_busy(self):
        player, output = agentmod.Player(), MagicMock()
        output.__enter__.return_value = output
        output.write.side_effect = lambda data: player.stop()
        sd = SimpleNamespace(RawOutputStream=Mock(return_value=output), PortAudioError=RuntimeError)
        with patch.dict(sys.modules, {"sounddevice": sd}):
            player.play(wav_bytes(b"\0\0" * 3200))
        self.assertEqual(output.write.call_count, 1)
        output.abort.assert_called_once()
        self.assertFalse(player.playing.is_set())
        self.assertIsNone(player.stream)

    def test_hotkey_capture_pushes_normalized_rms(self):
        from array import array
        a = self.bare()
        a.audio(array("h", [16384, -16384]), 2, None, False)
        a.overlay.push_level.assert_called_once_with(0.5)
        self.assertEqual(a.frames, 2)

    def test_failed_wake_start_shows_reason_once_and_keeps_hotkey_worker(self):
        a = self.bare()
        a.root, a.wake = Mock(), None
        listener = Mock(reason="Model missing")
        listener.start.return_value = False
        with patch.object(agentmod.threading, "Thread") as thread, \
             patch.object(agentmod.voicewake, "WakeListener", return_value=listener):
            a.run()
        self.assertEqual(thread.call_count, 2)
        a.root.mainloop.assert_called_once()
        a.overlay.set_state.assert_called_once_with("error", "Model missing")
        listener.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
