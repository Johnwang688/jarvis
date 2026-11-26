"""Deliver one morning digest and one evening wrap-up per Chicago day.

The evening wrap-up can link to the day's learning digest. The
morning-briefing skill, voice reading, P2 commitments, I4 activity, C3
due-soon items and closing old digests are outside B4. Agents cannot read
the feed through the skill. This job answers no brief.
"""

from base64 import b64decode, b64encode
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import date, datetime, time as wall_time, timedelta
import fcntl
from functools import partial
import json
import os
from pathlib import Path
import stat
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis import config, untrusted
from jarvis.v2 import redact
from jarvis.v2.quiet import parse_hhmm

from . import claude_jobs
from ._paths import open_directory
from .feed import FeedError, _redact_text, _shape, _text, _unique_keys
from .messages import MAX_BODY, Message, MessageService, clean_source
from .planner import _safe, _spans

JOB = "brief"
MODEL = "haiku"
MAX_LINES = 8
RANK = 3
MORNING = "07:15"
EVENING = "21:00"

_CHICAGO = ZoneInfo("America/Chicago")
_WAKE, _NIGHT = wall_time(7), wall_time(23)
_HEADINGS = {"morning": "Morning brief", "evening": "Evening wrap-up"}
_PROMPT = Path(__file__).with_name("prompts") / "daily_brief.md"
_DEFAULT_RUN_JOB = claude_jobs.run_job
_STATE = "state.json"
_LOG = "log.jsonl"
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
_STATE_LIMIT = 1024
_EMAIL_MAX_ITEMS = 6
_COUNT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
                "nine", "ten", "eleven", "twelve")


@dataclass(frozen=True)
class Digest:
    ask: str
    lines: list[dict[str, str]]
    items: int
    times: tuple[str | None, ...] = ()
    ranks: tuple[int | None, ...] = ()


@dataclass(frozen=True)
class Result:
    outcome: str
    brief_id: str | None = None
    lines: int = 0
    summary: str = "none"
    note: str = ""
    retryable: bool = False


class _DigestWindowClosed(Exception):
    """The claimed day is over or quiet hours have begun."""


def _schedule(morning, evening):
    start, wrap = parse_hhmm(morning), parse_hhmm(evening)
    if start is None or wrap is None or not _WAKE <= start < wrap < _NIGHT:
        raise ValueError("Brief schedules require 07:00 <= morning < evening < 23:00.")
    return start, wrap


def schedule_from_env(environ):
    morning = environ.get("JARVIS_BRIEF_MORNING", MORNING)
    evening = environ.get("JARVIS_BRIEF_EVENING", EVENING)
    try:
        _schedule(morning, evening)
    except ValueError:
        return MORNING, EVENING
    return morning, evening


def _check_which(which):
    if not isinstance(which, str) or which not in _HEADINGS:
        raise ValueError("Choose the morning brief or the evening wrap-up.")


def due(now, *, last_run, which, morning=MORNING, evening=EVENING) -> bool:
    _check_which(which)
    start, wrap = _schedule(morning, evening)
    local = datetime.fromtimestamp(now, _CHICAGO)
    first, last = (start, wrap) if which == "morning" else (wrap, _NIGHT)
    return first <= local.time() < last and (
        last_run is None or datetime.fromtimestamp(last_run, _CHICAGO).date() < local.date()
    )


def read_calendar(start, end, *, grant_path=None):
    """Read primary Calendar titles and start times through the existing client."""
    if grant_path is not None and grant_path != Path(config.GOOGLE_TOKEN_PATH):
        raise ValueError("The Calendar grant is outside the OAuth token path.")
    from .tools.calendar import CalendarTools

    return CalendarTools().event_titles(start, end)


def read_mail(index, since, until):
    """Read only the indexed headlines and their Gmail message IDs."""
    docs = index.documents_between(since=since.timestamp(), until=until.timestamp())
    return [{"id": doc["ref"], "title": doc["title"]} for doc in docs if doc["source"] == "gmail"][:3]


