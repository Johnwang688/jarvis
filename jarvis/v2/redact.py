"""One scrub for model-written text bound for a surface (SV-2 review, 2026-10-15;
hardened 2026-10-25, "Scrubbing: stronger shared credential shapes").

Two entry points, because two kinds of reader get this text:

- **`for_model(text)`** (alias `scrub_text`) — text an agent will read: a chat
  relay's body and reply, a chat git tool's output, a sub-agent's words, a
  Codex compaction reason, diff review's input. It withholds Jarvis's own
  credential values (`secrets.scrub`) and every **high-confidence credential
  shape** (`shapes`), and nothing else: a model must be able to read ordinary
  code as written, so no heuristic about what follows a label runs here
  (review of PR #308: `api_key=settings.API_KEY` must reach a model intact).
- **`for_display(text)`** — text the owner reads (the desk's Diff tab, the
  transcript through `chat_reads.scrub`, the Discord mirror and reporter):
  `for_model`, then `labelled_values`, the phone's tuned rule for a value
  written after a credential's label (`password: …`, `client_secret: …`).

`shapes` is one table (`SHAPES`) plus a few rules that need more than a
regular expression: a private key's whole block (`private_key_spans`, SEC-5),
a `Bearer`/`Basic` credential, a URL's password, an AWS secret key beside or
labelled as one. Every withheld span becomes `[withheld: <kind>]`, so the
reader knows something was there and what kind of thing it was.

Matching runs on **normalized views** of the text, never on the text shown
(`_View`): fullwidth forms folded to ASCII; zero-width, bidi, soft-hyphen,
control, private-use and combining characters dropped (and, in a second
view, read as a space); unusual spaces (NBSP, U+2028, U+0085, the Unicode
spaces) read as a space, and in a third view dropped, so a token split by
one is whole again; and a prefixed token split across an ordinary space,
tab or newline is joined when what follows the break looks random
(`_split_spans`). A span found in a view is mapped back and withheld in the
original; everything else comes back byte for byte (`for_model`'s output
differs from its input only inside withheld spans and where `secrets.scrub`
acts).

An approval card is a third reader (`card_spans`, `CardPass`): the display
scrub's spans, found on the text as typed and held through
`secrets.redact_command`, within the owner's condition that a withheld span
never hides code that runs.

`bearerish`, `TOKENISH`, `JWTISH`, `JMCPISH` and `private_key_blocks` stay
for their existing callers (`claude._safe`, `secrets._key_blocks`).
Pure functions of their input apart from `secrets.scrub`'s file reads, so
call them outside any lock that matters.
"""
from __future__ import annotations

import base64
import binascii
import bisect
from functools import lru_cache
import re
import unicodedata

from jarvis import credential_patterns

TOKENISH = re.compile(r"\b(?:sk|oat|rt)[-_][A-Za-z0-9_\-]{12,}", re.IGNORECASE)
JWTISH = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+")
# A jarvis-mcp caller token (`callers.TOKEN_PREFIX`, plan 2026-10-10 §1.1).
JMCPISH = re.compile(r"jmcp_[A-Za-z0-9_\-]{8,}")
REDACTED = "[redacted]"
WITHHELD_PREFIX = "[withheld: "


def withheld(kind: str) -> str:
    """The marker a withheld span becomes."""
    return f"{WITHHELD_PREFIX}{kind}]"


def bearerish(text: str) -> str:
    """Bearer-shaped tokens, JWTs and jarvis-mcp caller tokens redacted."""
    text = TOKENISH.sub(REDACTED, text)
    text = JMCPISH.sub(REDACTED, text)
    return JWTISH.sub(REDACTED, text)


# A private key's header and footer, exactly as `credential_patterns` knows
# them (PEM, OpenSSH, PGP; plain, RSA/EC/DSA, ENCRYPTED …). Shared, not copied,
# so a format added there is a block here too.
_SHAPES = dict(credential_patterns.PATTERNS)
KEY_BEGIN = _SHAPES["private-key"]
KEY_END = _SHAPES["private-key-end"]

