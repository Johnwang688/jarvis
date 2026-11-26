"""Join microphone capture, voice turns and far-end playback for one daemon."""

from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
import logging
import re
from threading import Condition, Event, Thread, current_thread
import time
from unicodedata import category
from uuid import uuid4

from jarvis import config, untrusted
from jarvis.v2.quiet import QuietHours

from ..work_watch import Notice
from .approving import ANSWER_DECODE_GRACE_S, ANSWER_WINDOW_S, answer_decision
from .engines import EngineChoice, EngineUnavailable, KokoroVoice
from .hearing import FRAME_MS, Hearing
from .speaking import Speaking
from .turns import is_stop_word


WAKE_NAME = "Jarvis"
WAKE_FOLLOW_S = 6
WAKE_DECODE_LAG_S = 2
FOLLOW_UP_S = config.V3_FOLLOW_UP_S
FOLLOW_LEVEL_DB = config.V3_FOLLOW_LEVEL_DB
ECHO_TAIL_S = 0.3
PLAYED_GRACE_S = 2
APPROVAL_POLL_S = 0.1
MAX_PENDING_NOTICES = 3
NOTICE_CONTEXT_S = 60
_NOTICE_WORDS = {
    "finished": ("Finished", "finished"),
    "failed": ("Failed", "failed"),
    "stopped": ("Stopped", "stopped"),
    "waiting": ("Waiting on you", "is waiting on you"),
}
_NOTICE_QUOTES = str.maketrans({quote: "'" for quote in '"“”„‟«»‘’‚‛‹›'})
ANSWER_WORDS = ("yes", "no", "approve", "deny", "allow", "reject", "cancel")
PROMPT_NAMES = ("Codex", "Claude", "Chrome", "VS Code")
_LEAD_IN = {"ok", "okay", "hey"}
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
LOG = logging.getLogger(__name__)


def _wake_request(text):
    """Return the original text after a leading wake name, or None."""
    words = _WORD.finditer(text)
    for index, word in enumerate(words):
        token = word[0].casefold()
        if token == WAKE_NAME.casefold():
            return text[word.end():].lstrip(" \t\r\n,.:;!?-–—…")
        if index == 2 or token not in _LEAD_IN:
            break
    return None


def _name(engine):
    if isinstance(engine, KokoroVoice):
        device = engine.active_device or engine.device
        return f"kokoro-{'gpu' if device == 'cuda' else device}"
    return getattr(engine, "name", type(engine).__name__)


class _LocalTranscriber:
    """Resolve the local engine once on a warm-up worker."""

    def __init__(self, source):
        self._source = source
        self.ready = Future()

    def load(self):
        try:
            if callable(self._source) and not hasattr(self._source, "transcribe"):
                choice = self._source()
            elif self._source is None:
                choice = EngineChoice(None, None, "no transcriber configured")
            else:
                engine = self._source
                warm = getattr(engine, "warm", None)
                if warm is not None:
                    warm()
                available = getattr(engine, "available", None)
                if available is None or available():
                    choice = EngineChoice(engine, _name(engine), "configured transcriber")
                else:
                    choice = EngineChoice(None, None, "configured transcriber unavailable")
        except Exception:
            choice = EngineChoice(None, None, "transcriber initialization failed")
        self.ready.set_result(choice)
        return choice

    def transcribe(self, samples, *, names):
        engine = self.ready.result().engine
        if engine is None:
            raise EngineUnavailable("transcriber unavailable")
        return engine.transcribe(samples, names=names)

    def close(self):
        engine = self.ready.result().engine
        _close(engine if engine is not None else self._source)