def _redact_display(value):
    clean = _redact_text(value)
    if clean != f"{redact.WITHHELD_PREFIX}{redact.KEY}]":
        return clean
    first, newline, _ = value.partition("\n")
    # A footer-only key match can absorb a short heading like "Lab".
    # Keep it only when it cannot be a canonical base64 line and the same
    # scrub with a prose prefix leaves it intact and withholds all that follows.
    if not (newline and 1 <= len(first) < 4 and first.isascii() and first.isalpha()):
        return clean
    padded = first.encode("ascii").ljust(4, b"=")
    if len(first) > 1 and b64encode(b64decode(padded)) == padded:
        return clean
    prefix = "Title: "
    candidate = _redact_text(prefix + value).removeprefix(prefix)
    return candidate if candidate == first + newline + clean else clean


def _display_line(value, limit):
    if not isinstance(value, str):
        return ""
    # Keep the original line boundaries for multiline secrets. Scrub again
    # after normalization, before any cut, in case it exposes another token.
    raw_clean = _redact_display(value)
    line = untrusted.one_line(raw_clean, cap=max(2, len(raw_clean)))
    clean = _text(line, max(limit, len(line)), empty=True)
    return _text(untrusted.one_line(clean, cap=limit), limit, empty=True)


def _label(value):
    try:
        return _display_line(value, 120) or "(untitled)"
    except FeedError:
        return "(untitled)"


def _item(source, row, title="title", *, time_field=None):
    identifier = row.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    ref = f"{source}:{identifier}"
    try:
        if _text(ref, 2048) != ref:
            return None
    except FeedError:
        return None
    item = {"label": _label(row.get(title)), "ref": ref}
    if time_field is not None:
        item["time"] = row[time_field].astimezone(_CHICAGO).isoformat()
    return item


def _read(reader, args, name, notes, expected=list):
    if reader is None:
        return expected()
    try:
        result = reader(*args)
        if isinstance(result, expected):
            return result
    except Exception:
        pass
    notes.append({"label": f"{name} unavailable", "ref": f"jarvis:{name.lower()}"})
    return expected()


def _items(source, rows, *, time_field=None):
    return [item for row in rows if isinstance(row, dict)
            and (item := _item(source, row, time_field=time_field)) is not None]


def _timed(rows, field, *, until=None):
    valid = []
    for row in rows:
        when = row.get(field) if isinstance(row, dict) else None
        if isinstance(when, datetime) and when.utcoffset() is not None:
            stamp = when.timestamp()
            if until is None or stamp < until:
                valid.append((stamp, row))
    valid.sort(key=lambda entry: entry[0])
    return [row for _, row in valid]


def _activity_line(activity):
    parts = [f"{count} {_label(name)}" for name, count in activity.items()
             if isinstance(name, str) and type(count) is int and 0 < count <= 2**63 - 1]
    return [{"label": _label(", ".join(parts)), "ref": "jarvis:activity"}] if parts else []


