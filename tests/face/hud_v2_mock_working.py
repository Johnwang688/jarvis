"""The owner's working-set routes (WS-1, `jarvis/v2/working.py`) for the HUD mock,
so the HUD's Inbox section (WS-2) can be driven headless.

Owner-only as the daemon's are, checked before anything else on every
`/working` path (the same rule `hud_v2_mock_inbox` and the sub-agent routes
apply).

The world's `working` section (`state(mock)`):
  - `entries`: what `GET /working` serves as `entries`, in the daemon's shape
    (`thread_id, project_id, since, last, seq, status, rows, why_not`);
  - `needs_you`: the rows served under `needs_you`; `count`, `pending`;
  - `fail`: (status, sentence) to answer `GET /working` with instead (404 for
    an older daemon); `reads`: every `GET /working` served;
  - `done_posts` / `undo_posts`: every Done and Undo that got through the
    owner test, as (thread id, body);
  - `done_reply`: thread id -> (status, body) to answer that Done with instead;
  - `refused`: every request the owner test refused.

A Done with the entry's current `seq`, and no `why_not`, takes the entry out
and publishes the daemon's `working_changed` (no ids, no data); its token is
`tok-<id>`. A stale `seq` or an ineligible entry is the daemon's 409 carrying
the current entry. Undo with that token puts it back.
"""
from __future__ import annotations

from urllib.parse import unquote

try:
    from tests.face import hud_v2_mock_subagents as mock_subagents
except ImportError:  # run from tests/face
    import hud_v2_mock_subagents as mock_subagents  # type: ignore[no-redef]


def state(mock) -> dict:
    return mock.world.setdefault("working", {
        "entries": [], "needs_you": [], "count": 0, "pending": False, "fail": None, "reads": 0,
        "done_posts": [], "undo_posts": [], "done_reply": {}, "undone": {}, "refused": 0})


def entry(thread_id: str, *, project_id: str = "p1", seq: int = 1, last: str = "2026-11-14T10:00:00+00:00",
          status: str = "idle", why_not: str | None = None, rows: list | None = None) -> dict:
    return {"thread_id": thread_id, "project_id": project_id, "since": "2026-11-14T08:00:00+00:00", "last": last,
            "seq": seq, "status": status, "rows": rows or [], "why_not": why_not}


def changed(mock) -> None:
    """Publish what `WorkingSet.observe` does: no ids, no data."""
    mock.emit("working_changed", {})


def handle(h, mock, method: str, path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != "working":
        return False
    s = state(mock)
    if not mock_subagents._owner(h, mock, method):
        s["refused"] += 1
        h._err(403, "only the owner can do this, from the HUD")
        return True
    if parts == ["working"] and method == "GET":
        s["reads"] += 1
        if s["fail"]:
            status, sentence = s["fail"]
            h._err(status, sentence)
            return True
        h._json({"entries": s["entries"], "needs_you": s["needs_you"], "count": s["count"],
                 "pending": s["pending"]})
        return True
    if len(parts) == 3 and parts[2] in ("done", "undo") and method == "POST":
        body = h._body()
        thread_id = unquote(parts[1], errors="strict")
        s["done_posts" if parts[2] == "done" else "undo_posts"].append((thread_id, body))
        mock.calls.append(("POST", f"/working/*/{parts[2]}", {"id": thread_id,
                                                              **(body if isinstance(body, dict) else {})}))
        scripted = s["done_reply"].get(thread_id) if parts[2] == "done" else None
        if scripted:
            status, payload = scripted
            h._json(payload, status)
            return True
        current = next((e for e in s["entries"] if e["thread_id"] == thread_id), None)
        if parts[2] == "done":
            if current is None:
                h._json({"error": "this chat is not in your Inbox", "entry": None}, 409)
            elif (body or {}).get("seq") != current["seq"]:
                h._json({"error": "this chat changed since you saw it", "entry": current}, 409)
            elif current["why_not"]:
                h._json({"error": current["why_not"], "entry": current}, 409)
            else:
                s["entries"] = [e for e in s["entries"] if e["thread_id"] != thread_id]
                s["undone"][thread_id] = current
                changed(mock)
                h._json({"thread_id": thread_id, "done": True, "token": f"tok-{thread_id}", "undo_s": 10,
                         "entry": {"thread_id": thread_id, "reason": "done"}})
            return True
        held = s["undone"].get(thread_id)
        if held is None or (body or {}).get("token") != f"tok-{thread_id}":
            h._json({"error": "too late to undo", "entry": current}, 409)
        else:
            s["entries"].append(s["undone"].pop(thread_id))
            changed(mock)
            h._json({"thread_id": thread_id, "done": False, "entry": {"thread_id": thread_id}})
        return True
    h._err(404, "route not found")
    return True
