"""Held-out authored speech. No desktop, pairing key, network or model download."""
from statistics import median
from time import perf_counter_ns
import unittest

from jarvis.v2.voice_intent import route


# (source, transcript, expected kind, target, optional output text). Targets
# include names unseen in training, dictated questions, quoted commands and
# near-miss actions an output-only accuracy count could otherwise conceal.
CASES = [
    ("wake", "Jarvis, open Chrome.", "open_app", "Chrome"),
    ("wake", "Open Google Chrome", "open_app", "Google Chrome"),
    ("hotkey", "Would you please launch Blender?", "open_app", "Blender"),
    ("hotkey", "Jarvis, Would you please launch Blender?", "open_app", "Blender"),
    ("wake", "Could you bring up the Firefox Developer Edition app?", "open_app", "Firefox Developer Edition"),
    ("wake", "Jarvis! Fire up Audacity, please.", "open_app", "Audacity"),
    ("hotkey", "Open Visual Studio Code.", "open_app", "Visual Studio Code"),
    ("hotkey", "Jarvis, Open Visual Studio Code.", "open_app", "Visual Studio Code"),
    ("wake", "Please switch over to Microsoft Teams.", "switch_app", "Microsoft Teams"),
    ("wake", "bring Krita to the front", "switch_app", "Krita"),
    ("hotkey", "Focus the Steam window.", "dictate", ""),
    ("hotkey", "Jarvis, Focus the Steam window.", "switch_app", "Steam"),
    ("wake", "Return to the Zoom application", "switch_app", "Zoom"),
    ("wake", "Could you give Inkscape focus?", "switch_app", "Inkscape"),
    ("wake", "Search for why bridges sway.", "search", "why bridges sway"),
    ("hotkey", "Google what is a binary tree", "dictate", ""),
    ("hotkey", "Jarvis, Google what is a binary tree", "search", "what is a binary tree"),
    ("wake", "Look up octopus camouflage", "search", "octopus camouflage"),
    ("wake", "please browse the web for tea varieties", "search", "tea varieties"),
    ("wake", "Look online for examples of open source software", "search", "examples of open source software"),
    ("wake", "Read the highlighted paragraph aloud.", "read_selection", ""),
    ("wake", "please say the selected words out loud", "read_selection", ""),
    ("hotkey", "Could you read back what I have selected?", "dictate", ""),
    ("hotkey", "Jarvis, Could you read back what I have selected?", "read_selection", ""),
    ("wake", "Read my current selection to me.", "read_selection", ""),
    ("wake", "Jarvis how do I open Chrome?", "ask", ""),
    ("wake", "What is the difference between opening and launching Word?", "ask", ""),
    ("wake", "If I say open Chrome, what happens?", "ask", ""),
    ("wake", "Don't open Chrome.", "ask", ""),
    ("wake", "Open Chrome when I get home.", "ask", ""),
    ("wake", "Open Chrome and then find my calendar.", "ask", ""),
    ("wake", "Open my cabinet", "ask", ""),
    ("wake", "Open https://example.com.", "ask", ""),
    ("wake", "What does 'read the selection' mean?", "ask", ""),
    ("wake", "Read me a bedtime story.", "ask", ""),
    ("wake", "Can I google that?", "ask", ""),
    ("wake", "What's happening?", "ask", ""),
    ("wake", "Set this aside for later.", "ask", ""),
    ("wake", "How would you prepare a report for class?", "ask", ""),
    ("wake", "Watch the unit tests and let me know when they pass", "ask", "task"),
    ("wake", "Draft a short letter to the school board.", "ask", "task"),
    ("wake", "Prepare a spreadsheet comparing subscription prices.", "ask", "task"),
    ("hotkey", "Jarvis, review my branch and summarize the changes.", "ask", "task"),
    ("wake", "Implement a fix for those failing tests", "ask", "task"),
    ("wake", "Keep an eye on the backup until it finishes.", "ask", "task"),
    ("hotkey", "Open Google Chrome for me.", "open_app", "Google Chrome"),
    ("hotkey", "open google chrome for me", "open_app", "google chrome"),
    ("hotkey", "Could you open Google Chrome for me?", "open_app", "Google Chrome"),
    ("hotkey", "Open Chrome, please", "open_app", "Chrome"),
    ("hotkey", "Please open the Google Chrome app for me.", "open_app", "Google Chrome"),
    ("hotkey", "Please launch Notepad++ for me, please.", "open_app", "Notepad++"),
    ("hotkey", "Type open Chrome", "dictate", "literal", "open Chrome"),
    ("hotkey", "How do I open Google Chrome?", "dictate", ""),
    ("hotkey", "Open Chrome was what she said.", "dictate", ""),
    ("hotkey", "Open Chrome and send a message.", "dictate", ""),
    ("hotkey", "Open Chrome --incognito", "dictate", ""),
    ("hotkey", "Open Chrome search for the weather", "dictate", ""),
    ("hotkey", '"Open Google Chrome for me."', "dictate", "literal"),
    ("hotkey", "Open Chrome", "open_app", "Chrome"),
    ("hotkey", "Could you explain the answer to me?", "dictate", ""),
    ("hotkey", "Please make an appointment with my tutor", "dictate", ""),
    ("hotkey", "Please search for cloud gardens", "dictate", ""),
    ("hotkey", "She said Jarvis open Chrome", "dictate", ""),
    ("hotkey", "Jarvis's shortcut opens the browser", "dictate", ""),
    ("hotkey", "How do I open Chrome?", "dictate", ""),
    ("hotkey", "Open source contributions take time.", "dictate", ""),
    ("hotkey", "The search for our keys continues.", "dictate", ""),
    ("hotkey", "Read receipts make me nervous.", "dictate", ""),
    ("hotkey", "I told him to switch to Chrome.", "dictate", ""),
    ("hotkey", "The README says open Chrome.", "dictate", ""),
    ("hotkey", "Why did nobody answer?", "dictate", ""),
    ("hotkey", "New line", "dictate", ""),
    ("hotkey", "Message complete, send it", "dictate", ""),
    ("hotkey", "Scratch that", "dictate", ""),
    ("hotkey", "Type safety matters.", "dictate", ""),
    ("hotkey", "Type the words open Chrome", "dictate", "literal", "open Chrome"),
    ("hotkey", "Insert the phrase: send it", "dictate", "literal", "send it"),
    ("hotkey", "Type “scratch that”.", "dictate", "literal", "scratch that"),
    ("hotkey", "Dictate the sentence: how do I open Chrome?", "dictate", "literal", "how do I open Chrome?"),
    ("hotkey", "Look up from the page", "dictate", ""),
    ("hotkey", "Return to sender", "dictate", ""),
    ("wake", "I’d like you to open Chrome", "open_app", "Chrome"),
    ("hotkey", "Can you switch me back to Teams?", "dictate", ""),
    ("hotkey", "Jarvis, Can you switch me back to Teams?", "switch_app", "Teams"),
    ("wake", "Let's open Chrome", "open_app", "Chrome"),
    ("wake", "I'd like to open Chrome please", "open_app", "Chrome"),
    ("hotkey", "I'd like to open Chrome please", "dictate", ""),
    ("hotkey", "What if we send it?", "dictate", "literal"),
    ("hotkey", "Don't send it", "dictate", "literal"),
    ("hotkey", "We should not send it.", "dictate", "literal"),
    ("hotkey", "I wonder if she'll send it", "dictate", "literal"),
    ("hotkey", "Open enrollment begins on Monday", "dictate", ""),
    ("hotkey", "Google was founded in California", "dictate", ""),
    ("hotkey", "Google announced a new phone", "dictate", ""),
    ("hotkey", "Google Chrome keeps crashing", "dictate", ""),
    ("hotkey", "Search for a job is hard", "dictate", ""),
    ("hotkey", "Open Chrome for me", "open_app", "Chrome"),
    ("hotkey", "Jarvis, Open Chrome for me", "open_app", "Chrome"),
    ("hotkey", "Open Chrome for developers is the title", "dictate", ""),
    ("hotkey", "Focus groups help users", "dictate", ""),
    ("hotkey", "Launch day went well", "dictate", ""),
    ("hotkey", "Type two diabetes is common", "dictate", ""),
    ("wake", "Type the words new line", "dictate", "literal", "new line"),
    ("hotkey", 'I heard her say "send it"', "dictate", "literal"),
    ("hotkey", 'The first option is “send it”', "dictate", "literal"),
    ("hotkey", "She said send it", "dictate", "literal"),
    ("hotkey", "“new line”", "dictate", "literal"),
    ("hotkey", "The phrase was scratch that", "dictate", "literal"),
]