def assemble(which, *, now, feed, calendar=None, commitments=None, mail=None, activity=None,
             learning=None, include_all=False) -> Digest:
    """Select display lines and count items before the display cap."""
    _check_which(which)
    local = datetime.fromtimestamp(now, _CHICAGO)
    today = datetime.combine(local.date(), wall_time.min, _CHICAGO)
    tomorrow, later = today + timedelta(days=1), today + timedelta(days=2)
    morning = which == "morning"
    notes = []
    decisions, other, ranks = [], [], {}
    for row in _read(feed.batch, (), "Feed", notes):
        if not isinstance(row, dict) or row.get("kind") == "digest":
            continue
        if line := _item("brief", row, "ask"):
            (decisions if row.get("kind") == "decision" else other).append(line)
            if type(row.get("rank")) is int:
                ranks[line["ref"]] = row["rank"]

    opening, closing = (today, tomorrow) if morning else (tomorrow, later)
    events = _read(calendar, (opening, closing), "Calendar", notes)
    if calendar is None:
        notes.append({"label": "Calendar not connected", "ref": "jarvis:calendar"})
    calendar_lines = _items("calendar", _timed(events, "start"), time_field="start")
    pending = _read(commitments, (later,), "Commitments", notes)
    commitment_lines = _items("commitment", _timed(pending, "due", until=later.timestamp()), time_field="due")

    if morning:
        since = datetime.fromtimestamp(now - 86_400, _CHICAGO)
        headlines = _read(mail, (since, local), "Mail", notes)
        lines = decisions + commitment_lines + calendar_lines + _items("gmail", headlines[:3]) + other
    else:
        counts = _read(activity, (today, local), "Activity", notes, expected=dict)
        lines = _activity_line(counts)
        if learning is not None:
            try:
                learning_line = learning()
                if learning_line is not None:
                    _shape(learning_line, ("label", "ref"))
                    lines.append({"label": _text(learning_line["label"], 120),
                                  "ref": _text(learning_line["ref"], 2048)})
            except Exception:
                notes.append({"label": "Learning unavailable", "ref": "jarvis:learning"})
        lines += decisions + calendar_lines + commitment_lines + other
    count = len(lines)
    ask = f"{_HEADINGS[which]} · {count} {'item' if count == 1 else 'items'}"
    visible = (lines if include_all else lines[:MAX_LINES - len(notes)]) + notes
    times = tuple(line.pop("time", None) for line in visible)
    return Digest(ask, visible, count, times, tuple(ranks.get(line["ref"]) for line in visible))


def build_prompt(digest) -> str:
    rows = []
    for index, line in enumerate(digest.lines):
        row = {"label": line["label"]}
        if index < len(digest.times) and (when := digest.times[index]) is not None:
            row["time"] = when
        rows.append(json.dumps(row, ensure_ascii=False))
    body = "\n".join(rows)
    return _PROMPT.read_text(encoding="utf-8").strip() + "\n\n" + untrusted.fence(body, "daily brief items")


