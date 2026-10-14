"""Free checks for the paid STT comparison probe (tests/stt_compare_probe.py). No network, no key, no spend.

The probe is the one thing in V13 that can cost money, so its guards are pinned here: it refuses
without --yes-spend, it cannot be told to spend past $0.24, it stops before the running estimate
would pass the cap, it never prints the key, and it does not import the module that reads `.env`.

    .venv/bin/python tests/stt_compare_probe_check.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("stt_compare_probe", HERE / "stt_compare_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

KEY = "sk-or-v1-THIS-MUST-NEVER-BE-PRINTED"


def write_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(b"\x01\x00" * int(seconds * 16000))


class FakeEngine:
    def __init__(self, *a, **kw):
        pass

    def transcribe(self, audio, mime="audio/wav", *, timeout):
        return "local text"


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.fixtures = root / "fixtures"
        self.fixtures.mkdir()
        manifest = []
        for v, voice in enumerate(("af_a", "af_b")):
            for s in range(6):
                name = f"kokoro_{voice}_{s:02d}.wav"
                write_wav(self.fixtures / name, 6.0)
                manifest.append({"file": name, "text": f"sentence {s} for voice {v}", "voice": voice,
                                 "engine": "kokoro", "sentence": s, "seconds": 6.0})
        manifest.append({"file": "pocket_x.wav", "text": "pocket", "voice": "x", "engine": "pocket", "sentence": 0, "seconds": 1.0})
        (self.fixtures / "manifest.json").write_text(json.dumps(manifest))
        self.manifest = manifest
        stub = mock.MagicMock()
        stub.Engine = FakeEngine
        patches = [mock.patch.object(probe.common, "FIXTURES", self.fixtures),
                   mock.patch.object(probe.common, "WORK", root / "work"),
                   mock.patch.object(probe, "find_model_dir", return_value=root),
                   mock.patch.object(probe, "load_stt_local", return_value=stub),
                   mock.patch.dict(os.environ, {probe.KEY_ENV: KEY})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, *argv, cloud=None):
        out = io.StringIO()
        calls = []

        def default_cloud(wav, model, key):
            calls.append((model, key))
            return "cloud text", None

        with mock.patch.object(probe, "cloud_transcribe", side_effect=cloud or default_cloud) as spy, \
                contextlib.redirect_stdout(out):
            code = probe.main(list(argv))
        return code, out.getvalue(), spy


class Guards(Harness):
    def test_refuses_without_yes_spend(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            code, out, spy = self.run_main()
            self.assertEqual(code, 2)
            self.assertIn("--yes-spend", out)
            self.assertFalse(spy.called)
            code, out, spy = self.run_main("--clips", "4")
            self.assertEqual((code, spy.called), (2, False))
        self.assertNotIn(KEY, out)

    def test_dry_run_is_free_and_needs_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(probe.KEY_ENV, None)
            code, out, spy = self.run_main("--dry-run", "--clips", "4")
        self.assertEqual(code, 0, out)
        self.assertFalse(spy.called)
        self.assertIn("local done", out)

    def test_no_key_no_run(self):
        os.environ.pop(probe.KEY_ENV)
        code, out, spy = self.run_main("--yes-spend")
        self.assertEqual((code, spy.called), (2, False))
        self.assertIn(probe.KEY_ENV, out)

    def test_the_cap_cannot_be_raised_past_the_approved_amount(self):
        for bad in ("0.25", "1", "0", "-1", "abc"):
            code, out, spy = self.run_main("--yes-spend", "--max-usd", bad)
            self.assertEqual((code, spy.called), (2, False), bad)
        self.assertLessEqual(probe.HARD_CAP_USD, 0.24)
        probe.Meter(0.24)
        with self.assertRaises(ValueError):
            probe.Meter(0.2401)

    def test_stops_before_the_running_estimate_passes_the_cap(self):
        # 6 s clips at $0.60/min = $0.06 each: four fit under $0.24, the fifth must not be sent.
        code, out, spy = self.run_main("--yes-spend", "--clips", "12", "--price-per-min", "0.6")
        self.assertEqual(code, 0, out)
        self.assertEqual(spy.call_count, 4)
        self.assertIn("stopped before the $0.24 cap", out)
        result = json.loads((self.fixtures.parent / "work" / "results" / "stt_compare.json").read_text())
        self.assertLessEqual(result["spent_usd_estimate"], 0.24)
        self.assertEqual(result["comparison"]["clips"], 4, "compared only on the clips the cloud answered")

    def test_a_reported_cost_larger_than_the_estimate_counts(self):
        costs = iter([0.2, 0.2, 0.2])
        code, out, spy = self.run_main("--yes-spend", "--clips", "6", cloud=lambda w, m, k: ("t", next(costs)))
        # The first reported $0.20 plus the next clip's small estimate still fits under $0.24, so a
        # second request goes out; its reported $0.20 takes the total to $0.40 and nothing more is sent.
        self.assertEqual(spy.call_count, 2)

    def test_gives_up_after_consecutive_failures(self):
        def broken(wav, model, key):
            raise RuntimeError(f"connection reset for {key}")

        code, out, spy = self.run_main("--yes-spend", "--clips", "12", cloud=broken)
        self.assertEqual(spy.call_count, probe.MAX_CONSECUTIVE_FAILURES)
        self.assertIn("failures in a row", out)
        self.assertNotIn(KEY, out)
        self.assertIn("no cloud clip completed", out)

    def test_the_key_is_never_printed_and_goes_only_to_the_request(self):
        code, out, spy = self.run_main("--yes-spend", "--clips", "3")
        self.assertEqual(code, 0, out)
        self.assertNotIn(KEY, out)
        self.assertNotIn(KEY, (self.fixtures.parent / "work" / "results" / "stt_compare.json").read_text())
        self.assertTrue(all(call.args[2] == KEY for call in spy.call_args_list))
        self.assertEqual(probe.scrub(f"bad {KEY} here", KEY), "bad <key> here")

    def test_does_not_import_the_module_that_reads_dotenv(self):
        source = (HERE / "stt_compare_probe.py").read_text()
        self.assertNotIn("from jarvis import", source)
        self.assertNotRegex(source, r"^import jarvis", "no direct jarvis import")
        # The product engine is loaded from its file under a stub config with no _load_dotenv.
        saved = {n: sys.modules.get(n) for n in ("jarvis", "jarvis.config", "jarvis.stt_local")}
        for name in saved:
            sys.modules.pop(name, None)
        try:
            spec2 = importlib.util.spec_from_file_location("probe2", HERE / "stt_compare_probe.py")
            fresh = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(fresh)
            loaded = fresh.load_stt_local(Path(self.tmp.name))
            self.assertFalse(hasattr(sys.modules["jarvis.config"], "_load_dotenv"))
            self.assertTrue(hasattr(loaded, "Engine"))
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


class Pure(unittest.TestCase):
    def test_select_clips_is_deterministic_unique_and_kokoro_only(self):
        manifest = [{"file": f"k_{v}_{s}.wav", "text": "t", "voice": f"v{v}", "engine": "kokoro", "sentence": s}
                    for v in range(14) for s in range(24)] + [{"file": "p.wav", "engine": "pocket", "voice": "p", "sentence": 0, "text": "p"}]
        a = probe.select_clips(manifest, 96)
        self.assertEqual(a, probe.select_clips(manifest, 96))
        self.assertEqual(len(a), len({c["file"] for c in a}))
        self.assertTrue(all(c["engine"] == "kokoro" for c in a))
        self.assertGreaterEqual(len(a), 90)
        self.assertEqual(probe.select_clips([], 5), [])

    def test_compare_numbers(self):
        refs = ["one two three four five"] * 4
        same = probe.compare(refs, refs, refs)
        self.assertEqual((same["local_wer_pct"], same["cloud_wer_pct"], same["local_minus_cloud_points"]), (0, 0, 0))
        self.assertTrue(same["local_within_1_point"])
        local = ["one two three four five"] * 3 + ["one two three four"]      # one deletion in 20 words: 5%
        got = probe.compare(refs, local, refs)
        self.assertAlmostEqual(got["local_wer_pct"], 5.0)
        self.assertAlmostEqual(got["local_minus_cloud_points"], 5.0)
        self.assertFalse(got["local_within_1_point"])
        close = probe.compare(["a b c d e f g h i j"] * 10, ["a b c d e f g h i j"] * 9 + ["a b c d e f g h i"], ["a b c d e f g h i j"] * 10)
        self.assertAlmostEqual(close["local_minus_cloud_points"], 1.0)
        self.assertTrue(close["local_within_1_point"], "exactly 1 point is within 1 point")
        better = probe.compare(refs, refs, local)
        self.assertLess(better["local_minus_cloud_points"], 0)
        self.assertTrue(better["local_within_1_point"])
        self.assertEqual(probe.compare(refs, local, refs)["bootstrap95_points"],
                         probe.compare(refs, local, refs)["bootstrap95_points"], "seeded: repeatable")

    def test_meter_and_estimates(self):
        self.assertAlmostEqual(probe.estimate_usd(60, 0.006), 0.006)
        m = probe.Meter(0.24)
        self.assertTrue(m.allow(0.24))
        self.assertFalse(m.allow(0.2401))
        m.charge(0.1)
        self.assertFalse(m.allow(0.15))
        self.assertTrue(m.allow(0.13))
        m.charge(0.01, reported=True)       # a bool is not a cost
        self.assertAlmostEqual(m.spent, 0.11)
        m.charge(0.01, reported="1.0")      # nor a string
        self.assertAlmostEqual(m.spent, 0.12)

    def test_multipart(self):
        body, ctype = probe.multipart({"model": "m/x"}, "audio.wav", "audio/wav", b"RIFFdata")
        boundary = ctype.split("boundary=")[1]
        self.assertIn(b'name="model"\r\n\r\nm/x', body)
        self.assertIn(b'filename="audio.wav"', body)
        self.assertIn(b"RIFFdata", body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))


if __name__ == "__main__":
    unittest.main(verbosity=1)
