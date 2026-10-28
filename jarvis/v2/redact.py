"""One scrub for model-written text bound for a surface (SV-2 review, 2026-10-15;
hardened 2026-10-25, "Scrubbing: stronger shared credential shapes"; review
of #314).

Three readers, three entry points:

- **`for_model(text)`** (alias `scrub_text`): text an agent will read on a
  path that ran main's `scrub_text` (a chat relay's body and reply, a chat
  git tool's output, a sub-agent's words, Codex's compaction reason,
  another chat's rows). It withholds Jarvis's own credential values (`secrets.scrub`),
  every **high-confidence credential shape** (`SHAPES` and the rules below)
  and main's two legacy patterns (`HEURISTIC`, so nothing main withheld there
  is shown), and nothing else: a model must read ordinary code as written,
  so no rule about what follows a label runs here (review of PR #308:
  `api_key=settings.API_KEY` reaches a model intact).
- **`for_model_strict(text)`**: a model's input main gave no shape scrub
  (diff review's input): the high-confidence shapes only.
- **`for_display(text)`**: text the owner reads (the desk's Diff tab, the
  transcript through `chat_reads.scrub`, the Discord mirror and reporter):
  everything above, a URL password at any host, then `labelled_values`, the
  phone's tuned rule for a value written after a credential's label
  (`password: …`, `client_secret: …`).

The rules beyond the table: a private key's whole block (`private_key_spans`,
SEC-5: header to footer, as main withholds it), a `Bearer`/`Basic` credential, a URL's password (a model's input
keeps a local or test host's: `http://u:p@localhost:5173/`), an AWS secret
key beside or labelled as one, a lower-case `sk`/`oat`/`rt` bearer with a
random run, a Discord token whose first part decodes to an id. Every
withheld span becomes `[withheld: <kind>]`, so the reader knows something
was there and what kind of thing it was.

Matching runs on a **normalized view** of the text, never on the text shown
(`_View`): fullwidth forms folded to ASCII, combining marks dropped, and an
invisible or unusual-space character (zero-width, bidi, soft hyphen, NBSP,
U+2028, U+0085 …) dropped where it sits inside a token (what follows reads
as more of one, or the join completes one) and read as a space where it does
not, so a token never swallows the word after it. A prefixed token split
across an ordinary space, tab or newline is joined when what follows looks
random (`_split_spans`; after a whole token only a newline is crossed, so a
wrapped key's tail goes with it). A span found in the view is mapped back
and withheld in the original. **Everything else comes back byte for byte**:
`secrets.scrub` rejoins lines with `\\n`, and its lines are put back with the
original's own endings (`_known_values`), so CRLF, a final newline, U+2028
and `\\x0b` survive; only where it drops a protected file's line does its
own output stand.

An approval card is a fourth reader (`card_spans`, `CardPass`): spans found
on the text as typed and held through `secrets.redact_command`, within the
owner's condition that a withheld span never hides code that runs. A string
the shell never runs (a `Write`'s content) takes every display span and a
private key's whole block when the block is nothing but key material (never
code between a header and a footer); a command (every string beside a command or argv
key, a path, the headline) only the formats `credential_patterns` knows, never
at a command's word or on a line that runs text (review of #314, H1).

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

# The three readers a scrub serves (review of #314, S5):
# - STRICT: a model's input that had no shape scrub on main (diff review's
#   input): the high-confidence shapes only;
# - MODEL: a model's input that ran main's `scrub_text` (chat relays, chat
#   git output, sub-agent words, Codex's compaction reason, another chat's rows):
#   the high-confidence shapes and main's two legacy patterns (`HEURISTIC`),
#   so nothing main withheld there is shown;
# - DISPLAY: what the owner reads: all of it, a URL password at any host.
STRICT, MODEL, DISPLAY = "strict", "model", "display"

# (kind, pattern): the **high-confidence** shapes, withheld wherever text
# goes, a model's input included. Each is a prefix or framing nobody types by
# accident; the order is the order a kind is named when two spans overlap
# (the earlier start wins, then the longer span). `credential_patterns.PATTERNS`
# (the terminal read's refusal list, SELF_PROTECTED) is a subset in spirit; it
# decides refusals, so the scrub keeps its own table.
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
        # After a `-` too, as main's `\b` had it (review of #314, L2: `-eyJ…`).
        ("JWT", r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
        ("Hugging Face token", _B + r"hf_[A-Za-z0-9]{30,}" + _E),
        ("npm token", _B + r"npm_[A-Za-z0-9]{36}" + _E),
        ("GitLab token", _B + r"glpat-[A-Za-z0-9_\-]{20,}"),
        ("PyPI token", _B + r"pypi-AgE[A-Za-z0-9_\-]{50,}"),
        ("Jarvis caller token", r"jmcp_[A-Za-z0-9_\-]{8,}"),
        # A private key's header or footer on its own (the block rule takes
        # the whole block first; this is what is left of a stray one).
        (KEY, r"-----(?:BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    )
)
# The **heuristic** shapes: what main's shared scrub (`scrub_text`) already
# withheld with a looser pattern than a credential's (Claude's provider's
# `sk`/`oat`/`rt` + 12 in any case; `credential_patterns`' Discord bot
# token, which any long dotted identifier fits:
# `ModelConfigurationRegistry.values.get_…()`). Kept on DISPLAY and on the
# MODEL paths main ran them on (never less than main withheld there); not
# on STRICT, where `sk_buff_alloc_head`, `rt_mutex_lock_interruptible`
# and `SK_ColorTRANSPARENT_BLACK` reach the model as the code they are
# (review of #314, S5). The tightened versions below run on every path.
HEURISTIC: tuple[tuple[str, re.Pattern], ...] = (
    ("token", re.compile(r"(?i)\b(?:sk|oat|rt)[-_][A-Za-z0-9_\-]{12,}")),
    ("Discord bot token", re.compile(
        _B + r"[MNO][A-Za-z0-9_\-]{23,27}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}")),
)
# The tightened versions, on every path. A bearer (`sk-`, `sk_`, `oat_`,
# `rt_` in lower case) whose body holds a run of 12 letters and digits that
# reads as random (a digit and a letter, or both cases); a Discord token whose
# first part decodes to the bot's numeric id.
_TOKEN = re.compile(_B + r"(?:sk|oat|rt)[-_]([A-Za-z0-9_\-]{12,})")
_RUN12 = re.compile(r"[A-Za-z0-9]{12,}")
_DISCORD = re.compile(_B + r"([MNO][A-Za-z0-9_\-]{23,27})\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}")
# The kinds whose pattern starts with a fixed prefix: a token split across a
# break is joined and tried against these (`_split_spans`, `_joins`).
_PREFIXED = tuple((kind, p) for kind, p in SHAPES if kind != KEY)
_PREFIXES = (r"sk-|gh[pousr]_|github_pat_|xox[abeprs]-|xapp-|AIza|(?:sk|rk)_(?:live|test)_|eyJ|hf_|npm_"
             r"|glpat-|pypi-|jmcp_|IQoJb3JpZ2lu|FwoGZXIvYXdz|FQoGZXIvYXdz|AKIA|ASIA|ABIA|ACCA")
# `_split_spans`' pieces (review of #314, M2): each maximal run of token
# characters is read once, the first prefix in it found by one search, and
# the pieces after it matched one break at a time, so a long run holding many
# prefixes and no break (`x.sk-.sk-…`, `/AKIA/AKIA…`) costs one pass, not one
# pass per prefix (the old single pattern's greedy run backtracked from every
# prefix to the run's end: 70 s at 200 KB).
_SPLIT_RUN = re.compile(r"[A-Za-z0-9_\-./+=]+")
_SPLIT_START = re.compile(_B + r"(?:" + _PREFIXES + r")")
_SPLIT_PIECE = re.compile(r"(\r\n|\r|\n|[ \t])([A-Za-z0-9_\-./+=]+)")
_SPLIT_PIECES = 8
_SPLIT_BREAK = re.compile(r"(\r\n|\r|\n|[ \t])")
_NEWLINES = ("\n", "\r\n", "\r")
# A line that starts a new statement is never the tail of a wrapped token
# (review of #314, L1): `NAME=…`, or a word a shell line starts with. A
# word of letters and digits ending in `=` padding with nothing after it is
# base64's tail, not an assignment (third review L4: a wrapped AWS session
# token's `…==`); one with an `_` in its name stays one (`S3_BUCKET=`).
_STATEMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(?!=*\Z)|[A-Za-z0-9]*_[A-Za-z0-9_]*=+\Z|(?:export|declare|typeset|local|readonly|set|unset"
                        r"|env|sudo|exec|eval|source|alias|cd|echo|printf|if|then|else|elif|fi|for"
                        r"|while|until|do|done|case|esac|function|return|exit)\Z")
# Any prefix a prefixed shape starts with: a cheap first look before trying
# every pattern on a run.
_ANY_PREFIX = re.compile(_PREFIXES)
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
# `%(password)s`, `****`, `…`, `password`). On a model's input a local or
# test host stays too (`http://u:p@localhost:5173/` in a fixture is code), and
# so does a one- or two-letter example (`http://u:p@[a]/` in a comment).
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}\Z")
_USERINFO = re.compile(r"([^\s:/?#@'\"<>`\[\]{}]*):([^\s/?#@'\"<>`]+)@")
_TOKEN_USERINFO = re.compile(r"([A-Za-z0-9_\-.~]{20,})@")
_PLACEHOLDER = re.compile(r"(?i)[$%{<*…]|\.\.\.|\A(?:x+|\*+|•+|pass(?:word|wd)?|pwd?|secret|token|changeme)\Z")
_HOST = re.compile(r"\[([0-9A-Fa-f:.]*)\]|([A-Za-z0-9.\-]*)")
_LOCAL_HOST = re.compile(r"(?i)\A(?:localhost|127(?:\.\d+){3}|0\.0\.0\.0|::1"
                         r"|.*\.(?:localhost|local|test|example|invalid)"
                         r"|(?:.*\.)?example\.(?:com|org|net))\Z")
# AWS: a secret access key is 40 characters of base64 with both cases. It
# goes when a label names it, or when an access key id is near it.
# (Not preceded by base64: `=` may precede, as in `AWS_SECRET_ACCESS_KEY=…`.)
_AWS_SECRET = r"(?<![A-Za-z0-9/+])([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])"
# (Each branch starts with a literal, so the scan of a long text is fast; only
# the value after the label is ever withheld.)
_AWS_LABELLED = re.compile(
    r"(?i)aws[_\-. ]?secret[_\-. ]?access[_\-. ]?key(?:[_\-. ]?id)?"
    r"|secret[_\-. ]?access[_\-. ]?key(?:[_\-. ]?id)?|aws[_\-. ]?secret(?:[_\-. ]?key)?")
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
_HANGUL_FILLERS = frozenset("\u115f\u1160\u3164\uffa0")
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-./+=")
_MARKS = ("Mn", "Me")


@lru_cache(maxsize=4096)
def _fold(ch: str) -> str | None:
    """What the view holds for `ch`: None (as it is), "" (dropped), " " (a
    break) or a replacement."""
    code = ord(ch)
    if 0xFF01 <= code <= 0xFF5E:
        return chr(code - 0xFEE0)                   # fullwidth ASCII
    if ch in _HANGUL_FILLERS:
        return ""                                   # Lo, but shows nothing (review of #314, L3)
    category = unicodedata.category(ch)
    if ch.isspace() or category in ("Zs", "Zl", "Zp"):
        return " "
    if category in ("Cc", "Cf", "Mn", "Me", "Co", "Cn"):
        return ""
    folded = unicodedata.normalize("NFKC", ch)
    if len(folded) == 1 and folded.isascii() and (folded.isalnum() or folded in "_-./+="):
        return folded
    return None


def _as_token(ch: str) -> str | None:
    """`ch` as a token character of the view ("" for a combining mark, which
    a token carries through), or None where a token's run ends."""
    if ch in _TOKEN_CHARS:
        return ch
    if ch.isascii():
        return None
    if unicodedata.category(ch) in _MARKS:
        return ""
    folded = _fold(ch)
    return folded if folded and folded in _TOKEN_CHARS else None


