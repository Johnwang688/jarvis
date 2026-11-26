"""An opening clause must obey the existing voice turn holdback."""

import unittest

from jarvis.v3.voice.turns import VoiceTurns


CLAIM = "The fixture file has been saved successfully, and the updated copy is ready to use."
NEXT_FRAGMENT = " The rest"


class ScriptedBrain:
    def __init__(self):
        self.approvals = []

    def subscribe(self, listener):
        self.listener = listener
        return lambda: None

    def send(self, prompt, *, input_id, **_options):
        self.emit("user", input_id=input_id)

    def emit(self, kind, **fields):
        self.listener({"kind": kind, "turn": "fixture-turn", **fields})

    def pending_approvals(self, turn):
        return self.approvals


class ClauseHoldbackCheck(unittest.TestCase):
    def capture(self):
        brain = ScriptedBrain()
        events = []
        voice = VoiceTurns(brain, on_event=lambda kind, **fields: events.append((kind, fields)))
        self.addCleanup(voice.close)
        voice.heard("Save the fixture file.")
        brain.emit("delta", text=CLAIM)
        brain.emit("delta", text=NEXT_FRAGMENT)
        return brain, voice, events

    @staticmethod
    def spoken(events):
        return [fields["text"] for kind, fields in events if kind == "say"]

    def test_provisional_success_claim_is_withheld_before_tool_and_approval(self):
        brain, _, events = self.capture()
        brain.emit("step_end", has_tools=True, text=CLAIM + NEXT_FRAGMENT)
        brain.emit("tool_start")
        brain.approvals.append({
            "id": "fixture-card", "digest": "fixture-digest", "tool": "fixture_write",
            "card": {"title": "Save the fixture file", "rows": []},
        })
        brain.emit("approval_pending")
        self.assertIn("asking", [kind for kind, _ in events])
        self.assertEqual(self.spoken(events), [])

    def test_stop_discards_the_entire_unconfirmed_reply(self):
        brain, voice, events = self.capture()
        voice.heard("Jarvis, stop talking.")
        brain.emit("step_end", has_tools=False, text=CLAIM + NEXT_FRAGMENT)
        brain.emit("reply", text=CLAIM + NEXT_FRAGMENT)
        self.assertIn("speech_stop", [kind for kind, _ in events])
        self.assertEqual(self.spoken(events), [])


if __name__ == "__main__":
    unittest.main()
