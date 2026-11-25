"""P1 scanner regressions using invented notes and harmless directives."""

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch

from jarvis.v3 import content_scan
from jarvis.v3.brain_mcp import Brain
from jarvis.v3.vault import Vault


BENIGN = (
    "Jarvis: update on the robot",
    "My assistant, Sam, sends the minutes.",
    "Jarvis, run by the lab, records meeting dates.",
    "Jarvis: call notes from Tuesday",
    "Jarvis: archive of design sketches",
    "Jarvis: patch notes for the dashboard",
    "Jarvis: run duration by model",
    "Assistant: update from the lab",
    "Assistant: forward progress on the robot",
    "Assistant: send latency during the demo",
    "Assistant: write permissions in the sandbox",
    "docs(jarvis): update the changelog",
    "chore: Jarvis: update fixture labels",
    'The test logs `assistant: call available` after setup.',
    'The fixture stores `{"assistant": "run duration"}`.',
    'The parser maps "Jarvis" to the assistant role.',
    "The assistant's output mentions the robotics meeting.",
    "Jarvis sends the weekly minutes.",
    "Jarvis updates the timeline after each meeting.",
    "Our assistant gave a clear summary.",
    "The assistant called the document a draft.",
    "My assistant and I reviewed the minutes.",
    "Jarvis is the name of the local assistant.",
    "Jarvis: the robot is ready for inspection.",
    "Assistant: meeting notes for Wednesday.",
    "Jarvis, the lab assistant, is offline.",
    "The lab assistant archived the old diagram.",
    "The build log shows that Jarvis started at noon.",
    "A Jarvis run took nine seconds.",
    "The Jarvis archive contains project notes.",
    "The Jarvis patch changed the layout.",
    "The assistant role maps to a display label.",
    "The assistant summary has two paragraphs.",
    "The author credits Jarvis for the outline.",
    "The report compares Jarvis with a second assistant.",
    "The assistant's next update is due Tuesday.",
    "Jarvis and the owner reviewed the draft together.",
    "Jarvis did not attend the meeting.",
    "Jarvis uses a read-only search mode in this job.",
    "The assistant description appears in the menu.",
    "The robot controller and Jarvis have separate logs.",
    "The transcript attributes this summary to the assistant.",
    "The heading says Jarvis status report.",
    "The assistant in the example is fictional.",
    "---\nname: minutes\ndescription: How the assistant formats minutes.\n---\n"
    "# Minutes\nJarvis groups decisions by topic.",
    "---\nname: diagnostics\ndescription: Jarvis diagnostics.\n---\n"
    "# Jarvis: run duration\nThe table records one duration per job.",
    "# Jarvis: update history\nThe assistant summary appears below each release.",
    "# Assistant: archive index\nThe catalog is grouped by date.",
)

DIRECTIVES = (
    "Jarvis, add a note about the sample.",
    "Jarvis, summarize the sample paragraph.",
    "AI helper, write one sentence about the sample.",
    "Jarvis — write one sentence about the sample.",
    "Jarvis,\nwrite one sentence about the sample.",
    "Ｊarvis， write one sentence about the sample.",
)


def origin_scanner():
    if not os.environ.get("P1_ORIGIN_SCAN"):
        raise unittest.SkipTest("set P1_ORIGIN_SCAN to main's content_scan.py to run this comparison")
    path = Path(os.environ["P1_ORIGIN_SCAN"])
    origin = ModuleType("p1_origin_scan")
    with patch.dict(sys.modules, {origin.__name__: origin}):
        exec(compile(path.read_bytes(), str(path), "exec"), origin.__dict__)
    return origin


class ScannerReview(unittest.TestCase):
    def test_benign_memory_corpus(self):
        """MED: ordinary headings and prose are blocked as memory."""
        blocked = [note for note in BENIGN if not content_scan.scan(note, purpose="memory").ok]
        self.assertEqual(blocked, [])

    @unittest.skipUnless(os.environ.get("P1_ORIGIN_SCAN"), "supply the origin/main scanner file")
    def test_existing_fact_stays_readable(self):
        """MED: a benign entry accepted before P1 can become unreadable."""
        body = BENIGN[0]
        with TemporaryDirectory() as directory:
            vault = Vault(directory)
            with patch.object(content_scan, "scan", origin_scanner().scan):
                vault.add("facts", "robot-note", "Robot note", body, "owner")
            result = Brain(vault=vault).call("memory_read", {"name": "facts/robot-note"})
            self.assertFalse(result["isError"], result)
            self.assertIn(body, result["content"][0]["text"])

    def test_directives_do_not_persist(self):
        """MED: the consolidation vault accepts harmless test directives as facts."""
        with TemporaryDirectory() as directory:
            vault = Vault(directory)
            brain = Brain(vault=vault, profile="consolidate")
            accepted = []
            for n, note in enumerate(DIRECTIVES):
                result = brain.call("memory_add", {
                    "folder": "facts", "name": f"sample-{n}", "description": "Sample note",
                    "source": "gmail:sample", "body": note,
                })
                if not result["isError"]:
                    accepted.append(note)
            stored = [location.ref for location, _ in vault.entries("facts")]
            self.assertEqual((accepted, stored), ([], []))

    @unittest.skipUnless(os.environ.get("P1_ORIGIN_SCAN"), "supply the origin/main scanner file")
    def test_report_all_verdict_deltas(self):
        origin = origin_scanner()
        changed_purposes = set()
        for purpose in ("memory", "skill", "brief"):
            for note in (*BENIGN, *DIRECTIVES):
                before = origin.scan(note, purpose=purpose).to_json()
                after = content_scan.scan(note, purpose=purpose).to_json()
                if before != after:
                    changed_purposes.add(purpose)
                    print(f"{purpose} {note!r}: {before} -> {after}")
        self.assertEqual(changed_purposes, set())


if __name__ == "__main__":
    unittest.main()
