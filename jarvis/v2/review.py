"""The morning review (work package MR, `docs/plans/2026-10-28-morning-review.md`;
the owner's words of 2026-10-28: "i should be able to respond to each
question individually or as a group").

One report of everything that waited overnight, at the desk (`GET
/inbox/review`, the HUD's "Morning review" view) and on the phone (`GET
/m/api/review`, its Review tab). Computed on every read from the inbox's own
projection (`inbox.rows`, IN-1); nothing is stored.

**The route contract** (frozen in MR-A's first commit):

  HUD listener, `projects.owner_only` first (mounted by `inbox.route`):
    GET  /inbox/review                  200 the report (below), desk form
    POST /inbox/review/approve          {items: [{id, tag}]}, 1 to 10
                                        200 {ok, approved, results: [{ok, id, delivery, note, grant_until}]}
                                        400 {error}: malformed, a duplicate, more than 10
                                        409 {error, changed: [{id, reason}]}: nothing was approved
    POST /inbox/review/leave            {items: [{id}]}, 1 to 25
                                        200 {results: [{ok, id, delivery, note} | {ok: false, id, status, error}]}

  Remote listener, past `remote_auth.owner_status` (a session, the exact
  Host, and for a POST the exact Origin; mounted by `remote_auth.api_route`):
    GET  /m/api/review                  200 the report, phone form, with `group` and `opens_here`
    POST /m/api/review/approve/options  {items: [{id, tag}]}
                                        200 {publicKey, count}: a challenge bound to `subject(items)`
                                        409 {error, changed} (the first check) or {error: "limited: …", secure}
    POST /m/api/review/approve          {items: [{id, tag}], assertion}
                                        200 as the HUD's; 401 without or with a refused assertion;
                                        409 {error, changed} or limited; nothing written on any refusal
    POST /m/api/review/leave            {items: [{id}]}: as the HUD's, the session only

  An item's `id` is the inbox row id: `defer:<deferral id>`; Leave also
  takes an "and N more" row, `defer:more.<chat>`. A `tag` is 64 hex.

  The report: `{v: 1, now, quiet: {enabled, now, start, end}, summary:
  {approvals, questions, decisions, blocked, proposals, live}, live: [{row}],
  overnight: [item], before: [item], fyi: {count}, more, open, limits:
  {approve, leave, shown}, projects: [{id, name}], places: [{key, kind, id,
  project_id, title}]}`, plus on the phone `group: {ok, why}` and
  `opens_here`. An item is `{row, section, place, approve, leave, why,
  tag}`: `row` is the inbox row (on the phone, `remote_api.inbox_row`'s
  projection, scrubbed), `approve` whether it may be ticked for Approve
  selected here and now, `leave` for Leave selected, `why` a code when it
  may not be approved, `tag` only when it may.

**What it holds (MR-1).** `quiet.QuietHours.last_window` is the night (the
current quiet window, or the last one that ended). Overnight: rows at
ranks 1 to 4 and open decisions (rank 6) asked in it. Still waiting from
before: every other row at ranks 1 to 4 (with quiet hours off, this is all
there is). For your information: only how many rank-8 rows came in the
night. No PRs or checks. A live approval (rank 1) is "Live now", never
selectable (IN-3 unchanged for live cards). At most `SHOWN` items, then
"and N more in the Inbox".

**What a group approval binds (MR-6).** An approvable deferred item carries
`tag = HMAC-SHA256(key, "mr-v1" ‖ id ‖ digest ‖ sha256(canonical card))`,
`key` 32 random bytes made when this module is first imported in this
daemon's process and never written: the tag binds QH's exact digest and the
exact card shown, and reveals neither. A restart makes every tag stale. The
phone's assertion is bound to `subject(items)`: `defers:` and 56 hex of the
SHA-256 of `"jarvis-morning-review-v1\\n"` and the sorted `id\\ttag` lines,
inside `remote_auth._DEFER_SUBJECT`, Allow only. The group route always uses
it, even for one item; the per-row route (`remote_answer`) keeps
`defer:<id>`; neither takes the other's assertion.

**All or nothing (MR-7, the owner's answer 4).** Checked before a challenge
is issued (`check_group`), and again after the assertion and §4.1, under the
deferral store's lock (`quiet_defer.approve_many` → `settle_many`, with
`_check`): any item no longer waiting, past 24 h, whose tag no longer
matches (another card, another digest, or a restart), withheld by the
phone's scrub, HUD-only, whose task is no longer `blocked`, or whose chat is
closed → 409 `changed`, no grant written, nothing delivered. Then one
message per chat and one resume per task.

**Authority (MR-10, MR-11).** At the desk: `owner_only`, the HUD's own
confirmation dialog, no passkey (as QH's single desk Approve and retry). On
the phone: the session, Host and Origin, a fresh assertion bound to the
subject, and all five §4.1 checks passing; a limited phone gets no group
approval (the owner's answer 3), only the per-row Approve and retry. Leave
needs the session only. At most `APPROVE_MAX` (10, the owner's answer 2) per
Approve selected, `LEAVE_MAX` per Leave selected. The grants are QH's,
unchanged (MR-9): exact digest and thread, single use, 30 minutes.

SELF_PROTECTED (`jarvis/tools/files.py`): it decides which deferred calls an
owner's single confirmation grants. Tests: tests/v2/review_check.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import re
import secrets

LOG = logging.getLogger(__name__)

APPROVE_MAX = 10                # the owner's answer 2
LEAVE_MAX = 25
SHOWN = 60                      # items in the report; the rest are "in the Inbox"
TITLE_CAP = 120
SUBJECT_DOMAIN = "jarvis-morning-review-v1\n"
TAG_DOMAIN = b"mr-v1"
# The key every tag is made with: this process's own, never written (MR-6).
_KEY = secrets.token_bytes(32)

_ROW = re.compile(r"defer:([A-Za-z0-9_-]{1,64})")
_MORE = re.compile(r"defer:more\.([A-Za-z0-9_-]{1,200})")
_TAG = re.compile(r"[0-9a-f]{64}")

# Within a chat (MR-2): deferred approvals, questions, decisions, blocked
# tasks, proposals. The summary's kinds, by row kind.
KIND_ORDER = {"deferred": 0, "question": 1, "task_question": 1, "decision": 2, "task": 3, "proposal": 4}
SUMMARY = {"deferred": "approvals", "question": "questions", "task_question": "questions",
           "decision": "decisions", "task": "blocked", "proposal": "proposals", "approval": "live"}
# Why an item cannot be ticked for Approve selected, as codes the HUD and
# the phone put in their own words.
WHY = frozenset({"", "more", "gone", "answered", "not-grantable", "hud-only", "expired", "task-running",
                 "chat-closed", "no-chat", "withheld", "changed"})


class Bad(Exception):
    """A malformed body: 400, a fixed sentence."""


# -- the binding (MR-6) ----------------------------------------------------------------

def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def item_tag(record: dict) -> str:
    """HMAC-SHA256 under this process's key of "mr-v1", the deferral's id,
    its request digest and the SHA-256 of its card's canonical JSON, each
    delimited. 64 hex; reveals neither the digest nor the card."""
    card = hashlib.sha256(_canonical(record["card"])).hexdigest()
    message = b"\x00".join((TAG_DOMAIN, str(record["id"]).encode("utf-8"),
                            str(record["digest"]).encode("ascii"), card.encode("ascii")))
    return hmac.new(_KEY, message, hashlib.sha256).hexdigest()


def subject(items) -> str:
    """What a group's passkey assertion is bound to: `defers:` and the first
    56 hex of the SHA-256 of the domain line and the sorted `id\\ttag`
    lines. Order-free; another id or tag is another subject."""
    lines = sorted(f"{item_id}\t{tag}" for item_id, tag in items)
    digest = hashlib.sha256((SUBJECT_DOMAIN + "\n".join(lines)).encode("utf-8")).hexdigest()
    return "defers:" + digest[:56]


def parse_items(body, *, approve: bool, extra: tuple = ()) -> list[tuple[str, str | None]]:
    """`[(row id, tag or None)]` of `{items: [...]}` (plus the `extra` keys
    the route takes), or `Bad`: approve takes `{id, tag}` of a deferral, at
    most `APPROVE_MAX`; leave takes `{id}` of a deferral or an "and N more"
    row, at most `LEAVE_MAX`. No duplicate, nothing else."""
    if not isinstance(body, dict) or "items" not in body or set(body) - {"items", *extra}:
        raise Bad("bad request")
    items = body["items"]
    cap = APPROVE_MAX if approve else LEAVE_MAX
    if not isinstance(items, list) or not items:
        raise Bad("items must be a list of what you selected")
    if len(items) > cap:
        raise Bad(f"at most {cap} at once")
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise Bad("bad request")
        item_id = item.get("id")
        if approve:
            if set(item) != {"id", "tag"} or not isinstance(item_id, str) or not _ROW.fullmatch(item_id):
                raise Bad("bad request")
            tag = item["tag"]
            if not isinstance(tag, str) or not _TAG.fullmatch(tag):
                raise Bad("bad request")
        else:
            if set(item) != {"id"} or not isinstance(item_id, str) or not (
                    _ROW.fullmatch(item_id) or _MORE.fullmatch(item_id)):
                raise Bad("bad request")
            tag = None
        if item_id in seen:
            raise Bad("an item is selected twice")
        seen.add(item_id)
        out.append((item_id, tag))
    return out


# -- whether an item may be approved -------------------------------------------------

def _store(daemon):
    return getattr(getattr(daemon, "inbox", None), "deferred", None)


def _state_refusal(record: dict, now: datetime) -> str:
    """QH's own conditions: waiting, grantable, not HUD-only, under a day."""
    from . import quiet_defer
    if record["state"] != "waiting":
        return "answered"
    if record["hud_only"]:
        return "hud-only"
    if not record["grantable"] or record["digest"] is None:
        return "not-grantable"
    at = quiet_defer._parse(record["at"])
    if at is None or (now - at).total_seconds() >= quiet_defer.WAITING_S:
        return "expired"
    return ""


