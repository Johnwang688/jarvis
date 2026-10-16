"""The native sub-agent routes of `docs/hud-api.md` (SV-1/SV-2/SV-3/SV-4), for
the HUD mock: `GET /threads/{id}/subagents`,
`GET /threads/{id}/subagents/{sid}/transcript` and (SV-4)
`POST /threads/{id}/subagents/{sid}/stop`.

Owner-only as the daemon's are (`projects.is_owner`), checked before anything
else: a request with an Origin is the owner's only when it is this window's
own (`http://<Host>` on a loopback Host); one with none only when it is a
plain read (GET/HEAD) the browser marked `Sec-Fetch-Site: same-origin`. A gate checked
only against a mock is a gate checked against the mock's idea of the browser
(control-plane notes), so `refused` counts every refusal for the check to
read, and `reads` every read that got through.

The world's `subagents` section (`state(mock)`):
  - `lists`: thread id -> the list rows (as the daemon serves them);
  - `transcripts`: "thread|sid" -> the transcript body (`subagent`,
    `messages`, `truncated`, optional `brief`);
  - `fail`: "thread" or "thread|sid" -> a status to answer instead (404 for a
    gone record, 400 for an older daemon);
  - `reads`, `refused`: what the routes saw;
  - `stops`: every Stop that got through, as (thread, sid); `stop_answer`:
    sid -> (status, body) to answer instead of the daemon's 200 (which,
    like the daemon, marks the listed row `stop: "stopping"`).
"""
from __future__ import annotations

import re
from urllib.parse import unquote

SID = re.compile(r"(?:cl|cx):[A-Za-z0-9_-]{1,128}")


def state(mock) -> dict:
    return mock.world.setdefault("subagents", {"lists": {}, "transcripts": {}, "fail": {},
                                               "reads": [], "refused": 0, "stops": [],
                                               "stop_answer": {}})


def _owner(h, mock, method: str = "GET") -> bool:
    host = h.headers.get("Host", "")
    loopback = host in (f"127.0.0.1:{mock.port}", f"localhost:{mock.port}")
    origin = h.headers.get("Origin")
    if origin is not None:
        return loopback and origin == f"http://{host}"
    # As `projects.is_owner`: no Origin is the owner's only for a plain read
    # (a write with no Origin never is: SV-4's Stop).
    return method in ("GET", "HEAD") and loopback and h.headers.get("Sec-Fetch-Site") == "same-origin"


def handle(h, mock, method: str, path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    if not (len(parts) >= 3 and parts[0] == "threads" and parts[2] == "subagents"):
        return False
    s = state(mock)
    if not _owner(h, mock, method):
        s["refused"] += 1
        h._err(403, "only the owner can do this, from the HUD")
        return True
    if len(parts) == 5 and parts[4] == "stop":
        return _stop(h, mock, s, method, parts)
    if method != "GET":
        h._err(405, "sub-agents are read-only here")
        return True
    tid = parts[1]
    if not any(t["id"] == tid for t in mock.world["threads"]):
        h._err(404, f"Thread {tid} not found")
        return True
    if len(parts) == 3:
        s["reads"].append(("list", tid))
        status = s["fail"].get(tid)
        if status:
            h._err(status, "refused by the test")
            return True
        h._json({"subagents": s["lists"].get(tid, [])})
        return True
    if len(parts) == 5 and parts[4] == "transcript":
        sid = unquote(parts[3])
        if not SID.fullmatch(sid):
            h._err(400, "not a sub-agent id")
            return True
        s["reads"].append(("transcript", tid, sid))
        status = s["fail"].get(f"{tid}|{sid}")
        if status:
            h._err(status, "refused by the test")
            return True
        body = s["transcripts"].get(f"{tid}|{sid}")
        if body is None:
            h._err(404, "no such sub-agent on this thread")
            return True
        h._json(body)
        return True
    h._err(404, "not found")
    return True


def _stop(h, mock, s, method, parts) -> bool:
    """`POST …/subagents/{sid}/stop`, after the owner gate: as the daemon
    answers it (`Daemon.stop_subagent`)."""
    if method != "POST":
        h._err(405, "a sub-agent is stopped with POST")
        return True
    sid = unquote(parts[3])
    if not SID.fullmatch(sid):
        h._err(400, "not a sub-agent id")
        return True
    tid = parts[1]
    if not any(t["id"] == tid for t in mock.world["threads"]):
        h._err(404, f"Thread {tid} not found")
        return True
    s.setdefault("stops", []).append((tid, sid))
    answer = s.setdefault("stop_answer", {}).get(sid)
    if answer:
        h._json(answer[1], answer[0])
        return True
    row = next((r for r in s["lists"].get(tid, []) if r.get("sid") == sid), None)
    if row is None:
        h._err(404, "no such sub-agent on this thread")
        return True
    if row.get("status") not in ("running", "backgrounded"):
        h._json({"error": "This sub-agent has already ended.", "stale": True}, 409)
        return True
    if row.get("stop") == "stopping":
        h._json({"ok": True, "stop": "stopping", "already": True})
        return True
    row["stop"] = "stopping"
    h._json({"ok": True, "stop": "stopping"})
    return True