def _run(text: str, i: int, step: int, limit: int) -> str:
    """The token run beside `text[i]` (after it for step 1, before it for
    -1), folded, up to `limit` characters."""
    out, j = [], i + step
    while 0 <= j < len(text) and len(out) < limit:
        c = _as_token(text[j])
        if c is None:
            break
        out.append(c)
        j += step
    return "".join(out if step > 0 else reversed(out))


def _ends_whole(run: str) -> bool:
    """Does a prefixed shape match `run` up to its end?"""
    return _ANY_PREFIX.search(run) is not None and any(
        m.end() == len(run) for _k, p in _PREFIXED for m in p.finditer(run))


def _starts_whole(run: str) -> bool:
    """Is `run` a whole prefixed token from its start?"""
    return _ANY_PREFIX.match(run) is not None and any(p.match(run) for _k, p in _PREFIXED)


# A bearer the heuristic or tightened rule reads (`sk`/`oat`/`rt` + a
# separator): an invisible character right before one is a break, never a
# join, so `word<ZWSP>oat_…` keeps the token's own start (review of #314, L2;
# main's `\b` saw one there).
_BEARER_START = re.compile(r"(?i)(?:sk|oat|rt)[-_][A-Za-z0-9_\-]{12}")


def _completes(before: str, after: str) -> bool:
    """Does joining `before` and `after` make a prefixed shape that crosses
    the join, where `before` held no whole one at its end?"""
    joined = before + after
    if _ANY_PREFIX.search(joined) is None:
        return False
    return not _ends_whole(before) and any(
        m.start() < len(before) < m.end() for _k, p in _PREFIXED for m in p.finditer(joined))


