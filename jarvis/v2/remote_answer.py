"""Answering from the phone (phone surface design PH-4, PH-7, §4.1, §5;
work package P-4).

Every route here is under `/m/api/` on the remote listener and is reached
only through `remote_auth.api_route`, **after** `remote_auth.owner_status`
passed (design §4 items 1 to 4: this daemon's remote listener, the exact
Host, a live session of an active passkey, and for a write the exact
Origin). Nothing here is the HUD, and `projects.is_owner` is never true on
this listener.

    GET  /m/api/inbox/<id>/detail           an approval's card, to answer it
    POST /m/api/inbox/<id>/answer/options   {decision: "allow"|"always"}
    POST /m/api/inbox/<id>/answer           an answer, any answerable kind
    POST /m/api/inbox/decisions/<id>        a decision's answer (B1)

**Deny cheap, authorize deliberate, made physical (PH-4).** Deny
(`{decision: "deny"}`) needs the session only, on any card, a HUD-only one
included. Allow and Always each need their own passkey assertion: the phone
asks `answer/options` for a challenge bound to this approval, this exact
answer and this session (`RemoteAuth.answer_options`: single use, 60 s, a
pool of its own), the owner verifies on the phone (Face ID or the passcode),
and `answer` carries `{decision, assertion}`. The assertion is verified
(`RemoteAuth.answer_verify`: origin, rpId, the UV flag, the counter rule,
`crossOrigin` not true, the session's own passkey) **before** anything is
answered, and an assertion for Allow is never an Always, nor any other
card's.

**HUD authority only under §4.1 (PH-7, O-3).** After the assertion, every
§4.1 check runs again, in full (`remote_secure.SecureMonitor.check`, with
this session and this request). All pass: the answer carries
`owner_verified=True`, the HUD's authority: Always, and the HUD-only
(Browser) cards. Any fails ("limited"): Discord's authority. Allow on an
ordinary card still goes (`owner_verified=False`); Always is refused (409),
and a HUD-only card says "answer at the desk" (409). The only other caller
that may pass `owner_verified=True` is the HUD's own route.

**The owner never authorizes what they cannot fully see.** The card the
phone answers from is the HUD's own projection (`ApprovalRequest.to_json`,
or `owner_details` for a HUD-only card under §4.1), not P-3's capped read
(`remote_api` cut a command at 4,000 characters and hid the arguments).
But the phone's scrub (`remote_api._scrub`: `chat_reads.scrub` plus labelled
values) still applies to everything sent off the box. **If it would change
any field of the card, Allow and Always are refused** ("this card has
withheld text: answer it at the desk"): the phone is sent the scrubbed card
and may only Deny it. Checked at the detail, at the options and again at
the answer.

**A HUD-only card's private detail** (`owner_details`) is sent only by
`GET .../detail`, only while the card is pending, only to a signed-in
session, and only while every §4.1 check holds (fresh). It never enters the
inbox list (`remote_api.inbox`, the private projection), the event stream
(`remote_api.stream_record`, ids only), a push or a log line: nothing here
logs a card, an argument or an answer's text.

**Always shows its rule first.** `answer/options` for "always" returns the
exact entry `PendingApprovals.resolve(always=True)` would write
(`PendingApprovals.always_rule`, a dry run that writes nothing), and the
phone shows it before the passkey prompt. The entry is written only by the
verified answer.

**Questions, task questions and decisions** need the session only, as on
the HUD, through the inbox's own dispatch (`inbox.answer`, IN-2) with
`via="phone"`: a decision's answer reaches its chat as
`UserMessage(origin="owner", via="phone", desk=False)` with B1's framing,
never at the desk and never counted for Bypass; a chat question's log
record names "phone". A proposal's withdraw stays at the desk.

SELF_PROTECTED: it decides what the phone may answer, and with whose
authority. Tests: tests/v2/remote_answer_check.py.
"""
from __future__ import annotations

import logging

LOG = logging.getLogger(__name__)

API = "/m/api/"
WITHHELD = "this card has withheld text: answer it at the desk"
AT_DESK = "answer at the desk"
NO_ALWAYS = "this card takes no Always"
WITHDRAW_AT_DESK = "withdraw it at the desk"
DECISIONS = ("allow", "always", "deny")


class _Refused(Exception):
    def __init__(self, status: int, word: str, extra: dict | None = None):
        super().__init__(word)
        self.status = status
        self.word = word
        self.extra = extra or {}


def _match(parts: list[str]):
    if len(parts) == 3 and parts[0] == "inbox" and parts[1] == "decisions":
        return "decision", parts[2]
    if len(parts) == 3 and parts[0] == "inbox" and parts[2] == "detail":
        return "detail", parts[1]
    if len(parts) == 3 and parts[0] == "inbox" and parts[2] == "answer":
        return "answer", parts[1]
    if len(parts) == 4 and parts[0] == "inbox" and parts[2] == "answer" and parts[3] == "options":
        return "options", parts[1]
    return None


