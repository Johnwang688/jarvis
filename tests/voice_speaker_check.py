"""Free checks for local speaker enrollment and verification (voice rework V12).

`jarvis/speaker.py` with a fake embedder (`tests/speaker_fakes.py`), a temporary
store and no network: the 0700 store and its hostile files, enrollment quality,
the proposed threshold, verification order (Guest first, then unenrolled, then
everything that fails closed), the engine's lazy load and idle unload, and the
pinned model. The daemon routes are `tests/v2/speaker_check.py`; the real model is
`tests/voice_speaker_real.py`.

    .venv/bin/python tests/voice_speaker_check.py
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from jarvis import config, speaker  # noqa: E402
from tests.speaker_fakes import FRIEND, OWNER, RATE, STRANGER, FakeBackend, clip, fake_engine, voice, wav  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "speaker"
        guard = patch.object(config, "SPEAKER_DIR", self.dir)
        guard.start()
        self.addCleanup(guard.stop)
        self.engine = fake_engine()
        previous = speaker.set_engine(self.engine)
        self.addCleanup(speaker.set_engine, previous)
        # Built-in voices: far-apart fundamentals, spoken by the fake synthesizer, never the real TTS.
        voices = [f"af_v{i}" for i in range(6)]
        for target, value in (("impostor_voices", lambda: voices),
                              ("synth_voice", lambda n: clip(150 + 35 * int(n[-1]), 5, seed=7))):
            g = patch.object(speaker, target, value)
            g.start()
            self.addCleanup(g.stop)

    def enroll(self, n=3, f0=OWNER, threshold=None):
        for i in range(n):
            r = speaker.add_reading(clip(f0, 10.5, seed=i))
            self.assertTrue(r["accepted"], r)
        return speaker.accept(threshold)

    def mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)


class Store(Base):
    def test_store_is_0700_and_files_0600(self):
        speaker.add_reading(clip(OWNER, 10.5))
        speaker.set_guest(True)
        self.assertEqual(self.mode(self.dir), 0o700)
        self.assertEqual(self.mode(self.dir / "readings"), 0o700)
        for f in (self.dir / "guest.json", *(self.dir / "readings").iterdir()):
            self.assertEqual(self.mode(f), 0o600, f)

    def test_the_folder_is_created_private_not_tightened_afterwards(self):
        modes = []
        real = os.mkdir

        def spy(path, mode=0o777, **kw):
            if "speaker" in str(path) or kw.get("dir_fd") is not None:     # the store and its readings, not a parent
                modes.append(mode)
            return real(path, mode, **kw)

        with patch.object(os, "mkdir", spy):
            speaker.add_reading(clip(OWNER, 10.5))
        self.assertTrue(modes and all(m == 0o700 for m in modes), modes)

    def test_a_loose_folder_is_tightened_when_used(self):
        self.dir.mkdir()
        os.chmod(self.dir, 0o755)
        speaker.set_guest(False)
        self.assertEqual(self.mode(self.dir), 0o700)

    def test_a_symlinked_folder_is_refused_and_delete_removes_only_the_link(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("mine")
        self.dir.symlink_to(target, target_is_directory=True)
        st = speaker.status()
        self.assertEqual((st["state"], st["mode"]), ("unsafe", "blocked"))
        with self.assertRaises(speaker.StoreError):
            speaker.set_guest(True)
        with self.assertRaises(speaker.StoreError):
            speaker.add_reading(clip(OWNER, 10.5))
        self.assertEqual(list(target.iterdir()), [target / "keep.txt"], "nothing was written through the link")
        with self.assertRaises(speaker.VerifyUnavailable):
            speaker.verify(clip(OWNER, 3))
        speaker.delete()
        self.assertFalse(os.path.lexists(self.dir))
        self.assertEqual((target / "keep.txt").read_text(), "mine", "the link's target is never followed")
        self.assertEqual(speaker.status()["mode"], "off")

    def test_a_folder_that_is_a_file_is_unsafe_not_a_crash(self):
        self.dir.write_text("not a folder")
        self.assertEqual(speaker.status()["state"], "unsafe")
        speaker.delete()
        self.assertFalse(os.path.lexists(self.dir))

    def hostile(self, name, make):
        """Plant `make(path)` as voiceprint.json after a good enrollment; status and verify must fail closed, fast."""
        self.enroll()
        path = self.dir / name
        path.unlink()
        make(path)
        started = time.monotonic()
        st = speaker.status()
        self.assertLess(time.monotonic() - started, 2, "a hostile file must never be waited on")
        return st

    def test_hostile_voiceprints_fail_closed(self):
        good = json.loads((self.enroll() and (self.dir / "voiceprint.json")).read_text()) if False else None
        self.enroll()
        good = json.loads((self.dir / "voiceprint.json").read_text())
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(good))

        def fifo(p):
            os.mkfifo(p)

        def link(p):
            p.symlink_to(outside)

        def directory(p):
            p.mkdir()

        def text(body):
            return lambda p: p.write_bytes(body if isinstance(body, bytes) else body.encode())

        def mutate(**changes):
            bad = dict(good)
            bad.update(changes)
            return text(json.dumps(bad))

        cases = {
            "fifo": fifo, "symlink to a good file": link, "directory": directory,
            "empty": text(""), "garbage": text("not json"), "bad utf-8": text(b"\xff\xfe{"),
            "truncated": text(json.dumps(good)[:200]),
            "deeply nested": text("[" * 200_000 + "]" * 200_000),
            "nested object": text('{"a":' * 100_000 + "1" + "}" * 100_000),
            "oversize": text(json.dumps(dict(good, pad="x" * (speaker._FILE_CAP + 10)))),
            "huge int": text('{"version":1,"model":"m","dim":' + "9" * 5000 + "}"),
            "infinity": text(json.dumps(good).replace(str(good["centroid"][0]), "1e999", 1)),
            "NaN": text(json.dumps(good).replace(str(good["centroid"][0]), "NaN", 1)),
            "wrong dim": mutate(dim=good["dim"] + 1), "bool dim": mutate(dim=True),
            "zero centroid": mutate(centroid=[0.0] * good["dim"]),
            "string centroid": mutate(centroid=["a"] * good["dim"]),
            "threshold too low": mutate(threshold=0.0), "threshold too high": mutate(threshold=2.0),
            "threshold string": mutate(threshold="0.5"), "threshold bool": mutate(threshold=True),
            "version 2": mutate(version=2), "model list": mutate(model=["x"]),
            "readings negative": mutate(readings=-1), "a list": text("[1,2,3]"),
        }
        for label, make in cases.items():
            with self.subTest(label):
                st = self.hostile("voiceprint.json", make)
                self.assertEqual(st["state"], "damaged", label)
                self.assertEqual(st["mode"], "blocked", label)
                self.assertFalse(st["enrolled"])
                with self.assertRaises(speaker.VerifyUnavailable):
                    speaker.verify(clip(OWNER, 3, seed=9))
                before = FakeBackend.calls
                with self.assertRaises(speaker.VerifyUnavailable):
                    speaker.verify(clip(FRIEND, 3, seed=9))
                self.assertEqual(FakeBackend.calls, before, "a damaged voiceprint embeds nothing")
                # Guest voice is the owner's way through; Delete is the other.
                speaker.set_guest(True)
                self.assertEqual(speaker.verify(clip(FRIEND, 3)), "skipped_guest")
                speaker.set_guest(False)
                speaker.delete()
                self.assertEqual(speaker.status()["mode"], "off")
                self.enroll()                                  # fresh good store for the next case
                self.assertEqual(speaker.status()["mode"], "on")

    def test_a_pipe_holding_good_json_is_not_a_file(self):
        """A FIFO holding perfectly good JSON is still refused: only regular files are read."""
        self.enroll()
        good = (self.dir / "voiceprint.json").read_bytes()
        held = []

        def feed(path, data):
            if os.path.lexists(path):
                os.unlink(path)
            os.mkfifo(path)
            reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)         # so the writer can open at once
            writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            os.write(writer, data)
            os.close(writer)                      # the bytes wait in the pipe, and a reader reaches EOF after them
            held.append(reader)

        self.addCleanup(lambda: [os.close(fd) for fd in held])
        feed(self.dir / "voiceprint.json", good)
        self.assertEqual(speaker.status()["state"], "damaged")
        with self.assertRaises(speaker.VerifyUnavailable):
            speaker.verify(clip(OWNER, 3, seed=9))
        # The same for the Guest switch: a pipe saying {"guest": true} does not turn it on.
        speaker.delete()
        self.enroll()
        feed(self.dir / "guest.json", b'{"guest": true}')
        self.assertFalse(speaker.status()["guest"])
        with self.assertRaises(speaker.NotOwner):
            speaker.verify(clip(FRIEND, 3))

    def test_hostile_guest_file_means_guest_off(self):
        self.enroll()
        guest = self.dir / "guest.json"
        for label, make in {"fifo": lambda p: os.mkfifo(p), "garbage": lambda p: p.write_text("{"),
                            "string true": lambda p: p.write_text('{"guest": "true"}'),
                            "int one": lambda p: p.write_text('{"guest": 1}'),
                            "list": lambda p: p.write_text("[true]"),
                            "deep": lambda p: p.write_text("[" * 100_000),
                            "link": lambda p: p.symlink_to(self.root / "nope"),
                            "big": lambda p: p.write_text(" " * 10_000 + '{"guest": true}')}.items():
            with self.subTest(label):
                if os.path.lexists(guest):
                    guest.unlink()
                make(guest)
                self.assertFalse(speaker.status()["guest"], label)
                with self.assertRaises(speaker.NotOwner):
                    speaker.verify(clip(FRIEND, 3))

    def test_hostile_reading_files_are_ignored(self):
        self.enroll()
        readings = self.dir / "readings"
        os.mkfifo(readings / "r-050.wav")
        (readings / "r-051.wav").symlink_to(self.root / "nope")
        (readings / "r-052.wav").mkdir()
        (readings / "r-053.wav").write_bytes(b"RIFFjunk")
        (readings / "notes.txt").write_text("x")
        # (a junk regular file with a reading's name counts as a file; it is skipped when read)
        self.assertEqual(speaker.status()["readings"], 4)
        started = time.monotonic()
        self.assertEqual(len(speaker._load_readings()), 3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(speaker.accept()["mode"], "on")

    def test_a_readings_folder_that_is_a_link_is_refused(self):
        speaker.add_reading(clip(OWNER, 10.5))
        shutil_target = self.root / "away"
        shutil_target.mkdir()
        (self.dir / "readings").rename(self.root / "moved")
        (self.dir / "readings").symlink_to(shutil_target, target_is_directory=True)
        with self.assertRaises(speaker.StoreError):
            speaker.add_reading(clip(OWNER, 10.5, seed=2))
        self.assertEqual(list(shutil_target.iterdir()), [])

    def test_delete_removes_everything(self):
        self.enroll()
        speaker.set_guest(True)
        (self.dir / "readings" / "stray.tmp").write_text("x")
        (self.dir / "extra").mkdir()
        (self.dir / "extra" / "deep").write_text("x")
        outside = self.root / "outside.txt"
        outside.write_text("mine")
        (self.dir / "link").symlink_to(outside)
        os.mkfifo(self.dir / "pipe")
        st = speaker.delete()
        self.assertFalse(os.path.lexists(self.dir), "the folder and every file in it are gone")
        self.assertEqual((st["state"], st["mode"], st["readings"], st["guest"], st["enrolled"]),
                         ("not_enrolled", "off", 0, False, False))
        self.assertEqual(outside.read_text(), "mine")
        self.assertIsNone(st["last"])
        self.assertEqual(speaker.delete()["state"], "not_enrolled")      # idempotent

    def test_no_partial_file_is_left_behind(self):
        self.enroll()
        names = {p.name for p in self.dir.rglob("*")}
        self.assertFalse([n for n in names if n.endswith(".tmp")], names)

    def test_writes_are_atomic_a_failed_write_keeps_the_old_file(self):
        self.enroll()
        before = (self.dir / "voiceprint.json").read_bytes()
        with patch.object(os, "rename", side_effect=OSError("boom")):
            with self.assertRaises(speaker.StoreError):
                speaker.set_threshold(0.5)
        self.assertEqual((self.dir / "voiceprint.json").read_bytes(), before)
        self.assertFalse([p for p in self.dir.iterdir() if p.name.endswith(".tmp")])

    def test_status_carries_no_audio_and_no_embedding(self):
        self.enroll()
        vp = json.loads((self.dir / "voiceprint.json").read_text())
        text = json.dumps(speaker.status())
        for number in vp["centroid"][:5]:
            self.assertNotIn(repr(number), text)
        self.assertNotIn("centroid", text)
        self.assertNotIn("RIFF", text)


class Protected(Base):
    """V-12b (owner, 2026-10-14): `voiceprint.json` and `guest.json` switch the voice check off, so no
    agent write, shell command or grant may change them. The readings and the cache beside them stay ordinary."""

    def test_the_two_files_are_protected_state_and_the_rest_is_not(self):
        from jarvis.tools import files
        protected = files._protected_state()
        for name in ("voiceprint.json", "guest.json"):
            self.assertIn((self.dir / name).resolve(), protected, name)
        for other in ("impostors.json", "readings/r-000.wav", "notes.txt"):
            self.assertNotIn((self.dir / other).resolve(), protected, other)

    def test_shell_detector_does_not_allow_writes_to_them(self):
        from jarvis import permissions, rules
        for target in (self.dir / "guest.json", self.dir / "voiceprint.json"):
            for command in (f"cp /tmp/x {target}", f"echo '{{}}' > {target}", f"sh -c 'cp /tmp/x {target}'",
                            f"cd {self.dir} && cp /tmp/x {target.name}", f"rm {target}", f"mv /tmp/x {target}"):
                verdict = permissions.static_verdict(command)
                self.assertNotEqual(verdict.decision, rules.ALLOW, f"{command}: {verdict}")
        # Deleting the folder holds both files, so it is not a free pass either.
        for command in (f"rm -rf {self.dir}", f"cd {self.dir.parent} && rm -rf {self.dir.name}"):
            self.assertNotEqual(permissions.static_verdict(command).decision, rules.ALLOW, command)

    def test_v2_layer_one_refuses_them_and_says_whose_they_are(self):
        from jarvis.v2 import permissions as v2
        for name in ("voiceprint.json", "guest.json"):
            target = self.dir / name
            self.assertIn(target.resolve(), v2.protected_paths())
            reason = v2.denied_file(str(target))
            self.assertTrue(reason, name)
            self.assertIn("speaker verification", reason)
            self.assertIn("Settings > Voice", reason)
            self.assertNotIn("permission state", reason)
            self.assertTrue(v2.denied_command(f"cp /tmp/x {target}"))
            self.assertTrue(v2._command_writes_protected_state(f"cp /tmp/x {target}"))
            self.assertIsNone(v2._command_writes_protected_state(f"cp {target} /tmp/backup"), "a read is not a write")
        for name in ("impostors.json", "readings/r-000.wav"):
            self.assertIsNone(v2.denied_file(str(self.dir / name)), name)
        # The folder is the ancestor of both files: a write grant on it (or on a parent that holds it) is refused,
        # so deleting the folder is covered too. A read grant is not a write.
        for ancestor in (self.dir, self.dir.parent):
            self.assertIn("permission state", v2.denied_grant(str(ancestor), "write") or "", ancestor)
        self.assertIsNone(v2.denied_grant(str(self.dir / "readings"), "write"))


class Enrollment(Base):
    def test_quality_checks(self):
        cases = {
            "too short": clip(OWNER, 3.0),
            "too long": clip(OWNER, 31.0),
            "quiet": clip(OWNER, 10.5, level=0.002, noise=0.0),
            "loud": clip(OWNER, 10.5, level=1.0),
            "silence": wav(np.zeros(int(10.5 * RATE), dtype=np.float32)),
            "mostly silence": wav(np.concatenate([voice(OWNER, 2.0), np.zeros(int(9 * RATE), dtype=np.float32)])),
        }
        clipped = voice(OWNER, 10.5, level=1.0) * 4
        cases["clipping"] = wav(clipped)
        for label, audio in cases.items():
            with self.subTest(label):
                r = speaker.add_reading(audio)
                self.assertFalse(r["accepted"], (label, r))
                self.assertTrue(r["problems"])
        self.assertEqual(speaker.status()["readings"], 0, "a failed reading is never stored")
        self.assertTrue(speaker.add_reading(clip(OWNER, 10.5))["accepted"])

    def test_hostile_audio_is_refused_and_stores_nothing(self):
        stereo = io.BytesIO()
        with wave.open(stereo, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"\x00\x01" * 4)
        eight = io.BytesIO()
        with wave.open(eight, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(RATE)
            w.writeframes(b"\x80" * 1000)
        slow = io.BytesIO()
        with wave.open(slow, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(96000)
            w.writeframes(b"\x00\x00" * 100)
        header = clip(OWNER, 1.0)
        for label, audio in {"not riff": b"OggS" + b"\0" * 100, "empty": b"", "truncated": header[:30],
                             "8-bit": eight.getvalue(), "96 kHz": slow.getvalue(),
                             "too big": b"RIFF" + b"\0" * (speaker.MAX_READING_BYTES + 1)}.items():
            with self.subTest(label):
                with self.assertRaises(speaker.EnrollError):
                    speaker.add_reading(audio)
        with self.assertRaises(speaker.EnrollError):
            speaker.add_reading(clip(OWNER, 10.5), "audio/webm")
        # A header that claims far more audio than the body holds is only as long as the body.
        lying = header[:40] + (0x7FFFFFF0).to_bytes(4, "little") + header[44:]
        self.assertFalse(speaker.add_reading(lying)["accepted"])
        self.assertFalse(self.dir.exists() and list((self.dir / "readings").iterdir()))

    def test_what_is_stored_is_our_own_encoding(self):
        speaker.add_reading(clip(OWNER, 10.5))
        (stored,) = (self.dir / "readings").iterdir()
        with wave.open(str(stored)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, RATE))

    def test_readings_are_bounded(self):
        for i in range(speaker.READINGS_MAX):
            self.assertTrue(speaker.add_reading(clip(OWNER, 10.5, seed=i))["accepted"])
        with self.assertRaises(speaker.EnrollError):
            speaker.add_reading(clip(OWNER, 10.5, seed=99))
        self.assertEqual(len(list((self.dir / "readings").iterdir())), speaker.READINGS_MAX)

    def test_propose_stores_nothing_and_needs_three(self):
        for i in range(2):
            speaker.add_reading(clip(OWNER, 10.5, seed=i))
        with self.assertRaises(speaker.EnrollError):
            speaker.propose()
        speaker.add_reading(clip(OWNER, 10.5, seed=2))
        before = {p.name for p in self.dir.rglob("*") if p.name != "impostors.json"}
        p = speaker.propose()
        self.assertFalse((self.dir / "voiceprint.json").exists())
        self.assertEqual(before, {q.name for q in self.dir.rglob("*") if q.name != "impostors.json"})
        self.assertEqual(speaker.status()["mode"], "off", "verification switches on only after the owner accepts")
        self.assertEqual(p["genuine"]["n"], 9)
        self.assertEqual(p["impostors"]["n"], 6)
        self.assertGreater(p["genuine"]["p10"], p["impostors"]["max"])
        self.assertTrue(p["impostors"]["max"] < p["threshold"] < p["genuine"]["p10"], p)
        self.assertFalse(p["overlap"])
        self.assertEqual((p["false_reject"], p["false_accept"]), (0.0, 0.0))
        self.assertEqual(sum(p["genuine"]["hist"]), 9)
        self.assertEqual(sum(p["impostors"]["hist"]), 6)
        self.assertEqual(len(p["impostors"]["closest"]), 3)
        self.assertNotIn("centroid", json.dumps(p))

    def test_a_proposal_that_needs_confirming_is_not_accepted_without_anyway(self):
        with patch.object(speaker, "synth_voice", lambda n: clip(OWNER, 5, seed=0)):
            for i in range(3):
                speaker.add_reading(clip(OWNER, 10.5, seed=i))
            self.assertTrue(speaker.propose()["confirm"])
            with self.assertRaises(speaker.EnrollError) as raised:
                speaker.accept()
            self.assertIn("record the readings again", str(raised.exception))
            with self.assertRaises(speaker.EnrollError):
                speaker.accept(0.6)                       # a number typed by the owner does not skip the warning
            self.assertFalse((self.dir / "voiceprint.json").exists())
            st = speaker.accept(0.6, anyway=True)
        self.assertEqual((st["mode"], st["threshold"]), ("on", 0.6))
        self.assertGreater(json.loads((self.dir / "voiceprint.json").read_text())["impostor_max"], 0.9)

    def test_accept_records_the_closest_built_in_voice(self):
        self.enroll()
        vp = json.loads((self.dir / "voiceprint.json").read_text())
        self.assertIsNotNone(vp["impostor_max"])
        self.assertAlmostEqual(vp["impostor_max"], speaker.propose()["impostors"]["max"], places=3)

    def test_a_typed_threshold_may_go_down_to_the_minimum_with_a_warning(self):
        self.enroll()
        self.assertEqual(speaker.status()["threshold_warning"], "")
        st = speaker.set_threshold(0.35)
        self.assertEqual(st["threshold"], 0.35)
        self.assertIn("below 0.50", st["threshold_warning"])
        st = speaker.set_threshold(0.50)
        self.assertEqual(st["threshold_warning"], "")
        self.assertEqual(speaker.status()["proposal_floor"], config.SPEAKER_PROPOSAL_FLOOR)
        st = speaker.accept(0.31)                            # also at Accept, when the readings are clear
        self.assertIn("below 0.50", st["threshold_warning"])
        with self.assertRaises(ValueError):
            speaker.set_threshold(0.29)

    def test_propose_warns_when_a_built_in_voice_sounds_like_the_owner(self):
        with patch.object(speaker, "synth_voice", lambda n: clip(OWNER, 5, seed=0)):
            for i in range(3):
                speaker.add_reading(clip(OWNER, 10.5, seed=i))
            p = speaker.propose()
        self.assertTrue(p["overlap"] and p["confirm"])
        self.assertIn("alike", p["warning"])
        self.assertGreaterEqual(p["threshold"], min(p["impostors"]["max"] + 0.01, config.SPEAKER_THRESHOLD_MAX))

    def test_propose_without_built_in_voices_says_so(self):
        with patch.object(speaker, "impostor_voices", lambda: []):
            for i in range(3):
                speaker.add_reading(clip(OWNER, 10.5, seed=i))
            p = speaker.propose()
            self.assertEqual(p["impostors"]["n"], 0)
            self.assertIn("no built-in voice", p["warning"])
            self.assertLessEqual(p["threshold"], config.SPEAKER_THRESHOLD_DEFAULT)
            speaker.accept(0.6)                                      # the voiceprint knows no closest voice
        self.assertIsNone(json.loads((self.dir / "voiceprint.json").read_text())["impostor_max"])

    def test_impostor_embeddings_are_cached_per_model(self):
        for i in range(3):
            speaker.add_reading(clip(OWNER, 10.5, seed=i))
        calls = []
        real = speaker.synth_voice
        with patch.object(speaker, "synth_voice", lambda n: calls.append(n) or real(n)):
            speaker.propose()
            first = len(calls)
            speaker.propose()
        self.assertEqual((first, len(calls)), (6, 6), "the second proposal reuses the first's built-in voices")
        # A different model id cannot reuse them.
        self.engine.model = "another_model"
        with patch.object(speaker, "synth_voice", lambda n: calls.append(n) or real(n)):
            speaker.propose()
        self.assertEqual(len(calls), 12)

    def test_a_voice_that_will_not_speak_is_skipped(self):
        def flaky(name):
            if name == "af_v2":
                raise RuntimeError("no such voice /secret/path")
            return clip(150 + 35 * int(name[-1]), 5, seed=7)

        for i in range(3):
            speaker.add_reading(clip(OWNER, 10.5, seed=i))
        with patch.object(speaker, "synth_voice", flaky):
            self.assertEqual(speaker.propose()["impostors"]["n"], 5)

    def test_accept_builds_the_voiceprint_and_switches_verification_on(self):
        st = self.enroll()
        self.assertEqual((st["mode"], st["state"], st["enrolled"]), ("on", "enrolled", True))
        vp = json.loads((self.dir / "voiceprint.json").read_text())
        self.assertEqual((vp["model"], vp["readings"], vp["windows"], vp["dim"]), (speaker.MODEL_NAME, 3, 9, 24))
        self.assertTrue(config.SPEAKER_THRESHOLD_MIN <= vp["threshold"] <= config.SPEAKER_THRESHOLD_MAX)
        self.assertEqual(st["threshold"], round(vp["threshold"], 3))

    def test_accept_with_a_chosen_threshold_and_bounds(self):
        st = self.enroll(threshold=0.55)
        self.assertEqual(st["threshold"], 0.55)
        for bad in (0.0, 0.29, 0.91, 1, -1, float("nan"), float("inf"), "0.5", True, None.__class__):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    speaker.set_threshold(bad)
        self.assertEqual(speaker.set_threshold(0.7)["threshold"], 0.7)
        self.assertEqual(speaker.status()["threshold"], 0.7)

    def test_set_threshold_needs_a_voiceprint(self):
        with self.assertRaises(speaker.EnrollError):
            speaker.set_threshold(0.5)

    def test_accept_needs_three_readings_and_an_engine(self):
        speaker.add_reading(clip(OWNER, 10.5))
        with self.assertRaises(speaker.EnrollError):
            speaker.accept()
        speaker.set_engine(None)
        with patch.object(speaker, "installed", return_value=(False, "the sherpa-onnx package is not installed")):
            with self.assertRaises(speaker.EnrollError):
                speaker.accept(0.5)

    def test_a_new_model_rebuilds_from_the_kept_readings(self):
        """Q12: the readings stay in the store so an upgrade can re-derive the voiceprint."""
        self.enroll()
        self.engine.model = "titanet_large"
        st = speaker.status()
        self.assertEqual((st["state"], st["mode"], st["enrolled"]), ("stale", "blocked", False))
        with self.assertRaises(speaker.VerifyUnavailable):
            speaker.verify(clip(OWNER, 3, seed=5))
        self.assertEqual(speaker.accept(0.6)["mode"], "on")
        self.assertEqual(json.loads((self.dir / "voiceprint.json").read_text())["model"], "titanet_large")
        self.assertEqual(speaker.verify(clip(OWNER, 3, seed=5)), "matched")


class Threshold(unittest.TestCase):
    """`choose_threshold`: the rules of the proposal, pinned case by case (M2)."""

    def pick(self, g10, top):
        return speaker.choose_threshold(g10, top)

    def test_halfway_between_the_closest_voice_and_the_owners_weakest(self):
        got = self.pick(0.95, 0.20)
        self.assertAlmostEqual(got["threshold"], 0.575)
        self.assertFalse(got["confirm"])
        self.assertEqual(got["warning"], "")

    def test_never_nearer_the_owner_than_a_tenth_below_the_weakest_reading(self):
        # halfway would be 0.70; the margin says 0.68 (and 0.64 is the floor above the impostors)
        got = self.pick(0.78, 0.62)
        self.assertAlmostEqual(got["threshold"], 0.68)
        # and the margin loses to "above the closest voice": halfway 0.75, margin 0.70, closest + 0.02 = 0.72
        self.assertAlmostEqual(self.pick(0.80, 0.70)["threshold"], 0.72)
        # a wide gap is halfway, not the margin
        self.assertAlmostEqual(self.pick(0.86, 0.60)["threshold"], 0.73)

    def test_overlap_goes_just_above_the_closest_voice_and_needs_confirming(self):
        got = self.pick(0.70, 0.74)
        self.assertAlmostEqual(got["threshold"], 0.76)
        self.assertTrue(got["confirm"] and got["overlap"])
        self.assertIn("score alike", got["warning"])
        self.assertIn("record the readings again", got["warning"])
        self.assertEqual(self.pick(0.95, 0.95)["threshold"], config.SPEAKER_THRESHOLD_MAX)      # clamped

    def test_the_proposal_floor(self):
        # Halfway would be 0.325: raised to the 0.50 floor, and the owner has to accept anyway.
        got = self.pick(0.55, 0.10)
        self.assertEqual(got["threshold"], config.SPEAKER_PROPOSAL_FLOOR)
        self.assertTrue(got["confirm"])
        self.assertIn("raised to 0.50", got["warning"])
        self.assertIn("record the readings again", got["warning"])
        # Exactly at the floor is fine.
        got = self.pick(0.70, 0.30)
        self.assertAlmostEqual(got["threshold"], 0.50)
        self.assertFalse(got["confirm"])
        # Just under is not.
        self.assertTrue(self.pick(0.69, 0.30)["confirm"])
        self.assertEqual(self.pick(0.69, 0.30)["threshold"], config.SPEAKER_PROPOSAL_FLOOR)

    def test_readings_that_match_each_other_weakly_need_confirming_even_when_far_from_every_voice(self):
        got = self.pick(0.59, 0.01)
        self.assertTrue(got["confirm"])
        self.assertIn("match each other weakly", got["warning"])
        self.assertFalse(self.pick(0.61, 0.01)["warning"].count("weakly"))
        self.assertTrue(self.pick(0.61, 0.01)["confirm"], "still below the floor: halfway is 0.31")

    def test_no_built_in_voice_scored(self):
        got = self.pick(0.90, None)
        self.assertAlmostEqual(got["threshold"], 0.60)          # min(default 0.60, 0.85)
        self.assertIn("no built-in voice", got["warning"])
        self.assertFalse(got["confirm"])
        self.assertAlmostEqual(self.pick(0.62, None)["threshold"], 0.57)            # g10 - 0.05 wins
        self.assertFalse(self.pick(0.62, None)["confirm"])
        low = self.pick(0.58, None)                             # weak readings: confirm, above the floor
        self.assertTrue(low["confirm"])
        self.assertAlmostEqual(low["threshold"], 0.53)
        floor = self.pick(0.52, None)                           # the floor applies here too
        self.assertEqual(floor["threshold"], config.SPEAKER_PROPOSAL_FLOOR)
        self.assertTrue(floor["confirm"])
        self.assertIn("raised to 0.50", floor["warning"])

    def test_the_ceiling(self):
        self.assertEqual(self.pick(1.0, 0.99)["threshold"], config.SPEAKER_THRESHOLD_MAX)


class ShortClips(unittest.TestCase):
    """L1: a command's relaxed threshold never drops to a built-in voice."""

    def vp(self, imax, threshold=0.70):
        return speaker.Voiceprint("m", 2, [1.0, 0.0], threshold, 3, 9, "", imax)

    def test_relaxed_unless_a_built_in_voice_is_closer(self):
        relax = config.SPEAKER_SHORT_RELAX
        self.assertAlmostEqual(speaker.effective_threshold(self.vp(None), 1.0), 0.70 - relax)
        self.assertAlmostEqual(speaker.effective_threshold(self.vp(0.30), 1.0), 0.70 - relax)
        self.assertAlmostEqual(speaker.effective_threshold(self.vp(0.66), 1.0), 0.68)     # closest + 0.02
        self.assertAlmostEqual(speaker.effective_threshold(self.vp(0.69), 1.0), 0.70)     # never above the full one
        self.assertAlmostEqual(speaker.effective_threshold(self.vp(0.95), 1.0), 0.70)
        self.assertAlmostEqual(speaker.effective_threshold(self.vp(0.66), 1.2), 0.70)     # not a command

    def test_accept_stores_it_and_a_bad_one_is_damage(self):
        for bad in ("high", True, 7, [0.5]):
            raw = {"version": 1, "model": "m", "dim": 2, "centroid": [1.0, 0.0], "threshold": 0.6, "readings": 3,
                   "windows": 9, "created": "", "impostor_max": bad}
            self.assertIsNone(speaker._parse_voiceprint(raw), bad)
        raw["impostor_max"] = None
        self.assertIsNone(speaker._parse_voiceprint(raw).impostor_max)
        del raw["impostor_max"]
        self.assertIsNone(speaker._parse_voiceprint(raw).impostor_max)


