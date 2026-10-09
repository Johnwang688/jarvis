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
  GET  /sessions the saved conversations, current one first
  POST /session  {"new": true} | {"id": ...} -> switch conversations

One Agent lives for the whole server process, so a voice session is a real
conversation with memory — and it is bound to a session on disk, so that
memory now survives a restart (see sessions.py). Switching sessions rebuilds
the agent around the other transcript. Dangerous tools are gated in the window
rather than hard-denied (see approvals.py): dispatch() runs them *unguarded*
when approve is None, so the agent here is always constructed with a real
approver.
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
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import agent as agent_mod
from .. import (
    avatars,
    config,
    discord_agent,
    discord_approvals,
    models as models_mod,
    permissions,
    sessions,
    voice,
)
from .. import tasks as tasks_mod
from ..tools import secrets as secrets_mod
from ..tools import voicectl, whiteboardctl
from .approvals import ApprovalBroker


# Words that cannot end a spoken phrase. Articles, auxiliaries, prepositions,
# conjunctions, determiners and degree adverbs all bind forward to whatever
# follows them, so a chunk boundary landing after one is heard as a stumble
# rather than a pause. The first-chunk clamp below falls back to the last
# space when it finds no clause boundary, and that space lands wherever it
# lands: "...and found the migration is" / "complete." is a split this really
# produced. Backing off a word or two costs nothing; the cut is permanent.
_BINDING = frozenset("""
a an the this that these those my your his her its our their
every each some any no all both another such
is are was were be been being am
do does did have has had will would shall should can could may might must
of to in on at by for with from into onto over under about across through
and or but nor so yet as than if while when where because although though
very quite rather entirely completely fairly really extremely
""".split())


def _word_cut(head: str, limit: int, floor: int = 20) -> int:
    """Last space before `limit` that does not strand a binding word.

    Walks back a word at a time while the chunk would end on something that
    binds to what comes next, and gives up rather than cutting the opener
    below `floor` — an opener too short to be worth speaking is its own
    defect. Falling back to the plain last space is always available, so this
    can only improve the cut, never fail to make one.
    """
    space = head.rfind(" ", 0, limit)
    if space < floor:
        return 0
    at = space
    while at >= floor:
        last = head[:at].rsplit(" ", 1)[-1].lower().strip(",;:.—-")
        if last not in _BINDING:
            return at
        nxt = head.rfind(" ", 0, at)
        if nxt < floor:
            break
        at = nxt
    return space


def _merges(out: list[str], part: str, max_len: int, first_min: int) -> bool:
    """Should `part` be glued onto the chunk before it?

    There are two reasons to merge and they are *not* symmetric, which is the
    bug this function exists to hold apart (2026-08-18). A tiny **incoming**
    part ("Done.", "A tiny one.") should never be synthesized as its own
    chunk. A tiny **preceding** chunk normally should absorb what follows —
    except when it is the opener, which is the one chunk that wants to stay
    short, because it is the only one time-to-first-speech is measured on.

    Treating those two as one rule swallowed a perfect 20-character opener
    into a 98-character chunk, which the clamp then split between "is" and
    "complete." Measured against local Kokoro: 607ms -> 1633ms to first audio,
    and an audible break mid-phrase.
    """
    if len(out[-1]) + len(part) + 1 >= max_len:
        return False
    if len(part) < 25:
        return True  # a stray fragment: glue it back
    if len(out[-1]) < 25:
        # The previous chunk is tiny. Absorb what follows — unless it is the
        # opener and already long enough to stand on its own.
        return not (len(out) == 1 and len(out[-1]) >= first_min)
    return False


def _sentences(
    text: str, max_len: int = 300, first_max: int = 90, first_min: int = 12
) -> list[str]:
    """Split a reply into speakable chunks: sentences, tiny ones merged.

    This is what makes streaming TTS work — the first chunk synthesizes in
    well under a second, so speech starts while the rest is still rendering.
    Synthesis time scales with text length, and the first chunk is the one
    the owner is waiting on, so it gets clamped extra short at a clause
    boundary — "Sure," starts playing while the rest of the sentence renders.
    """
    # Line breaks split too, not just sentence enders: a stripped-down markdown
    # list ("Fixed the bug\nRan 2 tests") carries no terminal punctuation, and
    # without this the whole list synthesizes as one long chunk with no pause
    # between the items.
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
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
        if out and _merges(out, part, max_len, first_min):
            out[-1] = out[-1] + " " + part
        else:
            out.append(part)
    if not out:
        return [text.strip()]

    if len(out[0]) > first_max:
        head = out[0]
        cut = max(head.rfind(", ", 0, first_max), head.rfind("; ", 0, first_max),
                  head.rfind(": ", 0, first_max), head.rfind(" — ", 0, first_max))
        take = cut + 1 if cut >= 20 else 0  # keep the comma, drop the space
        if take == 0:
            take = _word_cut(head, first_max)
        if take:
            out[:1] = [head[:take].strip(), head[take:].strip()]
    return out