def _word(text) -> str:
    """A refusal's words from the daemon (code-written), scrubbed and capped
    as every phone string is."""
    from .remote_api import _scrub
    return _scrub(str(text))[:300]


# -- the card ---------------------------------------------------------------------


def _split(row_id: str):
    from . import inbox
    from .daemon import APIError
    try:
        return inbox._split(row_id)
    except APIError as exc:
        raise _Refused(exc.status, _word(exc)) from None


def _request(daemon, row_id: str):
    """The pending approval an `appr:` row names, or a refusal."""
    prefix, rest, _row_id = _split(row_id)
    if prefix != "appr":
        raise _Refused(400, "only an approval has a card")
    request = next((r for r in daemon.approvals.pending() if r.req_id == rest), None)
    if request is None:
        raise _Refused(409, "this item is no longer waiting; it was answered or its source has gone")
    return request


def phone_card(card: dict) -> tuple[dict, bool]:
    """The card as the phone may be sent it, and whether the phone's scrub
    changed anything: `card` minus another surface's handles
    (`remote_api.APPROVAL_DETAIL_DROPPED`), every string through the phone's
    scrub, nothing cut."""
    from .remote_api import APPROVAL_DETAIL_DROPPED, _scrub_tree
    shown = {k: v for k, v in card.items() if k not in APPROVAL_DETAIL_DROPPED}
    scrubbed = _scrub_tree(shown)
    return scrubbed, scrubbed != shown


def _private_card(daemon, request) -> dict:
    """`owner_details`: the HUD-only card's private detail, while pending."""
    try:
        return daemon.approvals.owner_details(request.req_id)
    except ValueError:
        raise _Refused(409, "this item is no longer waiting; it was answered or its source has gone") from None


def _monitor(daemon):
    monitor = getattr(daemon, "secure", None)
    if monitor is None:
        raise _Refused(409, "limited: " + "not checked yet")
    return monitor


def _limited(secure: dict) -> str:
    from .remote_secure import LABELS
    return "limited: " + LABELS.get(secure.get("failed"), str(secure.get("failed") or "a check failed"))


def _shown_secure(secure: dict) -> dict:
    from .remote_secure import LABELS
    failed = secure.get("failed")
    return {"ok": bool(secure.get("ok")), "failed": failed, "reason": secure.get("reason") or "",
            "label": LABELS.get(failed, "") if failed else "", "at": secure.get("at")}


def _authority(request, secure: dict, withheld: bool) -> tuple[dict, str]:
    ok = bool(secure.get("ok"))
    allow = not withheld and (ok or not request.private)
    always = (not withheld and ok and not request.private and request.allowlistable is not False)
    why = (WITHHELD if withheld else AT_DESK if request.private and not ok
           else _limited(secure) if not ok else "")
    return {"deny": True, "allow": allow, "always": always}, why


def rule_words(entry) -> str:
    """The rule an Always writes, in words (the entry itself is shown too)."""
    if not isinstance(entry, dict):
        return "No standing rule can be written for this card: Always allows it this once."
    tool = str(entry.get("tool") or "")
    if "prefix" in entry:
        return f"From now on, allow {tool} for any command whose stem is {entry['prefix']!r}, without asking."
    return f"From now on, allow every call of {tool}, without asking."


def detail(handler, daemon, session, row: str) -> dict:
    """`GET .../detail`: the card to answer from, with what the phone may
    do. A HUD-only card's private detail only under §4.1 (fresh)."""
    request = _request(daemon, row)
    secure = _monitor(daemon).check(session=session, handler=handler)
    if request.private:
        if not secure.get("ok"):
            raise _Refused(409, AT_DESK, {"secure": _shown_secure(secure)})
        card, withheld = phone_card(_private_card(daemon, request))
    else:
        card, withheld = phone_card(request.to_json())
    can, why = _authority(request, secure, withheld)
    return {"card": card, "private": bool(request.private), "withheld": withheld,
            "secure": _shown_secure(secure), "can": can, "why": why}


def _decision(body: dict, allowed=DECISIONS) -> str:
    value = body.get("decision")
    if not isinstance(value, str) or value not in allowed:
        raise _Refused(400, "decision must be one of " + ", ".join(allowed))
    return value


def options(handler, daemon, session, row: str, body: dict) -> dict:
    """`POST .../answer/options`: the challenge for one Allow or Always
    (PH-4), after the refusals that need no passkey; for Always, the rule."""
    decision = _decision(body, ("allow", "always"))
    if set(body) - {"decision"}:
        raise _Refused(400, "bad request")
    request = _request(daemon, row)
    secure = _monitor(daemon).check(session=session, handler=handler)
    _gate(daemon, request, decision, secure)
    out: dict = {"decision": decision}
    if decision == "always":
        try:
            entry, resolution = daemon.approvals.always_rule(request.req_id)
        except ValueError:
            raise _Refused(409, "this item is no longer waiting; it was answered or its source has gone") from None
        out.update(rule=entry, rule_words=rule_words(entry), resolution=resolution)
    out["publicKey"] = handler.remote_auth.answer_options(session, request.req_id, decision)
    return out


