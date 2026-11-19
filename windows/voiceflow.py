"""Sequential voice workflow; dependencies are injected for pure checks."""
from __future__ import annotations

import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass

if __package__:
    from .voiceactions import LaunchStatus, action_scope
    from .voicetype import JARVIS_ADDRESS, POLITE_PREFIX, mentions_command, plan, sanitize, shortcut_open_target
    from .voicewire import LinkError, VoiceQuestionError
else:
    from voiceactions import LaunchStatus, action_scope
    from voicetype import JARVIS_ADDRESS, POLITE_PREFIX, mentions_command, plan, sanitize, shortcut_open_target
    from voicewire import LinkError, VoiceQuestionError


PROFILE_CHOICE_SECONDS = 15.0
# How long a retired or answered prompt still recognizes its labels. Long
# enough to catch an immediately repeated reply, short enough that dead
# labels never shadow ordinary dictation for long.
PROFILE_REPLY_GRACE_SECONDS = 10.0
PROFILE_QUESTION = "Which Chrome profile would you like?"
PROFILE_MANUAL = "Chrome needs a profile. Choose one in Chrome."
PROFILE_UNVERIFIED = "Chrome profile selection could not be verified. Check Chrome."
PROFILE_CANCELED = "Chrome profile selection canceled."


def _profile_name(text):
    return " ".join(unicodedata.normalize("NFKC", sanitize(text)).casefold().split()).rstrip(".!?").rstrip()


@dataclass(frozen=True)
class _ProfileChoice:
    session: str | None
    labels: tuple[str, ...]
    receipt: str | None
    expires_at: float


@dataclass(frozen=True)
class _ProfileReplyContext:
    """Labels of a retired or already answered prompt: recognition only.

    It holds no receipt and no session, so nothing reached through it can
    invoke Chrome or confirm an action. ``repeat`` is the overlay state a
    recognized reply re-shows; None means the prompt was never answered.
    ``until`` is fixed when the context is made: carrying it to a new client,
    idle checks, unrelated speech and recognized repeats never move it.
    """
    labels: tuple[str, ...]
    until: float
    repeat: tuple[str, str] | None = None


# Courtesy words around a reply. Only "please"/"kindly" may lead a profile
# selection; the selection frame mirrors the spoken question ("Which Chrome
# profile would you like?") and nothing broader.
_APOSTROPHE = "['\u2018\u2019\u02bc]"
_LEAD_COURTESY = re.compile(r"^(?:(?:please|kindly)[, ]+)+")
_TRAIL_COURTESY = re.compile(r"(?:[, ]+(?:please|thanks|thank you))+$")
_LIKE_FRAME = re.compile(rf"^(?:i{_APOSTROPHE}d|i would) like[, ]+(?:to use[, ]+)?")
_CANCEL_REPLIES = frozenset({
    "cancel", "cancel it", "cancel that", "cancel profile selection", "cancel chrome profile selection",
    "cancel the profile selection", "cancel the chrome profile selection",
})
_NEGATIVE_REPLIES = frozenset({
    "no", "nope", "no thanks", "no thank you", "neither", "none", "neither of them", "none of them",
    "never mind", "nevermind",
})


def _profile_reply_forms(reading):
    """Readings of one normalized reply with only courtesy framing removed.

    The remainder is compared whole against the offered names, so profile
    punctuation is never collapsed and a substring never becomes a choice.
    """
    forms = {reading}
    core = _TRAIL_COURTESY.sub("", _LEAD_COURTESY.sub("", reading)).strip(" ,")
    if core:
        forms.add(core)
        framed = _LIKE_FRAME.sub("", core, count=1)
        if framed and framed != core:
            forms.add(framed)
            if framed.startswith("the "):
                # Both readings stay, so "Work" and "Work profile" are ambiguous.
                inner = framed[4:]
                forms.add(inner)
                if inner.endswith(" profile") and inner[:-8].strip():
                    forms.add(inner[:-8].strip())
    return forms


def _is_cancel_reply(reading):
    core = POLITE_PREFIX.sub("", reading)
    core = _TRAIL_COURTESY.sub("", core).strip(" ,")
    return reading in _CANCEL_REPLIES or core in _CANCEL_REPLIES


def _is_negative_reply(reading):
    core = _TRAIL_COURTESY.sub("", _LEAD_COURTESY.sub("", reading)).strip(" ,")
    return reading in _NEGATIVE_REPLIES or core in _NEGATIVE_REPLIES


