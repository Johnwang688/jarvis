"""V15: lowering the Remote Browser's sound while Jarvis speaks.

Free and hermetic: a fake PulseAudio (a list of sink-inputs behind an injected
runner), a fake process table, a fake clock. No pactl, no Chrome, no audio.

Run:
    PYTHONPATH=. .venv/bin/python tests/v2/speech_duck_check.py
"""
from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.v2 import speech_duck as D  # noqa: E402
from jarvis.v2.browser import audio as A  # noqa: E402

FOLDER = Path("/fixture/profile")
LIST = ("--format=json", "list", "sink-inputs")
NORM = 65536


class FakeProcesses:
    """Chrome root 100 -> 110 -> 120 (our audio service); 200 is another app's
    Chrome (same executable and name); 300 is not Chrome at all."""
    uid = 1000

    def __init__(self):
        self.rows = {100: A.Process(100, 9, 20, self.uid, A.CHROME),
                     110: A.Process(110, 100, 21, self.uid, A.CHROME),
                     120: A.Process(120, 110, 22, self.uid, A.CHROME),
                     200: A.Process(200, 9, 23, self.uid, A.CHROME),
                     300: A.Process(300, 9, 24, self.uid, "/usr/bin/mpv")}

    def identity(self, pid):
        return self.rows.get(pid)

    def profile(self, process, folder):
        return folder == FOLDER and self.rows.get(100) == process

    def owner(self):
        return A.OwnedChrome.pin(100, FOLDER, self)


def stream(index, pid, volume=(NORM, NORM), *, muted=False, name="Google Chrome"):
    return {"index": index, "mute": muted, "corked": False,
            "volume": {("front-left", "front-right", "x", "y")[i]: {"value": v} for i, v in enumerate(volume)},
            "properties": ({"application.process.id": str(pid), "application.name": name}
                           if pid is not None else {"application.name": name})}


class Pulse:
    """A fake server: the listing, volume writes, and PulseAudio's habit of giving
    a new stream the volume last saved for its application name."""
    def __init__(self, rows):
        self.rows = rows
        self.commands = []
        self.fail = None            # None, or a callable(args) -> bool (raise DuckError when true)
        self.saved = {}             # application.name -> volume tuple (module-stream-restore)
        self.restore_on_create = True

    def run(self, args, timeout):
        self.commands.append(tuple(args))
        if self.fail and self.fail(args):
            raise D.DuckError("fake failure")
        if tuple(args) == LIST:
            return json.dumps(self.rows).encode()
        assert args[0] == "set-sink-input-volume", args
        row = next(r for r in self.rows if str(r["index"]) == args[1])
        values = [int(v) for v in args[2:]]
        assert len(values) == len(row["volume"]), "channel count must match"
        for channel, value in zip(row["volume"], values):
            row["volume"][channel]["value"] = value
        self.saved[row["properties"].get("application.name")] = tuple(values)
        return b""

    def add(self, row):
        if self.restore_on_create and row["properties"].get("application.name") in self.saved:
            for channel, value in zip(row["volume"], self.saved[row["properties"]["application.name"]]):
                row["volume"][channel]["value"] = value
        self.rows.append(row)

    def volume(self, index):
        return tuple(v["value"] for v in next(r for r in self.rows if r["index"] == index)["volume"].values())

    def writes(self):
        return [c for c in self.commands if c[0] != "--format=json"]

    def set(self, index, volume):
        row = next(r for r in self.rows if r["index"] == index)
        for channel, value in zip(row["volume"], volume):
            row["volume"][channel]["value"] = value


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name) / "speech_duck.json"
        self.procs = FakeProcesses()
        self.audible = True
        self.clock = Clock()
        self.pulse = Pulse([stream(1, 120), stream(2, 200), stream(3, None), stream(4, 300, name="mpv")])

    def make(self, **kw):
        kw.setdefault("threaded", False)
        kw.setdefault("journal", self.journal)
        kw.setdefault("clock", self.clock)
        return D.Ducker(lambda: [self.procs.owner()] if self.audible else [], run=self.pulse.run, **kw)


