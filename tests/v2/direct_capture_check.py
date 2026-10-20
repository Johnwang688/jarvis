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


def facts(**changes):
    """A passing probe report, as the installed HUD would post it."""
    want = [[220, 40, 40], [40, 180, 60], [40, 80, 220], [240, 200, 40], [250, 250, 250],
            [220, 40, 40], [40, 180, 60], [40, 80, 220], [240, 200, 40]]
    value = {
        "display_mode": "standalone", "chrome": 155,
        "apis": {"get_display_media": True, "crop_target": True, "image_capture": True},
        "prompt": "accepted", "error": "",
        "track": {"display_surface": "browser", "width": 480, "height": 300, "frame_rate": 30, "cursor": "never"},
        "crop": {"applied": True, "error": ""},
        "frame": {"width": 480, "height": 300}, "expected": {"width": 480, "height": 300},
        "rect": {"width": 320, "height": 200}, "dpr": 1.5,
        "samples": [{"x": 10 * i, "y": 10 * i, "want": w, "got": w + [255]} for i, w in enumerate(want)],
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
            self.assertEqual(saved["version"], 1)
            self.assertTrue(saved["passed"])
            self.assertEqual(saved["facts"]["frame"], {"width": 480, "height": 300})
            self.assertNotIn("data_b64", path.read_text())

    def test_verdict_is_recomputed_and_names_the_first_failure(self):
        cases = {
            "tracks_left": facts(tracks_live_after=1),
            "not_app_mode": facts(display_mode="browser"),
            "api_missing": facts(apis={"get_display_media": True, "crop_target": False, "image_capture": True}),
            "cancelled": facts(prompt="cancelled", error="NotAllowedError", track=None, frame=None),
            "prompt_error": facts(prompt="error", error="InvalidStateError", track=None),
            "not_this_tab": facts(track={**facts()["track"], "display_surface": "monitor"}),
            "not_cropped": facts(crop={"applied": False, "error": "NotSupportedError"}),
            "wrong_size": facts(frame={"width": 1920, "height": 1080}),
        }
        pixels = facts()
        pixels["samples"][4]["got"] = [0, 0, 0, 255]
        cases["wrong_pixels"] = pixels
        few = facts()
        few["samples"] = few["samples"][:8]
        cases["wrong_pixels "] = few
        for reason, value in cases.items():
            probe = D.DirectProbe()
            with self.subTest(reason=reason):
                out = probe.record(value)
                self.assertEqual(out["reason"], reason.strip())
                self.assertFalse(out["passed"])
                self.assertFalse(probe.passed())
                self.assertFalse(out["saved"])

    def test_two_px_rounding_slack_only(self):
        self.assertEqual(D.verdict(D.normalize(facts(frame={"width": 482, "height": 298}))), "")
        self.assertEqual(D.verdict(D.normalize(facts(frame={"width": 483, "height": 300}))), "wrong_size")

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
                facts(samples=[{"x": 1, "y": 1, "want": [1, 2, 3], "got": [1, 2, 3]}]),
                facts(samples=[{"x": 1, "y": 1, "want": [1, 2, 3], "got": [1, 2, 3, 256]}]),
                facts(samples=facts()["samples"] * 2), facts(frame={"width": -1, "height": 2}),
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