# The characters any shape in `credential_patterns` or above is made of, and
# base64's. A run of them glued to a header (no space or line break between)
# goes with the block: the per-pattern pass used to read such a run on into
# the header (`sk-or-` + 15 characters + `-----BEGIN` is 25), so cutting the
# block at the dashes would show what was hidden before.
_GLUED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.+/=")

# A line of key material, for a footer with no header above it: one run of
# base64 (a PEM, OpenSSH or PGP body line, padded or not), a PGP checksum
# (`=` + 4), or a PEM encapsulated header (`Proc-Type:`, `DEK-Info:`), with
# spaces or tabs around it and a CR at the end allowed. Deliberately narrow:
# a blank line or any line with a space inside it (words, a shell prompt, a
# certificate's footer) is not one. A bare word ("thanks") is base64 too, so
# one directly above a footer goes with it; that is the cost of the rule.
# (The header branch has no trailing `[ \t]*`: two quantifiers over the same
# spaces would backtrack quadratically on a long line.)
_KEY_LINE = re.compile(
    r"[ \t]*(?:(?:[A-Za-z0-9+/]+={0,2}|=[A-Za-z0-9+/]{4})[ \t]*|(?:Proc-Type|DEK-Info):[^\r\n]*)\r?")


def _line_start(text: str, pos: int, floor: int) -> int:
    """Where the line holding `pos` starts, never before `floor`."""
    i = text.rfind("\n", floor, pos)
    return floor if i < 0 else i + 1


def private_key_spans(text: str) -> list[tuple[int, int]]:
    """The spans `private_key_blocks` withholds, in order, never overlapping:

    1. Each header through the nearest private-key footer after it (of any
       kind: a mismatched label still ends the block), or through the end of
       the text when no footer follows, with any run of `_GLUED` characters
       stuck to the header's front. Non-greedy, so two blocks stay two.
    2. A footer left with no header (a read that starts inside a key) and the
       key material above it. The rule, conservative on purpose: starting at
       the footer's own line (whose text before the footer must be empty or
       key material, else nothing is withheld here), walk up one line at a
       time while each line is `_KEY_LINE`, and stop at the first that is
       not, at the start of the text, at the previous footer or at a block of
       rule 1. A footer with no key material above it is left to the shape
       pass, which withholds just the footer.

    Linear: every search starts where the last block ended, and no walk
    crosses the previous footer's end."""
    blocks, pos = [], 0
    while (head := KEY_BEGIN.search(text, pos)) is not None:
        start = head.start()
        while start > pos and text[start - 1] in _GLUED:
            start -= 1
        foot = KEY_END.search(text, head.end())
        if foot is None:
            blocks.append((start, len(text)))
            pos = len(text)
            break
        blocks.append((start, foot.end()))
        pos = foot.end()
    if not blocks and KEY_END.search(text) is None:
        return []
    tails, floor, b = [], 0, 0
    for foot in KEY_END.finditer(text):
        while b < len(blocks) and blocks[b][1] <= foot.start():
            floor = max(floor, blocks[b][1])
            b += 1
        if b < len(blocks) and blocks[b][0] <= foot.start() < blocks[b][1]:
            continue                                # this block's own footer
        start = foot.start()
        first = _line_start(text, start, floor)
        if first == start or _KEY_LINE.fullmatch(text, first, start):
            while first > floor:
                above = _line_start(text, first - 1, floor)
                if not _KEY_LINE.fullmatch(text, above, first - 1):
                    break
                first = above
            if first < start:
                tails.append((first, foot.end()))
        floor = foot.end()
    return sorted(blocks + tails)


def _apply(text: str, spans, marker) -> str:
    """`text` with each (start, end[, kind]) span replaced by `marker` (a
    string, or a function of the kind). Spans must be sorted and disjoint."""
    out, pos = [], 0
    for span in spans:
        a, b = span[0], span[1]
        out += [text[pos:a], marker(span[2]) if callable(marker) else marker]
        pos = b
    out.append(text[pos:])
    return "".join(out)


def private_key_blocks(text: str, marker: str = REDACTED) -> str:
    """`text` with every private key's whole block (header, body, footer) as
    one `marker`: a terminated block, an unterminated one (to the end), and a
    footer-only tail (see `private_key_spans`). Text with no private-key
    header or footer comes back unchanged."""
    spans = private_key_spans(text)
    return _apply(text, spans, marker) if spans else text


