"""A mock of `docs/hud-api.md`, for driving the built HUD v2 headlessly.

Free — no API calls, no daemon, no providers. The contract is fixed and both
WP12a (the real backend) and this file implement it, so the frontend can be
built and tested before the backend lands and then re-verified against it
(`tests/face/hud_v2_live_check.py`).

It is the v1 `hud_state_check` puppet pattern one level up: every route
answers from a mutable world the test can rewrite between assertions, and the
SSE stream is a queue the test releases one frame at a time. Nothing
here guesses at a shape the contract does not state.

Run it standalone to poke at the HUD by hand:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python \\
        tests/face/hud_v2_mock.py            # serves hud/dist on 8478
"""

from __future__ import annotations

import collections
import hashlib
import io
import math
import wave
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

# The daemon's own headers (WP-E, ED-4), never a copy: the HUD under test is
# always rendered under exactly what 8402 sends — the whole Content-Security-
# Policy with `frame-ancestors 'none'`, and `X-Frame-Options: DENY` — so every
# headless suite runs under the CSP; the mock workshop runs under the
# workshop's sandbox.
from tests.face import hud_v2_mock_browser as mock_browser
from jarvis.v2.hud_api import AVATAR_CSP, frame_headers  # noqa: E402

_HUD_LISTENER = SimpleNamespace(server=SimpleNamespace())
_PREVIEW_LISTENER = SimpleNamespace(server=SimpleNamespace(preview_only=True))

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "hud" / "dist"

try:  # rename, edit, archive, restore, delete (decisions part B)
    from tests.face import hud_v2_mock_projects as mock_projects
except ImportError:  # run as a script from tests/face
    import hud_v2_mock_projects as mock_projects  # type: ignore[no-redef]
try:  # the terminals' HTTP routes (WP-C/WP-D); the socket is the test's fake PTY
    from tests.face import hud_v2_mock_terminals as mock_terminals
except ImportError:
    import hud_v2_mock_terminals as mock_terminals  # type: ignore[no-redef]
try:  # the VEX routes (VX-1's contract, VX-2's view); builds and downloads are terminal rows
    from tests.face import hud_v2_mock_vex as mock_vex
except ImportError:
    import hud_v2_mock_vex as mock_vex  # type: ignore[no-redef]

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


def etag_of(content: str) -> str:
    """The daemon's etag (ED-2): the first 128 bits of the bytes' SHA-256."""
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def eol_of(content: str):
    crlf = content.count("\r\n")
    lf = content.count("\n") - crlf
    cr = content.count("\r") - crlf
    kinds = [n for n, c in (("LF", lf), ("CRLF", crlf), ("CR", cr)) if c]
    return None if not kinds else kinds[0] if len(kinds) == 1 else "mixed"


READONLY = {
    "env": "Jarvis can't read this file. Only you can, here.",
    "gate": "Jarvis's own permission state: read-only here. Edit it by hand.",
    "safety": "part of Jarvis's safety layer: read-only here. Edit it by hand.",
    "binary": "read-only here: it is not UTF-8.",
}


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
                {"name": ".env", "kind": "file", "size": 61, "mtime": 1757900003, "policy": "env"},
                {"name": ".env.production", "kind": "file", "size": 40, "mtime": 1757900005, "policy": "env"},
                {"name": "google_token.json", "kind": "file", "size": 61, "mtime": 1757900006, "policy": "withheld"},
                {"name": "allowlist.json", "kind": "file", "size": 3, "mtime": 1757900007},
                {"name": "notes.txt", "kind": "file", "size": 9, "mtime": 1757900008},
            ],
            "src": [
                {"name": "main.py", "kind": "file", "size": 12, "mtime": 1757900004},
            ],
        },
        "files": {
            "calc.py": {"content": "def add(a, b):\n    return a + b\n", "mtime": 1757900002},
            "README.md": {"content": "# Title\n\nSome **docs** here.\n", "mtime": 1757900001},
            "src/main.py": {"content": "print('hi')\n", "mtime": 1757900004},
            # The owner's env files (ED-2): the owner edits them, Jarvis never
            # reads them. Fake values, made up for the mock.
            ".env": {"content": "MOCK_KEY=fake-value-not-a-secret\n", "mtime": 1757900003},
            ".env.production": {"content": "API_KEY=sk-mock-ed2-fake-0000\n", "mtime": 1757900005},
            "allowlist.json": {"content": "[]\n", "mtime": 1757900007},
            "notes.txt": {"content": "a\r\nb\nc\n", "mtime": 1757900008},
        },
        # What the daemon's editor_policy says of each file (ED-2); a file not
        # here is text.
        "kinds": {".env": "env", ".env.production": "env", "allowlist.json": "gate"},
        # A withheld credential comes back 200 with no content, ever.
        "protected": {"google_token.json"},
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
        # The sidebar dots (2026-10-08): what `GET /activity` answers. A seen
        # call sets the row idle and says so on SSE, as the daemon does.
        "activity": {"threads": {}, "tasks": {}},
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
        # `GET /discord` (S1 + PR A): the Discord light reads this.
        "discord": {
            "connected": True,
            "commands": {"state": "ok", "count": 11, "synced_at": 1.0, "error": None},
            "reporter": {"state": "ok", "reason": "", "counters": {"posts": 3},
                         "dropped": 0, "last_error": None},
            # B1: the server is set up and the bot has exactly what it needs.
            "guild": {"configured": True, "id": "800000000000000001"},
            "linker": {"state": "ok", "reason": "", "pending_renames": 0, "awaiting_approval": 0},
            "permissions": {"missing": [], "excess": [], "administrator": False, "checked_at": 1.0},
        },
        "route": {
            "table": {"chains": {"orchestrator": ["claude", "codex"],
                                 "implementer": ["codex", "claude"],
                                 "reviewer": ["claude"]}},
            "states": {"claude": "available", "codex": "over_threshold", "fast": "available"},
            # PR #20 review: saved routing that runs differently now, in words.
            "notes": ["routing.json models.reviewer.codex: gpt-5.5 is not a codex model the "
                         "account offers now; using the default <b>until</b> it does"],
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
            "default": "openai/gpt-5.6-luna",
            "default_effort": "max",
            "models": [
                {"id": "openai/gpt-5.6-luna", "name": "GPT-5.6 Luna",
                 "efforts": ["low", "medium", "high", "max"], "effort": None},
                # A name off the network, carrying markup: this window draws
                # authorization cards, so it must render as text.
                {"id": "evil/model", "name": "<img src=x onerror=\"window.__pwned=1\">Bad",
                 "efforts": [], "effort": None},
            ],
        },
        # `GET /models/catalog`: every eligible OpenRouter model (tool calling
        # is the refusal, so nothing here lacks it).
        "catalog": [
            {"id": "openai/gpt-5.6-luna", "name": "GPT-5.6 Luna", "vision": True,
             "efforts": ["low", "medium", "high", "max"], "prompt_usd": 0.2, "completion_usd": 0.8},
            {"id": "moonshotai/kimi-k3", "name": "Kimi K3", "vision": True,
             "efforts": ["low", "medium", "high"], "prompt_usd": 0.6, "completion_usd": 2.5},
            {"id": "deepseek/deepseek-v4-flash-0731", "name": "DeepSeek V4 Flash", "vision": False,
             "efforts": ["low", "medium", "high"], "prompt_usd": 0.1, "completion_usd": 0.3},
            {"id": "evil/model", "name": "<img src=x onerror=\"window.__pwned=1\">Bad", "vision": True,
             "efforts": [], "prompt_usd": 0, "completion_usd": 0},
        ],
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