def _joins(text: str, i: int) -> bool:
    """Is the invisible or unusual-space character at `i` inside a token, so
    the view drops it: what follows reads as more of a token (`_random_chunk`)
    and does not start a whole one of its own, or the join completes a shape
    the run before it was not already. Otherwise it is a break (so a token
    never swallows the word after it: `ghp_…<NBSP>and more`)."""
    after = _run(text, i, 1, 64)
    if not after or _starts_whole(after) or _BEARER_START.match(after):
        return False
    if _random_chunk(after):
        return True
    return _completes(_run(text, i, -1, 256), after)


class _View:
    """`text` as the shapes read it, and the way back to `text`: fullwidth
    folded, combining marks dropped, and each invisible or unusual-space
    character dropped when it sits inside a token (`_joins`) or read as a
    space when it does not. Ordinary spaces, tabs and newlines are never in
    `_SPECIAL`, so they always stay (`_split_spans` joins across those)."""

    def __init__(self, text: str):
        self.text, self.marks, self.views = text, None, None
        pieces, at, last, vpos = [], [], 0, 0
        for match in _SPECIAL.finditer(text):
            i = match.start()
            rep = _fold(text[i])
            if rep is None:
                continue
            if rep == " " or (rep == "" and unicodedata.category(text[i]) not in _MARKS):
                rep = "" if _joins(text, i) else " "
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
        """The original span of the view's [a, b), with any combining mark
        right after it (a mark on the token's last letter goes with it)."""
        if b <= a:
            return self._at(a), self._at(a)
        start, end = self._at(a), self._at(b - 1) + 1
        text = self.text
        while end < len(text) and not text[end].isascii() and unicodedata.category(text[end]) in _MARKS:
            end += 1
        return start, end


# Context a glued token can need beside a withheld span (`_shape_spans`'
# boundary rounds), wider than any shape's token.
_ROUND_WINDOW = 512


def _regex_spans(view: str, table, lo: int = 0, hi: int | None = None) -> list[tuple[int, int, str]]:
    seg = view if lo == 0 and hi is None else view[lo:hi]
    return [(lo + m.start(), lo + m.end(), kind) for kind, p in table for m in p.finditer(seg)]


# Where any shape of `SHAPES` can start: one scan finds the places, and only
# there is each pattern tried (review of #314, S2: one full pass per pattern
# made a 5 MB patch take seconds).
_CANDIDATE = re.compile(_PREFIXES + r"|-----(?:BEGIN|END) ")


# The kinds a later start can sit inside an earlier one's run (review of
# #314, third review M2): a JWT may start after a `-` (as main's `\b` had it)
# and a caller token after anything, so `-eyJ-eyJ…` and `jmcp_jmcp_…` are
# a start every few characters, each read to the run's end (quadratic: 15 s
# at 400 KB). (kind: (its start, its body's run).) A start inside the body
# run of an earlier start of the same kind is dominated by it: its match,
# if any, ends where the earlier one's does (a JWT's first part ends at its
# first `.`; a caller token's at its run's end) and the earlier one, having
# the longer body, matches whenever it does. So once a start has been tried,
# every start of that kind before its run's end is skipped, matched or not.
_RUN_STARTS = {
    "JWT": (re.compile(r"(?<![A-Za-z0-9_])eyJ"), re.compile(r"[A-Za-z0-9_\-]*")),
    "Jarvis caller token": (re.compile(r"jmcp_"), re.compile(r"[A-Za-z0-9_\-]*")),
}


def _prefixed_spans(view: str, table, lo: int = 0, hi: int | None = None) -> list[tuple[int, int, str]]:
    hi = len(view) if hi is None else hi
    out, dead = [], {}
    for candidate in _CANDIDATE.finditer(view, lo, hi):
        pos = candidate.start()
        for kind, pattern in table:
            if pos < dead.get(kind, -1):
                continue
            match = pattern.match(view, pos, hi)
            if match is not None:
                out.append((match.start(), match.end(), kind))
            run = _RUN_STARTS.get(kind)
            if run is not None and (start := run[0].match(view, pos, hi)) is not None:
                dead[kind] = run[1].match(view, start.end(), hi).end()
    return out


