"""A mock of `docs/hud-api.md`, for driving the built HUD v2 headlessly.

Free — no API calls, no daemon, no providers. The contract is fixed and both
WP12a (the real backend) and this file implement it, so the frontend can be
built and tested before the backend lands and then re-verified against it
(`tests/face/hud_v2_live_check.py`).

It is the v1 `hud_state_check` puppet pattern one level up: every route
answers from a mutable world the test can rewrite between assertions, and the
SSE stream is a `queue.Queue` the test releases one frame at a time. Nothing
here guesses at a shape the contract does not state.

Run it standalone to poke at the HUD by hand:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python \\
        tests/face/hud_v2_mock.py            # serves hud/dist on 8478
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "hud" / "dist"

# A hostile avatar, checked for what it does rather than what it says: the art
# is drawn in the window that gates approvals, so an `onload` in it must never
# run. The HUD renders it through an <img>, which cannot run script whatever
# the file contains.
HOSTILE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
    'onload="window.__pwned = true">'
    '<script>window.__pwned = true;</script>'
    '<circle cx="50" cy="50" r="40" fill="#38bdf8"/>'
    "</svg>"
)


def _world() -> dict:
    """A small but complete world: one project, one chat thread, one task."""
    return {
        "projects": [
            {
                "id": "p1", "name": "jarvis", "root": "/home/johnw/projects/Jarvis",
                "created": "2026-09-15T00:00:00+00:00", "profile": "auto",
                "routing": {"chains": {}, "models": {}, "no_new_work": None},
                "discord_channel_id": None, "extra_dirs": [], "always_ask": [],
                "inbox": True,
            },
            {
                "id": "p2", "name": "schoolwork", "root": "/mnt/c/myday/schoolwork",
                "created": "2026-09-15T00:00:00+00:00", "profile": "ask",
                "routing": {"chains": {}, "models": {}, "no_new_work": None},
                "discord_channel_id": None, "extra_dirs": [], "always_ask": [],
                "inbox": False,
            },
        ],
        "threads": [
            {
                "id": "t1", "project_id": "p1", "role": "chat", "provider": "fast",
                "provider_session_id": None, "task_id": None, "title": "desk chat",
                "created": "2026-09-15T00:00:00+00:00",
                "updated": "2026-09-15T00:00:00+00:00",
                "turns": 2, "cost_usd": 0.0012, "tokens": 900,
                "model": "openai/gpt-5.6-luna", "effort": None,
                "cwd": "/home/johnw/projects/Jarvis",
            },
        ],
        "transcripts": {
            "t1": [
                {"role": "user", "text": "what is the plan", "at": "2026-09-15T00:00:00+00:00"},
                {"role": "assistant", "text": "**Ready.**", "at": "2026-09-15T00:00:01+00:00"},
            ],
        },
        "tasks": [
            {
                "id": "k1", "project_id": "p1", "brief": "add multiply to calc.py",
                "state": "running",
                "created": "2026-09-15T00:00:00+00:00",
                "updated": "2026-09-15T00:01:00+00:00",
                "spec": {
                    "goal": "add multiply", "deliverable": "calc.py plus tests",
                    "acceptance": ["multiply(2,3) == 6", "unittest passes"],
                    "constraints": [],
                    "questions": [
                        {"text": "commit on main or a branch?", "blocking": True,
                         "options": ["main", "branch"], "answer": None, "assumed": None},
                        {"text": "python version?", "blocking": False, "options": [],
                         "answer": None, "assumed": "3.14"},
                    ],
                },
                "plan": ["read calc.py", "add multiply", "write tests", "run tests", "verify"],
                "status": {
                    "phase": "running", "step": 2, "steps": 5,
                    "started": "2026-09-15T00:00:10+00:00", "elapsed_s": 96.0,
                    "cost_usd": 0.4211, "tokens": 40312,
                    "last_tool": "apply_patch", "last_file": "calc.py",
                    "open_question": None,
                    "routing": [
                        {"role": "orchestrator", "provider": "claude",
                         "reason": "project table", "at": "2026-09-15T00:00:11+00:00"},
                        {"role": "implementer", "provider": "codex",
                         "reason": "claude over threshold", "at": "2026-09-15T00:00:12+00:00"},
                    ],
                },
                "report": None,
                "thread_ids": ["kt1", "kt2"],
                "worktree": "/home/johnw/projects/Jarvis/.jarvis/worktrees/k1",
                "branch": "jarvis/add-multiply", "profile": None, "ceilings": {},
            },
        ],
        "task_threads": {
            "k1": [
                {"thread_id": "kt1", "role": "orchestrator", "provider": "claude",
                 "model": None, "state": "open", "turns": 4, "cost_usd": 0.31},
                {"thread_id": "kt2", "role": "implementer", "provider": "codex",
                 "model": "gpt-5-codex", "state": "open", "turns": 9, "cost_usd": 0.11},
            ],
        },
        "tree": {
            "": [
                {"name": "src", "kind": "dir", "size": 0, "mtime": 1757900000},
                {"name": "README.md", "kind": "file", "size": 42, "mtime": 1757900001},
                {"name": "calc.py", "kind": "file", "size": 61, "mtime": 1757900002},
                {"name": ".env", "kind": "file", "size": 61, "mtime": 1757900003},
            ],
            "src": [
                {"name": "main.py", "kind": "file", "size": 12, "mtime": 1757900004},
            ],
        },
        "files": {
            "calc.py": {"content": "def add(a, b):\n    return a + b\n", "mtime": 1757900002},
            "README.md": {"content": "# Title\n\nSome **docs** here.\n", "mtime": 1757900001},
            "src/main.py": {"content": "print('hi')\n", "mtime": 1757900004},
        },
        # A protected credential name comes back 200 with no content, ever.
        "protected": {".env"},
        "diff": {
            "base": "main", "head": "jarvis/add-multiply",
            "files": [
                {"path": "calc.py", "status": "M", "additions": 6, "deletions": 0},
                {"path": "test_calc.py", "status": "A", "additions": 21, "deletions": 0},
            ],
            "patch": "--- a/calc.py\n+++ b/calc.py\n",
            "truncated": True,
        },
        "diff_files": {
            "calc.py": {"before": "def add(a, b):\n    return a + b\n",
                        "after": "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"},
            "test_calc.py": {"before": "", "after": "import unittest\n"},
        },
        "approvals": [],
        "usage": {
            "providers": {
                "claude": {
                    "state": "available", "reason": "",
                    "today": {"work_tokens": 412000, "spend_usd": 0.0, "equivalent_usd": 9.42},
                    "allowance": {"work_tokens": 2000000},
                    # Never computed: absent means the provider did not report one.
                    "quota": None,
                },
                "codex": {
                    "state": "over_threshold", "reason": "weekly window at 82%",
                    "today": {"work_tokens": 220000, "spend_usd": 0.0, "equivalent_usd": 0.0},
                    "allowance": {"work_tokens": 1000000},
                    "quota": {"windows": [
                        {"name": "5h", "used_percent": 41, "resets_at": "2026-09-15T04:00:00Z"},
                        {"name": "weekly", "used_percent": 82, "resets_at": "2026-09-19T00:00:00Z"},
                    ]},
                },
                "fast": {
                    "state": "available", "reason": "",
                    "today": {"work_tokens": 9100, "spend_usd": 0.0121, "equivalent_usd": 0.0121},
                    "allowance": None, "quota": None,
                },
            }
        },
        "schedules": [
            {"id": "s1", "project_id": "p1", "brief": "morning briefing",
             "cron": "0 7 * * *", "every_s": None, "enabled": True,
             "describe": "every day at 07:00",
             "last_run_at": "2026-09-15T07:00:00-05:00", "last_task_id": "k0",
             "next_run_at": "2026-09-16T07:00:00-05:00", "created": "2026-09-01T00:00:00+00:00"},
        ],
        "route": {
            "table": {"chains": {"orchestrator": ["claude", "codex"],
                                 "implementer": ["codex", "claude"],
                                 "reviewer": ["claude"]}},
            "states": {"claude": "available", "codex": "over_threshold", "fast": "available"},
            "decisions": [
                {"task_id": "k1", "role": "orchestrator", "provider": "claude",
                 "reason": "project table", "at": "2026-09-15T00:00:11+00:00"},
                {"task_id": "k1", "role": "implementer", "provider": "codex",
                 "reason": "claude over threshold", "at": "2026-09-15T00:00:12+00:00"},
            ],
        },
        "avatars": {
            "active": "",
            "avatars": [
                {"slug": "jarvis", "name": "Jarvis", "wake": ["jarvis"],
                 "wake_source": [r"\bjarvis\b"], "rings": "default", "accent": None},
                {"slug": "hoot", "name": "Hoot", "wake": ["hoot"],
                 "wake_source": [r"\bhoot\b"], "rings": "triangles", "accent": "168,85,247"},
            ],
        },
        "voices": {
            "override": "",
            "voices": [
                {"name": "bm_george", "backend": "kokoro", "kind": "builtin"},
                {"name": "pocket:alba", "backend": "pocket", "kind": "builtin"},
                {"name": "pocket:dad", "backend": "pocket", "kind": "clone"},
            ],
        },
        "models": {
            "selected": "openai/gpt-5.6-luna",
            "models": [
                {"id": "openai/gpt-5.6-luna", "name": "GPT-5.6 Luna",
                 "efforts": ["low", "medium", "high", "max"], "effort": None},
                # A name off the network, carrying markup: this window draws
                # authorization cards, so it must render as text.
                {"id": "evil/model", "name": "<img src=x onerror=\"window.__pwned=1\">Bad",
                 "efforts": [], "effort": None},
            ],
        },
        # `GET /fs/dirs` — names only, and only under $HOME or /mnt/<drive>/.
        # Anything else is 403, which is the picker's whole boundary: the
        # window does not re-implement the rule, it shows the refusal.
        "dirs": {
            "/home/johnw": ["projects", "notes"],
            "/home/johnw/projects": ["Jarvis", "jarvis-trading-firm"],
            "/home/johnw/projects/Jarvis": ["docs", "hud", "jarvis", "tests"],
            "/home/johnw/projects/Jarvis/docs": [],
            "/home/johnw/notes": ["2026"],
            "/mnt/c": ["myday"],
            "/mnt/c/myday": ["schoolwork"],
        },
        "home": "/home/johnw",
        "stt_text": "what is the weather",
    }


_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _preview(cron, every_s) -> dict:
    """What the real backend answers, in the shapes the HUD reads.

    Deterministic rather than clock-driven: a test that asserted on "the next
    three real fire times" would be asserting on the minute it ran in.
    """
    if every_s:
        unit, n = ("hours", every_s // 3600) if every_s % 3600 == 0 else ("minutes", every_s // 60)
        describe = f"every {n} {unit}" if n != 1 else f"every {unit[:-1]}"
        return {"describe": describe,
                "next": ["2026-09-15T12:%02d:00-05:00" % (m * 5) for m in (1, 2, 3)]}
    fields = (cron or "").split()
    if len(fields) != 5:
        return {"describe": "", "next": []}
    minute, hour, _dom, _mon, dow = fields
    try:
        at = "%02d:%02d" % (int(hour), int(minute))
    except ValueError:
        at = f"{hour}:{minute}"
    if dow == "*":
        describe = f"every day at {at}"
    elif dow == "1-5":
        describe = f"weekdays at {at}"
    elif dow.isdigit():
        describe = f"{_DAYS[int(dow) % 7]}s at {at}"
    else:
        describe = f"cron {cron} (America/Chicago)"
    return {"describe": describe,
            "next": [f"2026-09-{d}T{at}:00-05:00" for d in ("16", "17", "18")]}


class MockDaemon:
    """The mock server plus the calls it recorded, for the test to assert on."""

    def __init__(self, port: int):
        self.port = port
        self.world = _world()
        self.sse: queue.Queue = queue.Queue()
        self.calls: list[tuple[str, str, dict]] = []
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        mock = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(DIST), **kw)

            def log_message(self, *args):
                pass

            # -- helpers
            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _err(self, status, message):
                self._json({"error": message}, status)

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except Exception:
                    return {"_raw": raw.decode("utf-8", "replace")}

            def _record(self, method, path, body):
                mock.calls.append((method, path, body))

            # -- GET
            def do_GET(self):
                url = urlparse(self.path)
                path, q = url.path, parse_qs(url.query)
                self._record("GET", path, {k: v[0] for k, v in q.items()})
                w = mock.world

                if path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while True:
                        try:
                            event = mock.sse.get(timeout=0.5)
                        except queue.Empty:
                            try:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                            except Exception:
                                return
                            continue
                        if event is None:
                            return
                        try:
                            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                            self.wfile.flush()
                        except Exception:
                            return

                if path == "/status":
                    return self._json({"version": "2.0-mock", "uptime_s": 1})
                if path == "/projects":
                    return self._json(w["projects"])
                if path == "/threads":
                    pid = q.get("project", [None])[0]
                    return self._json([t for t in w["threads"] if not pid or t["project_id"] == pid])
                if path == "/tasks":
                    pid = q.get("project", [None])[0]
                    return self._json([t for t in w["tasks"] if not pid or t["project_id"] == pid])
                if path == "/approvals":
                    return self._json(w["approvals"])
                if path == "/usage":
                    return self._json(w["usage"])
                if path == "/schedules":
                    return self._json(w["schedules"])
                if path == "/route":
                    return self._json(w["route"])
                if path == "/fs/dirs":
                    want = q.get("path", [""])[0] or w["home"]
                    want = want.rstrip("/") or "/"
                    inside = (want == w["home"] or want.startswith(w["home"] + "/")
                              or want == "/mnt" or want.startswith("/mnt/"))
                    if not inside:
                        return self._err(403, "outside $HOME and /mnt/<drive>/")
                    if want == "/mnt":
                        dirs, parent = ["c"], None
                    else:
                        entry = w["dirs"].get(want)
                        if entry is None:
                            return self._err(404, "no such directory")
                        dirs = entry
                        parent = want.rsplit("/", 1)[0] or None
                        if parent and not (parent == w["home"]
                                           or parent.startswith(w["home"] + "/")
                                           or parent.startswith("/mnt")):
                            parent = None
                    return self._json({"path": want, "parent": parent, "dirs": dirs})
                if path == "/avatars":
                    return self._json(w["avatars"])
                if path == "/voices":
                    return self._json(w["voices"])
                if path == "/models":
                    return self._json(w["models"])
                if path == "/models/catalog":
                    return self._json({"models": w["models"]["models"]})
                if path == "/avatar.svg":
                    body = HOSTILE_SVG.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/svg+xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)

                parts = [p for p in path.split("/") if p]
                # /projects/{id}/platform|tree|file
                if len(parts) == 3 and parts[0] == "projects":
                    pid, verb = parts[1], parts[2]
                    project = next((p for p in w["projects"] if p["id"] == pid), None)
                    if not project:
                        return self._err(404, "no such project")
                    if verb == "platform":
                        windows = project["root"].startswith("/mnt/")
                        return self._json({
                            "platform": "windows" if windows else "wsl",
                            "note": ("worked from WSL over 9p; git is slow and line endings "
                                     "are mangled") if windows else None,
                        })
                    if verb == "tree":
                        rel = q.get("path", [""])[0]
                        entries = w["tree"].get(rel)
                        if entries is None:
                            return self._err(404, "no such directory")
                        return self._json({"path": rel, "entries": entries})
                    if verb == "file":
                        rel = q.get("path", [""])[0]
                        if rel in w["protected"]:
                            return self._json({"path": rel, "mtime": 0, "size": 0,
                                               "protected": True})
                        rec = w["files"].get(rel)
                        if rec is None:
                            return self._err(404, "no such file")
                        return self._json({"path": rel, "content": rec["content"],
                                           "mtime": rec["mtime"],
                                           "size": len(rec["content"]), "protected": False})

                # /threads/{id}/transcript|log
                if len(parts) == 3 and parts[0] == "threads" and parts[2] == "transcript":
                    return self._json({"messages": w["transcripts"].get(parts[1], [])})

                # /tasks/{id}[/threads|journal|diff]
                if len(parts) == 2 and parts[0] == "tasks":
                    task = next((t for t in w["tasks"] if t["id"] == parts[1]), None)
                    return self._json(task) if task else self._err(404, "no such task")
                if len(parts) == 3 and parts[0] == "tasks":
                    tid, verb = parts[1], parts[2]
                    if verb == "threads":
                        return self._json(w["task_threads"].get(tid, []))
                    if verb == "journal":
                        return self._json([])
                    if verb == "diff":
                        return self._json(w["diff"])
                if len(parts) == 4 and parts[0] == "tasks" and parts[2:] == ["diff", "file"]:
                    rel = q.get("path", [""])[0]
                    rec = w["diff_files"].get(rel)
                    return self._json(rec) if rec else self._err(404, "not in the diff")

                return super().do_GET()

            # -- POST / PATCH / PUT / DELETE
            def do_POST(self):
                url = urlparse(self.path)
                path = url.path
                w = mock.world
                if path == "/stt":
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b""
                    self._record("POST", "/stt", {"bytes": len(raw),
                                                  "type": self.headers.get("Content-Type", "")})
                    return self._json({"text": w["stt_text"]})
                body = self._body()
                self._record("POST", path, body)

                if path == "/projects":
                    rec = {
                        "id": f"p{len(w['projects']) + 1}", "name": body.get("name", ""),
                        "root": body.get("root", ""), "created": "2026-09-15T00:00:00+00:00",
                        "profile": body.get("profile", "auto"),
                        "routing": {"chains": {}, "models": {}, "no_new_work": None},
                        "discord_channel_id": None,
                        "extra_dirs": body.get("extra_dirs", []),
                        "always_ask": [], "inbox": False,
                    }
                    w["projects"].append(rec)
                    return self._json(rec, 201)
                if path == "/threads":
                    rec = {
                        "id": f"t{len(w['threads']) + 1}",
                        "project_id": body.get("project_id", "p1"), "role": "chat",
                        "provider": "fast", "provider_session_id": None, "task_id": None,
                        "title": body.get("title", "new thread"),
                        "created": "2026-09-15T00:00:00+00:00",
                        "updated": "2026-09-15T00:00:00+00:00",
                        "turns": 0, "cost_usd": 0.0, "tokens": 0, "model": None, "effort": None,
                        # The folder is fixed at open, from the project's root.
                        "cwd": next((p["root"] for p in w["projects"]
                                     if p["id"] == body.get("project_id", "p1")), None),
                    }
                    w["threads"].append(rec)
                    w["transcripts"][rec["id"]] = []
                    return self._json(rec, 201)
                if path == "/tasks":
                    rec = dict(w["tasks"][0])
                    rec.update({"id": f"k{len(w['tasks']) + 1}", "state": "intake",
                                "brief": body.get("brief", "")})
                    w["tasks"].append(rec)
                    return self._json(rec, 201)
                if path == "/schedules/preview":
                    return self._json(_preview(body.get("cron"), body.get("every_s")))
                if path == "/schedules":
                    rec = {"id": f"s{len(w['schedules']) + 1}", "enabled": True,
                           "cron": body.get("cron"), "every_s": body.get("every_s"),
                           "project_id": body.get("project_id", "p1"),
                           "brief": body.get("brief", ""), "last_run_at": None,
                           "last_task_id": None, "next_run_at": None,
                           "describe": _preview(body.get("cron"), body.get("every_s"))["describe"],
                           "created": "2026-09-15T00:00:00+00:00"}
                    w["schedules"].append(rec)
                    return self._json(rec, 201)

                parts = [p for p in path.split("/") if p]
                if len(parts) == 3 and parts[0] == "threads" and parts[2] == "send":
                    # The daemon logs the user line before it answers 202, so a
                    # transcript read right after a send already has it.
                    w["transcripts"].setdefault(parts[1], []).append(
                        {"role": "user", "text": body.get("text", ""), "at": "2026-09-15T00:00:02+00:00"})
                    return self._json({"turn_id": "turn-1"}, 202)
                if len(parts) == 3 and parts[0] == "threads" and parts[2] == "interrupt":
                    return self._json({"ok": True})
                if len(parts) == 3 and parts[0] == "tasks":
                    return self._json({"ok": True})
                if len(parts) == 2 and parts[0] == "approvals":
                    req_id = parts[1]
                    w["approvals"] = [r for r in w["approvals"] if r["req_id"] != req_id]
                    return self._json({"ok": True})
                if len(parts) == 3 and parts[0] == "schedules" and parts[2] == "run-now":
                    return self._json({"ok": True})
                if path in ("/avatar", "/voice", "/model", "/mute"):
                    if path == "/avatar":
                        w["avatars"]["active"] = body.get("slug", "")
                        desc = next((a for a in w["avatars"]["avatars"]
                                     if a["slug"] == body.get("slug")), {})
                        return self._json(desc)
                    if path == "/voice":
                        w["voices"]["override"] = body.get("name", "")
                    if path == "/model" and body.get("id"):
                        w["models"]["selected"] = body["id"]
                    return self._json({"ok": True})
                return self._err(404, "no such route")

            def do_PATCH(self):
                body = self._body()
                path = urlparse(self.path).path
                self._record("PATCH", path, body)
                parts = [p for p in path.split("/") if p]
                w = mock.world
                if len(parts) == 2 and parts[0] == "projects":
                    for p in w["projects"]:
                        if p["id"] == parts[1]:
                            p.update({k: v for k, v in body.items() if k in p})
                            return self._json(p)
                    return self._err(404, "no such project")
                if len(parts) == 2 and parts[0] == "threads":
                    for t in w["threads"]:
                        if t["id"] == parts[1]:
                            # A task's threads move with their task: the rule is
                            # the backend's, and this is where the HUD learns it
                            # if it ever stops asking first.
                            if t.get("task_id"):
                                return self._err(409, "task threads move with their task")
                            pid = body.get("project_id", "")
                            if not any(p["id"] == pid for p in w["projects"]):
                                return self._err(404, "no such project")
                            t["project_id"] = pid
                            return self._json(t)
                    return self._err(404, "no such thread")
                if len(parts) == 2 and parts[0] == "schedules":
                    for s in w["schedules"]:
                        if s["id"] == parts[1]:
                            s.update(body)
                            if "cron" in body or "every_s" in body:
                                s["describe"] = _preview(s.get("cron"), s.get("every_s"))["describe"]
                            return self._json(s)
                    return self._err(404, "no such schedule")
                return self._err(404, "no such route")

            def do_PUT(self):
                body = self._body()
                path = urlparse(self.path).path
                self._record("PUT", path, body)
                parts = [p for p in path.split("/") if p]
                w = mock.world
                if len(parts) == 3 and parts[0] == "projects" and parts[2] == "file":
                    rel = body.get("path", "")
                    if rel in w["protected"]:
                        return self._err(403, "protected file")
                    rec = w["files"].get(rel)
                    if rec is None:
                        return self._err(404, "no such file")
                    if body.get("expected_mtime") != rec["mtime"]:
                        # Someone else wrote it since it was read.
                        return self._err(409, "changed on disk")
                    rec["content"] = body.get("content", "")
                    rec["mtime"] = rec["mtime"] + 1
                    return self._json({"mtime": rec["mtime"]})
                return self._err(404, "no such route")

            def do_DELETE(self):
                path = urlparse(self.path).path
                self._record("DELETE", path, {})
                parts = [p for p in path.split("/") if p]
                w = mock.world
                if len(parts) == 2 and parts[0] == "schedules":
                    w["schedules"] = [s for s in w["schedules"] if s["id"] != parts[1]]
                    return self._json({"ok": True})
                return self._err(404, "no such route")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.sse.put(None)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    # -- driving -----------------------------------------------------------

    def emit(self, kind: str, data: dict | None = None, **extra):
        """Release one SSE frame."""
        frame = {"kind": kind, "data": data or {}}
        frame.update(extra)
        self.sse.put(frame)

    def posted(self, path: str) -> list[dict]:
        return [b for m, p, b in self.calls if m == "POST" and p == path]

    def sent(self, method: str, path: str) -> list[dict]:
        return [b for m, p, b in self.calls if m == method and p == path]

    def saw(self, method: str, path: str) -> bool:
        return any(m == method and p == path for m, p, _ in self.calls)


def main():  # pragma: no cover - hand-driving helper
    port = 8478
    mock = MockDaemon(port).start()
    print(f"mock daemon on http://127.0.0.1:{port} (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mock.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