# ---- speaking ahead of the turn --------------------------------------------

SPECULATION_LIMIT = 6  # chunks rendered ahead; the opening of a reply, not all of it


def _stable_chunks(partial: str) -> list[str]:
    """The chunks of a half-written reply that appending cannot still change.

    `_sentences()` merges a stray fragment backwards, so the last chunk of a
    growing buffer is never safe to trust: "Done." becomes "Done. The tests
    pass." the moment the next sentence arrives. A chunk is settled once the
    one after it exists *and* is long enough that it will not be merged back
    into it — which is the same 25-character rule `_merges` applies, read from
    the other side.
    """
    chunks = _sentences(partial)
    stable: list[str] = []
    for i in range(len(chunks) - 1):
        if len(chunks[i + 1]) < 25:
            break
        stable.append(chunks[i])
    return stable


class _Speculator:
    """Synthesize sentences while the model is still writing them.

    Time-to-first-speech was the full synthesis of the first chunk — measured
    at 607ms to 1633ms against local Kokoro — and all of it was spent *after*
    the turn had already finished, with the owner listening to silence. The
    text has been streaming the whole time (it is what draws the HUD's live
    draft), so the chunks can be built as they appear and simply be waiting.

    This changes nothing about what is spoken or when. Audio still goes out
    only once `run_turn` has returned, in order, exactly as before — the
    owner's call, and the safe one: a sentence spoken early is a sentence a
    later tool call can contradict. It is a cache, keyed on the chunk **text**
    rather than its position, so a guess that does not match the finished
    reply is thrown away and resynthesized. Being wrong costs CPU, never
    correctness.

    It is deliberately cautious about being wrong, because local Kokoro
    serializes on one model instance: a wasted synthesis holds the lock the
    chunk actually being waited on needs. Hence `_stable_chunks`, the limit,
    and the reset on `interim_text`. Pocket TTS voices serialize on their own
    lock the same way (jarvis/pocket.py), so the caution applies per backend.
    """

    def __init__(self, pool: ThreadPoolExecutor):
        self.pool = pool
        self.buf = ""
        self.ready: dict[str, Future] = {}
        self.hits = 0

    def feed(self, piece: str) -> None:
        # The whole body is guarded, not just the synthesis. This runs inside
        # `llm.chat`'s streaming loop by way of on_delta, so anything raised
        # here comes out of the middle of the model call — a turn must never
        # fail because a guess about it did, and "the part I thought could
        # fail" is not the same promise.
        try:
            self.buf += piece
            if len(self.ready) >= SPECULATION_LIMIT:
                return
            speech = voice.speakable(self.buf)
            for chunk in _stable_chunks(speech):
                if chunk in self.ready:
                    continue
                if len(self.ready) >= SPECULATION_LIMIT:
                    break
                self.ready[chunk] = self.pool.submit(voice.tts, chunk)
        except Exception:
            pass

    def restart(self) -> None:
        """A step that ended in a tool call: what was said was thinking out
        loud on the way to it, not the answer, so none of it will be spoken."""
        self.buf = ""
        self.discard()

    def take(self, chunk: str) -> Future | None:
        future = self.ready.pop(chunk, None)
        if future is not None:
            self.hits += 1
        return future

    def discard(self) -> None:
        for future in self.ready.values():
            future.cancel()
        self.ready.clear()


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

# The conversation on disk this window is talking into. main() sets it; the
# HUD switches it through POST /session. None means an unsaved conversation,
# which only happens if a caller runs the server without picking one.
_session: sessions.Session | None = None

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


