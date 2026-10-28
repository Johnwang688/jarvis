"""Quiet hours (`jarvis/v2/quiet.py`; phone design §7, §11 question 5, the
owner's answer of 2026-10-28): the setting, `is_quiet`, `ends_at`, the file,
the owner-only desk route and its protection.

Free and hermetic: temporary HOME and data folder; daemons on ephemeral
loopback ports (never 8402, 8403, 8405 or 8408). Times are built relative to
a fixed local midnight in a fixed zone, never from the system clock.

Run: PYTHONPATH=. .venv/bin/python tests/v2/quiet_check.py
"""
from __future__ import annotations

import ast
from datetime import datetime, time as dtime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("remote_auth_check", Path(__file__).with_name("remote_auth_check.py"))
rac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rac)

from jarvis import config  # noqa: E402
from jarvis.v2 import quiet  # noqa: E402
from jarvis.v2.quiet import DEFAULT, QuietHours  # noqa: E402

ZONE = timezone(timedelta(hours=-5))
MIDNIGHT = datetime(2026, 3, 10, tzinfo=ZONE)


def at(hours: float, days: int = 0) -> datetime:
    return MIDNIGHT + timedelta(days=days, hours=hours)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "data"
        self.clock = Clock(at(12))
        self.q = QuietHours(self.root, clock=self.clock)


class Window(_Base):

    def test_default_is_on_23_to_07(self):
        self.assertEqual(DEFAULT, {"enabled": True, "start": "23:00", "end": "07:00"})
        self.assertEqual(self.q.settings(), DEFAULT)
        self.assertFalse(self.root.exists(), "reading the default writes nothing")
        print("ok  default: on, 23:00 to 07:00, nothing written until the owner saves")

    def test_is_quiet_across_midnight(self):
        cases = {22.99: False, 23: True, 23.5: True, 24 + 3: True, 24 + 6.99: True, 24 + 7: False, 12: False}
        for hours, want in cases.items():
            self.assertEqual(self.q.is_quiet(at(hours)), want, hours)
        print("ok  is_quiet: [23:00, 07:00) across midnight, end excluded")

    def test_ends_at(self):
        self.assertEqual(self.q.ends_at(at(23.5)), at(24 + 7))
        self.assertEqual(self.q.ends_at(at(24 + 2)), at(24 + 7))
        self.assertEqual(self.q.ends_at(at(23)), at(24 + 7))
        self.assertIsNone(self.q.ends_at(at(24 + 7)))
        self.assertIsNone(self.q.ends_at(at(15)))
        self.assertEqual(self.q.ends_at(at(23.5)).tzinfo, ZONE)
        print("ok  ends_at: the next 07:00 while quiet, None otherwise, in now's zone")

    def test_clock_is_injected(self):
        self.clock.now = at(1)
        self.assertTrue(self.q.is_quiet())
        self.assertEqual(self.q.ends_at(), at(7))
        self.clock.now = at(1) + timedelta(hours=6)
        self.assertFalse(self.q.is_quiet())
        self.assertIsNone(self.q.ends_at())
        view = self.q.view(at(2))
        self.assertEqual(view, {**DEFAULT, "quiet_now": True, "ends_at": at(7).isoformat(timespec="minutes")})
        print("ok  the clock is injectable; view() says quiet now and until when")

    def test_daytime_window_and_off(self):
        self.q.update({"start": "13:00", "end": "15:30"})
        self.assertTrue(self.q.is_quiet(at(14)))
        self.assertFalse(self.q.is_quiet(at(15.5)))
        self.assertFalse(self.q.is_quiet(at(23.5)))
        self.assertEqual(self.q.ends_at(at(13)), at(15.5))
        self.q.update({"enabled": False})
        self.assertFalse(self.q.is_quiet(at(14)))
        self.assertIsNone(self.q.ends_at(at(14)))
        print("ok  a same-day window; off is never quiet")

    def test_update_checks_its_input(self):
        for bad in ({}, {"start": "7:00"}, {"start": "24:00"}, {"end": "07:60"}, {"start": "07:00"},
                    {"enabled": "yes"}, {"enabled": 1}, {"colour": "red"}, {"start": None}, [],
                    {"start": "23:00 "}):
            with self.assertRaises(ValueError, msg=bad):
                self.q.update(bad)
        self.assertEqual(self.q.settings(), DEFAULT)
        self.assertFalse((self.root / "quiet.json").exists())
        print("ok  update refuses unknown fields, bad times and start == end, writing nothing")