def _gate(daemon, request, decision: str, secure: dict) -> None:
    """The refusals for an Allow or an Always, in order: a HUD-only card
    while limited, Always while limited or on a card that takes none, and a
    card the phone's scrub would change."""
    ok = bool(secure.get("ok"))
    if request.private and not ok:
        raise _Refused(409, AT_DESK, {"secure": _shown_secure(secure)})
    if decision == "always":
        if not ok:
            raise _Refused(409, _limited(secure) + "; Always needs every check",
                           {"secure": _shown_secure(secure)})
        if request.private or request.allowlistable is False:
            raise _Refused(409, NO_ALWAYS)
    card = _private_card(daemon, request) if request.private else request.to_json()
    if phone_card(card)[1]:
        raise _Refused(409, WITHHELD)


def _dispatch(daemon, row_id: str, body: dict, *, owner_verified: bool) -> dict:
    """The inbox's own dispatch (IN-2), with the phone's `via`."""
    from . import inbox
    from .daemon import APIError, DaemonError
    try:
        return inbox.answer(daemon, row_id, body, owner_verified=owner_verified, via="phone")
    except APIError as exc:
        raise _Refused(exc.status, _word(exc)) from None
    except DaemonError as exc:
        raise _Refused(409, _word(exc)) from None


def answer(handler, daemon, session, row: str, body: dict) -> dict:
    """`POST .../answer`: one answer to one row."""
    row_id = row
    prefix, _rest, _ = _split(row_id)
    if prefix == "prop":
        raise _Refused(400, WITHDRAW_AT_DESK)
    if prefix != "appr":
        # A question, a task question, a decision: the session is enough,
        # as on the HUD. The dispatch checks the body's shape.
        return _dispatch(daemon, row_id, body, owner_verified=False)
    decision = _decision(body)
    if decision == "deny":
        if set(body) - {"decision"}:
            raise _Refused(400, "bad request")
        _request(daemon, row_id)
        return _dispatch(daemon, row_id, {"decision": "deny"}, owner_verified=False)
    if set(body) - {"decision", "assertion"}:
        raise _Refused(400, "bad request")
    assertion = body.get("assertion")
    if not isinstance(assertion, dict):
        raise _Refused(401, "an Allow or an Always needs the passkey")
    request = _request(daemon, row_id)
    # PH-4 first: the assertion, bound to this card and this answer.
    handler.remote_auth.answer_verify(session, assertion, request.req_id, decision)
    # Then every §4.1 check, in full, with this session and this request.
    secure = _monitor(daemon).check(session=session, handler=handler)
    _gate(daemon, request, decision, secure)
    owner_verified = bool(secure.get("ok"))
    out = _dispatch(daemon, row_id, {"decision": "allow", "always": decision == "always"},
                    owner_verified=owner_verified)
    return {**out, "authority": "hud" if owner_verified else "limited"}


def decision(handler, daemon, session, row: str, body: dict) -> dict:
    """`POST /m/api/inbox/decisions/<id>`: B1's own function, from the phone."""
    from . import inbox
    from .daemon import APIError
    try:
        return inbox.answer_decision(daemon, row, body, via="phone")
    except APIError as exc:
        raise _Refused(exc.status, _word(exc)) from None


def route(handler, path: str, session: dict, body: bytes):
    """(status, payload, extra headers) for one of this module's paths, or
    None for any other path (P-3's reads take it)."""
    if not path.startswith(API):
        return None
    matched = _match(path[len(API):].split("/"))
    if matched is None:
        return None
    name, row = matched
    method = handler.command
    want = "GET" if name == "detail" else "POST"
    if method != want:
        return 405, {"error": "method not allowed"}, [("Allow", want)]
    daemon = getattr(handler, "daemon", None)
    auth = getattr(handler, "remote_auth", None)
    if daemon is None or auth is None:
        return 404, {"error": "not found"}, []
    from .remote_auth import AuthError, _json_body
    try:
        if name == "detail":
            payload = detail(handler, daemon, session, row)
            # The detail may be private: it leaves only while the session holds.
            if not auth.session_live(session.get("token_sha256")):
                return 401, {"error": "unauthorized"}, []
            return 200, payload, []
        value = _json_body(body)
        if name == "options":
            return 200, options(handler, daemon, session, row, value), []
        if name == "answer":
            return 200, answer(handler, daemon, session, row, value), []
        return 200, decision(handler, daemon, session, row, value), []
    except _Refused as exc:
        return exc.status, {"error": exc.word, **exc.extra}, []
    except AuthError as exc:
        from .remote_auth import WORDS
        word = exc.word if exc.status in (409, 429, 503) else WORDS.get(exc.status, "unauthorized")
        return exc.status, {"error": word}, []