class Attribution(Base):
    def test_only_the_daemons_own_chrome_streams_are_lowered(self):
        d = self.make()
        d.grant(); d.tick()
        self.assertEqual(self.pulse.volume(1), D.ducked((NORM, NORM)))
        self.assertLess(self.pulse.volume(1)[0], NORM)
        # Another Chrome with the very same application name, a stream with no
        # process, and another program: never written.
        for index in (2, 3, 4):
            self.assertEqual(self.pulse.volume(index), (NORM, NORM))
        self.assertEqual([c[1] for c in self.pulse.writes()], ["1"])

    def test_a_foreign_chrome_is_not_ours_whatever_its_name(self):
        self.pulse.rows[:] = [stream(2, 200)]
        d = self.make(); d.grant(); d.tick()
        self.assertEqual(self.pulse.writes(), [])

    def test_the_duck_is_about_fifteen_db_and_keeps_the_balance(self):
        self.assertEqual(D.ducked((NORM, NORM)), (36854, 36854))      # pactl's own -15dB
        self.assertEqual(D.ducked((40000, 30000)), (22494, 16870))
        self.assertEqual(D.ducked((0, 5000))[0], 0)
        self.assertAlmostEqual(20 * __import__("math").log10((36854 / NORM) ** 3), -15.0, places=1)

    def test_nothing_audible_means_no_pactl_call_at_all(self):
        self.audible = False
        d = self.make(); d.grant(); d.tick()
        self.assertEqual(self.pulse.commands, [])

    def test_the_master_permission_off_is_a_no_op_through_the_real_owner_source(self):
        # BrowserSound.duck_owners() is the source: off, muted, private or closing instances are not listed.
        from jarvis.v2.browser import host as H
        from jarvis.v2.browser.sound import BrowserSound
        host = H.BrowserHost()
        sound = BrowserSound(host, probe=lambda: A.Availability(True, True))
        host._sound = sound
        profile = "p-12345678"
        audible = H._Instance(profile, FOLDER, object(), object(), generation=1, audio=A.InstanceAudio(True))
        host._instances[profile] = audible
        root = self.procs.owner()
        with patch.object(H, "chrome_pids", return_value=[100]), \
                patch.object(A.OwnedChrome, "pin", side_effect=lambda pid, folder, reader=None: root):
            self.assertEqual(sound.duck_owners(), [])                       # no owner gesture: nothing requested
            sound._requests.add(profile)
            self.assertEqual(len(sound.duck_owners()), 1)                  # requested and launched audible
            audible.audio = A.InstanceAudio(False)
            self.assertEqual(sound.duck_owners(), [])                       # launched muted: structurally quiet
            audible.audio = A.InstanceAudio(True)
            audible.closing = True
            self.assertEqual(sound.duck_owners(), [])
            audible.closing = False
            host._settings.update(sound=False); sound.sync()               # master OFF clears the request
            self.assertEqual(sound.duck_owners(), [])
            host._settings.update(sound=True); sound.sync()
            self.assertEqual(sound.duck_owners(), [])                       # and ON does not revive it
            host._stopped = True
            sound._requests.add(profile)
            self.assertEqual(sound.duck_owners(), [])
        host._stopped = True


class Restore(Base):
    def test_release_restores_the_exact_per_channel_volume(self):
        self.pulse.set(1, (40000, 30000))
        d = self.make(); d.grant(); d.tick()
        self.assertEqual(self.pulse.volume(1), (22494, 16870))
        d.release(); d.tick()
        self.assertEqual(self.pulse.volume(1), (40000, 30000))
        self.assertEqual(d.status()["ducked"], 0)

    def test_the_lease_expiring_restores_without_anyone_saying_so(self):
        d = self.make(); d.grant(); d.tick()
        self.assertLess(self.pulse.volume(1)[0], NORM)
        self.clock.now += D.LEASE_S - 0.1; d.tick()
        self.assertLess(self.pulse.volume(1)[0], NORM, "still within the lease")
        d.grant(); self.clock.now += D.LEASE_S - 0.1; d.tick()
        self.assertLess(self.pulse.volume(1)[0], NORM, "a renewal extends it")
        self.clock.now += 0.2; d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_a_scan_with_nothing_changed_writes_nothing(self):
        d = self.make(); d.grant(); d.tick()
        before = len(self.pulse.writes())
        for _ in range(5):
            d.tick()
        self.assertEqual(len(self.pulse.writes()), before)

    def test_stop_restores_and_refuses_new_leases(self):
        d = self.make(); d.grant(); d.tick()
        d.stop()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        self.assertFalse(d.grant())

    def test_the_owner_changing_the_volume_during_a_duck_is_never_stomped(self):
        d = self.make(); d.grant(); d.tick()
        self.pulse.set(1, (20000, 20000))           # the owner moved the slider in the Windows mixer
        writes = len(self.pulse.writes())
        d.tick()                                     # noticed; not re-ducked within this lease
        self.assertEqual(self.pulse.volume(1), (20000, 20000))
        self.assertEqual(len(self.pulse.writes()), writes)
        d.release(); d.tick()                        # and not "restored" over their choice
        self.assertEqual(self.pulse.volume(1), (20000, 20000))
        self.assertEqual(len(self.pulse.writes()), writes)
        # The next lease ducks it again from the owner's level.
        d.grant(); d.tick()
        self.assertEqual(self.pulse.volume(1), D.ducked((20000, 20000)))
        d.release(); d.tick()
        self.assertEqual(self.pulse.volume(1), (20000, 20000))

    def test_an_owner_change_found_only_at_restore_stands(self):
        d = self.make(); d.grant(); d.tick()
        self.pulse.set(1, (50000, 50000))
        d.release(); d.tick()
        self.assertEqual(self.pulse.volume(1), (50000, 50000))