# Where a turn's streamed text goes while that turn is running. Set under
# `_agent_lock` for the life of one /converse request and cleared afterwards,
# which is safe because the lock already serialises turns.
#
# Deltas ride the **turn stream**, not SSE, and that is not an accident: SSE is
# a separate connection with no ordering guarantee against it, so a delta could
# arrive after the `meta` line that carries the finished reply and append stale
# text underneath it. Phases live on the turn stream for the same reason.
_delta_sink = None
_speculator: "_Speculator | None" = None


def _agent_event(kind: str, data) -> None:
    if kind == "delta":
        sink = _delta_sink
        if sink is not None:
            sink(str(data))
        return
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
        # It is also the signal that everything streamed so far belongs to a
        # step that is not the answer, so anything synthesized from it is
        # wrong and is holding the TTS lock the real chunks will want.
        spec = _speculator
        if spec is not None:
            spec.restart()
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
# The away-from-desk half of the gate: the same question, DMed to the owner
# and answered with YES / NO / ALWAYS. Inert unless Discord is connected.
DISCORD_APPROVALS = discord_approvals.DiscordApprovals()
APPROVALS = ApprovalBroker(
    broadcast=broadcast,
    viewers=_viewers,
    on_always=permissions.add_allow,
    remote=DISCORD_APPROVALS,
)
DISCORD_APPROVALS.bind(APPROVALS)

# The MUTE button and the spoken "mute yourself" both land on voicectl; the
# broadcast keeps every open window's toggle in sync with reality.
voicectl.on_change(lambda muted: broadcast("mute", {"muted": muted}))

# Switching avatars from anywhere — the HUD picker, `jarvis avatar`, or the
# set_avatar tool — redraws every open window: name, wake phrases, and face.
avatars.on_change(lambda av: broadcast("avatar", av.describe()))

# whiteboard_close broadcasts a self-close signal. Only whiteboard.html
# handles it; jarvis.html ignores it on purpose (see whiteboardctl).
whiteboardctl.on_close(lambda: broadcast("wb_close", {}))

# Background-task lifecycle (started / done / failed / cancelled) shows in the
# OPERATIONS ticker, so delegated work is visible without asking about it.
tasks_mod.set_notify(broadcast)


def _get_agent() -> agent_mod.Agent:
    global _agent
    if _agent is None:
        _agent = agent_mod.Agent(
            system=VOICE_SYSTEM,
            max_steps=config.FACE_MAX_STEPS,
            approve=permissions.gate(APPROVALS.approver()),
            on_event=_agent_event,
            should_stop=_cancel.is_set,
            session=_session,
        )
    return _agent


def set_session(session: sessions.Session) -> sessions.Session:
    """Point the conversation at another session, dropping the live agent.

    Rebuilding rather than mutating is the honest move: the Agent's message
    list *is* the conversation, so the new one starts from the saved
    transcript with today's system prompt on top. Callers hold `_agent_lock`
    so this can never land mid-turn.
    """
    global _agent, _session
    _session = session
    _agent = None
    broadcast("session", session.describe())
    return session


# ---- discord mode (the gateway listener) -----------------------------------

# The conversational core (persistent agent, spoken-turn approval isolation)
# is shared with `jarvis daemon` — see discord_agent.py. This file only adds
# the HUD hooks: turns tick the ops feed and drop notes in COMMS.
RESPONDER = discord_agent.DiscordResponder(
    broker=APPROVALS,
    channel=DISCORD_APPROVALS,
    on_event=_agent_event,
    on_note=lambda text: broadcast("note", {"text": text}),
)


