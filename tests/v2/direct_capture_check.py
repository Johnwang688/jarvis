"""B-DIRECT-CAPTURE (BR-4/M6): the owner switch, the probe record and the
URL-only Direct capture reference. No Chrome, no provider, no pixels: the
real Windows app-mode prompt is the owner's probe, not this suite.

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/direct_capture_check.py
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v2.browser import direct_probe as D
from jarvis.v2.browser.captures import Captures, DIRECT_OFF
from jarvis.v2.browser.settings import BrowserSettings, DEFAULTS


def facts(frame=(480, 344), rect=(320, 229), dpr=1.5, viewport=(1600, 1000), **changes):
    """A passing probe report, as the installed HUD would post it: samples at
    the daemon's own positions, coloured as its own pattern."""
    f = None if frame is None else {"width": frame[0], "height": frame[1]}
    value = {
        "display_mode": "standalone", "chrome": 155,
        "apis": {"get_display_media": True, "crop_target": True, "image_capture": True},
        "prompt": "accepted", "error": "",
        "track": {"display_surface": "browser", "width": 2400, "height": 1500, "frame_rate": 5, "cursor": "never"},
        "crop": {"applied": True, "error": ""},
        "frame": f, "viewport": {"width": viewport[0], "height": viewport[1]},
        "rect": {"width": rect[0], "height": rect[1]}, "dpr": dpr,
        "samples": [] if f is None else [{"x": x, "y": y, "got": list(D.PATTERN[name]) + [255]}
                                         for x, y, name in D.sample_positions(f)],
        "tracks_live_after": 0,
        "timing_ms": {"crop_target": 1, "prompt": 2100.5, "crop": 12, "frame": 40, "total": 2200},
    }
    value.update(changes)
    return value


class Switch(unittest.TestCase):
    def test_default_off_and_fail_closed(self):
        self.assertIs(DEFAULTS["direct_capture"], False)
        self.assertIs(BrowserSettings().value("direct_capture"), False)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "browser.json")
            for raw in ('{"direct_capture": "yes"}', "not json", '{"direct_capture": 1}'):
                path.write_text(raw)
                with self.subTest(raw=raw):
                    self.assertIs(BrowserSettings(path).value("direct_capture"), False)
            path.write_text('{"direct_capture": true}')
            self.assertIs(BrowserSettings(path).value("direct_capture"), True)