class MidDuck(Base):
    def test_a_stream_that_appears_mid_duck_is_ducked_and_restored_to_its_own_volume(self):
        self.pulse.restore_on_create = False
        d = self.make(); d.grant(); d.tick()
        self.pulse.add(stream(5, 120, (30000, 30000)))
        d.tick()
        self.assertEqual(self.pulse.volume(5), D.ducked((30000, 30000)))
        d.release(); d.tick()
        self.assertEqual(self.pulse.volume(5), (30000, 30000))
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_a_stream_born_from_pulseaudios_memory_of_the_duck_is_restored_to_the_real_volume(self):
        # module-stream-restore gives a new "Google Chrome" stream the last saved
        # (ducked) volume; the duck must not take that as the owner's level.
        d = self.make(); d.grant(); d.tick()
        self.pulse.rows.append(stream(5, 120, D.ducked((NORM, NORM))))      # born already ducked
        d.tick()
        self.assertEqual(self.pulse.volume(5), D.ducked((NORM, NORM)))      # unchanged, held
        d.release(); d.tick()
        self.assertEqual(self.pulse.volume(5), (NORM, NORM))

    def test_a_muted_stream_is_left_entirely_alone_and_mute_is_never_written(self):
        self.pulse.rows[0]["mute"] = True
        d = self.make(); d.grant(); d.tick()
        self.assertEqual(self.pulse.writes(), [])
        self.pulse.rows[0]["mute"] = False          # the owner unmutes it mid-duck: now ducked
        d.tick()
        self.assertLess(self.pulse.volume(1)[0], NORM)
        d.release(); d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        self.assertFalse(any("mute" in part for c in self.pulse.commands for part in c if part != "--format=json"))
        self.assertFalse(self.pulse.rows[0]["mute"])

    def test_a_mute_the_owner_sets_during_the_duck_survives_the_restore(self):
        d = self.make(); d.grant(); d.tick()
        self.pulse.rows[0]["mute"] = True
        d.release(); d.tick()
        self.assertTrue(self.pulse.rows[0]["mute"])
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_index_reuse_before_the_write_by_another_app_is_never_written(self):
        original = self.pulse.run
        state = {"n": 0}

        def run(args, timeout):
            if tuple(args) == LIST:
                state["n"] += 1
                if state["n"] == 2:          # between the scan and the guard: index 1 became someone else's
                    self.pulse.rows[0] = stream(1, 300, name="mpv")
            return original(args, timeout)
        self.pulse.run = run
        d = D.Ducker(lambda: [self.procs.owner()], run=run, threaded=False, journal=self.journal, clock=self.clock)
        d.grant(); d.tick()
        self.assertEqual(self.pulse.writes(), [])
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_a_stream_that_dies_mid_duck_is_not_an_error(self):
        d = self.make(); d.grant(); d.tick()
        del self.pulse.rows[0]
        d.tick()
        self.assertEqual(d.status()["ducked"], 0)
        d.release(); d.tick()


class Failures(Base):
    def test_a_failing_listing_is_non_fatal_and_logged_once(self):
        self.pulse.fail = lambda args: True
        d = self.make(); d.grant()
        with self.assertLogs("jarvis.v2.speech_duck", level="WARNING") as logs:
            for _ in range(5):
                d.tick()
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.pulse.writes(), [])
        self.pulse.fail = None
        d.tick()
        self.assertLess(self.pulse.volume(1)[0], NORM)

    def test_a_timeout_in_the_write_leaves_nothing_held_and_is_retried(self):
        self.pulse.fail = lambda args: args[0] == "set-sink-input-volume"
        d = self.make(); d.grant(); d.tick()
        self.assertEqual(d.status()["ducked"], 0)
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        self.pulse.fail = None
        d.tick()
        self.assertLess(self.pulse.volume(1)[0], NORM)

    def test_a_failed_restore_stays_held_and_is_retried_until_it_works(self):
        d = self.make(); d.grant(); d.tick()
        self.pulse.fail = lambda args: True
        d.release()
        for _ in range(3):
            d.tick()
        self.assertEqual(d.status()["ducked"], 1)
        self.pulse.fail = None
        d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        self.assertEqual(d.status()["ducked"], 0)

    def test_garbage_from_pactl_writes_nothing(self):
        for raw in (b"not json", b"{}", b'[{"index": 1}]', b'[{"index":1,"mute":false,"properties":{"application.process.id":"x"},"volume":{}}]',
                    json.dumps([stream(1, 120, (VOL := D.READ_MAX + 1, VOL))]).encode()):
            with self.subTest(raw=raw[:30]):
                pulse = Pulse([])
                pulse.run = lambda args, timeout, raw=raw: raw
                d = D.Ducker(lambda: [self.procs.owner()], run=pulse.run, threaded=False, journal=None, clock=self.clock)
                d.grant(); d.tick()
                self.assertEqual(d.status()["ducked"], 0)

    def test_an_unexpected_exception_never_escapes_the_worker(self):
        def boom():
            raise RuntimeError("owner source broke")
        d = D.Ducker(boom, run=self.pulse.run, threaded=False, journal=None, clock=self.clock)
        d.grant()
        with self.assertLogs("jarvis.v2.speech_duck", level="WARNING"):
            d.tick()


