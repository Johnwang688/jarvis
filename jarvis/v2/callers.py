"""Per-session caller tokens: which chat is calling Jarvis through jarvis-mcp.

The peers plan's Phase 0, designed once (plan `docs/plans/2026-10-10-
terminal-read-mcp-plan.md` §1). An owner's chat thread on Claude or Codex that
is offered session tools gets one token for its daemon session, minted in
`Daemon._run_brief` and carried to the jarvis-mcp relay the CLI starts — never
on a command line (a file for Claude, the private `config.toml` for Codex).
The relay presents it to the bridge's `/mcp/*` routes as `Authorization:
Bearer jmcp_…`, and `authenticate` turns it back into the session.

What holds it together:

- **Only a digest is kept.** `CallerTokens` maps `sha256(token)` to a
  `Caller`; the raw token exists only in the run copy of the brief, the
  carrier file and the relay's environment. Nothing here can print one.
- **A token names a daemon session, not a thread.** `authenticate` requires
  `daemon._sessions.get(thread_id) is caller.session`, so even a revoke that
  was missed can never reach a newer session of the same thread.
- **Revoked on every way a session ends** (§1.4): `Daemon._cleanup` (close,
  archive, delete, a lost session), `_open`'s failure branch, and
  `Daemon.stop`, which calls `revoke_all` first. A restart forgets them all:
  they lived only in memory.
- **One refusal sentence.** Whatever is wrong — no header, a malformed one,
  an unknown or revoked token, a session that is not an owner's Claude or
  Codex chat, a browser's `Origin` — the answer is 403 `FORBIDDEN`, so a
  probe learns nothing about which part failed.

The /proc caveat (§1.8) stands: any program running as the owner can read a
live token from the carrier or the relay's environment. This guards against a
confused agent, not an adversary.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time

LOG = logging.getLogger(__name__)

TOKEN_PREFIX = "jmcp_"
# `secrets.token_urlsafe(32)`: 43 characters of the URL-safe alphabet.
TOKEN_RE = re.compile(r"jmcp_[A-Za-z0-9_\-]{43}")
FORBIDDEN = "not a Jarvis chat session"


@dataclass(frozen=True)
class Caller:
    thread_id: str
    session: object          # the daemon's `_Session`, compared by identity
    provider: str
    minted_at: float


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CallerTokens:
    """The daemon's live tokens, by digest. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_digest: dict[str, Caller] = {}

    def mint(self, session) -> str:
        """A new token for `session`, replacing any it already had."""
        thread = session.thread
        provider = getattr(thread.provider, "value", str(thread.provider))
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        digest = _digest(token)
        with self._lock:
            self._drop(session)
            self._by_digest[digest] = Caller(thread.id, session, provider, time.time())
        session.caller_key = digest
        return token

    def resolve(self, token) -> Caller | None:
        """The caller a token names, or None for anything else."""
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            return None
        with self._lock:
            return self._by_digest.get(_digest(token))

    def revoke(self, session) -> None:
        with self._lock:
            self._drop(session)
        if getattr(session, "caller_key", None) is not None:
            session.caller_key = None

    def revoke_all(self) -> None:
        with self._lock:
            sessions = [caller.session for caller in self._by_digest.values()]
            self._by_digest.clear()
        for session in sessions:
            try:
                session.caller_key = None
            except AttributeError:
                pass

    def live(self) -> int:
        """How many tokens are live. For tests and `/status`-style counts."""
        with self._lock:
            return len(self._by_digest)

    def _drop(self, session) -> None:
        """Under the lock: every token of `session` (one, by construction)."""
        for digest in [d for d, c in self._by_digest.items() if c.session is session]:
            del self._by_digest[digest]


def _bearer(handler) -> str | None:
    """The one bearer token in the request, or None for anything else: no
    header, two headers, another scheme, an empty token, two tokens."""
    values = handler.headers.get_all("Authorization") or []
    if len(values) != 1:
        return None
    parts = values[0].split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def authenticate(handler, daemon):
    """`(Caller, _Session)` for a request on a `/mcp/*` route, or 403.

    The single check every token route uses. Refused, with one sentence:
    a request carrying an `Origin` (a browser, never a relay), no or a
    malformed `Authorization`, an unknown or revoked token, a token whose
    session is no longer the thread's live one (closed, lost, resumed since),
    one that is closing, and a session that is not an owner's Claude or Codex
    chat. A session still opening is accepted: a CLI lists its MCP tools while
    it connects, before the daemon has its handle.
    """
    from .daemon import APIError
    from .model import ProviderName
    from . import thread_model
    if handler.headers.get("Origin") is not None:
        raise APIError(403, FORBIDDEN)
    registry = getattr(daemon, "callers", None)
    caller = registry.resolve(_bearer(handler)) if registry is not None else None
    if caller is None:
        raise APIError(403, FORBIDDEN)
    with daemon._lock:
        session = daemon._sessions.get(caller.thread_id)
        if (session is None or session is not caller.session or session.closing
                or session.retired or session.lost):
            raise APIError(403, FORBIDDEN)
        thread = session.thread
        if (not thread_model.is_chat(thread)
                or thread.provider not in (ProviderName.CLAUDE, ProviderName.CODEX)):
            raise APIError(403, FORBIDDEN)
    return caller, session


# -- the start-up sweep (§1.3) -------------------------------------------------

# A Claude session's carrier is `mcp-<32 hex>.json` (one per session, so a
# closing session never removes its successor's); `mcp.json` is the WP-1
# first round's per-thread name, and `.mcp-*` a write a crash interrupted.
_CLAUDE_CARRIER = re.compile(r"(?:mcp(?:-[0-9a-f]{32})?\.json|\.mcp-.+)")


def sweep_carriers(root: Path) -> int:
    """Remove token carriers a crash left under `root` (the daemon's data
    folder): every Claude session carrier under `claude/<thread>/`, and
    `mcp_servers` from every `codex/<thread>/config.toml`. Their tokens died
    with the process that minted them; the files would only keep a dead
    secret on disk. Called once this daemon holds its port — the bind is the
    single-instance lock — so it never touches a live daemon's carriers.
    Returns how many files changed. Never raises: a failure is a log line."""
    changed = 0
    root = Path(root)
    claude = root / "claude"
    if claude.is_dir() and not claude.is_symlink():
        for home in claude.iterdir():
            try:
                if home.is_symlink() or not home.is_dir():
                    continue
                for carrier in home.iterdir():
                    if _CLAUDE_CARRIER.fullmatch(carrier.name) and (
                            carrier.is_symlink() or carrier.is_file()):
                        carrier.unlink()
                        changed += 1
            except OSError as exc:
                LOG.warning("Cannot remove a leftover Claude MCP carrier (%s)", type(exc).__name__)
    codex = root / "codex"
    if codex.is_dir() and not codex.is_symlink():
        from .providers import codex_config
        for home in codex.iterdir():
            path = home / "config.toml"
            try:
                if home.is_symlink() or not home.is_dir() or path.is_symlink() or not path.is_file():
                    continue
                if codex_config.strip_mcp_servers(path):
                    changed += 1
            except Exception as exc:  # noqa: BLE001 — a sweep never stops a start
                LOG.warning("Cannot strip a leftover Codex MCP config (%s); removing it",
                            type(exc).__name__)
                try:
                    os.unlink(path)
                    changed += 1
                except OSError:
                    pass
    if changed:
        LOG.info("Removed %d leftover jarvis-mcp token carrier(s)", changed)
    return changed
