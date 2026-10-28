"""What one HUD chat may see of another (chat-to-chat plan §3, C0).

Pure over `Stores`: no daemon, no model call, no v1 session, no native CLI
transcript. The one source is the daemon's own `log.jsonl` for each thread,
and only the rows of the projection table (§3.2) ever come back:

| log record                    | row role                                   | text                        |
|-------------------------------|--------------------------------------------|-----------------------------|
| `user` (owner, Discord, DM)   | `owner`, with marks                        | `data.typed`, never `text`  |
| `text`                        | `Jarvis` / `Claude` / `Codex`              | `data.text`                 |
| `relay_in`                    | `chat <title> (<id>), not the owner`       | `data.text`                 |
| `relay_out`                   | `sent to <title> (<id>)`                   | `data.text`                 |
| `relay_reply`                 | `reply from <title> (<id>)`                | `data.text`                 |
| `model_set`, `notice`,        | `system`                                   | the daemon's own sentence   |
| `cli_turn`                    |                                            |                             |

Everything else — tool events and their arguments and summaries, thinking,
gates, questions, plans, usage, errors, sub-agents — is omitted. A `user`
record's `text` is what the provider received, with any attached file
inlined, so it is never read: `typed` is the owner's own words, and
attachments are named, never opened. A `user` record from the system (a
schedule, an escape-hatch result) shows its first line only, as the Discord
mirror does, because what follows is command output.

Every row's text is scrubbed where it is read (§3.3), outside any daemon
lock (`scrub_read`): `strip_invisible` first (so a zero-width character
cannot split a key out of a pattern's sight), then `secrets.redact_inline`
(#205's display redaction: URL userinfo, typed credentials, credential-named
JSON keys), then `redact.for_model` (known values and the credential shapes,
matched on a normalized view), then `strip_invisible` again. Rows are what
another chat's **model** reads (`tools/chats.py`), so the display-only rule
for a value after a credential's label is not here; the phone adds it to
the rows it shows (`remote_api`). Three scrubs, one per reader (scrub
hardening, 2026-10-25):

- `scrub_read`: the rows and titles (model input, the owner's typed words
  among them, so `redact_inline`'s typed credentials stay withheld);
- `scrub`: what the owner reads (a relay's progress, an inbox post, the
  phone): `scrub_read`'s layers plus `redact.labelled_values`;
- `scrub_for_model`: what one agent wrote for another, as the other model
  gets it (a relay's body and reply): known values and shapes only, so
  ordinary code arrives as written (review of PR #308). The relay's records
  and a compaction reason, which the owner reads, take `scrub` (review of
  #314, M1).

A scrub that raises withholds the row (`WITHHELD`); it never passes the text
through. Titles are model-written too: cleaned (`approvals.clean_line`),
scrubbed, capped.

Every result that carries a chat's words is one `untrusted.fence_chat`, with
a fresh nonce. Reads are bounded by the constants below, and a search by a
wall-clock budget too. A malformed log line is skipped, never raised.

The relay records are C3's (assumed shape, read here):
    relay_in:    data {"message_id", "from", "title", "provider", "text", "chars"}
    relay_out:   data {"message_id", "to", "title", "provider", "text", ...}
    relay_reply: data {"message_id", "from", "title", "provider", "text", ...}

`live` is the daemon's snapshot of what is running (plan §3.1):
    {thread_id: {"running": bool, "relay_from": <sender id> | None,
                 "queued": int, "relays_waiting": int}}
A thread missing from it, or `live=None`, is idle.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import time
from typing import Any, Callable, Iterable
import unicodedata

from jarvis import untrusted
from jarvis.tools import secrets

from . import redact
from .approvals import clean_line
from .model import ProviderName, Role, Thread, utcnow
from .projects import name_key

# -- limits (plan §3.5); pinned by tests/v2/chat_reads_check.py ------------------------

TAIL_BYTES = 4 * 1024 * 1024        # the window of log.jsonl any read looks at
SEARCH_BUDGET_S = 2.0               # a search's wall budget
SEARCH_CHATS = 200                  # the most recently updated chats a search scans
LIST_DEFAULT, LIST_MAX = 20, 50
LIST_ROW_CHARS = 200
SUMMARY_CHARS = 3000                # a whole summary
SUMMARY_ROWS = 6                    # the last rows a summary shows
SNIPPET_CHARS = 300                 # first owner message, summary rows, search hits
SEARCH_DEFAULT, SEARCH_MAX = 20, 50
QUERY_MIN, QUERY_MAX = 2, 200
READ_DEFAULT, READ_MAX = 20, 100
ROW_CHARS = 2000                    # one row in read_tail, with a cut note
READ_TOTAL_CHARS = 16000            # a whole read_tail
TITLE_CHARS = 80
PROJECT_CHARS = 40
WITHHELD = "[withheld: could not be checked]"

_ID = re.compile(r"[0-9a-f]{8}")
_PROVIDER_WORD = {ProviderName.FAST.value: "Jarvis", ProviderName.CLAUDE.value: "Claude",
                  ProviderName.CODEX.value: "Codex"}
_VIA_MARK = {"discord": "via Discord", "dm": "via DM",
             # The owner's answer to a decision (inbox B1): their pick or words,
             # framed by code, with the asking chat's own question quoted.
             "inbox": "via the inbox (not typed in this chat)",
             # The same, answered on the phone (P-4).
             "phone": "via the phone's inbox (not typed in this chat)"}
_SYSTEM_KINDS = ("model_set", "notice", "cli_turn", "compacted")
_RELAY_KINDS = ("relay_in", "relay_out", "relay_reply")


@dataclass(frozen=True)
class Refusal:
    text: str                       # always starts "Error: "


@dataclass(frozen=True)
class Row:
    role: str
    text: str                       # scrubbed, or WITHHELD
    at: str | None = None
    marks: tuple[str, ...] = ()


# -- the scrub (§3.3) ---------------------------------------------------------------------


def scrub_read(text: Any) -> str:
    """A row's or a title's text as another chat's model reads it: invisible
    characters out, the credentials typed inline (`secrets.redact_inline`),
    Jarvis's credential values and every credential shape
    (`redact.for_model`); `WITHHELD` if any layer raises."""
    try:
        out = untrusted.strip_invisible(str(text or ""))
        out = secrets.redact_inline(out)
        out = redact.for_model(out)
        return untrusted.strip_invisible(out)
    except Exception:  # noqa: BLE001 - a scrub that fails withholds, whatever failed
        return WITHHELD


def scrub(text: Any) -> str:
    """`text` as the owner reads it (a relay's progress, an inbox post, what
    the phone receives): `scrub_read`'s layers, then the value after a
    credential's label (`redact.labelled_values`); `WITHHELD` if any layer
    raises. **Never for text an agent reads**: that is `scrub_read` or
    `scrub_for_model`."""
    try:
        out = untrusted.strip_invisible(str(text or ""))
        out = secrets.redact_inline(out)
        out = redact.for_display(out)
        return untrusted.strip_invisible(out)
    except Exception:  # noqa: BLE001 - a scrub that fails withholds, whatever failed
        return WITHHELD


def scrub_for_model(text: Any) -> str:
    """`text` as an agent reads it (a relay's body, reply and failure
    reason as the models get them; never what is kept or shown, which is
    `scrub`: review of #314, M1): invisible characters out, then
    `redact.for_model` (known values and high-confidence credential shapes
    only, so ordinary code reaches the model as written: review of PR #308);
    `WITHHELD` if any layer raises."""
    try:
        out = untrusted.strip_invisible(str(text or ""))
        out = redact.for_model(out)
        return untrusted.strip_invisible(out)
    except Exception:  # noqa: BLE001 - a scrub that fails withholds, whatever failed
        return WITHHELD


def _cap(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:max(1, cap - 1)].rstrip() + "…"


def clean_title(title: Any, cap: int = TITLE_CHARS) -> str:
    """A title (model-written) as one clean, scrubbed, capped line."""
    if not isinstance(title, str) or not title.strip():
        return "(untitled)"
    line = scrub_read(clean_line(title, cap=10 * cap))
    if line == WITHHELD:
        return "(title withheld)"
    return clean_line(line, cap=cap) or "(untitled)"


# A chat's title on an approval card (C6): "turn started by a message from
# chat ‹title› (Provider · id)". It lands on an authorization surface, so
# beyond `clean_title` it can neither close the ‹ › it sits in nor draw one
# (every opening, closing and quotation bracket but ASCII ( ) [ ] { }, and
# < > and their look-alikes, become "_"), nor draw the card's " · " parts
# (dot look-alikes become "_"), nor paint a symbol (an emoji, a warning sign
# or a coloured dot standing in for pending or error meaning), nor stack
# combining marks out of its line, nor render blank (Hangul fillers go).
# `hud/src/lib/relays.ts` `cardTitle` applies the same rule again.
CARD_TITLE_CHARS = 60
_CARD_UNDERSCORE = frozenset("<>\uFE64\uFE65\uFF1C\uFF1E"
                             "·•∙⋅・‧･⸱ꞏ\u2022\u00B7")
_CARD_BRACKET_CATEGORIES = frozenset(("Ps", "Pe", "Pi", "Pf"))
_CARD_KEPT_BRACKETS = frozenset("()[]{}")
_CARD_DROPPED = frozenset(("Mn", "Me", "So", "Sk", "Co", "Cn", "Cs"))
_CARD_FILLERS = frozenset("\u3164\u115F\u1160\uFFA0")


def _card_char(ch: str) -> str:
    category = unicodedata.category(ch)
    if category in _CARD_DROPPED or ch in _CARD_FILLERS:
        return ""
    if ch in _CARD_UNDERSCORE or (category in _CARD_BRACKET_CATEGORIES and ch not in _CARD_KEPT_BRACKETS):
        return "_"
    return ch


def card_title(title: Any) -> str:
    """`title` (already one clean, scrubbed line, `chat_relay._title`) for an
    approval card: one plain line of at most `CARD_TITLE_CHARS`, "(untitled)"
    if nothing visible is left. Pure and total; it reads no file."""
    if not isinstance(title, str):
        return "(untitled)"
    text = unicodedata.normalize("NFC", clean_line(title, cap=10 * CARD_TITLE_CHARS))
    text = "".join(_card_char(ch) for ch in text)
    return clean_line(" ".join(text.split()), cap=CARD_TITLE_CHARS) or "(untitled)"


def provider_word(provider: Any) -> str:
    value = getattr(provider, "value", provider)
    return _PROVIDER_WORD.get(str(value), "assistant")


def _thread_id(value: Any) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else "unknown"


# -- eligibility ----------------------------------------------------------------------------


def _projects(stores) -> dict[str, Any]:
    """Every readable, non-archived project by id; a corrupt one is left out."""
    out = {}
    try:
        ids = stores.projects.ids()
    except Exception:  # noqa: BLE001
        return out
    for project_id in ids:
        try:
            project = stores.projects.get(project_id)
        except Exception:  # noqa: BLE001 - one unreadable record never stops a read
            continue
        if project is not None and not project.archived:
            out[project.id] = project
    return out


def _eligible(stores) -> tuple[list[Thread], dict[str, Any]]:
    projects = _projects(stores)
    threads = []
    try:
        ids = stores.threads.ids()
    except Exception:  # noqa: BLE001
        ids = []
    for thread_id in ids:
        try:
            thread = stores.threads.get(thread_id)
        except Exception:  # noqa: BLE001 - corrupt thread.json: not readable, not eligible
            continue
        if (thread is None or thread.role != Role.CHAT or thread.task_id is not None
                or thread.archived or thread.project_id not in projects):
            continue
        threads.append(thread)
    threads.sort(key=lambda t: (str(t.updated or ""), t.id), reverse=True)
    return threads, projects


def eligible(stores) -> list[Thread]:
    """Open HUD chats, most recently updated first: role CHAT, no task, not
    archived, in a readable project that is not archived. Never raises on a
    corrupt record; a trashed chat's directory is gone, so it is not here."""
    return _eligible(stores)[0]


def _project_name(projects: dict, thread: Thread) -> str:
    project = projects.get(thread.project_id)
    return clean_title(project.name if project else "", PROJECT_CHARS)


def source(stores, thread: Thread, projects: dict | None = None) -> str:
    """The fence's provenance: `HUD chat <id> "<title>" (<Provider>, project <name>)`."""
    projects = _projects(stores) if projects is None else projects
    return (f'HUD chat {thread.id} "{clean_title(thread.title)}" '
            f"({provider_word(thread.provider)}, project {_project_name(projects, thread)})")


# -- resolve (§3.4) -------------------------------------------------------------------------


def resolve(stores, text: str, caller_id: str | None, *, purpose: str) -> Thread | Refusal:
    """The one open chat `text` names, by id, title or `Project / Title`.

    Never a guess: several candidates are refused with their ids. An id is
    one candidate among the titles, so a title spelled like another chat's id
    is ambiguous. `purpose="send"` refuses the caller's own chat."""
    if purpose not in ("read", "send"):
        return Refusal(f"Error: unknown purpose {purpose!r}.")
    wanted = text.strip() if isinstance(text, str) else ""
    if not wanted:
        return Refusal("Error: name a chat — its id (chat_list shows ids) or its title.")
    threads, projects = _eligible(stores)
    key = name_key(wanted)
    found: dict[str, Thread] = {}
    for thread in threads:
        if _ID.fullmatch(wanted) and thread.id == wanted:
            found[thread.id] = thread
        if thread.title and key in _title_keys(thread.title):
            found[thread.id] = thread
    start = 0
    while (cut := wanted.find(" / ", start)) != -1:
        left, right = name_key(wanted[:cut]), name_key(wanted[cut + 3:])
        start = cut + 1
        for thread in threads:
            project = projects.get(thread.project_id)
            if (project is not None and thread.title and left in _title_keys(project.name)
                    and right in _title_keys(thread.title)):
                found[thread.id] = thread
    shown = untrusted.one_line(wanted, cap=TITLE_CHARS)
    if not found:
        return Refusal(f'Error: no open HUD chat is called "{shown}" — chat_list shows ids.')
    if len(found) > 1:
        names = ", ".join(f"{t.id} ({_project_name(projects, t)}, {getattr(t.provider, 'value', t.provider)})"
                          for t in found.values())
        return Refusal(f'Error: {len(found)} chats match "{shown}": {names}; use the id.')
    (thread,) = found.values()
    if purpose == "send" and caller_id is not None and thread.id == caller_id:
        return Refusal("Error: that is this chat; a chat cannot send to itself.")
    return thread


def _title_keys(title: str) -> set[str]:
    """What a title is matched on: as stored, and as `chat_list` shows it."""
    return {name_key(title), name_key(clean_line(title, cap=len(title) + 1))}


# -- the log window -------------------------------------------------------------------------


def tail_rows(stores, thread: Thread, *, max_bytes: int = TAIL_BYTES) -> list[dict]:
    """The complete lines in the last `max_bytes` of the thread's `log.jsonl`,
    each parsed alone. A line cut by the window, a malformed line and a
    non-object are skipped. Never raises; a missing log is empty."""
    try:
        path = stores.threads.path(thread.id).with_name("log.jsonl")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except Exception:  # noqa: BLE001 - a bad id, a missing log or a symlink: nothing to read
        return []
    try:
        with os.fdopen(fd, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - max(0, int(max_bytes)))
            if start:
                # One byte before the window says whether it starts a line.
                handle.seek(start - 1)
                data = handle.read(size - start + 1)
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut != -1 else b""
            else:
                data = handle.read(size)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            item = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError):
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


# -- the projection (§3.2) ------------------------------------------------------------------


def _dropped(records: Iterable[dict]) -> set[str]:
    """Message ids a `queued_dropped` record says never ran."""
    out = set()
    for record in records:
        if record.get("kind") == "queued_dropped" and isinstance(record.get("data"), dict):
            message_id = record["data"].get("message_id")
            if isinstance(message_id, str):
                out.add(message_id)
    return out


def _class(record: dict) -> str | None:
    """Which projection row a record makes, without reading its text."""
    kind = record.get("kind")
    if not isinstance(record.get("data"), dict):
        return None
    if kind == "user":
        return "owner"
    if kind == "text":
        return "assistant"
    if kind in _RELAY_KINDS or kind in _SYSTEM_KINDS:
        return kind if kind in _RELAY_KINDS else "system"
    return None


def _owner_row(data: dict, at, dropped: set[str]) -> Row:
    via = data.get("via") if isinstance(data.get("via"), str) else "hud"
    typed = data.get("typed")
    typed = typed if isinstance(typed, str) else ""   # never data["text"]: it has inlined files
    if via == "system":
        # A schedule's prompt or an escape-hatch result: the first line only,
        # because what follows is a command's output.
        lines = typed.strip().splitlines() or [""]
        more = " (the rest is shown only in the HUD)" if len(lines) > 1 else ""
        return Row("system", scrub_read(lines[0]) + more, at, ("not typed by the owner",))
    if isinstance(data.get("skill"), str):
        typed = f"/skill {data['skill']} {typed}".rstrip()
    names = [str(n)[:200] for n in (data.get("attachments") or []) if isinstance(n, str)][:8]
    if names:
        typed += "\n(attached: " + ", ".join(names) + ")"
    images = data.get("images")
    if isinstance(images, int) and not isinstance(images, bool) and images > 0:
        typed += f"\n({images} image{'s' if images != 1 else ''} attached)"
    marks = []
    if via in _VIA_MARK:
        marks.append(_VIA_MARK[via])
    if data.get("spoken"):
        marks.append("spoken")
    if data.get("steer"):
        marks.append("steering")
    if isinstance(data.get("message_id"), str) and data["message_id"] in dropped:
        marks.append("not sent")
    role = "owner"
    if via == "peer":
        role, marks = "another thread, not the owner", []
    return Row(role, scrub_read(typed), at, tuple(marks))


def _relay_row(kind: str, data: dict, at) -> Row:
    title = clean_title(data.get("title"))
    if kind == "relay_in":
        role = f"chat {title} ({_thread_id(data.get('from'))}), not the owner"
    elif kind == "relay_out":
        role = f"sent to {title} ({_thread_id(data.get('to'))})"
    else:
        role = f"reply from {title} ({_thread_id(data.get('from'))})"
    text = data.get("text")
    return Row(role, scrub_read(text if isinstance(text, str) else ""), at)


def _row(record: dict, provider, dropped: set[str]) -> Row | None:
    which = _class(record)
    if which is None:
        return None
    data, at = record["data"], record.get("at") if isinstance(record.get("at"), str) else None
    if which == "owner":
        return _owner_row(data, at, dropped)
    if which == "assistant":
        text = data.get("text")
        return Row(provider_word(provider), scrub_read(text if isinstance(text, str) else ""), at)
    if which == "system":
        text = data.get("text")
        line = untrusted.one_line(text if isinstance(text, str) else "", cap=SNIPPET_CHARS)
        return Row("system", scrub_read(line), at)
    return _relay_row(which, data, at)


def transcript(rows: list[dict], provider) -> list[Row]:
    """Log records (`tail_rows`) as projection rows, oldest first, scrubbed."""
    return [row for _index, row in projected(rows, provider)]


def projected(rows: list[dict], provider) -> list[tuple[int, Row]]:
    """`transcript`, each row with the index of the log record it came from:
    the phone's log cursor (`remote_api`, phone design P-3), so a page can
    resume where the last one stopped. The same rows, the same scrub."""
    dropped = _dropped(rows)
    out = []
    for index, record in enumerate(rows):
        row = _row(record, provider, dropped)
        if row is not None:
            out.append((index, row))
    return out


# -- formatting -------------------------------------------------------------------------------


def _when(at: str | None) -> str:
    if not at:
        return ""
    return untrusted.one_line(at, cap=32).replace("+00:00", "Z")


def _header(row: Row) -> str:
    parts = [row.role, *row.marks]
    if row.at:
        parts.append(_when(row.at))
    return " · ".join(parts)


def _snippet(text: str, cap: int = SNIPPET_CHARS) -> str:
    return _cap(" ".join(text.split()), cap)


def _state(live, thread_id: str, names: dict[str, str]) -> str:
    entry = live.get(thread_id) if isinstance(live, dict) else None
    if not isinstance(entry, dict):
        return "idle"

    def count(name):
        value = entry.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
    waiting = count("queued") + count("relays_waiting")
    if entry.get("running") is True:
        sender = entry.get("relay_from")
        if isinstance(sender, str) and sender:
            name = names.get(sender) or f"another chat ({_thread_id(sender)})"
            state = f"working on a message from {_cap(name, 40)}"
        else:
            state = "working"
    else:
        state = "idle"
    return f"{state} · {waiting} waiting" if waiting else state


def _titles(stores, threads: list[Thread], live) -> dict[str, str]:
    names = {t.id: clean_title(t.title) for t in threads}
    for entry in (live.values() if isinstance(live, dict) else ()):
        sender = entry.get("relay_from") if isinstance(entry, dict) else None
        if isinstance(sender, str) and sender not in names and _ID.fullmatch(sender):
            try:
                found = stores.threads.get(sender)
            except Exception:  # noqa: BLE001
                found = None
            if found is not None:
                names[sender] = clean_title(found.title)
    return names


# -- the reads --------------------------------------------------------------------------------


def listing(stores, live, caller_id: str | None, query: str = "", limit: int = LIST_DEFAULT) -> str:
    """Open HUD chats, most recently active first, one fenced line each:
    id · provider · state · project / title · turns · last active."""
    limit = _bounded(limit, LIST_DEFAULT, LIST_MAX)
    threads, projects = _eligible(stores)
    names = _titles(stores, threads, live)
    key = name_key(query) if isinstance(query, str) else ""
    lines = []
    matched = 0
    for thread in threads:
        title, project = names[thread.id], _project_name(projects, thread)
        if key and not (key in title.casefold() or key in project.casefold() or key == thread.id):
            continue
        matched += 1
        if len(lines) >= limit:
            continue
        mine = " (this chat)" if caller_id is not None and thread.id == caller_id else ""
        turns = thread.turns if isinstance(thread.turns, int) else 0
        row = (f"{thread.id} · {getattr(thread.provider, 'value', thread.provider)} · "
               f"{_state(live, thread.id, names)} · {project} / {title}{mine} · "
               f"{turns} turn{'s' if turns != 1 else ''} · last active {_when(thread.updated)}")
        lines.append(_cap(row, LIST_ROW_CHARS))
    body = "\n".join(lines) if lines else "(no open HUD chat matches)"
    note = f"[{len(lines)} of {matched} open HUD chats shown; give chat_read an id]"
    return untrusted.fence_chat(body, "the list of open HUD chats", notes=[note])


def summary(stores, live, thread: Thread) -> str:
    """A fixed-shape summary, no model call: the chat's header, its first
    owner message, its last rows and counts, within `SUMMARY_CHARS`."""
    threads, projects = _eligible(stores)
    names = _titles(stores, threads, live)
    names.setdefault(thread.id, clean_title(thread.title))
    records = tail_rows(stores, thread)
    dropped = _dropped(records)
    counts: dict[str, int] = {}
    first = None
    for record in records:
        which = _class(record)
        if which is None:
            continue
        counts[which] = counts.get(which, 0) + 1
        if first is None and which == "owner":
            row = _row(record, thread.provider, dropped)
            if row is not None and row.role == "owner":
                first = row
    last: list[Row] = []
    for record in reversed(records):
        if len(last) >= SUMMARY_ROWS:
            break
        row = _row(record, thread.provider, dropped)
        if row is not None:
            last.append(row)
    last.reverse()
    turns = thread.turns if isinstance(thread.turns, int) else 0
    lines = [f"State: {_state(live, thread.id, names)} · {turns} turns · "
             f"last active {_when(thread.updated)}",
             "First owner message: " + (_snippet(first.text) if first else "(none in view)"),
             f"Last {len(last)} rows:"]
    lines += [f"- {_cap(' · '.join([row.role, *row.marks]), 100)}: {_snippet(row.text)}"
              for row in last]
    word = provider_word(thread.provider)
    total = sum(counts.values())
    tally = (f"[{total} rows in view: {counts.get('owner', 0)} owner, {counts.get('assistant', 0)} "
             f"{word}, {counts.get('relay_in', 0)} from other chats, {counts.get('relay_out', 0)} sent, "
             f"{counts.get('relay_reply', 0)} replies, {counts.get('system', 0)} system]")
    return untrusted.fence_chat("\n".join(lines), source(stores, thread, projects),
                                max_chars=SUMMARY_CHARS, notes=[tally])


def _excerpt(text: str, index: int, size: int, cap: int = SNIPPET_CHARS) -> str:
    """About `cap` characters of `text` around a match at `index` (an index
    into `text.casefold()`, mapped back) of `size` casefolded characters."""
    if len(text.casefold()) != len(text):
        folded = 0
        for i, ch in enumerate(text):
            if folded >= index:
                index = i
                break
            folded += len(ch.casefold())
        else:
            index = len(text)
    lead = max(0, index - cap // 3)
    piece = " ".join(text[lead:lead + cap].split())
    if lead:
        piece = "…" + piece
    if lead + cap < len(text):
        piece += "…"
    return _cap(piece, cap)


def _may_match(record: dict, needle: str) -> bool:
    """A cheap first look, before the scrub: could this row's text hold the
    query? Only the fields the row is built from (never a `user` record's
    `text`). It can only drop a row; a hit is still judged on scrubbed text,
    so what it misses is a match the scrub itself made (a removed URL
    userinfo joining two halves), never a credential."""
    data = record["data"]
    parts = [data.get("typed") if record.get("kind") == "user" else data.get("text"),
             data.get("skill"), data.get("title")]
    names = data.get("attachments") if record.get("kind") == "user" else None
    if isinstance(names, list):
        parts += names
    raw = "\n".join(part for part in parts if isinstance(part, str))
    if not raw.isascii():
        raw = untrusted.strip_invisible(raw)
    return needle in raw.casefold()


def search(stores, query: str, *, chat=None, limit: int = SEARCH_DEFAULT,
           clock: Callable[[], float] = time.monotonic) -> str:
    """Rows whose scrubbed text holds `query` (casefold substring), newest
    first, across the `SEARCH_CHATS` most recently updated open chats (or
    only `chat`), within `SEARCH_BUDGET_S` on `clock`."""
    if not isinstance(query, str):
        return "Error: give a query."
    wanted = query.strip()
    if not QUERY_MIN <= len(wanted) <= QUERY_MAX:
        return f"Error: a query is {QUERY_MIN} to {QUERY_MAX} characters."
    limit = _bounded(limit, SEARCH_DEFAULT, SEARCH_MAX)
    needle = wanted.casefold()
    start = clock()
    threads, projects = _eligible(stores)
    if chat is not None:
        threads = [chat]
    threads = threads[:SEARCH_CHATS]
    hits: list[str] = []
    scanned, early = 0, False
    for thread in threads:
        if clock() - start >= SEARCH_BUDGET_S:
            early = True
            break
        records = tail_rows(stores, thread)
        dropped = _dropped(records)
        title = clean_title(thread.title, 40)
        for record in reversed(records):
            if len(hits) >= limit:
                break
            if _class(record) is None or not _may_match(record, needle):
                continue
            if clock() - start >= SEARCH_BUDGET_S:
                early = True
                break
            row = _row(record, thread.provider, dropped)
            if row is None or row.text == WITHHELD:
                continue
            index = row.text.casefold().find(needle)
            if index == -1:
                continue
            hits.append(f"{thread.id} · {title} · {_cap(_header(row), 100)}: "
                        f"{_excerpt(row.text, index, len(needle))}")
        if early:
            break
        scanned += 1
        if len(hits) >= limit:
            break
    shown = untrusted.one_line(wanted, cap=60)
    notes = [f"[{len(hits)} hit{'s' if len(hits) != 1 else ''} in {scanned} of {len(threads)} "
             f"chats scanned; chat_read an id for the whole row]"]
    if early:
        notes.append(f"[search stopped early: the {SEARCH_BUDGET_S:g}-second budget ran out]")
    body = "\n".join(hits) if hits else "(no matches)"
    return untrusted.fence_chat(body, f'a search of open HUD chats for "{shown}"', notes=notes)


def read_tail(stores, thread: Thread, last: int = READ_DEFAULT) -> str:
    """The chat's last `last` rows, each within `ROW_CHARS`, in one fence
    within `READ_TOTAL_CHARS`; the oldest are left out to fit."""
    last = _bounded(last, READ_DEFAULT, READ_MAX)
    where = source(stores, thread)
    records = tail_rows(stores, thread)
    dropped = _dropped(records)
    total = sum(1 for record in records if _class(record) is not None)
    blocks: list[str] = []          # newest first, each already as the fence will hold it
    for record in reversed(records):
        if len(blocks) >= last:
            break
        row = _row(record, thread.provider, dropped)
        if row is None:
            continue
        text = row.text
        if len(text) > ROW_CHARS:
            note = f" … [row cut: {len(text):,} characters]"
            text = text[:ROW_CHARS - len(note)] + note
        blocks.append(untrusted.neutralize_line_markers(untrusted.neutralize_markers(
            untrusted.strip_invisible(f"{_header(row)}\n{text}"))))

    def note(shown: int) -> str:
        cut = "; older ones left out to fit" if shown < len(blocks) else ""
        return f"[Chat {thread.id} · read {_when(utcnow())} · {shown} of {total} messages{cut}]"
    # The fence around an empty body, with the widest note, is the fixed cost.
    room = READ_TOTAL_CHARS - len(untrusted.fence_chat("", where, notes=[note(0) + " " * 12]))
    shown, used = 0, 0
    for block in blocks:
        cost = len(block) + (2 if shown else 0)
        if used + cost > room:
            break
        shown, used = shown + 1, used + cost
    body = "\n\n".join(reversed(blocks[:shown])) or "(no messages)"
    return untrusted.fence_chat(body, where, max_chars=READ_TOTAL_CHARS, notes=[note(shown)])


def _bounded(value: Any, default: int, most: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return default
    return min(value, most)