class LocalIntent(unittest.TestCase):
    def test_authored_holdout(self):
        for source, words, kind, target, *payload in CASES:
            with self.subTest(source=source, words=words):
                self.assertEqual(route(words, source), dict(kind=kind, text=payload[0] if payload else words,
                                                           target=target))

    def test_training_and_evaluation_do_not_share_utterances(self):
        from jarvis.v2.voice_intent import _TRAIN
        clauses = {x.casefold().strip(".!? ") for group in _TRAIN.values() for x in group}
        self.assertFalse(clauses & {x[1].casefold().strip(".!? ") for x in CASES})

    def test_oversized_or_empty_arguments_cannot_be_actions(self):
        for words in ("", "Jarvis", "search for " + "r" * 90, "open", "focus", "give focus"):
            self.assertEqual(route(words, "wake")["kind"], "ask")

    def test_router_is_self_protected(self):
        from jarvis.tools.files import SELF_PROTECTED
        self.assertIn("jarvis/v2/voice_intent.py", SELF_PROTECTED)


if __name__ == "__main__":
    samples = []
    for _ in range(20):
        for source, text, *_ in CASES:
            start = perf_counter_ns()
            route(text, source)
            samples.append((perf_counter_ns() - start) / 1e6)
    samples.sort()
    print(f"{len(CASES)} held-out utterances; median {median(samples):.3f} ms; p95 {samples[int(.95*len(samples))]:.3f} ms")
    unittest.main()
