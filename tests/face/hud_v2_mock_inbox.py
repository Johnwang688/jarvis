"""The owner inbox routes (inbox A1's `GET /inbox` and `POST /inbox/<id>/answer`,
`jarvis/v2/inbox.py`) for the HUD mock, so the HUD's inbox view (A2) can be
driven headless.

Owner-only as the daemon's are (`projects.owner_only`), checked before
anything else on every `/inbox` path: the same rule the sub-agent routes'
mock applies (`hud_v2_mock_subagents._owner`). The id is percent-decoded
**once**, as `inbox._split` does (the HUD sends it through
`encodeURIComponent`), then matched exactly against the rows; a row that is
not there is a 409 with the daemon's sentence.

The world's `inbox` section (`state(mock)`):
  - `rows`: what `GET /inbox` serves, in the daemon's shape and order;
  - `fail`: (status, sentence) to answer `GET /inbox` with instead (404 for
    an older daemon);
  - `answer_status`: row id -> (status, body) to answer that row's POST with
    instead of the daemon's 200 (a 409 is "already answered or gone");
  - `decided`: every `POST /inbox/decisions/<id>` (inbox B1) that got
    through, as (raw path, decoded decision id, body);
  - `answers`: every answer that got through the owner test, as
    (raw path, decoded id, body); `reads`: every `GET /inbox` served;
    `refused`: every request the owner test refused.
  - `marks` (inbox B4): row id -> `{snoozed_until, dismissed_at}`, the
    owner's snoozes and dismissals; `mark_posts`: every `POST
    /inbox/<id>/mark` that got through, as (raw path, decoded id, body).
    `GET /inbox?all=1` serves every row with `markable` (rank 5 to 8, never
    `prx:`/`pr_unavailable`) and a hidden one with `hidden`; without `all`
    the hidden ones are left out, as the daemon's `rows()` does. A mark on
    a row that is not markable is the daemon's 409.
A 200 answer removes the row and publishes `inbox_changed` `{ids: [id],
reason: "answered"}`, as the daemon's bus does after the source's own event;
an approval row's answer also drops the card (`approval_resolved`).
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

try:
    from tests.face import hud_v2_mock_subagents as mock_subagents
except ImportError:  # run from tests/face
    import hud_v2_mock_subagents as mock_subagents  # type: ignore[no-redef]

GONE = "this item is no longer waiting; it was answered or its source has gone"


def state(mock) -> dict:
    return mock.world.setdefault("inbox", {"rows": [], "fail": None, "answer_status": {},
                                           "answers": [], "reads": 0, "refused": 0})


def markable(row) -> bool:
    """`inbox_marks.markable`: rank 5 to 8, never "PR list unavailable"."""
    rank = row.get("rank")
    return (isinstance(rank, int) and 5 <= rank <= 8 and row.get("kind") != "pr_unavailable"
            and not str(row.get("id", "")).startswith("prx:"))


def changed(mock, ids=(), reason: str = "test") -> None:
    """Publish what the daemon's `Inbox.observe` would: `{ids, reason}`, never text."""
    mock.emit("inbox_changed", {"ids": sorted(set(ids)), "reason": reason})