def _discord_turn(text: str, channel_id: str, spoken: bool = False) -> str:
    return RESPONDER.turn(text, channel_id, spoken)


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
                    "llm": models_mod.tier("orchestrator"),
                    "effort": models_mod.effort_for(models_mod.tier("orchestrator")),
                    "stt": config.STT_MODEL,
                    "tts": config.TTS_MODEL,
                    "voice": voice.selected_voice(),
                    "speed": config.TTS_SPEED,
                    # Who he is presenting as: name, wake phrases, accent, and
                    # whether there is a face to draw. The art itself is a
                    # separate resource (see /avatar.svg).
                    "avatar": avatars.active().describe(),
                    "muted": voicectl.is_muted(),
                    "permissions": permissions.mode(),
                    "allowlist": len(permissions.load_allowlist()),
                    # Includes the tail so a window opening onto a resumed
                    # conversation can redraw its log instead of starting blank.
                    "session": (
                        {**_session.describe(), "tail": _session.tail(8)}
                        if _session
                        else None
                    ),
                }
            )
            return
        if self.path.split("?", 1)[0] == "/avatar.svg":
            self._avatar_svg()
            return
        if self.path == "/avatars":
            self._json_reply(
                {
                    "current": avatars.active().slug,
                    "avatars": [a.describe() for a in avatars.available()],
                }
            )
            return
        if self.path == "/voices":
            # `override` is null when the avatar/config voice is speaking —
            # the picker's AVATAR DEFAULT row marks itself active off it.
            self._json_reply(
                {
                    "current": voice.selected_voice(),
                    "override": voice._voice_override,
                    "voices": voice.catalog(),
                }
            )
            return
        if self.path == "/models":
            self._json_reply(models_mod.describe())
            return
        if self.path == "/models/catalog":
            # Every eligible model on OpenRouter, filtered in the window: 290
            # entries is small enough to send whole, and a search box that
            # round-trips per keystroke is a search box nobody uses.
            try:
                entries = [m.describe() for m in models_mod.catalog()]
            except LookupError as exc:
                self._json_error(503, str(exc))
                return
            self._json_reply(
                {
                    "models": entries,
                    "roster": models_mod.roster().models,
                    "stale": models_mod.stale_reason(),
                }
            )
            return
        if self.path == "/sessions":
            recent = [s.describe() for s in sessions.recent(12)]
            current = _session.describe() if _session else None
            # A conversation with nothing said in it is not on disk yet, so
            # put the live one in the list explicitly — the picker should
            # always show where you are.
            if current and all(s["id"] != current["id"] for s in recent):
                recent.insert(0, current)
            self._json_reply({"current": current, "sessions": recent})
            return
        super().do_GET()

    def _avatar_svg(self):
        """One avatar's art, sanitized, as its own resource.

        Served rather than inlined so the HUD can render it in an `<img>`,
        which cannot run script or fetch anything however the file is written.
        `avatars.sanitize_svg` has already stripped it; the CSP is the belt to
        that suspenders, and the reason this file never goes near innerHTML.

        `?slug=` picks one — the picker draws every avatar's face at thumbnail
        size — and defaults to the active one. `avatars.load()` validates the
        slug against its own pattern, so this is not a path into the
        filesystem: a slug with a slash or a dot in it resolves to nothing.
        """
        query = parse_qs(urlparse(self.path).query)
        slug = (query.get("slug") or [""])[0]
        av = avatars.load(slug) if slug else avatars.active()
        art = avatars.svg(av) if av else None
        if not art:
            self._json_error(404, "no avatar art")
            return
        body = art.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _switch_voice(self):
        """Change the active voice for subsequent speech (any catalog name;
        an empty voice clears the override back to the avatar's)."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            self._json_error(403, "cross-origin voice switch refused")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            selected = voice.set_voice(str(data.get("voice", "")))
        except LookupError as exc:
            self._json_error(404, str(exc))
            return
        except Exception as exc:
            self._json_error(400, f"{type(exc).__name__}: {exc}")
            return
        broadcast("voice", {"voice": selected})
        self._json_reply({"voice": selected})

    def _switch_model(self):
        """Choose which model the loop runs on; "" returns to the default.

        Same-origin like every other control. Takes `_agent_lock`, so a switch
        requested mid-turn lands *after* that turn rather than changing models
        under a transcript halfway through — the reason /session takes it too.

        The live agent is mutated rather than rebuilt: the model is a property
        of the next request, not of the conversation, so switching must not
        cost the transcript the way switching sessions deliberately does.
        """
        if not self._same_origin("model switch"):
            return
        try:
            data = self._read_json()
            chosen = models_mod.select(str(data.get("model", "")))
        except models_mod.NotOnRoster as exc:
            self._json_error(404, str(exc))
            return
        except Exception as exc:
            self._json_error(400, f"{type(exc).__name__}: {exc}")
            return
        with _agent_lock:
            if _agent is not None:
                _agent.model = models_mod.tier("orchestrator")
        payload = models_mod.describe()
        broadcast("model", payload)
        self._json_reply(payload)

    def _edit_roster(self):
        """Add or remove one model on the owner's shortlist: {"add"|"remove": id}.

        `add` is validated against the catalog inside models.add — a roster
        entry is something the owner can then select, and a selection that
        cannot call tools fails every later turn far from this decision.
        """
        if not self._same_origin("roster edit"):
            return
        try:
            data = self._read_json()
        except Exception as exc:
            self._json_error(400, f"{type(exc).__name__}: {exc}")
            return
        add_id = str(data.get("add") or "").strip()
        remove_id = str(data.get("remove") or "").strip()
        effort_id = str(data.get("model") or "").strip()
        given = [x for x in (add_id, remove_id, effort_id) if x]
        if len(given) != 1:
            self._json_error(
                400,
                'expected exactly one of {"add": id} / {"remove": id} / '
                '{"model": id, "effort": level}',
            )
            return
        if "effort" in data and not effort_id:
            # An effort riding an add or a remove used to be dropped silently.
            self._json_error(400, 'effort goes with {"model": id, "effort": level} only')
            return
        try:
            if add_id:
                models_mod.add(add_id)
            elif remove_id:
                models_mod.remove(remove_id)
            else:
                # "" clears the pin and hands the model back to the global
                # default, which is the way out of any choice made here.
                models_mod.set_effort(effort_id, str(data.get("effort") or ""))
        except models_mod.RosterRefused as exc:
            # Unpinning the last model, or the default nothing else replaces.
            self._json_error(409, str(exc))
            return
        except models_mod.NotEligible as exc:
            self._json_error(400, str(exc))
            return
        except models_mod.NotOnRoster as exc:
            self._json_error(404, str(exc))
            return
        payload = models_mod.describe()
        # Removing the selected model clears the selection, so the live agent
        # can fall back here as well — the same reason the switch mutates it.
        with _agent_lock:
            if _agent is not None:
                _agent.model = models_mod.tier("orchestrator")
        broadcast("model", payload)
        self._json_reply(payload)

    def _same_origin(self, what: str) -> bool:
        """True when this request came from the window, not another page."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            self._json_error(403, f"cross-origin {what} refused")
            return False
        return True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _switch_avatar(self):
        """Change who he is presenting as. Same-origin, like /approve.

        Not destructive, but it moves the wake word and the name — nothing
        outside the window should be able to reach in and do that.
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            self._json_error(403, "cross-origin avatar switch refused")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            av = avatars.set_active(str(data.get("slug", "")))
        except LookupError as exc:
            self._json_error(404, str(exc))
            return
        except Exception as exc:
            self._json_error(400, f"{type(exc).__name__}: {exc}")
            return
        self._json_reply(av.describe())

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
            # agent thread blocked on a card nobody can see — but a question
            # that also went out as a DM is still answerable, so it survives.
            if _viewers() == 0 and APPROVALS.pending_count:
                APPROVALS.deny_all("window-closed", include_remote=False)

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
        if self.path == "/session":
            self._switch_session()
            return
        if self.path == "/avatar":
            self._switch_avatar()
            return
        if self.path == "/model":
            self._switch_model()
            return
        if self.path == "/models":
            self._edit_roster()
            return
        if self.path == "/voice":
            self._switch_voice()
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

    def _switch_session(self):
        """Change conversations: {"new": true} or {"id": "<session id>"}.

        Same-origin like /approve and /cancel. Nothing here is destructive —
        a switch never deletes a transcript — but which conversation the
        window is talking into is the owner's call, not another page's.

        Takes `_agent_lock`, so a switch requested mid-turn waits for that
        turn to finish and be saved rather than swapping the transcript out
        from under it.
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            self._json_error(403, "cross-origin session switch refused")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._json_error(400, f"{type(exc).__name__}: {exc}")
            return

        session_id = str(data.get("id") or "").strip()
        if not session_id and not data.get("new"):
            self._json_error(400, 'expected {"new": true} or {"id": ...}')
            return

        with _agent_lock:
            if session_id:
                target = sessions.load(session_id)
                if target is None:
                    self._json_error(404, f"no session {session_id!r}")
                    return
            else:
                target = sessions.new("face")
            set_session(target)
            reply = target.describe()
            # The window redraws its log from this, so a resumed conversation
            # looks resumed instead of blank.
            reply["tail"] = target.tail(8)
        self._json_reply(reply)

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
            if heard:
                user_input = (
                    "[Voice transcription notice: this message was transcribed from speech and may contain errors. If it does not make sense, use prior context to infer the intended meaning when possible; otherwise ask the owner for clarification.]\n\n"
                    + user_input
                )

            self._nd({"type": "phase", "phase": "thinking"})
            t0 = time.monotonic()
            global _delta_sink, _speculator
            # Started before the turn rather than after it: the chunks of the
            # reply are synthesized as the model writes them, so the first one
            # is already waiting when `run_turn` returns instead of costing
            # another 600-1600ms of silence. Muted turns synthesize nothing at
            # all, so they speculate nothing either.
            pool = ThreadPoolExecutor(max_workers=2)
            spec = None if voicectl.is_muted() else _Speculator(pool)
            with _agent_lock:
                # Cleared here, under the lock: a cancel aimed at the previous
                # turn must not carry over and kill this one before it starts.
                _cancel.clear()

                def _sink(text: str) -> None:
                    if spec is not None:
                        spec.feed(text)
                    # A dead client mid-generation is normal (barge-in aborts
                    # the fetch), and must not take the turn down with it.
                    try:
                        self._nd({"type": "delta", "text": text})
                    except Exception:
                        pass

                _delta_sink = _sink
                _speculator = spec
                try:
                    turn = _get_agent().run_turn(user_input, images=images or None)
                finally:
                    _delta_sink = None
                    _speculator = None
            agent_ms = round((time.monotonic() - t0) * 1000)

            if turn.cancelled:
                # Interrupted mid-thought. No reply, and above all no speech —
                # the whole point was to stop him talking over the correction.
                if spec is not None:
                    spec.discard()
                pool.shutdown(wait=False, cancel_futures=True)
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
                # (which also means muted turns cost no TTS). Mute can be
                # flipped mid-turn, so anything speculated before it was is
                # thrown away here rather than spoken.
                if spec is not None:
                    spec.discard()
                pool.shutdown(wait=False, cancel_futures=True)
                self._nd({"type": "done", "tts_ms": 0, "muted": True})
                return

            # Markdown comes off before chunking, not just inside voice.tts():
            # _sentences() clamps the first chunk by length, and counting
            # asterisks would cut speech in the wrong place. A reply with
            # nothing speakable left (a bare code block) is text-only.
            speech = voice.speakable(reply[:MAX_SAY_CHARS])
            if not speech:
                if spec is not None:
                    spec.discard()
                pool.shutdown(wait=False, cancel_futures=True)
                self._nd({"type": "done", "tts_ms": 0})
                return

            # The first sentence takes ~0.5-1s to synthesize. Without this the
            # window sat on the last tool's label through all of it.
            self._nd({"type": "phase", "phase": "composing"})
            tts_total = 0
            # Chunks stream in order. Anything the speculator already built
            # while the model was writing is claimed by its exact text; a guess
            # that does not match the finished reply is simply a miss, and is
            # synthesized here exactly as it always was.
            try:
                sentences = _sentences(speech)
                futures = [
                    (spec.take(s) if spec is not None else None) or pool.submit(voice.tts, s)
                    for s in sentences
                ]
                if spec is not None:
                    spec.discard()  # guesses the reply did not use
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
                self._nd(
                    {
                        "type": "done",
                        "tts_ms": tts_total,
                        # How much of the reply was already spoken-ready when
                        # the turn ended. Worth reporting: it is the whole
                        # point of the speculator, and a number that quietly
                        # goes to zero is how you find out it stopped working.
                        "prerendered": (spec.hits if spec is not None else 0),
                        "chunks": len(sentences),
                    }
                )
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


def main(
    page: str = "jarvis.html",
    port: int = PORT,
    session: sessions.Session | None = None,
) -> int:
    global _session
    _session = session or sessions.new("face")
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
    listener = None
    if config.DISCORD_TOKEN_PATH.exists():
        from .. import daemon as daemon_mod

        if daemon_mod.is_running():
            # The daemon owns the gateway: a second IDENTIFY would double
            # every reply, and DM answers route to *its* broker — so this
            # process neither listens nor asks remotely. Approvals stay on
            # the card, which is right for the at-the-desk surface.
            print("[discord] a jarvis daemon owns the gateway — approvals stay on the HUD card")
            APPROVALS.detach_remote()
        else:
            try:
                from .. import discord_gateway

                listener = discord_gateway.GatewayListener(run_turn=_discord_turn)
                listener.start()
            except Exception as exc:
                print(f"[discord] listener not started: {type(exc).__name__}: {exc}")
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
        if listener is not None:
            listener.stop()
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
