"""Sequential voice workflow; dependencies are injected for pure checks."""
from __future__ import annotations

import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace

if __package__:
    from .voiceactions import LaunchStatus, action_scope, match_app
    from .voicetype import (JARVIS_ADDRESS, POLITE_PREFIX, dictation_choice, literal_payload,
                            mentions_command, navigation_target, plan, sanitize, shortcut_open_target,
                            spoken_websites)
    from .voicewire import LinkError, VoiceQuestionError
else:
    from voiceactions import LaunchStatus, action_scope, match_app
    from voicetype import (JARVIS_ADDRESS, POLITE_PREFIX, dictation_choice, literal_payload,
                           mentions_command, navigation_target, plan, sanitize, shortcut_open_target,
                           spoken_websites)
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
SITE_CHOICE_SECONDS = 15.0
SITE_CANCELED = "Website choice canceled."
SITE_EXPIRED = "That website choice expired. Ask again."
SITE_UNCLEAR = "No website was chosen. Ask again and say first, second or a site name."
SITE_FAILED = "Could not send the website to the browser."
SITE_UNVERIFIED = "The website handoff could not be verified. Check the browser."
SITE_CHOSEN = "Website already chosen."
ASSISTANT_UPDATE = ("Restart the Jarvis daemon to use Right Ctrl commands. "
                    "Ctrl+D still dictates.")
ROUTE_UNAVAILABLE = "Voice command routing is unavailable. Ctrl+D still dictates."
CONVERSATION_ONLY = "Desktop actions are unavailable; answering in conversation only."
CLARIFY = "I wasn't sure what to do. Please say that again more specifically."
TOO_LONG = "That is too long for a command. Hold Ctrl+D to dictate long text."
VOICE_SOURCES = frozenset(("hotkey", "wake", "assistant", "dictation"))


def _profile_name(text):
    return " ".join(unicodedata.normalize("NFKC", sanitize(text)).casefold().split()).rstrip(".!?").rstrip()


@dataclass(frozen=True)
class _ProfileChoice:
    session: str | None
    labels: tuple[str, ...]
    receipt: str | None
    expires_at: float
    turn: str | None = None  # the routed turn awaiting an outcome, if any


@dataclass(frozen=True)
class _SiteChoice:
    """Two websites the owner named for navigation. Only an active choice acts."""
    session: str | None
    choices: tuple[str, str]
    receipt: str | None
    turn: str | None
    expires_at: float


@dataclass(frozen=True)
class _SiteReplyContext:
    """Recognition only (no session, receipt or turn): a late reply never acts."""
    choices: tuple[str, ...]
    until: float
    repeat: tuple[str, str] = ("done", SITE_EXPIRED)


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


_SITE_LEAD = re.compile(rf"^(?:(?:open|go to|go with|let{_APOSTROPHE}s go with|visit|choose|pick|select|use|"
                        rf"take me to|show me|i{_APOSTROPHE}d like|i would like|i want|let{_APOSTROPHE}s do|"
                        r"make it|maybe|oh|um|uh|ok|okay)[, ]+)+")
_SITE_TAIL = re.compile(r"(?:[, ]+(?:one|site|website|option|link|please|thanks|thank you))+$")
_SITE_ORDINALS = {"first": 0, "1st": 0, "number one": 0, "the first": 0, "former": 0, "the former": 0,
                  "second": 1, "2nd": 1, "number two": 1, "the second": 1, "last": 1, "the last": 1,
                  "latter": 1, "the latter": 1, "other": 1, "the other": 1}
