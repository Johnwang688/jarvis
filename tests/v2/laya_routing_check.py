"""Offline voice adapter and benchmark regressions with invented transcripts."""

import tempfile
import unittest
from pathlib import Path

from jarvis.v2.laya_routing import CHOICES, decide
from scripts.bench_laya_voice import read_rows, run_rows, timing


class AdapterTests(unittest.TestCase):
    def test_repeated_and_spoken_sites_are_grounded_once(self):
        for text in ("Visit monarch.orgYo, visit Monarch.ORG.",
                     "Visit monarch.orgmonarch.org", "Go to monarch dot org",
                     "Try monarch . org"):
            with self.subTest(text=text):
                self.assertEqual(
                    decide("assistant", text, lambda _: {"navigate": 0.95, "ask": 0.05}),
                    {"kind": "navigate", "target": "https://monarch.org"},
                )

    def test_invalid_and_multiple_sites_clarify_after_classification(self):
        for text in ("Go to https://monarch.org/path", "Try admin@monarch.org",
                     "Try something.local", "Visit monarch.orgYo, visit moon.gov"):
            with self.subTest(text=text):
                seen = []

                def classify(transcript):
                    seen.append(transcript)
                    return {"navigate": 0.95, "ask": 0.05}

                self.assertEqual(decide("wake", text, classify),
                                 {"kind": "clarify", "target": ""})
                self.assertEqual(seen, [text])

    def test_exact_app_and_window_names(self):
        for apps, expected in (
            (("Paint",), {"kind": "open_app", "target": "Paint"}),
            (("AI Paint", "Paint"), {"kind": "open_app", "target": "AI Paint"}),
            (("AI Paint", "AI Paint"), {"kind": "clarify", "target": ""}),
            (("Pain",), {"kind": "clarify", "target": ""}),
        ):
            with self.subTest(apps=apps):
                self.assertEqual(
                    decide("assistant", "Launch AI   Paint", lambda _: {"open_app": 0.95, "ask": 0.05},
                           apps=apps),
                    expected,
                )
        self.assertEqual(
            decide("assistant", "Either Paint or Calendar",
                   lambda _: {"switch_app": 0.95, "ask": 0.05}, windows=("Paint", "Calendar")),
            {"kind": "clarify", "target": ""},
        )

    def test_search_keeps_speakers_query_and_refuses_missing_or_multiple_queries(self):
        for text, query in (
            ("Uh, look up Why Bees Dance, please.", "Why Bees Dance"),
            ("Search the web for me.", ""),
            ("Search the web for", ""),
            ("Look up for me", ""),
            ("Look up bees and search for bridges", ""),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    decide("assistant", text, lambda _: {"search": 0.95, "ask": 0.05}),
                    {"kind": "search" if query else "clarify", "target": query},
                )

    def test_uncertain_and_malformed_scores(self):
        for scores, expected in (
            ({"ask": 0.64, "navigate": 0.05}, "clarify"),
            ({"ask": 0.65, "navigate": 0.46}, "clarify"),
            ({"ask": 0.65, "navigate": 0.45}, "ask"),
            ({"ask": float("nan"), "navigate": 0.01}, "unavailable"),
            ({"ask": float("inf"), "navigate": 0.01}, "unavailable"),
            ({"ask": True, "navigate": 0.01}, "unavailable"),
            ({"ask": 0.99, "different_label": 0.01}, "unavailable"),
            ({"ask": 0.99}, "unavailable"),
            ([0.99, 0.01], "unavailable"),
        ):
            with self.subTest(scores=scores):
                self.assertEqual(decide("assistant", "Uh, what is this?", lambda _: scores),
                                 {"kind": expected, "target": ""})


class BenchTests(unittest.TestCase):
    def test_synchronized_serial_calls_and_distinct_warm_latency(self):
        events = []
        scores = dict.fromkeys(CHOICES, 0.01)
        scores["ask"] = 0.94

        def predict(text):
            events.append(text)
            return {"answers": {"route": {"probabilities": scores}},
                    "usage": {"truncated": text == "truncated"}}

        rows = [
            dict(id=1, mode="dictation", text="open monarch.org", apps=[], windows=[]),
            dict(id=2, mode="wake", text="Jarvis, type the words  Send it.  ", apps=[], windows=[]),
            dict(id=3, mode="wake", text="Hey, um, Jarvis, why do birds fly?", apps=[], windows=[]),
            dict(id=4, mode="assistant", text="truncated", apps=[], windows=[]),
            dict(id=5, mode="assistant", text="  What makes the wind blow?  ", apps=[], windows=[]),
        ]
        reports = run_rows(rows, predict, lambda: events.append("sync"))
        self.assertEqual(events, [item for row in rows[2:] for item in ("sync", row["text"], "sync")])
        self.assertEqual([row["model_calls"] for row in reports], [0, 0, 1, 1, 1])
        self.assertEqual(reports[0]["target"], rows[0]["text"])
        self.assertEqual(reports[1]["target"], "Send it.  ")
        self.assertNotIn("raw_scores", reports[1])
        self.assertNotIn("inference_ms", reports[1])
        self.assertEqual(reports[3]["kind"], "unavailable")
        self.assertEqual(reports[3]["raw_scores"], scores)
        summary = timing(reports)
        self.assertEqual(summary["first_routing_ms"], reports[2]["latency_ms"])
        self.assertEqual(summary["warm_p95_ms"], reports[4]["latency_ms"])
        self.assertEqual(summary["warm_inference_p95_ms"], reports[4]["inference_ms"])
        self.assertEqual(summary["warm_samples"], 1)

    def test_labelled_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inputs.jsonl"
            path.write_text('{"id":1,"mode":"wake","text":"hi","apps":[],"windows":[],"kind":"ask"}\n')
            with self.assertRaisesRegex(ValueError, "invalid row"):
                read_rows(path)


if __name__ == "__main__":
    unittest.main()