def _close(engine):
    close = getattr(engine, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            LOG.warning("A voice engine could not be closed.")


@dataclass
class _Playback:
    approval_id: str | None
    ends_at: float | None = None
    seconds_sent: float = 0
    deadline: float | None = None
    delivered: bool = False
    notice: Notice | None = None


@dataclass(frozen=True)
class _SentenceVoice:
    synthesize: Callable


@dataclass(frozen=True)
class _CaptureMode:
    generation: int
    asking: str | None
    answer_until: float | None
    echo: bool


class VoiceSession:
    """One link at a time. A worker orders events and audio at the link boundary.

    The input lock protects capture and the worker's published status. Only the
    worker calls the link callbacks, including when a callback reenters the session.
    """

    def __init__(self, turns, *, transcriber, voices, names, on_event=None, on_audio=None,
                 clock=time.monotonic, on_answer=None, approving=None, detector=None,
                 follow_up_s=None, follow_level_db=None, quiet=None):
        self._turns = turns
        self._transcriber = _LocalTranscriber(transcriber)
        self._fast, self._best = voices
        self._names = names
        self._on_event = on_event
        self._on_audio = on_audio
        self._approving = approving
        self.on_answer = (approving.heard if approving is not None else
                          on_answer if on_answer is not None else lambda _text: False)
        self._clock = clock
        self._quiet = QuietHours(config.V2_DATA_DIR).is_quiet if quiet is None else quiet
        self._detector = detector
        self._follow_up_s = FOLLOW_UP_S if follow_up_s is None else follow_up_s
        follow_db = FOLLOW_LEVEL_DB if follow_level_db is None else follow_level_db
        self._follow_ratio = 10 ** (follow_db / 10)
        self._changed = Condition()
        self._mailbox = deque()
        self._pending_notices = 0
        self._active = False
        self._closed = False
        self._generation = self._running_generation = 0
        self._worker = self._hearing = self._speaking = None
        self._warmers = []
        self._state = "resting"
        self._voice = self._fast if self._fast is not None else self._best
        self._gpu_warm = False
        self._voice_reason = "fast start" if self._fast is not None and self._best is not None else "only voice"
        if self._voice is None:
            self._voice_reason = "no voice configured"
        self._choice = None
        self._counters = Counter(ignored=0, heard=0, played=0, playback_timeouts=0, errors=0,
                                 announced=0, announced_quietly=0, announcements_dropped=0)
        # Input and acknowledgement handlers share these under _changed.
        self._handed_audio = {}  # Sentence ID to whether its last chunk was handed out.
        self._flushed = {}  # Sentence ID to its playback deadline.
        self._flush_number = 0
        self._pending_flush = None
        self._echo_until = self._echo_frames = 0
        self._asking = self._answer_after = self._answer_until = None
        self._unsubscribe = turns.subscribe(lambda kind, **fields: self._post("turn", kind, fields))

        # These fields are owned by the worker.
        self._link = False
        self._turn_state = "idle"
        self._exchange = self._key_down = False
        self._capture = None
        self._woken = set()
        self._playback = {}
        self._wake_from = self._wake_until = self._follow_from = self._follow_until = 0
        self._answer_finished = None
        self._may_follow = False
        self._exchange_power = None
        self._approval_fields = None
        self._approval_poll_at = 0
        self._notice_follow = None

    def attach(self, on_event, on_audio):
        """Set the sinks for the next helper before starting capture."""
        with self._changed:
            self._on_event, self._on_audio = on_event, on_audio

    def detach(self):
        """Release the disconnected helper's callbacks."""
        self.attach(None, None)

    def announce(self, notice: Notice):
        """Hand a work notice to the link worker without waiting for playback."""
        with self._changed:
            if not self._active or self._on_event is None:
                self._counters["announcements_dropped"] += 1
                return
            # Overflow uses the worker for link events too, but cannot become speech.
            overflow = self._pending_notices >= MAX_PENDING_NOTICES
            if not overflow:
                self._pending_notices += 1
            self._queue(self._generation, "notice", notice, overflow)

    def start(self):
        """Bring up capture and begin first-use warm-up off the caller's thread."""
        with self._changed:
            if self._closed or self._active:
                return
            self._active = True
            self._generation += 1
            self._queue(None, "start", self._generation)
            if self._worker is not None:
                return
            self._hearing = Hearing(self._transcriber, names=self._decode_names, clock=self._clock,
                                    detector=self._detector, capture_details=True, on_event=self._hearing_event,
                                    prefer_cancelled=lambda mode: mode.answer_until is not None)
            self._speaking = Speaking(_SentenceVoice(self._synthesize), clock=self._clock,
                                      keep_history=False, on_audio=self._handoff,
                                      on_done=lambda id: self._post("delivered", id),
                                      on_error=lambda id: self._post("audio_error", id))
            self._worker = Thread(target=self._run, name="voice-session", daemon=True)
            self._worker.start()
            self._warm("transcriber", self._warm_transcriber)
            self._warm("detector", self._hearing.warm)
            self._warm("brain", self._warm_brain)
            if self._fast is not None and self._best is not None and self._best is not self._fast:
                self._warm("voice", self._warm_best)

    def _warm(self, name, target):
        worker = Thread(target=target, name=f"voice-warm-{name}", daemon=True)
        self._warmers.append(worker)
        worker.start()

    def _warm_transcriber(self):
        choice = self._transcriber.load()
        with self._changed:
            self._queue(None, "transcriber", choice)

    def _warm_brain(self):
        try:
            self._turns.warm()
        except Exception:
            self._post("error", "brain_warm_failed")

    def _warm_best(self):
        try:
            pcm, rate = self._best.synthesize("The voice is ready.")
            if not len(pcm) or rate <= 0:
                raise ValueError("empty warm-up")
        except Exception:
            with self._changed:
                self._voice_reason = "best voice warm-up failed"
        else:
            with self._changed:
                self._gpu_warm = True
                self._voice = self._best
                self._voice_reason = "best voice warmed"

    def _synthesize(self, text):
        """Speaking calls this once per sentence, with a stable voice choice."""
        with self._changed:
            voice = self._voice
        if voice is None:
            raise EngineUnavailable("no voice configured")
        result = voice.synthesize(text)
        if voice is self._best and self._fast is None:
            with self._changed:
                self._gpu_warm = True
        return result

    def feed(self, raw, cancelled=None):
        """Accept one pair of 320-sample mono frames at 16 kHz."""
        with self._changed:
            if not self._active:
                return
            now = self._clock()
            echo = self._echo_at(now)
            context = self._capture_mode(now, echo)
            self._hearing.feed(raw if cancelled is None else cancelled,
                               raw=None if cancelled is None else raw, echo=echo, context=context, at=now)
            if self._echo_frames and not self._handed_audio and not self._flushed:
                self._echo_frames -= 1
            self._changed.notify_all()

    def _echo_at(self, now):
        return self._running_generation == self._generation and (
            bool(self._handed_audio or self._flushed) or now < self._echo_until or self._echo_frames > 0)

    def _capture_mode(self, now, echo):
        asking = self._asking if self._running_generation == self._generation else None
        ready = (asking is not None and not echo and self._answer_after is not None
                 and self._answer_after <= now < self._answer_until)
        return _CaptureMode(self._generation, asking, self._answer_until if ready else None, echo)

    def key(self, down: bool):
        """Capture one utterance from press through release."""
        with self._changed:
            if self._active:
                now = self._clock()
                self._queue(self._generation, "key", down)
                self._hearing.key(down, context=self._capture_mode(now, self._echo_at(now)))

    def played(self, id):
        """Acknowledge that the link finished playing a sentence."""
        with self._changed:
            if (not self._active or self._running_generation != self._generation
                    or not isinstance(id, str) or not self._handed_audio.get(id)):
                return
            at = self._clock()
            del self._handed_audio[id]
            self._update_echo(at)
            if self._asking == id:
                self._answer_after = at + ECHO_TAIL_S
                self._answer_until = at + ANSWER_WINDOW_S
            self._queue(self._generation, "played", id, at)

    def click(self, id, digest, decision):
        self._post("click", id, digest, decision)

    def playback_stopped(self, number):
        """Acknowledge a flush and start the acoustic tail from that point."""
        with self._changed:
            if (not self._active or self._running_generation != self._generation
                    or type(number) is not int or number != self._pending_flush):
                return
            self._pending_flush = None
            if self._flushed:
                self._flushed.clear()
                self._update_echo(self._clock())
                self._changed.notify_all()

    def stop(self):
        """Discard this link's capture and playback while keeping engines warm."""
        with self._changed:
            if self._active:
                self._discard_input()
                self._queue(None, "stop")

    def close(self):
        """End the session and drain its workers."""
        with self._changed:
            first = not self._closed
            if first:
                self._closed = True
                self.detach()
                if self._active:
                    self._discard_input()
            worker = self._worker
            if first and worker is not None:
                self._queue(None, "close")
        if worker is None:
            if first:
                self._unsubscribe()
                self._close_voices()
        elif worker is not current_thread():
            worker.join()

    def _discard_input(self):
        self._active = False
        self._hearing.reset()
        self._counters["announcements_dropped"] += sum(action == "notice" for _, action, _ in self._mailbox)
        self._pending_notices = 0
        self._mailbox = deque(message for message in self._mailbox if message[0] is None)

    def _close_voices(self):
        _close(self._fast)
        if self._best is not self._fast:
            _close(self._best)

    def apply_settings(self, settings):
        with self._changed:
            for voice in (self._fast, self._best):
                if voice is not None:
                    voice.speed = settings.speed
            self._follow_up_s = settings.follow_up_s

    def status(self):
        with self._changed:
            state = self._state
            choice = self._choice or EngineChoice(None, None, "microphone link has not started"
                                                  if self._worker is None else "loading local transcriber")
            status = {
                "state": state, "gpu_warm": self._gpu_warm, "counters": dict(self._counters),
                "transcriber": {"name": choice.name, "reason": choice.reason},
                "voice": {"name": _name(self._voice) if self._voice is not None else None,
                          "reason": self._voice_reason},
            }
            if state == "unavailable":
                status["reason"] = "transcriber_unavailable"
            hearing = self._hearing
        if self._approving is not None:
            status["counters"].update(self._approving.status())
        status["detector"] = hearing.detector_status() if hearing is not None else "microphone link has not started"
        return status

    def _decode_names(self, capture):
        asking = capture.context.asking is not None
        # Put short recognition hints ahead of names that can fill the prompt.
        # Measured on the owner's recording: with the wake name as the only (or last) name in the
        # transcriber's prompt, a leading "Jarvis," was dropped from the transcript. So the name goes
        # first and is always followed by a few fixed names.
        names = [name for name in self._names() if name.casefold() != WAKE_NAME.casefold()]
        extra = [name for name in PROMPT_NAMES if name.casefold() not in {n.casefold() for n in names}]
        return (WAKE_NAME, *(ANSWER_WORDS if asking else ()), *names, *extra)

    def _hearing_event(self, kind, *, capture, **fields):
        generation = capture.context.generation
        with self._changed:
            if self._active and self._generation == generation:
                self._queue(generation, "hearing", kind, capture, fields)

    def _handoff(self, id, pcm, rate, first, last):
        # Keep Speaking's backpressure without invoking the link under its lock.
        done = Event()
        with self._changed:
            if not self._active:
                return
            generation = self._generation
            self._queue(generation, "audio", id, pcm, rate, first, last, done)
            self._changed.wait_for(lambda: done.is_set() or not self._active or generation != self._generation)

    def _post(self, action, *args):
        with self._changed:
            if self._active:
                self._queue(self._generation, action, *args)

    def _queue(self, generation, action, *args):
        self._mailbox.append((generation, action, args))
        self._changed.notify_all()

    def _run(self):
        try:
            while True:
                now = self._clock()
                self._expire(now)
                with self._changed:
                    if not self._mailbox:
                        self._changed.wait(self._next_timeout(now))
                        continue
                    generation, action, args = self._mailbox.popleft()
                    if action == "notice" and not args[1]:
                        self._pending_notices -= 1
                    current = generation is None or (self._active and generation == self._generation)
                try:
                    if current:
                        if action == "close":
                            self._disconnect()
                            return
                        self._handle(action, *args)
                finally:
                    if action == "audio":
                        with self._changed:
                            args[-1].set()
                            self._changed.notify_all()
                    del args
        finally:
            with self._changed:
                self._active = False
                self._closed = True
                self.detach()
                self._changed.notify_all()
            self._unsubscribe()
            self._hearing.close()
            self._speaking.close()
            self._hearing.join()
            self._speaking.join()
            for warm in self._warmers:
                warm.join()
            self._close_voices()
            with self._changed:
                self._mailbox.clear()

    def _handle(self, action, *args):
        if action == "start":
            self._link = True
            with self._changed:
                self._running_generation = args[0]
            self._publish_state(force=True)
            if self._state == "unavailable":
                self._error("transcriber_unavailable")
        elif action == "stop":
            self._disconnect()
        elif action == "key":
            self._key_down = args[0]
            if self._key_down:
                if self._playback or (self._asking is None and self._turns.can_interrupt()):
                    self._barge()
                else:
                    self._end_conversation()
            self._publish_state()
        elif action == "hearing":
            self._handle_hearing(*args)
        elif action == "notice":
            self._announce(*args)
        elif action == "turn":
            self._handle_turn(*args)
        elif action == "audio":
            self._send_audio(*args[:-1])
        elif action == "delivered":
            self._delivered(*args)
        elif action == "played":
            self._complete(*args)
        elif action == "click" and self._approving is not None:
            if not self._turns.is_asking(self._asking):
                self._end_approval()
            if self._approving.click(*args):
                self._end_approval()
        elif action == "audio_error":
            self._complete(args[0], self._clock(), failed=True)
        elif action == "transcriber":
            with self._changed:
                self._choice = args[0]
            self._publish_state()
            if self._state == "unavailable":
                self._error("transcriber_unavailable")
        elif action == "error":
            self._error(*args)

    def _disconnect(self):
        if not self._link:
            return
        self._link = False
        self._flush()
        if self._exchange:
            self._turns.interrupt()
        self._exchange = self._key_down = False
        self._capture = None
        self._woken.clear()
        self._end_conversation()
        self._notice_follow = None
        self._exchange_power = None
        self._turn_state = "idle"
        self._clear_approval()
        with self._changed:
            self._echo_until = self._echo_frames = 0
            self._handed_audio.clear()
            self._flushed.clear()
            self._pending_flush = None
        self._publish_state()

    def _next_timeout(self, now):
        deadlines = [value for value in (self._wake_until, self._follow_until) if value > now]
        deadlines.extend(item.deadline for item in self._playback.values() if item.delivered)
        deadlines.extend(self._flushed.values())
        if self._approval_fields is not None:
            deadlines.append(self._approval_poll_at)
        return max(0, min(deadlines) - now) if deadlines else None

    def _expire(self, now):
        if self._approval_fields is not None and now >= self._approval_poll_at:
            self._approval_poll_at = now + APPROVAL_POLL_S
            if not self._turns.is_asking(self._asking) or self._approving.pending() is None:
                self._end_approval()
        for id, item in tuple(self._playback.items()):
            if item.delivered and now >= item.deadline:
                self._complete(id, now, timed_out=True)
        with self._changed:
            expired = [id for id, deadline in self._flushed.items() if now >= deadline]
            for id in expired:
                del self._flushed[id]
            if expired:
                self._update_echo(now, timed_out=True)
        # Keep the bounds for speech that started before expiry and is still
        # decoding. State depends on the deadline itself.
        if (self._wake_until or self._follow_until) and self._link:
            self._publish_state()

    def _publish_state(self, *, force=False):
        now = self._clock()
        with self._changed:
            reason = None
            if not self._link:
                state = "resting"
            elif self._choice is not None and self._choice.engine is None:
                state, reason = "unavailable", "transcriber_unavailable"
            elif self._approval_fields is not None:
                state = "asking"
            elif self._key_down or (self._capture is not None and (
                    self._capture in self._woken or self._admitted(self._capture))):
                state = "listening"
            elif self._asking is not None:
                state = "asking"
            elif self._playback or self._turn_state == "speaking":
                state = "speaking"
            elif now < self._wake_until:
                state = "listening"
            elif self._turn_state == "thinking":
                state = "working"
            elif self._follow_from < self._follow_until and now < self._follow_until:
                state = "listening"
            else:
                state = "resting"
            if not force and self._state == state:
                return
            self._state = state
        self._event("state", state=state, **({"reason": reason} if reason else {}))

    def _admitted(self, capture):
        if capture.keyed:
            return True
        start = capture.started_at
        return (self._wake_from <= start < self._wake_until
                or (self._follow_from <= start < self._follow_until and not capture.context.echo
                    and self._exchange_power is not None
                    and capture.power >= self._exchange_power * self._follow_ratio))

    def _handle_hearing(self, kind, capture, fields):
        if kind == "speech_start":
            self._capture = capture
            self._publish_state()
        elif kind == "speech_end":
            self._woken.discard(capture)
            if self._capture is capture:
                self._capture = None
            self._publish_state()
        elif kind == "partial":
            if self._asking is not None or capture.context.asking is not None:
                return  # The answer hook gets the final before anything is displayed.
            text = fields["text"]
            request = _wake_request(text)
            if request is not None:
                self._woken.add(capture)
                self._publish_state()
                if request:
                    self._event("partial", text=request)
            elif self._ordinary_admission(capture, fields):
                self._event("partial", text=text)
        elif kind == "final":
            self._final(capture, fields)

    def _final(self, capture, fields):
        if fields["failed"]:
            self._withdraw_wake(capture)
            if self._state != "unavailable":
                self._error("decode_failed")
            return
        text = fields["text"].strip()
        if not text:
            if self._withdraw_wake(capture):
                self._ignored()
            return
        request = _wake_request(text)
        stop = is_stop_word(text if request is None else request)
        active = bool(self._playback) or self._turns.can_interrupt()
        captured_answer = (self._approving is not None and capture.context.asking is not None
                           and answer_decision(text) is not None)
        started_at, mode = fields["speech_onset"] or (None, capture.context)
        if ((self._approving is not None or not stop) and started_at is not None
                and mode.answer_until is not None and self._clock() <= mode.answer_until + ANSWER_DECODE_GRACE_S
                and mode.asking == capture.context.asking == self._asking and not fields["uncancelled_echo"]
                and self._turns.is_asking(self._asking)):
            try:
                answer_timing = {"started_at": started_at} if self._approving is not None else {}
                if self.on_answer(text, **answer_timing):
                    if self._approving is not None:
                        if self._approving.take_needs_click():
                            self._queue_sentence(uuid4().hex, "That one needs a click.",
                                                 approval_id=self._asking)
                        elif self._approving.pending() is None:
                            self._end_approval()
                    return
            except Exception:
                self._error("answer_hook_failed")
                return
        elif captured_answer:
            self._approving.ignore()
        if captured_answer and (not stop or mode.answer_until is not None):
            return  # A late or repeated answer belongs to this capture's card.
        stop_interrupt = stop and (active or capture.context.echo or fields["uncancelled_echo"])
        if request is None and not stop_interrupt and not self._ordinary_admission(capture, fields):
            self._withdraw_wake(capture)
            self._ignored()
            return
        if active and (request is not None or stop):
            self._barge()
        from_wake = self._wake_from <= capture.started_at < self._wake_until
        starts_exchange = request is not None or fields["keyed"] or from_wake
        if request is not None:
            self._exchange_power = capture.power or None
            if not _WORD.search(request):
                self._follow_from = self._follow_until = 0
                # The window runs from the end of the name, not from when its decode finished: the
                # owner says "Jarvis", pauses, and goes on before the decode is back. A decode that
                # lagged too long opens nothing, so old room speech is never admitted afterwards.
                ended = fields["speech_ended_at"]
                if self._clock() - ended <= WAKE_DECODE_LAG_S:
                    self._wake_from, self._wake_until = ended, ended + WAKE_FOLLOW_S
                else:
                    self._wake_from = self._wake_until = 0
                self._publish_state()
                return
            text = request
        elif fields["keyed"]:
            self._exchange_power = fields.get("power", capture.power) or None
        notice_prefix = self._notice_prefix(capture.started_at) if starts_exchange and not stop else None
        self._notice_follow = None
        if stop:
            self._end_conversation()
        else:
            self._wake_from = self._wake_until = 0
            self._may_follow = starts_exchange
            if self._may_follow:
                self._follow_until = 0
            # Suspend admission while this turn runs. A continuation can
            # reopen the window with the same deadline once its reply ends.
            self._follow_from = self._follow_until
        self._count("heard")
        self._event("heard", text=text)
        with self._changed:
            if not self._active or capture.context.generation != self._generation:
                return
        if not stop:
            self._exchange = True
            self._answer_finished = None
            self._turn_state = "thinking"
            self._clear_approval()
        try:
            self._turns.heard(text, speech_ended_at=fields["speech_ended_at"],
                              silence_ms=fields["silence_ms"], decode_ms=fields["decode_ms"],
                              prefix=notice_prefix)
        except Exception:
            self._turn_state = "idle"
            self._error("turn_failed")
        self._publish_state()

    def _ordinary_admission(self, capture, fields):
        return fields["keyed"] or (not fields["uncancelled_echo"] and self._admitted(capture)
                                    and not self._playback and not self._turns.can_interrupt())

    def _withdraw_wake(self, capture):
        if capture in self._woken:
            self._woken.remove(capture)
            self._event("partial", text="")
            self._publish_state()
            return True
        return False

    def _handle_turn(self, kind, fields):
        if not self._exchange:
            return
        if kind == "state":
            self._turn_state = fields["state"]
            if self._turn_state == "idle":
                self._clear_approval()
                self._open_follow_up()
            self._publish_state()
        elif kind == "say":
            if self._turns.has_audio(fields["id"]):
                self._queue_sentence(fields["id"], fields["text"])
        elif kind == "ack":
            self._event("earcon", name="ack")
        elif kind == "speech_stop":
            self._flush()
        elif kind == "asking":
            if not self._turns.is_asking(fields["id"]):
                return
            for id in self._flush():
                self._turns.spoken(id)
            self._clear_approval()
            if self._approving is not None:
                fields["voice_allowed"] = self._approving.ask(fields)
                pending = self._approving.pending()
                if pending is None:
                    self._approving.clear()
                    self._publish_state()
                    return
                if not fields["voice_allowed"]:
                    fields["sentence"] = fields["sentence"].removesuffix("Approve?") + "This one needs a click."
                fields["click_expires_in"] = pending["expires_in"]
                fields["expires_in"] = ANSWER_WINDOW_S
                fields["voice_waiting"] = True
                self._approval_fields = fields
                self._approval_poll_at = self._clock() + APPROVAL_POLL_S
            with self._changed:
                self._asking = fields["id"]
            self._publish_state()
            self._event("asking", **fields)
            self._queue_sentence(fields["id"], fields["sentence"], approval_id=fields["id"])
        elif kind == "approval_end":
            self._end_approval()
        elif kind == "error":
            self._turn_state = "idle"
            self._error("turn_failed")
            self._publish_state()
        elif kind == "ignored":
            self._turn_state = "idle"
            self._publish_state()

    def _clear_approval(self):
        if self._approving is not None:
            self._approving.clear()
        with self._changed:
            self._approval_fields = None
            self._asking = self._answer_after = self._answer_until = None

    def _end_approval(self):
        self._clear_approval()
        if any(item.approval_id is not None for item in self._playback.values()):
            self._flush()
        self._publish_state()

    def _announce(self, notice, overflow):
        with self._changed:
            attached = self._active and self._on_event is not None
            flushing = self._pending_flush is not None
        if not attached or overflow:
            self._count("announcements_dropped")
        if not attached:
            return
        title = " ".join("".join(char for char in untrusted.strip_invisible(notice.title)
                                 if char.isspace() or category(char) not in {"Cf", "Cs"}).split())
        title = title.translate(_NOTICE_QUOTES)[:60].rstrip()
        notice = replace(notice, title=title or "a background task")
        label, phrase = _NOTICE_WORDS["failed" if notice.failed else notice.category]
        quiet = overflow or flushing or self._state != "resting" or self._turns.can_interrupt()
        if not quiet:
            try:
                quiet = self._quiet()
            except OSError:
                quiet = True
                LOG.warning("Quiet hours could not be checked for a work announcement.")
        self._event("notice", title=label, text=notice.title)
        if quiet:
            self._count("announced_quietly")
            self._event("earcon", name="notice")
        else:
            self._count("announced")
            self._notice_follow = None
            self._queue_sentence(uuid4().hex, f'Your task "{notice.title}" {phrase}.', notice=notice)

    def _notice_prefix(self, started_at):
        if self._notice_follow is None:
            return None
        notice, at = self._notice_follow
        if not at <= started_at < at + NOTICE_CONTEXT_S:
            return None
        phrase = ("is waiting on him" if notice.category == "waiting" else
                  "failed" if notice.failed else notice.category)
        return (f'[Jarvis just told the owner: task "{notice.title}" {phrase}. '
                f'Thread id: {notice.thread_id}.]')

    def _queue_sentence(self, id, text, *, approval_id=None, notice=None):
        if id in self._playback:
            return
        self._playback[id] = _Playback(approval_id, notice=notice)
        self._publish_state()
        self._event("say", id=id, text=text)
        self._speaking.say(id, text)

    def _send_audio(self, id, pcm, rate, first, last):
        item = self._playback.get(id)
        if item is None:
            return
        if item.notice is None and not (
                self._turns.is_asking(item.approval_id) if item.approval_id is not None
                else self._turns.has_audio(id)):
            return
        now = self._clock()
        previous = max((row.ends_at or now for row in self._playback.values()), default=now)
        seconds = len(pcm) / rate
        item.seconds_sent += seconds
        item.ends_at = max(now, previous) + seconds
        item.deadline = max(item.ends_at, now + item.seconds_sent) + PLAYED_GRACE_S
        with self._changed:
            if not self._active or self._running_generation != self._generation:
                return
            self._handed_audio[id] = False
            on_audio = self._on_audio
        if first:
            self._turns.first_audio(id, now)
        try:
            if on_audio is not None:
                if last:
                    with self._changed:
                        self._handed_audio[id] = True
                on_audio(id, pcm, rate, first, last)
        except Exception:
            self._complete(id, now, failed=True)

    def _delivered(self, id):
        item = self._playback.get(id)
        if item is not None and item.deadline is not None:
            item.delivered = True

    def _complete(self, id, at, *, timed_out=False, failed=False):
        with self._changed:
            if timed_out and id not in self._handed_audio:
                return  # Receipt already ended playback. Its worker message is still queued.
            item = self._playback.pop(id, None)
            if item is None:
                return
            if id in self._handed_audio:
                del self._handed_audio[id]
                if failed:
                    self._flushed[id] = item.deadline
                self._update_echo(at, timed_out=timed_out)
        if failed:
            self._error("speech_failed")
        else:
            self._count("playback_timeouts" if timed_out else "played")
        if item.approval_id == self._asking == id and self._approving is not None:
            confirmed = not (failed or timed_out)
            if confirmed:
                self._approving.prompt_finished(id, at)
            pending = self._approving.pending()
            if pending is not None:
                remaining = max(0, at + ANSWER_WINDOW_S - self._clock()) if confirmed else 0
                self._event("asking", **dict(self._approval_fields, voice_waiting=False,
                            voice_allowed=self._approval_fields["voice_allowed"] and remaining > 0,
                            expires_in=remaining,
                            click_expires_in=pending["expires_in"]))
        if item.notice is not None:
            if not (failed or timed_out):
                self._notice_follow = item.notice, at
        elif item.approval_id is None:
            if not self._playback:
                self._answer_finished = None if failed else (id, at)
            self._turns.spoken(id)
            self._open_follow_up()
        self._publish_state()

    def _open_follow_up(self):
        if self._answer_finished is not None:
            id, at = self._answer_finished
            if self._turns.answered(id):
                if self._may_follow:
                    with self._changed:
                        self._follow_until = at + self._follow_up_s
                self._follow_from = at + ECHO_TAIL_S
                self._may_follow = False
                self._answer_finished = None

    def _update_echo(self, at, *, timed_out=False):
        # Caller holds _changed so capture can see the tail as soon as the link confirms.
        if not self._handed_audio and not self._flushed:
            self._echo_until = max(self._echo_until, at + ECHO_TAIL_S)
        if timed_out:
            # A silent link has not delivered any of the acoustic tail.
            # Keep that much input cancelled when microphone frames resume.
            self._echo_frames = round(ECHO_TAIL_S * 1000 / FRAME_MS)

    def _flush(self, *, force=False):
        ids = tuple(self._playback)
        if ids or force:
            # Audio already handed to the link may still play until it confirms the flush.
            with self._changed:
                self._flushed.update((id, self._playback[id].deadline) for id in self._handed_audio)
                self._handed_audio.clear()
                self._flush_number += 1
                self._pending_flush = self._flush_number
            self._playback.clear()
            self._speaking.stop(wait=False)
            self._answer_finished = None
            self._publish_state()
            self._event("flush", n=self._flush_number)
        return ids

    def _end_conversation(self):
        self._wake_from = self._wake_until = self._follow_from = self._follow_until = 0
        self._answer_finished = None
        self._may_follow = False

    def _barge(self):
        self._end_conversation()
        self._clear_approval()
        self._flush(force=True)
        self._turns.interrupt()

    def _ignored(self):
        self._event("ignored", count=self._count("ignored"))

    def _count(self, name):
        with self._changed:
            self._counters[name] += 1
            return self._counters[name]

    def _error(self, reason):
        self._count("errors")
        self._event("error", reason=reason)

    def _event(self, kind, **fields):
        with self._changed:
            if not self._active or self._running_generation != self._generation:
                return
            on_event = self._on_event
        try:
            if on_event is not None:
                on_event(kind, **fields)
        except Exception:
            LOG.warning("A voice session event callback failed.")