# Words that make a reply about an offered site something other than a plain
# yes to it ("don't open noaa.gov", "no, the second one", "not nasa"). Such a
# reply never selects; it stays with the prompt instead of reaching the router.
_SITE_NEGATION = re.compile(
    rf"(?<![\w'])(?:don{_APOSTROPHE}?t|do not|dont|doesn{_APOSTROPHE}?t|not|no|never|neither|nor|"
    rf"except|without|instead|rather|skip|avoid|won{_APOSTROPHE}?t|wouldn{_APOSTROPHE}?t|"
    rf"shouldn{_APOSTROPHE}?t|can{_APOSTROPHE}?t|cannot|isn{_APOSTROPHE}?t|but|stop|cancel)(?![\w'])")
_SITE_ORDINAL_WORDS = re.compile(r"(?<![\w])(?:first|second|1st|2nd|former|latter)(?![\w])")
# A reply made only of offered sites, ordinals and joining words, but not one
# whole affirmative choice ("noaa noaa", "first or second", "the second one
# the second one"), is a repeated or listed reply: it stays local too.
_SITE_FILLER = frozenset({"first", "second", "1st", "2nd", "former", "latter", "last", "other", "number",
                          "one", "two", "the", "or", "and", "then", "site", "website", "option", "link",
                          "dot", "\x00"})


def _site_forms(choice):
    """Whole spoken forms of one offered website: host, www host, URL or label."""
    host = choice.split("://", 1)[-1].rstrip("/").casefold()
    label = host.split(".", 1)[0]
    return {host, "www." + host, choice.casefold(), choice.casefold().rstrip("/")}, label


def _site_reply(text, choices):
    """0 or 1 for a choice the owner picked, "cancel", "unclear", or None.

    Only a whole affirmative form selects: an ordinal, or one offered site's
    host, URL or label with nothing but courtesy or a selection verb around
    it. A negated or otherwise inexact reply that still names an offered site
    or ordinal is "unclear": it never selects and never reaches the router.
    None means the speech was not about this prompt.
    """
    reading = _profile_name(JARVIS_ADDRESS.sub("", text, count=1))
    if not reading:
        return None
    if _is_cancel_reply(reading) or _is_negative_reply(reading) or reading in ("stop", "never mind"):
        return "cancel"
    core = _TRAIL_COURTESY.sub("", POLITE_PREFIX.sub("", reading)).strip(" ,")
    core = _SITE_LEAD.sub("", core)
    stripped = _SITE_TAIL.sub("", core).strip(" ,")
    whole = {core, stripped, stripped.removeprefix("the ")}
    for form in (core, stripped, stripped.removeprefix("the ")):
        if form in _SITE_ORDINALS:
            return _SITE_ORDINALS[form]
    forms = [_site_forms(choice) for choice in choices]
    spelled = {re.sub(r"\s+dot\s+", ".", form) for form in whole}
    hits = {index for index, (urls, label) in enumerate(forms)
            if spelled & urls or (label in whole and [lab for _, lab in forms].count(label) == 1)}
    if len(hits) == 1:
        return hits.pop()
    named = set(spoken_websites(text)) & set(choices)
    labels = [label for _, label in forms]
    mentions = (bool(named) or bool(_SITE_ORDINAL_WORDS.search(reading))
                or any(re.search(rf"(?<![\w]){re.escape(label)}(?![\w])", reading) for label in labels if label))
    if not mentions:
        return None
    if _SITE_NEGATION.search(reading):
        return "unclear"
    # Mark every offered host, URL, spoken host and label (longest first),
    # then see whether anything else was said.
    spoken_forms = set()
    for urls, label in forms:
        spoken_forms |= urls | {label} | {url.split("://", 1)[-1].replace(".", " dot ") for url in urls}
    marked = stripped
    for form in sorted(filter(None, spoken_forms), key=len, reverse=True):
        marked = re.sub(rf"(?<![\w.]){re.escape(form)}(?![\w])", "\x00", marked)
    words = re.sub(r"[,;]", " ", marked).replace("\x00", " \x00 ").split()
    if words and all(word in _SITE_FILLER for word in words):
        return "unclear"
    return None


