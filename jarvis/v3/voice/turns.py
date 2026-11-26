"""Send transcribed utterances through chat and hand out completed sentences."""

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import logging
from math import isfinite
import re
from threading import Lock
import time
from urllib.parse import urlsplit
from uuid import uuid4

from ..work import GENERAL_WORKSPACE
from .brain import VoiceBrain
from .sentences import MIN_SENTENCE as MIN_SENTENCE, Sentences


FRESH_AFTER_S = 600
MAX_APPROVAL_WINDOW_S = 600
LOOKAHEAD_SENTENCES = 2
TRACE_LIMIT = 200
ERROR_SENTENCE = "Something went wrong with that."
_ERROR_AFTER_WRITE = "That was done, but something went wrong afterwards."
_FILLER = frozenset({"um", "uh", "hmm", "mm", "ah"})
_QUIET = frozenset({"stop", "stop talking", "quiet", "be quiet", "shut up"})
_CANCEL = frozenset({"cancel", "cancel that", "never mind", "nevermind", "stop that"})
_NAME = re.compile(r"^(?:hey )?jarvis(?: |$)")
_TARGETS = {
    "vault_add": ("Entry",),
    "vault_patch": ("Entry",),
    "vault_archive": ("Entry",),
    "calendar_create": ("Title", "Start"),
    "calendar_update": ("Current title", "New title", "Event"),
    "calendar_delete": ("Current title", "Event"),
    "work_start": ("Project", "Thread title"),
    "work_send": ("Thread", "In project"),
}
# The PC tools are asked about in plain words: the app's name, the project,
# or the link's host (a whole address is not something to listen to).
_SPOKEN = {
    "app_open": ("App", "Open {}"),
    "folder_open": ("Project", "Open the folder for {}"),
    "delegate": ("Project", "Hand this to Sol in {}"),
    "link_open": ("URL", "Open a link to {}"),
}
LOG = logging.getLogger(__name__)


def _command_words(text):
    words = "".join(char if char.isalpha() or char.isdigit() else " " for char in text)
    return " ".join(words.lower().split())


def is_stop_word(text):
    command = _command_words(text)
    return command in _QUIET or command in _CANCEL


def _approval_sentence(item):
    # The card has already redacted each display field. Only use its title
    # and the target labels, keeping bodies, briefs and messages out of speech.
    card = item["card"]
    rows = {row["label"]: row["value"] for row in card["rows"]}
    if item["tool"] in _SPOKEN:
        label, words = _SPOKEN[item["tool"]]
        value = " ".join(str(rows.get(label) or "").split())
        if item["tool"] == "delegate" and value == GENERAL_WORKSPACE:
            return "Hand this to Sol. Approve?"
        if item["tool"] == "link_open":
            try:
                value = urlsplit(value).hostname or ""
            except ValueError:
                value = ""
        if value:
            return f"{words.format(value.rstrip('.!?'))}. Approve?"
    targets = [rows[label] for label in _TARGETS.get(item["tool"], ()) if rows.get(label)]
    target = " ".join(", ".join(targets).split()).rstrip(".!?")
    title = card["title"].rstrip(".!?")
    return f"{title} for {target}. Approve?" if target else f"{title}. Approve?"


@dataclass
class _VoiceTurn:
    text: str
    heard_at: float
    turn: str | None = None
    steer: int | None = None
    muted: bool = False
    tools: list = field(default_factory=list)
    approvals: set = field(default_factory=set)
    pending_audio: set = field(default_factory=set)
    last_sentence_id: str | None = None
    replied: bool = False
    sentences: Sentences = field(default_factory=Sentences)
    streamed: list = field(default_factory=list)
    lookahead: deque[str] = field(default_factory=deque)
    ended_without_tools: bool = False
    changed_state: bool = False
    reported: bool = False
    code_built_sentences: list = field(default_factory=list)
    sentence_count: int = 0
    sentence_chars: int = 0
    delegate_reminders: int = 0
    first_ack_s: float | None = None
    first_say_s: float | None = None
    end_s: float | None = None
    speech_ended_at: float | None = None
    silence_ms: int | None = None
    decode_ms: float | None = None
    first_audio_ms: float | None = None
    asking: str | None = None
    voice_origin: bool = False