# -- the shapes -----------------------------------------------------------------------

# A token must not continue an identifier on either side (as in
# `credential_patterns`).
_B = r"(?<![A-Za-z0-9_\-])"
_E = r"(?![A-Za-z0-9_\-])"

KEY = "private key"

# (kind, pattern). Each is a prefix or framing nobody types by accident; the
# order is the order a kind is named when two spans overlap (the earlier
# start wins, then the longer span). `credential_patterns.PATTERNS` (the
# terminal read's refusal list) is a subset in spirit; it is SELF_PROTECTED
# and decides refusals, so the display and model scrub keep their own table.
SHAPES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (kind, re.compile(pattern)) for kind, pattern in (
        ("Anthropic key", _B + r"sk-ant-[A-Za-z0-9_\-]{20,}"),
        ("OpenRouter key", _B + r"sk-or-[A-Za-z0-9_\-]{20,}"),
        ("OpenAI key", _B + r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}"),
        ("API key", _B + r"sk-(?=[A-Za-z0-9_\-]*[0-9])[A-Za-z0-9_\-]{20,}"),
        ("GitHub token", _B + r"gh[pousr]_[A-Za-z0-9]{36,}" + _E),
        ("GitHub token", _B + r"github_pat_[A-Za-z0-9_]{22,}"),
        ("AWS access key id", r"(?<![A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Z0-9])"),
        ("AWS session token", r"(?<![A-Za-z0-9/+=])(?:IQoJb3JpZ2lu|FwoGZXIvYXdz|FQoGZXIvYXdz)"
                              r"[A-Za-z0-9/+=]{20,}"),
        ("Slack token", _B + r"xox[abeprs]-[A-Za-z0-9\-]{10,}"),
        ("Slack token", _B + r"xapp-[0-9]-[A-Za-z0-9\-]{10,}"),
        ("Google API key", _B + r"AIza[0-9A-Za-z_\-]{35}" + _E),
        ("Stripe key", _B + r"(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{20,}"),
        ("JWT", _B + r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
        ("Hugging Face token", _B + r"hf_[A-Za-z0-9]{30,}" + _E),
        ("npm token", _B + r"npm_[A-Za-z0-9]{36}" + _E),
        ("GitLab token", _B + r"glpat-[A-Za-z0-9_\-]{20,}"),
        ("PyPI token", _B + r"pypi-AgE[A-Za-z0-9_\-]{50,}"),
        ("Discord bot token", _B + r"[MNO][A-Za-z0-9_\-]{23,27}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}"),
        ("Jarvis caller token", r"jmcp_[A-Za-z0-9_\-]{8,}"),
        # Claude's provider's own bearer shapes (`claude._safe`): an OAuth or
        # API bearer (`sk-`/`oat`/`rt` + 12), whatever the case.
        ("token", r"(?i)\b(?:sk|oat|rt)[-_][A-Za-z0-9_\-]{12,}"),
        # A private key's header or footer on its own (the block rule takes
        # the whole block first; this is what is left of a stray one).
        (KEY, r"-----(?:BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    )
)
# The kinds whose pattern starts with a fixed prefix: a token split across a
# break is joined and tried against these (`_split_spans`).
_PREFIXED = tuple((kind, p) for kind, p in SHAPES if kind not in ("AWS access key id", KEY, "Discord bot token"))
_SPLIT_PREFIX = re.compile(
    _B + r"(?:sk-|gh[pousr]_|github_pat_|xox[abeprs]-|xapp-|AIza|(?:sk|rk)_(?:live|test)_|eyJ|hf_|npm_"
    r"|glpat-|pypi-|jmcp_|IQoJb3JpZ2lu|FwoGZXIvYXdz|FQoGZXIvYXdz)"
    r"[A-Za-z0-9_\-./+=]*(?:(?:\r?\n|[ \t])[A-Za-z0-9_\-./+=]+){1,8}")
_SPLIT_BREAK = re.compile(r"(\r?\n|[ \t])")
_DIGIT = re.compile(r"[0-9]")

# `Authorization: Bearer <token>` (and any `Bearer <token>` in prose or a
# header dump): the scheme word stays, the token goes. A token needs 16
# characters with a letter and a digit, so `Bearer authentication` and
# `"Bearer " + token` stay.
_BEARER = re.compile(r"(?i)(?<![A-Za-z0-9_])bearer[ \t]+([A-Za-z0-9\-._~+/]{16,}=*)(?![A-Za-z0-9\-._~+/=])")
# `Basic <base64 of user:password>`: only when it decodes to printable text
# holding a colon.
_BASIC = re.compile(r"(?i)(?<![A-Za-z0-9_])basic[ \t]+([A-Za-z0-9+/]{8,}={0,2})(?![A-Za-z0-9+/=])")
# A URL's userinfo (`scheme://user:password@host`): the password goes, the
# user stays. A template or placeholder stays (`${DB_PASSWORD}`, `<pw>`,
# `%(password)s`, `****`, `password`).
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}\Z")
_USERINFO = re.compile(r"([^\s:/?#@'\"<>`\[\]{}]*):([^\s/?#@'\"<>`]+)@")
_TOKEN_USERINFO = re.compile(r"([A-Za-z0-9_\-.~]{20,})@")
_PLACEHOLDER = re.compile(r"(?i)[$%{<*]|\A(?:x+|\*+|•+|pass(?:word|wd)?|pwd?|secret|token|changeme)\Z")
# AWS: a secret access key is 40 characters of base64 with both cases. It
# goes when a label names it, or when an access key id is near it.
# (Not preceded by base64: `=` may precede, as in `AWS_SECRET_ACCESS_KEY=…`.)
_AWS_SECRET = r"(?<![A-Za-z0-9/+])([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])"
_AWS_LABELLED = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:aws[_\-. ]?)?secret[_\-. ]?access[_\-. ]?key(?:[_\-. ]?id)?"
    r"|(?<![A-Za-z0-9])aws[_\-. ]?secret(?:[_\-. ]?key)?")