def handle(h, mock, method: str, path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != "inbox":
        return False
    s = state(mock)
    if not mock_subagents._owner(h, mock, method):
        s["refused"] += 1
        h._err(403, "only the owner can do this, from the HUD")
        return True
    if parts == ["inbox"] and method == "GET":
        s["reads"] += 1
        if s["fail"]:
            status, sentence = s["fail"]
            h._err(status, sentence)
            return True
        # Inbox B4: `?all=1` keeps the hidden rows, flagged; without it they go.
        every = parse_qs(urlparse(h.path).query).get("all") == ["1"]
        s.setdefault("all_reads", 0)
        s["all_reads"] += 1 if every else 0
        marks = s.setdefault("marks", {})
        out = []
        for row in s["rows"]:
            hidden = marks.get(row.get("id")) if markable(row) else None
            if hidden and not every:
                continue
            out.append({**row, "markable": markable(row), **({"hidden": hidden} if hidden else {})}
                       if every else row)
        h._json(out)
        return True
    if len(parts) == 3 and parts[2] == "mark" and method == "POST":
        # Inbox B4 (IN-7): `{snooze_until}`, `{dismiss: true}` or `{clear: true}`.
        body = h._body()
        row_id = unquote(parts[1], errors="strict")
        s.setdefault("mark_posts", []).append((path, row_id, body))
        mock.calls.append(("POST", "/inbox/*/mark", {"id": row_id, **(body if isinstance(body, dict) else {})}))
        marks = s.setdefault("marks", {})
        if not isinstance(body, dict) or len(body) != 1:
            h._err(400, "give exactly one of snooze_until, dismiss and clear")
            return True
        if body.get("clear") is True:
            had = marks.pop(row_id, None) is not None
            changed(mock, [row_id], "inbox_marked")
            h._json({"ok": True, "id": row_id, "hidden": None, "cleared": had})
            return True
        row = next((r for r in s["rows"] if r.get("id") == row_id), None)
        if row is None:
            h._err(409, GONE)
            return True
        if not markable(row):
            h._err(409, "this item cannot be snoozed or dismissed: it is live or holds up work")
            return True
        hidden = ({"snoozed_until": body["snooze_until"], "dismissed_at": None} if "snooze_until" in body
                  else {"snoozed_until": None, "dismissed_at": "2026-10-28T10:00:00+00:00"})
        marks[row_id] = hidden
        changed(mock, [row_id], "inbox_marked")
        h._json({"ok": True, "id": row_id, "hidden": hidden})
        return True
    if len(parts) == 3 and parts[2] == "answer" and method == "POST":
        body = h._body()
        row_id = unquote(parts[1], errors="strict")
        s["answers"].append((path, row_id, body))
        mock.calls.append(("POST", "/inbox/*/answer", {"id": row_id, **(body if isinstance(body, dict) else {})}))
        scripted = s["answer_status"].get(row_id)
        if scripted:
            status, payload = scripted
            h._json(payload, status)
            return True
        row = next((r for r in s["rows"] if r.get("id") == row_id), None)
        if row is None:
            h._err(409, GONE)
            return True
        s["rows"] = [r for r in s["rows"] if r.get("id") != row_id]
        if row.get("kind") == "approval":
            req = (row.get("detail") or {}).get("req_id")
            mock.world["approvals"] = [r for r in mock.world.get("approvals", []) if r.get("req_id") != req]
            mock.emit("approval_resolved", {"req_id": req, "decision": (body or {}).get("decision", "deny")})
        changed(mock, [row_id], "answered")
        if row.get("kind") == "deferred":
            # QH: Approve and retry, or Leave it, with the daemon's fixed note.
            left = (body or {}).get("action") == "leave"
            h._json({"ok": True, "delivery": "left" if left else "queued",
                     "note": "Left: nothing was sent." if left
                     else "Approved once for 30 minutes; the chat hears it after the turn already running."})
            return True
        h._json({"ok": True})
        return True
    if len(parts) == 3 and parts[1] == "decisions" and method == "POST":
        # Inbox B1: `POST /inbox/decisions/<id>` with `{choice}` or `{text}`.
        # The answered row leaves (`decision_answered` → `inbox_changed`) and
        # the reply carries the daemon's `delivery` and `note`.
        body = h._body()
        decision_id = unquote(parts[2], errors="strict")
        s.setdefault("decided", []).append((path, decision_id, body))
        mock.calls.append(("POST", "/inbox/decisions/*", {"id": decision_id,
                                                         **(body if isinstance(body, dict) else {})}))
        row_id = f"dec:{decision_id}"
        scripted = s["answer_status"].get(row_id)
        if scripted:
            status, payload = scripted
            h._json(payload, status)
            return True
        row = next((r for r in s["rows"] if r.get("id") == row_id), None)
        if row is None:
            h._err(404, "no such decision")
            return True
        if (row.get("detail") or {}).get("answer") is not None:
            h._err(409, "this decision was already answered")
            return True
        s["rows"] = [r for r in s["rows"] if r.get("id") != row_id]
        changed(mock, [row_id], "decision_answered")
        h._json({"ok": True, "decision_id": decision_id, "delivery": "queued",
                 "note": "Sent to the asking chat; it runs your answer after the turn already running."})
        return True
    if parts == ["inbox", "checks", "import"] and method == "POST":
        # Inbox B2 (§6): `{path}` returns the scripted drafts, unsaved;
        # `{path, digest, save}` saves one.
        body = h._body()
        s.setdefault("imports", []).append(body)
        mock.calls.append(("POST", "/inbox/checks/import", body if isinstance(body, dict) else {}))
        if isinstance(body, dict) and "save" in body:
            h._json({"ok": True, "check_id": "c00000000000a", "index": body["save"]})
        else:
            h._json(s.get("import_reply") or {"path": "", "digest": "", "drafts": []})
        return True
    if len(parts) == 5 and parts[1] == "checks" and parts[3] == "steps" and method == "POST":
        # Inbox B2 (IN-6): `POST /inbox/checks/<id>/steps/<n>` `{verdict, note}`.
        body = h._body()
        check_id, step = unquote(parts[2], errors="strict"), int(parts[4])
        s.setdefault("marked", []).append((path, check_id, step, body))
        mock.calls.append(("POST", "/inbox/checks/*/steps/*", {"id": check_id, "step": step,
                                                              **(body if isinstance(body, dict) else {})}))
        row_id = f"chk:{check_id}"
        scripted = s["answer_status"].get(row_id)
        if scripted:
            status, payload = scripted
            h._json(payload, status)
            return True
        row = next((r for r in s["rows"] if r.get("id") == row_id), None)
        if row is None:
            h._err(404, "no such check")
            return True
        detail = row["detail"]
        detail["results"] = [r for r in detail["results"] if r["step"] != step] + [
            {"step": step, "verdict": body["verdict"], "note": body.get("note", ""), "at": "2026-10-25T10:00:00+00:00"}]
        done = len(detail["results"]) == len(detail["steps"])
        if done:
            detail["done"], row["rank"] = True, 8
        changed(mock, [row_id], "check_marked")
        h._json({"ok": True, "check_id": check_id, "step": step, "done": done})
        return True
    h._err(404, "route not found")
    return True