class File(_Base):
    def test_saved_0600_and_read_back(self):
        self.q.update({"start": "22:30"})
        path = self.root / "quiet.json"
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {**DEFAULT, "start": "22:30"})
        self.assertEqual(QuietHours(self.root).settings()["start"], "22:30")
        self.assertEqual([p.name for p in self.root.iterdir()], ["quiet.json"], "no temp file left")
        print("ok  saved 0600, atomically, and read back by a fresh reader")

    def test_unreadable_files_read_as_default(self):
        self.root.mkdir(parents=True)
        path = self.root / "quiet.json"
        other = self.root / "elsewhere.json"
        other.write_text(json.dumps({"enabled": False, "start": "01:00", "end": "02:00"}))
        cases = {
            "link": lambda: path.symlink_to(other),
            "malformed": lambda: path.write_text("{not json"),
            "extra field": lambda: path.write_text(json.dumps({**DEFAULT, "x": 1})),
            "equal times": lambda: path.write_text(json.dumps({**DEFAULT, "end": "23:00"})),
            "too large": lambda: path.write_text(json.dumps(DEFAULT) + " " * 5000),
            "a folder": lambda: path.mkdir(),
        }
        for name, make in cases.items():
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            elif path.exists() or path.is_symlink():
                path.unlink()
            make()
            self.assertEqual(QuietHours(self.root).settings(), DEFAULT, name)
        print("ok  a link, a folder, a malformed, odd or oversized file reads as the default")

    def test_write_over_a_link_replaces_the_link_only(self):
        self.root.mkdir(parents=True)
        target = self.root / "target.json"
        target.write_text("keep")
        (self.root / "quiet.json").symlink_to(target)
        self.q.update({"enabled": False})
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse((self.root / "quiet.json").is_symlink())
        print("ok  a save never writes through a link")


class Source(unittest.TestCase):
    def test_imports_no_http_push_or_daemon(self):
        tree = ast.parse((ROOT / "jarvis" / "v2" / "quiet.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * node.level) + (node.module or ""))
        self.assertEqual(names - {"__future__", "datetime", "errno", "json", "logging", "os", "pathlib",
                                  "re", "secrets", "stat", "threading"}, set(), names)
        print("ok  quiet.py imports only the standard library's basics")

    def test_self_protected(self):
        from jarvis.tools.files import SELF_PROTECTED
        self.assertIn("jarvis/v2/quiet.py", SELF_PROTECTED)
        print("ok  quiet.py is SELF_PROTECTED")

    def test_pure_helpers(self):
        self.assertTrue(quiet.within(dtime(23), dtime(7), dtime(0)))
        self.assertFalse(quiet.within(dtime(23), dtime(7), dtime(7)))
        self.assertIsNone(quiet.parse_hhmm("1:00"))
        self.assertEqual(quiet.parse_hhmm("01:05"), dtime(1, 5))
        print("ok  within() and parse_hhmm()")


class Desk(rac.Remote):
    def test_route_is_owner_only(self):
        status, body = self.hud("GET", "/quiet")
        self.assertEqual(status, 200, body)
        self.assertEqual({k: body[k] for k in DEFAULT}, DEFAULT)
        self.assertIn(body["quiet_now"], (True, False))
        status, body = self.hud("POST", "/quiet", {"start": "22:00", "end": "06:30"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["start"], body["end"], body["enabled"]), ("22:00", "06:30", True))
        self.assertEqual(json.loads((Path(config.V2_DATA_DIR) / "quiet.json").read_text())["end"], "06:30")
        status, body = self.hud("POST", "/quiet", {"start": "22:00", "end": "22:00"})
        self.assertEqual(status, 400, body)
        status, body = self.hud("POST", "/quiet", {"loud": True})
        self.assertEqual(status, 400, body)
        # Not the HUD's own page (no Origin on a write), not the HUD listener,
        # and never the phone's listener.
        self.assertEqual(self.hud("POST", "/quiet", {"enabled": False}, origin=False)[0], 403)
        self.assertEqual(self.hud("POST", "/quiet", {"enabled": False}, port=self.d.port)[0], 403)
        self.assertEqual(self.hud("GET", "/quiet", port=self.d.port)[0], 403)
        status, _, _ = self.phone("GET", "/quiet")
        self.assertEqual(status, 404)
        self.assertTrue(self.d.quiet.settings()["enabled"])
        print("ok  /quiet: owner-only on the HUD listener (403 elsewhere, 404 on the phone's)")

    def test_protected_state_v1_and_v2(self):
        from jarvis import permissions as v1, rules as v1_rules, runtime, tools
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2
        target = Path(config.V2_DATA_DIR) / "quiet.json"
        self.d.quiet.update({"enabled": True})
        self.assertIn(target.resolve(), files._protected_state())
        self.assertIn(target.resolve(), v2.protected_paths())
        self.assertTrue(v2.denied_file(str(target)))
        for command in (f"cp /tmp/x {target}", f"echo '{{}}' > {target}", f"rm {target}",
                        f"python3 -c \"open('{target}', 'w').write('x')\""):
            self.assertTrue(v2.denied_command(command), command)
            self.assertNotEqual(v1.static_verdict(command).decision, v1_rules.ALLOW, command)
        with runtime.unconfined():
            out = tools.dispatch("write_file", json.dumps({"path": str(target), "content": "{}"}))
        self.assertTrue(json.loads(target.read_text())["enabled"], out.text)
        print("ok  quiet.json is protected state: refused at v2 layer 1, never ALLOW in v1, write_file refused")


if __name__ == "__main__":
    unittest.main(verbosity=1)
