"""Bind microphone and pop-up answers to the one announced broker card."""

from collections import Counter
from dataclasses import dataclass
import re

from .. import policy
from ..approvals import ApprovalConflict


ANSWER_WINDOW_S = 15
ANSWER_DECODE_GRACE_S = 5
VOICE_NEEDS_CLICK = policy.MONEY | policy.NEVER
_CLICK_PREFIXES = ("credential.", "policy.", "gate.", "money.", "browser.")
YES = frozenset({
    "yes", "yeah", "yep", "yes please", "approve", "approved", "go ahead",
    "do it", "allow", "allow it", "yes go ahead", "yes do it",
})
NO = frozenset({
    "no", "nope", "no thanks", "deny", "denied", "don't", "do not",
    "cancel", "reject", "stop",
})
_NON_WORD = re.compile(r"[^\w]|_")
_NAME = re.compile(r"^(?:hey )?jarvis(?: |$)")
_WORDS = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_DENY = NO - {"don't"} | {"don t"}


def answer_decision(text):
    phrase = _NAME.sub("", " ".join(_NON_WORD.sub(" ", text.lower()).split()))
    if phrase in YES:
        return "approve"
    if phrase in _DENY:
        return "deny"
    return None


@dataclass
class _Card:
    id: str
    digest: str
    tool: str
    kind: str | None
    deadline: float | None = None
    warned: bool = False

    @property
    def voice_allowed(self):
        return (self.kind is not None and self.kind not in VOICE_NEEDS_CLICK
                and not self.kind.startswith(_CLICK_PREFIXES))


class VoiceApproving:
    """Used by the session's worker, with only counters read by status callers."""

    def __init__(self, broker, *, clock):
        self._broker = broker
        self._clock = clock
        self._card = None
        self._needs_click = False
        self._counters = Counter(voice_approvals=0, voice_denials=0, popup_answers=0, ignored_answers=0)

    def status(self):
        return dict(self._counters)

    def ask(self, item):
        from ..fastpath import _POLICY_KINDS

        tool = item["tool"]
        self._card = _Card(item["id"], item["digest"], tool, _POLICY_KINDS.get(tool))
        self._needs_click = False
        return self._card.voice_allowed

    def clear(self):
        self._card = None
        self._needs_click = False

    def prompt_finished(self, id, at):
        card = self._card
        if card is not None and card.id == id and card.deadline is None:
            card.deadline = at + ANSWER_WINDOW_S

    def pending(self):
        card = self._card
        if card is not None:
            return next((item for item in self._broker.pending(approval_id=card.id)
                         if (item["id"], item["digest"]) == (card.id, card.digest)), None)
        return None

    def ignore(self):
        self._counters["ignored_answers"] += 1

    def take_needs_click(self):
        needs_click, self._needs_click = self._needs_click, False
        return needs_click

    def heard(self, text, *, started_at=None):
        decision = answer_decision(text)
        if decision is None:
            return False
        card = self._card
        now = self._clock()
        if started_at is None:
            started_at = now
        if (card is None or card.deadline is None
                or not card.deadline - ANSWER_WINDOW_S <= started_at < card.deadline
                or now > card.deadline + ANSWER_DECODE_GRACE_S):
            self.ignore()
            return False
        if self.pending() is None:
            self.clear()
            self.ignore()
        elif decision == "approve" and not card.voice_allowed:
            if not card.warned:
                card.warned = self._needs_click = True
            self.ignore()
        elif self._answer(card, decision, via="voice", heard_words=len(_WORDS.findall(text))):
            self._counters["voice_approvals" if decision == "approve" else "voice_denials"] += 1
        return True

    def click(self, id, digest, decision):
        card = self._card
        if (card is None or (id, digest) != (card.id, card.digest)
                or decision not in ("approve", "deny")):
            self.ignore()
            return False
        if self.pending() is None:
            self.clear()
            self.ignore()
            return False
        if self._answer(card, decision, via="popup"):
            self._counters["popup_answers"] += 1
            return True
        return False

    def _answer(self, card, decision, *, via, heard_words=None):
        self.clear()
        try:
            self._broker.answer(card.id, card.digest, decision, via=via, heard_words=heard_words)
        except (ApprovalConflict, KeyError):
            self.ignore()
            return False
        return True
