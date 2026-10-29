"""The phone's read API (phone surface design §5 and §6, work package P-3).

Every route here is under `/m/api/` on the remote listener only
(`remote_listener.py`, PH-1) and is reached only through
`remote_auth.api_route`, **after** `remote_auth.owner_status` has passed
(design §4 items 1 to 4: this daemon's remote listener, the exact Host, a
live session of an active passkey). Without a session every path here is 401
before this module is imported for it; with one, an unknown path is 404 and a
known one asked with anything but GET is 405. The session is checked again
once a read is built and before it is sent (`session_live`), so a logout or
a revoke during a slow read gets 401. Nothing here writes: answering,
sending and task verbs are P-4 and P-5.

    GET /m/api/inbox                    `inbox.rows`
    GET /m/api/projects                 `projects.project_view`, unarchived
    GET /m/api/threads?project=<id>     `Daemon.thread_json`, as `GET /threads`
    GET /m/api/threads/<id>/log?after=  `chat_reads.projected` over the log
    GET /m/api/tasks                    the task records, as `GET /tasks`
    GET /m/api/tasks/<id>               one task record
    GET /m/api/tasks/<id>/diff          `hud_api._diff_parts`, as the Diff tab
    GET /m/api/events                   the event stream (below)

**The same projections the HUD reads, minus what the phone does not get.**
Each route calls the function the HUD's own route calls and then keeps an
allowlist of fields (`*_FIELDS`); what the HUD sends beyond it is listed in
`DROPPED` with the reason, and `tests/v2/remote_api_check.py` holds the two
equal field by field, so a field the HUD gains later is a test failure
until it is decided here. In short: no filesystem path (a project's root,
a thread's cwd, a task's worktree, a task status's `last_file`), no
permission configuration (profiles, routing, the always-ask list, Bypass),
no other surface's addressing (Discord ids, an approval's 4-character code),
no provider session handle. The inbox is an allowlist too, every kind:
`ROW_FIELDS`, and per kind `APPROVAL_DETAIL_FIELDS` / `DETAIL_FIELDS`; a kind
P-3 does not know keeps only `GENERIC_DETAIL_FIELDS`, scalar values only.
And never what the HUD's owner-only routes withhold: no `owner_json`
(a private approval card is its private projection, `to_json`, exactly as
`inbox.rows` builds it), no Browser detail, no terminal output, no file
contents beyond the diff. **Every string a model or an agent could have
written passes `chat_reads.scrub`**, the transcript's own scrub (credentials
withheld, invisible characters out): titles, briefs, plans, reports, task
status, every inbox row's `title`, `blocks` and `origin`, and every `detail`
string, an approval card's included. The card's own redaction
(`approvals.card_text`/`card_args`) is not enough off the box: it missed a
private key's body in an argument (it takes it since 2026-10-25), and a
card's `origin` and `relayed_from.title` are only `clean_line`d (review of
PR #308). So the
phone's card can withhold more than the desk's; answering (P-4) must show
the full shared card before any button works.

**The transcript is `chat_reads`' projection** (the design's §5 row): the
owner's typed words, the assistant's replies, relays and system lines,
scrubbed at the source; tool events and their output, thinking, gates and
usage never leave. Each row carries `seq`, the index of the log record it
came from: `after=<seq>` resumes, and `next` is the cursor for the next
call. Without `after` the call is the newest `LOG_TAIL` rows (`older` says
whether there are more before them, which the phone does not page back to:
they are at the desk). A page holds at most `LOG_PAGE` rows and `LOG_BYTES`
of text; a row longer than `ROW_CHARS` is cut and says so (`cut`).

**The diff is capped** by files (`DIFF_FILES`) and bytes (`DIFF_FILE_BYTES`
per file, `DIFF_BYTES` in all): a file past either has no patch and
`too_large`, and the whole answer then says `truncated` and carries
`TOO_LARGE`, "open it at the desk". Its files are the Diff tab's, with the
same files skipped (protected, credential and out-of-project paths), and its
patches the Diff tab's (`hud_api._scrub_source`, the display scrub
`redact.for_display` since 2026-10-25) passed through the transcript's scrub
as well (`_patch_scrub`, which adds `secrets.redact_inline`'s typed
credentials).

**The event stream** (`GET /m/api/events`) is Server-Sent Events, as the
HUD's `/events` is (the design's `/m/ws`; the HUD has no WebSocket stream to
match, and under `/m/api` it inherits the 401 and the agent-path DENY). It
carries three kinds and nothing else, each ids and a source kind only:

    inbox_changed   {"ids": [...], "reason": "<source kind>"}   as the HUD's
    thread_changed  {"thread_id", "project_id", "reason"}       a log grew,
                                                                 a turn began
                                                                 or ended, a
                                                                 title changed
    task_changed    {"task_id", "project_id", "reason"}         a task was
                                                                 made or moved
    resync          {}                                          events were
                                                                 dropped: read
                                                                 again

No text of any kind: the phone reads the routes above again. Nothing is
sent for a thread or task of an archived project (`_Hidden`). A session is
needed to open it (as every `/m/api` path), and the stream ends when that
session does (logout, a revoke, idle or absolute expiry), checked before
every write and at least every `SESSION_CHECK_S`, without touching the
session's last-seen (`RemoteAuth.session_live`): an open stream is not
activity. At most `MAX_STREAMS` at once; a new one beyond that ends the
oldest. The daemon's shutdown ends them all.

SELF_PROTECTED: this module decides what the phone can read off the PC.
Tests: tests/v2/remote_api_check.py.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

LOG = logging.getLogger(__name__)

API = "/m/api/"
_ID = re.compile(r"[0-9a-f]{8}")
_EVENT_ID = re.compile(r"[A-Za-z0-9_:.\-]{1,200}")

# -- caps (pinned by tests/v2/remote_api_check.py) ----------------------------------
INBOX_ROWS = 200
PROJECTS = 200
THREADS = 300
TASKS = 200
LOG_TAIL = 60
LOG_PAGE = 200
LOG_BYTES = 512 * 1024
ROW_CHARS = 20000
TEXT_CHARS = 20000          # one text field of a task (a brief, a report line)
LIST_ITEMS = 200            # one list in a task (plan steps, report lines)
BRIEF_CHARS = 400           # a brief in the task list
DIFF_FILES = 200
DIFF_FILE_BYTES = 256 * 1024
DIFF_BYTES = 1024 * 1024
RESPONSE_BYTES = 4 * 1024 * 1024
MAX_STREAMS = 8
SESSION_CHECK_S = 1.0
PING_S = 15.0
RETRY_MS = 3000

TOO_LARGE = "Too large to show here: open it at the desk."
ROW_CUT = " … [cut: the rest is shown at the desk]"

# -- what the phone gets of each HUD projection -------------------------------------

PROJECT_FIELDS = ("id", "name", "created", "inbox", "archived")
THREAD_FIELDS = ("id", "project_id", "role", "provider", "task_id", "title", "title_source",
                 "created", "updated", "turns", "cost_usd", "tokens", "model", "effort",
                 "archived", "running", "compacting", "queued", "relays_waiting", "relays_out",
                 "running_since")
TASK_FIELDS = ("id", "project_id", "brief", "state", "created", "updated", "spec", "plan",
               "status", "report", "thread_ids", "branch", "provider_override", "ceilings")
TASK_LIST_FIELDS = ("id", "project_id", "brief", "state", "created", "updated", "status")
# A task's `status` (runner-written): every field but `last_file`, which is an
# absolute path into the task's worktree.
STATUS_FIELDS = ("phase", "step", "steps", "started", "elapsed_s", "cost_usd", "tokens",
                 "last_tool", "open_question", "routing")

# An inbox row, every kind: these fields and nothing else (review of #308:
# an allowlist, so a field `inbox.rows` gains later never reaches the phone
# unseen).
ROW_FIELDS = ("id", "kind", "rank", "title", "blocks", "project_id", "thread_id", "task_id",
              "asked_at", "expires_at", "origin", "detail")
# An approval row's `detail` is `ApprovalRequest.to_json()` (a private card's
# private projection, a subset of these); the phone keeps these keys of it,
# every string scrubbed, and drops `APPROVAL_DETAIL_DROPPED`.
APPROVAL_DETAIL_FIELDS = ("req_id", "tool", "args", "command", "reason", "layer", "thread_id",
                          "task_id", "provider", "origin", "asked_at", "allowlistable", "timeout_s",
                          "headline", "hud_only", "subagent", "relayed_from",
                          # AS: the code-built Where line, and the model's summary (judged apart).
                          "where", "summary", "summary_state")
# AS: a card's model summary and its state. They arrive after the card and
# are a model's words, so they are judged apart from it (`split_summary`):
# never part of what makes a card withheld, and dropped (state "failed")
# when the phone's scrub would change them.
SUMMARY_KEYS = ("summary", "summary_state")
APPROVAL_DETAIL_DROPPED = ("code", "discord_channel_id")
RELAYED_FIELDS = ("title", "provider", "thread_id", "kind")
# The other kinds' `detail` (`inbox._question_rows`, `_task_rows`).
DETAIL_FIELDS = {
    "question": ("thread_id", "req_id", "text", "options"),
    "task_question": ("task_id", "index", "text", "options"),
    "proposal": ("task_id", "brief", "withdraw_until"),
    "task": ("task_id", "state", "brief"),
    # A3 (inbox IN-3): an approval denied while the owner was away.
    "missed": ("req_id", "tool", "origin", "thread_id", "task_id", "at", "resolution"),
    # B1 (inbox IN-5): a decision an agent posted, shown read-only (the
    # question, options and why are agent text, through the phone's scrub).
    "decision": ("decision_id", "question", "options", "default", "why", "answer", "delivery"),
    # B2 (inbox IN-6): a live check, shown read-only (its title and steps
    # are agent or runbook text, through the phone's scrub). Only the HUD
    # marks a step; the phone has no route for it.
    "check": ("check_id", "title", "prs", "steps", "results", "done", "deploy"),
    # QH (quiet_defer.py): an approval deferred during quiet hours. Its card
    # is the public projection the record kept, scrubbed as a whole like an
    # approval's; Approve and retry needs a passkey (remote_answer).
    "deferred": ("deferral_id", "tool", "card", "thread_id", "task_id", "at", "until", "grantable",
                 "why_not", "expired", "hud_only", "count", "more", "summary"),
    # PM-26 (PR rows, `inbox_prs` and `pr_summary`): code's facts, flags and
    # actions, the model's summary (display only), the hold and the card an
    # OK binds; the whole detail through `scrub_structure`. `why`,
    # `summary_note` and `summary_reason` are code's words for the row (no
    # action, the summary's state), kept so the phone can say why. PM-E's
    # `fix` joins it later.
    "pr": ("repo", "number", "title", "branch", "base", "author", "fork", "head", "checks",
           "merge_state", "facts", "flags", "failing", "conflicts", "files", "summary",
           "summary_state", "summary_note", "summary_reason", "why", "method", "actions", "card",
           "hold"),
}
# A kind P-3 does not know (A3's `missed` rows, anything later): only these
# keys, only scalar values, every string scrubbed.
GENERIC_DETAIL_FIELDS = ("req_id", "tool", "origin", "thread_id", "task_id", "at", "resolution",
                         "state", "text", "brief", "reason")

# What the HUD's projection carries and the phone's does not, and why. The
# PR lists this table; the check compares each route against the HUD's minus
# exactly these.
DROPPED = {
    "project": {
        "root": "a filesystem path; the phone has no file surface",
        "root_real": "a filesystem path",
        "extra_dirs": "filesystem paths",
        "profile": "permission configuration stays at the desk (design §5)",
        "routing": "routing stays at the desk (design §5)",
        "always_ask": "the always-ask list stays at the desk (design §5)",
        "discord_channel_id": "another surface's addressing",
        "discord_channel_origin": "another surface's addressing",
    },
    "thread": {
        "provider_session_id": "a provider's resume handle for the session",
        "cwd": "a filesystem path",
        "surface": "another surface's addressing (Discord)",
        "discord": "another surface's addressing (Discord names and link)",
        "bypass": "the Bypass switch stays at the desk (design §5)",
        "migrated_from": "a v1 session id, internal",
        "provider_cost_total": "accounting internals",
        "cost_recount": "accounting internals",
    },
    "task": {
        "worktree": "a filesystem path",
        "root": "a filesystem path",
        "discord_thread_id": "another surface's addressing",
        "profile": "permission configuration stays at the desk (design §5)",
        "migrated_from": "a v1 goal id, internal",
    },
    "task_status": {
        "last_file": "an absolute path into the task's worktree",
    },
    "task_list": {
        "spec": "in the task's own read (GET tasks/<id>), not the list",
        "plan": "in the task's own read",
        "report": "in the task's own read",
        "thread_ids": "in the task's own read",
        "branch": "in the task's own read",
        "provider_override": "in the task's own read",
        "ceilings": "in the task's own read",
    },
    "pr_detail": {
        "url": "the GitHub link is the desk's way out (PM-25); the phone answers from "
               "Jarvis's own facts, never GitHub",
    },
    "approval_detail": {
        "code": "the 4-character code is an answer handle on another surface (Discord); "
                "the phone answers nothing in P-3, and P-4 answers by row id",
        "discord_channel_id": "another surface's addressing",
    },
}

# Bus kinds the stream turns into `thread_changed`: a record the transcript
# projection reads was written, a turn began or ended, or the thread itself
# changed. Nothing else with a thread id is sent (tool events, thinking,
# deltas, usage, sub-agents, plans, gates).
THREAD_KINDS = frozenset({
    "user_message", "text", "notice", "model_set", "cli_turn", "compacted", "error",
    "turn_started", "turn_finished", "queued_started", "queued_dropped", "steer_queued",
    "relay_in", "relay_out", "relay_reply", "relay_state", "relay_delivered",
    "thread", "thread_updated", "thread_created", "thread_moved", "thread_archived",
    "thread_restored", "thread_deleted", "thread_closed",
})
TASK_KINDS = frozenset({"task_created", "task_status_changed", "task_updated", "task_question"})
STREAM_KINDS = frozenset({"inbox_changed", "shutdown"}) | THREAD_KINDS | TASK_KINDS


class _Refused(Exception):
    def __init__(self, status: int, word: str, extra=()):
        super().__init__(word)
        self.status = status
        self.word = word
        self.extra = list(extra)


# A value written after a credential's label (review of PR #308):
# `aws_secret_access_key = …`, `password: …`, `"password": "…"`,
# `client_secret: …`, tuned to keep code and references. It lives in
# `redact` now (scrub hardening, 2026-10-25), shared by every **display**
# surface (`redact.for_display`: the desk's Diff tab, the transcript's
# `chat_reads.scrub`, the Discord mirror and reporter, approval cards) and
# still never by what an agent reads (`redact.for_model`). The phone keeps
# applying it here as well; on text `chat_reads.scrub` already passed it is a
# no-op.
from .redact import LABELLED, labelled_values  # noqa: E402,F401


def _scrub(text):
    """What the phone receives of a string: the transcript's scrub, then
    `labelled_values`."""
    from .chat_reads import scrub
    return labelled_values(scrub(text))


def split_summary(card: dict) -> tuple[dict, dict]:
    """AS: (the card without its summary, the summary part as the phone may
    be sent it). The summary is scrubbed by itself; one the scrub would
    change is dropped and its state becomes "failed" (the command then shows
    expanded). A card that never had a summary gets no summary part."""
    rest = {k: v for k, v in card.items() if k not in SUMMARY_KEYS}
    if "summary_state" not in card:
        return rest, {}
    state = card.get("summary_state") if isinstance(card.get("summary_state"), str) else ""
    summary = card.get("summary") if isinstance(card.get("summary"), str) else ""
    if summary and _scrub(summary) != summary:
        summary, state = "", "failed"
    if state not in ("pending", "ready", "failed", "off") or (state == "ready" and not summary):
        summary, state = "", "failed"
    return rest, {"summary": summary if state == "ready" else "", "summary_state": state}


def _scrub_tree(value, *, cap: int | None = None, items: int | None = None):
    """Every string in `value` through the transcript's scrub, a dict's keys
    included (review of PR #313: a credential can be an argument's key);
    containers kept, lists cut at `items` and strings at `cap` when given."""
    if isinstance(value, str):
        out = _scrub(value)
        return out if cap is None or len(out) <= cap else out[:cap] + "…"
    if isinstance(value, list):
        kept = value if items is None else value[:items]
        return [_scrub_tree(v, cap=cap, items=items) for v in kept]
    if isinstance(value, dict):
        return {(_scrub(k) if isinstance(k, str) else k): _scrub_tree(v, cap=cap, items=items)
                for k, v in value.items()}
    return value


# What a value nested past `secrets.redact_value`'s depth becomes on a card:
# what it hides was never judged (review of PR #313).
TOO_DEEP = "[nested too deep to judge]"
WITHHELD_TEXT = "[withheld: open it at the desk]"


def _contains(value, text: str) -> bool:
    if isinstance(value, str):
        return value == text
    if isinstance(value, dict):
        return any(_contains(k, text) or _contains(v, text) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains(v, text) for v in value)
    return False


def scrub_structure(value) -> tuple[object, bool]:
    """A card's structure as the phone may be sent it, and whether anything
    in it was withheld (review of PR #313). Three passes: every key and
    string by itself (`_scrub_tree`); then the whole as one JSON text, so a
    label in a key and its value in the string beside it (`{"password":
    "…"}`, a nested `Authorization` header) are read together as the scrub
    reads `password: …`; and a value nested past the card redaction's depth
    (`TOO_DEEP`) counts as withheld, since nothing judged it. When the JSON
    pass withholds something the per-string pass did not, the JSON pass's
    own result is sent; if that is not JSON any more, every string is
    replaced by `WITHHELD_TEXT`."""
    scrubbed = _scrub_tree(value)
    changed = scrubbed != value or _contains(value, TOO_DEEP)
    scrubbed, paired = _pairs(scrubbed)
    changed = changed or paired
    try:
        flat = json.dumps(scrubbed, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return _blank(scrubbed), True
    again = _scrub(flat)
    if again != flat:
        changed = True
        try:
            parsed = json.loads(again)
        except ValueError:
            parsed = None
        scrubbed = parsed if type(parsed) is type(scrubbed) else _blank(scrubbed)
    return scrubbed, changed


# PM-9's part names (`pr_facts.facts_parts`): code's own words, from this set.
PR_FACT_KEYS = frozenset({"pr", "state", "branch", "checks", "conflicts", "merge_state", "review",
                          "files", "head", "fork", "method", "read"})


def scrub_pr(detail: dict) -> tuple[dict, bool]:
    """PM-26: a PR row's detail as the phone may be sent it, and whether
    anything was withheld: `scrub_structure` over it all, with each facts
    part's `key` set aside first and put back after. That `key` is code's
    own word (`PR_FACT_KEYS`), but the pairs pass would read `key: branch`
    as a credential's label and withhold every part; one outside the set
    is withheld."""
    facts = detail.get("facts")
    keys = None
    if isinstance(facts, list):
        keys = [part.get("key") if isinstance(part, dict) else None for part in facts]
        detail = {**detail, "facts": [{k: v for k, v in part.items() if k != "key"}
                                      if isinstance(part, dict) else part for part in facts]}
    out, withheld = scrub_structure(detail)
    if keys is not None and isinstance(out, dict):
        parts = out.get("facts")
        if not isinstance(parts, list) or len(parts) != len(keys):
            return out, True
        known = [k if k in PR_FACT_KEYS else None for k in keys]
        withheld = withheld or None in known
        out["facts"] = [{"key": k or WITHHELD_TEXT, **part} if isinstance(part, dict) else part
                        for k, part in zip(known, parts)]
    return out, withheld


def _paired(key: str, item: str) -> tuple[str, bool]:
    """One string read with its key as a header or an assignment."""
    for sep in (": ", "="):
        line = key + sep + item
        read = _scrub(line)
        if read != line:
            return (read[len(key) + len(sep):] if read.startswith(key + sep) else WITHHELD_TEXT), True
    return item, False


def _pairs(value, key: str | None = None) -> tuple[object, bool]:
    """Each string read with its nearest key as a header or an assignment
    (`<key>: <value>`, `<key>=<value>`), as the scrub reads such a line: a
    nested `{"Authorization": "Bearer …"}` or `{"password": ["…"]}` is
    withheld as `Authorization: Bearer …` is. (value with what the reading
    withheld, whether anything was.)"""
    if isinstance(value, str):
        return _paired(key, value) if key and value else (value, False)
    if isinstance(value, list):
        out, changed = [], False
        for item in value:
            item, hit = _pairs(item, key)
            out.append(item)
            changed = changed or hit
        return out, changed
    if isinstance(value, dict):
        out, changed = {}, False
        for k, item in value.items():
            item, hit = _pairs(item, k if isinstance(k, str) else None)
            out[k] = item
            changed = changed or hit
        return out, changed
    return value, False


def _blank(value):
    if isinstance(value, str):
        return WITHHELD_TEXT
    if isinstance(value, list):
        return [_blank(v) for v in value]
    if isinstance(value, dict):
        return {k: _blank(v) for k, v in value.items() if isinstance(k, str)}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return WITHHELD_TEXT          # a number can be the secret too
    return value


def _query(handler, allowed: tuple[str, ...]) -> dict[str, str]:
    raw = urlsplit(getattr(handler, "path", "") or "").query
    try:
        parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=8)
    except ValueError:
        raise _Refused(400, "bad request") from None
    if any(len(v) != 1 for v in parsed.values()) or set(parsed) - set(allowed):
        raise _Refused(400, "bad request")
    return {k: v[0] for k, v in parsed.items()}


def _hidden(daemon) -> set:
    from .projects import archived_project_ids
    try:
        return archived_project_ids(daemon.stores)
    except Exception:  # noqa: BLE001 — as inbox.rows: nothing hidden rather than a 500
        return set()


def _get(store, object_id):
    """The record, or None for a malformed id, a missing or an unreadable one."""
    if not isinstance(object_id, str) or not _ID.fullmatch(object_id):
        return None
    try:
        return store.get(object_id)
    except Exception:  # noqa: BLE001
        return None


# -- the reads -------------------------------------------------------------------------


def _scalar(value) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def _detail(kind, detail: dict) -> dict:
    """A row's `detail` for the phone: the kind's allowlisted keys, every
    string through the transcript's scrub (an approval's too: `card_args`
    misses a private key's body in an argument, and `origin` and
    `relayed_from.title` are only `clean_line`d)."""
    if kind == "approval":
        out = {k: detail[k] for k in APPROVAL_DETAIL_FIELDS if k in detail}
        relayed = out.get("relayed_from")
        if relayed is not None:
            out["relayed_from"] = ({k: relayed.get(k) for k in RELAYED_FIELDS if _scalar(relayed.get(k))}
                                   if isinstance(relayed, dict) else None)
        # The whole card, keys and labels across fields too (`scrub_structure`),
        # then the read view's caps; the summary apart (AS).
        out, summary = split_summary(out)
        return {**_scrub_tree(scrub_structure(out)[0], cap=TEXT_CHARS, items=LIST_ITEMS), **summary}
    if kind == "deferred":
        out = {k: detail[k] for k in DETAIL_FIELDS[kind] if k in detail}
        if isinstance(out.get("card"), dict):
            out["card"] = {k: v for k, v in out["card"].items()
                           if k not in APPROVAL_DETAIL_DROPPED and k not in SUMMARY_KEYS}
        summary = out.pop("summary", "")
        summary = summary if isinstance(summary, str) and summary and _scrub(summary) == summary else ""
        return {**_scrub_tree(scrub_structure(out)[0], cap=TEXT_CHARS, items=LIST_ITEMS), "summary": summary}
    if kind == "pr":
        # PM-26: the whole detail through `scrub_structure` (`scrub_pr`),
        # then the caps.
        out = {k: detail[k] for k in DETAIL_FIELDS[kind] if k in detail}
        return _scrub_tree(scrub_pr(out)[0], cap=TEXT_CHARS, items=LIST_ITEMS)
    if kind in DETAIL_FIELDS:
        out = {k: detail[k] for k in DETAIL_FIELDS[kind] if k in detail}
        return _scrub_tree(out, cap=TEXT_CHARS, items=LIST_ITEMS)
    out = {k: detail[k] for k in GENERIC_DETAIL_FIELDS if k in detail and _scalar(detail[k])}
    return _scrub_tree(out, cap=TEXT_CHARS)


def inbox_row(row: dict) -> dict:
    """One `inbox.rows` row as the phone gets it: `ROW_FIELDS` only; `title`,
    `blocks` and `origin` scrubbed; `detail` as `_detail` keeps it. Any kind,
    including ones added after P-3 (A3's "missed" rows), served generically."""
    out = {k: row.get(k) for k in ROW_FIELDS}
    for key in ("title", "blocks", "origin"):
        out[key] = _scrub(out[key]) if isinstance(out[key], str) else ""
    for key in ("id", "kind", "project_id", "thread_id", "task_id", "asked_at", "expires_at"):
        if not _scalar(out[key]):
            out[key] = None
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    out["detail"] = _detail(row.get("kind"), detail)
    return out


def inbox(daemon) -> dict:
    from . import inbox as _inbox
    rows = _inbox.rows(daemon)
    return {"rows": [inbox_row(r) for r in rows[:INBOX_ROWS]], "more": len(rows) > INBOX_ROWS}


def project(view: dict) -> dict:
    return {k: view.get(k) for k in PROJECT_FIELDS}


def projects(daemon) -> dict:
    from .daemon import safe_list
    from .projects import project_view
    rows = [project_view(p) for p in safe_list(daemon.stores.projects) if not p.archived]
    return {"projects": [project(v) for v in rows[:PROJECTS]], "more": len(rows) > PROJECTS}


def thread(record: dict) -> dict:
    out = {k: record[k] for k in THREAD_FIELDS if k in record}
    if isinstance(out.get("title"), str):
        out["title"] = _scrub(out["title"])
    return out


def _stamp(value) -> str:
    return value if isinstance(value, str) else ""


def threads(daemon, query: dict) -> dict:
    from .daemon import safe_list
    stores = daemon.stores
    filters = {}
    hidden = _hidden(daemon)
    if "project" in query:
        found = _get(stores.projects, query["project"])
        if found is None or found.archived:
            raise _Refused(404, "not found")
        filters["project_id"] = found.id
    rows = [t for t in safe_list(stores.threads, **filters)
            if not t.archived and t.project_id not in hidden]
    rows.sort(key=lambda t: (_stamp(t.updated), t.id), reverse=True)
    return {"threads": [thread(daemon.thread_json(t)) for t in rows[:THREADS]],
            "more": len(rows) > THREADS}


def _visible_thread(daemon, thread_id):
    found = _get(daemon.stores.threads, thread_id)
    if found is None or found.archived or found.project_id in _hidden(daemon):
        raise _Refused(404, "not found")
    return found


def log(daemon, thread_id: str, query: dict) -> dict:
    from .chat_reads import projected
    found = _visible_thread(daemon, thread_id)
    after = None
    if "after" in query:
        text = query["after"]
        if not re.fullmatch(r"[0-9]{1,9}", text):          # ASCII digits only, never a superscript
            raise _Refused(400, "bad request")
        after = int(text)
    try:
        raw = daemon.stores.threads.read_log(found.id)
    except Exception:  # noqa: BLE001 — a log that cannot be read is not guessed at
        raise _Refused(409, "this chat's log cannot be read") from None
    records = [r if isinstance(r, dict) else {} for r in raw]
    rows = projected(records, found.provider)
    older = False
    if after is None:
        older = len(rows) > LOG_TAIL
        rows = rows[-LOG_TAIL:]
    else:
        rows = [(i, r) for i, r in rows if i >= after]
    out, used, more = [], 0, False
    for index, row in rows:
        if len(out) >= LOG_PAGE:
            more = True
            break
        text, cut = labelled_values(row.text), None
        if len(text) > ROW_CHARS:
            cut = len(text)
            text = text[:ROW_CHARS - len(ROW_CUT)] + ROW_CUT
        item = {"seq": index, "role": row.role, "text": text, "at": row.at, "marks": list(row.marks)}
        if cut is not None:
            item["cut"] = cut
        cost = len(text.encode("utf-8", "replace")) + 200
        if out and used + cost > LOG_BYTES:
            more = True
            break
        used += cost
        out.append(item)
    if more:
        cursor = out[-1]["seq"] + 1
    else:
        cursor = max(len(records), after or 0)
    return {"thread": thread(daemon.thread_json(found)), "rows": out, "next": cursor,
            "more": more, "older": older}


def _task_fields(record: dict, fields: tuple[str, ...]) -> dict:
    out = {k: record[k] for k in fields if k in record}
    status = out.get("status")
    if "status" in out:
        out["status"] = ({k: status[k] for k in STATUS_FIELDS if k in status}
                         if isinstance(status, dict) else {})
    return out


def task(record: dict) -> dict:
    return _scrub_tree(_task_fields(record, TASK_FIELDS), cap=TEXT_CHARS, items=LIST_ITEMS)


def task_summary(record: dict) -> dict:
    out = _scrub_tree(_task_fields(record, TASK_LIST_FIELDS), cap=TEXT_CHARS, items=LIST_ITEMS)
    brief = out.get("brief")
    if isinstance(brief, str) and len(brief) > BRIEF_CHARS:
        out["brief"] = brief[:BRIEF_CHARS] + "…"
    return out


def tasks(daemon) -> dict:
    from .daemon import safe_list
    from .model import to_json
    hidden = _hidden(daemon)
    rows = [t for t in safe_list(daemon.stores.tasks) if t.project_id not in hidden]
    rows.sort(key=lambda t: (_stamp(t.updated), t.id), reverse=True)
    return {"tasks": [task_summary(to_json(t)) for t in rows[:TASKS]], "more": len(rows) > TASKS}


def _visible_task(daemon, task_id):
    found = _get(daemon.stores.tasks, task_id)
    if found is None or found.project_id in _hidden(daemon):
        raise _Refused(404, "not found")
    return found


def one_task(daemon, task_id: str) -> dict:
    from .model import to_json
    return {"task": task(to_json(_visible_task(daemon, task_id)))}


def _patch_scrub(text: str) -> str:
    """A patch the Diff tab's scrub already passed, through the transcript's
    too (the off-box rule: `chat_reads.scrub` knows key shapes the v1 scrub
    does not), its final newline kept as `hud_api._scrub_source` keeps it."""
    out = _scrub(text)
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def diff(daemon, task_id: str) -> dict:
    """The Diff tab's files and patches (`hud_api._diff_parts`, scrubbed as
    `hud_api.diff` scrubs them), within the phone's caps."""
    from . import hud_api
    from .daemon import APIError
    found = _visible_task(daemon, task_id)
    try:
        context = hud_api._diff_context(daemon, found)
        root, _base, ancestor, _head, project_ = context
        paths = hud_api._diff_paths(root, ancestor)
        base, head, files, patches = hud_api._diff_parts(
            daemon, found, context=context, rows=paths[:DIFF_FILES],
            allowed=hud_api._diff_allowed(project_, root))
    except APIError as exc:
        raise _Refused(exc.status if exc.status in (403, 404, 409) else 409,
                       _scrub(str(exc))[:200]) from None
    except Exception as exc:  # noqa: BLE001 — git failed, the worktree went: not a 500
        LOG.info("phone diff unavailable: %s", type(exc).__name__)
        raise _Refused(409, "the diff cannot be read now") from None
    out, used, truncated = [], 0, len(paths) > DIFF_FILES
    for info, patch in zip(files, patches):
        item = dict(info)
        text = _patch_scrub(hud_api._scrub_source(patch))
        size = len(text.encode("utf-8", "replace"))
        if size > DIFF_FILE_BYTES or used + size > DIFF_BYTES:
            item["patch"] = None
            item["too_large"] = True
            truncated = True
        else:
            item["patch"] = text
            used += size
        out.append(item)
    result = {"base": base, "head": head, "files": out, "truncated": truncated}
    if truncated:
        result["note"] = TOO_LARGE
    return result


# -- the event stream ----------------------------------------------------------------


def stream_record(record) -> dict | None:
    """One bus record as the stream sends it, or None: ids and a kind only."""
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    data = record.get("data") if isinstance(record.get("data"), dict) else {}

    def ident(value):
        return value if isinstance(value, str) and _ID.fullmatch(value) else None

    if kind == "inbox_changed":
        ids = data.get("ids") if isinstance(data.get("ids"), list) else []
        reason = data.get("reason")
        return {"kind": "inbox_changed",
                "data": {"ids": [i for i in ids if isinstance(i, str) and _EVENT_ID.fullmatch(i)][:200],
                         "reason": reason if isinstance(reason, str) and _EVENT_ID.fullmatch(reason) else ""}}
    if kind in THREAD_KINDS:
        thread_id = ident(record.get("thread_id")) or ident(data.get("thread_id"))
        if thread_id is None:
            return None
        return {"kind": "thread_changed",
                "data": {"thread_id": thread_id, "project_id": ident(record.get("project_id")),
                         "reason": kind}}
    if kind in TASK_KINDS:
        task_id = ident(record.get("task_id")) or ident(data.get("task_id"))
        if task_id is None:
            return None
        return {"kind": "task_changed",
                "data": {"task_id": task_id, "project_id": ident(record.get("project_id")),
                         "reason": kind}}
    return None


# A project's archive, restore or delete: never sent, only a cue to read
# again which projects are archived.
PROJECT_KINDS = frozenset({"project_archived", "project_restored", "project_deleted", "project_updated"})
HIDDEN_TTL_S = 5.0
_LISTENED = STREAM_KINDS | PROJECT_KINDS


def _wanted(record) -> bool:
    # Runs inside the bus's publish: a set lookup, nothing that blocks.
    return isinstance(record, dict) and record.get("kind") in _LISTENED


class _Hidden:
    """The archived projects' ids for one stream, read again after a project
    event or every `HIDDEN_TTL_S`: no `thread_changed` or `task_changed` is
    sent for a thread or task of an archived project (the phone shows none)."""

    def __init__(self, daemon):
        self.daemon = daemon
        self.ids: set = set()
        self.read_at = None

    def stale(self) -> None:
        self.read_at = None

    def _project_of(self, data: dict):
        if data.get("project_id"):
            return data["project_id"]
        if data.get("thread_id"):
            found = _get(self.daemon.stores.threads, data["thread_id"])
        else:
            found = _get(self.daemon.stores.tasks, data.get("task_id"))
        return getattr(found, "project_id", None)

    def hides(self, shaped: dict) -> bool:
        if shaped["kind"] not in ("thread_changed", "task_changed"):
            return False
        now = time.monotonic()
        if self.read_at is None or now - self.read_at >= HIDDEN_TTL_S:
            self.ids, self.read_at = _hidden(self.daemon), now
        return self._project_of(shaped["data"]) in self.ids


class Streams:
    """The open streams of one remote listener: at most MAX_STREAMS, a new
    one beyond that ends the oldest."""

    def __init__(self):
        self._lock = threading.Lock()
        self._open: list[threading.Event] = []

    def add(self) -> threading.Event:
        stop = threading.Event()
        with self._lock:
            self._open.append(stop)
            while len(self._open) > MAX_STREAMS:
                self._open.pop(0).set()
        return stop

    def remove(self, stop: threading.Event) -> None:
        with self._lock:
            if stop in self._open:
                self._open.remove(stop)

    def count(self) -> int:
        with self._lock:
            return len(self._open)


_STREAMS_LOCK = threading.Lock()


def streams_of(server) -> Streams:
    with _STREAMS_LOCK:
        found = getattr(server, "phone_streams", None)
        if not isinstance(found, Streams):
            found = Streams()
            server.phone_streams = found
        return found


def events(handler, daemon, session: dict) -> None:
    """Serve the stream on this request until the session ends, the client
    goes, the daemon stops or a newer stream displaces it."""
    auth = handler.remote_auth
    digest = session["token_sha256"]
    registry = streams_of(handler.server)
    stop = registry.add()
    subscription = daemon.bus.subscribe(_wanted)
    dropped = subscription.dropped
    hidden = _Hidden(daemon)

    def live() -> bool:
        return not stop.is_set() and auth.session_live(digest)

    try:
        handler._cache = "no-store"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.end_headers()
        handler.wfile.write(f"retry: {int(RETRY_MS)}\n: connected\n\n".encode())
        handler.wfile.flush()
        quiet_since = time.monotonic()
        while live():
            try:
                record = subscription.get(timeout=SESSION_CHECK_S)
            except queue.Empty:
                record = None
            if record is not None and record.get("kind") == "shutdown":
                break
            if not live():
                break
            out = []
            if subscription.dropped != dropped:
                dropped = subscription.dropped
                out.append({"kind": "resync", "data": {}})
            if record is not None and record.get("kind") in PROJECT_KINDS:
                hidden.stale()
                record = None
            shaped = stream_record(record) if record is not None else None
            if shaped is not None and hidden.hides(shaped):
                shaped = None
            if shaped is not None:
                out.append(shaped)
            if out:
                handler.wfile.write(b"".join(
                    b"data: " + json.dumps(item, separators=(",", ":")).encode() + b"\n\n" for item in out))
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= PING_S:
                handler.wfile.write(b": ping\n\n")
                quiet_since = time.monotonic()
            handler.wfile.flush()
    except OSError:
        pass
    finally:
        daemon.bus.unsubscribe(subscription)
        registry.remove(stop)


# -- the route table ------------------------------------------------------------------


def _match(parts: list[str]):
    """(name, args) of a read route, or None."""
    if parts == ["inbox"]:
        return "inbox", ()
    if parts == ["projects"]:
        return "projects", ()
    if parts == ["threads"]:
        return "threads", ()
    if len(parts) == 3 and parts[0] == "threads" and parts[2] == "log":
        return "log", (parts[1],)
    if parts == ["tasks"]:
        return "tasks", ()
    if len(parts) == 2 and parts[0] == "tasks":
        return "task", (parts[1],)
    if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "diff":
        return "diff", (parts[1],)
    if parts == ["events"]:
        return "events", ()
    return None


QUERY = {"threads": ("project",), "log": ("after",)}


def route(handler, path: str, session: dict):
    """A `/m/api` request from a signed-in phone (`remote_auth.api_route` has
    already checked the session): (status, payload, extra headers), or None
    once the event stream has answered on the socket itself."""
    daemon = getattr(handler, "daemon", None)
    if not path.startswith(API) or daemon is None:
        return 404, {"error": "not found"}, []
    matched = _match(path[len(API):].split("/"))
    if matched is None:
        return 404, {"error": "not found"}, []
    name, args = matched
    if handler.command != "GET":
        return 405, {"error": "method not allowed"}, [("Allow", "GET")]
    try:
        query = _query(handler, QUERY.get(name, ()))
        if name == "events":
            events(handler, daemon, session)
            return None
        if name == "inbox":
            payload = inbox(daemon)
        elif name == "projects":
            payload = projects(daemon)
        elif name == "threads":
            payload = threads(daemon, query)
        elif name == "log":
            payload = log(daemon, args[0], query)
        elif name == "tasks":
            payload = tasks(daemon)
        elif name == "task":
            payload = one_task(daemon, args[0])
        else:
            payload = diff(daemon, args[0])
    except _Refused as exc:
        return exc.status, {"error": exc.word}, exc.extra
    # A read can take a moment (a long log, a large diff): the session must
    # still hold when the answer leaves, so a logout or a revoke meanwhile
    # gets the 401 it would have got a moment later.
    auth = getattr(handler, "remote_auth", None)
    if auth is None or not auth.session_live(session.get("token_sha256")):
        return 401, {"error": "unauthorized"}, []
    if len(json.dumps(payload)) > RESPONSE_BYTES:
        LOG.warning("phone: a %s answer passed its cap and was refused", name)
        return 413, {"error": TOO_LARGE}, []
    return 200, payload, []
