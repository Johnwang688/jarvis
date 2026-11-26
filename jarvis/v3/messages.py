"""Deliver short owner messages as T3 threads.

On 2026-11-25 the owner decided that message threads need no approval.
The directive at the start of each thread is his wording.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
import unicodedata
from zoneinfo import ZoneInfo

from jarvis import untrusted

from .planner import _safe, _scrub
from .work import Thread, WorkError


_DIRECTIVE = "***ONLY NOTIFY THE OWNER. DO NOT TAKE ANY ACTIONS UNTIL OWNER RESPONDS***"
_OPEN = "--- MESSAGE FROM JARVIS ---"
_CLOSE = "--- END OF MESSAGE ---"
_ID = re.compile(r"brief:[0-9a-f]+|[0-9a-f]{16,}|t3-thread:[^\s]*", re.IGNORECASE | re.ASCII)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+•]|[0-9]+[.)])\s+\S")
_MARKUP = re.compile(r"(?:MESSAGE\s+FROM\s+JARVIS|END\s+OF\s+MESSAGE|\]\(|!\[)", re.IGNORECASE)
_ANGLE = re.compile(r"<(?=(.))")
_INVISIBLE = re.compile(r"[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0\U000e0000-\U000e0fff]")
_MAX_LIST_ITEMS = 8
MAX_BODY = 1800
_CHICAGO = ZoneInfo("America/Chicago")


def _control(char):
    return untrusted.unseen(char) or unicodedata.category(char) == "Cs" or bool(_INVISIBLE.fullmatch(char))


def _alphanumeric(text):
    return "".join(char for char in text if char.isalpha() or char.isdigit()).casefold()


_DIRECTIVE_TEXT = _alphanumeric(_DIRECTIVE)


def _delimiter(line):
    start = (char for char in line if not char.isspace())
    for _ in range(3):
        char = next(start, None)
        if char is None:
            return False
        name = unicodedata.name(char, "")
        if not (unicodedata.category(char) == "Pd" or "DASH" in name or "HYPHEN" in name
                or "MINUS SIGN" in name or name.startswith("BOX DRAWINGS")):
            return False
    return True


def _plain(value, *, multiline):
    return not (
        _DIRECTIVE_TEXT in _alphanumeric(value)
        or _MARKUP.search(value)
        or any(_control(char) and not (multiline and char == "\n") for char in value)
        or any(_delimiter(line) or line.lstrip().startswith(("#", ">", "```", "~~~"))
               for line in value.split("\n"))
    )


def _scrub_message(text):
    return _ANGLE.sub(lambda m: "< " if m[1].isalpha() or m[1] in "/!?" else "<", _scrub(text))


def _validate(value, minimum, maximum, *, multiline=False):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum or not value.strip()
            or _ID.search(value) or not _plain(value, multiline=multiline)):
        raise ValueError("Invalid Jarvis message body." if multiline else "Invalid Jarvis message subject.")
    if multiline:
        items = [index for index, line in enumerate(value.split("\n")) if _LIST_ITEM.match(line)]
        if items and (len(items) > _MAX_LIST_ITEMS or items[-1] - items[0] + 1 != len(items)):
            raise ValueError("A Jarvis message can contain one short list.")


@dataclass(frozen=True)
class Message:
    subject: str
    body: str
    project: str | None = None

    def __post_init__(self):
        if self.project is not None and not isinstance(self.project, str):
            raise ValueError("Invalid Jarvis message project.")
        for field, minimum, maximum in (("subject", 4, 80), ("body", 1, MAX_BODY)):
            value = getattr(self, field)
            multiline = field == "body"
            if not isinstance(value, str) or any(_control(char) and not (multiline and char == "\n")
                                                for char in value):
                raise ValueError(f"Invalid Jarvis message {field}.")
            clean = _scrub_message(value)
            _validate(clean, minimum, maximum, multiline=multiline)
            object.__setattr__(self, field, clean)


def thread_prompt(message: Message) -> str:
    _validate(message.subject, 4, 80)
    _validate(message.body, 1, MAX_BODY, multiline=True)
    return (
        f"{_DIRECTIVE}\n\n"
        "Jarvis has a message for the owner. Send the owner the message below word for word as your reply, "
        "with nothing added before or after it. Then stop and wait. When the owner replies, "
        "do what the owner asks.\n\n"
        f"{_OPEN}\nSubject: {message.subject}\n\n{message.body}\n{_CLOSE}"
    )


def relay_sha256(text):
    return hashlib.sha256(text.rstrip("\n").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Result:
    outcome: str
    thread: Thread | None = None
    reason: str = ""


class MessageService:
    def __init__(self, work, *, clock, cap_per_day=8):
        self._work = work
        self._clock = clock
        self._cap_per_day = cap_per_day

    def send(self, message: Message, *, notice_key=None, fallback=None) -> Result:
        try:
            with self._work.message_delivery():
                now = self._clock()
                day = datetime.fromtimestamp(now, _CHICAGO).date()
                sent_today = 0
                for thread in self._work.threads():
                    if not thread.message:
                        continue
                    if notice_key is not None and thread.notice_key == notice_key:
                        return Result("sent", thread)
                    sent_today += datetime.fromtimestamp(thread.started_at, _CHICAGO).date() == day
                if sent_today >= self._cap_per_day:
                    return Result("refused", reason="The daily Jarvis message limit was reached.")
                thread = self._work.start_message(message, at=now, notice_key=notice_key, fallback=fallback)
                return Result("sent", thread)
        except WorkError as exc:
            return Result("failed", reason=str(exc))
        except Exception:
            return Result("failed", reason="The Jarvis message could not be sent.")


def clean_source(text):
    """Scrub source material used in code-built messages before any clipping."""
    clean = _ID.sub("[omitted]", _scrub_message(text))
    if _plain(text, multiline=True) and _plain(clean, multiline=True):
        return clean
    return ""


def work_message(thread, status, at, history) -> Message:
    verb = "failed" if status == "failed" else "finished"
    title = untrusted.one_line(clean_source(thread.title), cap=79 - len(verb)) or "T3 thread"
    elapsed = max(0, int(at - thread.started_at))
    minutes, seconds = divmod(elapsed, 60)
    hours, minutes = divmod(minutes, 60)
    duration = f"{hours} h {minutes} min" if hours else f"{minutes} min {seconds} sec"
    final = next((text for role, text in reversed(history) if role == "assistant" and text.strip()), "")
    quote = "\n".join(clean_source(final).rstrip().splitlines()[-5:]).strip()
    if len(quote) > 600:
        quote = "…" + quote[-599:]
    if quote and _safe(quote):
        summary = f"From the thread’s final message:\n“{quote}”"
    else:
        summary = "No final reply was available to quote."
    body = (f"The work in {clean_source(thread.project)} ran for about {duration}.\n\n"
            f"{summary}\n\n"
            "Reply here with a question or tell me what you would like me to do next.")
    return Message(f"{title} {verb}", body, project=thread.project)