def _models(w) -> dict:
    """`models.describe()`'s shape: the roster, the selection, and what that
    resolves to (`current`) beside the configured `default`."""
    m = w["models"]
    return {**m, "current": m.get("selected") or m["default"],
            "default_source": "hud" if m.get("selected") else "config"}


def _remove_refusal(w, model_id) -> str | None:
    """`models.remove`'s refusals (2026-10-08), from the backend's own pure
    rule rather than a copy here that could drift from it."""
    from jarvis.models import removal_refusal
    m = w["models"]
    return removal_refusal([r["id"] for r in m["models"]], m.get("selected") or "",
                           m["default"], model_id)


def _task_verb(mock, task_id: str, verb: str, body) -> tuple[int, dict]:
    """`POST /tasks/{id}/<verb>` (§11.2): the daemon's own body check
    (`daemon.task_verb_body`) and, for an answer, the runner's own rule
    (`control.answer_refusal`) — imported, never copied. This used to answer
    `{"ok": true}` to anything, which is how the HUD sent `{question, answer}`
    to `/answer` for weeks with every check green while the daemon answered
    400 (2026-10-11). An accepted answer is applied as the runner applies it:
    the question answered, `open_question` the next blocking one, the task
    still CLARIFYING (the runner only queues it), one `task_status_changed`."""
    from jarvis.v2.control import answer_refusal
    from jarvis.v2.daemon import APIError, task_verb_body

    if verb not in ("start", "steer", "cancel", "resume", "answer"):
        return 404, {"error": "route not found"}
    try:
        args = task_verb_body(verb, body)
    except APIError as exc:
        return exc.status, {"error": str(exc)}
    except ValueError as exc:               # the dispatcher's answer to one
        return 400, {"error": f"request failed ({type(exc).__name__})"}
    if verb != "answer":
        return 200, {"ok": True}
    task = next((t for t in mock.world["tasks"] if t["id"] == task_id), None)
    if task is None:
        return 404, {"error": "no such task"}
    questions = task["spec"]["questions"]
    refusal = answer_refusal(task_id, task["state"], questions, args["index"], args["text"])
    if refusal:
        return 409, {"error": refusal}
    questions[args["index"]]["answer"] = args["text"].strip()
    remaining = [q for q in questions if q["blocking"] and q["answer"] is None]
    task["status"]["open_question"] = remaining[0]["text"] if remaining else None
    mock.emit("task_status_changed", {"task_id": task_id, "state": task["state"]}, task_id=task_id)
    return 200, task


# --- a chat thread's model (decisions 2026-10-06, A) -----------------------
# The same rules `jarvis/v2/thread_model.py` applies, small enough to read.

CLI_MODELS = {
    "claude": [{"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "vision": True,
                "efforts": ["low", "medium", "high", "xhigh", "max"]},
               {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "vision": True, "efforts": []}],
    "codex": [{"id": "gpt-6-astra", "name": "GPT-6 Astra", "vision": True,
               "efforts": ["low", "medium", "high", "xhigh"]},
              {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "vision": True,
               "efforts": ["low", "medium", "high", "xhigh"]}],
}
_LADDER = ["max", "xhigh", "high", "medium", "low", "minimal", "none"]


def _rows(w, provider):
    if provider != "fast":
        return CLI_MODELS[provider]
    out = []
    for row in w["models"]["models"]:
        cat = next((c for c in w["catalog"] if c["id"] == row["id"]), {})
        out.append({**cat, **row, "vision": cat.get("vision", row.get("vision", True))})
    return out


def _default_effort(w, provider, model):
    row = next((r for r in _rows(w, provider) if r["id"] == model), None)
    wanted = (row or {}).get("effort") or "high"
    if row is None:
        return wanted
    ladder = row.get("efforts") or []
    if not ladder:
        return None
    if wanted in ladder:
        return wanted
    i = _LADDER.index(wanted)
    for level in _LADDER[i + 1:] + list(reversed(_LADDER[:i])):
        if level in ladder:
            return level
    return ladder[0]


# Claude's and Codex's defaults with nothing chosen in the HUD (2026-10-08):
# Opus 5.5 built in, and routing's Codex default.
_BUILTIN = {"claude": ("claude-opus-5-5", "high"), "codex": ("gpt-6-astra", "xhigh")}


def _thread_models(w):
    labels = {"fast": "OpenRouter", "claude": "Claude", "codex": "Codex"}
    hud = w.setdefault("cli_defaults", {})
    providers = {}
    for p in labels:
        if p == "fast":
            default, source = _models(w)["current"], _models(w)["default_source"]
            effort = _default_effort(w, p, default)
        elif p in hud:
            default, source = hud[p]["model"], "hud"
            # No effort on routing's own Codex model is routing's effort.
            effort = hud[p]["effort"] or (_BUILTIN[p][1] if p == "codex" and default == _BUILTIN[p][0]
                                          else _default_effort(w, p, default))
        else:
            (default, effort), source = _BUILTIN[p], ("built-in" if p == "claude" else "routing")
        providers[p] = {
            "label": labels[p], "default": default, "default_effort": effort,
            "models": [dict(r, default_effort=_default_effort(w, p, r["id"])) for r in _rows(w, p)],
            "note": "", "default_source": source, "settable": p != "fast",
            "hud_default": dict(hud[p]) if p in hud else None,
            "builtin": ({"model": _BUILTIN[p][0], "effort": _BUILTIN[p][1]} if p in _BUILTIN else None),
            # `thread_model.PROFILES` / `TAKES_ALWAYS_ASK`, as the daemon sends them.
            "profiles": list(_PROFILES[p]), "always_ask": p != "codex"}
    return {"effort_default": "high", "providers": providers}


def _set_provider_default(w, body):
    """`thread_model.set_provider_default`'s refusals, in short: (status, error)
    on a refusal, else None."""
    if w.get("refuse_default"):
        return 400, w["refuse_default"]
    unknown = sorted(set(body) - {"provider", "model", "effort"})
    if unknown:
        return 400, "unknown fields: " + ", ".join(unknown)
    missing = sorted({"provider", "model"} - set(body))
    if missing:
        return 400, "missing fields: " + ", ".join(missing)
    provider, model, effort = body["provider"], body["model"], body.get("effort") or None
    if provider not in _BUILTIN:
        return 400, "the OpenRouter default is the Model picker's (POST /model)"
    hud = w.setdefault("cli_defaults", {})
    if not model:
        if effort:
            return 400, "a reset takes no effort: the built-in default brings its own"
        hud.pop(provider, None)
        return None
    row = next((r for r in CLI_MODELS[provider] if r["id"] == model), None)
    if row is None:
        return 400, f"{model} is not a {provider} model Jarvis knows"
    if effort and effort not in (row.get("efforts") or []):
        return 400, f"{model} does not offer {effort!r}"
    hud[provider] = {"model": model, "effort": effort}
    return None


_PROFILES = {"fast": ("auto", "ask"), "claude": ("auto", "ask"), "codex": ("auto",)}


def _provider_refusal(w, provider, project_id):
    """`thread_model.refusal`: why the project cannot run this provider."""
    project = next((p for p in w["projects"] if p["id"] == project_id), None)
    if project is None:
        return None
    if project["profile"] not in _PROFILES[provider]:
        return f"{provider} cannot run a project on the {project['profile']} profile"
    if project.get("always_ask") and provider == "codex":
        return "Codex cannot enforce this project's always-ask commands"
    return None


