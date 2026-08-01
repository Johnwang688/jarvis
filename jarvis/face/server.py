"""The face's local server and window.

Serves jarvis/face/static/ plus speech routes, and launches the HUD as a
chromeless Chromium app-mode window on the Windows desktop (via WSLg).

The port stays fixed at 8402 on purpose: browser permissions (the mic grant)
are scoped to the origin, so changing the port would re-prompt. The Chromium
profile lives in ~/.cache/jarvis-face for the same reason — do not delete it.

Routes:
  GET  /<file>   static HUD assets (jarvis.html, whiteboard.html, …)
  POST /say      {"text": ..., "voice"?: ..., "instructions"?: ...} -> audio/mpeg
  POST /converse one turn -> NDJSON stream. Body is raw audio (Content-Type
                 audio/webm), or application/json {"audio_b64"?, "audio_mime"?,
                 "text"?, "attachments"?: [{"name","mime","data_b64"}]} — the
                 HUD's typed input and staged files ride the same turn as the
                 speech. @path references in the text attach server-side files
                 (protected credential files refused, content scrubbed)
  POST /approve  {"id": ..., "allow": bool} -> answers a dangerous-tool card
  POST /design   {"prompt": ..., "image_b64"?: ...} -> design-mode turn; the
                 sketch goes to a separate designer agent, output files are
                 served by the workshop server on WORKSHOP_PORT (its own
                 origin — agent-written pages must never share the face's
                 origin, which owns /approve)

One Agent lives for the whole server process, so a voice session is a real
conversation with memory. Dangerous tools are gated in the window rather than
hard-denied (see approvals.py): dispatch() runs them *unguarded* when approve
is None, so the agent here is always constructed with a real approver.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import queue
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import agent as agent_mod
from .. import config, permissions, voice
from ..tools import secrets as secrets_mod
from ..tools import voicectl, whiteboardctl
from .approvals import ApprovalBroker


def _sentences(text: str, max_len: int = 300, first_max: int = 90) -> list[str]:
    """Split a reply into speakable chunks: sentences, tiny ones merged.

    This is what makes streaming TTS work — the first chunk synthesizes in
    well under a second, so speech starts while the rest is still rendering.
    Synthesis time scales with text length, and the first chunk is the one
    the owner is waiting on, so it gets clamped extra short at a clause
    boundary — "Sure," starts playing while the rest of the sentence renders.
    """
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        while len(part) > max_len:
            cut = part.rfind(" ", 0, max_len)
            cut = cut if cut > 0 else max_len
            out.append(part[:cut])
            part = part[cut:].strip()
        if out and (len(part) < 25 or len(out[-1]) < 25) and len(out[-1]) + len(part) < max_len:
            out[-1] = out[-1] + " " + part
        else:
            out.append(part)
    if not out:
        return [text.strip()]

    if len(out[0]) > first_max:
        head = out[0]
        cut = max(head.rfind(", ", 0, first_max), head.rfind("; ", 0, first_max),
                  head.rfind(" — ", 0, first_max))
        take = cut + 1 if cut >= 20 else 0  # keep the comma, drop the space
        if take == 0:
            space = head.rfind(" ", 0, first_max)
            take = space if space >= 20 else 0
        if take:
            out[:1] = [head[:take].strip(), head[take:].strip()]
    return out

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROFILE_DIR = Path.home() / ".cache" / "jarvis-face"
PORT = config.FACE_PORT


MAX_SAY_CHARS = 2000  # one spoken reply, not an audiobook

# ---- typed input + attachments ---------------------------------------------
# The HUD's input bar stages files (picker / drop / paste) and @path
# references; both arrive on /converse and are folded into the same user
# message as the speech. Owner-supplied context, so it is inlined rather than
# left for tool round-trips — but the secrets rules still apply: protected
# credential files are refused by name and every inlined text is scrubbed.

MAX_ATTACHMENTS = 8
MAX_ATTACH_BYTES = 4 * 1024 * 1024  # per file, decoded
MAX_INLINE_CHARS = 100_000  # per text file, after decode

_IMAGE_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
# An @path token: start-of-text or whitespace, then @, then the path.
_AT_PATH = re.compile(r"(?:(?<=\s)|^)@(\S+)")


def _resolve_at_paths(text: str) -> tuple[list[dict], list[str]]:
    """Owner-typed @path references -> attachment dicts + notes.

    Tokens that do not exist are left alone unless they look like a path
    (contain a slash) — "user@host.com" must not produce noise."""
    attachments: list[dict] = []
    notes: list[str] = []
    for token in _AT_PATH.findall(text or ""):
        raw = token.rstrip(".,;:!?)\"'")
        path = Path(raw).expanduser()
        if secrets_mod.is_protected(path):
            notes.append(f"[{raw} holds live credentials — not attached]")
            continue
        if not path.is_file():
            if "/" in raw:
                notes.append(f"[{raw} not found — not attached]")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            notes.append(f"[{raw}: {exc} — not attached]")
            continue
        attachments.append(
            {
                "name": str(path),
                "mime": _IMAGE_EXT.get(path.suffix.lower(), "text/plain"),
                "data_b64": base64.b64encode(data).decode(),
            }
        )
    return attachments, notes


def _assemble_turn(
    spoken: str, typed: str, attachments: list[dict]
) -> tuple[str, list[dict]]:
    """Fold speech, typed text, and attachments into one user message.

    Returns (user_input, images) for run_turn: images ride the message as
    multimodal parts, text files are inlined as fenced blocks, and every
    problem becomes a bracketed note the model can read and relay."""
    at_attachments, notes = _resolve_at_paths(typed)
    combined = list(attachments) + at_attachments
    images: list[dict] = []
    blocks: list[str] = []
    for attachment in combined[:MAX_ATTACHMENTS]:
        name = str(attachment.get("name") or "attachment")[:200]
        mime = str(attachment.get("mime") or "text/plain")
        try:
            data = base64.b64decode(attachment.get("data_b64") or "")
        except Exception:
            notes.append(f"[{name}: unreadable attachment data]")
            continue
        if len(data) > MAX_ATTACH_BYTES:
            notes.append(
                f"[{name} is over {MAX_ATTACH_BYTES // (1024 * 1024)}MB — not attached]"
            )
            continue
        if mime.startswith("image/"):
            images.append({"b64": base64.b64encode(data).decode(), "mime": mime})
            blocks.append(f"[attached image: {name}]")
        else:
            text = data.decode("utf-8", errors="replace")
            truncated = "\n[truncated]" if len(text) > MAX_INLINE_CHARS else ""
            text = secrets_mod.scrub(text[:MAX_INLINE_CHARS])
            blocks.append(f"[attached file: {name}]\n```\n{text}\n```{truncated}")
    if len(combined) > MAX_ATTACHMENTS:
        notes.append(
            f"[{len(combined) - MAX_ATTACHMENTS} attachment(s) over the "
            f"{MAX_ATTACHMENTS}-per-turn cap were dropped]"
        )
    parts = [p for p in (spoken.strip(), typed.strip()) if p]
    return "\n\n".join(parts + blocks + notes), images

VOICE_SYSTEM = config.SYSTEM_PROMPT + (
    "\n\nYou are in voice mode: your replies are spoken aloud. Keep them short "
    "and conversational — a few sentences, no markdown, no lists, no code. "
    "If a result is long, summarize it aloud instead of reading it out."
    "\n\nTools that change the system ask the owner to authorize them in the "
    "window, which takes a moment — call one when the task genuinely needs it "
    "and say what you are about to do first. If a request is declined, say so "
    "and offer another way; do not ask again for the same thing."
)

_agent: agent_mod.Agent | None = None
_agent_lock = threading.Lock()

# Set by POST /cancel, cleared at the start of each turn. The agent reads it
# between steps, so an abandoned turn stops at the next clean boundary instead
# of running to completion and talking over the correction.
_cancel = threading.Event()

# ---- live event stream (SSE) -----------------------------------------------
# The HUD subscribes to GET /events; agent activity is broadcast mid-turn so
# the face can show tools firing while Jarvis is still thinking.

_subscribers: list[queue.Queue] = []
_subs_lock = threading.Lock()


def broadcast(kind: str, data) -> None:
    msg = json.dumps({"kind": kind, "data": data})
    with _subs_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def _agent_event(kind: str, data) -> None:
    if kind == "tool_start":
        name, raw = data
        broadcast("tool", {"name": name, "args": str(raw)[:100]})
    elif kind == "tool_end":
        # Without this the window has no idea a tool finished, and sits on
        # "RUNNING · X" through the model call that follows it.
        name, result = data
        broadcast("tool_done", {"name": name, "ok": not str(result).startswith("Error:")})
    elif kind == "interim_text":
        # What Jarvis said on his way to using a tool — the reason the next
        # few seconds of silence are about to happen.
        broadcast("note", {"text": str(data)[:200]})
    elif kind == "context" and getattr(data, "saved", 0) > 500:
        broadcast("context", {"saved": data.saved})


def _viewers() -> int:
    with _subs_lock:
        return len(_subscribers)


# Dangerous tools are approved in the window. `dispatch()` runs them
# *unguarded* when approve is None, so the agent below must always be handed a
# real approver — never None, and never a plain `lambda: True`.
# on_always is the ALWAYS button: an approval that also writes a persistent
# allowlist entry, so that exact ask stops coming up.
APPROVALS = ApprovalBroker(
    broadcast=broadcast, viewers=_viewers, on_always=permissions.add_allow
)

# The MUTE button and the spoken "mute yourself" both land on voicectl; the
# broadcast keeps every open window's toggle in sync with reality.
voicectl.on_change(lambda muted: broadcast("mute", {"muted": muted}))

# whiteboard_close broadcasts a self-close signal. Only whiteboard.html
# handles it; jarvis.html ignores it on purpose (see whiteboardctl).
whiteboardctl.on_close(lambda: broadcast("wb_close", {}))


def _get_agent() -> agent_mod.Agent:
    global _agent
    if _agent is None:
        _agent = agent_mod.Agent(
            system=VOICE_SYSTEM,
            approve=permissions.gate(APPROVALS.approver()),
            on_event=_agent_event,
            should_stop=_cancel.is_set,
        )
    return _agent


# ---- design mode (the whiteboard) ------------------------------------------

DESIGNER_SYSTEM = f"""You are Jarvis in design mode. The owner sketches rough
ideas on a whiteboard — boxes, arrows, scribbled labels — and you turn them
into polished, working front-end designs.