class Journal(Base):
    def test_a_duck_is_recorded_before_it_is_written_and_forgotten_when_restored(self):
        seen = {}
        original = self.pulse.run

        def run(args, timeout):
            if args[0] == "set-sink-input-volume":
                seen["journal"] = json.loads(self.journal.read_text())
            return original(args, timeout)
        d = D.Ducker(lambda: [self.procs.owner()], run=run, threaded=False, journal=self.journal, clock=self.clock)
        d.grant(); d.tick()
        self.assertEqual(seen["journal"]["streams"][0]["prior"], [NORM, NORM])
        self.assertEqual(stat.S_IMODE(self.journal.stat().st_mode), 0o600)
        d.release(); d.tick()
        self.assertFalse(self.journal.exists())

    def test_a_killed_daemon_is_repaired_at_the_next_start_when_a_stream_is_born_ducked(self):
        d = self.make(); d.grant(); d.tick()
        # kill -9: no restore, the journal stays; PulseAudio keeps the ducked volume for the name.
        self.assertTrue(self.journal.exists())
        self.assertEqual(self.pulse.saved["Google Chrome"], D.ducked((NORM, NORM)))
        self.pulse.rows[:] = []                                            # Chrome went away with the daemon
        later = self.make(); later.recover()
        self.pulse.add(stream(9, 120))                                     # the next Remote stream: born quiet
        self.assertEqual(self.pulse.volume(9), D.ducked((NORM, NORM)))
        later.tick()
        self.assertEqual(self.pulse.volume(9), (NORM, NORM))
        self.assertFalse(self.journal.exists())

    def test_a_leftover_is_not_applied_to_a_foreign_stream_or_a_different_volume(self):
        d = self.make(); d.grant(); d.tick()
        self.pulse.rows[:] = []
        later = self.make(); later.recover()
        self.pulse.rows.extend([stream(9, 200, D.ducked((NORM, NORM))),      # another app's Chrome
                                stream(10, 120, (12345, 12345))])           # the owner's own level
        later.tick()
        self.assertEqual(self.pulse.volume(9), D.ducked((NORM, NORM)))
        self.assertEqual(self.pulse.volume(10), (12345, 12345))
        self.assertTrue(self.journal.exists())

    def test_a_leftover_met_under_a_new_lease_restores_to_the_real_prior(self):
        d = self.make(); d.grant(); d.tick()
        self.pulse.rows[:] = []
        later = self.make(); later.recover()
        self.pulse.add(stream(9, 120))
        later.grant(); later.tick()
        self.assertEqual(self.pulse.volume(9), D.ducked((NORM, NORM)))
        later.release(); later.tick()
        self.assertEqual(self.pulse.volume(9), (NORM, NORM))
        self.assertFalse(self.journal.exists())

    def test_a_stream_lost_mid_duck_leaves_a_leftover_not_a_silent_duck(self):
        d = self.make(); d.grant(); d.tick()
        del self.pulse.rows[0]                      # Chrome restarted (sound OFF/ON) during the duck
        d.release(); d.tick()
        self.assertTrue(self.journal.exists())
        self.pulse.add(stream(7, 120))              # born quiet
        d.tick()                                    # the idle repair
        self.assertEqual(self.pulse.volume(7), (NORM, NORM))
        self.assertFalse(self.journal.exists())

    def test_a_corrupt_oversized_or_linked_journal_is_empty(self):
        for how in ("corrupt", "big", "symlink", "shape", "dir"):
            with self.subTest(how=how):
                if self.journal.is_symlink() or self.journal.is_file():
                    self.journal.unlink()
                elif self.journal.is_dir():
                    self.journal.rmdir()
                if how == "corrupt":
                    self.journal.write_text("{not json")
                elif how == "big":
                    self.journal.write_text('{"streams": []}' + " " * 70000)
                elif how == "symlink":
                    target = Path(self.tmp.name) / "target.json"
                    target.write_text(json.dumps({"streams": [{"prior": [NORM, NORM], "applied": [1, 1], "at": 1}]}))
                    self.journal.symlink_to(target)
                elif how == "shape":
                    self.journal.write_text(json.dumps({"streams": [{"prior": [-1], "applied": [1], "at": 1},
                                                                      {"prior": [1, 2], "applied": [1], "at": 1},
                                                                      {"prior": [NORM] * 3, "applied": [NORM] * 3, "at": 1},
                                                                      "x", None]}))
                else:
                    self.journal.mkdir()
                d = self.make(); d.recover()
                self.assertEqual(d._residue, [])

    def test_old_leftovers_expire(self):
        self.journal.write_text(json.dumps({"streams": [{"prior": [NORM, NORM], "applied": [36854, 36854], "at": 100.0}]}))
        d = self.make(wall=lambda: 100.0 + D.RESIDUE_TTL_S + 1); d.recover()
        d.tick()
        self.assertEqual(d._residue, [])
        self.assertFalse(self.journal.exists())

    def test_no_journal_path_still_ducks(self):
        d = self.make(journal=None); d.grant(); d.tick()
        self.assertLess(self.pulse.volume(1)[0], NORM)


