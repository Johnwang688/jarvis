"""Read and edit the commitments table in the vault.

I2 owns writing SIGNALS_DIR/commitments.json and reads through load().
Scheduling is outside this module.
Drafting replies or reminders is outside this module.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from hashlib import sha256
from ipaddress import IPv6Address
import re
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt

from jarvis import untrusted


BEGIN = "<!-- commitments:begin -->"
END = "<!-- commitments:end -->"
COLUMNS = ("id", "who", "what", "due", "direction", "source", "state")
DIRECTIONS = ("owner-owes", "owed-to-owner")
STATES = ("open", "done", "dropped")
_CLOSED = STATES[1:]
_ID = re.compile(r"c-[0-9a-f]{8}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CELL_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
# Bracketed text can resolve to a link or footnote elsewhere in the document.
# The HUD preview also recognizes dollar-delimited math.
_EXTRA_MARKUP = re.compile(r"<!--|\[[^\]]*\]|\$\$.*?\$\$|\$(?![\s$]).*?(?<![\s$])\$(?![\d$])")
_IPV6_URL = re.compile(r"(\b[a-z][a-z0-9+.-]*://)\[([0-9a-f:.]+)\]", re.I)
_MARKDOWN = MarkdownIt("commonmark").enable("strikethrough")
_MARKER = re.compile(rf"^({BEGIN}|{END})[ \t\r]*(?:\n|\Z)", re.MULTILINE)
_SEPARATOR = re.compile(r":?-{3,}:?")


class CommitmentError(ValueError):
    """A malformed block or invalid commitment field."""


@dataclass(frozen=True)
class Commitment:
    id: str
    who: str
    what: str
    due: date | None
    direction: str
    source: str
    state: str


def _cell_input(value):
    if not isinstance(value, str):
        return None
    value = value.strip(" \t")
    return None if _CELL_CONTROL.search(value) else value


def _without_ipv6_brackets(match):
    try:
        IPv6Address(match[2])
    except ValueError:
        return match[0]
    return match[1] + match[2]


def _field(value, name, cap):
    raw = _cell_input(value) or ""
    value = raw.strip()
    if not value or len(value) > cap or "|" in raw:
        raise CommitmentError(f"{name} must be a single table cell of 1 to {cap} characters.")
    tokens = _MARKDOWN.parseInline(value)[0].children
    # Host brackets delimit a literal IPv6 address. Markdown still sees the original cell.
    bracket_probe = _IPV6_URL.sub(_without_ipv6_brackets, value)
    if (_EXTRA_MARKUP.search(bracket_probe) or untrusted.strip_invisible(raw) != raw
            or any(token.type != "text" for token in tokens)
            or "".join(token.content for token in tokens) != value):
        raise CommitmentError(f"{name} must be plain text without markup or hidden text.")
    return value


def validate_source(source):
    return _field(source, "Source", 200)


def _choice(value, choices, name):
    value = (_cell_input(value) or "").strip()
    if value not in choices:
        raise CommitmentError(f"Invalid commitment {name}.")
    return value


def validate_close(id, state):
    id = (_cell_input(id) or "").strip()
    if not _ID.fullmatch(id):
        raise CommitmentError("Invalid commitment id.")
    return id, _choice(state, _CLOSED, "closing state")


def _due(value):
    value = _cell_input(value)
    if value is not None:
        value = value.strip()
        if value in ("", "-"):
            return None
        if _DATE.fullmatch(value):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
    raise CommitmentError("Due date must be a valid YYYY-MM-DD date or empty.")


def validate_upsert(who, what, due, direction, source):
    return (_field(who, "Who", 80), _field(what, "What", 200), _due(due),
            _choice(direction, DIRECTIONS, "direction"), validate_source(source))


def _key(who, what, due):
    return " ".join(who.split()).casefold(), " ".join(what.split()).casefold(), due


def commitment_id(who: str, what: str, due: str) -> str:
    return "c-" + sha256("\x1f".join(_key(who, what, due)).encode()).hexdigest()[:8]


def _line(cells):
    return "| " + " | ".join(cells) + " |"


def render(rows: Iterable[Commitment], newline: str = "\n") -> str:
    lines = [BEGIN, _line(COLUMNS), _line(["---"] * len(COLUMNS))]
    for row in rows:
        lines.append(_line((row.id, row.who, row.what, row.due.isoformat() if row.due else "-",
                            row.direction, row.source, row.state)))
    return newline.join([*lines, END, ""])


def _markers(text):
    markers = iter(_MARKER.finditer(text))
    begin = next(markers, None)
    if begin is None:
        return None
    end = next(markers, None)
    if begin[1] != BEGIN or end is None or end[1] != END or next(markers, None):
        raise CommitmentError("Expected one paired commitments block.")
    return begin, end


def _cells(line):
    cells = tuple(cell.strip(" \t") for cell in line.split("|"))
    if len(cells) != len(COLUMNS) + 2 or cells[0] or cells[-1] or "\\|" in line:
        raise CommitmentError("Expected seven table cells.")
    if any(_CELL_CONTROL.search(cell) for cell in cells[1:-1]):
        raise CommitmentError("Commitment cells cannot contain line breaks or control characters.")
    return cells[1:-1]


def parse(text: str) -> list[Commitment]:
    markers = _markers(text)
    if markers is None:
        return []
    begin, end = markers
    first_line = text.count("\n", 0, begin.end()) + 1
    block = text[begin.end():end.start()].replace("\r\n", "\n")
    lines = iter((number, line) for number, line in
                 enumerate(block.split("\n"), first_line)
                 if line.strip(" \t"))
    header = next(lines, None)
    separator = next(lines, None)
    if header is None or tuple(cell.casefold() for cell in _cells(header[1])) != COLUMNS:
        raise CommitmentError("Invalid commitments table header.")
    if separator is None or not all(_SEPARATOR.fullmatch(cell) for cell in _cells(separator[1])):
        raise CommitmentError("Invalid commitments table separator.")
    rows = []
    ids = set()
    for number, line in lines:
        try:
            id, who, what, due, direction, source, state = _cells(line)
            if not _ID.fullmatch(id) or id in ids:
                raise CommitmentError("Invalid or duplicate commitment id.")
            fields = validate_upsert(who, what, due, direction, source)
            rows.append(Commitment(id, *fields, _choice(state, STATES, "state")))
            ids.add(id)
        except CommitmentError as exc:
            raise CommitmentError(f"Commitments line {number}: {exc}") from None
    return rows


def block_text(text: str) -> str | None:
    """Return the original block after validating it, or None when absent."""
    parse(text)
    markers = _markers(text)
    if markers is None:
        return None
    begin, end = markers
    return text[begin.start():end.end()]


def replace_block(text: str, rows: Iterable[Commitment]) -> str:
    markers = _markers(text)
    if markers is None:
        separator = "\n" if text and not text.endswith("\n") else ""
        return text + separator + render(rows)
    begin, end = markers
    newline = "\r\n" if begin[0].endswith("\r\n") else "\n"
    block = render(rows, newline)
    if not end[0].endswith("\n"):
        block = block.removesuffix(newline)
    return text[:begin.start()] + block + text[end.end():]


def upsert(text: str, *, who: str, what: str, due: str, direction: str,
           source: str) -> tuple[str, Commitment, bool]:
    who, what, due, direction, source = validate_upsert(who, what, due, direction, source)
    rows = parse(text)
    key = _key(who, what, due)
    for row in rows:
        if _key(row.who, row.what, row.due) == key:
            return text, row, False
    id = commitment_id(who, what, due.isoformat() if due else "")
    if any(row.id == id for row in rows):
        raise CommitmentError("Commitment id is already used by a different row.")
    row = Commitment(id, who, what, due, direction, source, "open")
    return replace_block(text, [*rows, row]), row, True


def close(text: str, id: str, state: str) -> tuple[str, Commitment]:
    id, state = validate_close(id, state)
    rows = parse(text)
    for position, row in enumerate(rows):
        if row.id == id:
            if row.state == state:
                return text, row
            rows[position] = replace(row, state=state)
            return replace_block(text, rows), rows[position]
    raise CommitmentError("Commitment id was not found.")


def due_within(rows: Iterable[Commitment], *, days: int, now: float) -> list[Commitment]:
    today = datetime.fromtimestamp(now, ZoneInfo("America/Chicago")).date()
    last_day = today + timedelta(days=days)
    return sorted((row for row in rows if row.state == "open" and row.due is not None
                   and row.due <= last_day), key=lambda row: (row.due, row.id))


def load(vault=None) -> list[Commitment]:
    if vault is None:
        from .vault import Vault

        vault = Vault()
    return parse(vault.read("commitments").body)