def _check_directory(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Daily briefs need an owner-owned directory with mode 0700.")


@contextmanager
def _private_file(directory, name, flags):
    fd = os.open(name, flags | _FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("Daily briefs need owner-owned regular files with mode 0600.")
        yield fd
    finally:
        os.close(fd)


def _read_state(directory):
    try:
        with _private_file(directory, _STATE, os.O_RDONLY) as fd:
            data = os.read(fd, _STATE_LIMIT + 1)
    except FileNotFoundError:
        return {}
    if len(data) <= _STATE_LIMIT:
        raw = json.loads(data, object_pairs_hook=_unique_keys)
        if isinstance(raw, dict) and raw and raw.keys() <= _HEADINGS.keys():
            days = {key: date.fromisoformat(value) for key, value in raw.items() if isinstance(value, str)}
            if raw == {key: day.isoformat() for key, day in days.items()}:
                return days
    raise ValueError("Invalid daily brief state.")


def _write_state(directory, claims):
    temp = ".daily-brief-" + uuid4().hex
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            os.fchmod(fd, 0o600)
            json.dump({key: day.isoformat() for key, day in claims.items()}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(fd)
        os.replace(temp, _STATE, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=directory)


def _append_log(directory, now, day, which, result):
    row = {"ts": now, "day": day.isoformat(), "which": which, "outcome": result.outcome,
           "lines": result.lines, "summary": result.summary, "note": result.note}
    with _private_file(directory, _LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT) as fd:
        # A contender can log its result while the winning run holds the
        # directory lock. Serialize the short writes on the log itself.
        fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(fd, "w", encoding="ascii", closefd=False) as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(fd)
        os.fsync(directory)


def _work_directory(directory, path):
    with suppress(FileExistsError):
        os.mkdir("work", mode=0o700, dir_fd=directory)
    fd = os.open("work", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        _check_directory(fd)
        if os.listdir(fd) or not os.path.samestat(os.fstat(fd), path.stat(follow_symlinks=False)):
            raise ValueError("Daily briefs need an empty working directory.")
    finally:
        os.close(fd)
    return path


def _model_text(prompt, directory, path, budget, run_job, check_window):
    cwd = _work_directory(directory, path / "work")
    if run_job is _DEFAULT_RUN_JOB:
        cli = claude_jobs.resolve_cli()
        if not cli or not Path(cli).is_file() or not os.access(cli, os.X_OK):
            return ""
        run_job = partial(claude_jobs.run_job, cli=cli)
    admission = budget.admit(JOB, start_model=MODEL)
    if not admission.ok:
        budget.skip(JOB, admission)
        return ""

    try:
        check_window()
        try:
            job = run_job(JOB, prompt, model=MODEL, tools=(), cwd=cwd, timeout=300)
        except Exception:
            job = claude_jobs.JobResult(JOB, MODEL)
        budget.record(admission, job)
    except BaseException:
        budget.release(admission)
        raise
    return job.text if job.ok and job.outcome == "ok" else ""


def _email_items(which, digest):
    rows = []
    for line, when, rank in zip(digest.lines, digest.times, digest.ranks, strict=True):
        source = line["ref"].split(":", 1)[0]
        row = {"item": clean_source(line["label"]) or "Item text omitted.", "source": source}
        if when is not None:
            row["time"] = when
        priority = rank if rank is not None else (4 if source in {"calendar", "commitment"} else 9)
        if which == "morning" and source == "calendar":
            # Assembly already sorted calendar events by their start instant.
            priority = 0
        rows.append((priority, row))
    return [row for _, row in sorted(rows, key=lambda entry: entry[0])]


def _email_prompt(which, items, body_limit):
    rows = "\n".join(json.dumps(item, ensure_ascii=False) for item in items)
    path = _PROMPT.with_name(f"{which}_message.md")
    prompt = path.read_text(encoding="utf-8").strip().format(body_limit=body_limit)
    return prompt + "\n\n" + untrusted.fence(rows, f"{which} items")


def _email_reply(text, items):
    subject, blank, body = text.partition("\n\n")
    if not blank or not subject.startswith("Subject: "):
        raise ValueError("Expected a subject and an email body.")
    message = Message(subject.removeprefix("Subject: "), body)
    copied = {span for item in items for span in _spans(item["item"])}
    if any(not _safe(value) or any(span in copied for span in _spans(value))
           for value in (message.subject, message.body)):
        raise ValueError("The brief email copied source text or contained unsafe text.")
    return message


def _fallback_email(which, items, overflow=""):
    subject = _HEADINGS[which]
    if not items:
        return Message(subject, "There are no items to review tonight.\n\n"
                       "Reply if there is something you would like me to pick up.")
    opening = "Here is what is on today." if which == "morning" else "Here are the items from today."
    lines = [opening + " I suggest starting with the first one.", ""]
    for index, row in enumerate(items, start=1):
        # The model path scans its own words. Here the source text itself is sent.
        item = row["item"] if _safe(row["item"]) else "Item text omitted."
        punctuation = "" if item.endswith((".", "?", "!")) else "."
        lines.append(f"{index}. {item}{punctuation}")
    body = "\n".join(lines)
    reply = "\n\nReply with the numbers you want me to start on."
    body += overflow + reply if which == "morning" else reply + overflow
    return Message(subject, body)


def _brief_email(which, digest, model):
    if not digest.items:
        return _fallback_email(which, []), "none"
    items = _email_items(which, digest)[:_EMAIL_MAX_ITEMS]
    remaining = digest.items - _EMAIL_MAX_ITEMS
    overflow = ""
    if remaining > 0:
        count = _COUNT_WORDS[remaining] if remaining < len(_COUNT_WORDS) else str(remaining)
        overflow = f'\nAnd {count} more: reply "more" and I will list them.'
    try:
        message = _email_reply(model(_email_prompt(which, items, MAX_BODY - len(overflow))), items)
        if overflow:
            message = replace(message, body=message.body + overflow)
        return message, "model"
    except _DigestWindowClosed:
        raise
    except Exception:
        return _fallback_email(which, items, overflow), "fallback"


def _post(directory, path, which, now, feed, budget, run_job, sources, check_window, messages):
    digest = assemble(which, now=now, feed=feed, include_all=True, **sources)
    check_window()
    summary = "none"
    notes = digest.lines[digest.items:]
    lines = digest.lines[:min(digest.items, MAX_LINES - len(notes))] + notes
    why_now = "The items are listed in context." if digest.items else "There are no items to review."
    fields = {"kind": "digest", "rank": RANK, "ask": digest.ask, "why_now": why_now, "context": lines}
    if digest.items or which == "evening":
        model = partial(_model_text, directory=directory, path=path, budget=budget,
                        run_job=run_job, check_window=check_window)
        # Any failure to build or send the email leaves the card below.
        try:
            message, summary = _brief_email(which, digest, model)
        except _DigestWindowClosed:
            raise
        except Exception:
            summary = "fallback"
        else:
            check_window()
            try:
                sent = messages.send(message, fallback=fields).outcome == "sent"
            except Exception:
                sent = False
            if sent:
                return Result("sent", lines=len(digest.lines), summary=summary)
    check_window()
    try:
        brief = feed.post(fields, actor="jarvis")
    except Exception:
        return Result("error", lines=len(lines), summary=summary, note="The digest could not be posted.")
    return Result("posted", brief["id"], len(lines), summary)


def run(which, *, feed, budget, now=None, state_dir=None, calendar=None, commitments=None,
        mail=None, activity=None, learning=None, run_job=_DEFAULT_RUN_JOB, messages=None) -> Result:
    """Claim the day before assembling the digest. Keep the claim on failure.

    A digest that reaches quiet hours or crosses into another day after the
    claim is dropped for the claimed day. An explicit ``now`` fixes the clock
    for the run.
    """
    _check_which(which)
    clock = time.time if now is None else lambda: now
    started_at = clock()
    local = datetime.fromtimestamp(started_at, _CHICAGO)
    day = local.date()

    def check_window():
        current = datetime.fromtimestamp(clock(), _CHICAGO)
        if current.date() != day or not _WAKE <= current.time() < _NIGHT:
            raise _DigestWindowClosed

    path = Path(config.V3_DIR / "daily-briefs" if state_dir is None else state_dir).absolute()
    sources = {"calendar": calendar, "commitments": commitments, "mail": mail, "activity": activity,
               "learning": learning}
    if messages is None:
        from .t3_client import T3
        from .work import WorkService

        messages = MessageService(WorkService(t3=T3(timeout=15)), clock=clock)
    try:
        with open_directory(path, create=True) as directory:
            _check_directory(directory)
            if not _WAKE <= local.time() < _NIGHT:
                result = Result("quiet")
            else:
                try:
                    fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    result = Result("already")
                else:
                    claimed = False
                    try:
                        claims = _read_state(directory)
                        if which in claims and day <= claims[which]:
                            result = Result("already")
                        else:
                            claims[which] = day
                            _write_state(directory, claims)
                            claimed = True
                            result = _post(directory, path, which, started_at, feed, budget, run_job,
                                           sources, check_window, messages)
                    except _DigestWindowClosed:
                        result = Result("quiet")
                    except Exception:
                        result = Result("error", note="Daily briefs could not complete safely.",
                                        retryable=not claimed)
            try:
                _append_log(directory, started_at, day, which, result)
            except (OSError, ValueError):
                result = replace(result, note=(result.note + " The daily brief log could not be written.").strip())
            return result
    except (OSError, ValueError):
        return Result("error", note="The daily brief state directory is unavailable.", retryable=True)
