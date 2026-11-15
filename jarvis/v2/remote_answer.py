"""Answering from the phone (phone surface design PH-4, PH-7, §4.1, §5;
work package P-4; the unlock window of WS-5c,
`docs/plans/2026-11-15-ws5-phone-working-set-plan.md` §5).

Every route here is under `/m/api/` on the remote listener and is reached
only through `remote_auth.api_route`, **after** `remote_auth.owner_status`
passed (design §4 items 1 to 4: this daemon's remote listener, the exact
Host, a live session of an active passkey, and for a write the exact
Origin). Nothing here is the HUD, and `projects.is_owner` is never true on
this listener.

    GET  /m/api/inbox/<id>/detail           an approval's card, to answer it
    GET  /m/api/inbox/<id>/answer/rule      the rule an Always would write, and its preview
    POST /m/api/inbox/<id>/answer           an answer, any answerable kind
    POST /m/api/inbox/decisions/<id>        a decision's answer (B1)

**Deny cheap, authorize deliberate (PH-4, as WS-5c amends it).** Deny
(`{decision: "deny"}`) needs the session only, on any card, a HUD-only one
included. Allow (`{decision: "allow", op}`) and Always (`{decision:
"always", preview, op}`) are gated actions: they need the session's unlock
window open (one passkey assertion opens it for an hour and every gated
action slides it: `RemoteAuth.require_unlocked`, 423 `locked_reply()` when
closed) and spend one server-issued op token (`remote_auth.gated`: a
duplicate is 409 and never runs again). The order is fixed: the body's
shape and the token's (400), the window (423, nothing else runs), the token
(410, 400), its admission (410, 409, 429), then every act-time check below
(409), then the answer.

**HUD authority only under §4.1 (PH-7, O-3).** At act time every §4.1 check
runs again, in full (`remote_secure.SecureMonitor.check`, with this session
and this request). All pass: the answer carries `owner_verified=True`, the
HUD's authority: Always, and the HUD-only (Browser) cards. Any fails
("limited"): Discord's authority. Allow on an ordinary card still goes
(`owner_verified=False`); Always is refused (409), and a HUD-only card says
"answer at the desk" (409). The only other caller that may pass
`owner_verified=True` is the HUD's own route.

**The owner never authorizes what they cannot fully see.** The card the
phone answers from is the HUD's own projection (`ApprovalRequest.to_json`,
or `owner_details` for a HUD-only card under §4.1), not P-3's capped read
(`remote_api` cut a command at 4,000 characters and hid the arguments).
But the phone's scrub (`remote_api._scrub`: `chat_reads.scrub` plus labelled
values) still applies to everything sent off the box. **If it would change
any field of the card, Allow and Always are refused** ("this card has
withheld text: answer it at the desk"): the phone is sent the scrubbed card
and may only Deny it. Checked at the detail, at the rule read and again at
the answer.

**A HUD-only card's private detail** (`owner_details`) is sent only by
`GET .../detail`, only while the card is pending, only to a signed-in
session, and only while every §4.1 check holds (fresh). It never enters the
inbox list (`remote_api.inbox`, the private projection), the event stream
(`remote_api.stream_record`, ids only), a push or a log line: nothing here
logs a card, an argument or an answer's text. A read: it works while
locked and never slides the window.

**Always shows its rule first, read without a challenge (WS-5c §5.4).**
`GET .../answer/rule` runs the checks Always's options ran (pending, §4.1,
`_gate`) and returns exactly `RULE_FIELDS`: the entry
`PendingApprovals.resolve(always=True)` would write (`always_rule`, a dry
run), keys only from `RULE_ENTRY_FIELDS`, its sentence, the resolution, and
`preview` (`preview_of`). An entry with another key, or a part the phone's
scrub would change, is shown only at the desk (409): never a partial rule.
Its own final `session_live` check holds before it leaves. The Always sends
`preview`; the server recomputes the rule at act time, and another digest is
409 (`RULE_CHANGED`). The preview binds the rule's freshness, not that a
human saw it: inside the window the session is the owner's authority.

**A deferred approval (QH, `quiet_defer.py`).** Leave it (`{action:
"leave"}`) needs the session only. Approve and retry (`{action: "retry",
op}`) is gated, bound to the deferral id in the URL; at act time the full
`_deferral` lookup runs (still waiting, grantable, under a day, its card
whole), then `_gate_deferral` (HUD-only while limited, a scrub-changed
card). No tag is added (the morning review's Approve selected keeps its
tags and its locked-store check: `review.py`). The grant itself is
`quiet_defer.answer` with `via="phone"` and `verified` true, passed by
`_answer_deferred` only, after admission.

**Questions, task questions and decisions** need the session only, as on
the HUD, through the inbox's own dispatch (`inbox.answer`, IN-2) with
`via="phone"`: a decision's answer reaches its chat as
`UserMessage(origin="owner", via="phone", desk=False)` with B1's framing,
never at the desk and never counted for Bypass; a chat question's log
record names "phone". **A proposal's withdraw** (P-5) needs the session
only, Deny's footing: it refuses work an agent proposed, inside the
proposal's grace window, through the inbox's own dispatch
(`TaskControl.cancel`, as `POST /tasks/<id>/cancel`), and it is recorded in
the task's journal as the phone's (`remote_write.record`). Cancelling a task
that already runs is `remote_write`'s, and is gated. A session-only answer
that lands slides an open window (`touch_unlock`) and never opens one.

SELF_PROTECTED: it decides what the phone may answer, and with whose
authority. Tests: tests/v2/remote_answer_check.py, tests/v2/remote_unlock_check.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)

API = "/m/api/"
WITHHELD = "this card has withheld text: answer it at the desk"
AT_DESK = "answer at the desk"
NO_ALWAYS = "this card takes no Always"
GONE = "this item is no longer waiting; it was answered or its source has gone"
DECISIONS = ("allow", "always", "deny")
# WS-5c §5.4: Always's rule as the phone may be sent it, and its digest.
RULE_FIELDS = ("rule", "rule_words", "resolution", "preview")
RULE_ENTRY_FIELDS = ("tool", "prefix")
RULE_AT_DESK = "this rule is shown only at the desk"
RULE_CHANGED = "the rule changed: read it again"
PREVIEW_DOMAIN = b"jarvis-phone-always-v1\x00"
_PREVIEW = re.compile(r"[0-9a-f]{64}")


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
    if len(parts) == 4 and parts[0] == "inbox" and parts[2] == "answer" and parts[3] == "rule":
        return "rule", parts[1]
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
    """The card as the phone may be sent it, and whether it is withheld:
    `card` minus another surface's handles
    (`remote_api.APPROVAL_DETAIL_DROPPED`), through the phone's scrub
    (`remote_api.scrub_structure`: every key and string, then the card as
    one JSON text, so a label in a key and its value beside it are read
    together), nothing cut. Withheld when any pass changed anything, or when
    a value was nested past the card redaction's depth and so never judged
    (review of PR #313). Withheld too when the card already shows a
    `[withheld: <kind>]` marker: since the scrub hardening (#314) the desk's
    own card pass (`approvals.card_text`/`card_args`, `redact.CardPass`)
    withholds much of what this scrub would, so the scrub then finds nothing
    left to change, yet the owner still cannot see what Allow would approve.
    A marker an agent typed itself only makes the card Deny-only."""
    import json
    from .redact import WITHHELD_PREFIX
    from .remote_api import APPROVAL_DETAIL_DROPPED, scrub_structure, split_summary
    shown = {k: v for k, v in card.items() if k not in APPROVAL_DETAIL_DROPPED}
    # AS-10: the model's summary is judged apart, never part of `withheld`:
    # it arrives after the card, and a model's words must not change what
    # the owner may answer. One the scrub would change is dropped.
    shown, summary = split_summary(shown)
    scrubbed, withheld = scrub_structure(shown)
    if not withheld:
        try:
            withheld = WITHHELD_PREFIX in json.dumps(shown, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 — a card that cannot be read is withheld
            withheld = True
    if summary and isinstance(scrubbed, dict):
        scrubbed = {**scrubbed, **summary}
    return scrubbed, withheld


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


def _deferral(daemon, row_id: str) -> tuple[str, dict]:
    """(its id, the record) of a `defer:` row that Approve and retry fits:
    waiting, under a day old, never a HUD-only card, its card whole and not
    changed by the phone's scrub. Else a refusal."""
    from . import quiet_defer
    _prefix, rest, _ = _split(row_id)
    store = getattr(getattr(daemon, "inbox", None), "deferred", None)
    record = store.get(rest) if store is not None else None
    if record is None or record["state"] != "waiting":
        raise _Refused(409, "this item is no longer waiting; it was answered or its source has gone")
    row = next((r for r in quiet_defer.rows(daemon, _row_builder()) if r["id"] == f"defer:{rest}"), None)
    if row is None or not row["detail"].get("grantable"):
        raise _Refused(409, "this item cannot be approved from here; it can only be left")
    if phone_card(record["card"])[1]:
        raise _Refused(409, WITHHELD)
    return rest, record


def _row_builder():
    from .inbox import _row
    return _row


def _gate_deferral(record: dict, secure: dict) -> None:
    """`_gate`'s refusals for an Allow, for a deferred card: a HUD-only one
    while limited (never granted anyway) and one the phone's scrub would
    change."""
    if record.get("hud_only") and not secure.get("ok"):
        raise _Refused(409, AT_DESK, {"secure": _shown_secure(secure)})
    if phone_card(record["card"])[1]:
        raise _Refused(409, WITHHELD)


# -- Always's rule, read without a challenge (WS-5c §5.4) ---------------------------


def preview_of(req_id: str, entry, resolution: str) -> str:
    """What an Always binds: sha256 of the domain, the request id, the
    canonical JSON of the **raw** entry and the resolution, 64 hex. It binds
    the rule's freshness (the rule written is the rule this request has
    now), not that a human saw it: it is public, and inside the unlock
    window the session is the owner's authority (§5.4, §9 item 2)."""
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    data = (PREVIEW_DOMAIN + req_id.encode("utf-8") + canonical.encode("ascii") + b"\x00"
            + str(resolution).encode("utf-8"))
    return hashlib.sha256(data).hexdigest()


def _rule(daemon, request) -> dict:
    """The rule an Always on `request` would write, as the phone may be sent
    it (`RULE_FIELDS`), or a refusal. An entry with any key outside
    `RULE_ENTRY_FIELDS`, a value that is not a string, or a `tool`, `prefix`,
    rule sentence or resolution the phone's scrub would change is shown only
    at the desk (409): never a partial rule."""
    from .remote_api import _scrub
    try:
        entry, resolution = daemon.approvals.always_rule(request.req_id)
    except ValueError:
        raise _Refused(409, GONE) from None
    if entry is not None:
        if (not isinstance(entry, dict) or set(entry) - set(RULE_ENTRY_FIELDS)
                or not all(isinstance(v, str) for v in entry.values())):
            raise _Refused(409, RULE_AT_DESK)
    words = rule_words(entry)
    shown = [words, resolution] + (list(entry.values()) if entry else [])
    if not isinstance(resolution, str) or any(_scrub(s) != s for s in shown):
        raise _Refused(409, RULE_AT_DESK)
    return {"rule": dict(entry) if entry else None, "rule_words": words, "resolution": resolution,
            "preview": preview_of(request.req_id, entry, resolution)}


def rule(handler, daemon, session, row: str) -> dict:
    """`GET .../answer/rule`: the rule an Always would write and its
    `preview`, after the checks Always's options made: the card pending,
    §4.1, and `_gate`'s refusals (private while limited, Always while limited,
    a card that takes none, a scrub-changed card). Session only: it works
    while locked and never slides the window. A deferred row takes no
    Always (409)."""
    if _split(row)[0] == "defer":
        raise _Refused(409, NO_ALWAYS)
    request = _request(daemon, row)
    secure = _monitor(daemon).check(session=session, handler=handler)
    _gate(daemon, request, "always", secure)
    shown = _rule(daemon, request)
    return {k: shown[k] for k in RULE_FIELDS}


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


def _catch(fn):
    """`fn()`'s answer, a refusal as its status: the act-time half of a
    gated action (`remote_auth.gated`). An `AuthError` (`Op.mutating`'s
    final check: 401, or `Locked`) passes through to the route's catch."""
    from .remote_auth import AuthError
    try:
        return fn()
    except _Refused as exc:
        return exc.status, {"error": exc.word, **exc.extra}, []
    except AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 — no 500, no exception text; it may have taken effect
        from .remote_write import UNCERTAIN
        LOG.exception("phone: an answer failed (%s)", type(exc).__name__)
        return 409, {"error": UNCERTAIN, "uncertain": True}, []


def answer(handler, daemon, session, path: str, row: str, body: dict):
    """`POST .../answer`: one answer to one row, as (status, payload,
    headers)."""
    from .remote_auth import gated, parse_op
    row_id = row
    prefix, _rest, _ = _split(row_id)
    if prefix == "prop":
        # P-5: a proposal's withdraw, the session only (Deny's footing).
        if set(body) != {"withdraw"} or body["withdraw"] is not True:
            raise _Refused(400, "withdraw must be true")
        from .remote_write import UNCERTAIN, record
        try:
            out = _dispatch(daemon, row_id, {"withdraw": True}, owner_verified=False)
        except _Refused:
            raise
        except Exception as exc:  # noqa: BLE001 — no 500, no exception text; it may have taken effect
            LOG.exception("phone: a withdraw failed (%s)", type(exc).__name__)
            raise _Refused(409, UNCERTAIN, {"uncertain": True}) from None
        record(daemon, _rest, "withdraw")
        return _session_only(handler, session, out)
    if prefix == "defer":
        return _answer_deferred(handler, daemon, session, path, row_id, body)
    if prefix != "appr":
        # A question, a task question, a decision: the session is enough,
        # as on the HUD. The dispatch checks the body's shape.
        return _session_only(handler, session, _dispatch(daemon, row_id, body, owner_verified=False))
    decision = _decision(body)
    if decision == "deny":
        if set(body) - {"decision"}:
            raise _Refused(400, "bad request")
        _request(daemon, row_id)
        return _session_only(handler, session,
                             _dispatch(daemon, row_id, {"decision": "deny"}, owner_verified=False))
    # Allow and Always: gated (WS-5c §5.3). Step 1, the body: Allow is exactly
    # {decision, op}; Always {decision, preview, op}.
    want = {"decision", "op"} | ({"preview"} if decision == "always" else set())
    if set(body) != want:
        raise _Refused(400, "bad request")
    preview = body.get("preview")
    if decision == "always" and (not isinstance(preview, str) or not _PREVIEW.fullmatch(preview)):
        raise _Refused(400, "preview must be the rule's 64-hex digest")
    op = parse_op(body["op"])

    def act(ticket):
        def run():
            request = _request(daemon, row_id)
            # Every §4.1 check, in full, with this session and this request.
            secure = _monitor(daemon).check(session=session, handler=handler)
            _gate(daemon, request, decision, secure)
            if decision == "always":
                # The rule this request has now must be the rule read (§5.4).
                shown = _rule(daemon, request)
                if not hmac.compare_digest(shown["preview"], preview):
                    raise _Refused(409, RULE_CHANGED)
            owner_verified = bool(secure.get("ok"))
            ticket.mutating()
            out = _dispatch(daemon, row_id, {"decision": "allow", "always": decision == "always"},
                            owner_verified=owner_verified)
            ticket.returned()
            ticket.result = "always_written" if decision == "always" else "allowed"
            return 200, {**out, "authority": "hud" if owner_verified else "limited"}, []
        return _catch(run)
    return gated(handler, session, path, body, op, "approval", act)


def _session_only(handler, session, out: dict):
    """A session-only answer that landed: it slides an open unlock window,
    never opening a closed one (WS-5c §5.1)."""
    handler.remote_auth.touch_unlock(session)
    return 200, out, []


def _answer_deferred(handler, daemon, session, path: str, row_id: str, body: dict):
    """QH: Leave it with the session; Approve and retry gated (WS-5c), bound
    to the deferral id in the URL, with the full `_deferral` lookup and
    `_gate_deferral` at act time."""
    from . import quiet_defer
    from .daemon import APIError
    from .remote_auth import gated, parse_op
    action = body.get("action")
    if action not in ("retry", "leave"):
        raise _Refused(400, "action must be retry or leave")
    _prefix, rest, _ = _split(row_id)
    if action == "leave":
        if set(body) - {"action"}:
            raise _Refused(400, "bad request")
        try:
            out = quiet_defer.answer(daemon, rest, {"action": "leave"}, via="phone", verified=False)
        except APIError as exc:
            raise _Refused(exc.status, _word(exc)) from None
        return _session_only(handler, session, out)
    if set(body) != {"action", "op"}:
        raise _Refused(400, "bad request")
    if not isinstance(rest, str) or not rest or "\n" in rest or ":" in rest:
        raise _Refused(400, "bad request")
    op = parse_op(body["op"])

    def act(ticket):
        def run():
            # Still waiting, grantable, under a day and whole; then every
            # §4.1 check and `_gate`'s refusals, as an Allow (#326 review, L5).
            record_id, _record = _deferral(daemon, row_id)
            secure = _monitor(daemon).check(session=session, handler=handler)
            _gate_deferral(_record, secure)
            verified = True
            ticket.mutating()
            try:
                out = quiet_defer.answer(daemon, record_id, {"action": "retry"}, via="phone",
                                         verified=verified)
            except APIError as exc:
                raise _Refused(exc.status, _word(exc)) from None
            ticket.returned()
            ticket.result = "allowed"
            return 200, out, []
        return _catch(run)
    return gated(handler, session, path, body, op, "approval", act)


def decision(handler, daemon, session, row: str, body: dict):
    """`POST /m/api/inbox/decisions/<id>`: B1's own function, from the phone."""
    from . import inbox
    from .daemon import APIError
    try:
        out = inbox.answer_decision(daemon, row, body, via="phone")
    except APIError as exc:
        raise _Refused(exc.status, _word(exc)) from None
    return _session_only(handler, session, out)


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
    want = "GET" if name in ("detail", "rule") else "POST"
    if method != want:
        return 405, {"error": "method not allowed"}, [("Allow", want)]
    daemon = getattr(handler, "daemon", None)
    auth = getattr(handler, "remote_auth", None)
    if daemon is None or auth is None:
        return 404, {"error": "not found"}, []
    from .remote_auth import AuthError, Locked, auth_reply, locked_reply, _json_body
    try:
        if name in ("detail", "rule"):
            if name == "rule" and urlsplit(getattr(handler, "path", "") or "").query:
                return 400, {"error": "bad request"}, []
            payload = (detail if name == "detail" else rule)(handler, daemon, session, row)
            # The detail may be private, and the rule is a card's: either
            # leaves only while the session holds.
            if not auth.session_live(session.get("token_sha256")):
                return 401, {"error": "unauthorized"}, []
            return 200, payload, []
        value = _json_body(body)
        if name == "answer":
            return answer(handler, daemon, session, path, row, value)
        return decision(handler, daemon, session, row, value)
    except _Refused as exc:
        return exc.status, {"error": exc.word, **exc.extra}, []
    except Locked:
        return locked_reply()
    except AuthError as exc:
        return auth_reply(exc)
