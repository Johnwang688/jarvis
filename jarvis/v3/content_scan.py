"""Scan stored agent content. An accident guard, not an adversarial boundary.

`denied-shell` is the gate's *lexical* judge (`rules._judge_segment`): destructive
commands. It is pure, so the refusals that resolve paths or know this machine
(daemon and owner commands, the keyring, `.env` writes) are not repeated here: the
gate refuses those when the command runs, whatever a stored skill says. Nothing
the scanner allows is thereby approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import re
import shlex
from typing import Iterator, Literal
from urllib.parse import urlsplit

from jarvis import credential_patterns, rules, untrusted
from jarvis.protected_state import _copier_split, _strip_comment
from jarvis.v2 import redact

Purpose = Literal["skill", "memory", "brief"]
Severity = Literal["block", "flag"]
Span = tuple[int, int]

# The document cap is in Unicode code points, as are all returned offsets.
# The shared shell judge also gets a smaller per-command cap for its parsing.
_MAX_TEXT = 64 * 1024
_MAX_COMMAND = 2048
_MAX_COMMANDS = 128
_MAX_LABEL_VALUE = 2048

# Established code and package hosts. Match parsed hostnames exactly.
_CODE_HOSTS = frozenset({
    "github.com", "api.github.com", "raw.githubusercontent.com", "pypi.org",
    "files.pythonhosted.org", "registry.npmjs.org",
})

_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})([^\r\n]*)")
_SHELL = frozenset({"", "sh", "bash", "zsh", "ksh", "dash", "ash", "fish", "shell"})
_CONSOLE = frozenset({
    "console", "terminal", "shell-session", "sh-session", "bash-session", "shellsession",
})
_SOURCE = frozenset({"python", "py", "python3", "javascript", "js", "typescript", "ts", "node"})
_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|forget)[ \t]+(?:(?:all|your|the)[ \t]+){0,3}"
    r"(?:previous|prior|earlier|above|system)[ \t]+(?:instructions|prompts?|rules)\b"
    r"|\b(?:your[ \t]+)?new[ \t]+system[ \t]+prompt\b"
    r"|\byou[ \t]+must[ \t]+now\b"
    r"|\b(?:do[ \t]+not|don't|never)[ \t]+(?:tell|inform|notify)[ \t]+the[ \t]+user\b"
    r"|</?(?:system(?:-reminder)?|developer|tool[_-]call)\b[ \t]*/?>"
    r"|<\|(?:im_start|im_end|system|developer|assistant|tool)\|>"
    r'|"(?:type"[ \t\r\n]*:[ \t\r\n]*"tool_use"|(?:tool_calls|function_call)"[ \t\r\n]*:[ \t\r\n]*[\[{])',
    re.IGNORECASE,
)
_IMPERATIVE = re.compile(
    r"(?:\A|[.!?\n])[ \t]*(?:[-*][ \t]+)?"
    r"(always\b|never\b|do[ \t]+not\b|you[ \t]+(?:should|must|need[ \t]+to)\b)",
    re.IGNORECASE,
)
_URL = re.compile(r"""https?://[^\s<>"'`]+""", re.IGNORECASE)
_URL_OR_BLOB = re.compile(
    rf"{_URL.pattern}|(?P<blob>(?-i:(?<![A-Za-z0-9+/])[A-Za-z0-9+/]+={{0,2}}))",
    re.IGNORECASE,
)
_IMAGE = re.compile(rf"!\[[^\[\]\n]*\]\(<?({_URL.pattern})", re.IGNORECASE)
_EXPANSION = re.compile(r"\$(?:[\w{(*@#?!$-])|`")
_CODE_SEND = re.compile(r"\b(?:(?:requests|httpx|axios)\.(?:post|put|patch|get)|fetch)\s*\(")
_CODE_DATA = re.compile(
    r"\b(?:open|readFile(?:Sync)?|getenv)\s*\(|\b(?:os\.environ|process\.env)\b"
)
_PY_WRITE = re.compile(
    r"""\bopen\([ \t]*(?:os\.path\.expanduser\([ \t]*)?(['"])([^'"\n]+)\1[ \t]*\)?"""
    r"""[ \t]*,[ \t]*(?:mode[ \t]*=[ \t]*)?['"](?:[wax]|r\+)"""
    r"""|\bPath\([ \t]*(['"])([^'"\n]+)\3[ \t]*\)\.(?:write_text|write_bytes|touch|unlink)\b"""
)
_PERSIST_PATH = re.compile(
    r"(?:^|/)(?:\.(?:bashrc|bash_profile|bash_login|profile|zshrc|zprofile|zshenv|zlogin|kshrc)"
    r"|systemd/(?:user|system)(?:/.*)?|\.git/hooks(?:/.*)?|\.(?:claude|codex)(?:/.*)?"
    r"|cron\.d(?:/.*)?|crontab|rc\.local|fish/config\.fish)/?\Z"
)


@dataclass(frozen=True)
class Hit:
    rule: str
    span: Span
    severity: Severity