def _shape_spans(view: str, mode: str) -> list[tuple[int, int, str]]:
    """Every shape in `view`, as (start, end, kind) in the view, for the
    reader `mode` names (`STRICT`, `MODEL`, `DISPLAY`)."""
    found = [(a, b, KEY) for a, b in private_key_spans(view)]
    found += _prefixed_spans(view, SHAPES)
    if mode != STRICT:
        found += _regex_spans(view, HEURISTIC)
    found += _token_spans(view)
    found += _discord_spans(view)
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
    found += _url_spans(view, mode == DISPLAY)
    found += _aws_spans(view)
    found += _split_spans(view)
    # A withheld span is a boundary, as its marker was in the old one-pattern-
    # at-a-time pass: a token glued to one (`AKIA…hf_…`, `hf_…-----END …`) is
    # matched again with every span found so far blanked out, until nothing
    # new is found (SEC-5's tighten-only rule). Each round can only add, and
    # looks only around the spans (a glued token touches one), so a long text
    # is not scanned again in full.
    regex_table = tuple((k, p) for k, p in SHAPES if k != KEY)
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
        windows: list[list[int]] = []
        for a, b, _kind in sorted(found):
            lo, hi = max(0, a - _ROUND_WINDOW), min(len(view), b + _ROUND_WINDOW)
            stop = max(0, lo - 4096)
            while lo > stop and view[lo - 1] in _TOKEN_CHARS:   # never start inside a word
                lo -= 1
            if windows and lo <= windows[-1][1]:
                windows[-1][1] = max(windows[-1][1], hi)
            else:
                windows.append([lo, hi])
        seen = set(found)
        more = [s for lo, hi in windows
                for s in _prefixed_spans(blanked, regex_table, lo, hi)
                + (_regex_spans(blanked, HEURISTIC, lo, hi) if mode != STRICT else [])
                if s not in seen]
        if not more:
            break
        found += more
    return found


def _token_spans(view: str) -> list[tuple[int, int, str]]:
    out = []
    for match in _TOKEN.finditer(view):
        for run in _RUN12.findall(match.group(1)):
            if (_DIGIT.search(run) and re.search(r"[A-Za-z]", run)) or \
                    (re.search(r"[a-z]", run) and re.search(r"[A-Z]", run)):
                out.append((match.start(), match.end(), "token"))
                break
    return out