def _place_refusal(daemon, record: dict, *, phone: bool) -> str:
    """The morning review's own: a task's deferral only while the task is
    `blocked` (QH's Q2) and a runner is attached; a chat's only while the
    chat is open; on the phone, never a card the phone's scrub changes."""
    from . import quiet_defer
    from .model import TaskState
    if record["task_id"]:
        try:
            task = daemon.stores.tasks.get(record["task_id"])
        except Exception:  # noqa: BLE001
            task = None
        if task is None or task.state != TaskState.BLOCKED or getattr(daemon, "runner", None) is None:
            return "task-running"
    elif not record["thread_id"]:
        return "no-chat"
    elif quiet_defer.chat_closed(daemon, record["thread_id"]):
        return "chat-closed"
    if phone:
        from .remote_answer import phone_card
        if phone_card(record["card"])[1]:
            return "withheld"
    return ""


def refusal(daemon, record: dict, now: datetime, *, phone: bool) -> str:
    """Why this deferral cannot be ticked for Approve selected here, or ""."""
    return _state_refusal(record, now) or _place_refusal(daemon, record, phone=phone)


def _check(daemon, tags: dict[str, str], *, phone: bool):
    """`settle_many`'s check, under the store's lock: the tag the owner
    confirmed is this record's tag now, and `_place_refusal` holds."""
    def check(record):
        want = tags.get(record["id"])
        if not isinstance(want, str) or not hmac.compare_digest(item_tag(record), want):
            raise ValueError("changed")
        why = _place_refusal(daemon, record, phone=phone)
        if why:
            raise ValueError(why)
    return check