def _looks_like_profile_reply(words, names):
    # Recognize common inexact replies so they cannot fall through to typing.
    # Only the exact-name path may select a profile.
    def candidate(part):
        part = POLITE_PREFIX.sub("", _profile_name(part))
        part = re.sub(rf"^(?:(?:choose|pick|select|use|maybe|the|not|no|i mean|i meant|make that"
                      rf"|i{_APOSTROPHE}d like|i would like|i want)[, ]+"
                      r"|(?:chrome )?profile[: ]+)+", "", part)
        return re.sub(r"(?:[ ,]+(?:please|thanks|thank you|(?:chrome )?profile|one|i think|instead))+$", "", part)

    words = candidate(words)
    parts = re.split(r"(?:[,;.!?]*\s+(?:or|and not|and|but not|but|not|instead of|rather than|no)\b[, ]*"
                     r"|[,;]\s*)", words)
    return all(part and any(part == name or name.startswith(part + " ") or name.endswith(" " + part)
                            for name in names)
               for part in map(candidate, parts))


def _run_native(current, action, *args, **kwargs):
    # A worker may retain the scope's callable after the native call returns.
    running = True

    def authorized():
        return running and current()

    with action_scope(authorized):
        try:
            return action(*args, **kwargs)
        finally:
            running = False


class SpokenAnswer:
    """Play WAVs in order while the link continues delivering text and sound."""
    def __init__(self, player):
        self.player = player
        self.pending = queue.Queue(maxsize=2)
        self.error = None
        self.worker = threading.Thread(target=self._play, name="voice-playback", daemon=True)

    def _play(self):
        try:
            while True:
                wav = self.pending.get()
                if wav is None:
                    return
                if not self.player.cancelled.is_set():
                    self.player.play(wav)
        except Exception:
            self.error = RuntimeError("Speech playback unavailable")

    def offer(self, wav):
        while self.worker.is_alive():
            if wav is not None and self.player.cancelled.is_set():
                return
            try:
                self.pending.put(wav, timeout=0.05)
                return
            except queue.Full:
                pass

    def ask(self, client, words, delta, *, defer_speech=False):
        self.worker.start()
        try:
            answer = client.ask_spoken(words, delta, self.offer, self.player.cancelled,
                                       defer_speech=defer_speech)
        except BaseException:
            self.player.stop()
            raise
        finally:
            self.offer(None)
            self.worker.join()
        if self.error:
            raise self.error
        return answer