class Forged(Base):
    """HIGH-1: a journal is a file a hostile process might write. Nothing it says can raise a volume."""

    def forge(self, rows):
        self.journal.write_text(json.dumps({"streams": rows}))

    def idle_repair(self, volume, rows):
        self.forge(rows)
        self.pulse.rows[:] = [stream(1, 120, volume)]
        d = self.make(); d.recover(); d.tick()
        return d

    def test_the_leads_repro_cannot_raise_a_stream_to_400_percent(self):
        now = time.time()
        d = self.idle_repair((NORM, NORM), [{"prior": [262144, 262144], "applied": [NORM, NORM], "at": now}])
        self.assertEqual(d._residue, [])
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        self.assertEqual(self.pulse.writes(), [])
        d.grant(); d.tick()                                        # and under a lease the duck only lowers
        self.assertLessEqual(max(self.pulse.volume(1)), NORM)
        self.assertEqual(self.pulse.volume(1), D.ducked((NORM, NORM)))
        d.release(); d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_implausible_future_and_non_finite_entries_are_dropped_on_load(self):
        now = time.time()
        good = {"prior": [NORM, NORM], "applied": list(D.ducked((NORM, NORM))), "at": now}
        for what, row in (("prior above 100%", {**good, "prior": [NORM + 1, NORM], "applied": list(D.ducked((NORM + 1, NORM)))}),
                          ("applied not the duck of prior", {**good, "applied": [30000, 30000]}),
                          ("applied the same as prior", {**good, "applied": [NORM, NORM]}),
                          ("applied raises", {"prior": [20000, 20000], "applied": [NORM, NORM], "at": now}),
                          ("channel count differs", {**good, "applied": [good["applied"][0]]}),
                          ("in the future", {**good, "at": now + 10 * D.SKEW_S}),
                          ("negative time", {**good, "at": -5}),
                          ("infinite time", {**good, "at": float("inf")}),
                          ("boolean time", {**good, "at": True}),
                          ("string volume", {**good, "prior": ["65536", "65536"]})):
            with self.subTest(what=what):
                self.forge([row])
                d = self.make(); d.recover()
                self.assertEqual(d._residue, [])
        self.journal.write_text('{"streams": [{"prior": [65536, 65536], "applied": [36854, 36854], "at": NaN}]}')
        d = self.make(); d.recover()
        self.assertEqual(d._residue, [])
        self.forge([good])
        d = self.make(); d.recover()
        self.assertEqual(len(d._residue), 1)                               # an honest record still loads
        self.forge([{**good, "applied": [good["applied"][0] + 2, good["applied"][1] - 2]}])
        d = self.make(); d.recover()
        self.assertEqual(len(d._residue), 1)                               # the server's rounding is allowed

    def test_a_duck_never_writes_above_the_streams_own_volume_or_100_percent(self):
        # Defence in depth: even an in-memory leftover the loader would have refused cannot raise.
        d = self.make()
        d._load()
        d._residue = [D.Residue((NORM, NORM), (30000, 30000), time.time())]
        self.pulse.rows[:] = [stream(1, 120, (30000, 30000))]
        d.grant(); d.tick()
        self.assertTrue(all(int(v) <= 30000 for c in self.pulse.writes() for v in c[2:]), self.pulse.writes())
        d2 = self.make(journal=None)
        d2._residue = [D.Residue((262144, 262144), (30000, 30000), time.time())]
        self.pulse.rows[:] = [stream(1, 120, (30000, 30000))]
        d2.grant(); d2.tick()
        self.assertLessEqual(max(self.pulse.volume(1)), 30000)
        d2.release(); d2.tick()
        self.assertLessEqual(max(self.pulse.volume(1)), D.NORM)

    def test_a_restore_and_a_sweep_never_write_above_100_percent(self):
        d = self.make(journal=None)
        d._loaded = True
        d._held[1] = D._Held(1, 120, (262144, 262144), D.ducked((NORM, NORM)), time.time())
        self.pulse.rows[:] = [stream(1, 120, D.ducked((NORM, NORM)))]
        d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        d._residue = [D.Residue((262144, 262144), (36854, 36854), time.time())]
        self.pulse.rows[:] = [stream(2, 120, (36854, 36854))]
        d.tick()
        self.assertEqual(self.pulse.volume(2), (NORM, NORM))

    def test_a_stream_boosted_past_100_percent_is_left_alone(self):
        self.pulse.rows[:] = [stream(1, 120, (90000, 90000))]
        d = self.make(); d.grant(); d.tick()
        self.assertEqual(self.pulse.writes(), [])
        self.assertEqual(self.pulse.volume(1), (90000, 90000))


class OneBadForeignStream(Base):
    """A foreign client can set odd properties; it must not stop our streams being restored."""

    def foreigners(self):
        weird_pid = stream(7, 200); weird_pid["properties"]["application.process.id"] = "abc"
        loud = stream(8, 200, (5 * NORM, 5 * NORM))
        no_props = {"index": 9, "mute": False, "volume": {"front-left": {"value": 1}}, "properties": "x"}
        short_volume = stream(10, 200); short_volume["volume"] = {}
        return {"pid abc": weird_pid, "five times the volume": loud, "properties not an object": no_props,
                "no channels": short_volume}

    def test_a_bad_foreign_row_neither_blocks_the_duck_nor_is_ever_written(self):
        for what, row in self.foreigners().items():
            with self.subTest(what=what):
                self.pulse.commands.clear()
                self.pulse.rows[:] = [stream(1, 120), row]
                d = self.make(journal=None); d.grant(); d.tick()
                self.assertLess(self.pulse.volume(1)[0], NORM)
                d.release(); d.tick()
                self.assertEqual(self.pulse.volume(1), (NORM, NORM), "restored on release")
                self.assertNotIn(row["index"], [int(c[1]) for c in self.pulse.writes()])

    def test_a_bad_foreign_row_does_not_block_a_restore_at_shutdown_or_the_idle_repair(self):
        for what, row in self.foreigners().items():
            with self.subTest(what=what):
                self.pulse.rows[:] = [stream(1, 120)]
                d = self.make(journal=None); d.grant(); d.tick()
                self.pulse.rows.append(row)                       # it appears mid-duck
                d.tick()
                d.stop()
                self.assertEqual(self.pulse.volume(1), (NORM, NORM), "restored by stop()")
        # the idle repair of a lost duck still finds its stream among odd ones
        self.pulse.rows[:] = [self.foreigners()["pid abc"], stream(1, 120, D.ducked((NORM, NORM)))]
        d = self.make(journal=None); d._loaded = True
        d._residue = [D.Residue((NORM, NORM), D.ducked((NORM, NORM)), time.time())]
        d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_a_held_stream_that_turns_malformed_becomes_a_leftover_not_a_write(self):
        d = self.make(); d.grant(); d.tick()
        self.pulse.rows[0]["properties"]["application.process.id"] = "abc"
        writes = len(self.pulse.writes())
        d.release(); d.tick()
        self.assertEqual(len(self.pulse.writes()), writes)
        self.assertEqual(d.status()["ducked"], 0)
        self.assertEqual(len(d._residue), 1)
        self.assertTrue(self.journal.exists())

    def test_only_a_structural_problem_fails_the_listing(self):
        row = stream(1, 120)
        for what, rows in (("not a list", {}), ("a row that is not an object", [row, 5]),
                           ("an index that is not an integer", [{**row, "index": "1"}]),
                           ("a repeated index", [row, stream(1, 121)]),
                           ("a mute that is not a boolean", [{**row, "mute": 1}]),
                           ("more rows than the cap", [stream(i, 120) for i in range(A.STREAM_CAP + 1)])):
            with self.subTest(what=what), self.assertRaises(D.DuckError):
                D.parse(json.dumps(rows).encode())
        self.assertEqual(D.parse(json.dumps([row, {**stream(2, 120), "volume": "x"}]).encode()), [D.Stream(1, 120, False, (NORM, NORM))])

    def test_no_duck_is_written_once_stop_has_been_called(self):
        d = self.make()
        original = d._persist

        def stop_in_between():
            original()
            d._stopped = True                                   # stop() arrives after the record, before the write
        d._persist = stop_in_between
        d.grant(); d.tick()
        self.assertEqual(self.pulse.writes(), [])
        self.assertEqual(d.status()["ducked"], 0)