class VoiceTurns:
    def __init__(self, brain: VoiceBrain, *, clock=time.monotonic, on_event):
        self._brain = brain
        self._clock = clock
        self._on_event = on_event
        self._listeners = []
        self._lock = Lock()
        self._incoming = {}
        self._turn = None
        self._trace = deque(maxlen=TRACE_LIMIT)
        self._events = deque()
        self._notifying = False
        self._state = "idle"
        self._closed = False
        self._acked_turn = None
        self._spoken_turn = None
        self._unsubscribe = brain.subscribe(self._brain_event)

    def subscribe(self, callback):
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def warm(self):
        warm = getattr(self._brain, "warm", None)
        if warm is not None:
            warm()

    @contextmanager
    def _update(self):
        try:
            with self._lock:
                yield
        finally:
            self._notify()

    def _emit(self, kind, **fields):
        self._events.append((kind, fields))

    def _notify(self):
        with self._lock:
            if self._notifying or not self._events:
                return
            self._notifying = True
        while True:
            with self._lock:
                if not self._events:
                    self._notifying = False
                    return
                kind, fields = self._events.popleft()
            if kind in ("asking", "say"):
                turn_id = fields.pop("turn") if kind == "asking" else fields["turn"]
                pending = self._brain.pending_approvals(turn_id)
            with self._lock:
                if kind == "say":
                    turn = self._turn
                    if turn is None or fields["id"] not in turn.pending_audio:
                        continue
                    if pending:
                        turn.pending_audio.remove(fields["id"])
                        self._state_to("thinking" if turn.end_s is None else "idle")
                        continue
                    if turn.first_say_s is None:
                        turn.first_say_s = self._clock() - turn.heard_at
                    self._spoken_turn = turn.turn
                    turn.last_sentence_id = fields["id"]
                    turn.sentence_count += 1
                    turn.sentence_chars += len(fields["text"])
                    if fields.pop("code_built"):
                        turn.code_built_sentences.append(fields["text"])
                elif kind == "asking":
                    turn = self._turn
                    card = next((item for item in pending
                                 if item["id"] == fields["id"] and item["digest"] == fields["digest"]), None)
                    if (self._closed or turn is None or turn.muted or turn.turn != turn_id
                            or fields["id"] in turn.approvals or card is None):
                        continue
                    remaining = card.get("expires_in", 0)
                    if type(remaining) is int or (type(remaining) is float and isfinite(remaining)):
                        fields["expires_in"] = min(max(remaining, 0), MAX_APPROVAL_WINDOW_S)
                    turn.approvals.add(fields["id"])
                    turn.asking = fields["id"]
                listeners = tuple(self._listeners)
            for callback in (self._on_event, *listeners):
                try:
                    callback(kind, **fields)
                except Exception:
                    LOG.warning("A voice event callback failed.")
                    if kind == "say":
                        self.spoken(fields["id"])

    def _state_to(self, state):
        if self._state != state:
            self._state = state
            self._emit("state", state=state)

    def _say(self, turn, sentence, *, code_built=False):
        sentence_id = uuid4().hex
        turn.pending_audio.add(sentence_id)
        turn.reported = True
        self._state_to("speaking")
        self._emit("say", id=sentence_id, text=sentence, turn=turn.turn, code_built=code_built)

    def _feed(self, turn, text, *, final=False):
        # Hold each sentence until two later sentences have begun. The
        # unfinished fragment counts as one start if it has speakable text.
        turn.lookahead.extend(turn.sentences.feed(text, final=final))
        keep = 0 if final else LOOKAHEAD_SENTENCES - int(turn.sentences.has_fragment)
        while len(turn.lookahead) > keep:
            self._say(turn, turn.lookahead.popleft())

    @staticmethod
    def _reset_sentences(turn):
        turn.streamed.clear()
        turn.sentences = Sentences()
        turn.lookahead.clear()

    def _finish(self, turn):
        turn.end_s = self._clock() - turn.heard_at
        self._reset_sentences(turn)

    def _activate(self, turn, event):
        previous = self._turn
        if previous is not None:
            if previous.pending_audio:
                previous.pending_audio.clear()
                self._emit("speech_stop")
            if previous.end_s is None:
                # The new utterance supersedes the voice response, even where
                # chat delivers both inputs in the same underlying turn.
                self._finish(previous)
        turn.turn = event["turn"]
        turn.steer = event["seq"] if event["kind"] == "steer" else None
        turn.voice_origin = (turn.steer is None or (
            previous is not None and previous.turn == turn.turn and previous.voice_origin))
        self._turn = turn
        self._trace.appendleft(turn)
        self._state_to("thinking")

    def heard(self, text, *, speech_ended_at=None, silence_ms=None, decode_ms=None):
        text = " ".join(text.split())
        words = _command_words(text)
        command = _NAME.sub("", words)
        filler_words = re.findall(r"\w+", text.lower())
        reason = None
        if not text:
            reason = "empty"
        elif len(text) == 1:
            reason = "single_character"
        elif filler_words and all(word in _FILLER for word in filler_words):
            reason = "filler"
        elif words and not command:
            reason = "wake-only"
        if reason:
            with self._update():
                if not self._closed:
                    self._emit("ignored", reason=reason)
            return
        if command in _QUIET or command in _CANCEL:
            self._interrupt(cancel=command in _CANCEL)
            return

        input_id = uuid4().hex
        with self._lock:
            if self._closed:
                return
            self._incoming[input_id] = _VoiceTurn(text=text, heard_at=self._clock(),
                                                speech_ended_at=speech_ended_at, silence_ms=silence_ms,
                                                decode_ms=decode_ms)
        try:
            self._brain.send(text, steer=True, input_id=input_id, fresh_after=FRESH_AFTER_S)
        except (ValueError, RuntimeError):
            with self._update():
                if self._incoming.pop(input_id, None) is not None and not self._closed:
                    self._emit("error")

    def interrupt(self):
        self._interrupt(cancel=False)

    def can_interrupt(self):
        """Whether this request can still produce speech, including between sentences."""
        with self._lock:
            turn = self._turn
            return (any(not item.muted for item in self._incoming.values())
                    or (turn is not None and not turn.muted and (turn.end_s is None or bool(turn.pending_audio))))

    def has_audio(self, sentence_id):
        with self._lock:
            return self._turn is not None and sentence_id in self._turn.pending_audio

    def is_asking(self, approval_id):
        with self._lock:
            turn = self._turn
            return (approval_id is not None and turn is not None and turn.asking == approval_id
                    and not turn.muted)

    def answered(self, sentence_id):
        """Check whether this was the last sentence of a completed, unmuted reply."""
        with self._lock:
            turn = self._turn
            return (turn is not None and turn.replied and not turn.muted
                    and not turn.pending_audio and turn.last_sentence_id == sentence_id)

    def _interrupt(self, *, cancel):
        try:
            with self._lock:
                if self._closed:
                    return
                self._emit("speech_stop")
                for turn in self._incoming.values():
                    turn.muted = True
                turn = self._turn
                if turn is not None:
                    turn.muted = True
                    turn.pending_audio.clear()
                    self._reset_sentences(turn)
                    self._state_to("thinking" if turn.end_s is None else "idle")
            if cancel:
                self._brain.stop()
        finally:
            self._notify()

    def spoken(self, sentence_id):
        with self._update():
            turn = self._turn
            if turn is not None and sentence_id in turn.pending_audio:
                turn.pending_audio.remove(sentence_id)
                if turn.end_s is not None and not turn.pending_audio:
                    self._state_to("idle")

    def first_audio(self, sentence_id, at):
        with self._lock:
            turn = self._turn
            if (turn is not None and (sentence_id in turn.pending_audio or sentence_id in turn.approvals)
                    and turn.first_audio_ms is None and turn.speech_ended_at is not None):
                turn.first_audio_ms = (at - turn.speech_ended_at) * 1000

    def _brain_event(self, event):
        kind = event["kind"]
        turn_id = event["turn"]
        pending = ()
        if kind in ("steer", "approval_pending", "tool_end"):
            with self._lock:
                current = self._turn
                relevant = event.get("input_id") in self._incoming or (
                    current is not None and current.turn == turn_id and current.end_s is None
                    and (kind != "tool_end" or current.asking)
                )
            if relevant:
                pending = self._brain.pending_approvals(turn_id)
        with self._update():
            if self._closed:
                return
            incoming = self._incoming.pop(event.get("input_id"), None)
            if incoming is not None:
                self._activate(incoming, event)
            turn = self._turn
            if turn is None or turn.end_s is not None:
                return
            if kind == "user" and turn.steer is not None and any(
                row["seq"] == turn.steer for row in event.get("inputs", ())
            ):
                turn.turn = turn_id
                turn.steer = None
                turn.voice_origin = len(event["inputs"]) == 1
            if turn.turn != turn_id:
                return
            for item in pending:
                if turn.voice_origin and item["id"] not in turn.approvals:
                    self._state_to("thinking")
                    self._emit("asking", turn=turn_id, id=item["id"], digest=item["digest"],
                               tool=item["tool"], sentence=_approval_sentence(item))
            if kind == "steering_delivered" and turn.steer in event["inputs"]:
                turn.steer = None
            if turn.steer is not None:
                # A steer received during the last model request will be an
                # input of the next chat turn. Ignore the older request's prose.
                if kind in ("error", "stopped"):
                    self._end(turn, event)
                return
            if kind == "tool_start":
                if not turn.muted and turn.turn not in (self._acked_turn, self._spoken_turn):
                    self._acked_turn = turn.turn
                    turn.first_ack_s = self._clock() - turn.heard_at
                    self._emit("ack")
            elif kind == "tool_end":
                if turn.asking and not pending:
                    turn.asking = None
                    self._emit("approval_end")
                turn.tools.append({"name": event["name"], "outcome": event["outcome"]})
                turn.changed_state |= event["changes_state"] and event["outcome"] == "ok"
                turn.reported = False
                if turn.pending_audio:
                    self._state_to("speaking")
            elif kind == "delta" and not turn.muted:
                turn.streamed.append(event["text"])
                self._feed(turn, event["text"])
            elif kind == "delegate_reminder":
                turn.delegate_reminders += 1
            elif kind == "step_end" and not turn.muted:
                turn.ended_without_tools = not event["has_tools"]
                if turn.ended_without_tools:
                    streamed = "".join(turn.streamed)
                    text = event["text"]
                    suffix = text[len(streamed):] if text.startswith(streamed) else ""
                    self._feed(turn, suffix, final=True)
                self._reset_sentences(turn)
            elif kind in ("reply", "error", "stopped"):
                self._end(turn, event)

    def _end(self, turn, event):
        kind = event["kind"]
        if turn.asking:
            turn.asking = None
            self._emit("approval_end")
        if kind == "error":
            self._emit("error")
        if kind == "stopped" and turn.pending_audio:
            turn.pending_audio.clear()
            self._emit("speech_stop")
        if not turn.muted:
            if kind == "error":
                self._say(turn, _ERROR_AFTER_WRITE if turn.changed_state else ERROR_SENTENCE,
                          code_built=True)
            elif kind == "reply":
                turn.replied = True
                # A reply without a final model step can be a code-built
                # message from the brain when it exhausts its step budget.
                if not turn.ended_without_tools:
                    self._feed(turn, event["text"], final=True)
                if turn.tools and not turn.reported:
                    if turn.approvals and not turn.changed_state and any(
                        tool["outcome"] == "refused" for tool in turn.tools
                    ):
                        sentence = "Nothing was done."
                    elif any(tool["outcome"] != "ok" for tool in turn.tools):
                        sentence = "That did not work."
                    elif turn.changed_state:
                        sentence = "Done."
                    else:
                        sentence = "I have nothing to add."
                    self._say(turn, sentence, code_built=True)
        self._finish(turn)
        if not turn.pending_audio:
            self._state_to("idle")

    def trace(self):
        with self._lock:
            return [
                {"turn": turn.turn, "heard_at": turn.heard_at, "text": turn.text,
                 "tools": deepcopy(turn.tools), "approval_asked": bool(turn.approvals),
                 "sentence_count": turn.sentence_count, "sentence_chars": turn.sentence_chars,
                 "delegate_reminders": turn.delegate_reminders,
                 "code_built_sentences": list(turn.code_built_sentences),
                 "silence_ms": turn.silence_ms, "decode_ms": turn.decode_ms,
                 "first_audio_ms": turn.first_audio_ms,
                 "first_ack_s": turn.first_ack_s, "first_say_s": turn.first_say_s, "end_s": turn.end_s}
                for turn in self._trace
            ]

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._incoming.clear()
            turn = self._turn
            self._events.clear()
            if turn is not None:
                if turn.pending_audio:
                    turn.pending_audio.clear()
                    self._emit("speech_stop")
                if turn.end_s is None:
                    self._finish(turn)
            self._state_to("idle")
        self._unsubscribe()
        self._notify()
