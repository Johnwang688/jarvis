"""One scrub for model-written text bound for a surface (SV-2 review, 2026-10-15).

Three layers, because they cover different things:

- `secrets.scrub` knows the credential files Jarvis owns and the values in
  them (and the process's credential-named environment values);
- `credential_patterns.PATTERNS` knows the distinctive shapes of provider
  keys (`sk-…`, `ghp_…`, `AKIA…`, a PEM header …);
- the bearer shapes below were Claude's provider's own (`claude._safe`):
  anything that looks like an OAuth/API bearer (`sk-`/`oat`/`rt` + 12), a
  JWT, and a jarvis-mcp caller token (`callers.TOKEN_PREFIX`). Claude's CLI
  holds an OAuth bundle Jarvis does not own, and a child process's output or a
  model-written argument may carry one from anywhere.

`scrub_text` applies all three; `bearerish` only the last (what `_safe`
adds to `secrets.scrub`). Before the shapes, `scrub_text` takes a private
key's whole block (`private_key_blocks`, SEC-5): the shapes know only a
key's header and footer, and the base64 body between them is the secret.
Both are pure functions of their input apart from `secrets.scrub`'s file
reads, so call them outside any lock that matters.
"""
from __future__ import annotations

import re

from jarvis import credential_patterns

TOKENISH = re.compile(r"\b(?:sk|oat|rt)[-_][A-Za-z0-9_\-]{12,}", re.IGNORECASE)
JWTISH = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+")
# A jarvis-mcp caller token (`callers.TOKEN_PREFIX`, plan 2026-10-10 §1.1).
JMCPISH = re.compile(r"jmcp_[A-Za-z0-9_\-]{8,}")
REDACTED = "[redacted]"


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


def _whole_blocks(text: str) -> str:
    """Each header through the nearest private-key footer after it (of any
    kind: a mismatched label still ends the block), or through the end of the
    text when no footer follows, as one marker, with any run of `_GLUED`
    characters stuck to the header's front. Non-greedy, so two blocks stay
    two. Linear: every search starts where the last block ended."""
    out, pos = [], 0
    while (head := KEY_BEGIN.search(text, pos)) is not None:
        start = head.start()
        while start > pos and text[start - 1] in _GLUED:
            start -= 1
        out += [text[pos:start], REDACTED]
        foot = KEY_END.search(text, head.end())
        if foot is None:
            return "".join(out)
        pos = foot.end()
    out.append(text[pos:])
    return "".join(out)


def _footer_tails(text: str) -> str:
    """A footer left with no header (a read that starts inside a key) and the
    key material above it, as one marker. The rule, conservative on purpose:
    starting at the footer's own line (whose text before the footer must be
    empty or key material, else nothing changes here), walk up one line at a
    time while each line is `_KEY_LINE`, and stop at the first that is not,
    at the start of the text, or at the previous footer. That line and
    everything above it stay. A footer with no key material above it is left
    to the per-pattern pass, which replaces just the footer as it always has.
    Linear: no walk or line search crosses the previous footer's end, and a
    footer's own line is never key material (it holds a space)."""
    out, pos, floor = [], 0, 0
    for foot in KEY_END.finditer(text):
        start = foot.start()
        first = _line_start(text, start, floor)
        if first == start or _KEY_LINE.fullmatch(text, first, start):
            while first > floor:
                above = _line_start(text, first - 1, floor)
                if not _KEY_LINE.fullmatch(text, above, first - 1):
                    break
                first = above
            if first < start:
                out += [text[pos:first], REDACTED]
                pos = foot.end()
        floor = foot.end()
    out.append(text[pos:])
    return "".join(out)


def private_key_blocks(text: str) -> str:
    """`text` with every private key's whole block (header, body, footer) as
    one `[redacted]`: a terminated block, an unterminated one (to the end),
    and a footer-only tail (see `_footer_tails`). Text with no private-key
    header or footer comes back unchanged."""
    return _footer_tails(_whole_blocks(text))


def scrub_text(text: str) -> str:
    """Every layer: Jarvis's own credential values, private keys' whole
    blocks, the known key shapes and the bearer shapes. Raises only if
    `secrets.scrub` does; a caller that cannot scrub must withhold the text,
    never show it unscrubbed."""
    from jarvis.tools import secrets
    out = private_key_blocks(secrets.scrub(str(text or "")))
    for _name, pattern in credential_patterns.PATTERNS:
        out = pattern.sub(REDACTED, out)
    return bearerish(out)