_AWS_LABEL_VALUE = re.compile(r"[\"']?[ \t]*[:=][ \t]*[\"']?" + _AWS_SECRET)
_AWS_NEAR = re.compile(_AWS_SECRET)
_AWS_BEFORE, _AWS_AFTER = 200, 400


def _aws_secret_like(value: str) -> bool:
    return (any(c.isupper() for c in value) and any(c.islower() for c in value)
            and (any(c.isdigit() for c in value) or "/" in value or "+" in value))


def _random_chunk(chunk: str) -> bool:
    """Does a piece after a break look like more of a token rather than a
    word: a digit, a lower-then-upper letter pair, or a long run with no
    vowel or in capitals."""
    if _DIGIT.search(chunk) or re.search(r"[a-z][A-Z]", chunk):
        return True
    letters = [c for c in chunk if c.isalpha()]
    return len(chunk) >= 10 and (chunk.isupper() or not any(c in "aeiouAEIOU" for c in letters))


# -- the normalized view ----------------------------------------------------------------

# The characters the view changes: C0/C1 controls, Unicode spaces and
# separators, zero-width/bidi/format characters, combining marks, variation
# selectors, private use, fullwidth ASCII, tag characters and mathematical
# alphanumerics. A fixed class keeps the scan fast on long non-ASCII text
# (CJK, emoji); anything outside it is matched as it is.
_SPECIAL = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\xa0\xad\u034f\u0300-\u036f\u061c\u115f\u1160\u1680"
    "\u17b4\u17b5\u180b-\u180f\u1ab0-\u1aff\u1dc0-\u1dff\u2000-\u200f\u2028-\u202f\u205f-\u206f"
    "\u20d0-\u20ff\u3000\u3164\ue000-\uf8ff\ufe00-\ufe0f\ufe20-\ufe2f\ufeff\uff01-\uff5e\uffa0"
    "\ufff0-\ufffb\U0001d173-\U0001d17a\U0001d400-\U0001d7ff\U000e0000-\U000e0fff]")