def _discord_spans(view: str) -> list[tuple[int, int, str]]:
    out = []
    for match in _DISCORD.finditer(view):
        head = match.group(1)
        try:
            decoded = base64.b64decode(head + "=" * (-len(head) % 4), altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            continue
        if decoded.isdigit() and 15 <= len(decoded) <= 21:
            out.append((match.start(), match.end(), "Discord bot token"))
    return out


def _url_spans(view: str, display: bool = True) -> list[tuple[int, int, str]]:
    out = []
    for sep in re.finditer(r"://", view):
        scheme = _URL_SCHEME.search(view, max(0, sep.start() - 32), sep.start())
        if scheme is None or (scheme.start() > 0 and re.match(r"[A-Za-z0-9+.\-]", view[scheme.start() - 1])):
            continue
        info = _USERINFO.match(view, sep.end())
        if info is not None:
            password = info.group(2)
            at = _HOST.match(view, info.end())
            local = _LOCAL_HOST.match(at.group(1) or at.group(2) or "") is not None
            if not _PLACEHOLDER.search(password) and (display or not (local or len(password) < 3)):
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


def _split_candidates(view: str):
    """(start, text) of each prefixed run followed by up to `_SPLIT_PIECES`
    pieces, each after one space, tab or line break (what the old
    `_SPLIT_PREFIX` matched), in one pass over `view`."""
    pos, n = 0, len(view)
    while pos < n:
        run = _SPLIT_RUN.search(view, pos)
        if run is None:
            return
        end = run.end()
        start = _SPLIT_START.search(view, run.start(), end)
        piece = _SPLIT_PIECE.match(view, end) if start is not None else None
        if piece is None:
            pos = end
            continue
        for _k in range(_SPLIT_PIECES):
            if piece is None:
                break
            end = piece.end()
            piece = _SPLIT_PIECE.match(view, end)
        yield start.start(), view[start.start():end]
        pos = end


def _own_line(view: str, at: int, chunk: str, whole: bool) -> bool:
    """Is the line starting at `at` (its first word `chunk`) the text's own,
    never a wrapped token's tail (review of #314, L1)? It starts a new
    statement (`NAME=…`, `export`, a word a shell line starts with), or,
    after a whole token, it goes on past its first word (`x9dropper
    --install`): a wrapped key's tail fills its line alone."""
    if _STATEMENT.match(chunk):
        return True
    after = at + len(chunk)
    return whole and after < len(view) and view[after] in " \t"


def _split_spans(view: str) -> list[tuple[int, int, str]]:
    """A prefixed token broken across single ordinary whitespace characters
    (a space, tab or line break; an invisible or unusual space is joined by
    the view itself): the pieces after the first are joined while each looks
    random (`_random_chunk`), and the joined text is tried against every
    prefixed shape. Only a match that goes past the first piece counts. When
    the first piece is a whole token already, only a line break is crossed (a
    wrapped line: `sk-or-v1-<30 hex>` then the other 34 on the next, review
    of #314, S3), and only into a line that holds nothing else; after a space
    or tab the next word is the text's own. A line that starts a new
    statement (`NAME=…`, `export`, a word a shell line starts with) is never
    joined (L1)."""
    out = []
    for cand_start, text in _split_candidates(view):
        parts = _SPLIT_BREAK.split(text)
        first = parts[0]
        whole = _starts_whole(first)
        joined, offsets, pos = first, [(0, cand_start)], cand_start + len(first)
        for k in range(1, len(parts) - 1, 2):
            brk, chunk = parts[k], parts[k + 1]
            if whole and brk not in _NEWLINES:
                break
            if brk in _NEWLINES and _own_line(view, pos + len(brk), chunk, whole):
                break
            pos += len(brk)
            if not _random_chunk(chunk) or _starts_whole(chunk):
                break
            offsets.append((len(joined), pos))
            joined += chunk
            pos += len(chunk)
        if len(offsets) < 2:
            continue
        best = None
        for kind, pattern in _PREFIXED:
            match = pattern.match(joined)
            if match is not None and match.end() > len(first) and (best is None or match.end() > best[0]):
                best = (match.end(), kind)
        if best is not None:
            end, kind = best
            j_at, v_at = max(o for o in offsets if o[0] < end)
            out.append((cand_start, v_at + (end - j_at), kind))
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


def spans(text: str, *, mode: str = "model") -> list[tuple[int, int, str]]:
    """Every credential shape in `text`, as sorted, disjoint (start, end,
    kind) spans of `text` itself, found on its normalized view, for the
    reader `mode` names (`STRICT`, `MODEL`, `DISPLAY`)."""
    text = str(text or "")
    view = _View(text)
    return _merge((*view.span(a, b), kind) for a, b, kind in _shape_spans(view.view, mode))


def shapes(text: str, *, mode: str = "model") -> str:
    """`text` with every credential shape withheld as `[withheld: <kind>]`;
    every other character as it was."""
    text = str(text or "")
    found = spans(text, mode=mode)
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
# A backslash escape after a value is the text's, not the value's: `printf
# 'password: …\n'` keeps its `\n` (review of #314, N3).
_TRAILING_ESCAPE = re.compile(r"(?:\\[nrt0])+\Z")
LABELLED_MARKER = withheld("labelled secret")


def _labelled_keep(core: str) -> bool:
    return (len(core) < 6 or "(" in core or "[" in core or _MASK.fullmatch(core) is not None
            or (_IDENTIFIER.fullmatch(core) is not None and not _DIGIT.search(core))
            or (len(core) < 16 and not _DIGIT.search(core))
            or core.startswith(("[redacted", WITHHELD_PREFIX)))


def _value_core(value: str) -> str:
    core = value
    while True:
        trimmed = _TRAILING_ESCAPE.sub("", core.rstrip(_TRAILING))
        if trimmed == core:
            return core
        core = trimmed


def labelled_spans(text: str) -> list[tuple[int, int, str]]:
    """The values `labelled_values` withholds, as (start, end, kind)."""
    out = []
    for match in LABELLED.finditer(text):
        core = _value_core(match.group(2))
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
# Every line break `str.splitlines` knows but `\n`.
_OTHER_BREAKS = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def _across_separators(text: str, mode: str) -> str:
    """Before `secrets.scrub`: the shapes that cross one of `_SEPARATORS`,
    withheld (only those: a known value holding a shape must still be whole
    for `secrets.scrub` to find it)."""
    if not _SEPARATORS.search(text):
        return text
    found = [s for s in spans(text, mode=mode) if _SEPARATORS.search(text, s[0], s[1])]
    return _apply(text, found, withheld) if found else text


def _known_values(text: str) -> str:
    """`secrets.scrub` (Jarvis's credential values, and the lines a tool
    attributes to a protected file), with the text's own line breaks kept.
    The v1 scrub splits on every line break and joins with `\\n` (CRLF became
    LF, a final newline went, U+2028 became `\\n`); its lines are put back
    with the original's endings, so text it withholds nothing from comes back
    byte for byte (review of #314, S4). When it drops lines (a protected
    file's) the line count no longer matches, and its own output stands."""
    from jarvis.tools import secrets
    out = secrets.scrub(text)
    if not text:
        return out
    if not _OTHER_BREAKS.search(text):
        # Only `\n` breaks: the v1 output of a text it withheld nothing from
        # is the text without its final newline (a cheap check on a long one).
        bare = text[:-1] if text.endswith("\n") else text
        if out == bare:
            return text
        if text.endswith("\n") and len(out.split("\n")) == len(bare.split("\n")):
            return out + "\n"
        return out
    bodies = text.splitlines()
    lines = out.split("\n")
    if len(lines) != len(bodies):
        return out
    ends = [line[len(body):] for line, body in zip(text.splitlines(keepends=True), bodies)]
    return "".join(line + end for line, end in zip(lines, ends))


def _scrubbed(text, mode: str) -> str:
    text = str(text or "")
    return shapes(_known_values(_across_separators(text, mode)), mode=mode)


def for_model(text: str) -> str:
    """Text an agent will read: Jarvis's own credential values
    (`secrets.scrub`), every high-confidence credential shape and main's
    two legacy patterns (`HEURISTIC`: never less than main's `scrub_text`
    withheld), nothing else; no labelled value. Outside a withheld span the text is the input's, byte for
    byte. Raises only if `secrets.scrub` does; a caller that cannot scrub
    must withhold the text, never pass it on unscrubbed."""
    return _scrubbed(text, MODEL)


def for_model_strict(text: str) -> str:
    """`for_model` without main's two legacy patterns, for a model path
    main gave no shape scrub at all (diff review's input): the
    high-confidence shapes only, so code such as `sk_buff_alloc_head(skb)`
    or `ModelConfigurationRegistry.values.get_default_configuration_for_model()`
    reaches the reviewer as written."""
    return _scrubbed(text, STRICT)


def scrub_text(text: str) -> str:
    """`for_model`, under the name its callers know (SV-2)."""
    return for_model(text)


def for_display(text: str) -> str:
    """Text the owner reads: Jarvis's own credential values, every
    credential shape, the heuristic ones too, then `labelled_values`. Raises
    only if `secrets.scrub` does."""
    return labelled_values(_scrubbed(text, DISPLAY))


def for_display_capped(text: str, cap: int, margin: int = 16384) -> tuple[str, bool]:
    """`for_display` of no more of `text` than a `cap`-byte view needs, and
    whether anything was left out (the Diff tab, review of #314, S2: a 5 MB
    patch was scrubbed whole before 1 MB of it was shown). The scrub runs on
    a head of the text `margin` characters past the cap, doubled until its
    scrubbed form is `margin` bytes past the cap or is the whole text, so a
    token the head's end cuts in two (shown unwithheld at the scrubbed
    head's very end) is always past the cut. A trailing newline is kept as
    the text has it."""
    head = text
    if len(text) > cap + margin:
        size = cap + margin
        while True:
            head = text[:size]
            out = for_display(head)
            if len(out.encode("utf-8", "replace")) >= cap + margin or size >= len(text):
                break
            size *= 2
    else:
        out = for_display(text)
    if head.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    encoded = out.encode("utf-8", "replace")
    truncated = len(head) < len(text) or len(encoded) > cap
    return encoded[:cap].decode("utf-8", errors="ignore"), truncated


# -- an approval card -------------------------------------------------------------------

# The keys a command line, or its argv, travels in (`approvals.COMMAND_KEYS`
# and `_ARGV_KEYS`). A dict that holds any of them is a command's arguments:
# **every** string in it is judged as a command, an argv beside `command`
# included (review of #314, B1: `{"command": "bash", "args": ["-c",
# "PASSWORD=…;curl …|sh"]}` is one command line, and a value judged as text
# swallowed `;curl …|sh`).
_SHELL_KEYS = frozenset({"command", "cmd", "commands", "script", "args", "argv", "arguments"})


# Whitespace bash never splits a word on (NBSP, U+2028, U+0085, the Unicode
# spaces): to the shell it is a word's own character, so a span holding one
# hides no structure. Read as a letter when the shell's quoting is judged.
_NOT_A_BLANK = re.compile("[\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]")
# What `secrets._UNSAFE_VALUE` calls structure: a blank, an operator, an
# expansion, a glob.
_SHELL_CHAR = re.compile(r"[\s;&|<>()$`{}*?\[\]\\]")


# What a command's card may withhold beyond `redact_command` (review of
# #314, H1): only the formats v1's `credential_patterns` knows (the shapes
# `redact_command` itself withholds), now also when split, fullwidth or
# hidden by an invisible character. Never the heuristic shapes, a lower-case
# bearer, an AWS secret found near a key id, a `Bearer`/`Basic` value or a
# URL's userinfo (`redact_command`'s own rules read those): a command's
# words are code first (`oat_deploy_production_cluster`,
# `/home/u/rt_hooks_dir_v2/pre-commit`, a 40-character payload beside an
# access key id).
_CARD_COMMAND_KINDS = frozenset({
    "OpenRouter key", "OpenAI key", "API key", "Anthropic key", "GitHub token", "AWS access key id",
    "Slack token", "Google API key", "Stripe key", "JWT", "Hugging Face token", "npm token",
    "GitLab token", "PyPI token", "Discord bot token"})
_CARD_COMMAND_PATTERNS = tuple(p for name, p in credential_patterns.PATTERNS
                               if name not in ("private-key", "private-key-end"))
_JOIN_BREAKS = re.compile(r"[ \t\r\n]+")
# Words after which the next word is a command's, as `_stem_at` does not
# skip them: shell keywords and a subshell's opening.
_COMMAND_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"})
_SUBSHELL = re.compile(r"\$\(|[(`]")
# The argument keys a card judges as a command's (`map_strings`): a command
# or its argv, and a path (`Write`'s `file_path`: a file is named, never a
# secret's place; review of #314, H1).
_PATH_KEYS = frozenset({"file_path", "notebook_path", "path"})


def _command_words(text: str) -> list[tuple[int, int]]:
    """Each segment's program word (`secrets._stem_at`, after any shell
    keyword) and each word right after a subshell's opening, as spans of
    `text`. A span touching one is never withheld: it would hide what runs."""
    from jarvis.tools import secrets as S
    out, last, bounds = [], 0, []
    for sep in S._SEGMENTS.finditer(text):
        bounds.append((last, sep.start()))
        last = sep.end()
    bounds.append((last, len(text)))
    for lo, hi in bounds:
        words = list(S._WORD_RE.finditer(text, lo, hi))
        values = [S._literal(w.group()) or w.group() for w in words]
        i = 0
        while i < len(values) and values[i] in _COMMAND_KEYWORDS:
            i += 1
        at = i + S._stem_at(values[i:])
        if at < len(words):
            out.append(words[at].span())
    for sub in _SUBSHELL.finditer(text):
        word = S._WORD_RE.search(text, sub.end())
        if word is not None and not text[sub.end():word.start()].strip(" \t"):
            out.append(word.span())
    return out


# A private key's block on an approval card that is text (third review of
# #314, H; the coordinator's decision of round 3): every other reader
# withholds header to footer whole, as main does (`private_key_spans`), but
# a card must never hide code that will run, so a fake header and footer
# around code (`HEADER = "-----BEGIN RSA PRIVATE KEY-----"`, code,
# `FOOTER = …`) must not hide the code. A block goes whole on a card only
# when every word between its header and footer is key material or framing;
# otherwise its header, footer and the key-material words next to them go,
# never anything past a word that is not.
#
# The breaks between a block's lines: a real line break (CRLF, LF or a lone
# CR), a `\n`/`\r` escape however many backslashes deep (JSON in JSON), and
# spaces (a key flattened onto one line). A line that is an armor or PEM
# header (`Version:`, `Comment:`, `Proc-Type:`, `DEK-Info:`, of plain
# characters) is one item; any other line is its words.
_CARD_KEY_SPLIT = re.compile("(" + KEY_BEGIN.pattern + ")|(" + KEY_END.pattern + r")|(\r\n|\r|\n|\\+(?:r\\+)?n|\\+r)")
_CARD_WORD = re.compile(r"[^ \t]+")
_CARD_ARMOR = re.compile(r"(?:Version|Comment|Hash|Charset|MessageID|Proc-Type|DEK-Info):[ \t]*[A-Za-z0-9 .,:/@+_=\-]*")
# A word that is framing alone: quotes, a `+` operator, a bracket, a comma
# or semicolon, a comment's or quote's `#`, `>` or `//`, a continuation's `\`.
_CARD_FRAME = re.compile(r"[\"'+(),;#>\\]+|//")
# A word's framing: before it, `(`, `+` and `,` only when a quote follows
# (with a `b`/`r`/`u` prefix); after it, a quote then `+`, `)`, `,` or `;`;
# then a continuation's `\`. A `(` or `+` not next to a quote is the word's.
_CARD_FRAME_HEAD = re.compile(r"[(+,]*[bBrRuU]{0,2}[\"']")
_CARD_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_CARD_CHECKSUM = re.compile(r"=[A-Za-z0-9+/]{4}")
_CARD_ESCAPED_SLASH = re.compile(r"\\+/")
_CARD_GAP = re.compile(r"(?:\s|\\+[rn])*")
# A key's body line: at least this long, of both cases and a digit.
_CARD_KEY_MIN = 40
# A block's last line before its footer: at most this long, `=` padded.
_CARD_TAIL_MAX = 64


def _card_core(text: str, a: int, b: int) -> tuple[int, int]:
    """The word `text[a:b]` without its framing."""
    if b > a and text[b - 1] == "\\":
        b -= 1
    j = b
    while j > a and text[j - 1] in "+),;":
        j -= 1
    if j > a and text[j - 1] in "\"'":
        b = j - 1
    head = _CARD_FRAME_HEAD.match(text, a, b)
    return (head.end() if head else a), b


def _card_word(text: str, a: int, b: int):
    """(what, core start, core end, core length unescaped) for one word of a
    block: what is "frame"
    (framing alone), "key" (a body line: 40+ base64 characters of both cases
    and a digit; or a PGP checksum), "tail" (base64 of at most 64 ending in
    `=` padding: key material only as the block's last line before its
    footer), "slash-key"/"slash-tail" (either, starting with `/`: counted
    only where the block's own line width vouches for it), or None (anything else:
    never withheld as a key's, and nothing past it is)."""
    if _CARD_FRAME.fullmatch(text, a, b):
        return "frame", a, a, 0
    ca, cb = _card_core(text, a, b)
    core = _CARD_ESCAPED_SLASH.sub("/", text[ca:cb])
    if not core:
        return "frame", a, a, 0
    if _CARD_CHECKSUM.fullmatch(core):
        return "key", ca, cb, 0
    if not _CARD_BASE64.fullmatch(core):
        return None, ca, cb, 0
    long_key = (len(core) >= _CARD_KEY_MIN and any(c.isupper() for c in core)
                and any(c.islower() for c in core) and _DIGIT.search(core) is not None)
    tail = len(core) <= _CARD_TAIL_MAX and core.endswith("=")
    if not (long_key or tail):
        return None, ca, cb, 0
    sort = "key" if long_key else "tail"
    return ("slash-" + sort if core.startswith("/") else sort), ca, cb, len(core)


def _card_items(text: str, a: int, b: int) -> list[list]:
    """A block's span as items [kind, start, end, (what, core start, core
    end)]: "head", "foot", "break", "armor" and "word"."""
    items: list[list] = []

    def line(lo: int, hi: int) -> None:
        stripped = text[lo:hi].strip(" \t")
        if stripped and _CARD_ARMOR.fullmatch(stripped):
            items.append(["armor", lo, hi, ("frame", lo, lo, 0)])
            return
        for w in _CARD_WORD.finditer(text, lo, hi):
            items.append(["word", w.start(), w.end(), _card_word(text, w.start(), w.end())])

    pos = a
    for m in _CARD_KEY_SPLIT.finditer(text, a, b):
        if m.start() > pos:
            line(pos, m.start())
        kind = "head" if m.group(1) is not None else "foot" if m.group(2) is not None else "break"
        items.append([kind, m.start(), m.end(), None])
        pos = m.end()
    if pos < b:
        line(pos, b)
    # Glued to a header (`private_key_spans` takes a token stuck to its
    # front): part of it.
    if (len(items) > 1 and items[0][0] == "word" and items[1][0] == "head"
            and items[0][2] == items[1][1]):
        items[:2] = [["head", items[0][1], items[1][2], None]]
    return items


def _card_counts(items) -> list[bool]:
    """For each item, whether it is key material or framing (True) or
    something a card must show (False). A "tail" counts only as the last
    word before a footer; a "slash" only at the width two or more of the
    block's body lines share (a `/` starts one base64 line in 64, and a
    path never is one)."""
    widths: dict[int, int] = {}
    for kind, a, b, what in items:
        if kind == "word" and what[0] == "key" and what[3]:
            widths[what[3]] = widths.get(what[3], 0) + 1
    width = max(widths, key=lambda n: (widths[n], n)) if widths else 0
    if widths.get(width, 0) < 2:
        width = 0
    out = []
    for i, (kind, a, b, what) in enumerate(items):
        if kind != "word":
            out.append(True)
            continue
        sort, n = what[0], what[3]
        final = False
        if sort in ("tail", "slash-tail"):
            j = i + 1
            while j < len(items) and (items[j][0] == "break" or (items[j][0] == "word" and items[j][3][0] == "frame")):
                j += 1
            final = j < len(items) and items[j][0] == "foot"
        if sort == "slash-key":
            ok = bool(width) and n == width
        elif sort == "slash-tail":
            ok = bool(width) and final and n <= width
        else:
            ok = sort in ("frame", "key") or (sort == "tail" and final)
        out.append(ok)
    return out


def _whole_key_block(text: str, items) -> bool:
    """Is every item of a block's span key material, framing or a break?"""
    return all(_card_counts(items))


def _card_key_spans(text: str, a: int, b: int) -> list[tuple[int, int]]:
    """What a card withholds of a private key's span `text[a:b]` (a block,
    an unterminated one, or a footer's tail; `private_key_spans`): the whole
    span when every word in it is key material or framing
    (`_whole_key_block`); else each header and footer, and the key-material
    words next to one (below a header, above a footer, across framing,
    breaks and armor lines), never a word that is anything else, nor any
    past it."""
    items = _card_items(text, a, b)
    if _whole_key_block(text, items):
        return [(a, b)]
    counts = _card_counts(items)
    # A line's words go together: a line holding one word that is not key
    # material is shown whole (`Ab3…Gh5 --install` keeps its program).
    line_ok, start = [True] * len(items), 0
    for i in range(len(items) + 1):
        if i == len(items) or items[i][0] != "word":
            ok = all(counts[start:i])
            for k in range(start, i):
                line_ok[k] = ok
            start = i + 1
    held = []
    for i, (kind, lo, hi, _what) in enumerate(items):
        if kind not in ("head", "foot"):
            continue
        held.append((lo, hi))
        step = 1 if kind == "head" else -1
        j = i + step
        while 0 <= j < len(items) and items[j][0] in ("word", "break", "armor"):
            if not (counts[j] and line_ok[j]):
                break
            if items[j][0] == "word" and items[j][3][0] != "frame":
                held.append(items[j][3][1:3])
            j += step
    out: list[list[int]] = []
    for lo, hi in sorted(set(held)):        # one marker for words only breaks apart
        if out and (lo <= out[-1][1] or _CARD_GAP.fullmatch(text, out[-1][1], lo) is not None):
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def card_spans(text: str, *, command: bool = True) -> list[tuple[int, int, str]]:
    """What an approval card withholds beyond `secrets.redact_command`, as
    spans of `text` as typed.

    - **A string the shell never runs** (a `Write`'s `content`, an `Edit`'s
      `new_string`): every display shape (the heuristic ones included) and
      labelled value, and a private key's whole block when every line of it
      is key material, quoted or concatenated lines and armor headers
      included (review of #314, M3); otherwise its header, footer and the
      key-material lines next to them, never another line nor any past it
      (`_card_key_spans`; third review, H and L5: a fake header and footer
      around code hid the code). Before this the key's body stayed on the
      card: `redact_command` takes a block only from inside one shell
      quoting, which plain text never has (review of PR #308).
    - **A command** (every string beside one: `map_strings`; a path; a
      card's command, reason and headline): the owner's condition for
      redacting cards (2026-10-15) is that a withheld value never hides
      code that runs. So (review of #314, H1): nothing at all on a line
      that may run text (rule 3, `secrets._run_names(...) is True`: `eval`,
      `source`, `bash -c`, a pipe into a shell …; `redact_command` marks
      its values `[shown: this value may be run]`); only the formats
      `credential_patterns` knows (`_CARD_COMMAND_KINDS`), found on the
      normalized view, and labelled values; never a span touching a
      command's word (`_command_words`); each only where the shell reads no
      structure (`secrets._Mask`, rule 1, with a non-ASCII space read as the
      word character bash reads it as); a private key left to
      `secrets._key_blocks`, a URL's userinfo to `strip_url_credentials`."""
    from jarvis.tools import secrets as S
    if not command:
        view = _View(text)
        found = []
        for va, vb, kind in _shape_spans(view.view, DISPLAY):
            for lo, hi in _card_key_spans(view.view, va, vb) if kind == KEY else [(va, vb)]:
                found.append((*view.span(lo, hi), kind))
        return _merge(found + _labelled_card_spans(text))
    if S._run_names(text) is True:
        return []
    view = _View(text)
    found = []
    for va, vb, kind in _shape_spans(view.view, DISPLAY):
        if kind not in _CARD_COMMAND_KINDS:
            continue
        joined = _JOIN_BREAKS.sub("", view.view[va:vb])
        if not any(p.search(joined) for p in _CARD_COMMAND_PATTERNS):
            continue
        a, b = view.span(va, vb)
        found.append((a, b, kind))
    found = _merge(found + _labelled_card_spans(text))
    if found:
        found = _clear_of(found, _command_words(text))
    if found:
        mask = S._Mask(_NOT_A_BLANK.sub("x", text))
        found = [s for s in found if mask.safe(s[0], s[1])]
    return found


def _clear_of(found, words) -> list[tuple[int, int, str]]:
    """The spans of `found` (sorted, disjoint) that touch no span of `words`,
    in one sweep over both sorted lists (review of #314, third review M3:
    every span against every word was 14 s on a 1 MB card)."""
    words = sorted(words)
    out, k, reach = [], 0, -1      # reach: the furthest end of the words starting before s's end
    for s in found:
        while k < len(words) and words[k][0] < s[1]:
            reach = max(reach, words[k][1])
            k += 1
        # A word starting before s's end overlaps s iff it ends past s's start.
        if reach <= s[0]:
            out.append(s)
    return out


def _labelled_card_spans(text: str) -> list[tuple[int, int, str]]:
    """Labelled values, each ending at the first character the shell could
    read as structure, in any argument (an argv under a key no rule names
    still never loses `;curl …|sh` to a marker; review of #314, B1)."""
    out = []
    for a, b, kind in labelled_spans(text):
        cut = _SHELL_CHAR.search(text, a, b)
        b = cut.start() if cut else b
        if b - a >= 6:
            out.append((a, b, kind))
    return out


class CardPass:
    """`card_spans` around `secrets.redact_command`/`redact_value`. The spans
    are found on the text as typed (`hide`), because `redact_command`'s own
    replacements would take away what they are found by (an access key id
    beside its secret, a split token's first piece). Each is held as a
    placeholder through `redact_command` and comes back as its marker
    (`show`); one that `redact_command` itself withheld (a placeholder as a
    `NAME=` value) stays its `[redacted]`. A placeholder never holds a shell
    character, and a span that would hold one is never taken (`card_spans`),
    so `redact_command` reads the same structure either way."""

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
        # One pass over the text (third review M3: one `replace` per held
        # span was O(spans x text)).
        held = re.compile("\ue000" + re.escape(self._nonce) + "[0-9]+\ue001")
        return held.sub(lambda m: withheld(self._kinds[m.group()]) if m.group() in self._kinds
                        else m.group(), text)


def map_strings(value, fn, *, command: bool = True, depth: int = 0):
    """Every string in a JSON-shaped `value` through `fn(text, command=…)`,
    structure kept. Every string inside a dict that holds a command or argv
    key (`_SHELL_KEYS`) is judged as a command, at any depth below it, and so
    is a path (`_PATH_KEYS`); any other as text, unless `command` says so for
    the top level."""
    if isinstance(value, str):
        return fn(value, command=command)
    if depth >= 32:
        return value
    if isinstance(value, dict):
        shell = command or any(isinstance(k, str) and k.lower() in _SHELL_KEYS for k in value)
        return {k: map_strings(v, fn, command=shell or (isinstance(k, str) and k.lower() in _PATH_KEYS),
                               depth=depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [map_strings(v, fn, command=command, depth=depth + 1) for v in value]
    return value


# The logged fields judged as a command's (`map_field`): a command or argv, a
# path, and the headline, which names what the card's command does (the
# sandbox widening's paths: review of #314, H1), as the card's own
# `card_text` judges it.
_FIELD_COMMAND_KEYS = _SHELL_KEYS | _PATH_KEYS | {"headline"}


def map_field(key: str, value, fn):
    """One logged field (`daemon._log_fields`) through `map_strings`: a dict
    (a tool's arguments) is judged by its own keys; a string or list under a
    command or argv key (`command`, `cmd`, an `args` argv), a path or the
    `headline` as a command; any other (`reason`) as text."""
    if isinstance(value, dict):
        return map_strings(value, fn, command=False)
    return map_strings(value, fn, command=isinstance(key, str) and key.lower() in _FIELD_COMMAND_KEYS)
