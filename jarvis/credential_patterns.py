"""Credential formats with a distinctive shape — one list, for anything that
must recognise a secret by how it looks rather than by knowing its value.

Used by `terminal_read` (decisions W-2, rule 2: a read whose text holds any of
these is refused whole). Pure: regular expressions and nothing else, so the
list can be tested on its own with placeholder strings shaped like each
format — never real values, and the tests build them by concatenation so no
contiguous token ever sits in the repository.

What belongs here is a **prefix or framing nobody types by accident**:
`ghp_` + 36 characters, `AKIA` + 16, a PEM private-key header. A shape that
also fits ordinary text (a bare 40-character hex string, a UUID) does not —
those are the heuristic's business, and only beside a keyword. The decisions
list is the floor; a few equally distinctive formats are added (Hugging Face,
npm, GitLab, PyPI, Discord bot tokens, Stripe test keys), each in the safe
direction: a refusal is a read the owner can redo, a credential in a
transcript is a key to rotate.

**Not a boundary.** A secret with no recognisable shape — a random password
printed on its own — matches nothing here. The terminal's "Jarvis can read"
switch and the HUD note on every read are the backstops for that.
"""
from __future__ import annotations

import re

# A token must not continue an identifier on either side: `xsk-…` or a key
# glued to more key-ish characters is still found by the inner boundary, but a
# fragment of a longer word is not mistaken for a prefix.
_B = r"(?<![A-Za-z0-9_\-])"
_E = r"(?![A-Za-z0-9_\-])"

# (name, pattern). The name is for tests and for nothing else: a refusal never
# says which format matched (W-2: "the refusal never says what or where").
PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in (
        # OpenRouter (`sk-or-v1-…`), and the other `sk-…` provider keys
        # (OpenAI `sk-proj-…`/`sk-svcacct-…`/legacy `sk-…`, Anthropic
        # `sk-ant-…`, DeepSeek, Moonshot …). A digit is required so a long
        # hyphenated word that happens to start `sk-` is not a key.
        ("openrouter", _B + r"sk-or-[A-Za-z0-9_\-]{20,}"),
        ("sk-provider", _B + r"sk-(?=[A-Za-z0-9_\-]*[0-9])[A-Za-z0-9_\-]{20,}"),
        # GitHub: classic, OAuth, user-to-server, server-to-server, refresh;
        # and fine-grained personal access tokens.
        ("github", _B + r"gh[pousr]_[A-Za-z0-9]{36,}" + _E),
        ("github-pat", _B + r"github_pat_[A-Za-z0-9_]{22,}"),
        # AWS access key ids (long-term and temporary).
        ("aws-key-id", r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
        # Slack bot, app-user, user and refresh tokens.
        ("slack", _B + r"xox[abpr]-[A-Za-z0-9\-]{10,}"),
        # Google API keys.
        ("google-api", _B + r"AIza[0-9A-Za-z_\-]{35}" + _E),
        # Stripe secret and restricted keys, live and test.
        ("stripe", _B + r"(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{20,}"),
        # Any PEM/OpenSSH/PGP private key block — its header *and* its
        # footer (PKCS#8, RSA/EC/DSA, OPENSSH, ENCRYPTED, PGP … BLOCK), so a
        # read that starts below the header still sees the block end.
        ("private-key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
        ("private-key-end", r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
        # JWTs: a base64url JSON header (`eyJ` is `{"`), then two more
        # dot-separated segments (the signature may be empty for alg=none).
        ("jwt", _B + r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
        # Extras, each a fixed prefix.
        ("huggingface", _B + r"hf_[A-Za-z0-9]{30,}" + _E),
        ("npm", _B + r"npm_[A-Za-z0-9]{36}" + _E),
        ("gitlab", _B + r"glpat-[A-Za-z0-9_\-]{20,}"),
        ("pypi", _B + r"pypi-AgE[A-Za-z0-9_\-]{50,}"),
        # Discord bot tokens: base64 user id . timestamp . HMAC.
        ("discord-bot", _B + r"[MNO][A-Za-z0-9_\-]{23,27}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}"),
    )
)


def find(text: str) -> str | None:
    """The name of the first format `text` holds, or None. For tests: callers
    that refuse must not repeat the name to anyone."""
    for name, pattern in PATTERNS:
        if pattern.search(text):
            return name
    return None


def holds_credential(text: str) -> bool:
    """True if `text` holds anything shaped like a known credential."""
    return find(text) is not None
