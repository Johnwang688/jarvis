"""Authorizing a dangerous tool from a Discord DM, when you are not at the PC.

The HUD card is the primary gate and stays exactly as it was. This is the
away-from-desk path: the same one-shot request, asked in the owner's DM
instead of (or as well as) the window, answered by replying YES / NO /
ALWAYS. It exists because a Discord-triggered turn was effectively read-only
— every dangerous tool denied for want of a human at the keyboard.

What holds it together, in order of how much it matters:

  1. **Only the owner can even be heard.** The gateway's should_respond()
     already drops everything that is not the owner's own message, so a
     stranger cannot reach this code at all. On top of that, an answer is
     only accepted in the *same DM channel the question was asked in* — a
     guild channel reply, even from the owner, is not an authorization.
  2. **The code is one-shot and per-request.** Each ask carries a 4-character
     code; the broker's request id underneath it can be resolved exactly
     once. An old "yes" cannot be replayed onto a later request, and with two
     questions outstanding an unqualified "yes" is refused rather than
     guessed.
  3. **Everything that is not a clear yes denies.** Unparseable replies do
     not resolve anything, the timeout denies, and a Discord that is not
     connected simply reports "not sent" so the broker falls back to its
     existing no-window denial.

The honest trade, worth stating plainly: approval used to require physical
access to the machine, and now it also accepts anyone holding the owner's
Discord account. That is the point of the feature — it is the owner's call —
but it is a real widening, and it is why the ask echoes the full command
being authorized rather than just naming the tool.
"""

from __future__ import annotations

import re
import secrets
import threading
from typing import Any

REMOTE_TIMEOUT_S = 600.0
"""Ten minutes. The HUD's 120s assumes you are sitting in front of it; this
one assumes you have to get your phone out."""

MAX_ARG_CHARS = 400  # per argument, inside Discord's 2000-char message cap

_ALLOW = {"yes", "y", "yeah", "yep", "ok", "okay", "allow", "approve", "approved", "go"}
_DENY = {"no", "n", "nope", "deny", "denied", "stop", "cancel", "nah"}
_ALWAYS = {"always"}
_CODE = re.compile(r"^[a-z0-9]{4}$")
_WORD = re.compile(r"[a-z0-9']+")


def _parse(text: str) -> tuple[str | None, str]:
    """-> (verdict, code). verdict is 'allow' | 'deny' | 'always' | None.

    Deliberately narrow: a reply has to *be* an answer, not merely contain
    one. "no, wait, actually yes" parses as nothing and gets asked again.
    """
    words = _WORD.findall((text or "").lower())
    if not words or len(words) > 2:
        return None, ""
    head = words[0]
    code = words[1] if len(words) > 1 else ""
    if code and not _CODE.match(code):
        return None, ""
    if head in _ALWAYS:
        return "always", code
    if head in _ALLOW:
        return "allow", code
    if head in _DENY:
        return "deny", code
    return None, ""


def _describe(item) -> str:
    lines = []
    for key, value in item.args.items():
        text = value if isinstance(value, str) else str(value)
        if len(text) > MAX_ARG_CHARS:
            text = text[:MAX_ARG_CHARS] + f"… [+{len(text) - MAX_ARG_CHARS} chars]"
        lines.append(f"  {key}: {text}")
    return "\n".join(lines) or "  (no arguments)"


class DiscordApprovals:
    """The broker's remote channel. Inert unless Discord is connected."""

    timeout_s = REMOTE_TIMEOUT_S

    def __init__(self, broker=None, announce=print, send=None, minutes: int = 10):
        self._broker = broker
        self._announce = announce
        # Injectable so the tests never touch the network.
        self._send = send
        self._minutes = minutes
        self._lock = threading.Lock()
        self._open: dict[str, dict[str, Any]] = {}

    def bind(self, broker) -> None:
        self._broker = broker

    def _dm(self, text: str) -> str:
        if self._send is not None:
            return self._send(text)
        from .tools.discord import dm_owner

        return dm_owner(text)

    def _new_code(self) -> str:
        with self._lock:
            taken = {rec["code"] for rec in self._open.values()}
        while True:
            code = secrets.token_hex(2)
            if code not in taken:
                return code

    # -- the asking side (called from the blocked agent thread) --------------

    def ask(self, item) -> bool:
        """DM the owner the request. False if it could not be delivered."""
        code = self._new_code()
        body = (
            f"**AUTHORIZATION NEEDED** · code `{code}`\n"
            f"`{item.tool}`\n```\n{_describe(item)}\n```\n"
            f"Reply **yes** to allow, **no** to deny, **always** to allow this "
            f"and stop asking. Expires in {self._minutes} minutes, and anything "
            f"else I hear counts as neither."
        )
        try:
            channel_id = self._dm(body)
        except Exception as exc:  # not connected, DMs closed, API down
            self._announce(f"[approval] Discord ask not sent: {type(exc).__name__}: {exc}")
            return False
        with self._lock:
            self._open[item.id] = {
                "code": code,
                "channel_id": str(channel_id),
                "tool": item.tool,
            }
        self._announce(f"[approval] asked the owner on Discord (code {code}): {item.tool}")
        return True

    def close(self, request_id: str, resolution: str) -> None:
        """The request is over. Say so in the DM if nobody there answered it."""
        with self._lock:
            record = self._open.pop(request_id, None)
        if record is None or resolution in ("approved", "approved-always", "denied"):
            return  # the answering reply already said what happened
        try:
            self._dm(
                f"That authorization for `{record['tool']}` (code "
                f"`{record['code']}`) is closed — {resolution}. Nothing ran."
            )
        except Exception:
            pass  # a failed courtesy note must not break the turn

    # -- the answering side (called from a gateway worker thread) ------------

    def handle_reply(self, channel_id: str, text: str) -> str | None:
        """Interpret a DM as an answer. None means "not for me, run a turn".

        Returning a string short-circuits the agent entirely, which is what
        keeps a stray reply from blocking on the Discord agent's lock while
        the turn that asked the question is still holding it.
        """
        with self._lock:
            mine = [
                (rid, dict(rec))
                for rid, rec in self._open.items()
                if rec["channel_id"] == str(channel_id)
            ]
        if not mine:
            return None

        verdict, code = _parse(text)
        pending = ", ".join(f"`{rec['code']}` ({rec['tool']})" for _, rec in mine)
        if verdict is None:
            return (
                f"Still waiting on: {pending}. Reply **yes**, **no**, or "
                f"**always**" + (" plus the code." if len(mine) > 1 else ".")
            )

        if code:
            mine = [pair for pair in mine if pair[1]["code"] == code]
            if not mine:
                return f"No open authorization with code `{code}`. Waiting on: {pending}."
        elif len(mine) > 1:
            # Two questions, one bare "yes" — guessing which is exactly the
            # mistake this whole design is built to avoid.
            return f"Which one? Reply with the code: {pending}."

        request_id, record = mine[0]
        allowed = verdict != "deny"
        if self._broker is None or not self._broker.resolve(
            request_id, allowed, always=(verdict == "always")
        ):
            with self._lock:
                self._open.pop(request_id, None)
            return f"That one (`{record['code']}`) already expired — nothing ran."
        with self._lock:
            self._open.pop(request_id, None)
        if not allowed:
            return f"Denied. `{record['tool']}` did not run."
        if verdict == "always":
            return f"Authorized `{record['tool']}`, and I will stop asking about this one."
        return f"Authorized. Running `{record['tool']}` now."

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._open)
