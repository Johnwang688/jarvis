"""Synthetic checks for typed input + attachments on /converse. Free — no API.

What must hold:
  1. assembly: speech, typed text, and attachments fold into one user message;
     images ride as multimodal parts; text files inline as fenced blocks;
     @path references resolve server-side; caps produce notes, not silence
  2. secrets: a protected credentials file is refused by @path, and inlined
     text is scrubbed of known credential values
  3. routes: a typed-only JSON turn skips STT and still completes; voice+typed
     combine; the legacy raw-webm body still works; an empty turn is a 400

Run:  .venv/bin/python tests/face/attach_check.py
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from jarvis import agent as agent_mod
from jarvis import config, voice
from jarvis.face import server as face_server
from jarvis.tools import voicectl

PORT = 8476
BASE = f"http://127.0.0.1:{PORT}"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def assemble_checks(tmp: Path) -> None:
    notes_file = tmp / "notes.md"
    notes_file.write_text("meeting at noon", encoding="utf-8")
    png_file = tmp / "pic.png"
    png_file.write_bytes(b"\x89PNG fakedata")
    (tmp / ".env").write_text("API_KEY=super-secret-value-123\n", encoding="utf-8")

    # Speech + typed + one uploaded text attachment.
    text, images = face_server._assemble_turn(
        "spoken words", "typed words",
        [{"name": "up.txt", "mime": "text/plain", "data_b64": _b64(b"uploaded body")}],
    )
    assert text.startswith("spoken words\n\ntyped words"), text
    assert "[attached file: up.txt]" in text and "uploaded body" in text
    assert images == []

    # @path: a text file inlines, an image becomes a multimodal part.
    text, images = face_server._assemble_turn(
        "", f"see @{notes_file} and @{png_file}", []
    )
    assert "meeting at noon" in text and "[attached file:" in text
    assert len(images) == 1 and images[0]["mime"] == "image/png"
    assert base64.b64decode(images[0]["b64"]) == b"\x89PNG fakedata"
    assert "[attached image:" in text

    # @path niceties: missing paths get a note, email-ish tokens do not.
    text, _ = face_server._assemble_turn("", "read @/definitely/missing.txt", [])
    assert "not found — not attached" in text, text
    text, _ = face_server._assemble_turn("", "mail user@host.com about it", [])
    assert "not attached" not in text and "user@host.com" in text, text

    # Protected file: refused by name, content never read.
    text, _ = face_server._assemble_turn("", f"show me @{tmp / '.env'}", [])
    assert "holds live credentials — not attached" in text, text
    assert "super-secret-value-123" not in text

    # Scrub: an attachment that *contains* a known credential value is
    # redacted (value learned from the Onshape bundle path, like dispatch()).
    old_onshape = config.ONSHAPE_TOKEN_PATH
    config.ONSHAPE_TOKEN_PATH = tmp / "onshape_keys.json"
    config.ONSHAPE_TOKEN_PATH.write_text(
        json.dumps({"access_key": "AK-plain", "secret_key": "leaked-secret-value-xyz"}),
        encoding="utf-8",
    )
    try:
        text, _ = face_server._assemble_turn(
            "", "",
            [{"name": "cfg.txt", "mime": "text/plain",
              "data_b64": _b64(b"key is leaked-secret-value-xyz here")}],
        )
        assert "leaked-secret-value-xyz" not in text, text
        assert "redacted" in text.lower()
    finally:
        config.ONSHAPE_TOKEN_PATH = old_onshape

    # Caps: over-count drops with a note; oversize skips with a note.
    many = [
        {"name": f"f{i}.txt", "mime": "text/plain", "data_b64": _b64(b"x")}
        for i in range(face_server.MAX_ATTACHMENTS + 2)
    ]
    text, _ = face_server._assemble_turn("", "", many)
    assert "2 attachment(s) over the" in text, text
    old_max = face_server.MAX_ATTACH_BYTES
    face_server.MAX_ATTACH_BYTES = 4
    try:
        text, _ = face_server._assemble_turn(
            "", "", [{"name": "big.bin", "mime": "text/plain", "data_b64": _b64(b"12345")}]
        )
        assert "big.bin is over" in text and "12345" not in text, text
    finally:
        face_server.MAX_ATTACH_BYTES = old_max
    print("ok  assembly: speech+typed+files fold into one message, @paths resolve")
    print("ok  secrets: protected @path refused, inlined values scrubbed")


class FakeAgent:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def run_turn(self, user_input: str, images=None) -> agent_mod.Turn:
        self.calls.append((user_input, images))
        return agent_mod.Turn(text="ok reply", steps=1)


def _post_lines(payload=None, raw: bytes | None = None) -> list[dict]:
    if raw is not None:
        response = httpx.post(
            f"{BASE}/converse", content=raw,
            headers={"Content-Type": "audio/webm"}, timeout=30,
        )
    else:
        response = httpx.post(f"{BASE}/converse", json=payload, timeout=30)
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def route_checks() -> None:
    fake = FakeAgent()
    face_server._agent = fake
    stt_calls = []
    real_stt = voice.stt
    voice.stt = lambda audio, mime=None: (stt_calls.append(mime) or "spoken words")
    was_muted = voicectl.is_muted()
    voicectl.set_muted(True)  # skip TTS entirely — nothing touches the network
    server = face_server.create_server(PORT)
    try:
        time.sleep(0.1)
        # Typed-only: no STT, agent gets the text, meta.heard is non-empty.
        lines = _post_lines({"text": "hello there"})
        kinds = [line["type"] for line in lines]
        assert "heard" not in kinds, kinds  # window already showed the typed text
        meta = next(line for line in lines if line["type"] == "meta")
        assert meta["heard"] == "hello there" and meta["reply"] == "ok reply"
        assert lines[-1]["type"] == "done" and lines[-1]["muted"] is True
        assert stt_calls == [] and fake.calls[-1] == ("hello there", None)

        # Voice + typed + attachment: STT runs, everything lands in one turn.
        lines = _post_lines(
            {
                "audio_b64": _b64(b"\x00" * 4000),
                "text": "also this",
                "attachments": [
                    {"name": "notes.txt", "mime": "text/plain", "data_b64": _b64(b"alpha beta")}
                ],
            }
        )
        heard = next(line for line in lines if line["type"] == "heard")
        assert heard["text"] == "spoken words" and len(stt_calls) == 1
        sent, images = fake.calls[-1]
        # A turn carrying speech is prefixed with the transcription notice, so
        # pin the order of the parts rather than the head of the string.
        assert "transcribed from speech" in sent, sent
        assert "spoken words\n\nalso this" in sent and "alpha beta" in sent
        assert sent.index("spoken words") < sent.index("alpha beta"), sent
        assert images is None

        # Image attachment reaches run_turn as a multimodal part.
        _post_lines(
            {
                "text": "look",
                "attachments": [
                    {"name": "s.png", "mime": "image/png", "data_b64": _b64(b"\x89PNG!")}
                ],
            }
        )
        sent, images = fake.calls[-1]
        assert "[attached image: s.png]" in sent
        assert images == [{"b64": _b64(b"\x89PNG!"), "mime": "image/png"}]

        # Legacy raw-webm body still works end to end.
        lines = _post_lines(raw=b"\x00" * 4000)
        meta = next(line for line in lines if line["type"] == "meta")
        assert meta["heard"] == "spoken words"

        # An empty turn is a 400, not a hang.
        response = httpx.post(f"{BASE}/converse", json={}, timeout=10)
        assert response.status_code == 400, response.status_code
        print("ok  routes: typed-only skips STT, voice+typed combine, legacy body works")
    finally:
        server.shutdown()
        voice.stt = real_stt
        voicectl.set_muted(was_muted)
        face_server._agent = None


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        assemble_checks(Path(tmp))
    route_checks()
    print("\nall attachment checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
