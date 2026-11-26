"""Offline coverage for the v3 microphone and pop-up approval boundary."""

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis import config, tools
from jarvis.v3 import claude_jobs, fastpath, policy
from jarvis.v3.approvals import ApprovalBroker
from jarvis.v3.tools.memory import MemoryTools
from jarvis.v3.voice import approving, wire
from jarvis.v3.voice.claude_brain import ClaudeVoiceBrain
from jarvis.v3.voice.link import VoiceLink
from jarvis.v3.voice.session import ECHO_TAIL_S, VoiceSession
from jarvis.v3.voice.turns import VoiceTurns
from voice_brain_check import Factory, Recorder, REFUSAL, assistant, frame, result, script, tool_block
from voice_helper_check import FakeDevices
from voice_link_check import Helper, KEY_TEXT, until
from voice_popup_check import FakeWin32, drawing, popup
from voice_session_check import CANCELLED, QUIET, RAW, Detector, Rows, Transcriber, Voice
from voice_turns_check import VAULT_ADD
from windows import v3voice_main


YES = ("yes", "yeah", "yep", "yes please", "approve", "approved", "go ahead", "do it",
       "allow", "allow it", "yes go ahead", "yes do it")
NO = ("no", "nope", "no thanks", "deny", "denied", "don't", "do not", "cancel", "reject", "stop")


class BrokerFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.log = self.root / "approvals.jsonl"
        self.now = 100.0
        self.clock = lambda: self.now
        self.broker = ApprovalBroker(clock=self.clock, log=self.log)
        self.approving = approving.VoiceApproving(self.broker, clock=self.clock)
        self.pool = self.enterContext(ThreadPoolExecutor(max_workers=3))
        self.addCleanup(self.broker.shutdown)

    def ask(self, tool="vault_add", *, turn="voice"):
        announced = Event()
        waiter = self.pool.submit(
            self.broker.ask, tool, {}, {"title": "Apply the fixture change", "rows": []},
            session_id=turn, turn_id=turn, verdict={"level": "L2", "why": "fixture"},
            on_pending=announced.set,
        )
        self.assertTrue(announced.wait(3))
        return self.broker.pending(turn_id=turn)[-1], waiter

    def arm(self, card):
        allowed = self.approving.ask(card)
        self.approving.prompt_finished(card["id"], self.now)
        return allowed

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def click(self, card, decision="approve"):
        return self.approving.click(card["id"], card["digest"], decision)