class Probe(unittest.TestCase):
    def test_a_good_report_passes_and_is_saved_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "sub", "probe.json")
            probe = D.DirectProbe(path, clock=lambda: 0)
            self.assertFalse(probe.passed())
            self.assertIsNone(probe.status())
            out = probe.record(facts())
            self.assertEqual(out, {"passed": True, "reason": "", "at": "1970-01-01T00:00:00Z", "saved": True})
            self.assertTrue(probe.passed())
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["version"], 2)
            self.assertTrue(saved["passed"])
            self.assertEqual(saved["scale"], 1.5)
            self.assertEqual(saved["facts"]["frame"], {"width": 480, "height": 344})
            self.assertNotIn("data_b64", path.read_text())

    def test_verdict_is_recomputed_and_names_the_first_failure(self):
        cases = {
            "tracks_left": facts(tracks_live_after=1),
            "not_app_mode": facts(display_mode="browser"),
            "api_missing": facts(apis={"get_display_media": True, "crop_target": False, "image_capture": True}),
            "cancelled": facts(prompt="cancelled", error="NotAllowedError", track=None, frame=None, samples=[]),
            "prompt_error": facts(prompt="error", error="InvalidStateError", track=None),
            "not_this_tab": facts(track={**facts()["track"], "display_surface": "monitor"}),
            "not_cropped": facts(crop={"applied": False, "error": "NotSupportedError"}),
            "wrong_size": facts(frame=(1920, 1080)),
            "wrong_rect": facts(rect=(320, 200)),
            "not_cropped ": facts(frame=(2400, 1500)),
            "ambiguous": facts(rect=(700, 500), viewport=(1400, 1000), dpr=2, frame=(1400, 1000)),
        }
        pixels = facts()
        pixels["samples"][4]["got"] = [0, 0, 0, 255]
        cases["wrong_pixels"] = pixels
        few = facts()
        few["samples"] = few["samples"][:8]
        cases["wrong_pixels "] = few
        moved = facts()
        moved["samples"][0]["x"] += 1
        cases["wrong_pixels  "] = moved
        # Halfway between two pattern colours is not clearly either.
        mid = facts()
        mid["samples"][0]["got"] = [130, 110, 50, 255]
        cases["wrong_pixels   "] = mid
        for reason, value in cases.items():
            probe = D.DirectProbe()
            with self.subTest(reason=reason):
                out = probe.record(value)
                self.assertEqual(out["reason"], reason.strip())
                self.assertFalse(out["passed"])
                self.assertFalse(probe.passed())
                self.assertFalse(out["saved"])

    def test_any_uniform_scale_from_css_to_device_pixels(self):
        ok = lambda **kw: D.verdict(D.normalize(facts(**kw)))
        # Chrome delivered CSS pixels under a 1.5 DPR (the review's rerun).
        self.assertEqual(ok(frame=(320, 229)), "")
        # A large pane at 4K, downscaled by Chrome below device pixels.
        self.assertEqual(ok(rect=(1120, 800), dpr=2, viewport=(1920, 1080), frame=(1600, 1143)), "")
        # Rounding: about a pixel on the short edge, two on the long.
        self.assertEqual(ok(frame=(480, 345)), "")
        self.assertEqual(ok(frame=(480, 346)), "wrong_size")
        self.assertEqual(ok(frame=(100, 71)), "wrong_size")                      # below 0.5
        self.assertEqual(ok(dpr=1, frame=(640, 458)), "wrong_size")              # above the DPR
        self.assertEqual(ok(frame=(2400, 1500)), "not_cropped")                  # the whole window
        self.assertEqual(ok(frame=(1600, 1000)), "not_cropped")
        self.assertEqual(D.size_verdict({"width": 320, "height": 229}, {"width": 320, "height": 229}, 1.5,
                                        {"width": 1600, "height": 1000}), ("", 1.0))

    def test_the_report_records_the_scale_that_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "probe.json")
            D.DirectProbe(path).record(facts(frame=(320, 229)))
            self.assertEqual(json.loads(path.read_text())["scale"], 1.0)

    def test_expected_colours_and_positions_are_the_daemons(self):
        forged = facts()
        for s in forged["samples"]:
            s["got"] = [0, 0, 0, 255]
            s["want"] = [0, 0, 0]
        with self.assertRaises(D.ProbeRefused):
            D.DirectProbe().record(forged)
        for s in forged["samples"]:
            del s["want"]
        self.assertEqual(D.DirectProbe().record(forged)["reason"], "wrong_pixels")
        with self.assertRaises(D.ProbeRefused):
            D.DirectProbe().record({**facts(), "expected": {"width": 480, "height": 344}})

    def test_a_later_failure_withdraws_the_pass(self):
        probe = D.DirectProbe()
        probe.record(facts())
        probe.record(facts(prompt="cancelled", track=None, frame=None))
        self.assertFalse(probe.passed())

    def test_every_malformed_field_is_refused(self):
        bad = []
        for key in facts():
            v = facts(); del v[key]; bad.append(v)
        bad += [facts(extra=1), facts(display_mode="app"), facts(error="secret page text"),
                facts(chrome="155"), facts(dpr=float("nan")), facts(dpr=True),
                facts(samples=[{"x": 1, "y": 1, "got": [1, 2, 3]}]),
                facts(samples=[{"x": 1, "y": 1, "got": [1, 2, 3, 256]}]),
                facts(samples=facts()["samples"] * 2), facts(frame=(-1, 2)),
                facts(timing_ms={"other": 1}), facts(track={"display_surface": "browser"}),
                facts(tracks_live_after=True), "x", None, []]
        for value in bad:
            probe = D.DirectProbe()
            with self.subTest(value=str(value)[:60]), self.assertRaises(D.ProbeRefused) as caught:
                probe.record(value)
            self.assertEqual(str(caught.exception), D.REFUSED)
            self.assertIsNone(probe.status())

    def test_a_planted_report_file_is_never_read_as_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "probe.json")
            path.write_text(json.dumps({"version": 1, "passed": True, "reason": "", "facts": facts()}))
            probe = D.DirectProbe(path)
            self.assertFalse(probe.passed())
            self.assertIsNone(probe.status())