@dataclass(frozen=True)
class Verdict:
    hits: tuple[Hit, ...]

    @property
    def ok(self) -> bool:
        return all(hit.severity != "block" for hit in self.hits)

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "hits": [
                {"rule": hit.rule, "span": list(hit.span), "severity": hit.severity}
                for hit in self.hits
            ],
        }


def _invisible_spans(text: str) -> Iterator[Span]:
    start = int(text.startswith("\ufeff"))
    visible = untrusted.strip_invisible(text[start:])
    if len(visible) == len(text) - start:
        return
    next_visible = iter(visible)
    wanted = next(next_visible, None)
    removed = None
    for i in range(start, len(text)):
        if text[i] == wanted:
            if removed is not None:
                yield removed, i
                removed = None
            wanted = next(next_visible, None)
        elif removed is None:
            removed = i
    if removed is not None:
        yield removed, len(text)


def _credential_hits(text: str) -> Iterator[Hit]:
    spans = [(a, b) for a, b, _ in redact.spans(text, mode=redact.STRICT)]
    spans += [match.span() for _, pattern in credential_patterns.PATTERNS
              for match in pattern.finditer(text)]
    for match in redact.LABELLED.finditer(text):
        # The shared trimmer can revisit a candidate with interleaved
        # punctuation and escapes. Bound its work for each candidate.
        if len(match[2]) > _MAX_LABEL_VALUE:
            yield Hit("unscannable", match.span(2), "block")
            continue
        for a, b, _ in redact.labelled_spans(match[0]):
            spans.append((match.start() + a, match.start() + b))
    end = 0
    start = 0
    for a, b in sorted(spans):
        if a >= end:
            if end:
                yield Hit("credential", (start, end), "block")
            start = a
        end = max(end, b)
    if end:
        yield Hit("credential", (start, end), "block")