class ApprovalAnswers(BrokerFixture):
    def test_every_whole_phrase_and_private_log(self):
        for phrases, state in ((YES, "approved"), (NO, "denied")):
            for index, phrase in enumerate(phrases):
                with self.subTest(phrase=phrase):
                    card, waiter = self.ask()
                    self.assertTrue(self.arm(card))
                    prefix = ("", "Jarvis, ", "HEY — Jarvis: ")[index % 3]
                    text = prefix + " / ".join(phrase.upper().split()).replace("'", "’") + "?!"
                    self.assertTrue(self.approving.heard(text))
                    self.assertEqual(waiter.result(3), state == "approved")
                    row = self.rows()[-1]
                    self.assertEqual(set(row), {
                        "id", "tool", "digest", "level", "why", "state", "t", "via", "heard_words",
                    })
                    self.assertEqual((row["state"], row["via"], row["heard_words"]),
                                     (state, "voice", len(phrase.split()) + index % 3))
        self.assertEqual(self.approving.status(), {
            "voice_approvals": len(YES), "voice_denials": len(NO), "popup_answers": 0, "ignored_answers": 0,
        })

    def test_other_utterances_are_not_answers(self):
        card, _ = self.ask()
        self.arm(card)
        for text in ("yes but change the title", "yesterday", "no problem with that plan", "",
                     "Jarvis", "okay yes", "approve that", "yes no"):
            self.assertFalse(self.approving.heard(text), text)
        self.assertEqual([row["state"] for row in self.rows()], ["pending"])

    def test_replacement_digest_expiry_and_single_use(self):
        first, _ = self.ask(turn="first")
        second, _ = self.ask(turn="second")
        self.arm(first)
        self.approving.ask(second)
        self.approving.prompt_finished(first["id"], self.now)
        self.assertFalse(self.approving.heard("yes"))
        self.assertEqual(len(self.broker.pending()), 2)
        self.arm(dict(second, digest="different-digest"))
        self.assertTrue(self.approving.heard("yes"))
        self.assertEqual(len(self.broker.pending()), 2)
        self.now += 119
        self.arm(first)
        self.now += 1
        self.assertTrue(self.approving.heard("yes"))
        self.assertEqual(self.broker.pending(), [])
        self.assertEqual([row["state"] for row in self.rows()], ["pending", "pending", "expired", "expired"])
        for phrase, decision in (("yes", True), ("no", False)):
            card, waiter = self.ask()
            self.arm(card)
            self.assertTrue(self.approving.heard(phrase))
            self.assertEqual(waiter.result(3), decision)
            self.assertFalse(self.approving.heard("yes"))
            self.assertFalse(self.click(card))

    def test_window_starts_at_playback_and_click_routes_outlive_it(self):
        early, waiter = self.ask()
        self.approving.ask(early)
        self.now += 33
        self.assertFalse(self.approving.heard("yes"))
        self.approving.prompt_finished(early["id"], self.now)
        self.now += 14
        self.assertTrue(self.approving.heard("yes"))
        self.assertTrue(waiter.result(3))
        for route in ("popup", "ordinary"):
            late, waiter = self.ask()
            self.arm(late)
            self.now += 16
            self.assertFalse(self.approving.heard("yes"))
            if route == "popup":
                self.assertTrue(self.click(late))
                self.assertEqual(self.rows()[-1]["via"], "popup")
            else:
                self.broker.answer(late["id"], late["digest"], "approve")
                self.assertNotIn("via", self.rows()[-1])
            self.assertTrue(waiter.result(3))

    def test_transcript_grace_requires_an_in_window_onset(self):
        for onset, arrival, approved in ((14, 20, True), (14, 20.01, False), (15, 16, False), (-0.01, 1, False)):
            with self.subTest(onset=onset, arrival=arrival):
                card, waiter = self.ask()
                self.arm(card)
                spoken_at = self.now + onset
                self.now += arrival
                self.assertEqual(self.approving.heard("yes", started_at=spoken_at), approved)
                if not approved:
                    self.assertTrue(self.click(card, "deny"))
                self.assertEqual(waiter.result(3), approved)

    def test_click_only_kinds_and_current_chat_tools(self):
        expected = {"calendar_create", "calendar_update", "calendar_delete",
                    "vault_add", "vault_patch", "vault_archive", "work_start", "work_send", "delegate",
                    "app_open", "folder_open", "link_open"}
        self.assertEqual(set(fastpath._POLICY_KINDS), expected)
        for tool in expected:
            self.assertTrue(self.approving.ask({"id": "fixture", "digest": "fake-digest", "tool": tool}))
        kinds = sorted(policy.NEVER | policy.MONEY | {"credential.inspect", "gate.inspect",
                       "policy.inspect", "money.inspect"}) + [None]
        for kind in kinds:
            with self.subTest(kind=kind), patch.dict(
                fastpath._POLICY_KINDS, {} if kind is None else {"fixture_tool": kind},
            ):
                card, waiter = self.ask("fixture_tool")
                self.assertFalse(self.arm(card))
                self.assertTrue(self.approving.heard("yes"))
                self.assertTrue(self.approving.take_needs_click())
                self.assertTrue(self.approving.heard("yes"))
                self.assertFalse(self.approving.take_needs_click())
                self.assertFalse(waiter.done())
                self.assertTrue(self.approving.heard("no"))
                self.assertFalse(waiter.result(3))
                card, waiter = self.ask("fixture_tool")
                self.assertFalse(self.arm(card))
                self.assertTrue(self.click(card))
                self.assertTrue(waiter.result(3))

    def test_authenticated_click_frames_answer_only_the_current_card(self):
        key = self.root / "fake-pairing"
        key.touch(mode=0o600)
        key.write_bytes(KEY_TEXT)
        session = SimpleNamespace(start=lambda: None, stop=lambda: None, click=self.approving.click)
        link = VoiceLink(lambda **_sinks: session, port=0, key_path=key)
        self.addCleanup(link.close)
        self.assertTrue(link.start())
        self.assertNotIn(link.port, range(8402, 8411))
        with ExitStack() as stack:
            helper = Helper(stack, link.address).authenticate()
            typed, _ = self.ask(turn="typed")
            self.assertFalse(self.approving.heard("yes"))
            card, waiter = self.ask()
            self.arm(card)

            def send(item, decision):
                helper.send(wire.CLICK, {"id": item["id"], "digest": item["digest"], "decision": decision})

            send(dict(card, id="other-id"), "approve")
            send(dict(card, digest="other-digest"), "approve")
            send(typed, "approve")
            send(card, "deny")
            send(card, "approve")
            self.assertFalse(waiter.result(3))
            card, waiter = self.ask()
            self.arm(card)
            send(card, "approve")
            self.assertTrue(waiter.result(3))
            helper.send(wire.PING, b"fixture!")
            self.assertEqual(helper.read(), (wire.PONG, b"fixture!"))
            self.assertEqual(link.clicks, 6)
            self.assertEqual(self.approving.status(), {
                "voice_approvals": 0, "voice_denials": 0, "popup_answers": 2, "ignored_answers": 5,
            })
            self.assertEqual(self.broker.pending(), [typed])
            self.assertEqual([(row["state"], row["via"]) for row in self.rows() if "via" in row],
                             [("denied", "popup"), ("approved", "popup")])
            self.assertFalse(any("heard_words" in row for row in self.rows()))

    def test_guards_and_guide(self):
        tree = ast.parse(Path(approving.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("brain", node.module or "")
            elif isinstance(node, ast.Import):
                self.assertFalse(any("brain" in alias.name for alias in node.names))
        fastpath.register_tools(include_gated=True)
        registry = tools.REGISTRY
        for name, tool in registry.items():
            self.assertNotIn("approv", name)
            self.assertNotIn("approving", tool.func.__module__)
            self.assertIsNot(tool.func, ApprovalBroker.answer)
        guide = (Path(__file__).resolve().parents[2] / "AGENTS.md").read_bytes()
        for pinned in (b"Speech never authorizes an approval.", b"Dictation is not speaker-verified (owner)."):
            self.assertIn(pinned, guide)


class VoiceApprovalFlow(BrokerFixture):
    def setUp(self):
        super().setUp()
        self.factory, self.events, self.audio = Factory(), Recorder(), Rows()
        self.asr, self.voice = Transcriber(), Voice("fixture-voice")
        self.data = self.root / "voice"
        self.enterContext(patch.multiple(config, V3_POLICY_FILE=self.root / "absent-policy",
                                        GOOGLE_TOKEN_PATH=self.root / "absent-calendar"))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root)}, clear=True))
        self.enterContext(patch.multiple(claude_jobs,
                                        resolve_native_cli=lambda: "/example/bin/claude",
                                        cli_meets_floor=lambda *_a, **_k: True))
        self.brain = ClaudeVoiceBrain(self.data, approvals=self.broker, client_factory=self.factory,
                                      clock=self.clock, record_usage=lambda _row: None)
        self.addCleanup(self.brain.close)
        self.turns = VoiceTurns(self.brain, clock=self.clock, on_event=lambda *_a, **_k: None)
        self.addCleanup(self.turns.close)
        self.session = VoiceSession(
            self.turns, transcriber=self.asr, voices=(self.voice, None), names=tuple, clock=self.clock,
            approving=self.approving, detector=Detector(), follow_up_s=0,
            on_event=lambda kind, **fields: self.events(dict(fields, kind=kind)),
            on_audio=lambda id, _pcm, _rate, _first, last: self.audio.add(id) if last else None,
        )
        self.addCleanup(self.session.close)
        self.session.start()
        self.events.wait("state")

    def frames(self, count, raw=RAW, cancelled=CANCELLED):
        for _ in range(count):
            self.now += 0.02
            self.session.feed(raw, cancelled)

    def utter(self, text):
        self.asr.lines.append(text)
        self.frames(8)
        self.frames(25, QUIET, QUIET)

    def request(self, reply_text=None):
        if reply_text is None:
            async def play(client):
                yield frame("message_start")
                yield tool_block(VAULT_ADD[0])
                yield assistant(calls=[VAULT_ADD])
                yield frame("message_stop")
                await client.call(*VAULT_ADD)
                yield result(None)
        else:
            play = script(("", [VAULT_ADD]), text=reply_text)
        self.factory.scripts.append(play)
        self.utter("Jarvis, add the fixture note.")
        card = self.events.wait("asking")
        self.assertEqual(card["expires_in"], 15)
        self.assertTrue(card["voice_waiting"])
        self.audio.wait(lambda ids: card["id"] in ids)
        return card

    def played(self, card):
        at = self.now
        self.session.played(card["id"])
        renewed = self.events.wait("asking", count=2)
        self.assertEqual(self.now, at)
        self.assertEqual((renewed["id"], renewed["expires_in"], renewed["voice_waiting"]),
                         (card["id"], 15, False))
        self.assertGreater(renewed["click_expires_in"], 15)

    def watch_capture_end(self, card):
        processed = Event()
        handle = self.session._handle_hearing

        def observe(kind, capture, fields):
            handle(kind, capture, fields)
            if kind == "speech_end" and capture.context.asking == card["id"]:
                processed.set()

        self.enterContext(patch.object(self.session, "_handle_hearing", observe))
        return processed

    def done(self, card, sentence, *, approved):
        final = self.events.wait("say", count=2)
        self.assertEqual(final["text"], sentence)
        self.assertFalse(self.broker.pending())
        self.assertNotEqual(final["id"], card["id"])
        self.audio.wait(lambda ids: final["id"] in ids)
        self.session.played(final["id"])
        until(lambda: self.session.status()["state"] == "resting")
        results = self.factory.clients[0].results
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2], not approved)
        if not approved:
            self.assertEqual(results[0][1], REFUSAL)
        self.assertEqual((self.data / "vault" / "facts" / "fixture.md").is_file(), approved)
        self.assertEqual([event["text"] for event in self.events.events if event["kind"] == "heard"],
                         ["add the fixture note."])

    def test_real_gated_tool_approved_at_fourteen_seconds(self):
        card = self.request("The fixture note was added.")
        self.assertTrue(card["voice_allowed"])
        self.assertTrue(card["sentence"].endswith("Approve?"))
        self.played(card)
        self.now += 14
        self.utter("yes")
        self.done(card, "The fixture note was added.", approved=True)
        self.assertEqual(self.rows()[-1]["via"], "voice")
        self.assertEqual(self.session.status()["counters"]["voice_approvals"], 1)
        allowed = self.factory.clients[0].options.allowed_tools
        self.assertIn("mcp__jarvis__vault_add", allowed)
        self.assertFalse(any("approv" in name for name in allowed))

    def test_spoken_stop_denies_without_going_to_the_brain(self):
        card = self.request("I did not add the fixture note.")
        self.played(card)
        self.now += ECHO_TAIL_S + 0.01
        self.utter("Hey Jarvis, stop!")
        self.done(card, "I did not add the fixture note.", approved=False)
        self.assertEqual((self.rows()[-1]["state"], self.rows()[-1]["heard_words"]), ("denied", 3))

    def test_no_answer_until_broker_expiry(self):
        card = self.request()
        self.played(card)
        self.now += 120
        self.frames(1, QUIET, QUIET)
        self.done(card, "Nothing was done.", approved=False)
        self.assertEqual(self.rows()[-1]["state"], "expired")

    def test_popup_still_answers_after_sixteen_seconds(self):
        card = self.request("The fixture note was added.")
        self.played(card)
        self.now += 16
        self.utter("yes")
        until(lambda: self.session.status()["counters"]["ignored_answers"] == 1)
        self.assertTrue(self.broker.pending())
        self.session.click(card["id"], card["digest"], "approve")
        self.done(card, "The fixture note was added.", approved=True)
        self.assertEqual(self.rows()[-1]["via"], "popup")

    def test_ordinary_answer_retires_popup_before_a_slow_tool_finishes(self):
        entered, release = Event(), Event()
        add = MemoryTools.memory_add

        def slow_add(memory, **args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("fixture tool was not released")
            return add(memory, **args)

        self.enterContext(patch.object(MemoryTools, "memory_add", slow_add))
        self.addCleanup(release.set)
        card = self.request("The fixture note was added.")
        self.played(card)
        self.broker.answer(card["id"], card["digest"], "approve")
        self.assertTrue(entered.wait(3))
        self.now += 1
        self.frames(1, QUIET, QUIET)
        until(lambda: self.session.status()["state"] == "working")
        self.assertEqual(len([event for event in self.events.events if event["kind"] == "say"]), 1)
        release.set()
        self.done(card, "The fixture note was added.", approved=True)
        self.assertNotIn("via", self.rows()[-1])

    def test_typed_card_never_becomes_a_voice_question(self):
        typed, _ = self.ask(turn="typed")
        with patch.object(self.session, "on_answer", wraps=self.session.on_answer) as hook:
            self.utter("yes")
            self.events.wait("ignored")
            hook.assert_not_called()
        self.assertEqual(self.broker.pending(), [dict(typed, expires_in=120 - (self.now - 100))])
        self.assertFalse(any(event["kind"] in {"asking", "heard"} for event in self.events.events))

    def test_raw_playback_and_speech_during_prompt_do_not_reach_hook(self):
        card = self.request("I did not add the fixture note.")
        with patch.object(self.session, "on_answer", wraps=self.session.on_answer) as hook:
            self.frames(8, RAW, QUIET)
            self.frames(25, QUIET, QUIET)
            self.utter("Jarvis, yes.")
            until(lambda: self.session.status()["counters"]["ignored_answers"] == 1)
            hook.assert_not_called()
            self.assertTrue(self.broker.pending())
        self.assertEqual(len(self.asr.calls.rows), 2)  # Request and actual mic speech, no raw-only decode.
        self.broker.answer(card["id"], card["digest"], "deny")
        self.done(card, "I did not add the fixture note.", approved=False)

    def test_keyed_raw_only_audio_cannot_answer(self):
        card = self.request()
        self.played(card)
        processed = self.watch_capture_end(card)
        self.now += ECHO_TAIL_S + 0.01
        with patch.object(self.session, "on_answer", wraps=self.session.on_answer) as hook:
            self.asr.lines.append("yes")
            self.session.key(True)
            self.frames(8, RAW, QUIET)
            self.session.key(False)
            self.assertTrue(processed.wait(3))
            hook.assert_not_called()
        self.assertEqual(self.asr.calls.rows[-1][0], 0)
        self.assertTrue(self.broker.pending())

    def test_keyed_capture_without_canceller_keeps_its_speech_fallback(self):
        card = self.request("The fixture note was added.")
        self.played(card)
        self.now += ECHO_TAIL_S + 0.01
        self.asr.lines.append("yes")
        self.session.key(True)
        self.frames(8, RAW / 100, None)  # Quieter than the fixture detector's speech threshold.
        self.session.key(False)
        self.done(card, "The fixture note was added.", approved=True)

    def test_keyed_answer_uses_speech_onset_instead_of_key_down(self):
        card = self.request()
        self.played(card)
        processed = self.watch_capture_end(card)
        self.now += 14
        with patch.object(self.session, "on_answer", wraps=self.session.on_answer) as hook:
            self.asr.lines.append("yes")
            self.session.key(True)
            self.frames(8, QUIET, QUIET)
            self.now += 1
            self.frames(8)
            self.session.key(False)
            self.assertTrue(processed.wait(3))
            hook.assert_not_called()
        self.assertTrue(self.broker.pending())

    def test_result_more_than_five_seconds_late_does_not_reach_hook(self):
        card = self.request()
        self.played(card)
        processed = self.watch_capture_end(card)
        release = Event()
        self.addCleanup(release.set)
        self.asr.hold = release
        self.now += 14
        with patch.object(self.session, "on_answer", wraps=self.session.on_answer) as hook:
            self.utter("stop")
            self.asr.calls.wait(lambda rows: len(rows) == 2)
            self.now += 6
            release.set()
            self.assertTrue(processed.wait(3))
            hook.assert_not_called()
        self.assertTrue(self.broker.pending())
        self.assertTrue(self.turns.is_asking(card["id"]))

    def test_keyed_paired_answer_omits_unpaired_frames(self):
        card = self.request("The fixture note was added.")
        self.played(card)
        self.now += ECHO_TAIL_S + 0.01
        self.asr.lines.append("yes")
        self.session.key(True)
        self.frames(8)
        self.frames(8, RAW, None)
        self.session.key(False)
        self.done(card, "The fixture note was added.", approved=True)
        self.assertAlmostEqual(self.asr.calls.rows[-1][0], float(abs(CANCELLED).max()))

    def test_click_only_question_and_fixed_reminder(self):
        self.enterContext(patch.object(approving, "VOICE_NEEDS_CLICK",
                                       approving.VOICE_NEEDS_CLICK | {"vault.update"}))
        card = self.request("The fixture note was added.")
        self.assertFalse(card["voice_allowed"])
        self.assertTrue(card["sentence"].endswith("This one needs a click."))
        self.played(card)
        self.now += ECHO_TAIL_S + 0.01
        self.utter("yes")
        notice = self.events.wait("say", count=2)
        self.assertEqual(notice["text"], "That one needs a click.")
        self.audio.wait(lambda ids: notice["id"] in ids)
        self.session.played(notice["id"])
        self.now += ECHO_TAIL_S + 0.01
        self.utter("yes")
        until(lambda: self.session.status()["counters"]["ignored_answers"] == 2)
        self.assertEqual([event["text"] for event in self.events.events if event["kind"] == "say"],
                         [card["sentence"], notice["text"]])
        self.assertTrue(self.broker.pending())
        self.session.click(card["id"], card["digest"], "approve")
        self.assertEqual(self.events.wait("say", count=3)["text"], "The fixture note was added.")
        self.assertEqual(self.rows()[-1]["via"], "popup")


class PopupLifetime(unittest.TestCase):
    def test_playback_receipt_keeps_a_click_queued_for_the_same_card(self):
        transport = Mock()
        helper = v3voice_main.Helper(
            "127.0.0.1", 0, None, logger=Mock(), devices=FakeDevices(), make_canceller=lambda: None,
            make_client=Mock(return_value=transport), make_key=Mock(), make_popup=Mock(),
        )
        helper._connected = True
        target = {"id": "fixture-card", "digest": "fixture-digest"}
        asking = dict(target, kind="asking", sentence="Apply the fixture change. Approve?",
                      expires_in=15, click_expires_in=120, voice_allowed=True, voice_waiting=True)
        helper._event(asking)
        click = dict(target, decision="approve")
        helper._click(click)
        helper._event(dict(asking, voice_waiting=False, click_expires_in=116))
        helper._send_clicks()
        transport.send.assert_called_once_with(wire.CLICK, click)

    def test_physical_click_outlives_voice_countdown_but_not_broker_card(self):
        for elapsed, decision in ((16, "approve"), (121, None)):
            with self.subTest(elapsed=elapsed), ExitStack() as stack:
                clock = Mock(return_value=100.0)
                stack.enter_context(patch.object(popup, "monotonic", clock))
                received, desktop = [], FakeWin32()
                window = popup.Popup(received.append, win32=desktop)
                stack.callback(window.close)
                state = drawing.state_from_event(drawing.PopupState(), {
                    "kind": "asking", "id": "fixture-card", "digest": "fixture-digest",
                    "sentence": "Apply the fixture change. Approve?", "expires_in": 15,
                    "click_expires_in": 120, "voice_allowed": True, "voice_waiting": False,
                })
                window.set(state, expires_in=15, click_expires_in=120)
                desktop.until("paint")
                desktop.sync()
                clock.return_value += elapsed
                desktop.wake()
                desktop.sync()
                (_, position, scale), painted, _ = window._shown
                self.assertEqual((painted.kind, painted.expires), ("needs_click", 0))
                self.assertEqual(desktop.watch, decision is not None)
                left, top, right, bottom = dict(drawing._layout(painted, scale, 470, ()).buttons)["approve"]
                x, y = position[0] + (left + right) // 2, position[1] + (top + bottom) // 2
                desktop.mouse((0, popup.WM_LBUTTONDOWN, x, y), (0, popup.WM_LBUTTONUP, x, y))
                desktop.sync()
                expected = [] if decision is None else [{
                    "id": "fixture-card", "digest": "fixture-digest", "decision": decision,
                }]
                self.assertEqual(received, expected)


if __name__ == "__main__":
    unittest.main()