def check_group(daemon, items, *, phone: bool) -> list[dict]:
    """The first check (MR-7), before a challenge is issued: `[{id,
    reason}]` for each selected item that no longer fits, else []. Reads
    only; the answer checks again under the store's lock."""
    store = _store(daemon)
    if store is None:
        return [{"id": item_id, "reason": "gone"} for item_id, _tag in items]
    records = {r["id"]: r for r in store.all()}
    now = store.clock()
    changed = []
    for item_id, tag in items:
        record = records.get(item_id[len("defer:"):])
        if record is None:
            changed.append({"id": item_id, "reason": "gone"})
            continue
        why = refusal(daemon, record, now, phone=phone)
        if not why and not hmac.compare_digest(item_tag(record), tag or ""):
            why = "changed"
        if why:
            changed.append({"id": item_id, "reason": why})
    return changed


def approve(daemon, items, *, via: str, verified: bool = False) -> dict:
    """Approve selected: `quiet_defer.approve_many` with this module's
    check, ids and tags as the owner confirmed them. The result's `changed`
    names row ids."""
    from . import quiet_defer
    ids = [item_id[len("defer:"):] for item_id, _tag in items]
    tags = {item_id[len("defer:"):]: tag for item_id, tag in items}
    out = quiet_defer.approve_many(daemon, ids, via=via, verified=verified,
                                   check=_check(daemon, tags, phone=via == "phone"))
    if not out.get("ok") and out.get("changed"):
        out = dict(out, changed=[{"id": f"defer:{c['id']}", "reason": c["reason"]} for c in out["changed"]])
    return out