class Worker(Base):
    def wait(self, check, what, limit=10.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if check():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    def test_the_worker_thread_ducks_then_restores_when_renewals_stop(self):
        d = D.Ducker(lambda: [self.procs.owner()], run=self.pulse.run, journal=self.journal,
                     lease_s=0.4, scan_s=0.05, retry_s=0.05)
        self.addCleanup(d.stop)
        self.pulse.restore_on_create = False
        d.grant()
        self.wait(lambda: self.pulse.volume(1)[0] < NORM, "the duck")
        self.pulse.add(stream(5, 120, (30000, 30000)))
        self.wait(lambda: self.pulse.volume(5) == D.ducked((30000, 30000)), "the new stream's duck")
        # No renewal: the HUD vanished. Expiry alone restores.
        self.wait(lambda: self.pulse.volume(1) == (NORM, NORM) and self.pulse.volume(5) == (30000, 30000), "the expiry restore")
        self.assertFalse(d.status()["on"])

    def test_renewals_hold_the_duck_and_release_ends_it(self):
        d = D.Ducker(lambda: [self.procs.owner()], run=self.pulse.run, journal=None,
                     lease_s=0.4, scan_s=0.05, retry_s=0.05)
        self.addCleanup(d.stop)
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            d.grant(); time.sleep(0.1)
        self.assertLess(self.pulse.volume(1)[0], NORM)
        d.release()
        self.wait(lambda: self.pulse.volume(1) == (NORM, NORM), "the release restore")

    def test_stop_joins_the_worker_and_restores(self):
        d = D.Ducker(lambda: [self.procs.owner()], run=self.pulse.run, journal=None, lease_s=30, scan_s=0.05)
        d.grant()
        self.wait(lambda: self.pulse.volume(1)[0] < NORM, "the duck")
        d.stop()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        self.assertFalse(d._thread.is_alive())


class Hostile(Base):
    def test_a_fifo_at_the_old_predictable_temp_name_cannot_hang_the_worker(self):
        for name in (f".{self.journal.name}.{os.getpid()}.tmp", f".{self.journal.name}.tmp"):
            os.mkfifo(self.journal.parent / name)
        os.link  # a hardlink cannot be opened exclusively either; the random name never meets one
        d = self.make()
        done = threading.Event()

        def work():
            d.grant(); d.tick(); d.release(); d.tick()
            done.set()
        t = threading.Thread(target=work, daemon=True); t.start()
        self.assertTrue(done.wait(5), "the worker blocked on a FIFO")
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        self.assertFalse(self.journal.exists())

    def test_the_journal_is_written_through_a_random_exclusive_file_and_leaves_no_litter(self):
        d = self.make(); d.grant(); d.tick()
        self.assertEqual(stat.S_IMODE(self.journal.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.journal.parent.iterdir() if p.name.endswith(".tmp")], [])
        d.release(); d.tick()
        self.assertEqual(list(self.journal.parent.iterdir()), [])

    def test_a_journal_path_that_is_a_directory_is_not_fatal(self):
        self.journal.mkdir()
        d = self.make(); d.grant()
        with self.assertLogs("jarvis.v2.speech_duck", level="WARNING"):
            d.tick()
        d.release(); d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_held_rows_survive_the_journal_cap_and_only_leftovers_are_cut(self):
        d = self.make(); d._load()
        d._residue = [D.Residue((NORM, NORM), (36854 - i, 36854 - i), time.time()) for i in range(D.MAX_RESIDUE + 10)]
        self.pulse.rows[:] = [stream(1, 120, (50000, 50000))]
        d.grant(); d.tick()
        rows = json.loads(self.journal.read_text())["streams"]
        self.assertIn([50000, 50000], [r["prior"] for r in rows], "the active duck is always recorded")
        self.assertLessEqual(len(rows), D.MAX_RESIDUE + D.MAX_STREAMS)


class Bounded(Base):
    def test_restore_stops_writing_at_the_first_failure(self):
        self.pulse.rows[:] = [stream(i, 120) for i in (1, 2, 3)]
        d = self.make(); d.grant(); d.tick()
        self.assertEqual(d.status()["ducked"], 3)
        self.pulse.fail = lambda args: args[0] == "set-sink-input-volume"
        before = len(self.pulse.commands)
        d.release(); d.tick()
        tries = [c for c in self.pulse.commands[before:] if c[0] == "set-sink-input-volume"]
        self.assertEqual(len(tries), 1, "one timeout ends this round of writes")
        self.assertEqual(d.status()["ducked"], 3)                       # all still held and journaled
        self.assertTrue(self.journal.exists())
        self.pulse.fail = None
        d.tick()
        self.assertTrue(all(self.pulse.volume(i) == (NORM, NORM) for i in (1, 2, 3)))

    def test_stop_is_bounded_overall_even_when_pactl_is_slow(self):
        d = self.make(); d.grant(); d.tick()
        original = self.pulse.run

        def slow(args, timeout):
            time.sleep(min(timeout, 0.25))
            raise D.DuckError("timed out")
        d._run = slow
        with patch.object(D, "STOP_S", 0.6):
            began = time.monotonic()
            with self.assertLogs("jarvis.v2.speech_duck", level="WARNING"):
                d.stop()
            took = time.monotonic() - began
        self.assertLess(took, 1.5)
        self.assertTrue(self.journal.exists(), "what was not restored stays journaled")
        d._run = original

    def test_each_call_gets_only_the_time_left_of_the_overall_deadline(self):
        d = self.make(); d.grant(); d.tick()
        seen = []
        original = self.pulse.run

        def watch(args, timeout):
            seen.append(timeout)
            return original(args, timeout)
        d._run = watch
        d._restore(time.monotonic() + 0.5)
        self.assertTrue(seen and all(t <= 0.5 for t in seen), seen)
        d.grant(); d.tick()
        with self.assertRaises(D.DuckError):
            d._restore(time.monotonic() - 1)                            # no time at all: no call

    def test_the_cap_on_one_continuous_duck_restores_and_rests_even_while_renewed(self):
        d = self.make(max_lease_s=10)
        d.grant(); d.tick()
        with self.assertLogs("jarvis.v2.speech_duck", level="WARNING"):
            for _ in range(8):                                          # renewed every 1.5 s, for ever
                self.clock.now += 1.5; d.grant(); d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM), "past the cap the duck is over")
        self.assertFalse(d.status()["on"])
        for _ in range(2):                                              # renewals during the rest are not honoured
            self.clock.now += 1.5; d.grant(); d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))
        self.clock.now += D.CAP_REST_S; d.grant(); d.tick()             # then a request starts a new period
        self.assertLess(self.pulse.volume(1)[0], NORM)
        d.release(); d.tick()