@lru_cache(maxsize=4096)
def _fold(ch: str) -> str | None:
    """What the view holds for `ch`: None (as it is), "" (dropped) or a
    replacement."""
    code = ord(ch)
    if 0xFF01 <= code <= 0xFF5E:
        return chr(code - 0xFEE0)                   # fullwidth ASCII
    category = unicodedata.category(ch)
    if ch.isspace() or category in ("Zs", "Zl", "Zp"):
        return " "
    if category in ("Cc", "Cf", "Mn", "Me", "Co", "Cn"):
        return ""
    folded = unicodedata.normalize("NFKC", ch)
    if len(folded) == 1 and folded.isascii() and (folded.isalnum() or folded in "_-./+="):
        return folded
    return None


class _View:
    """`text` as the shapes read it, and the way back to `text`.

    `spaced`: a dropped character is read as a space (so it still separates
    a word from a token) rather than as nothing (so it no longer splits one).
    `joined`: an unusual space (NBSP, U+2028, U+0085, a Unicode space, a
    vertical tab) is dropped rather than read as a space, so a token split
    by one is whole again wherever the split is. Ordinary spaces, tabs and
    newlines are never in the class, so they always stay (`_split_spans`)."""

    def __init__(self, text: str, spaced: bool = False, joined: bool = False):
        self.text, self.marks, self.views = text, None, None
        pieces, at, last, vpos = [], [], 0, 0
        for match in _SPECIAL.finditer(text):
            i = match.start()
            rep = _fold(text[i])
            if rep is None:
                continue
            if rep == " " and joined:
                rep = ""
            elif rep == "" and spaced:
                rep = " "
            pieces.append(text[last:i])
            vpos += i - last
            if rep:
                at.append((vpos, i))
                pieces.append(rep)
                vpos += len(rep)
            last = i + 1
            at.append((vpos, last))
        if not at:
            self.view = text
            return
        pieces.append(text[last:])
        self.view = "".join(pieces)
        self.views = [v for v, _ in at]
        self.marks = [o for _, o in at]

    def _at(self, v: int) -> int:
        if self.views is None:
            return v
        k = bisect.bisect_right(self.views, v) - 1
        return v if k < 0 else self.marks[k] + (v - self.views[k])

    def span(self, a: int, b: int) -> tuple[int, int]:
        """The original span of the view's [a, b)."""
        if b <= a:
            return self._at(a), self._at(a)
        return self._at(a), self._at(b - 1) + 1


def _shape_spans(view: str) -> list[tuple[int, int, str]]:
    """Every shape in `view`, as (start, end, kind) in the view."""
    found = [(a, b, KEY) for a, b in private_key_spans(view)]
    for kind, pattern in SHAPES:
        for match in pattern.finditer(view):
            found.append((match.start(), match.end(), kind))
    for match in _BEARER.finditer(view):
        token = match.group(1)
        if _DIGIT.search(token) and re.search(r"[A-Za-z]", token):
            found.append((match.start(1), match.end(1), "bearer token"))
    for match in _BASIC.finditer(view):
        value = match.group(1)
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("ascii")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if ":" in decoded and decoded.isprintable():
            found.append((match.start(1), match.end(1), "basic credentials"))
    found += _url_spans(view)
    found += _aws_spans(view)
    found += _split_spans(view)
    # A withheld span is a boundary, as its marker was in the old one-pattern-
    # at-a-time pass: a token glued to one (`AKIA…hf_…`, `hf_…-----END …`) is
    # matched again with every span found so far blanked out, until nothing
    # new is found (SEC-5's tighten-only rule). Each round can only add.
    for _round in range(4):
        if not found:
            break
        pieces, pos = [], 0
        for a, b in sorted((a, b) for a, b, _kind in found):
            a = max(a, pos)
            if b > a:
                pieces += [view[pos:a], " " * (b - a)]
                pos = b
        blanked = "".join(pieces) + view[pos:]
        more = [(m.start(), m.end(), kind) for kind, pattern in SHAPES if kind != KEY
                for m in pattern.finditer(blanked)]
        if not more:
            break
        found += more
    return found