class Flow:
    def __init__(self, client, typer, actions, ui, player, *, can_act=lambda: True, revoked_at=lambda: None):
        self.client, self.typer, self.actions = client, typer, actions
        self.ui, self.player = ui, player
        self.can_act = can_act
        # When this clip's authority was first taken by a newer generation
        # (monotonic seconds), or None when the caller cannot say.
        self.revoked_at = revoked_at
        self.link_error = None
        self.typer.on_scratch = self._scratch

    def _scratch(self, which):
        try:
            self.client.scratch(which)
        except LinkError as exc:
            self.link_error = exc  # reconnect before the next clip, not into a dead link
        except Exception:
            pass

    def _category(self):
        try:
            app = self.typer.category()
        except Exception:
            app = "terminal"
        return "other" if app == "other" else "terminal"

    def _type(self, text):
        steps = []
        for step in plan(text):
            # The daemon refuses text over its cap; type such a step raw rather
            # than drop the link over it. Text holding a command phrase is never
            # cleaned, so command words are never altered.
            if step[0] == "text" and len(step[1]) <= 2000 and not mentions_command(step[1]):
                app = self._category()
                try:
                    got = self.client.clean(step[1], app)
                    # The focus may have moved during the request: a cleanup made
                    # for another kind of window is not typed (raw instead).
                    if (isinstance(got, dict) and isinstance(got.get("text"), str)
                            and got.get("pass") in ("off", "rules", "raw")
                            and self._category() == app):
                        step = ("text", got["text"], got["pass"])
                except LinkError as exc:
                    self.link_error = exc
                except Exception:
                    pass  # cleanup is optional; preserve every raw typing step
            steps.append(step)
        self.typer.run(steps)

    def _confirm_action(self, receipt, current):
        if receipt and current():
            try:
                self.client.confirm_action(receipt)
            except LinkError as exc:
                # The native action succeeded. Reconnect before the next clip
                # without replaying this action or reporting it as a failure.
                self.link_error = exc

    @staticmethod
    def retire_profile_choice(client):
        """Drop a prompt's authority; keep its labels briefly for recognition.

        The grace runs from the earlier of now and the prompt's scheduled
        expiry: an unanswered prompt noticed late (after a long transcription
        or between idle checks) still ends ten seconds after it expired, and
        an early retirement (cancel, session loss) ends ten seconds after it.
        An existing context is returned unchanged, never re-anchored.

        Returns the receipt-free context (or ``()``) so a reconnecting worker
        can carry reply recognition, never authority, to the next client.
        """
        pending = getattr(client, "_pending_chrome_profile", None)
        if isinstance(pending, _ProfileChoice):
            retired_at = min(time.monotonic(), pending.expires_at)
            pending = _ProfileReplyContext(pending.labels, retired_at + PROFILE_REPLY_GRACE_SECONDS)
            client._pending_chrome_profile = pending
        return pending if isinstance(pending, _ProfileReplyContext) else ()

    @classmethod
    def expire_profile_choice(cls, client):
        pending = getattr(client, "_pending_chrome_profile", None)
        if (isinstance(pending, _ProfileChoice)
                and (pending.session != getattr(client, "session", None) or time.monotonic() >= pending.expires_at)):
            pending = cls.retire_profile_choice(client)
        if isinstance(pending, _ProfileReplyContext):
            # Checked in the same pass as retirement, so the first clip after
            # a stall past the whole grace routes normally at once.
            if time.monotonic() >= pending.until:
                client._pending_chrome_profile = None
        elif pending is not None and not isinstance(pending, _ProfileChoice):
            # Anything else (an old bare label tuple) carries no deadline.
            client._pending_chrome_profile = None

    def _can_act(self):
        cancelled = getattr(self.player, "cancelled", None)
        return self.can_act() and (cancelled is None or cancelled.is_set() is not True)

    def _show_profile_choice(self, pending, source):
        instruction = ("Say Jarvis, then a profile name or cancel." if source == "wake"
                       else "Hold Right Ctrl and say a profile name or cancel.")
        self.ui.set_state("profile_choice", f"{PROFILE_QUESTION}\n{', '.join(pending.labels)}\n{instruction}",
                          pending.expires_at)

    def _offer_profile_choice(self, receipt, session, source, current):
        options = getattr(self.actions, "chrome_profile_options", None)
        choose = getattr(self.actions, "choose_chrome_profile", None)
        labels = options() if callable(options) and callable(choose) else ()
        if not current():
            return
        if (not isinstance(labels, tuple) or not labels
                or not all(isinstance(label, str) and _profile_name(label) for label in labels)):
            self.ui.set_state("error", PROFILE_MANUAL)
            return
        pending = _ProfileChoice(session, labels, receipt, time.monotonic() + PROFILE_CHOICE_SECONDS)
        self.client._pending_chrome_profile = pending
        self._show_profile_choice(pending, source)
        try:
            wav = self.client.say(PROFILE_QUESTION)
            if current() and time.monotonic() < pending.expires_at:
                self.player.play(wav)
        except (OSError, RuntimeError):
            self.retire_profile_choice(self.client)
            raise

    def _answered(self, labels, state, text, until=None):
        # The reply settled the prompt. A repeat of it inside the grace window
        # re-shows this outcome and never reaches Chrome, the typer or a receipt.
        # A settled active prompt gets ten seconds from now; a late reply to a
        # retired one keeps that context's deadline.
        if until is None:
            until = time.monotonic() + PROFILE_REPLY_GRACE_SECONDS
        self.client._pending_chrome_profile = _ProfileReplyContext(labels, until, (state, text))
        self.ui.set_state(state, text)

    def _handle_profile_reply(self, text, current):
        self.expire_profile_choice(self.client)
        pending = getattr(self.client, "_pending_chrome_profile", None)
        if not isinstance(pending, (_ProfileChoice, _ProfileReplyContext)):
            return False
        # A superseded clip belongs to neither the active prompt nor the
        # newly focused window. Discard it without consuming the reply slot.
        if not current():
            return True
        readings = {_profile_name(text), _profile_name(JARVIS_ADDRESS.sub("", text, count=1))}
        readings.discard("")
        active = isinstance(pending, _ProfileChoice)
        answered = isinstance(pending, _ProfileReplyContext) and pending.repeat is not None
        labels = pending.labels
        names = [_profile_name(label) for label in labels]
        forms = set().union(*(_profile_reply_forms(words) for words in readings)) if readings else set()
        cancel = any(_is_cancel_reply(words) for words in readings)
        matches = [label for label, name in zip(labels, names) if name in forms]
        # After an answer only a repeat-class reply is held back; a fresh
        # "no" is not a repeat and goes on to ordinary routing.
        negative = not answered and any(_is_negative_reply(words) for words in readings)
        reply = (cancel or negative or bool(matches)
                 or any(_looks_like_profile_reply(words, names) for words in readings))
        if not current():
            return True
        if not reply:
            # Unrelated or explicit dictation continues to normal routing,
            # which can replace the old server receipt. An active prompt loses
            # its authority here; its labels, like an existing context's, stay
            # recognition-only until the fixed deadline so a repeated reply
            # after this speech still never clicks, types or confirms.
            if active:
                self.retire_profile_choice(self.client)
            return False
        if not active:
            if answered:
                # Keep the original deadline: repeats never extend the window.
                self.ui.set_state(*pending.repeat)
            else:
                # A late reply to a retired prompt: show the outcome and keep
                # recognizing repeats until the same deadline.
                self._answered(labels, *(("done", PROFILE_CANCELED) if cancel else ("error", PROFILE_MANUAL)),
                               until=pending.until)
            return True
        if cancel:
            self._answered(labels, "done", PROFILE_CANCELED)
            return True
        if len(matches) != 1:
            self._answered(labels, "error", PROFILE_MANUAL)
            return True

        def authorized():
            return (current() and pending.session == getattr(self.client, "session", None)
                    and time.monotonic() < pending.expires_at)

        # Consume before the native call, so an adapter failure or a reply
        # repeated while it runs cannot press or confirm a second time.
        consumed = _ProfileReplyContext(
            labels, time.monotonic() + PROFILE_REPLY_GRACE_SECONDS, ("error", PROFILE_UNVERIFIED))
        self.client._pending_chrome_profile = consumed
        try:
            if authorized():
                result = _run_native(authorized, self.actions.choose_chrome_profile, matches[0])
                if result is LaunchStatus.LAUNCHED and authorized():
                    self._confirm_action(pending.receipt, authorized)
                    self.client._pending_chrome_profile = _ProfileReplyContext(
                        labels, time.monotonic() + PROFILE_REPLY_GRACE_SECONDS,
                        ("done", "Chrome profile already chosen."))
                    self.ui.set_state("done", "Opened Chrome.")
                    return True
            if current():
                message = PROFILE_MANUAL if time.monotonic() >= pending.expires_at else PROFILE_UNVERIFIED
                self._answered(labels, "error", message)
        finally:
            # Superseded while the selection ran (a newer capture, stop or
            # cancel): this clip may not touch the overlay, but its labels
            # stay recognition-only for ten seconds from the interruption,
            # as retire_profile_choice anchors them. The interruption is the
            # generation transition the caller stamped, not this return: a
            # native call that blocks past it must not extend the window.
            # Callers that cannot stamp (can_act alone) settle on this
            # return, the latest the interruption can have been. Either way
            # an interruption past the scheduled expiry anchors there. Only
            # the context this clip installed is settled, and its deadline
            # never shrinks, so a newer prompt or an already-settled outcome
            # is left alone.
            if self.client._pending_chrome_profile is consumed and not current():
                revoked_at = self.revoked_at()
                interrupted_at = min(time.monotonic() if revoked_at is None else revoked_at,
                                     pending.expires_at)
                self.client._pending_chrome_profile = _ProfileReplyContext(
                    labels, max(consumed.until, interrupted_at + PROFILE_REPLY_GRACE_SECONDS), consumed.repeat)
        return True

    def _open_app(self, target, intent, session, source, current, *, exact=False):
        if not current():
            return True
        kwargs = {"exact": True} if exact else {}
        result = _run_native(current, self.actions.open_app, target, **kwargs)
        if not current():
            return True
        if not result:
            return False
        if result == LaunchStatus.CHROME_PROFILE_REQUIRED:
            self._offer_profile_choice(intent.get("receipt"), session, source, current)
        elif result == LaunchStatus.CHROME_WINDOW_UNCONFIRMED:
            self.ui.set_state("error", "Chrome launch requested. Check the Chrome window.")
        else:
            self._confirm_action(intent.get("receipt"), current)
            self.ui.set_state("done", f"Opened {target}.")
        return True

    def on_clip(self, wav, source, *, transcript=None):
        self.link_error = None
        session = getattr(self.client, "session", None)

        def current():
            return self._can_act() and session == getattr(self.client, "session", None)

        try:
            if source not in {"hotkey", "wake"}:
                raise ValueError("Unknown voice source")
            self.ui.set_state("transcribing")
            text = transcript if transcript is not None else self.client.dictate(wav, source=source)["text"]
            if session != getattr(self.client, "session", None):
                return
            clean = sanitize(text)
            if not clean or (source == "wake" and clean.rstrip(".,!?").casefold() == "jarvis"):
                self.expire_profile_choice(self.client)
                pending = getattr(self.client, "_pending_chrome_profile", None)
                if isinstance(pending, _ProfileChoice) and current():
                    self._show_profile_choice(pending, source)
                else:
                    self.ui.set_state("done", "No speech heard")
                return
            if self._handle_profile_reply(clean, current):
                return
            if source == "hotkey" and len(text) > 2000:
                # Route replies are capped. A long paragraph stays complete
                # and literal; a quoted suffix must never become an Enter key.
                self.typer.run([("text", text)])
                self.ui.set_state("done")
                return
            addressed = source == "wake" or bool(JARVIS_ADDRESS.match(clean))
            self.ui.set_state("thinking")
            try:
                if getattr(self.client, "action_receipts", False) is True:
                    intent = self.client.route(text, source, with_receipt=True)
                else:
                    intent = self.client.route(text, source)
            except Exception as exc:
                # Routing is optional: preserve the utterance on failure.
                # Retain transport failures so the agent still reconnects.
                if isinstance(exc, LinkError):
                    self.link_error = exc
                intent = {"kind": "ask" if addressed else "dictate",
                          "text": text, "target": "" if addressed else "literal"}
            kind = intent["kind"]
            words, target = intent.get("text", text), intent.get("target", "")
            if not addressed and kind != "dictate":
                # Check the original speech even with a daemon that guesses a
                # target from prose. Only a unique exact Windows app may launch.
                candidate = shortcut_open_target(text) if kind == "open_app" else ""
                if (candidate and candidate.casefold() == target.casefold()
                        and self._open_app(candidate, intent, session, source, current, exact=True)):
                    return
                kind, words, target = "dictate", text, "literal"
            if kind == "dictate" and source == "hotkey":
                if target == "literal":
                    self.typer.run([("text", words)])
                else:
                    self._type(words)
            elif kind == "dictate" and target == "literal":
                self.ui.set_state("done", "Hold the dictation shortcut to type those words.")
                return
            elif kind in {"ask", "dictate"}:
                partial = ""

                def delta(piece):
                    nonlocal partial
                    partial += piece
                    self.ui.set_state("answering", partial)

                if getattr(self.client, "streaming", False) is True:
                    if self.player.cancelled.is_set():
                        return
                    answer = SpokenAnswer(self.player).ask(self.client, words, delta,
                                                           defer_speech=target == "task")
                    self.ui.set_state("answering", answer)
                else:
                    answer = self.client.ask(words, on_delta=delta)
                    self.ui.set_state("answering", answer)
                    self.player.play(self.client.say(answer))
            elif kind == "open_app":
                if not self._open_app(target, intent, session, source, current):
                    self.ui.set_state("error", f"Could not find {target}.")
                return
            elif kind == "switch_app":
                if not current():
                    return
                if not self.actions.switch_app(target):
                    self.ui.set_state("error", f"Could not find {target}.")
                    return
                self._confirm_action(intent.get("receipt"), current)
                self.ui.set_state("done", f"Switched to {target}.")
                return
            elif kind == "search":
                if not current():
                    return
                if not self.actions.search(target or words):
                    self.ui.set_state("error", "Could not open the search.")
                    return
                self._confirm_action(intent.get("receipt"), current)
            elif kind == "read_selection":
                if not current():
                    return
                selection = self.actions.read_selection()
                if not selection:
                    self.ui.set_state("error", "No selected text to read.")
                    return
                self.ui.set_state("answering", selection)
                self.player.play(self.client.say(selection))
                self._confirm_action(intent.get("receipt"), current)
            else:
                self.ui.set_state("error", "Unknown voice action.")
                return
            self.ui.set_state("done")
        except VoiceQuestionError as exc:
            self.ui.set_state("error", str(exc))
        except LinkError as exc:
            self.link_error = exc
            self.ui.set_state("error", str(exc))
        except (OSError, RuntimeError):
            # OS exception messages can contain document text or local paths.
            self.ui.set_state("error", "The voice action could not be completed.")
        finally:
            if self.link_error is not None:
                self.retire_profile_choice(self.client)
