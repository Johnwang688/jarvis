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
  - `answers`: every answer that got through the owner test, as
    (raw path, decoded id, body); `reads`: every `GET /inbox` served;
    `refused`: every request the owner test refused.
A 200 answer removes the row and publishes `inbox_changed` `{ids: [id],
reason: "answered"}`, as the daemon's bus does after the source's own event;
an approval row's answer also drops the card (`approval_resolved`).
"""
from __future__ import annotations

from urllib.parse import unquote

try:
    from tests.face import hud_v2_mock_subagents as mock_subagents
except ImportError:  # run from tests/face
    import hud_v2_mock_subagents as mock_subagents  # type: ignore[no-redef]

GONE = "this item is no longer waiting; it was answered or its source has gone"


def state(mock) -> dict:
    return mock.world.setdefault("inbox", {"rows": [], "fail": None, "answer_status": {},
                                           "answers": [], "reads": 0, "refused": 0})


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
        h._json(s["rows"])
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
        h._json({"ok": True})
        return True
    h._err(404, "route not found")
    return True