def _url_spans(view: str) -> list[tuple[int, int, str]]:
    out = []
    for sep in re.finditer(r"://", view):
        scheme = _URL_SCHEME.search(view, max(0, sep.start() - 32), sep.start())
        if scheme is None or (scheme.start() > 0 and re.match(r"[A-Za-z0-9+.\-]", view[scheme.start() - 1])):
            continue
        info = _USERINFO.match(view, sep.end())
        if info is not None:
            password = info.group(2)
            if not _PLACEHOLDER.search(password):
                out.append((info.start(2), info.end(2), "URL password"))
            continue
        token = _TOKEN_USERINFO.match(view, sep.end())
        if token is not None and _DIGIT.search(token.group(1)) and re.search(r"[A-Za-z]", token.group(1)):
            out.append((token.start(1), token.end(1), "URL token"))
    return out


def _aws_spans(view: str) -> list[tuple[int, int, str]]:
    out = []
    for label in _AWS_LABELLED.finditer(view):
        value = _AWS_LABEL_VALUE.match(view, label.end())
        if value is not None and _aws_secret_like(value.group(1)):
            out.append((value.start(1), value.end(1), "AWS secret key"))
    ids = [m for kind, p in SHAPES if kind == "AWS access key id" for m in p.finditer(view)]
    for key_id in ids:
        lo, hi = max(0, key_id.start() - _AWS_BEFORE), min(len(view), key_id.end() + _AWS_AFTER)
        for value in _AWS_NEAR.finditer(view, lo, hi):
            if _aws_secret_like(value.group(1)):
                out.append((value.start(1), value.end(1), "AWS secret key"))
    return out


def _split_spans(view: str) -> list[tuple[int, int, str]]:
    """A prefixed token broken across single ordinary whitespace characters
    (a space, tab or newline; an unusual space such as NBSP, U+2028 or U+0085
    is dropped by the joined view instead): the pieces after the first are
    joined while each looks random (`_random_chunk`), and the joined text is
    tried against every prefixed shape. Only a match that goes past the
    first piece counts, and none when the first piece is a whole token on
    its own: after an ordinary break the next word is the text's own."""
    out = []
    for cand in _SPLIT_PREFIX.finditer(view):
        parts = _SPLIT_BREAK.split(cand.group())
        first = parts[0]
        if any(p.match(first) for _k, p in _PREFIXED):
            continue
        joined, offsets, pos = first, [(0, cand.start())], cand.start() + len(first)
        for k in range(1, len(parts) - 1, 2):
            brk, chunk = parts[k], parts[k + 1]
            pos += len(brk)
            if not _random_chunk(chunk):
                break
            offsets.append((len(joined), pos))
            joined += chunk
            pos += len(chunk)
        if len(offsets) < 2:
            continue
        for kind, pattern in _PREFIXED:
            match = pattern.match(joined)
            if match is None or match.end() <= len(first):
                continue
            end = match.end()
            j_at, v_at = max(o for o in offsets if o[0] < end)
            out.append((cand.start(), v_at + (end - j_at), kind))
            break
    return out


def _merge(spans) -> list[tuple[int, int, str]]:
    """Sorted, disjoint spans; an overlap takes the kind of the span that
    starts first (then the longer)."""
    out: list[list] = []
    for a, b, kind in sorted(spans, key=lambda s: (s[0], -s[1])):
        if a >= b:
            continue
        if out and a < out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
            continue
        out.append([a, b, kind])
    return [tuple(s) for s in out]


def spans(text: str) -> list[tuple[int, int, str]]:
    """Every credential shape in `text`, as sorted, disjoint (start, end,
    kind) spans of `text` itself, found on both normalized views."""
    text = str(text or "")
    found = []
    for spaced, joined in ((False, False), (True, False), (False, True)):
        view = _View(text, spaced, joined)
        found += [(*view.span(a, b), kind) for a, b, kind in _shape_spans(view.view)]
        if view.views is None:
            break                                   # nothing to normalize: one view is all
    return _merge(found)


def shapes(text: str) -> str:
    """`text` with every credential shape withheld as `[withheld: <kind>]`;
    every other character as it was."""
    text = str(text or "")
    found = spans(text)
    return _apply(text, found, withheld) if found else text