def _same_site(url, other):
    def site(value):
        value = value.split("://", 1)[-1].casefold().rstrip("/")
        return value.removeprefix("www.")
    return isinstance(url, str) and isinstance(other, str) and site(url) == site(other)


def _names_site(value, site):
    """Whether an app name or spoken target is an offered website's name."""
    if not isinstance(value, str) or not isinstance(site, str):
        return False
    if _same_site(value, site):
        return True
    host = site.split("://", 1)[-1].casefold().rstrip("/").removeprefix("www.")
    words = " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split()).removeprefix("www ")
    return bool(words) and words in (" ".join(re.sub(r"[^\w]+", " ", host).split()), host.split(".", 1)[0])


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

    def ask(self, client, words, delta, *, defer_speech=False, turn=None):
        self.worker.start()
        try:
            answer = client.ask_spoken(words, delta, self.offer, self.player.cancelled,
                                       defer_speech=defer_speech, **({"turn": turn} if turn else {}))
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
        Flow.retire_site_choice(client)
        pending = getattr(client, "_pending_chrome_profile", None)
        if isinstance(pending, _ProfileChoice):
            retired_at = min(time.monotonic(), pending.expires_at)
            pending = _ProfileReplyContext(pending.labels, retired_at + PROFILE_REPLY_GRACE_SECONDS)
            client._pending_chrome_profile = pending
        return pending if isinstance(pending, _ProfileReplyContext) else ()

    @staticmethod
    def retire_choices(client):
        """Retire both prompts on a lost link; return what a new client may keep.

        The result holds only recognition contexts (no session, receipt or
        turn), so a reconnecting worker carries reply recognition for the
        Chrome profile and website prompts alike and never their authority.
        """
        profile = Flow.retire_profile_choice(client)
        site = getattr(client, "_pending_site_choice", None)
        return (profile if isinstance(profile, _ProfileReplyContext) else None,
                site if isinstance(site, _SiteReplyContext) else None)

    @staticmethod
    def carry_choices(client, carried):
        """Install contexts from ``retire_choices`` on a new client.

        Anything that is not a recognition-only context is dropped, so no
        click, navigation or receipt can cross a reconnect.
        """
        profile, site = carried if isinstance(carried, tuple) and len(carried) == 2 else (None, None)
        if isinstance(profile, _ProfileReplyContext):
            client._pending_chrome_profile = profile
        if isinstance(site, _SiteReplyContext):
            client._pending_site_choice = site

    @staticmethod
    def retire_site_choice(client):
        """Stop, cancel, a new capture's routing, or a lost session: never act again."""
        pending = getattr(client, "_pending_site_choice", None)
        if isinstance(pending, _SiteChoice):
            retired_at = min(time.monotonic(), pending.expires_at)
            client._pending_site_choice = _SiteReplyContext(
                pending.choices, retired_at + PROFILE_REPLY_GRACE_SECONDS)

    @staticmethod
    def expire_site_choice(client):
        pending = getattr(client, "_pending_site_choice", None)
        if (isinstance(pending, _SiteChoice)
                and (pending.session != getattr(client, "session", None)
                     or time.monotonic() >= pending.expires_at)):
            Flow.retire_site_choice(client)
            pending = client._pending_site_choice
        if isinstance(pending, _SiteReplyContext) and time.monotonic() >= pending.until:
            client._pending_site_choice = None

    @classmethod
    def expire_profile_choice(cls, client):
        cls.expire_site_choice(client)
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

    def _offer_profile_choice(self, receipt, session, source, current, turn=None):
        options = getattr(self.actions, "chrome_profile_options", None)
        choose = getattr(self.actions, "choose_chrome_profile", None)
        labels = options() if callable(options) and callable(choose) else ()
        if not current():
            return
        if (not isinstance(labels, tuple) or not labels
                or not all(isinstance(label, str) and _profile_name(label) for label in labels)):
            self.ui.set_state("error", PROFILE_MANUAL)
            return
        pending = _ProfileChoice(session, labels, receipt, time.monotonic() + PROFILE_CHOICE_SECONDS, turn)
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
                    self._outcome(pending.turn, "confirmed")
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
        turn = intent.get("turn")
        if not current():
            self._outcome(turn, "cancelled")
            return True
        kwargs = {"exact": True} if exact else {}
        result = _run_native(current, self.actions.open_app, target, **kwargs)
        if not current():
            self._outcome(turn, "cancelled")
            return True
        if not result:
            self._outcome(turn, "failed")
            return False
        if result == LaunchStatus.CHROME_PROFILE_REQUIRED:
            self._offer_profile_choice(intent.get("receipt"), session, source, current, turn)
        elif result == LaunchStatus.CHROME_WINDOW_UNCONFIRMED:
            self._outcome(turn, "failed")
            self.ui.set_state("error", "Chrome launch requested. Check the Chrome window.")
        else:
            self._confirm_action(intent.get("receipt"), current)
            self._outcome(turn, "confirmed")
            self.ui.set_state("done", f"Opened {target}.")
        return True

    def _outcome(self, turn, status):
        """Observational route feedback after dispatch; it can change nothing."""
        if not turn:
            return
        try:
            self.client.route_outcome(turn, status)
        except LinkError as exc:
            self.link_error = exc  # reconnect before the next clip; the action stands
        except Exception:
            pass

    def _route_names(self):
        """Exact installed app names and open-window app names to ground against.

        The helper offers names from the Start catalog only: no window titles
        or other window text leave this process.
        """
        names = getattr(self.actions, "route_names", None)
        try:
            got = names() if callable(names) else None
        except Exception:
            got = None
        if (isinstance(got, tuple) and len(got) == 2 and all(isinstance(part, tuple) for part in got)
                and all(isinstance(name, str) for part in got for name in part)):
            return got
        return (), ()

    def _dictate(self, text, current):
        """Ctrl+D: type into the focused app. No routing, chat, receipt or action."""
        if not current():
            return
        if len(text) > 2000:
            self.typer.run([("text", text)])  # a long paragraph stays complete and literal
        else:
            how, words = dictation_choice(text)
            if how == "literal":
                self.typer.run([("text", words)])
            else:
                self.typer.run(plan(words))
        self.ui.set_state("done")

    def _speak_prompt(self, text, current):
        wav = self.client.say(text)
        if current():
            self.player.play(wav)

    def _offer_site_choice(self, intent, session, source, current):
        choices = intent["choices"]
        pending = _SiteChoice(session, choices, intent.get("receipt"), intent.get("turn"),
                              time.monotonic() + SITE_CHOICE_SECONDS)
        self.client._pending_site_choice = pending
        names = " or ".join(choice.split("://", 1)[-1] for choice in choices)
        instruction = ("Say Jarvis, then first, second or cancel." if source == "wake"
                       else "Hold Right Ctrl and say first, second or cancel.")
        self.ui.set_state("profile_choice", f"Which website: {names}?\n{instruction}", pending.expires_at)
        try:
            wav = self.client.say(f"Which website: {names}?")
            if current() and time.monotonic() < pending.expires_at:
                self.player.play(wav)
        except (OSError, RuntimeError):
            self.retire_site_choice(self.client)
            raise

    def _offered_sites(self):
        """(sites, what to show) while an offer or its reply grace stands.

        An offered website is opened by the choice handler or not at all.
        Speech that names one without being a plain choice ("open noaa.gov,
        actually disregard that", or the same site again after it was chosen)
        may still be routed, but a routed navigation to an offered site is
        or app launch of an offered site is refused in ``_laya_turn`` and on
        the unrouted path: the offer answers it, never the router.
        """
        self.expire_site_choice(self.client)
        pending = getattr(self.client, "_pending_site_choice", None)
        if isinstance(pending, _SiteChoice):
            return pending.choices, ("done", SITE_UNCLEAR)
        if isinstance(pending, _SiteReplyContext):
            return pending.choices, pending.repeat
        return None

    def _handle_site_reply(self, text, current):
        self.expire_site_choice(self.client)
        pending = getattr(self.client, "_pending_site_choice", None)
        if not isinstance(pending, (_SiteChoice, _SiteReplyContext)):
            return False
        if not current():
            return True
        choice = _site_reply(text, pending.choices)
        if choice is None:
            # Unrelated speech: the offer loses its authority and routing goes on.
            self.retire_site_choice(self.client)
            return False
        if isinstance(pending, _SiteReplyContext):
            # Retired, expired or already answered: show the outcome again and
            # never navigate, confirm or route, whatever the reply now says.
            self.ui.set_state(*pending.repeat)
            return True

        def settle(state, message):
            # The prompt is answered. A repeat inside the grace re-shows what
            # actually happened, never a success the handoff did not have.
            self.client._pending_site_choice = _SiteReplyContext(
                pending.choices, time.monotonic() + PROFILE_REPLY_GRACE_SECONDS, (state, message))
            self.ui.set_state(state, message)

        # Consume before acting: a repeated reply can never navigate twice, and
        # an interrupted handoff is recalled as unverified, not as chosen.
        self.client._pending_site_choice = _SiteReplyContext(
            pending.choices, time.monotonic() + PROFILE_REPLY_GRACE_SECONDS, ("error", SITE_UNVERIFIED))
        if choice in ("cancel", "unclear"):
            self._outcome(pending.turn, "cancelled")
            settle("done", SITE_CANCELED if choice == "cancel" else SITE_UNCLEAR)
            return True

        def authorized():
            return (current() and pending.session == getattr(self.client, "session", None)
                    and time.monotonic() < pending.expires_at)

        navigate = getattr(self.actions, "navigate", None)
        if not callable(navigate):
            self._outcome(pending.turn, "failed")
            settle("error", "Website navigation is unavailable.")
            return True
        if not authorized():
            settle("done", SITE_EXPIRED)
            return True
        handed_off = _run_native(authorized, navigate, pending.choices[choice])
        if not current():
            self._outcome(pending.turn, "cancelled")
            return True
        if handed_off is not True:
            self._outcome(pending.turn, "failed")
            settle("error", SITE_FAILED)
            return True
        self._confirm_action(pending.receipt, current)
        self._outcome(pending.turn, "confirmed")
        self.client._pending_site_choice = _SiteReplyContext(
            pending.choices, time.monotonic() + PROFILE_REPLY_GRACE_SECONDS, ("done", SITE_CHOSEN))
        self.ui.set_state("done", "Sent the website to the browser.")
        return True

    def _answer(self, words, *, defer_speech=False, turn=None):
        partial = ""

        def delta(piece):
            nonlocal partial
            partial += piece
            self.ui.set_state("answering", partial)

        extra = {"turn": turn} if turn else {}
        if getattr(self.client, "streaming", False) is True:
            if self.player.cancelled.is_set():
                return False
            answer = SpokenAnswer(self.player).ask(self.client, words, delta,
                                                   defer_speech=defer_speech, **extra)
            self.ui.set_state("answering", answer)
        else:
            answer = self.client.ask(words, on_delta=delta, **extra)
            self.ui.set_state("answering", answer)
            self.player.play(self.client.say(answer))
        return True

    def _laya_turn(self, text, clean, source, session, current):
        """A negotiated Right Ctrl or wake command: one local or Laya decision.

        Explicit literal dictation and an active clarification reply are
        decided here without the router; anything else is routed once with
        the full transcript. Every action rechecks generation and session just
        before it runs and confirms its receipt only after native success.
        """
        if len(text) > 2000:
            self.ui.set_state("error", TOO_LONG)
            return
        payload = literal_payload(text)
        if payload:
            self.retire_site_choice(self.client)
            if current():
                # The payload is text through the entire call: "send it" stays words.
                self.typer.run([("text", payload)])
                self.ui.set_state("done")
            return
        offered = self._offered_sites()
        if self._handle_site_reply(clean, current):
            return
        apps, windows = self._route_names()
        if not current():
            return
        self.ui.set_state("thinking")
        try:
            intent = self.client.laya_route(
                text, source, apps=apps, windows=windows,
                with_receipt=getattr(self.client, "action_receipts", False) is True)
        except LinkError as exc:
            self.link_error = exc
            self.ui.set_state("error", ROUTE_UNAVAILABLE)
            return
        turn, kind, target = intent["turn"], intent["kind"], intent["target"]
        if not current():
            self._outcome(turn, "cancelled")
            return
        if (kind in ("navigate", "open_app") and offered
                and any(_names_site(target, site) for site in offered[0])):
            # Not a plain choice, so the offer did not select it; never the
            # router either, as a page or as an installed app of that name.
            self._outcome(turn, "cancelled")
            pending = getattr(self.client, "_pending_site_choice", None)
            if isinstance(pending, _SiteReplyContext) and pending.choices == offered[0]:
                self.client._pending_site_choice = replace(pending, repeat=offered[1])
            self.ui.set_state(*offered[1])
            return
        if kind == "navigate":
            navigate = getattr(self.actions, "navigate", None)
            if not callable(navigate):
                self._outcome(turn, "failed")
                self.ui.set_state("error", "Website navigation is unavailable.")
                return
            handed_off = _run_native(current, navigate, target)
            if not current():
                self._outcome(turn, "cancelled")
                return
            if handed_off is not True:
                self._outcome(turn, "failed")
                self.ui.set_state("error", "Could not send the website to the browser.")
                return
            self._confirm_action(intent.get("receipt"), current)
            self._outcome(turn, "confirmed")
            self.ui.set_state("done", "Sent the website to the browser.")
        elif kind == "open_app":
            # Exact catalog resolution of a name this helper offered.
            if not self._open_app(target, intent, session, source, current, exact=True):
                self.ui.set_state("error", f"Could not find {target}.")
        elif kind in ("switch_app", "search", "read_selection"):
            if kind == "switch_app":
                # Exact window grounding of an offered catalog name: a title
                # that merely mentions it (a Notepad note about Chrome) never
                # takes focus.
                done = _run_native(current, self.actions.switch_app, target, exact=True) is True
                failure = f"Could not find {target}."
            elif kind == "search":
                done = _run_native(current, self.actions.search, target) is True
                failure = "Could not open the search."
            else:
                selection = _run_native(current, self.actions.read_selection)
                done = isinstance(selection, str) and bool(selection)
                failure = "No selected text to read."
                if done and current():
                    self.ui.set_state("answering", selection)
                    self.player.play(self.client.say(selection))
                    done = not self.player.cancelled.is_set()
                selection = None
            if not current():
                self._outcome(turn, "cancelled")
                return
            if not done:
                self._outcome(turn, "failed")
                self.ui.set_state("error", failure)
                return
            self._confirm_action(intent.get("receipt"), current)
            self._outcome(turn, "confirmed")
            self.ui.set_state("done", f"Switched to {target}." if kind == "switch_app" else None)
        elif kind == "clarify":
            if target == "website":
                self._offer_site_choice(intent, session, source, current)
            else:
                self.ui.set_state("done", CLARIFY)
                self._speak_prompt(CLARIFY, current)
        elif kind == "ask":
            if target == "conversation":
                self.ui.set_state("thinking", CONVERSATION_ONLY)
            if self._answer(text, turn=turn):
                self.ui.set_state("done")
        else:
            self.ui.set_state("error", "Unknown voice action.")

    def on_clip(self, wav, source, *, transcript=None):
        self.link_error = None
        session = getattr(self.client, "session", None)

        def current():
            return self._can_act() and session == getattr(self.client, "session", None)

        try:
            if source not in VOICE_SOURCES:
                raise ValueError("Unknown voice source")
            self.ui.set_state("transcribing")
            # Both physical shortcuts transcribe as before; only wake verifies the speaker.
            text = transcript if transcript is not None else self.client.dictate(
                wav, source="wake" if source == "wake" else "hotkey")["text"]
            if session != getattr(self.client, "session", None):
                return
            clean = sanitize(text)
            if source == "dictation":
                # Ctrl+D never routes, asks, acts, confirms or answers a prompt.
                if not clean:
                    self.ui.set_state("done", "No speech heard")
                else:
                    self._dictate(text, current)
                return
            laya = getattr(self.client, "laya_routing", False) is True
            if source == "assistant" and not laya:
                # A daemon without the negotiated mode cannot tell this from
                # dictation. Never type or act on it.
                self.ui.set_state("error", ASSISTANT_UPDATE)
                return
            if not clean or (source in ("wake", "assistant") and clean.rstrip(".,!?").casefold() == "jarvis"):
                self.expire_profile_choice(self.client)
                pending = getattr(self.client, "_pending_chrome_profile", None)
                if isinstance(pending, _ProfileChoice) and current():
                    self._show_profile_choice(pending, source)
                else:
                    self.ui.set_state("done", "No speech heard")
                return
            if self._handle_profile_reply(clean, current):
                return
            if laya and source in ("assistant", "wake"):
                self._laya_turn(text, clean, source, session, current)
                return
            if source == "hotkey" and len(text) > 2000:
                # Route replies are capped. A long paragraph stays complete
                # and literal; a quoted suffix must never become an Enter key.
                self.typer.run([("text", text)])
                self.ui.set_state("done")
                return
            url = navigation_target(text, source)
            addressed = source == "wake" or bool(JARVIS_ADDRESS.match(clean))
            self.ui.set_state("thinking")
            try:
                if getattr(self.client, "action_receipts", False) is True:
                    intent = self.client.route(text, source, with_receipt=True)
                else:
                    intent = self.client.route(text, source)
            except Exception as exc:
                # Preserve dictation on route failure. The complete navigation
                # frame below still requires an agreed target before any action.
                if isinstance(exc, LinkError):
                    self.link_error = exc
                intent = {"kind": "ask" if addressed else "dictate",
                          "text": text, "target": "" if addressed else "literal"}
            kind = intent["kind"]
            words, target = intent.get("text", text), intent.get("target", "")
            # A reply context carried to a helper without Laya routing still
            # keeps its sites: nothing here opens one the offer did not.
            offered = self._offered_sites()
            named = [url, target] if kind in ("navigate", "open_app") else [url]
            if offered and kind == "open_app":
                # The legacy matcher resolves loosely ("NOAA" finds an app
                # called noaa.gov): judge the app it would launch as well.
                apps = getattr(self.actions, "apps", None)
                app = match_app(target, apps) if isinstance(apps, list) and isinstance(target, str) else None
                named.append(app["name"] if app else None)
            if offered and any(_names_site(value, site) for value in named for site in offered[0]):
                self.ui.set_state(*offered[1])
                return
            if url or kind == "navigate":
                if not current():
                    return
                navigate = getattr(self.actions, "navigate", None)
                if not url or kind != "navigate" or target != url or not callable(navigate):
                    self.ui.set_state("error", "Website navigation is unavailable.")
                    return
                handed_off = _run_native(current, navigate, url)
                if not current():
                    return
                if handed_off is not True:
                    self.ui.set_state("error", "Could not send the website to the browser.")
                    return
                self._confirm_action(intent.get("receipt"), current)
                self.ui.set_state("done", "Sent the website to the browser.")
                return
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
                if not self._answer(words, defer_speech=target == "task"):
                    return
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