def _allowed_host(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if "\\" in parsed.netloc:
            return False
        host = parsed.hostname
        if host in _CODE_HOSTS or host == "localhost":
            return True
        return host is not None and ip_address(host).is_loopback
    except ValueError:
        return False


def _embedded_exfiltration(text: str) -> Iterator[Span]:
    for match in _URL_OR_BLOB.finditer(text):
        if (match.lastgroup == "blob" and match.end() - match.start() > 1024
                and len(match[0].rstrip("=")) % 4 != 1):
            yield match.span()
    for match in _IMAGE.finditer(text):
        url = match[1].rstrip(")")
        if _EXPANSION.search(url) and not _allowed_host(url):
            yield match.start(), match.start(1) + len(url)


def _network_write(stem: str, words: list[str], piped: bool, redirects: list[rules.Redirect]) -> bool:
    if stem in {"nc", "ncat", "netcat"}:
        peers = [word for word in words if not word.startswith("-")]
        if peers and (piped or any(r.op == "<" for r in redirects)):
            host = peers[0]
            try:
                if ip_address(host.strip("[]")).is_loopback:
                    return False
            except ValueError:
                pass
            return not _allowed_host("//" + host)
        return False
    if stem not in {"curl", "wget", "fetch"}:
        return False
    sends_file = piped or any(r.op == "<" for r in redirects) or any(
        word.startswith(("-T", "--upload-file", "--post-file", "--body-file"))
        or word.startswith(("@", "-d@"))
        or (word.startswith(("--data", "--form", "-F")) and "@" in word)
        or "=@" in word or "=<" in word
        for word in words
    )
    expanded = sends_file or any(
        not isinstance(word, rules._Word) or word.active[match.start()]
        for word in words for match in _EXPANSION.finditer(word)
    )
    return expanded and any(
        not _allowed_host(match[0]) for word in words for match in _URL.finditer(word)
    )


def _persistent_write(stem: str, words: list[str], redirects: list[rules.Redirect]) -> bool:
    if any(r.writes and _PERSIST_PATH.search(r.word.strip("'\"")) for r in redirects):
        return True
    if stem == "crontab":
        return bool(words) and "-l" not in words
    if stem == "systemctl":
        verb = next((word for word in words if not word.startswith("-")), "")
        return verb in {"enable", "disable", "reenable", "mask", "unmask", "edit", "link", "preset"}
    copier = _copier_split(stem, words)
    if copier is not None:
        targets, _, _ = copier
    elif stem in {"mv", "ln"}:
        targets = words[-1:]
    elif stem in {"tee", "touch", "mkdir", "rm", "chmod", "chown", "truncate"}:
        targets = words
    else:
        hazard = rules.reader_hazard([stem, *words])
        targets = (words if hazard.operands else []) + list(hazard.writes) if hazard else []
    return any(_PERSIST_PATH.search(word) for word in targets)


def _denied_segment(segment: str) -> bool:
    # The top-level judge can resolve paths in its env-file and here-document
    # checks. At this depth the segment judge performs its lexical DENYs but
    # cannot reenter that path through a runner. The caller walks runner
    # strings and argv using the shared text parser instead.
    with rules.work_scope(fresh=True):
        return rules._judge_segment(segment, depth=rules.PROGRAM_DEPTH).decision == rules.DENY


def _shell_hits(command: str, budget: list[int]) -> set[str]:
    queue = [(command, 0)]
    seen = set()
    found = set()
    for line, depth in queue:
        if line in seen:
            continue
        seen.add(line)
        if depth > rules.PROGRAM_DEPTH or len(line) > _MAX_COMMAND:
            found.add("unscannable")
            break
        line = "\n".join(map(_strip_comment, rules.join_continuations(line).split("\n")))
        parts = rules._segment_parts(line)
        if parts is None:
            parts = [(segment, "") for segment in rules.segments(line)]
        piped = False
        for segment, separator in parts:
            if not segment:
                continue
            if not budget[0]:
                return found | {"unscannable"}
            budget[0] -= 1
            redirects = rules.redirections(segment)
            words = rules.command_start(rules._shell_words(rules.strip_redirections(segment)))
            if _denied_segment(segment):
                found.add("denied-shell")
            if words:
                stem = rules._basename(words[0])
                args = words[1:]
                if _network_write(stem, args, piped, redirects):
                    found.add("exfiltration")
                if _persistent_write(stem, args, redirects):
                    found.add("persistence")
                try:
                    started = rules.starts(words)
                except rules.TooComplex:
                    found.add("unscannable")
                    started = None
                if started is not None:
                    queue.extend((inner, depth + 1) for inner in started.strings)
                    render = " ".join if started.joined else shlex.join
                    queue.extend((render(argv), depth + 1) for argv in started.programs)
                elif stem in rules._SHELL_STEMS:
                    strings, _ = rules._shell_command(words)
                    queue.extend((inner, depth + 1) for inner in strings)
            elif _persistent_write("", [], redirects):
                found.add("persistence")
            piped = separator == "|"
    return found


def _source_hits(line: str) -> Iterator[str]:
    if line.lstrip().startswith(("#", "//")):
        return
    if _CODE_SEND.search(line) and (_CODE_DATA.search(line) or _EXPANSION.search(line)):
        if any(not _allowed_host(match[0]) for match in _URL.finditer(line)):
            yield "exfiltration"
    if any(_PERSIST_PATH.search(m[2] or m[4]) for m in _PY_WRITE.finditer(line)):
        yield "persistence"


def _fenced_hits(text: str) -> Iterator[Hit]:
    fence = None
    language = None
    pending = []
    offset = start = size = 0
    budget = [_MAX_COMMANDS]
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        end = offset + len(raw)
        line_end = offset + len(line)
        match = _FENCE.fullmatch(line)
        closing = (match and fence and match[1][0] == fence[0]
                   and len(match[1]) >= len(fence) and not match[2].strip())
        if pending and closing:
            yield Hit("unscannable", (start, offset), "block")
            pending.clear()
            size = 0
        if closing:
            fence = language = None
        elif fence is None and match and (match[1][0] != "`" or "`" not in match[2]):
            fence = match[1]
            words = match[2].lower().split()
            language = words[0] if words else ""
        elif language in _SOURCE:
            for rule in _source_hits(line):
                yield Hit(rule, (offset, line_end), "block")
        elif language in _SHELL or language in _CONSOLE:
            line = line.lstrip()
            if language in _CONSOLE:
                if line.startswith(("$ ", "# ")) or (pending and line.startswith("> ")):
                    line = line[2:]
                elif not pending:
                    offset = end
                    continue
            if line and (pending or not line.startswith("#")):
                if not pending:
                    start = line_end - len(line)
                pending.append(line)
                size += len(line) + 1
                if size > _MAX_COMMAND + 1:
                    yield Hit("unscannable", (start, end), "block")
                    return
                if (len(line) - len(line.rstrip("\\"))) % 2 == 0:
                    span = start, line_end
                    for rule in sorted(_shell_hits("\n".join(pending), budget)):
                        yield Hit(rule, span, "block")
                        if rule == "unscannable" and not budget[0]:
                            return
                    pending.clear()
                    size = 0
        offset = end
    if pending:
        yield Hit("unscannable", (start, offset), "block")


def scan(text: str, *, purpose: Purpose) -> Verdict:
    """Return only named rules, severities and source character offsets."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if purpose not in ("skill", "memory", "brief"):
        raise ValueError("unknown scan purpose")
    if len(text) > _MAX_TEXT:
        return Verdict((Hit("unscannable", (_MAX_TEXT, len(text)), "block"),))

    hits = [Hit("invisible-unicode", span, "block") for span in _invisible_spans(text)]
    hits.extend(_credential_hits(text))
    hits.extend(Hit("agent-override", m.span(), "block") for m in _OVERRIDE.finditer(text))
    hits.extend(Hit("exfiltration", span, "block") for span in _embedded_exfiltration(text))
    hits.extend(_fenced_hits(text))
    if purpose == "memory":
        hits.extend(Hit("imperative", m.span(1), "flag") for m in _IMPERATIVE.finditer(text))
    return Verdict(tuple(sorted(hits, key=lambda hit: (hit.span, hit.rule))))