# -- the value after a credential's label (display only) --------------------------------

# A value written after a credential's label (review of PR #308):
# `aws_secret_access_key = …` (the ~/.aws/credentials form), `password: …`,
# `"password": "…"`, `client_secret: …`. **Display only**: what a model
# reads (chat relays, git tool output) never takes it, because a rule this
# loose would rewrite ordinary code there. It keeps anything that reads as
# code or a reference rather than a value:
#   - a bare or dotted identifier with no digit (`settings.API_KEY`,
#     `access_token`, `SecretStr`);
#   - anything with `(` or `[` (a call, a subscript, a type);
#   - anything starting `~ / ! * % & @ $ {` (a path, a tag, a mask, a
#     variable, a template) or `<`, `=` (a placeholder, an operator);
#   - a mask (`********`);
#   - under 16 characters, anything without a digit.
# Moved here from `remote_api` (the phone keeps using it through there).
_LABELS = (r"aws_secret_access_key|aws_session_token|secret_access_key|password|passwd|passphrase"
           r"|api[_-]?key|apikey|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token"
           r"|refresh[_-]?token|private[_-]?key")
LABELLED = re.compile(r"(?<![A-Za-z0-9])((?:" + _LABELS + r")[\"']?[ \t]*[:=][ \t]*[\"']?)"
                      r"([^\s\"'<>={}$\[~/!*%&@][^\s\"']{5,})", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_MASK = re.compile(r"[*•●·#xX]+")
# Punctuation after a value, not part of it (a backtick too: markdown's
# inline code, `` `api_key=settings.API_KEY` ``, is code).
_TRAILING = ".,;:!?)]}`"
LABELLED_MARKER = withheld("labelled secret")


def _labelled_keep(core: str) -> bool:
    return (len(core) < 6 or "(" in core or "[" in core or _MASK.fullmatch(core) is not None
            or (_IDENTIFIER.fullmatch(core) is not None and not _DIGIT.search(core))
            or (len(core) < 16 and not _DIGIT.search(core))
            or core.startswith(("[redacted", WITHHELD_PREFIX)))


def labelled_spans(text: str) -> list[tuple[int, int, str]]:
    """The values `labelled_values` withholds, as (start, end, kind)."""
    out = []
    for match in LABELLED.finditer(text):
        value = match.group(2)
        core = value.rstrip(_TRAILING)
        if not _labelled_keep(core):
            out.append((match.start(2), match.start(2) + len(core), "labelled secret"))
    return out


def labelled_values(text: str) -> str:
    """`text` with every value written after a credential's label withheld
    (the rules above); the label kept. Display only."""
    if not isinstance(text, str):
        return text
    found = labelled_spans(text)
    return _apply(text, found, withheld) if found else text


# -- the two scrubs ---------------------------------------------------------------------


# The line separators `str.splitlines` breaks on besides `\n` and `\r`.
# `secrets.scrub` rejoins lines with `\n`, so a token split by one of these
# would reach the shapes split by an ordinary newline instead.
_SEPARATORS = re.compile("[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def _across_separators(text: str) -> str:
    """Before `secrets.scrub`: the shapes that cross one of `_SEPARATORS`,
    withheld (only those: a known value holding a shape must still be whole
    for `secrets.scrub` to find it)."""
    if not _SEPARATORS.search(text):
        return text
    found = [s for s in spans(text) if _SEPARATORS.search(text, s[0], s[1])]
    return _apply(text, found, withheld) if found else text


def for_model(text: str) -> str:
    """Text an agent will read: Jarvis's own credential values
    (`secrets.scrub`) and every credential shape (`shapes`), nothing else.
    Raises only if `secrets.scrub` does; a caller that cannot scrub must
    withhold the text, never pass it on unscrubbed."""
    from jarvis.tools import secrets
    return shapes(secrets.scrub(_across_separators(str(text or ""))))


def scrub_text(text: str) -> str:
    """`for_model`, under the name its callers know (SV-2)."""
    return for_model(text)


def for_display(text: str) -> str:
    """Text the owner reads: `for_model`, then `labelled_values`. Raises
    only if `secrets.scrub` does."""
    return labelled_values(for_model(text))


# -- an approval card -------------------------------------------------------------------

_COMMAND_KEYS = frozenset({"command", "cmd", "commands", "script"})


# Whitespace bash never splits a word on (NBSP, U+2028, U+0085, the Unicode
# spaces): to the shell it is a word's own character, so a span holding one
# hides no structure. Read as a letter when the shell's quoting is judged.
_NOT_A_BLANK = re.compile("[\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]")


def card_spans(text: str, *, command: bool = True) -> list[tuple[int, int, str]]:
    """What an approval card withholds beyond `secrets.redact_command`, as
    spans of `text` as typed: every credential shape and labelled value.

    - **A string the shell never runs** (a `Write`'s `content`, an `Edit`'s
      `new_string`): all of them, and a private key's whole block when every
      line of it is key material (`secrets._key_material`). Before this the
      key's body stayed on the card: `redact_command` takes a block only from
      inside one shell quoting, which plain text never has (review of PR
      #308).
    - **A command**: the owner's condition for redacting cards (2026-10-15)
      is that a withheld span never hides code that runs. So each span only
      where the shell reads no structure (`secrets._Mask`, rule 1, with a
      non-ASCII space read as the word character bash reads it as); labelled
      values not at all on a line that re-parses or runs text (rule 3,
      `secrets._run_names(...) is True`: `eval`, `source`, `bash -c`, a pipe
      into a shell …; a credential shape is not code and still goes, as
      `redact_command`'s own shape pass has it); a private key
      left to `secrets._key_blocks` (an unquoted line of key material at a
      command's place, or after a here-document's `<<`, is a command); a
      URL's userinfo left to `redact_command`'s own rule
      (`strip_url_credentials`: a malformed URL keeps it in sight with its
      expansion)."""
    from jarvis.tools import secrets as S
    found = []
    for a, b, kind in spans(text):
        if kind == KEY and (command or not S._key_material(text[a:b])):
            continue
        if command and kind in ("URL password", "URL token"):
            continue
        found.append((a, b, kind))
    if not (command and S._run_names(text) is True):
        found += labelled_spans(text)
    found = _merge(found)
    if command and found:
        mask = S._Mask(_NOT_A_BLANK.sub("x", text))
        found = [s for s in found if mask.safe(s[0], s[1])]
    return found


class CardPass:
    """`card_spans` around `secrets.redact_command`/`redact_value`. The spans
    are found on the text as typed (`hide`), because `redact_command`'s own
    replacements would take away what they are found by (an access key id
    beside its secret, a split token's first piece). Each is held as a
    placeholder through `redact_command` and comes back as its marker
    (`show`); one that `redact_command` itself withheld (a placeholder as a
    `NAME=` value) stays its `[redacted]`."""

    def __init__(self):
        import secrets as _random
        self._nonce = _random.token_hex(4)
        self._kinds: dict[str, str] = {}

    def hide(self, text: str, *, command: bool = True) -> str:
        found = card_spans(text, command=command)
        if not found:
            return text

        def hold(kind: str) -> str:
            key = f"\ue000{self._nonce}{len(self._kinds)}\ue001"
            self._kinds[key] = kind
            return key
        return _apply(text, found, hold)

    def show(self, text: str, *, command: bool = True) -> str:
        if not self._kinds or "\ue000" not in text:
            return text
        for key, kind in self._kinds.items():
            text = text.replace(key, withheld(kind))
        return text


def map_strings(value, fn, *, command: bool = True, depth: int = 0):
    """Every string in a JSON-shaped `value` through `fn(text, command=…)`,
    structure kept. A string under a command-like key (`command`, `cmd`) is
    judged as a command; any other as text, unless `command` says otherwise
    for the top level."""
    if isinstance(value, str):
        return fn(value, command=command)
    if depth >= 32:
        return value
    if isinstance(value, dict):
        return {k: map_strings(v, fn, command=isinstance(k, str) and k.lower() in _COMMAND_KEYS,
                               depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [map_strings(v, fn, command=command, depth=depth + 1) for v in value]
    return value