class Verification(Base):
    def test_unenrolled_skips_and_never_embeds(self):
        self.assertEqual(speaker.verify(clip(FRIEND, 3)), "skipped_unenrolled")
        self.assertEqual(speaker.verify(b"not even audio", "audio/webm"), "skipped_unenrolled")
        self.assertEqual(FakeBackend.calls, 0)
        self.assertEqual(self.engine.loads, 0)

    def test_enrolling_but_not_accepted_still_skips(self):
        for i in range(3):
            speaker.add_reading(clip(OWNER, 10.5, seed=i))
        self.assertEqual(speaker.verify(clip(FRIEND, 3)), "skipped_unenrolled")

    def test_owner_matches_and_others_are_refused(self):
        self.enroll()
        self.assertEqual(speaker.verify(clip(OWNER, 3, seed=21)), "matched")
        self.assertEqual(speaker.verify(clip(OWNER, 12, seed=22)), "matched", "only the first 3 s are embedded")
        for other in (FRIEND, STRANGER):
            with self.assertRaises(speaker.NotOwner) as raised:
                speaker.verify(clip(other, 3, seed=23))
            self.assertEqual(str(raised.exception), "not the enrolled speaker")
        self.assertEqual(speaker.status()["last"]["result"], "rejected")

    def test_a_clip_with_too_little_speech_is_not_judged_and_not_a_refusal(self):
        """L2: no_speech, so the daemon answers an empty text and transcribes nothing; nobody is called a stranger."""
        self.enroll()
        before = FakeBackend.calls
        for audio in (wav(np.zeros(RATE * 2, dtype=np.float32)), clip(OWNER, 0.1), clip(OWNER, 0.3, pauses=False)):
            self.assertEqual(speaker.verify(audio), "no_speech")
        self.assertEqual(FakeBackend.calls, before)
        self.assertEqual(speaker.status()["last"]["result"], "no_speech")
        self.assertEqual(speaker.MIN_VOICED_S, 0.35, "the HUD's own too_short (350 ms)")
        self.assertEqual(speaker.verify(clip(OWNER, 0.45, pauses=False)), "matched", "a clip just over it is judged")

    def test_only_the_first_three_seconds_are_embedded(self):
        self.enroll()
        seen = []
        real = FakeBackend.embed

        def spy(self_, samples, rate):
            seen.append(len(samples) / rate)
            return real(self_, samples, rate)

        with patch.object(FakeBackend, "embed", spy):
            speaker.verify(clip(OWNER, 20, seed=30))
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], speaker.WINDOW_S + 0.001)

    def test_leading_silence_does_not_use_up_the_window(self):
        self.enroll()
        padded = wav(np.concatenate([np.zeros(RATE * 2, dtype=np.float32), voice(OWNER, 4, seed=31)]))
        self.assertEqual(speaker.verify(padded), "matched")

    def test_a_short_command_gets_the_relaxed_threshold(self):
        self.enroll()
        speaker.set_threshold(speaker.config.SPEAKER_THRESHOLD_MIN)
        near = 100.0          # a voice that scores about 0.7 against the owner's
        speaker.verify(clip(near, 1.0, seed=40))
        score = speaker.status()["last"]["score"]
        self.assertTrue(0.5 < score < 0.85, score)
        # Just above this clip's own score: refused as a long clip, accepted as a short one.
        speaker.set_threshold(round(score + 0.04, 3))
        self.assertEqual(speaker.verify(clip(near, 1.0, seed=40)), "matched")
        # The same audio, reported as a clip of 2 s (not a command), gets no relaxation.
        real = speaker.decode_wav

        def longer(audio, mime, **kw):
            x, rate, _ = real(audio, mime, **kw)
            return x, rate, 2.0

        with patch.object(speaker, "decode_wav", longer):
            with self.assertRaises(speaker.NotOwner):
                speaker.verify(clip(near, 1.0, seed=40))
        vp = speaker.read_state().voiceprint
        self.assertAlmostEqual(speaker.effective_threshold(vp, 1.19), vp.threshold - config.SPEAKER_SHORT_RELAX)
        self.assertEqual(speaker.effective_threshold(vp, 1.2), vp.threshold)

    def test_guest_voice_skips_verification_and_embeds_nothing(self):
        self.enroll()
        speaker.set_guest(True)
        before = FakeBackend.calls
        self.assertEqual(speaker.verify(clip(FRIEND, 3)), "skipped_guest")
        self.assertEqual(speaker.verify(clip(STRANGER, 3)), "skipped_guest")
        self.assertEqual(FakeBackend.calls, before, "Guest voice has no score to record")
        self.assertIsNone(speaker.status()["last"]["score"])
        speaker.set_guest(False)
        with self.assertRaises(speaker.NotOwner):
            speaker.verify(clip(FRIEND, 3))

    def test_guest_voice_persists_until_turned_off(self):
        """It is a file beside the voiceprint: a fresh read (a restarted daemon) sees it, no timer ends it."""
        self.enroll()
        speaker.set_guest(True)
        self.assertEqual(json.loads((self.dir / "guest.json").read_text()), {"guest": True})
        with patch("time.time", return_value=time.time() + 30 * 86400), \
                patch("time.monotonic", return_value=time.monotonic() + 30 * 86400):
            self.assertTrue(speaker.status()["guest"])
            self.assertEqual(speaker.verify(clip(FRIEND, 3)), "skipped_guest")
        subprocess_view = subprocess.run(
            [sys.executable, "-c", "from jarvis import speaker; import json; print(json.dumps(speaker.status()['guest']))"],
            env={**os.environ, "PYTHONPATH": str(REPO), "JARVIS_SPEAKER_DIR": str(self.dir)},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(subprocess_view.stdout.strip(), "true", subprocess_view.stderr)

    def test_guest_before_enrollment_is_stored_and_label_state_is_true(self):
        st = speaker.set_guest(True)
        self.assertEqual((st["guest"], st["mode"], st["enrolled"]), (True, "guest", False))
        self.assertEqual(speaker.verify(clip(FRIEND, 3)), "skipped_guest")

    def test_guest_must_be_a_boolean(self):
        for bad in ("true", 1, None, [True]):
            with self.assertRaises(ValueError):
                speaker.set_guest(bad)

    def test_enrolled_with_no_engine_fails_closed(self):
        self.enroll()
        speaker.set_engine(None)
        with patch.object(speaker, "installed", return_value=(False, "the speaker model is not downloaded")):
            st = speaker.status()
            self.assertEqual(st["mode"], "blocked")
            with self.assertRaises(speaker.VerifyUnavailable) as raised:
                speaker.verify(clip(OWNER, 3))
        self.assertIn("not downloaded", str(raised.exception))

    def test_enrolled_with_audio_it_cannot_decode_fails_closed(self):
        self.enroll()
        for audio, mime in ((b"\x1aE\xdf\xa3webm", "audio/webm"), (clip(OWNER, 3), "audio/webm"),
                            (b"RIFF" + b"\0" * 20, "audio/wav"), (b"", "audio/wav")):
            with self.subTest(mime=mime):
                with self.assertRaises(speaker.VerifyUnsupported):
                    speaker.verify(audio, mime)
        big = b"RIFF" + b"\0" * speaker.MAX_VERIFY_WAV_BYTES
        with self.assertRaises(speaker.VerifyUnsupported):
            speaker.verify(big, "audio/wav")

    def test_an_embedder_that_fails_or_lies_fails_closed_without_leaking_its_text(self):
        self.enroll()

        class Boom(FakeBackend):
            def embed(self, samples, rate):
                raise RuntimeError("/home/johnw/secret/model.onnx exploded")

        speaker.set_engine(speaker.Engine(lambda t: Boom(), schedule=False))
        with self.assertRaises(speaker.VerifyUnavailable) as raised:
            speaker.verify(clip(OWNER, 3))
        self.assertNotIn("secret", str(raised.exception))
        for lie in ([], [float("nan")] * 24, [0.0] * 24, "abc", None, [1.0] * 5000, [1.0]):
            class Liar(FakeBackend):
                def embed(self, samples, rate, lie=lie):
                    return lie

            speaker.set_engine(speaker.Engine(lambda t: Liar(), schedule=False))
            with self.subTest(lie=str(lie)[:20]):
                with self.assertRaises(speaker.VerifyUnavailable):
                    speaker.verify(clip(OWNER, 3))
        # A vector of the wrong length is not compared against the voiceprint.
        class Short(FakeBackend):
            def embed(self, samples, rate):
                return [1.0, 2.0, 3.0]

        speaker.set_engine(speaker.Engine(lambda t: Short(), schedule=False))
        with self.assertRaises(speaker.VerifyUnavailable):
            speaker.verify(clip(OWNER, 3))

    def test_status_never_loads_the_model(self):
        self.enroll()
        loads = self.engine.loads
        speaker.status()
        speaker.status()
        self.assertEqual(self.engine.loads, loads)


class VoiceWarnings(Base):
    """L3: choosing a voice that could pass for the owner is said so."""

    def test_a_voice_that_sounds_like_the_owner_is_named(self):
        self.enroll()
        with patch.object(speaker, "synth_voice", lambda n: clip(OWNER, 5, seed=4)):
            warning = speaker.voice_warning("af_twin")
        self.assertIn("af_twin sounds like you", warning)
        self.assertIn("Settings > Voice", warning)
        # (a near miss inside the margin counts too)
        with patch.object(speaker, "synth_voice", lambda n: clip(100.0, 5, seed=4)):
            self.assertIsNotNone(speaker.voice_warning("af_near"))

    def test_a_different_voice_is_fine_and_is_cached(self):
        self.enroll()
        calls = []
        far = lambda n: calls.append(n) or clip(STRANGER, 5, seed=4)      # noqa: E731
        with patch.object(speaker, "synth_voice", far):
            self.assertIsNone(speaker.voice_warning("af_far"))
            self.assertIsNone(speaker.voice_warning("af_far"))
        self.assertEqual(calls, ["af_far"], "spoken once, then read from the cache beside the voiceprint")

    def test_nothing_is_said_when_there_is_nothing_to_check(self):
        twin = lambda n: clip(OWNER, 5, seed=4)                         # noqa: E731
        with patch.object(speaker, "synth_voice", twin):
            self.assertIsNone(speaker.voice_warning("af_twin"))          # nobody enrolled
            self.enroll()
            speaker.set_guest(True)
            self.assertIsNone(speaker.voice_warning("af_twin"))          # the check is off anyway
            speaker.set_guest(False)
            self.assertIsNotNone(speaker.voice_warning("af_twin"))
        with patch.object(speaker, "synth_voice", lambda n: (_ for _ in ()).throw(RuntimeError("no tts"))):
            self.assertIsNone(speaker.voice_warning("af_broken"))        # a voice that will not speak never blocks the choice
        self.engine.model = "another"
        with patch.object(speaker, "synth_voice", twin):
            self.assertIsNone(speaker.voice_warning("af_twin2"))         # a stale voiceprint cannot be compared


class ModelFile(unittest.TestCase):
    """L4: the model is opened O_NOFOLLOW, must be a small regular file, and is hashed before it is loaded."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.body = b"pretend model " * 100
        import hashlib

        self.sha = hashlib.sha256(self.body).hexdigest()
        self.path = self.tmp / "m.onnx"
        self.path.write_bytes(self.body)

    def test_a_good_file_is_handed_out_as_the_bytes_that_were_hashed(self):
        with speaker.verified_model(self.path, self.sha) as handed:
            self.assertEqual(Path(handed).read_bytes(), self.body)
            self.assertTrue(handed.startswith("/proc/self/fd/"))
            self.path.write_bytes(b"rewritten in place after the check")   # neither a rewrite ...
            self.assertEqual(Path(handed).read_bytes(), self.body)
            os.replace(self.path, self.tmp / "gone")                         # ... nor a swap changes what is read
            self.assertEqual(Path(handed).read_bytes(), self.body)
        self.assertFalse(os.path.exists(handed), "released afterwards")

    def test_everything_else_is_refused(self):
        outside = self.tmp / "outside.onnx"
        outside.write_bytes(self.body)
        link = self.tmp / "link.onnx"
        link.symlink_to(outside)
        pipe = self.tmp / "pipe.onnx"
        os.mkfifo(pipe)
        big = self.tmp / "big.onnx"
        big.write_bytes(b"x" * 1024)
        for label, path, sha, expect in (
                ("symlink", link, self.sha, "cannot be opened"), ("fifo", pipe, self.sha, "regular file"),
                ("directory", self.tmp, self.sha, "regular file"), ("missing", self.tmp / "nope", self.sha, "cannot be opened"),
                ("wrong hash", self.path, "0" * 64, "not the pinned one"), ("empty", self.tmp / "e", self.sha, "")):
            if label == "empty":
                (self.tmp / "e").write_bytes(b"")
                expect = "regular file"
            with self.subTest(label):
                with self.assertRaises(speaker.VerifyUnavailable) as raised:
                    with speaker.verified_model(path, sha):
                        self.fail("a refused model must not be handed out")
                self.assertIn(expect, str(raised.exception))
                self.assertNotIn(str(self.tmp), str(raised.exception))
        with patch.object(speaker, "MAX_MODEL_BYTES", 512):
            with self.assertRaises(speaker.VerifyUnavailable):
                with speaker.verified_model(big, self.sha):
                    pass

    def test_the_pinned_hash_is_the_default_and_the_environment_cannot_blank_it(self):
        for value, want in ((None, speaker.MODEL_SHA256), ("", speaker.MODEL_SHA256), ("nothex", speaker.MODEL_SHA256),
                            ("AB" * 32, "ab" * 32)):
            env = dict(os.environ)
            env.pop("JARVIS_SPEAKER_MODEL_SHA256", None)
            if value is not None:
                env["JARVIS_SPEAKER_MODEL_SHA256"] = value
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(speaker._expected_sha(), want, value)

    def test_the_download_is_capped(self):
        import urllib.request

        class Endless(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b"x" * min(n if n > 0 else 1 << 20, 1 << 20)

        dest = self.tmp / "d" / "model.onnx"
        with patch.object(urllib.request, "urlopen", lambda *a, **k: Endless()), patch.object(speaker, "MAX_MODEL_BYTES", 3 << 20):
            with self.assertRaises(SystemExit) as raised:
                speaker.fetch(dest)
        self.assertIn("larger than 3 MB", str(raised.exception))
        self.assertFalse(dest.exists())
        self.assertEqual(list(dest.parent.iterdir()), [], "no scratch left")


class EngineChecks(unittest.TestCase):
    def test_lazy_load_pinned_threads_and_idle_unload(self):
        now = [100.0]
        seen = []

        class B(FakeBackend):
            def __init__(self, threads):
                super().__init__(threads)
                seen.append(threads)

        eng = speaker.Engine(B, threads=1, idle_s=60.0, clock=lambda: now[0], schedule=False)
        self.assertEqual((eng.loaded, eng.loads), (False, 0))
        x = voice(OWNER, 3)
        eng.embed(x, RATE)
        self.assertEqual((eng.loaded, eng.loads, seen), (True, 1, [1]))
        eng.embed(x, RATE)
        self.assertEqual(eng.loads, 1, "loaded once")
        now[0] += 59
        self.assertFalse(eng.reap())
        self.assertTrue(eng.loaded)
        now[0] += 2
        self.assertTrue(eng.reap())
        self.assertFalse(eng.loaded)
        eng.embed(x, RATE)
        self.assertEqual(eng.loads, 2, "an unloaded engine loads again on use")
        self.assertTrue(eng.unload())
        self.assertFalse(eng.unload())

    def test_the_idle_timer_is_armed_after_use_and_unloads(self):
        eng = speaker.Engine(lambda t: FakeBackend(t), threads=1, idle_s=0.05)
        eng.embed(voice(OWNER, 3), RATE)
        deadline = time.monotonic() + 3
        while eng.loaded and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(eng.loaded)

    def test_a_failed_load_is_not_retried_at_once(self):
        now = [0.0]
        attempts = []

        def factory(threads):
            attempts.append(1)
            raise OSError("/secret/path is broken")

        eng = speaker.Engine(factory, clock=lambda: now[0], schedule=False)
        for _ in range(3):
            with self.assertRaises(speaker.VerifyUnavailable) as raised:
                eng.embed(voice(OWNER, 3), RATE)
            self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(len(attempts), 1)
        now[0] += speaker.LOAD_RETRY_S + 1
        with self.assertRaises(speaker.VerifyUnavailable):
            eng.embed(voice(OWNER, 3), RATE)
        self.assertEqual(len(attempts), 2)

    def test_one_embedding_at_a_time_and_a_busy_engine_gives_up(self):
        gate = threading.Event()
        started = threading.Event()

        class Slow(FakeBackend):
            def embed(self, samples, rate):
                started.set()
                gate.wait(5)
                return super().embed(samples, rate)

        eng = speaker.Engine(lambda t: Slow(t), schedule=False)
        worker = threading.Thread(target=lambda: eng.embed(voice(OWNER, 3), RATE), daemon=True)
        worker.start()
        self.assertTrue(started.wait(3))
        with self.assertRaises(speaker.VerifyUnavailable):
            eng.embed(voice(OWNER, 3), RATE, timeout=0.1)
        gate.set()
        worker.join(5)

    def test_sherpa_is_not_imported_with_the_module(self):
        out = subprocess.run(
            [sys.executable, "-c", "import sys; import jarvis.speaker as s; s.status(); print('sherpa_onnx' in sys.modules)"],
            env={**os.environ, "PYTHONPATH": str(REPO), "HOME": tempfile.mkdtemp()},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_config_threads_are_pinned_and_bounded(self):
        for value, expected in (("", 1), ("2", 2), ("9", 4), ("0", 1), ("x", 1), ("nan", 1)):
            out = subprocess.run(
                [sys.executable, "-c", "from jarvis import config; print(config.SPEAKER_THREADS)"],
                env={**os.environ, "PYTHONPATH": str(REPO), "JARVIS_SPEAKER_THREADS": value},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(out.stdout.strip(), str(expected), (value, out.stderr))


class Pinned(unittest.TestCase):
    def test_the_download_is_the_one_v0_measured(self):
        models = json.loads((REPO / "tests/voice_v0/models.json").read_text())["models"]
        entry = models["spk-nemo-titanet-small"]
        self.assertEqual((speaker.MODEL_URL, speaker.MODEL_SHA256), (entry["url"], entry["sha256"]))

    def test_fetch_checks_the_hash_before_putting_the_file_in_place(self):
        import urllib.request

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        tmp = Path(tempfile.mkdtemp())
        dest = tmp / "m" / "model.onnx"
        with patch.object(urllib.request, "urlopen", lambda *a, **k: Resp(b"not the model")):
            with self.assertRaises(SystemExit) as raised:
                speaker.fetch(dest)
        self.assertIn("sha256 mismatch", str(raised.exception))
        self.assertFalse(dest.exists())
        import hashlib

        body = b"pretend model"
        with patch.object(urllib.request, "urlopen", lambda *a, **k: Resp(body)):
            self.assertEqual(speaker.fetch(dest, sha256=hashlib.sha256(body).hexdigest()), dest)
        self.assertEqual(dest.read_bytes(), body)
        self.assertEqual([p.name for p in dest.parent.iterdir()], ["model.onnx"], "no scratch left beside the model")

    def test_the_module_reaches_no_network_and_no_approval(self):
        """No import of a network client outside fetch(), and nothing here can answer an approval card."""
        source = (REPO / "jarvis/speaker.py").read_text()
        head = source.split("def fetch(")[0]
        for word in ("urllib", "requests", "httpx", "socket", "llm", "approvals", "decide"):
            self.assertNotRegex(head, rf"(?m)^\s*(import|from)\s+.*\b{word}\b", word)
        routes = (REPO / "jarvis/v2/speaker_routes.py").read_text()
        for word in ("approvals", "resolve", "decide"):
            self.assertNotIn(word, routes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