class Windows(Base):
    A, B = "a" * 32, "b" * 32

    def test_one_windows_off_cannot_end_anothers_lease(self):
        d = self.make()
        d.grant(self.A); d.grant(self.B); d.tick()
        d.release(self.A); d.tick()
        self.assertLess(self.pulse.volume(1)[0], NORM)
        self.assertTrue(d.status()["on"])
        d.release(self.B); d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_each_lease_expires_on_its_own_and_the_latest_deadline_wins(self):
        d = self.make()
        d.grant(self.A); self.clock.now += 2; d.grant(self.B); d.tick()
        self.clock.now += 2; d.tick()                                    # A is out, B has a second left
        self.assertLess(self.pulse.volume(1)[0], NORM)
        self.clock.now += 2; d.tick()
        self.assertEqual(self.pulse.volume(1), (NORM, NORM))

    def test_the_number_of_remembered_windows_is_bounded(self):
        d = self.make()
        for i in range(D.MAX_WINDOWS * 3):
            d.grant(f"{i:032x}")
        self.assertLessEqual(len(d._leases), D.MAX_WINDOWS)


class Backoff(Base):
    def test_the_idle_leftover_repair_backs_off_when_nothing_matches_and_resets_on_a_request(self):
        d = self.make(idle_s=3.0); d._load()
        d._residue = [D.Residue((NORM, NORM), (36854, 36854), time.time())]
        waits = []
        for _ in range(8):
            d.tick()
            with d._cv:
                waits.append(d._next_wait_locked())
        self.assertEqual(waits[0], 6.0)
        self.assertEqual(waits[-1], D.IDLE_MAX_S)
        self.assertEqual(waits, sorted(waits))
        d.grant(); d.release()
        with d._cv:
            self.assertEqual(d._next_wait_locked(), 3.0)

    def test_a_chrome_that_was_muted_before_is_not_ducked(self):
        from jarvis.v2.browser import host as H
        from jarvis.v2.browser.sound import BrowserSound
        host = H.BrowserHost()
        sound = BrowserSound(host, probe=lambda: A.Availability(True, True))
        host._sound = sound
        profile = "p-12345678"
        instance = H._Instance(profile, FOLDER, object(), object(), generation=1, audio=A.InstanceAudio(True))
        host._instances[profile] = instance
        sound._requests.add(profile)
        root = self.procs.owner()
        with patch.object(H, "chrome_pids", return_value=[100]), \
                patch.object(A.OwnedChrome, "pin", side_effect=lambda pid, folder, reader=None: root):
            self.assertEqual(len(sound.duck_owners()), 1)
            attempt = instance.audio.begin_mute()
            self.assertEqual(sound.duck_owners(), [], "a mute is in flight")
            instance.audio.finish_mute(attempt)
            self.assertEqual(sound.duck_owners(), [], "tainted: a mute was attempted on this instance")
        host._stopped = True