def leave(daemon, items, *, via: str) -> dict:
    """Leave selected: QH's Leave it per item, one result each."""
    from . import quiet_defer
    return {"results": [quiet_defer.leave(daemon, item_id[len("defer:"):], via=via) for item_id, _ in items]}


# -- the report (MR-1, MR-2) ---------------------------------------------------------------

def _when(value):
    from .inbox import _parse_time
    return _parse_time(value) if value else None


def _place(daemon, row: dict, places: dict, hidden: set) -> str:
    """The row's chat or task, recorded once in `places`: its key."""
    task_id, thread_id = row.get("task_id"), row.get("thread_id")
    if task_id:
        key, kind, ident = f"task:{task_id}", "task", task_id
    elif thread_id:
        key, kind, ident = f"chat:{thread_id}", "chat", thread_id
    else:
        key, kind, ident = "none", "none", None
    if key in places:
        return key
    project_id, title = row.get("project_id"), ""
    try:
        if kind == "task":
            task = daemon.stores.tasks.get(task_id)
            if task is not None:
                project_id = project_id or task.project_id
                title = task.brief if isinstance(task.brief, str) else ""
        elif kind == "chat":
            thread = daemon.stores.threads.get(thread_id)
            if thread is not None:
                project_id = project_id or thread.project_id
                title = thread.title if isinstance(thread.title, str) else ""
    except Exception:  # noqa: BLE001 — a place that cannot be read is shown by its id
        pass
    if project_id in hidden:
        project_id = None
    places[key] = {"key": key, "kind": kind, "id": ident, "project_id": project_id,
                   "title": " ".join(title.split())[:TITLE_CAP]}
    return key


def _secure_view(daemon) -> dict:
    monitor = getattr(daemon, "secure", None)
    if monitor is None:
        return {"ok": False, "why": "limited: not checked yet"}
    try:
        view = monitor.view()
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "limited: not checked yet"}
    if view.get("ok"):
        return {"ok": True, "why": ""}
    from .remote_answer import _limited
    return {"ok": False, "why": _limited(view)}


