"""The `/terminals` HTTP routes of `docs/hud-api.md` (WP-C), for the HUD mock.

Free and hermetic: no shell ever runs. The WebSocket half — the attach —
is never served here; `tests/face/hud_v2_terminal_check.py` plays the PTY
with Playwright's `route_web_socket`, so the socket never leaves the
browser. This file only keeps the world's terminal rows and their tickets:

  - `GET /terminals` → the rows;
  - `POST /terminals {in, cols?, rows?}` → 201 row; `in` is "home" or exactly
    one of `{"thread"|"project"|"task": id}`, any other shape 400, an unknown
    id 404, the seventh terminal 409 with the daemon's sentence;
  - `PATCH /terminals/{id} {readable}` → row, exactly that key and a bool;
  - `POST /terminals/{id}/ticket {}` → `{ticket, expires_in}`, recorded in
    `mock.terminal_tickets` (ticket → id) for the fake PTY to redeem once;
  - `DELETE /terminals/{id}` → `{ok, id, exit_code}`, 404 when gone.

Owner-only on the real daemon: a request with an `Origin` that is not the
mock's own is 403 here too, so a HUD that ever sent one would be caught.
"""

from __future__ import annotations

import secrets

CAP = 6


def row(tid: str, title: str, *, project_id: str | None = "p1",
        folder: str = "/home/johnw/projects/Jarvis", **over) -> dict:
    rec = {"id": tid, "title": title, "folder": folder, "project_id": project_id,
           "created": "2026-10-09T12:00:00+00:00", "cols": 80, "rows": 24, "shown": False,
           "exited": False, "exit_code": None, "readable": True, "busy": False,
           "integration": "bash", "integrated": True, "marked": True}
    rec.update(over)
    return rec


def _resolve(w, spec):
    """(label, project_id) from ids, or an (status, error) refusal."""
    if spec == "home":
        return ("~", None), None
    if not isinstance(spec, dict) or len(spec) != 1 or next(iter(spec)) not in ("thread", "project", "task"):
        return None, (400, 'in must be "home" or one of {"thread": id}, {"project": id}, {"task": id}')
    kind, value = next(iter(spec.items()))
    if kind == "thread":
        t = next((t for t in w["threads"] if t["id"] == value), None)
        if t is None:
            return None, (404, f"thread {value} not found")
        pid = t["project_id"]
    elif kind == "task":
        t = next((t for t in w["tasks"] if t["id"] == value), None)
        if t is None:
            return None, (404, f"task {value} not found")
        pid = t["project_id"]
    else:
        pid = value
    project = next((p for p in w["projects"] if p["id"] == pid), None)
    if project is None:
        return None, (404, f"project {pid} not found")
    return (project["name"], project["id"]), None


def handle(h, mock, method: str, path: str, body) -> bool:
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != "terminals":
        return False
    origin = h.headers.get("Origin")
    if origin is not None and origin != f"http://127.0.0.1:{mock.port}":
        h._err(403, "terminals answer only the HUD's own window")
        return True
    w = mock.world
    rows = w.setdefault("terminals", [])
    tickets = mock.__dict__.setdefault("terminal_tickets", {})
    if parts == ["terminals"]:
        if method == "GET":
            h._json(rows)
            return True
        if method == "POST":
            if not isinstance(body, dict) or set(body) - {"in", "cols", "rows"} or "in" not in body:
                h._err(400, "expected {in, cols?, rows?}")
                return True
            resolved, refused = _resolve(w, body["in"])
            if refused:
                h._err(*refused)
                return True
            if mock.__dict__.get("terminal_refuse"):
                h._err(409, mock.terminal_refuse)
                return True
            if len(rows) >= CAP:
                h._err(409, f"{CAP} terminals are open; close one first")
                return True
            label, pid = resolved
            seq = mock.__dict__.get("terminal_seq", 0) + 1
            mock.terminal_seq = seq
            # `terminal_defaults`: what the next terminals are like (a shell
            # with no integration, one whose startup file never ran).
            rec = row(f"{0xa0000000 + seq:08x}", f"bash · {label}", project_id=pid,
                      **mock.__dict__.get("terminal_defaults", {}))
            rows.append(rec)
            mock.__dict__.setdefault("terminal_created", []).append((rec["id"], body["in"]))
            h._json(rec, 201)
            return True
    if len(parts) in (2, 3):
        rec = next((r for r in rows if r["id"] == parts[1]), None)
        if rec is None:
            h._err(404, f"terminal {parts[1]} not found")
            return True
        if len(parts) == 2 and method == "DELETE":
            rows.remove(rec)
            h._json({"ok": True, "id": rec["id"], "exit_code": rec["exit_code"]})
            return True
        if len(parts) == 2 and method == "PATCH":
            if not isinstance(body, dict) or set(body) != {"readable"} or not isinstance(body["readable"], bool):
                h._err(400, "expected exactly {readable: bool}")
                return True
            rec["readable"] = body["readable"]
            h._json(rec)
            return True
        if parts[2:] == ["ticket"] and method == "POST":
            if body not in ({}, None):
                h._err(400, "expected {}")
                return True
            ticket = secrets.token_urlsafe(16)
            tickets[ticket] = rec["id"]
            h._json({"ticket": ticket, "expires_in": 30})
            return True
    h._err(404, "route not found")
    return True
