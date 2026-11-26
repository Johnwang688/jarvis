from threading import Event
import unittest

from jarvis.v3.voice.hearing import END_SILENCE_MS, FRAME_MS
from jarvis.v3.voice.session import VoiceSession
from jarvis.v3.voice.turns import VoiceTurns
from voice_session_check import Brain, CANCELLED, Detector, QUIET, RAW, Rows, Transcriber, Voice


class Fixture:
    def __init__(self, test, *, answer=None):
        self.now = 100.0
        self.events, self.audio, self.actions = Rows(), Rows(), Rows()
        self.brain, self.asr, self.voice = Brain(), Transcriber(), Voice("fixture")
        self.turns = VoiceTurns(self.brain, clock=lambda: self.now, on_event=lambda *_a, **_k: None)
        test.addCleanup(self.turns.close)
        self.session = VoiceSession(
            self.turns, transcriber=self.asr, voices=(self.voice, None), names=tuple,
            on_event=lambda kind, **fields: self.events.add((kind, fields)),
            on_audio=lambda *args: self.audio.add(args[0]), on_answer=answer,
            clock=lambda: self.now, detector=Detector(), follow_up_s=8, follow_level_db=-6,
        )
        test.addCleanup(self.session.close)
        handle = self.session._handle

        def observe(action, *args):
            handle(action, *args)
            self.actions.add(args[0] if action == "hearing" else action)

        self.session._handle = observe
        self.session.start()
        self.actions.wait(lambda rows: "transcriber" in rows)

    def frames(self, count, raw=RAW, cancelled=CANCELLED):
        for _ in range(count):
            self.now += FRAME_MS / 1000
            self.session.feed(raw, cancelled)

    def wait_action(self, action, previous):
        self.actions.wait(lambda rows: rows.count(action) > previous)

    def fence(self):
        previous = self.actions.rows.count("review_fence")
        self.session._post("review_fence")
        self.wait_action("review_fence", previous)

    def utter(self, text):
        previous = self.actions.rows.count("speech_end")
        self.asr.lines.append(text)
        self.frames(8)
        self.end()
        self.wait_action("speech_end", previous)

    def end(self):
        self.frames(END_SILENCE_MS // FRAME_MS, QUIET, QUIET)

    def finish_reply(self, *, error=False):
        delivered = self.actions.rows.count("delivered")
        old_ids = set(self.audio.rows)
        self.brain.emit("error" if error else "reply", text="Here is a fixture answer.")
        self.audio.wait(lambda rows: bool(set(rows) - old_ids))
        sentence = self.audio.rows[-1]
        self.wait_action("delivered", delivered)
        self.played(sentence)
        return sentence

    def played(self, sentence):
        previous = self.actions.rows.count("played")
        self.session.played(sentence)
        self.wait_action("played", previous)
        self.fence()

    def wait_event(self, kind, **fields):
        self.events.wait(lambda rows: (kind, fields) in rows)

    def ask(self):
        self.brain.pending = [{
            "id": "fake-card", "digest": "fake-digest", "tool": "vault_add",
            "card": {"title": "Write a fixture note", "rows": []},
        }]
        self.brain.emit("approval_pending")
        self.audio.wait(bool)


class AdmissionReview(unittest.TestCase):
    def test_contradicted_wake_partial_does_not_admit_final(self):
        f = Fixture(self)
        room = "Private fixture discussion"
        f.asr.lines.extend(["Jarvis", room, room])
        f.frames(25)
        f.wait_action("partial", 0)
        f.frames(25)
        f.wait_action("partial", 1)
        f.end()
        f.wait_action("speech_end", 0)
        for channel, rows in (("brain", f.brain.inputs.rows), ("events", f.events.rows),
                              ("trace", f.turns.trace())):
            with self.subTest(channel=channel):
                self.assertNotIn(room, str(rows))

    def test_room_yes_does_not_reach_hook_during_prompt_playback(self):
        answers = []
        f = Fixture(self, answer=lambda text: answers.append(text) or True)
        f.utter("Jarvis, check the fixture.")
        f.ask()
        f.utter("yes")
        self.assertEqual(answers, [])

    def test_approval_ending_during_capture_keeps_addressed_request(self):
        f = Fixture(self, answer=lambda _text: False)
        f.utter("Jarvis, check the fixture.")
        f.ask()
        previous = f.actions.rows.count("speech_end")
        f.asr.lines.append("Jarvis, open the fixture folder.")
        f.frames(8)
        f.brain.pending.clear()
        f.brain.emit("tool_end", name="vault_add", outcome="ok", changes_state=True)
        f.wait_event("flush", n=1)
        f.end()
        f.wait_action("speech_end", previous)
        self.assertEqual(f.brain.inputs.rows, ["check the fixture.", "open the fixture folder."])

    def test_unaimed_follow_up_cannot_keep_microphone_open(self):
        f = Fixture(self)
        f.utter("Jarvis, check the fixture.")
        f.finish_reply()
        f.wait_event("state", state="listening")
        f.now += 7
        f.utter("Tell me more about the fixture.")
        self.assertEqual(len(f.brain.inputs.rows), 2)
        f.finish_reply()
        f.now += 2
        f.utter("Private fixture discussion")
        self.assertEqual(len(f.brain.inputs.rows), 2)

    def test_error_sentence_does_not_open_follow_up(self):
        f = Fixture(self)
        f.utter("Jarvis, check the fixture.")
        f.finish_reply(error=True)
        f.utter("Private fixture discussion")
        self.assertEqual(f.brain.inputs.rows, ["check the fixture."])

    def test_wake_only_expires_even_when_decode_lags(self):
        f = Fixture(self)
        release = f.asr.hold = Event()
        self.addCleanup(release.set)
        f.asr.lines.append("Jarvis")
        f.frames(8)
        f.end()
        f.asr.calls.wait(bool)
        f.now += 7
        f.asr.lines.append("Private fixture discussion")
        f.frames(8)
        f.end()
        release.set()
        f.wait_action("speech_end", 1)
        self.assertEqual(f.brain.inputs.rows, [])


if __name__ == "__main__":
    unittest.main()