def report(daemon, *, phone: bool) -> dict:
    """The morning review, computed now (MR-1, MR-2)."""
    from . import inbox as _inbox
    from .projects import archived_project_ids
    quiet = daemon.quiet
    now = quiet.clock()
    try:
        window = quiet.last_window(now)
        quiet_now = quiet.is_quiet(now)
    except Exception as exc:  # noqa: BLE001 — a night that cannot be read: everything is "before"
        LOG.warning("Cannot read the quiet window for the morning review (%s)", type(exc).__name__)
        window, quiet_now = None, False
    try:
        hidden = archived_project_ids(daemon.stores)
    except Exception:  # noqa: BLE001
        hidden = set()
    rows = _inbox.rows(daemon, with_checks=False)
    store = _store(daemon)
    records = {r["id"]: r for r in store.all()} if store is not None else {}
    store_now = store.clock() if store is not None else datetime.now(timezone.utc)
    live, overnight, before, fyi = [], [], [], 0
    for row in rows:
        at = _when(row.get("asked_at"))
        inside = window is not None and at is not None and window[0] <= at < window[1]
        rank, kind = row.get("rank"), row.get("kind")
        if rank == 1 and kind == "approval":
            live.append(row)
        elif rank in (1, 2, 3, 4):
            (overnight if inside else before).append(row)
        elif kind == "decision" and rank == 6 and inside:
            overnight.append(row)
        elif rank == 8 and inside and kind not in ("check", "pr", "pr_unavailable"):
            fyi += 1
    places: dict = {}
    projects: dict = {}

    def shown(row):
        if not phone:
            return row
        from .remote_api import inbox_row
        return inbox_row(row)

    def item(row, section):
        out = {"row": shown(row), "section": section, "place": _place(daemon, row, places, hidden),
               "approve": False, "leave": False, "why": "", "tag": None}
        if row.get("kind") == "deferred":
            out["leave"] = True
            rid = row["id"][len("defer:"):]
            record = records.get(rid)
            why = ("more" if rid.startswith("more.") else "gone") if record is None else \
                refusal(daemon, record, store_now, phone=phone)
            if why:
                out["why"] = why
            else:
                out["approve"], out["tag"] = True, item_tag(record)
        return out
    waiting = [(row, "overnight") for row in overnight] + [(row, "before") for row in before]
    items = [item(row, section) for row, section in waiting[:SHOWN]]
    summary = {name: 0 for name in ("approvals", "questions", "decisions", "blocked", "proposals", "live")}
    for row in live + overnight + before:
        name = SUMMARY.get(row.get("kind"))
        if name:
            summary[name] += 1
    for place in places.values():
        pid = place["project_id"]
        if pid and pid not in projects:
            try:
                project = daemon.stores.projects.get(pid)
            except Exception:  # noqa: BLE001
                project = None
            projects[pid] = {"id": pid, "name": getattr(project, "name", "") or pid}
    out = {"v": 1, "now": now.isoformat(timespec="seconds"),
           "quiet": {"enabled": window is not None, "now": bool(quiet_now),
                     "start": window[0].isoformat(timespec="minutes") if window else None,
                     "end": window[1].isoformat(timespec="minutes") if window else None},
           "summary": summary, "live": [{"row": shown(row)} for row in live],
           "overnight": [i for i in items if i["section"] == "overnight"],
           "before": [i for i in items if i["section"] == "before"],
           "fyi": {"count": fyi}, "more": max(0, len(waiting) - SHOWN), "open": len(waiting),
           "limits": {"approve": APPROVE_MAX, "leave": LEAVE_MAX, "shown": SHOWN},
           "projects": list(projects.values()), "places": list(places.values())}
    if phone:
        from .remote_api import _scrub
        for project in out["projects"]:
            project["name"] = _scrub(project["name"])
        for place in out["places"]:
            place["title"] = _scrub(place["title"])
        out["group"] = _secure_view(daemon)
        push = getattr(daemon, "push", None)
        opens = getattr(push, "review_opens", None)
        out["opens_here"] = bool(callable(opens) and opens() and out["open"] > 0)
    return out


# -- the HUD's routes (MR-10) ----------------------------------------------------------------

def hud_route(handler, daemon, parts, query):
    """`/inbox/review...` on the HUD listener, after `inbox.route`'s
    `owner_only`; None for any other path."""
    if parts[:2] != ["inbox", "review"]:
        return None
    from .daemon import APIError, _object
    _object(query, ())
    method = handler.command
    if parts == ["inbox", "review"] and method == "GET":
        return 200, report(daemon, phone=False)
    if parts in (["inbox", "review", "approve"], ["inbox", "review", "leave"]) and method == "POST":
        approving = parts[2] == "approve"
        try:
            items = parse_items(handler._body(), approve=approving)
        except Bad as exc:
            raise APIError(400, str(exc)) from None
        if not approving:
            return 200, leave(daemon, items, via="inbox")
        out = approve(daemon, items, via="inbox")
        if not out["ok"]:
            return out["status"], {k: v for k, v in out.items() if k in ("error", "changed")}
        return 200, out
    raise APIError(404, "route not found")