class Routes(unittest.TestCase):
    def setUp(self):
        from tests.v2.hud_backend_check import Backend
        from tests.v2.browser_panes_check import W, L, report
        f = Backend("test_attachment_limits_scrubbing_and_scope")
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.f, self.W, self.L = f, W, L
        f.daemon.browser_captures = Captures(values=lambda: [])
        registry = f.daemon.browser_panes
        registry.window_started(W)
        self.receipt = registry.report(report())
        view = self.receipt["panes"][0]["tabs"][0]["view_id"]
        self.body = {"window": W, "pane": "4", "local_tab": L, "view": view}

    def test_probe_routes_are_owner_only_and_gate_the_switch(self):
        f = self.f
        for port, headers in ((None, None), (f.daemon.face_port, None),
                              (f.daemon.face_port, {"Origin": "http://foreign.test"})):
            f.request("POST", "/browser/direct-capture/probe", facts(), 403, port=port, headers=headers)
            f.request("PATCH", "/browser/settings", {"direct_capture": True}, 403, port=port, headers=headers)
        self.assertEqual(f.owner("GET", "/browser/direct-capture/probe"), {"probe": None})
        f.owner("PATCH", "/browser/settings", {"direct_capture": True}, 409)
        self.assertFalse(f.daemon.browser_settings.value("direct_capture"))
        f.owner("POST", "/browser/direct-capture/probe", {**facts(), "extra": 1}, 400)
        self.assertEqual(f.owner("POST", "/browser/direct-capture/probe", facts(display_mode="browser"))["reason"],
                         "not_app_mode")
        f.owner("PATCH", "/browser/settings", {"direct_capture": True}, 409)
        self.assertTrue(f.owner("POST", "/browser/direct-capture/probe", facts())["passed"])
        self.assertTrue(f.owner("GET", "/browser/direct-capture/probe")["probe"]["passed"])
        self.assertTrue(f.owner("PATCH", "/browser/settings", {"direct_capture": True})["direct_capture"])
        # Off always works, with or without a pass.
        f.owner("POST", "/browser/direct-capture/probe", facts(prompt="cancelled", track=None, frame=None))
        self.assertFalse(f.owner("PATCH", "/browser/settings", {"direct_capture": False})["direct_capture"])

    def test_reference_needs_the_switch_the_exact_direct_binding_and_no_card(self):
        f = self.f
        for port, headers in ((None, None), (f.daemon.face_port, None),
                              (f.daemon.face_port, {"Origin": "http://foreign.test"})):
            f.request("POST", "/browser/direct-capture", self.body, 403, port=port, headers=headers)
        refused = f.owner("POST", "/browser/direct-capture", self.body, 409)
        self.assertIn(DIRECT_OFF, json.dumps(refused))
        self.assertEqual(f.daemon.browser_captures._rows, {})
        f.daemon.browser_settings.update(direct_capture=True)
        f.owner("POST", "/browser/direct-capture", {**self.body, "extra": "1"}, 400)
        f.owner("POST", "/browser/direct-capture", {**self.body, "view": "v_" + "0" * 24}, 409)
        f.owner("POST", "/browser/direct-capture", {**self.body, "pane": "13"}, 409)
        with patch.object(f.daemon.browser, "input_held", return_value=True):
            f.owner("POST", "/browser/direct-capture", self.body, 409)
        self.assertEqual(f.daemon.browser_captures._rows, {})
        out = f.owner("POST", "/browser/direct-capture", self.body)
        self.assertEqual(set(out), {"capture_id", "url", "title", "text_chars", "whole", "notes"})
        self.assertEqual((out["url"], out["title"], out["text_chars"], out["whole"]),
                         ("http://localhost:23456/secret", "", 0, False))
        self.assertIn("no credential check", out["notes"][0])
        fenced = f.daemon.browser_captures.resolve(out["capture_id"], owner=True)
        self.assertIn("URL: http://localhost:23456/secret", fenced)
        self.assertEqual(f.daemon.browser_captures.resolve(out["capture_id"]), "[page text unavailable: only an owner HUD send can resolve this capture]")

    def test_a_remote_binding_or_a_covered_window_gets_no_direct_reference(self):
        from tests.v2.browser_panes_check import report
        f = self.f
        f.daemon.browser_settings.update(direct_capture=True)
        f.daemon.browser_panes.report(report(seq=2, covered=True))
        f.owner("POST", "/browser/direct-capture", self.body, 409)
        self.assertEqual(f.daemon.browser_captures._rows, {})

    def test_a_credential_in_the_address_is_withheld_as_for_remote(self):
        from tests.v2.browser_panes_check import report
        f = self.f
        f.daemon.browser_settings.update(direct_capture=True)
        body = report(seq=2)
        body["panes"][0]["tabs"][0]["url"] = "http://localhost:23456/?k=TOKEN=abcdef0123456789ABCDEFfedcba987654"
        receipt = f.daemon.browser_panes.report(body)
        view = receipt["panes"][0]["tabs"][0]["view_id"]
        out = f.owner("POST", "/browser/direct-capture", {**self.body, "view": view})
        self.assertEqual(out["url"], "[address withheld]")
        self.assertNotIn("abcdef0123456789", json.dumps(out))
        self.assertNotIn("abcdef0123456789", f.daemon.browser_captures.resolve(out["capture_id"], owner=True))


if __name__ == "__main__":
    unittest.main()
