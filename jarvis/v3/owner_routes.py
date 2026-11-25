"""The v3 HUD's exact routes and browser owner identity rule."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from email import errors as header_errors
from functools import partial
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import time
from typing import TYPE_CHECKING

from jarvis import config

from .approvals import ApprovalConflict
from .chat import ChatBusy, ChatInboxFull

if TYPE_CHECKING:
    from .chat import ChatService
    from .feed import Feed
    from .work import Ledger, Registry


MAX_BODY = 64 * 1024
FEED_LOCK_TIMEOUT = 2.0
WORK_THREAD_CAP = 50
_MALFORMED_HEADERS = (
    header_errors.FirstHeaderLineIsContinuationDefect,
    header_errors.InvalidHeaderDefect,
    header_errors.MisplacedEnvelopeHeaderDefect,
    header_errors.MissingHeaderBodySeparatorDefect,
)


@dataclass(frozen=True)
class Route:
    handler: Callable[..., tuple[int, object]]
    owner: bool = field(default=True, kw_only=True)
    with_feed: bool = field(default=False, kw_only=True)
    with_chat: bool = field(default=False, kw_only=True)
    with_data_dir: bool = field(default=False, kw_only=True)
    with_work: bool = field(default=False, kw_only=True)

    def run(self, body: dict | None, get_feed: Callable[[], Feed],
            chat: ChatService | None = None, *, data_dir: Path | None = None,
            get_work: Callable[[], tuple[Registry, Ledger]] | None = None) -> tuple[int, object]:
        if self.with_work:
            return self.handler(body, get_work)
        if self.with_data_dir:
            return self.handler(body, data_dir)
        if self.with_chat:
            try:
                return self.handler(body, chat)
            except ChatInboxFull:
                return 429, {"error": "The chat steering queue is full."}
            except ChatBusy:
                return 409, {"error": "Chat is busy."}
        if not self.with_feed:
            return self.handler(body)
        from .feed import BriefNotFound, FeedBusy, FeedError

        try:
            return self.handler(body, get_feed())
        except FeedBusy:
            return 503, {"error": "Feed is busy."}
        except BriefNotFound:
            return 404, {"error": "Brief not found."}
        except FeedError:
            return 400, {"error": "Feed operation refused."}


def _feed_snapshot(body, feed):
    return 200, feed.snapshot()


def _work_snapshot(body, get_work):
    """Read recorded work. Starting, steering, watching and T3 calls are out of scope."""
    from .work import WorkError

    try:
        registry, ledger = get_work()
        projects = registry.list()
        threads = ledger.threads()
    except WorkError:
        return 503, {"error": "Service Unavailable"}

    projects = sorted(projects, key=lambda project: project.name)[:32]
    threads = sorted(threads, key=lambda thread: thread.started_at, reverse=True)[:WORK_THREAD_CAP]
    return 200, {
        "projects": [{"name": project.name} for project in projects],
        "threads": [
            {
                "thread_id": thread.thread_id,
                "project": thread.project,
                "title": thread.title,
                "status": thread.status,
                "waiting_on_owner": thread.waiting_on_owner,
                "started_at": thread.started_at,
                "updated_at": thread.updated_at,
            }
            for thread in threads
        ],
    }


def _feed_answer(body, feed):
    """Answer an existing brief. The lookup may create the empty private feed folder."""
    if (not {"id", "action"} <= body.keys() <= {"id", "action", "text"}
            or not all(isinstance(value, str) for value in body.values())):
        return 400, {"error": "Expected a brief ID, an action and optional answer text."}
    bid, action = body["id"], body["action"]
    brief = feed.answer(bid, action, text=body.get("text"), return_brief=True)
    result = {"brief": brief}
    if action == "approve" and brief["proposal_id"] is not None:
        result["executed"] = False
    return 200, result


def _feed_feedback(body, feed):
    if body.keys() != {"id", "tag"} or not all(isinstance(value, str) for value in body.values()):
        return 400, {"error": "Expected a brief ID and a feedback tag."}
    return 200, {"brief": feed.feedback(body["id"], body["tag"], return_brief=True)}


def _chat_snapshot(body, chat):
    return 200, chat.snapshot()


def _chat_send(body, chat):
    if body.keys() != {"text"}:
        return 400, {"error": "Expected a chat message."}
    try:
        return 202, chat.send(body["text"])
    except ValueError:
        return 400, {"error": "Invalid chat message."}


def _chat_events(body, chat):
    if body.keys() != {"after"} or type(body["after"]) is not int or body["after"] < 0:
        return 400, {"error": "Expected a nonnegative event sequence number."}
    return 200, chat.events(body["after"])


def _chat_stop(body, chat):
    if body:
        return 400, {"error": "Expected an empty object."}
    return 200, chat.stop()


def _chat_new(body, chat):
    if body:
        return 400, {"error": "Expected an empty object."}
    return 200, chat.new_session()


def _approvals_list(body, chat):
    return 200, {"approvals": []} if chat is None else chat.approval_snapshot()


def _approvals_answer(body, chat):
    if (body.keys() != {"id", "digest", "decision"}
            or not all(isinstance(value, str) for value in body.values())
            or body["decision"] not in ("approve", "deny")):
        return 400, {"error": "Expected an approval ID, digest and decision."}
    if chat is None or chat.approvals is None:
        return 404, {"error": "Approval not found."}
    try:
        state = chat.approvals.answer(body["id"], body["digest"], body["decision"])
    except KeyError:
        return 404, {"error": "Approval not found."}
    except ApprovalConflict:
        return 409, {"error": "Approval conflicts with the pending request."}
    return 200, {"id": body["id"], "state": state}


def _learned_target(store, rid, *, rollback, nonblocking=False):
    with store._operation(initialize=False, nonblocking_read=nonblocking) as op:
        rows = op.records()
        target = next((row for row in rows if row["id"] == rid), None)
        if target is None or op.status(target) != "applied":
            return target, False, False
        if rollback:
            for row in reversed(rows):
                if (row["skill"], row["path"]) != (target["skill"], target["path"]) or op.status(row) != "applied":
                    continue
                repeated = (row["id"] != rid and row["op"] == "rollback" and row["actor"] == "owner"
                            and (row["before"], row["after"]) == (target["after"], target["before"]))
                return target, True, repeated
        return target, True, False


def _learned(body, data_dir, *, action):
    rollback = action == "rollback"
    keys = {"id"} if rollback else {"id", "on"}
    if (body.keys() != keys or not isinstance(body["id"], str)
            or (not rollback and type(body["on"]) is not bool)):
        return 400, {"error": "Invalid learned skill action."}

    from .skill_store import SkillStore, StoreBusy, StoreError, _ID

    rid = body["id"]
    not_found = 404, {"error": "Skill ledger record not found."}
    refused = 409, {"error": "Skill operation refused."}
    busy = 503, {"error": "Skill store is busy."}
    if not _ID.fullmatch(rid):
        return not_found
    store = SkillStore(data_dir / "skills", ledger=data_dir / "skill-ledger",
                       lock_deadline=time.monotonic() + FEED_LOCK_TIMEOUT)
    try:
        target, applied, repeated = _learned_target(store, rid, rollback=rollback)
    except FileNotFoundError:
        return not_found
    except StoreBusy:
        return busy
    except (OSError, ValueError):
        return refused
    if target is None:
        return not_found
    if not applied:
        return refused
    if repeated:
        return 200, {"changed": False}
    try:
        if rollback:
            store.rollback(rid)
            return 200, {"changed": True}
        set_flag = store.pin if action == "pin" else store.lock
        result = set_flag(target["skill"], body["on"])
        return 200, {"changed": result != "Skill flag is already in the requested state."}
    except StoreBusy:
        return busy
    except StoreError:
        # Another owner action may have completed the rollback after lookup.
        # After a write-phase refusal, this check can only clarify the result.
        if rollback:
            try:
                if _learned_target(store, rid, rollback=True, nonblocking=True)[2]:
                    return 200, {"changed": False}
            except (OSError, ValueError):
                pass
        return refused
    except (OSError, ValueError):
        return refused


ROUTES: dict[tuple[str, str], Route] = {
    ("GET", "/v3/owner/whoami"): Route(lambda body: (200, {"owner": True})),
    ("POST", "/v3/owner/ping"): Route(lambda body: (200, {"ok": True})),
    ("GET", "/v3/owner/work"): Route(_work_snapshot, with_work=True),
    ("GET", "/v3/owner/feed"): Route(_feed_snapshot, with_feed=True),
    ("POST", "/v3/owner/feed/answer"): Route(_feed_answer, with_feed=True),
    ("POST", "/v3/owner/feed/feedback"): Route(_feed_feedback, with_feed=True),
    ("GET", "/v3/owner/chat"): Route(_chat_snapshot, with_chat=True),
    ("POST", "/v3/owner/chat/send"): Route(_chat_send, with_chat=True),
    ("POST", "/v3/owner/chat/stop"): Route(_chat_stop, with_chat=True),
    ("POST", "/v3/owner/chat/events"): Route(_chat_events, with_chat=True),
    ("POST", "/v3/owner/chat/new"): Route(_chat_new, with_chat=True),
    ("GET", "/v3/owner/approvals"): Route(_approvals_list, with_chat=True),
    ("POST", "/v3/owner/approvals/answer"): Route(_approvals_answer, with_chat=True),
    ("POST", "/v3/owner/learned/rollback"): Route(partial(_learned, action="rollback"), with_data_dir=True),
    ("POST", "/v3/owner/learned/pin"): Route(partial(_learned, action="pin"), with_data_dir=True),
    ("POST", "/v3/owner/learned/lock"): Route(partial(_learned, action="lock"), with_data_dir=True),
}
PUBLIC = frozenset(key for key, route in ROUTES.items() if not route.owner)


def is_owner(handler: BaseHTTPRequestHandler) -> bool:
    """Recognize the HUD page on the v3 loopback listener.

    The strict Host check refuses rebound names. Same-origin GET and HEAD
    often omit Origin, so a plain read can use browser fetch metadata instead.
    Page scripts cannot set that header. Programs on the owner's machine can
    supply these headers themselves.
    """
    port = handler.server.server_port
    if handler.server.server_address != ("127.0.0.1", port):
        return False
    # The email parser also records MIME body defects based on Content-Type.
    # Only consider defects that can hide or misinterpret HTTP headers here.
    if any(isinstance(defect, _MALFORMED_HEADERS) for defect in handler.headers.defects):
        return False
    if handler.folded_headers & {b"host", b"origin", b"sec-fetch-site"}:
        return False
    hosts = handler.headers.get_all("Host", [])
    if len(hosts) != 1 or not config.own_host_header(hosts[0], port):
        return False

    read = handler.command in ("GET", "HEAD")
    sites = handler.headers.get_all("Sec-Fetch-Site", [])
    if len(sites) > 1 or sites == ["cross-site"]:
        return False
    origins = handler.headers.get_all("Origin", [])
    if origins:
        return origins == [f"http://{hosts[0]}"] and (read or sites in ([], ["same-origin"]))
    return read and sites == ["same-origin"] and "Upgrade" not in handler.headers