class OnlyTheOwnersDaemon(unittest.TestCase):
    def test_the_journal_belongs_to_the_owners_daemon_alone(self):
        from jarvis import config
        from jarvis.v2.daemon import Daemon
        mine = SimpleNamespace(_owns_data=lambda: True)
        other = SimpleNamespace(_owns_data=lambda: False)
        self.assertEqual(Daemon._duck_journal(mine), config.SPEECH_DUCK_PATH)
        self.assertIsNone(Daemon._duck_journal(other))


class Runner(unittest.TestCase):
    def test_nothing_but_a_listing_and_a_volume_write_is_ever_run(self):
        with patch.object(D.subprocess, "Popen") as start:
            for args in (("set-sink-input-mute", "1", "1"), ("set-sink-input-mute", "1", "0"), ("set-sink-mute", "0", "1"),
                         ("set-sink-volume", "0", "100"), ("set-default-sink", "x"), ("info",), ("--format=json", "list"),
                         ("set-sink-input-volume", "1"), ("set-sink-input-volume", "x", "100"),
                         ("set-sink-input-volume", str(2**32 - 1), "100"), ("set-sink-input-volume", "1", "-5"),
                         ("set-sink-input-volume", "1", "+5%"), ("set-sink-input-volume", "1", "100%"),
                         ("set-sink-input-volume", "1", str(D.NORM + 1)), ("set-sink-input-volume", "1", "262144"),
                         ("set-sink-input-volume", "1", *["100"] * (D.MAX_CHANNELS + 1)), ("set-sink-input-volume", "1; x", "5")):
                with self.subTest(args=args), self.assertRaises(D.DuckError):
                    D.pactl(args, 1)
            with self.assertRaises(D.DuckError):
                D.pactl(LIST, 0)
            start.assert_not_called()

    def test_the_child_gets_a_fixed_argv_and_a_curated_environment(self):
        real = subprocess.Popen
        seen = []
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "u.py"
            script.write_text("import json,os; print(json.dumps(dict(os.environ)))")

            def start(argv, **kw):
                seen.append((argv, kw["env"], kw["stdin"], kw.get("shell")))
                return real([sys.executable, str(script)], **kw)
            with patch.object(D.subprocess, "Popen", side_effect=start):
                out = D.pactl(LIST, 3)
        self.assertEqual(seen[0][0], [str(A.PACTL), *LIST])
        self.assertEqual(seen[0][1], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PULSE_SERVER": A.PULSE_SERVER})
        self.assertIsNone(seen[0][3])
        self.assertNotIn("HOME", json.loads(out))

    def test_a_slow_child_is_killed_and_reaped(self):
        real = subprocess.Popen
        procs = []

        def start(argv, **kw):
            p = real([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
            procs.append(p)
            return p
        with patch.object(D.subprocess, "Popen", side_effect=start):
            began = time.monotonic()
            with self.assertRaises(D.DuckError):
                D.pactl(LIST, 0.1)
        self.assertLess(time.monotonic() - began, 3)
        self.assertIsNotNone(procs[0].poll())

    def test_output_is_read_through_the_shared_capped_runner(self):
        real = subprocess.Popen
        with patch.object(D.subprocess, "Popen", side_effect=lambda argv, **kw: real(
                [sys.executable, "-c", "import os; os.write(1, b'x' * 300000)"], **kw)):
            with self.assertRaises(D.DuckError):
                D.pactl(LIST, 2)
        self.assertIs(D.audio._run_bounded, A._run_bounded)

    def test_a_failing_or_missing_pactl_is_a_duck_error(self):
        real = subprocess.Popen
        with patch.object(D.subprocess, "Popen", side_effect=lambda argv, **kw: real([sys.executable, "-c", "import sys; sys.exit(1)"], **kw)):
            with self.assertRaises(D.DuckError):
                D.pactl(LIST, 2)
        with patch.object(D.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaises(D.DuckError):
                D.pactl(LIST, 2)

    def test_the_listing_reduces_to_pid_mute_and_volume(self):
        rows = [stream(1, 120, (1, 2)), stream(2, None), {**stream(3, 120), "properties": {"application.process.id": "120", "media.name": "SECRET"}}]
        got = D.parse(json.dumps(rows).encode())
        self.assertEqual(got, [D.Stream(1, 120, False, (1, 2)), D.Stream(3, 120, False, (NORM, NORM))])
        for bad in ([{"index": True}], [stream(1, 120), stream(1, 121)], "x", [1]):
            with self.subTest(bad=bad), self.assertRaises(D.DuckError):
                D.parse(json.dumps(bad).encode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