# -- the phone's routes (MR-11) --------------------------------------------------------------

API = "/m/api/review"
OPTIONS = API + "/approve/options"
APPROVE = API + "/approve"
LEAVE = API + "/leave"
ROUTES = {API: "GET", OPTIONS: "POST", APPROVE: "POST", LEAVE: "POST"}
GROUP_LIMITED = "; Approve selected needs every check: approve one at a time"


def _limited_refusal(secure: dict) -> tuple[int, dict]:
    from .remote_answer import _limited, _shown_secure
    return 409, {"error": _limited(secure) + GROUP_LIMITED, "secure": _shown_secure(secure)}


def route(handler, path: str, session: dict, body: bytes):
    """A signed-in phone's review routes (after `remote_auth.owner_status`),
    as (status, payload, extra headers); None for any other path."""
    want = ROUTES.get(path)
    if want is None:
        return None
    method = handler.command
    if method != want:
        return 405, {"error": "method not allowed"}, [("Allow", want)]
    daemon = getattr(handler, "daemon", None)
    auth = getattr(handler, "remote_auth", None)
    if daemon is None or auth is None:
        return 404, {"error": "not found"}, []
    from .remote_auth import WORDS, AuthError, _json_body
    try:
        if path == API:
            payload = report(daemon, phone=True)
            # The report leaves only while the session holds (as every read).
            if not auth.session_live(session.get("token_sha256")):
                return 401, {"error": "unauthorized"}, []
            return 200, payload, []
        value = _json_body(body)
        if path == LEAVE:
            return 200, leave(daemon, parse_items(value, approve=False), via="phone"), []
        if path == OPTIONS:
            items = parse_items(value, approve=True)
            changed = check_group(daemon, items, phone=True)
            if changed:
                from .quiet_defer import CHANGED
                return 409, {"error": CHANGED, "changed": changed}, []
            # A limited phone gets no group approval (the owner's answer 3).
            secure = _monitor_check(daemon, session, handler)
            if not secure.get("ok"):
                status, payload = _limited_refusal(secure)
                return status, payload, []
            return 200, {"count": len(items),
                         "publicKey": auth.answer_options(session, subject(items), "allow")}, []
        items = parse_items(value, approve=True, extra=("assertion",))
        assertion = value.get("assertion")
        if not isinstance(assertion, dict):
            return 401, {"error": "Approve selected needs the passkey"}, []
        # The assertion first, bound to exactly this set (recomputed from the body).
        auth.answer_verify(session, assertion, subject(items), "allow")
        seen = getattr(daemon, "owner_seen", None)
        if callable(seen):
            seen()                      # QH-3: a verified group approval is presence
        # Then every §4.1 check, in full; then the second check and the grants.
        secure = _monitor_check(daemon, session, handler)
        if not secure.get("ok"):
            status, payload = _limited_refusal(secure)
            return status, payload, []
        out = approve(daemon, items, via="phone", verified=True)
        if not out["ok"]:
            return out["status"], {k: v for k, v in out.items() if k in ("error", "changed")}, []
        return 200, out, []
    except Bad as exc:
        return 400, {"error": str(exc)}, []
    except AuthError as exc:
        word = exc.word if exc.status in (409, 429, 503) else WORDS.get(exc.status, "unauthorized")
        return exc.status, {"error": word}, []


def _monitor_check(daemon, session, handler) -> dict:
    """§4.1 in full, with this session and this request; no monitor is limited."""
    monitor = getattr(daemon, "secure", None)
    if monitor is None:
        return {"ok": False, "failed": "pending", "reason": "not checked yet"}
    return monitor.check(session=session, handler=handler)