def _check_choice(w, provider, model, effort, current=None):
    """None when the choice is fine, else the refusal's words."""
    if w.get("refuse_choice"):
        return w["refuse_choice"]
    ids = [r["id"] for r in _rows(w, provider)]
    if model and model != current and model not in ids:
        return (f"{model} is not on your model roster; pin it to the roster first" if provider == "fast"
                else f"{model} is not a {provider} model Jarvis knows")
    target = model or _thread_models(w)["providers"][provider]["default"]
    row = next((r for r in _rows(w, provider) if r["id"] == target), None)
    if effort and row is not None and effort not in (row.get("efforts") or []):
        return f"{target} does not offer {effort!r}"
    return None


# POST control -> (allowed keys, required keys), exactly as
# `jarvis/v2/hud_api.py` `pickers` reads them.
PICKER_KEYS = {
    "/avatar": (("slug",), ("slug",)),
    "/voice": (("voice",), ("voice",)),
    "/model": (("model",), ("model",)),
    "/mute": (("muted",), ("muted",)),
    "/models": (("add", "remove", "model", "effort"), ()),
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


# The mock dev server's page (WP-E): whether its storage works, and the origin
# it runs under — "null" in a sandboxed frame without allow-same-origin.
DEV_PAGE = """<!doctype html><meta charset="utf-8"><title>mock dev server</title>
<h1>dev server</h1><p id="storage">?</p><p id="origin">?</p>
<script>
let r;
try { localStorage.setItem("k", "v"); r = localStorage.getItem("k") === "v" ? "ok" : "blocked"; }
catch (e) { r = "blocked"; }
document.getElementById("storage").textContent = r;
document.getElementById("origin").textContent = String(self.origin);
</script>
"""

# Dev pages that take the keyboard by themselves (R-1; amendment 2 §H AO-2).
# Each logs every key it receives to its own server (`/keys`, an image
# request, which a sandboxed page can always make), and the mock counts them
# (`dev_keys`): a key the page never got is a key the HUD kept.
#   /focus-mount  focuses its input as it is parsed, React-style on mount;
#   /focus-timer  calls window.focus() and its input's focus() every `every`
#                 ms (400), starting `after` ms (0) after it loads;
#   /focus-load   focuses once on its `load` event;
#   /focus-none   never focuses itself (it only logs keys; `grab()` from a test);
#   /focus-pointer grabs on the first pointer event it sees (move, over, wheel);
#   /focus-reload reloads itself every 400 ms and grabs once, `grab` ms (4000)
#                 after its first load (`t0` rides in the query).
# `?delay=N` holds the response N ms (at most 5000), so a page can arrive
# while the owner is typing.
FOCUS_PAGE = """<!doctype html><meta charset="utf-8"><title>mock focus page</title>
<h1>focus page</h1><input id="i" aria-label="page input"><p id="mode">%(mode)s</p>
<script>
const q = new URLSearchParams(location.search);
let n = 0;
document.addEventListener("keydown", (e) => {
  n += 1;
  new Image().src = "/keys?p=" + encodeURIComponent(location.pathname + location.search) + "&n=" + n + "&k=" + encodeURIComponent(e.key);
}, true);
const grab = () => { window.focus(); document.getElementById("i").focus(); };
const mode = %(mode_js)s;
if (mode === "focus-mount") grab();
if (mode === "focus-load") window.addEventListener("load", grab);
if (mode === "focus-timer") window.addEventListener("load", () => {
  setTimeout(() => { grab(); setInterval(grab, Number(q.get("every") || 400)); }, Number(q.get("after") || 0));
});
if (mode === "focus-pointer") {
  let done = false;
  const once = () => { if (!done) { done = true; grab(); } };
  for (const t of ["pointermove", "pointerover", "mousemove", "wheel"]) addEventListener(t, once, true);
}
if (mode === "focus-reload") {
  const t0 = Number(q.get("t0") || Date.now());
  if (Date.now() - t0 >= Number(q.get("grab") || 4000)) grab();
  else setTimeout(() => {
    const u = new URL(location.href); u.searchParams.set("t0", String(t0)); location.replace(u.toString());
  }, 400);
}
</script>
"""


def wav_tone(ms: int, hz: int = 440) -> bytes:
    """A quiet sine of `ms` milliseconds: what the mock's `/say` "speaks"."""
    rate = 8000
    frames = b"".join(int(6000 * math.sin(2 * math.pi * hz * i / rate)).to_bytes(2, "little", signed=True)
                      for i in range(rate * ms // 1000))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(frames)
    return out.getvalue()


class MockDaemon:
    """The mock server plus the calls it recorded, for the test to assert on."""

    def __init__(self, port: int):
        self.port = port
        self.world = _world()
        # The SSE frames not yet handed to a connection. Only the newest
        # `/events` connection takes frames (`_sse_gen`): a window that
        # reloads leaves its old handler blocked here, and that handler cannot
        # tell its client is gone until a *second* write fails, so with one
        # shared queue it used to take the next frame and write it into a dead
        # socket. That is how `approval_requested`, emitted shortly after the
        # dictation section's reload, went missing (the "approval-origin"
        # timeout).
        self._sse: collections.deque = collections.deque()
        self._sse_cond = threading.Condition()
        self._sse_gen = 0
        self._sse_closed = False
        self.calls: list[tuple[str, str, dict]] = []
        self.say_fail = False        # /say answers 503 (the local voice is down)
        self.ms_per_char = 12        # /say's tone length per character of text
        self.summary_text = "The change is done and all the tests pass."   # /speech/summary's answer (V8)
        self.summary_fail = False    # /speech/summary answers 502
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # The sidebar dots' races (2026-10-09): while `activity_gate` is an
        # Event, `GET /activity` takes its snapshot on arrival and answers
        # only once the Event is set, so a record emitted in between was
        # published while the snapshot was in flight. `activity_seen_quiet`
        # makes `/seen` answer with no SSE record behind it, as when the
        # window's stream is reconnecting.
        self.activity_gate: threading.Event | None = None
        self.activity_seen_quiet = False
        # `/status` says every HUD and API response refuses to be framed
        # (WP-E); a test turns it off to play a daemon too old to say so.
        self.frame_hardened = True
        self.dev_fixture = None     # AO-3 uses the real in-memory claim registry
        # While `stt_gate` is an Event, `POST /stt` is recorded on arrival and
        # answers only once the Event is set: a transcript still in flight
        # (the crash checks land one after the window stopped drawing).
        self.stt_gate: threading.Event | None = None
        # What the explorer moved to the trash (ED-7's `fs` delete).
        self.trashed: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        mock = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(DIST), **kw)

            def log_message(self, *args):
                pass

            _csp = None             # a response's own directives (the avatar's)

            def end_headers(self):
                for name, value in frame_headers(_HUD_LISTENER, self._csp):
                    self.send_header(name, value)
                self._csp = None
                super().end_headers()

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
                if path == "/manifest.webmanifest" or path.startswith("/icons/"):
                    from jarvis.v2 import hud_api
                    return hud_api.hud_files(self, path.strip("/").split("/"))
                w = mock.world
                if mock_vex.handle(self, mock, "GET", path, {}):
                    return
                if mock_projects.handle(self, mock, "GET", path, {}):
                    return
                if mock_browser.handle(self, mock, "GET", path, {}):
                    return
                if mock_terminals.handle(self, mock, "GET", path, {}):
                    return

                if mock.dev_fixture and mock.dev_fixture.handle(self, "GET", path, {k: v[0] for k, v in q.items()}):
                    return
                if path == "/dev-servers":
                    return self._json({"auto_open": True, "servers": []})
                if path == "/events":
                    mock_browser.connected(mock, q.get("window", [None])[0])
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with mock._sse_cond:
                        mock._sse_gen += 1
                        mine = mock._sse_gen
                        mock._sse_cond.notify_all()
                    while True:
                        with mock._sse_cond:
                            mock._sse_cond.wait_for(
                                lambda: mock._sse_closed or mock._sse_gen != mine or mock._sse,
                                timeout=0.5)
                            if mock._sse_closed or mock._sse_gen != mine:
                                return              # superseded: the window reconnected
                            event = mock._sse.popleft() if mock._sse else None
                        if event is None:
                            try:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                            except Exception:
                                return
                            continue
                        try:
                            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                            self.wfile.flush()
                        except Exception:
                            return

                if path == "/status":
                    # Every port the window must judge (WP-E): this mock is
                    # the HUD listener, and its API listener and workshop
                    # are listeners of their own on ephemeral ports.
                    status = {"version": "2.0-mock", "uptime_s": 1, "workshop_port": mock.workshop_port,
                              "hud_port": mock.port, "api_port": mock.api_port}
                    if mock.frame_hardened:
                        status["frame_hardened"] = True
                    return self._json(status)
                if path == "/projects":
                    # The daemon's `project_view`: each root beside where it
                    # resolves (#50). A world project may say `root_real`
                    # itself (a root through a symlink); else it is the root.
                    return self._json([{**p, "root_real": p.get("root_real") or p["root"]}
                                       for p in w["projects"]])
                if path == "/threads":
                    pid = q.get("project", [None])[0]
                    return self._json([t for t in w["threads"] if not pid or t["project_id"] == pid])
                if path == "/tasks":
                    pid = q.get("project", [None])[0]
                    return self._json([t for t in w["tasks"] if not pid or t["project_id"] == pid])
                if path == "/approvals":
                    return self._json(w["approvals"])
                if path == "/activity":
                    snapshot = json.loads(json.dumps(w["activity"]))
                    gate = mock.activity_gate
                    if gate is not None:
                        gate.wait(5)
                    return self._json(snapshot)
                if path == "/usage":
                    return self._json(w["usage"])
                if path == "/discord":
                    return self._json(w["discord"])
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
                    return self._json(_models(w))
                if path == "/thread-models":
                    return self._json(_thread_models(w))
                if path == "/models/catalog":
                    return self._json({"models": w["catalog"],
                                       "roster": [m["id"] for m in w["models"]["models"]],
                                       "stale": ""})
                if path == "/avatar.svg":
                    body = HOSTILE_SVG.encode()
                    self.send_response(200)
                    self._csp = AVATAR_CSP          # as the daemon's `pickers` sends it
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
                    if verb == "files":
                        # Quick open's list (ED-7): never a withheld file or
                        # the gate's state; env files listed and said.
                        self._record("GET", path, {})
                        return self._json(mock.nav_files())
                    if verb == "tree":
                        rel = q.get("path", [""])[0]
                        entries = w["tree"].get(rel)
                        if entries is None:
                            return self._err(404, "no such directory")
                        return self._json({"path": rel, "entries": entries})
                    if verb == "file":
                        rel = q.get("path", [""])[0]
                        abs_ = mock.abs_of(project, rel)
                        if rel in w["protected"]:
                            return self._json({"path": rel, "abs": abs_, "kind": "withheld", "editable": False,
                                               "readonly": "a credential file: its contents are never sent to this window.",
                                               "protected": True})
                        rec = w["files"].get(rel)
                        if rec is None:
                            return self._err(404, "no such file")
                        kind = w["kinds"].get(rel, "text")
                        content = rec["content"]
                        # The daemon's `editable` (ED-2's review, MED1): an
                        # editable kind the editor still may not write — an
                        # env file that is not UTF-8 — is in `uneditable`.
                        editable = kind in ("text", "env") and rel not in w.get("uneditable", ())
                        return self._json({
                            "path": rel, "abs": abs_, "content": content, "etag": etag_of(content),
                            "mtime": rec["mtime"], "size": len(content.encode()), "protected": False,
                            "encoding": "utf-8" if editable or kind != "env" else None,
                            "eol": eol_of(content), "kind": kind, "editable": editable,
                            "readonly": (READONLY.get(kind) if editable or kind != "env"
                                         else READONLY["env"] + " And read-only here: it holds NUL bytes.")})

                # /projects/{id}/git/base|status (ED-9). The real refusals are
                # the daemon's (tests/v2/editor_git_check.py); these mirror
                # their shape. No `git` in the world: not a repository.
                if len(parts) == 4 and parts[0] == "projects" and parts[2] == "git":
                    return mock.git_route(parts[3], q, self)

                # /threads/{id}/transcript|log
                if len(parts) == 3 and parts[0] == "threads" and parts[2] == "transcript":
                    if q.get("tools") == ["1"] and w.get("transcript_tools_unsupported"):
                        return self._err(400, "unknown query field: tools")
                    rows = w["transcripts"].get(parts[1], [])
                    if q.get("tools") != ["1"]:
                        rows = [{k: v for k, v in row.items() if k != "turn_id"} for row in rows if row.get("role") != "tool"]
                    return self._json({"messages": rows})

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
                parts = [p for p in path.split("/") if p]
                if len(parts) == 3 and parts[0] == "projects" and parts[2] in ("search", "fs"):
                    # Find in files and the explorer's operations (ED-7). The
                    # real refusals are the daemon's (tests/v2/editor_nav_check.py);
                    # these mirror their shape.
                    body = self._body()
                    self._record("POST", path, body)
                    status, payload = (mock.nav_search(body) if parts[2] == "search" else mock.nav_fs(body))
                    return self._json(payload, status)
                if len(parts) == 4 and parts[0] == "projects" and parts[2:] == ["files", "stat"]:
                    body = self._body()
                    self._record("POST", path, body)
                    paths = body.get("paths")
                    if not isinstance(paths, list):
                        return self._err(400, "paths must be a list of strings")
                    if len(paths) > 100:
                        return self._err(413, "at most 100 paths at once")
                    files = {}
                    for rel in paths:
                        if rel in w["protected"]:
                            files[rel] = {"kind": "withheld"}
                        elif rel in w["files"]:
                            # `stat_etags` (on cue): what the poll says while a
                            # read already sees a newer write — a disk that moved
                            # again between the stat and the read.
                            files[rel] = {"etag": w.get("stat_etags", {}).get(rel)
                                          or etag_of(w["files"][rel]["content"]),
                                          "kind": w["kinds"].get(rel, "text")}
                        else:
                            files[rel] = {"gone": True}
                    return self._json({"files": files})
                if path == "/stt":
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b""
                    self._record("POST", "/stt", {"bytes": len(raw),
                                                  "type": self.headers.get("Content-Type", "")})
                    gate = mock.stt_gate
                    if gate is not None:
                        gate.wait(10)
                    return self._json({"text": w["stt_text"]})
                body = self._body()
                self._record("POST", path, body)
                if mock.dev_fixture and mock.dev_fixture.handle(self, "POST", path, body):
                    return
                if mock_vex.handle(self, mock, "POST", path, body):
                    return
                if mock_projects.handle(self, mock, "POST", path, body):
                    return
                if mock_browser.handle(self, mock, "POST", path, body):
                    return
                if mock_terminals.handle(self, mock, "POST", path, body):
                    return

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
                    # The real daemon's body: provider and brief optional for a
                    # chat thread, the model checked before anything exists.
                    unknown = set(body) - {"project_id", "role", "provider", "brief"}
                    if unknown:
                        return self._err(400, "unknown fields: " + ", ".join(sorted(unknown)))
                    provider = body.get("provider", "fast")
                    if provider not in ("fast", "claude", "codex"):
                        return self._err(400, f"{provider!r} is not a valid ProviderName")
                    brief = body.get("brief") or {}
                    refusal = (_provider_refusal(w, provider, body.get("project_id", "p1"))
                               or _check_choice(w, provider, brief.get("model"), brief.get("effort")))
                    if refusal:
                        return self._err(400, refusal)
                    rec = {
                        "id": f"t{len(w['threads']) + 1}",
                        "project_id": body.get("project_id", "p1"), "role": "chat",
                        "provider": provider, "provider_session_id": None, "task_id": None,
                        "title": "new thread",
                        "created": "2026-09-15T00:00:00+00:00",
                        "updated": "2026-09-15T00:00:00+00:00",
                        "turns": 0, "cost_usd": 0.0, "tokens": 0,
                        "model": brief.get("model"), "effort": brief.get("effort"),
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
                    if w.get("fail_send"):
                        w["fail_send"] -= 1
                        return self._err(409, w.get("fail_send_error") or "thread session is opening or closing")
                    # The daemon logs the user line before it answers 202, so a
                    # transcript read right after a send already has it.
                    row = {"role": "user", "text": body.get("text", ""), "at": "2026-09-15T00:00:02+00:00"}
                    w["transcripts"].setdefault(parts[1], []).append(row)
                    # `Daemon.deliver` (2026-10-08): `send_status` scripts what
                    # a send during a running turn became.
                    status = w.get("send_status") or "started"
                    w["sends"] = w.get("sends", 0) + 1
                    reply = {"status": status, "turn_id": f"turn-{w['sends']}"}
                    row["turn_id"] = reply["turn_id"]
                    if status != "started":
                        reply.update(message_id=f"msg-{w['sends']}",
                                     mode="native" if status == "steered" else None,
                                     position=1)
                    if "message_id" in reply: row["message_id"] = reply["message_id"]
                    return self._json(reply, 202)
                if len(parts) == 3 and parts[0] == "threads" and parts[2] == "interrupt":
                    return self._json({"ok": True})
                if len(parts) == 3 and parts[0] in ("threads", "tasks") and parts[2] == "seen":
                    of = parts[0]
                    status = w["activity"][of].get(parts[1], "idle")
                    if status in ("unread", "failed"):
                        w["activity"][of].pop(parts[1], None)
                        if not mock.activity_seen_quiet:
                            mock.emit("activity", {"of": of[:-1], "id": parts[1], "project_id": "p1",
                                                   "status": "idle"})
                        status = "idle"
                    return self._json({"status": status})
                if len(parts) == 3 and parts[0] == "tasks":
                    status, payload = _task_verb(mock, parts[1], parts[2], body)
                    return self._json(payload, status)
                if len(parts) == 2 and parts[0] == "approvals":
                    req_id = parts[1]
                    w["approvals"] = [r for r in w["approvals"] if r["req_id"] != req_id]
                    mock_vex.approval_decided(mock, req_id, body)      # VX-3's project_folder card
                    return self._json({"ok": True})
                if len(parts) == 3 and parts[0] == "schedules" and parts[2] == "run-now":
                    return self._json({"ok": True})
                if path == "/thread-models":
                    # Claude's or Codex's default (2026-10-08): the daemon's
                    # keys and refusals, and one `model` event per change.
                    refused = _set_provider_default(w, body)
                    if refused:
                        return self._err(*refused)
                    payload = _thread_models(w)
                    info = payload["providers"][body["provider"]]
                    mock.emit("model", {"provider": body["provider"], "default": info["default"],
                                        "default_effort": info["default_effort"],
                                        "default_source": info["default_source"]})
                    return self._json(payload)
                if path == "/say":
                    # V6: the real route's contract -- known keys only, a voice
                    # from the catalog, `local_only` a boolean -- answering a
                    # tone as long as the text (`ms_per_char`), so a test can
                    # count what was spoken without a speaker.
                    unknown = sorted(set(body) - {"text", "voice", "instructions", "local_only"})
                    if unknown:
                        return self._err(400, f"unknown fields ({len(unknown)})")
                    if not str(body.get("text", "")).strip():
                        return self._err(400, "no text to say")
                    names = {v["name"] for v in w["voices"]["voices"]}
                    if body.get("voice") is not None and body["voice"] not in names:
                        return self._err(400, "unknown voice")
                    if not isinstance(body.get("local_only", False), bool):
                        return self._err(400, "local_only must be a boolean")
                    if mock.say_fail:
                        return self._err(503, "local speech unavailable")
                    audio = wav_tone(max(80, len(body["text"]) * mock.ms_per_char))
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(audio)))
                    self.end_headers()
                    self.wfile.write(audio)
                    return
                if path == "/speech/summary":
                    # V8: the real route's contract -- thread_id and text required,
                    # nothing else -- answering
                    # `summary_text` (or a 502 when `summary_fail`).
                    unknown = sorted(set(body) - {"thread_id", "text"})
                    if unknown:
                        return self._err(400, f"unknown fields ({len(unknown)})")
                    if not isinstance(body.get("thread_id"), str) or not isinstance(body.get("text"), str):
                        return self._err(400, "thread_id and text must be strings")
                    if mock.summary_fail:
                        return self._err(502, "the summary could not be made")
                    return self._json({"summary": mock.summary_text})
                if path in PICKER_KEYS:
                    # The real daemon's keys, enforced the way it enforces
                    # them: an unknown key is a 400 naming it. The mock used
                    # to accept {id}/{name}/{mute}, which is how the suite
                    # passed while every click reset the setting (A6).
                    allowed, required = PICKER_KEYS[path]
                    unknown = sorted(set(body) - set(allowed))
                    missing = sorted(set(required) - set(body))
                    if unknown:
                        return self._err(400, "unknown fields: " + ", ".join(unknown))
                    if missing:
                        return self._err(400, "missing fields: " + ", ".join(missing))
                    if path == "/avatar":
                        w["avatars"]["active"] = body.get("slug", "")
                        desc = next((a for a in w["avatars"]["avatars"]
                                     if a["slug"] == body.get("slug")), {})
                        return self._json(desc)
                    if path == "/voice":
                        w["voices"]["override"] = body["voice"]
                        return self._json({"voice": body["voice"]})
                    if path == "/mute":
                        if not isinstance(body["muted"], bool):
                            return self._err(400, "muted must be a boolean")
                        w["muted"] = body["muted"]
                        return self._json({"ok": True, "muted": body["muted"]})
                    if path == "/model":
                        ids = [m["id"] for m in w["models"]["models"]]
                        if body["model"] and body["model"] not in ids:
                            return self._err(404, "not on the roster")
                        w["models"]["selected"] = body["model"]
                        if not body["model"] and w["models"]["default"] not in ids:
                            # Reset to config default re-lists the env model.
                            found = next((m for m in w["catalog"] if m["id"] == w["models"]["default"]), None)
                            w["models"]["models"].insert(0, dict(found or {"id": w["models"]["default"],
                                                                           "name": w["models"]["default"]},
                                                                 effort=None))
                        return self._json(_models(w))
                    given = [k for k in ("add", "remove", "model") if body.get(k)]
                    if len(given) != 1:
                        return self._err(400, "expected exactly one of add, remove, or model with effort")
                    if "effort" in body and not body.get("model"):
                        return self._err(400, "effort goes with {model, effort}, not with add or remove")
                    rows = w["models"]["models"]
                    if body.get("add"):
                        if not any(m["id"] == body["add"] for m in rows):
                            found = next((m for m in w["catalog"] if m["id"] == body["add"]), None)
                            if found is None:
                                return self._err(400, "not an OpenRouter model that supports tool calling")
                            rows.append(dict(found, effort=None))
                    elif body.get("remove"):
                        if not any(m["id"] == body["remove"] for m in rows):
                            return self._err(404, f"{body['remove']!r} is not on the roster")
                        why = _remove_refusal(w, body["remove"])
                        if why:
                            return self._err(409, why)
                        if w["models"].get("selected") == body["remove"]:
                            w["models"]["selected"] = ""
                        w["models"]["models"] = [m for m in rows if m["id"] != body["remove"]]
                    else:
                        row = next((m for m in rows if m["id"] == body["model"]), None)
                        if row is None:
                            return self._err(404, "not on the roster")
                        row["effort"] = body.get("effort") or None
                    return self._json(_models(w))
                return self._err(404, "no such route")

            def do_PATCH(self):
                body = self._body()
                path = urlparse(self.path).path
                self._record("PATCH", path, body)
                if mock_browser.handle(self, mock, "PATCH", path, body):
                    return
                if mock.dev_fixture and mock.dev_fixture.handle(self, "PATCH", path, body):
                    return
                if mock_terminals.handle(self, mock, "PATCH", path, body):
                    return
                parts = [p for p in path.split("/") if p]
                if len(parts) == 2 and parts[0] == "threads" and isinstance(body, dict):
                    # The daemon's rule: rename, move and model change are
                    # three mutually exclusive request shapes.
                    kinds = [k for k, keys in (("rename", {"title"}), ("move", {"project_id"}),
                                               ("model", {"model", "effort"})) if body.keys() & keys]
                    if len(kinds) != 1:
                        return self._err(400, "rename, move and model change are separate requests" if kinds
                                         else "missing fields: title, project_id, or model/effort")
                if mock_projects.handle(self, mock, "PATCH", path, body):
                    return
                w = mock.world
                if len(parts) == 2 and parts[0] == "projects":
                    for p in w["projects"]:
                        if p["id"] == parts[1]:
                            p.update({k: v for k, v in body.items() if k in p})
                            return self._json(p)
                    return self._err(404, "no such project")
                if len(parts) == 2 and parts[0] == "threads" and ("model" in body or "effort" in body):
                    # A chat thread's model and effort (decisions A1-A7), with
                    # the real daemon's refusals and its two broadcasts.
                    t = next((t for t in w["threads"] if t["id"] == parts[1]), None)
                    if t is None:
                        return self._err(404, "no such thread")
                    if set(body) - {"model", "effort"}:
                        return self._err(400, "unknown fields: " + ", ".join(sorted(set(body) - {"model", "effort"})))
                    if t.get("task_id") or t.get("role") != "chat":
                        return self._err(409, "a task's threads run the model routing gave them")
                    home = next((p for p in w["projects"] if p["id"] == t["project_id"]), {})
                    if t.get("archived") or home.get("archived"):
                        return self._err(409, "restore the thread before changing its model")
                    model = body["model"] if "model" in body else t.get("model")
                    effort = body.get("effort") if "model" in body else body["effort"]
                    refusal = _check_choice(w, t["provider"], model, effort, current=t.get("model"))
                    if refusal:
                        return self._err(400, refusal)
                    # An effort alone keeps a default thread on the default
                    # model (A4 amendment): the record holds the effort only.
                    was_default = t.get("model") is None
                    t["model"], t["effort"] = model, effort
                    shown = model or _thread_models(w)["providers"][t["provider"]]["default"]
                    shown_effort = effort or _default_effort(w, t["provider"], shown)
                    if model is None and was_default:
                        text = (f"effort → {effort or 'default · ' + (shown_effort or 'none')}"
                                f" (default model: {shown})")
                    else:
                        why = "default, from the next message" if model is None else "from the next message"
                        text = f"model → {shown}" + (f" · {shown_effort}" if shown_effort else "") + f" ({why})"
                    w["transcripts"].setdefault(t["id"], []).append(
                        {"role": "system", "text": text, "at": "2026-09-15T00:00:03+00:00"})
                    mock.emit("model_set", {"text": text, "model": model, "effort": effort},
                              thread_id=t["id"], project_id=t["project_id"])
                    mock.emit("thread_updated", dict(t, thread_id=t["id"], changed=["model", "effort"]),
                              thread_id=t["id"], project_id=t["project_id"])
                    # A slow answer, on cue (`patch_delays`, seconds, one per
                    # request): the change is made and broadcast at once, but
                    # this request's own answer — the record as it was then —
                    # arrives late, after any change sent behind it.
                    answer = dict(t)
                    delays = w.get("patch_delays")
                    if delays:
                        time.sleep(delays.pop(0))
                    return self._json(answer)
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
                    kind = w["kinds"].get(rel, "text")
                    if rel in w["protected"] or kind not in ("text", "env") or rel in w.get("uneditable", ()):
                        return self._err(403, "read-only here")
                    rec = w["files"].get(rel)
                    if "expected_etag" in body:
                        now = etag_of(rec["content"]) if rec else None
                        if now != body["expected_etag"]:
                            # Someone else wrote it since it was read: the
                            # disk's etag (null: gone), the file untouched.
                            return self._json({"error": "file changed on disk since it was read", "etag": now}, 409)
                    elif rec is None or body.get("expected_mtime") != rec["mtime"]:
                        return self._err(409, "changed on disk")
                    if rec is None:
                        rec = w["files"][rel] = {"content": "", "mtime": 1757900100}
                    rec["content"] = body.get("content", "")
                    rec["mtime"] = rec["mtime"] + 1
                    answer = {"etag": etag_of(rec["content"]), "mtime": rec["mtime"],
                              "abs": mock.abs_of(next(p for p in w["projects"] if p["id"] == parts[1]), rel)}
                    # A slow answer, on cue (`put_delays`, seconds, one per
                    # write): the file is written at once, the answer late.
                    delays = w.get("put_delays")
                    if delays:
                        time.sleep(delays.pop(0))
                    return self._json(answer)
                return self._err(404, "no such route")

            def do_DELETE(self):
                path = urlparse(self.path).path
                self._record("DELETE", path, {})
                if mock_projects.handle(self, mock, "DELETE", path, {}):
                    return
                mock_vex.terminal_closing(mock, path)
                if mock_browser.handle(self, mock, "DELETE", path, {}):
                    return
                if mock_terminals.handle(self, mock, "DELETE", path, {}):
                    return
                parts = [p for p in path.split("/") if p]
                w = mock.world
                if len(parts) == 2 and parts[0] == "schedules":
                    w["schedules"] = [s for s in w["schedules"] if s["id"] != parts[1]]
                    return self._json({"ok": True})
                return self._err(404, "no such route")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        # Port 0 asks for an ephemeral one; the bound port is the one to use.
        self.port = self.httpd.server_address[1]
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

        # The mock's own workshop origin, reported on /status, so the Preview
        # tab never builds a URL to the owner's live daemon on 8403 (which is
        # what put `GET /p/p1/index.html -> 400` in the live log).
        class Workshop(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def end_headers(self):
                for name, value in frame_headers(_PREVIEW_LISTENER):
                    self.send_header(name, value)
                super().end_headers()

            def do_GET(self):
                mock.workshop_hits.append(self.path)
                # `probe.html` says what origin it runs under and whether its
                # storage works (the keep-origin hijack check, WP-E).
                body = (DEV_PAGE.encode() if self.path.endswith("/probe.html")
                        else b"<!doctype html><title>mock preview</title><h1>preview</h1>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.workshop_hits: list[str] = []
        self.workshop = ThreadingHTTPServer(("127.0.0.1", 0), Workshop)
        self.workshop.daemon_threads = True
        self.workshop_port = self.workshop.server_address[1]
        threading.Thread(target=self.workshop.serve_forever, daemon=True).start()

        # The daemon's API listener, apart from the HUD's (WP-E): a port of
        # its own, reported on /status, that the window must never load into
        # a frame. Every hit is recorded, and the suites assert there is none.
        class Api(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def end_headers(self):
                for name, value in frame_headers(_HUD_LISTENER):
                    self.send_header(name, value)
                super().end_headers()

            def do_GET(self):
                mock.api_hits.append(self.path)
                body = b'{"error": "the API listener is never framed"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.api_hits: list[str] = []
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        self.api.daemon_threads = True
        self.api_port = self.api.server_address[1]
        threading.Thread(target=self.api.serve_forever, daemon=True).start()

        # A local dev server on a port of its own (WP-E): what a Preview
        # pane's "keep its own origin" is for. Its page says whether its
        # storage works and what origin it runs under.
        class Dev(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                mock.dev_hits.append(self.path)
                body = DEV_PAGE.encode()
                route, _, query = self.path.partition("?")
                if route == "/keys":
                    # A key a focus page received (FOCUS_PAGE): counted, never shown.
                    mock.dev_keys.append(self.path)
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                if route in ("/focus-mount", "/focus-timer", "/focus-load", "/focus-none", "/focus-pointer",
                             "/focus-reload"):
                    args = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                    try:
                        delay = min(max(int(args.get("delay", "0")), 0), 5000)
                    except ValueError:
                        delay = 0
                    if delay:
                        time.sleep(delay / 1000)
                    mode = route[1:]
                    body = (FOCUS_PAGE % {"mode": mode, "mode_js": json.dumps(mode)}).encode()
                if self.path.startswith("/to-workshop"):
                    # A dev page that sends its own frame to the workshop: the
                    # frame keeps the sandbox flags it was given, so with
                    # "keep its origin" on it would run with the workshop's
                    # real origin unless the workshop sandboxes itself.
                    target = f"http://127.0.0.1:{mock.workshop_port}/p/p1/probe.html"
                    body = f"<!doctype html><script>location.replace({json.dumps(target)})</script>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self.dev_hits: list[str] = []
        self.dev_keys: list[str] = []
        self.dev = ThreadingHTTPServer(("127.0.0.1", 0), Dev)
        self.dev.daemon_threads = True
        self.dev_port = self.dev.server_address[1]
        threading.Thread(target=self.dev.serve_forever, daemon=True).start()
        return self

    def stop(self):
        with self._sse_cond:
            self._sse_closed = True
            self._sse_cond.notify_all()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        for name in ("workshop", "api", "dev"):
            server = getattr(self, name, None)
            if server is not None:
                server.shutdown()
                server.server_close()

    # -- driving -----------------------------------------------------------

    def activity(self, of: str, object_id: str, status: str):
        """Set one row's status in the world and say so on SSE."""
        table = self.world["activity"][of + "s"]
        if status == "idle":
            table.pop(object_id, None)
        else:
            table[object_id] = status
        self.emit("activity", {"of": of, "id": object_id, "project_id": "p1", "status": status})

    def thread_running(self, thread_id: str, since: str | None):
        """A turn runs on that thread since `since` (ISO), as `GET /threads`
        says it: the daemon's live `running` and `running_since` (WP-H, H-b).
        `None` takes both away again — absent, as an older daemon's are, so
        the HUD's 15 s reconcile leaves every other test's busy panes alone."""
        for t in self.world["threads"]:
            if t["id"] != thread_id:
                continue
            if since is None:
                t.pop("running", None)
                t.pop("running_since", None)
            else:
                t["running"] = True
                t["running_since"] = since

    def emit(self, kind: str, data: dict | None = None, **extra):
        """Release one SSE frame."""
        frame = {"kind": kind, "data": data or {}}
        frame.update(extra)
        if self.dev_fixture:
            self.dev_fixture.publish(frame)
            return
        with self._sse_cond:
            self._sse.append(frame)
            self._sse_cond.notify_all()

    def sse_connections(self) -> int:
        """How many `/events` connections have opened; the newest one is live."""
        with self._sse_cond:
            return self._sse_gen

    def await_reconnect(self, before: int, timeout: float = 6.0) -> bool:
        """After a reload: wait for the window's new `/events` connection, so
        a frame emitted next cannot be handed to the old page's dead one."""
        with self._sse_cond:
            return self._sse_cond.wait_for(lambda: self._sse_gen > before, timeout=timeout)

    def posted(self, path: str) -> list[dict]:
        return [b for m, p, b in self.calls if m == "POST" and p == path]

    # -- the disk behind the editor (ED-2) -------------------------------------

    def abs_of(self, project: dict, rel: str) -> str:
        return project["root"].rstrip("/") + "/" + rel

    def disk_write(self, rel: str, content: str):
        """Something other than the editor writes a file (an agent, a shell)."""
        rec = self.world["files"].setdefault(rel, {"content": "", "mtime": 1757900200})
        rec["content"] = content
        rec["mtime"] += 1

    def disk_delete(self, rel: str):
        self.world["files"].pop(rel, None)

    # -- the editor's git (ED-9) -----------------------------------------------
    #
    # `world["git"]` (absent: not a repository): {"head", "branch", "base":
    # {rel: HEAD's text}, "files": {rel: state}, "dirs": {rel: state}}.
    # `git_base_calls` counts the base reads, for the suite.

    def git_route(self, verb: str, q: dict, handler):
        w = self.world
        repo = w.get("git")
        if verb == "status":
            if not repo:
                return handler._json({"repo": False, "head": None, "branch": None, "files": {}, "dirs": {},
                                      "truncated": False})
            return handler._json({"repo": True, "head": repo.get("head"), "branch": repo.get("branch", "main"),
                                  "files": dict(repo.get("files", {})), "dirs": dict(repo.get("dirs", {})),
                                  "truncated": False})
        if verb != "base":
            return handler._err(404, "route not found")
        rel = q.get("path", [""])[0]
        self.git_base_calls = getattr(self, "git_base_calls", 0) + 1
        # A slow repository, on cue (`base_delay`, seconds: git on /mnt/c).
        if repo and repo.get("base_delay"):
            time.sleep(float(repo["base_delay"]))
        if w["kinds"].get(rel) == "env":
            return handler._err(403, "an env file: its committed text is not served here (the gutter is off for it).")
        if rel in w["protected"] or w["kinds"].get(rel) in ("gate", "withheld"):
            return handler._err(403, "its committed text is not served here (the gutter is off for it).")
        # A file deleted from the working tree still answers what HEAD holds
        # (`deleted: true`); nothing on disk and nothing in HEAD is 404.
        deleted = rel not in w["files"]
        if deleted and not (repo and rel in repo.get("base", {})):
            return handler._err(404, "file not found")
        if not repo:
            return handler._json({"path": rel, "state": "no_repo", "head": None, "text": None})
        extra = {"deleted": True} if deleted else {}
        # `states` (on cue): binary, too_large, not_a_file, as the daemon says them.
        state = repo.get("states", {}).get(rel)
        if state:
            return handler._json({"path": rel, "state": state, "head": repo.get("head"), "text": None, **extra})
        text = repo.get("base", {}).get(rel)
        if text is None:
            return handler._json({"path": rel, "state": "new", "head": repo.get("head"), "text": None})
        try:
            return handler._json({"path": rel, "state": "ok", "head": repo.get("head"), "text": text,
                                  "size": len(text.encode()), "renamed": False, **extra})
        except (BrokenPipeError, ConnectionResetError):
            return None  # the window dropped it (aborted) while a slow answer was on its way

    # -- the editor's navigation (ED-7) ----------------------------------------

    def _hidden_from_nav(self, rel: str) -> bool:
        w = self.world
        return rel in w["protected"] or w["kinds"].get(rel) in ("gate", "withheld")

    def nav_files(self) -> dict:
        w = self.world
        files = sorted(r for r in w["files"] if not self._hidden_from_nav(r))
        return {"files": files, "env": [r for r in files if w["kinds"].get(r) == "env"],
                "truncated": False, "source": "git"}

    def nav_search(self, body: dict):
        import re as _re
        w = self.world
        query = body.get("query")
        if not isinstance(query, str) or not query:
            return 400, {"error": "query must be 1 to 1000 characters"}
        flags = 0 if body.get("case") else _re.IGNORECASE
        try:
            pattern = query if body.get("regex") else _re.escape(query)
            if body.get("word"):
                pattern = rf"\b(?:{pattern})\b"
            rx = _re.compile(pattern, flags)
        except _re.error as exc:
            return 400, {"error": f"not a regular expression: {exc}"}
        matches = []
        for rel in sorted(w["files"]):
            # The daemon's filter: never a withheld file, the gate's state or an env file.
            if self._hidden_from_nav(rel) or w["kinds"].get(rel) == "env":
                continue
            for i, line in enumerate(w["files"][rel]["content"].splitlines(), 1):
                ranges = [[m.start(), m.end()] for m in rx.finditer(line) if m.end() > m.start()]
                if ranges:
                    matches.append({"path": rel, "line": i, "text": line, "ranges": ranges[:20]})
        return 200, {"matches": matches[:2000], "files": len({m["path"] for m in matches}),
                     "truncated": len(matches) > 2000, "timed_out": False, "backend": "mock", "limit": 2000}

    def _tree_add(self, rel: str, kind: str):
        tree = self.world["tree"]
        parent, _, name = rel.rpartition("/")
        if parent and parent not in tree:
            self._tree_add(parent, "dir")
        entries = tree.setdefault(parent, [])
        if not any(e["name"] == name for e in entries):
            row = {"name": name, "kind": kind, "size": 0, "mtime": 1757900300}
            if kind == "file" and self.world["kinds"].get(rel) == "env":
                row["policy"] = "env"
            entries.append(row)
            entries.sort(key=lambda e: e["name"])
        if kind == "dir":
            tree.setdefault(rel, [])

    def _tree_remove(self, rel: str):
        tree = self.world["tree"]
        parent, _, name = rel.rpartition("/")
        tree[parent] = [e for e in tree.get(parent, []) if e["name"] != name]
        for key in [k for k in tree if k == rel or k.startswith(rel + "/")]:
            del tree[key]

    def _exists(self, rel: str) -> bool:
        w = self.world
        return (rel in w["files"] or rel in w["tree"]
                or any(e["name"] == rel.rpartition("/")[2] for e in w["tree"].get(rel.rpartition("/")[0], [])))

    def nav_fs(self, body: dict):
        from jarvis.tools.secrets import is_env_credential_name, is_protected
        w = self.world
        op, rel, to = body.get("op"), body.get("path"), body.get("to")
        if op not in ("new_file", "new_folder", "rename", "delete") or not isinstance(rel, str) or not rel:
            return 400, {"error": "op and path"}

        def refused(name: str) -> bool:
            base = name.rpartition("/")[2]
            return ((is_protected(base) and not is_env_credential_name(base)) or ".." in name.split("/")
                    or self._hidden_from_nav(name))
        if refused(rel) or (isinstance(to, str) and refused(to)):
            return 403, {"error": "refused: the editor never changes this"}
        if op in ("new_file", "new_folder"):
            if self._exists(rel):
                return 409, {"error": "something is already there"}
            if op == "new_file":
                w["files"][rel] = {"content": "", "mtime": 1757900300}
                if is_env_credential_name(rel.rpartition("/")[2]):
                    w["kinds"][rel] = "env"
            self._tree_add(rel, "file" if op == "new_file" else "dir")
            return 200, {"ok": True, "op": op, "path": rel, "abs": f"/home/johnw/projects/Jarvis/{rel}",
                         "kind": w["kinds"].get(rel, "text")}
        if not self._exists(rel):
            return 404, {"error": "nothing is there"}
        is_dir = rel in w["tree"]
        if op == "delete":
            self.trashed.append(rel)
            for key in [k for k in w["files"] if k == rel or k.startswith(rel + "/")]:
                del w["files"][key]
            self._tree_remove(rel)
            return 200, {"ok": True, "op": op, "path": rel, "abs": f"/home/johnw/projects/Jarvis/{rel}",
                         "dir": is_dir, "trash": {"where": "linux", "location": "/mock/Trash"}}
        if not isinstance(to, str) or not to:
            return 400, {"error": "rename needs `to`"}
        if self._exists(to):
            return 409, {"error": f"{to} already exists: a rename never replaces anything"}
        if w["kinds"].get(rel) == "env" and not is_env_credential_name(to.rpartition("/")[2]):
            return 403, {"error": "an env file keeps an env name here"}
        for key in [k for k in list(w["files"]) if k == rel or k.startswith(rel + "/")]:
            new = to + key[len(rel):]
            w["files"][new] = w["files"].pop(key)
            if key in w["kinds"]:
                w["kinds"][new] = w["kinds"].pop(key)
        if is_env_credential_name(to.rpartition("/")[2]):
            w["kinds"][to] = "env"
        entries = {k: v for k, v in w["tree"].items() if k == rel or k.startswith(rel + "/")}
        self._tree_remove(rel)
        for key, rows in entries.items():
            w["tree"][to + key[len(rel):]] = rows
        self._tree_add(to, "dir" if is_dir else "file")
        return 200, {"ok": True, "op": op, "path": to, "from": rel,
                     "abs": f"/home/johnw/projects/Jarvis/{to}", "from_abs": f"/home/johnw/projects/Jarvis/{rel}",
                     "dir": is_dir, "kind": w["kinds"].get(to, "text")}

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
