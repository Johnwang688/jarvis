"""V12: the daemon side of speaker verification, over real loopback listeners.

`/stt?verify=1` with a fake embedder (`tests/speaker_fakes.py`) and a recorder in
place of the cloud: a rejection never reaches `llm.transcribe`; Guest voice
skips verification and nothing else; every `/speaker` route is the owner's.
Nothing touches 8402/8403/8405, the owner's config or a speech engine.

    .venv/bin/python tests/v2/speaker_check.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config, llm, speaker, voice  # noqa: E402
from jarvis.v2 import hud_api as H  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.provider import Decision  # noqa: E402
from tests.speaker_fakes import FRIEND, OWNER, STRANGER, FakeBackend, clip, fake_engine  # noqa: E402
from tests.v2 import hud_backend_check as hbc  # noqa: E402  (the module, so its tests are not collected here)
from tests.v2.hud_backend_check import FakeControl, eventually  # noqa: E402

WAV = {"Content-Type": "audio/wav"}


class Routes(unittest.TestCase):
    request = hbc.Backend.request
    owner = hbc.Backend.owner
    events_all = hbc.Backend.events_all
    thread = hbc.Backend.thread

    def setUp(self):
        hbc.Backend.setUp(self)
        self.dir = self.root / "speaker"
        for name, value in (("SPEAKER_DIR", self.dir),):
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.engine = fake_engine()
        previous = speaker.set_engine(self.engine)
        self.addCleanup(speaker.set_engine, previous)
        for target, value in (("impostor_voices", lambda: [f"af_v{i}" for i in range(6)]),
                              ("synth_voice", lambda n: clip(150 + 35 * int(n[-1]), 5, seed=7))):
            guard = patch.object(speaker, target, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.cloud: list[int] = []

        def transcribe(audio, **kw):
            self.cloud.append(len(audio))
            return "dictated words"

        guard = patch.object(llm, "transcribe", transcribe)
        guard.start()
        self.addCleanup(guard.stop)
        self.port = self.daemon.face_port
        self.hud = {"Origin": f"http://127.0.0.1:{self.port}"}

    # -- helpers ----------------------------------------------------------------------
    def hud_audio(self, path, audio, status=200, mime="audio/wav"):
        return self.request("POST", path, raw=audio, status=status, port=self.port,
                            headers={**self.hud, "Content-Type": mime})

    def enroll(self, threshold=None):
        for i in range(3):
            r = self.hud_audio("/speaker/reading", clip(OWNER, 10.5, seed=i))
            self.assertTrue(r["accepted"], r)
        body = {"accept": True} if threshold is None else {"accept": True, "threshold": threshold}
        return self.owner("POST", "/speaker", body)

    def stt(self, audio, status=200, query="verify=1", mime="audio/wav"):
        return self.hud_audio("/stt" + ("?" + query if query else ""), audio, status, mime)

    # -- owner-only, content types, and nothing created for a stranger ----------------
    def test_every_speaker_route_is_the_owners_alone(self):
        reading = clip(OWNER, 10.5)
        asks = [("GET", "/speaker", None, None), ("POST", "/speaker", {"guest": True}, None),
                ("POST", "/speaker", {"propose": True}, None), ("DELETE", "/speaker", None, None),
                ("POST", "/speaker/reading", None, reading)]
        for method, path, body, raw in asks:
            headers = WAV if raw else None
            with self.subTest(method=method, path=path):
                # The API listener (every tool's client), a foreign Origin, and no Origin at all.
                self.request(method, path, body, status=403, raw=raw, headers=headers)
                self.request(method, path, body, status=403, raw=raw, port=self.port,
                             headers={"Origin": "http://evil.example", **(headers or {})})
                self.request(method, path, body, status=403, raw=raw, port=self.port, headers=headers)
                self.request(method, path, body, status=403, raw=raw, port=self.port,
                             headers={"Origin": "null", **(headers or {})})
        self.assertFalse(self.dir.exists(), "a refused request creates nothing")
        self.assertEqual(self.events_all(), [])

    def test_content_types_and_unknown_things(self):
        # Audio is allowed on an explicit set of routes, never a prefix.
        self.assertEqual(H.AUDIO_ROUTES, frozenset({"/stt", "/speaker/reading"}))
        for path in ("/speaker", "/speech/summary", "/speech/voices", "/say", "/mute"):
            with self.subTest(path):
                self.hud_audio(path, b"RIFF....WAVE", status=400)
        self.hud_audio("/speaker/reading", b"{}", status=400, mime="application/json")
        self.hud_audio("/speaker/reading", b"x", status=400, mime="text/plain")
        self.hud_audio("/speaker/reading", b"", status=400)
        for bad in ({"bogus": 1}, {}, {"guest": "yes"}, {"guest": 1}, {"guest": True, "propose": True},
                    {"accept": False}, {"accept": True, "guest": True}, {"propose": False},
                    {"threshold": "high"}, {"threshold": 0.1}, {"accept": True, "threshold": 5}):
            with self.subTest(bad):
                self.owner("POST", "/speaker", bad, status=400)
        self.owner("POST", "/speaker", {"threshold": 0.6}, status=409)          # nothing enrolled to tune
        self.owner("GET", "/speaker?x=1", status=400)
        self.owner("PUT", "/speaker", {}, status=404)
        self.owner("GET", "/speaker/reading", status=404)
        self.assertFalse(self.dir.exists())
        # Oversize bodies are refused from the header, before they are read.
        self.request("POST", "/speaker", raw=b"{}", status=413, port=self.port,
                     headers={**self.hud, "Content-Type": "application/json", "Content-Length": str(1 << 20)})
        self.request("POST", "/speaker/reading", raw=b"RIFF", status=413, port=self.port,
                     headers={**self.hud, "Content-Type": "audio/wav", "Content-Length": str(speaker.MAX_READING_BYTES + 1)})
        self.assertFalse(self.dir.exists())

    # -- the enrollment flow ---------------------------------------------------------------
    def test_enrollment_flow_and_the_events_it_publishes(self):
        st = self.owner("GET", "/speaker")
        self.assertEqual((st["state"], st["mode"], st["guest"], st["readings"]), ("not_enrolled", "off", False, 0))
        # The first look at the store is published once (nothing has been since the daemon started); a second changes nothing.
        first = [e["data"] for e in self.events_all() if e["kind"] == "speaker_guest"]
        self.assertEqual(first, [{"guest": False, "enrolled": False, "mode": "off"}])
        self.owner("GET", "/speaker")
        self.assertEqual(self.events_all(), [])
        self.assertEqual(len(st["prompts"]), 3)
        self.assertIn("min_s", st["quality"])
        bad = self.hud_audio("/speaker/reading", clip(OWNER, 2.0))
        self.assertFalse(bad["accepted"])
        self.assertTrue(bad["problems"])
        self.owner("POST", "/speaker", {"propose": True}, status=409)                 # three readings first
        for i in range(3):
            r = self.hud_audio("/speaker/reading", clip(OWNER, 10.5, seed=i))
            self.assertEqual((r["accepted"], r["readings"]), (True, i + 1))
        proposal = self.owner("POST", "/speaker", {"propose": True})
        self.assertEqual(self.owner("GET", "/speaker")["mode"], "off", "propose stores nothing")
        self.assertGreater(proposal["threshold"], proposal["impostors"]["max"])
        self.assertEqual(self.events_all(), [])
        done = self.owner("POST", "/speaker", {"accept": True, "threshold": proposal["threshold"]})
        self.assertEqual((done["mode"], done["enrolled"], done["threshold"]), ("on", True, proposal["threshold"]))
        self.assertEqual(self.owner("POST", "/speaker", {"threshold": 0.7})["threshold"], 0.7)
        guest = self.owner("POST", "/speaker", {"guest": True})
        self.assertEqual((guest["guest"], guest["mode"]), (True, "guest"))
        gone = self.owner("DELETE", "/speaker")
        self.assertEqual((gone["state"], gone["guest"], gone["mode"]), ("not_enrolled", False, "off"))
        kinds = [(e["kind"], e["data"]) for e in self.events_all() if e["kind"] == "speaker_guest"]
        self.assertEqual(kinds, [("speaker_guest", {"guest": False, "enrolled": True, "mode": "on"}),
                                 ("speaker_guest", {"guest": True, "enrolled": True, "mode": "guest"}),
                                 ("speaker_guest", {"guest": False, "enrolled": False, "mode": "off"})])

    def test_the_store_the_routes_make_is_0700(self):
        self.enroll()
        self.owner("POST", "/speaker", {"guest": True})
        for p in (self.dir, self.dir / "readings"):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o700, p)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["guest.json", "impostors.json", "readings", "voiceprint.json"])

    def test_no_route_returns_audio_or_an_embedding(self):
        vp = None
        self.enroll()
        vp = json.loads((self.dir / "voiceprint.json").read_text())
        for method, path, body in (("GET", "/speaker", None), ("POST", "/speaker", {"propose": True}),
                                   ("POST", "/speaker", {"threshold": 0.6}), ("POST", "/speaker", {"guest": False})):
            text = json.dumps(self.owner(method, path, body))
            self.assertNotIn("centroid", text)
            self.assertNotIn(repr(vp["centroid"][0]), text)
            self.assertNotIn("RIFF", text)

    def test_delete_removes_all_of_it_over_http(self):
        self.enroll()
        self.owner("POST", "/speaker", {"guest": True})
        self.owner("DELETE", "/speaker")
        self.assertFalse(os.path.lexists(self.dir))
        self.assertEqual(self.owner("GET", "/speaker")["readings"], 0)

    # -- /stt?verify=1 ----------------------------------------------------------------------
    def test_a_rejection_never_reaches_the_cloud(self):
        self.enroll()
        embedded = FakeBackend.calls
        ok = self.stt(clip(OWNER, 3, seed=50))
        self.assertEqual(ok, {"text": "dictated words", "verify": "matched"})
        self.assertEqual(len(self.cloud), 1)
        for other in (FRIEND, STRANGER):
            body = self.stt(clip(other, 3, seed=51), status=422)
            self.assertEqual(body, {"error": "not the enrolled speaker"})
        self.assertEqual(len(self.cloud), 1, "no transcription, no upload, for a voice that is not the owner's")
        self.assertGreater(FakeBackend.calls, embedded)
        # A rejected clip, also sent with ids and a budget, still goes nowhere.
        self.stt(clip(FRIEND, 3), status=422, query="verify=1&utterance_id=3&seq=4&budget_ms=15000")
        self.assertEqual(len(self.cloud), 1)

    def test_jarvis_own_voices_are_rejected_before_any_cloud_call(self):
        """Plan 3.4 (3): once enrolled, verification rejects Jarvis's voices."""
        self.enroll()
        for i in range(6):
            self.stt(speaker.synth_voice(f"af_v{i}"), status=422)
        self.assertEqual(self.cloud, [])

    def test_unenrolled_and_enrolling_skip_verification(self):
        body = self.stt(clip(FRIEND, 3))
        self.assertEqual(body["verify"], "skipped_unenrolled")
        for i in range(3):
            self.hud_audio("/speaker/reading", clip(OWNER, 10.5, seed=i))
        self.assertEqual(self.stt(clip(FRIEND, 3))["verify"], "skipped_unenrolled",
                         "readings alone switch nothing on: the owner has to accept")
        self.assertEqual(len(self.cloud), 2)
        self.assertEqual(FakeBackend.calls, 0, "nothing was embedded")

    def test_guest_voice_skips_verification_and_only_that(self):
        self.enroll()
        self.stt(clip(FRIEND, 3), status=422)
        self.assertEqual(self.cloud, [])
        self.owner("POST", "/speaker", {"guest": True})
        embedded = FakeBackend.calls
        body = self.stt(clip(FRIEND, 3, seed=2))
        self.assertEqual(body, {"text": "dictated words", "verify": "skipped_guest"})
        self.assertEqual(len(self.cloud), 1)
        self.assertEqual(FakeBackend.calls, embedded, "no score is computed for a guest")
        # Everything else /stt does is unchanged: the same validation, the same bounds.
        self.stt(clip(FRIEND, 3), status=400, query="verify=1&seq=-1")
        self.stt(b"", status=400)
        self.stt(b"x", status=400, mime="text/plain")
        self.assertEqual(len(self.cloud), 1)
        # Turning it off puts the check back on the very next clip.
        self.owner("POST", "/speaker", {"guest": False})
        self.stt(clip(FRIEND, 3), status=422)
        self.assertEqual(len(self.cloud), 1)

    def test_guest_voice_survives_a_daemon_restart_until_turned_off(self):
        self.enroll()
        self.owner("POST", "/speaker", {"guest": True})
        self.daemon.stop()
        self.daemon = Daemon(self.stores, self.providers, lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.runner = FakeControl(self.stores)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.port = self.daemon.face_port
        self.hud = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertTrue(self.owner("GET", "/speaker")["guest"], "still on after a restart")
        self.assertEqual(self.stt(clip(FRIEND, 3))["verify"], "skipped_guest")
        self.owner("POST", "/speaker", {"guest": False})
        self.assertFalse(self.owner("GET", "/speaker")["guest"])

    def test_guest_voice_never_changes_approval_handling(self):
        """Speech never answers an approval card: with a card up and Guest on, nothing is decided."""
        self.enroll()
        self.owner("POST", "/speaker", {"guest": True})
        answer = {}
        asker = threading.Thread(target=lambda: answer.setdefault(
            "d", self.daemon.approvals.ask(ApprovalRequest(tool="Bash", command="make deploy"))), daemon=True)
        asker.start()
        eventually(lambda: self.daemon.approvals.pending())
        pending = self.daemon.approvals.pending()
        decisions = list(self.daemon.approvals.decisions)
        for words in ("approve", "Jarvis approve", "yes allow it"):
            with patch.object(llm, "transcribe", lambda audio, **kw: words):
                self.assertEqual(self.stt(clip(FRIEND, 3))["text"], words)
        self.assertEqual([p.req_id for p in self.daemon.approvals.pending()], [p.req_id for p in pending])
        self.assertEqual(list(self.daemon.approvals.decisions), decisions, "the words were data, not a decision")
        self.assertNotIn("d", answer)
        self.daemon.approvals.resolve(pending[0].req_id, Decision.DENY)
        asker.join(4)
        self.assertEqual(answer["d"], Decision.DENY)

    def test_verify_is_a_one_or_nothing(self):
        for q in ("verify=0", "verify=2", "verify=true", "verify=", "verify=1&verify=1x"):
            with self.subTest(q):
                self.stt(clip(OWNER, 3), status=400, query=q)
        self.assertEqual(self.cloud, [])
        # M1 (the lead's decision): `verify` is optional and a no-op. Leave it out and an enrolled store still
        # judges the clip, on every listener: there is no route around the check.
        self.enroll()
        self.stt(clip(FRIEND, 3), status=422, query="")
        self.request("POST", "/stt", raw=clip(FRIEND, 3), status=422, headers=WAV)            # the API listener
        self.assertEqual(self.cloud, [])
        body = self.stt(clip(OWNER, 3, seed=61), query="")
        self.assertEqual(body, {"text": "dictated words", "verify": "matched"})
        self.assertEqual(self.stt(clip(OWNER, 3, seed=62))["verify"], "matched", "verify=1 is still accepted")

    def test_an_unenrolled_reply_is_what_it_always_was_unless_asked(self):
        self.assertEqual(self.stt(clip(OWNER, 3), query=""), {"text": "dictated words"})
        self.assertEqual(self.stt(clip(OWNER, 3)), {"text": "dictated words", "verify": "skipped_unenrolled"})

    def test_too_little_speech_is_an_empty_answer_not_a_refusal_and_not_an_upload(self):
        """L2."""
        self.enroll()
        body = self.stt(clip(OWNER, 0.1), query="utterance_id=3&seq=4")
        self.assertEqual(body, {"text": "", "utterance_id": 3, "seq": 4, "verify": "no_speech"})
        self.assertEqual(self.cloud, [])
        # Guest or unenrolled: nothing is decoded, so the clip goes on to the cloud as before.
        self.owner("POST", "/speaker", {"guest": True})
        self.assertEqual(self.stt(clip(OWNER, 0.1))["text"], "dictated words")

    # -- M3: a change the routes did not make ------------------------------------------------------
    def modes(self):
        return [e["data"] for e in self.events_all() if e["kind"] == "speaker_guest"]

    def test_a_gate_change_made_behind_the_routes_is_published_when_seen(self):
        self.enroll()
        self.events_all()
        self.stt(clip(OWNER, 3, seed=70))
        self.assertEqual(self.modes(), [], "an unchanged mode publishes nothing")
        # guest.json edited by hand (an agent, an editor, the owner in a terminal): the next clip says so.
        (self.dir / "guest.json").write_text('{"guest": true}')
        body = self.stt(clip(FRIEND, 3))
        self.assertEqual(body["verify"], "skipped_guest")
        self.assertEqual(self.modes(), [{"guest": True, "enrolled": True, "mode": "guest"}])
        self.stt(clip(FRIEND, 3))
        self.assertEqual(self.modes(), [], "published once, not per clip")
        # ... and back.
        (self.dir / "guest.json").unlink()
        self.stt(clip(FRIEND, 3), status=422)
        self.assertEqual(self.modes(), [{"guest": False, "enrolled": True, "mode": "on"}])
        # voiceprint.json deleted: the check is off, and the label must follow.
        (self.dir / "voiceprint.json").unlink()
        self.assertEqual(self.stt(clip(FRIEND, 3))["verify"], "skipped_unenrolled")
        self.assertEqual(self.modes(), [{"guest": False, "enrolled": False, "mode": "off"}])
        # A voiceprint that is damaged blocks everything, and that is published too.
        self.enroll()
        self.events_all()
        (self.dir / "voiceprint.json").write_text("{")
        self.stt(clip(OWNER, 3), status=503)
        self.assertEqual(self.modes(), [{"guest": False, "enrolled": False, "mode": "blocked"}])

    def test_a_gate_change_is_also_seen_by_a_status_read(self):
        self.enroll()
        self.events_all()
        (self.dir / "voiceprint.json").unlink()
        self.assertEqual(self.owner("GET", "/speaker")["mode"], "off")
        self.assertEqual(self.modes(), [{"guest": False, "enrolled": False, "mode": "off"}])

    def test_accept_anyway_over_http(self):
        with patch.object(speaker, "synth_voice", lambda n: clip(OWNER, 5, seed=0)):
            for i in range(3):
                self.hud_audio("/speaker/reading", clip(OWNER, 10.5, seed=i))
            self.assertTrue(self.owner("POST", "/speaker", {"propose": True})["confirm"])
            refused = self.owner("POST", "/speaker", {"accept": True}, status=409)
            self.assertIn("record the readings again", refused["error"])
            self.owner("POST", "/speaker", {"accept": True, "anyway": "yes"}, status=400)
            self.owner("POST", "/speaker", {"anyway": True}, status=400)
            self.assertFalse((self.dir / "voiceprint.json").exists())
            done = self.owner("POST", "/speaker", {"accept": True, "threshold": 0.6, "anyway": True})
        self.assertEqual((done["mode"], done["threshold"]), ("on", 0.6))

    def test_choosing_a_voice_that_sounds_like_the_owner_says_so(self):
        """L3, on both routes that choose a voice."""
        from jarvis import voice
        self.enroll()
        listed = [{"name": "af_twin", "backend": "kokoro", "kind": "builtin"}, {"name": "af_far", "backend": "kokoro", "kind": "builtin"}]
        speak = lambda n: clip(OWNER if n == "af_twin" else STRANGER, 5, seed=4)       # noqa: E731
        thread = self.thread()
        with patch.object(speaker, "synth_voice", speak), patch.object(voice, "catalog", return_value=listed), \
                patch.object(voice, "available_voices", return_value={"af_twin", "af_far"}), \
                patch.object(voice, "set_voice", side_effect=lambda n: n), \
                patch.object(voice, "selected_voice", return_value="af_far"):
            near = self.owner("POST", "/voice", {"voice": "af_twin"})
            self.assertIn("af_twin sounds like you", near["speaker_warning"])
            self.assertEqual(near["voice"], "af_twin")
            self.assertNotIn("speaker_warning", self.owner("POST", "/voice", {"voice": "af_far"}))
            chat = self.owner("POST", "/speech/voices", {"thread_id": thread.id, "voice": "af_twin"})
            self.assertIn("sounds like you", chat["speaker_warning"])
            self.assertNotIn("speaker_warning", self.owner("POST", "/speech/voices", {"thread_id": thread.id, "voice": "af_far"}))
            self.assertNotIn("speaker_warning", self.owner("POST", "/speech/voices", {"thread_id": thread.id, "muted": True}))
            # Guest voice: the check is off, so there is nothing to warn about.
            self.owner("POST", "/speaker", {"guest": True})
            self.assertNotIn("speaker_warning", self.owner("POST", "/voice", {"voice": "af_twin"}))

    def test_enrolled_but_unable_to_verify_uploads_nothing(self):
        self.enroll()
        # Not WAV: cannot be judged here, so it is not sent.
        self.stt(b"\x1aE\xdf\xa3webm....", status=415, mime="audio/webm")
        # No engine on this machine.
        speaker.set_engine(None)
        with patch.object(speaker, "installed", return_value=(False, "the speaker model is not downloaded")):
            body = self.stt(clip(OWNER, 3), status=503)
            self.assertIn("not downloaded", body["error"])
        speaker.set_engine(self.engine)
        # A damaged voiceprint.
        (self.dir / "voiceprint.json").write_text("{")
        body = self.stt(clip(OWNER, 3), status=503)
        self.assertIn("voiceprint", body["error"])
        self.assertEqual(self.cloud, [])
        # The owner's ways out: Guest voice, or Delete and start again.
        self.owner("POST", "/speaker", {"guest": True})
        self.assertEqual(self.stt(clip(OWNER, 3))["verify"], "skipped_guest")
        self.owner("DELETE", "/speaker")
        self.assertEqual(self.stt(clip(OWNER, 3))["verify"], "skipped_unenrolled")

    def test_an_error_names_no_path_and_no_score(self):
        self.enroll()
        body = self.stt(clip(FRIEND, 3), status=422)
        text = json.dumps(body)
        self.assertNotRegex(text, r"\d\.\d")
        self.assertNotIn(str(self.root), text)

    def test_the_only_listener_that_verifies_is_the_one_asked(self):
        """`verify=1` works from any listener (agents' tools do not need it); enrollment is the owner's."""
        self.enroll()
        self.request("POST", "/stt?verify=1", raw=clip(FRIEND, 3), status=422, headers=WAV)
        self.request("POST", "/speaker/reading", raw=clip(OWNER, 10.5), status=403, headers=WAV)


if __name__ == "__main__":
    unittest.main(verbosity=2)
