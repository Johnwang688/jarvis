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
adds to `secrets.scrub`). Both are pure functions of their input apart from
`secrets.scrub`'s file reads, so call them outside any lock that matters.
"""
from __future__ import annotations

import re

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


def scrub_text(text: str) -> str:
    """Every layer: Jarvis's own credential values, the known key shapes and
    the bearer shapes. Raises only if `secrets.scrub` does; a caller that
    cannot scrub must withhold the text, never show it unscrubbed."""
    from jarvis import credential_patterns
    from jarvis.tools import secrets
    out = secrets.scrub(str(text or ""))
    for _name, pattern in credential_patterns.PATTERNS:
        out = pattern.sub(REDACTED, out)
    return bearerish(out)