Each request arrives as text plus a PNG of the sketch. Treat the sketch as
layout intent, not a picture to reproduce: align things properly, choose a
real palette and typography, and fill in unstated details tastefully.
Written annotations on the sketch override everything else.

Write each design as a self-contained page at
{config.DESIGNS_DIR}/<short-kebab-name>/index.html
— always that absolute path, never a relative one (the server can be
launched from any working directory). CSS inline or in files beside it.
Iterations on the same design edit the same directory. Never touch files
outside that directory.

That directory is the web root of the preview server: the finished page is
served at http://localhost:{config.WORKSHOP_PORT}/<short-kebab-name>/ with
no extra path segments. For anything non-trivial, open that URL and check
it with browser_screenshot (looks are the point; a text snapshot cannot
judge them) before answering.

Reply with a short summary: what you built, the key visual choices, and any
sketch annotation you could not honor. The owner sees the live page in a
panel beside your reply, so never paste code into the reply.
"""

DESIGNER_TOOLS = [
    "read_file",
    "write_file",
    "list_dir",
    "find_files",
    "get_datetime",
    "browser_goto",
    "browser_snapshot",
    "browser_screenshot",
    "browser_click",
    "web_search",
    "fetch_page",
    # 24 steps of building is long enough to lose the thread; the plan is the
    # part of the transcript pruning cannot reach.
    "plan_write",
]

_designer: agent_mod.Agent | None = None
_designer_lock = threading.Lock()


def _get_designer() -> agent_mod.Agent:
    global _designer
    if _designer is None:
        _designer = agent_mod.Agent(
            system=DESIGNER_SYSTEM,
            tool_names=DESIGNER_TOOLS,
            max_steps=24,
            approve=permissions.gate(APPROVALS.approver()),
            on_event=_agent_event,
        )
    return _designer


def _scan_designs() -> dict[str, float]:
    root = config.DESIGNS_DIR
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.stat().st_mtime for p in root.rglob("*") if p.is_file()
    }


def _changed_designs(before: dict[str, float]) -> list[str]:
    """Files under designs/ that a turn created or modified.

    An mtime diff rather than tool-event plumbing: it catches the output no
    matter how the agent produced it."""
    return sorted(p for p, m in _scan_designs().items() if before.get(p) != m)


def _preview_url(changed: list[str]) -> str | None:
    pages = [p for p in changed if p.endswith(".html")]
    if not pages:
        return None
    entry = next((p for p in pages if p.endswith("index.html")), pages[-1])
    return f"http://localhost:{config.WORKSHOP_PORT}/{entry}"


class FaceHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep stdout for real events
        pass

    def do_GET(self):
        if self.path == "/events":
            self._events()
            return
        if self.path == "/config":
            self._json_reply(
                {
                    "llm": config.TIERS["orchestrator"],
                    "stt": config.STT_MODEL,
                    "tts": config.TTS_MODEL,
                    "voice": config.TTS_VOICE,
                    "speed": config.TTS_SPEED,
                    "muted": voicectl.is_muted(),
                    "permissions": permissions.mode(),
                    "allowlist": len(permissions.load_allowlist()),
                }
            )
            return
        super().do_GET()

    def _events(self):
        """Server-sent events: one long-lived response per HUD window."""
        q: queue.Queue = queue.Queue(maxsize=200)
        with _subs_lock:
            _subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _subs_lock:
                if q in _subscribers:
                    _subscribers.remove(q)
            # The window that was being asked just went away. Do not leave the
            # agent thread blocked on a card nobody can see.
            if _viewers() == 0 and APPROVALS.pending_count:
                APPROVALS.deny_all("window-closed")

    def _json_error(self, code: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/converse":
            self._converse()
            return
        if self.path == "/approve":
            self._approve()
            return
        if self.path == "/design":
            self._design()
            return
        if self.path == "/cancel":
            # Abandon the turn in flight. Same-origin like /approve: it is not
            # destructive, but nothing outside the window should be able to
            # keep interrupting the owner's conversation.
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                self._json_error(403, "cross-origin cancel refused")
                return
            _cancel.set()
            broadcast("cancelled", {})
            self._json_reply({"ok": True})
            return
        if self.path == "/mute":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length) or b"{}")
                voicectl.set_muted(bool(data.get("muted")))
                self._json_reply({"ok": True, "muted": voicectl.is_muted()})
            except Exception as exc:
                self._json_error(400, f"{type(exc).__name__}: {exc}")
            return
        if self.path != "/say":
            self._json_error(404, "unknown route")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            text = str(data.get("text", "")).strip()
            if not text:
                self._json_error(400, "no text to say")
                return
            audio = voice.tts(
                text[:MAX_SAY_CHARS],
                voice=data.get("voice"),
                instructions=data.get("instructions"),
            )
        except Exception as exc:
            self._json_error(500, f"{type(exc).__name__}: {exc}")
            return

        self.send_response(200)
        # Local backend emits WAV, cloud emits MP3 — sniff, don't assume.
        mime = "audio/wav" if audio.startswith(b"RIFF") else "audio/mpeg"
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def _approve(self):
        """Answer a pending dangerous-tool request: {"id": ..., "allow": bool}.

        Same-origin only. The id is the real protection — it is a one-shot
        token the window learns from the SSE stream — but the origin and
        content-type checks stop a page in another tab from firing a blind
        POST at this port. (Requiring JSON also forces a CORS preflight on any
        cross-origin attempt, which this server never answers.)

        The origin is compared against this request's own Host rather than a
        fixed 8402, so a server started on another port still approves.
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            self._json_error(403, "cross-origin approval refused")
            return
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json_error(415, "expected application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            request_id = str(data.get("id", ""))
            allow = data.get("allow")
        except Exception as exc:
            self._json_error(400, f"{type(exc).__name__}: {exc}")
            return

        if not isinstance(allow, bool):
            self._json_error(400, "allow must be true or false")
            return
        if not APPROVALS.resolve(request_id, allow, always=bool(data.get("always"))):
            self._json_error(409, "unknown, expired, or already-answered request")
            return
        self._json_reply({"ok": True, "allowed": allow})

    def _design(self):
        """One whiteboard turn: {"prompt": ..., "image_b64"?: ...} -> JSON.

        The sketch rides into the designer agent as an image on the user
        message. Output files are found by mtime diff and previewed from the
        workshop origin.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            prompt = str(data.get("prompt", "")).strip()
            image = data.get("image_b64") or None
            if not prompt:
                self._json_error(400, "no prompt")
                return
        except Exception as exc:
            self._json_error(400, f"{type(exc).__name__}: {exc}")
            return

        try:
            before = _scan_designs()
            t0 = time.monotonic()
            with _designer_lock:
                turn = _get_designer().run_turn(prompt, images=[image] if image else None)
            changed = _changed_designs(before)
            self._json_reply(
                {
                    "reply": (turn.text or "").strip(),
                    "files": changed,
                    "preview_url": _preview_url(changed),
                    "steps": turn.steps,
                    "cost_usd": round(turn.cost_usd, 5),
                    "ms": round((time.monotonic() - t0) * 1000),
                }
            )
        except Exception as exc:
            self._json_error(500, f"{type(exc).__name__}: {exc}")

    def _converse(self):
        """One voice turn, streamed: audio in -> STT -> agent -> NDJSON out.

        The response is newline-delimited JSON, written as each stage of the
        turn actually begins:

            phase transcribing -> heard -> phase thinking -> meta
                               -> phase composing -> audio* -> done

        Headers go out before any work starts, so the window knows the upload
        landed instead of staring at a dead orb. Phases ride this stream
        rather than SSE because they are turn-scoped and have to stay ordered
        with the meta and audio lines; tool activity still comes over SSE,
        which is why the window only lets a tool event drive its state while
        the turn is in the thinking phase.

        A client that aborts mid-stream (barge-in) surfaces here as a broken
        pipe — normal, not an error.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
            content_type = self.headers.get("Content-Type", "audio/webm")
            body = self.rfile.read(length) if length else b""
            if "application/json" in content_type:
                data = json.loads(body or b"{}")
                audio_in = base64.b64decode(data.get("audio_b64") or "") or None
                audio_mime = str(data.get("audio_mime") or "audio/webm")
                typed = str(data.get("text") or "")
                attachments = data.get("attachments") or []
                if not isinstance(attachments, list):
                    attachments = []
            else:
                # Legacy shape: the whole body is the recording.
                if not body:
                    self._json_error(400, "no audio")
                    return
                audio_in, audio_mime, typed, attachments = body, content_type, "", []
            if not audio_in and not typed.strip() and not attachments:
                self._json_error(400, "empty turn")
                return
        except Exception as exc:
            self._json_error(400, f"{type(exc).__name__}: {exc}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            heard, stt_ms = "", 0
            if audio_in:
                self._nd({"type": "phase", "phase": "transcribing"})
                t0 = time.monotonic()
                heard = voice.stt(audio_in, mime=audio_mime)
                stt_ms = round((time.monotonic() - t0) * 1000)

                if not heard and not typed.strip() and not attachments:
                    self._nd({"type": "meta", "heard": "", "ms": {"stt": stt_ms}})
                    return
                if heard:
                    # Your own words land in the log now, not after the whole
                    # turn. Typed text is not echoed back — the window already
                    # showed it the moment it was sent.
                    self._nd({"type": "heard", "text": heard, "ms": {"stt": stt_ms}})

            user_input, images = _assemble_turn(heard, typed, attachments)

            self._nd({"type": "phase", "phase": "thinking"})
            t0 = time.monotonic()
            with _agent_lock:
                # Cleared here, under the lock: a cancel aimed at the previous
                # turn must not carry over and kill this one before it starts.
                _cancel.clear()
                turn = _get_agent().run_turn(user_input, images=images or None)
            agent_ms = round((time.monotonic() - t0) * 1000)

            if turn.cancelled:
                # Interrupted mid-thought. No reply, and above all no speech —
                # the whole point was to stop him talking over the correction.
                self._nd({"type": "cancelled", "ms": {"stt": stt_ms, "agent": agent_ms}})
                return

            reply = (turn.text or "").strip() or "I have nothing to say to that, somehow."

            self._nd(
                {
                    "type": "meta",
                    # Non-empty for typed/attachment turns too — an empty
                    # heard is the client's "nothing arrived" signal.
                    "heard": heard or typed.strip() or "[attachments]",
                    "reply": reply,
                    "ms": {"stt": stt_ms, "agent": agent_ms},
                    "steps": turn.steps,
                    "cost_usd": round(turn.cost_usd, 5),
                }
            )

            if voicectl.is_muted():
                # Muted: the transcript still renders; no audio is synthesized
                # (which also means muted turns cost no TTS).
                self._nd({"type": "done", "tts_ms": 0, "muted": True})
                return

            # The first sentence takes ~0.5-1s to synthesize. Without this the
            # window sat on the last tool's label through all of it.
            self._nd({"type": "phase", "phase": "composing"})
            tts_total = 0
            # Pipeline: chunks synthesize concurrently (two in flight) and
            # stream in order, so later sentences never stall behind the one
            # currently being written out.
            pool = ThreadPoolExecutor(max_workers=2)
            try:
                sentences = _sentences(reply[:MAX_SAY_CHARS])
                futures = [pool.submit(voice.tts, s) for s in sentences]
                t0 = time.monotonic()
                for seq, future in enumerate(futures):
                    chunk = future.result()
                    ms = round((time.monotonic() - t0) * 1000)
                    t0 = time.monotonic()
                    tts_total += ms
                    if ms > 2500:
                        # Catch the intermittent stall red-handed: correlate
                        # this line with what the owner heard, then run
                        # tests/net_probe.py to localize it.
                        print(
                            f"[voice] SLOW tts chunk #{seq}: {ms}ms "
                            f"for {len(sentences[seq])} chars "
                            f"(pin: {config.TTS_PROVIDER or 'none'})"
                        )
                    self._nd(
                        {
                            "type": "audio",
                            "seq": seq,
                            "b64": base64.b64encode(chunk).decode(),
                            "ms": ms,
                        }
                    )
                self._nd({"type": "done", "tts_ms": tts_total})
            finally:
                # Barge-in aborts mid-stream; don't keep synthesizing speech
                # nobody will hear.
                pool.shutdown(wait=False, cancel_futures=True)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client barged in or closed the window
        except Exception as exc:
            try:
                self._nd({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            except OSError:
                pass

    def _nd(self, obj) -> None:
        self.wfile.write((json.dumps(obj) + "\n").encode())
        self.wfile.flush()

    def _json_reply(self, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), partial(FaceHandler, directory=str(STATIC_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _WorkshopHandler(SimpleHTTPRequestHandler):
    """Static file server for agent-written designs — its own origin.

    Deliberately a separate port from the face: designer output can run
    scripts, and any page on the face's origin could talk to /approve. Being
    cross-origin keeps the approval gate out of reach, and (since only the
    face origin is blocked to Jarvis's browser) leaves the workshop open for
    him to screenshot his own work.
    """

    def log_message(self, *args):
        pass

    def end_headers(self):
        # Iterations rewrite files in place; the preview iframe must never
        # show a stale cached version.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def create_workshop(port: int | None = None) -> ThreadingHTTPServer:
    config.DESIGNS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", port or config.WORKSHOP_PORT),
        partial(_WorkshopHandler, directory=str(config.DESIGNS_DIR)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def find_chromium() -> str:
    hits = sorted(
        glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    )
    if not hits:
        raise FileNotFoundError(
            "no Playwright Chromium found — run: .venv/bin/playwright install chromium"
        )
    return hits[-1]


# Windows-side browsers give a clean native window (no Chrome-for-Testing
# banner, no WSLg frame) and use the Windows audio stack directly. Windows
# reaches the WSL server via automatic localhost forwarding.
WINDOWS_BROWSERS = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
]


def find_browser() -> tuple[str, bool]:
    """-> (executable, is_windows). JARVIS_FACE_BROWSER overrides detection."""
    override = os.environ.get("JARVIS_FACE_BROWSER")
    if override:
        return override, override.startswith("/mnt/")
    for path in WINDOWS_BROWSERS:
        if Path(path).exists():
            return path, True
    return find_chromium(), False


def launch_window(url: str, size: tuple[int, int] = (1280, 860)) -> subprocess.Popen:
    browser, is_windows = find_browser()
    args = [browser, f"--app={url}", f"--window-size={size[0]},{size[1]}"]
    if not is_windows:
        # The CfT fallback needs its own profile (mic grant persistence).
        args += [
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _face_already_running(port: int) -> bool:
    """True if the thing holding our port is another face server."""
    import http.client

    try:
        conn = http.client.HTTPConnection("localhost", port, timeout=2)
        conn.request("GET", "/config")
        body = conn.getresponse().read()
        conn.close()
        return "stt" in json.loads(body)
    except Exception:
        return False


def main(page: str = "jarvis.html", port: int = PORT) -> int:
    url = f"http://localhost:{port}/{page}"
    try:
        server = create_server(port)
    except OSError as exc:
        if exc.errno != 98:  # EADDRINUSE
            raise
        if _face_already_running(port):
            # Another `jarvis face` owns the server — just give it a window.
            print(f"face server already running — opening a window on {url}")
            launch_window(url)
            return 0
        print(f"port {port} is taken by something that is not a face server.")
        return 1

    print(f"face server on {url}")
    # Warm the TTS backend off the critical path: local Kokoro takes ~3s to
    # load the ONNX model on first use, which should happen now, not on the
    # first word of the first reply. (In cloud mode this warms the
    # connection pool instead — a fraction of a cent.)
    threading.Thread(
        target=lambda: voice.tts("Systems online."), daemon=True
    ).start()
    try:
        workshop = create_workshop()
        print(f"workshop (designs) on http://localhost:{config.WORKSHOP_PORT}/")
    except OSError:
        workshop = None  # port taken — most likely a stale workshop; not fatal
    started = time.monotonic()
    proc = launch_window(url)
    try:
        proc.wait()
        # A Windows browser that was already running delegates the window to
        # its existing process and exits at once — keep serving in that case.
        if time.monotonic() - started < 5:
            print("window delegated to a running browser — server stays up, Ctrl-C to stop.")
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        APPROVALS.deny_all("shutdown")  # never leave a waiter blocked
        server.shutdown()
        server.server_close()
        if workshop is not None:
            workshop.shutdown()
            workshop.server_close()
        # A browser left running dies noisily when the process exits under
        # it; no-op if no voice turn ever used a browser tool.
        from ..browser import SESSION

        SESSION.stop(trace_name="face")
    return 0
