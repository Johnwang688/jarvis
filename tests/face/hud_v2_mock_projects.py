"""The mock's half of decisions part B: rename, edit, archive, restore, delete.

Kept beside `hud_v2_mock.py` rather than inside it so the shared mock only
gains one hook line per method. `handle()` answers the routes this feature
added (and the list filtering archiving implies) from the same mutable world,
in the shapes `docs/hud-api.md` states, and returns False for everything else.

The rules mirrored here are the backend's, stated once more for the window to
be graded against: a colliding name is numbered, never refused; an archived
project is hidden from every list; archive and delete take the token the
confirmation was read from; unfinished tasks block an archive; only something
archived can be deleted, and a delete goes to the trash.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

TERMINAL = {"done", "failed", "cancelled"}
_SUFFIX = re.compile(r"^(.*?) \((\d+)\)$")


def _key(name):
    return (name or "").strip().casefold()


def unique_name(wanted, taken):
    """The backend's `jarvis.v2.projects.unique_name`, without importing jarvis."""
    base = (wanted or "").strip()
    keys = {_key(t) for t in taken if t}
    if _key(base) not in keys:
        return base
    match = _SUFFIX.match(base)
    stem = match.group(1) if match else base
    n = 1
    while _key(f"{stem} ({n})") in keys:
        n += 1
    return f"{stem} ({n})"


def _state(w):
    w.setdefault("trash", {"location": "/home/johnw/.local/share/Trash", "retention_days": 30, "entries": 0})
    w.setdefault("running_turns", {})        # project id -> [{thread_id, title}]
    w.setdefault("deleted", [])
    return w


def _archived_ids(w):
    return {p["id"] for p in w["projects"] if p.get("archived")}


def impact(w, pid):
    project = next((p for p in w["projects"] if p["id"] == pid), None)
    if project is None:
        return None
    threads = [t for t in w["threads"] if t["project_id"] == pid]
    chat = [t for t in threads if not t.get("task_id")]
    tasks = [k for k in w["tasks"] if k["project_id"] == pid]
    active = [k for k in tasks if k["state"] not in TERMINAL]
    schedules = [s for s in w["schedules"] if s["project_id"] == pid]
    running = w["running_turns"].get(pid, [])
    counted = sorted([["thread", t["id"], bool(t.get("archived"))] for t in threads]
                     + [["task", k["id"], k["state"]] for k in tasks]
                     + [["schedule", s["id"], s.get("enabled")] for s in schedules]
                     + [["project", pid, project["root"]]])
    token = hashlib.sha256(json.dumps(counted).encode()).hexdigest()[:16]
    blockers = [f"task {k['id']} is {k['state']}: {k['brief'][:80]}" for k in active]
    blockers += [f"a chat turn is running in thread {r['title']}" for r in running]
    root = project["root"].rstrip("/")
    return {
        "project_id": pid, "name": project["name"], "root": project["root"],
        "inbox": project.get("inbox", False), "archived": project.get("archived"), "token": token,
        "blockers": blockers,
        "chat_threads": {"count": len(chat), "archived": sum(bool(t.get("archived")) for t in chat)},
        "tasks": {"total": len(tasks), "finished": len(tasks) - len(active),
                  "active": [{"id": k["id"], "brief": k["brief"], "state": k["state"]} for k in active]},
        "task_threads": len(threads) - len(chat),
        "running_turns": running,
        "schedules": [{"id": s["id"], "brief": s.get("brief", ""), "describe": s.get("describe", ""),
                       "enabled": bool(s.get("enabled"))} for s in schedules],
        "worktrees": [{"task_id": k["id"], "path": k["worktree"], "branch": k.get("branch"), "exists": True}
                      for k in tasks if k.get("worktree")],
        "on_root": {"threads": sum(1 for t in threads if (t.get("cwd") or "").rstrip("/") == root),
                    "tasks": [{"id": k["id"], "brief": k["brief"], "state": k["state"]} for k in active]},
        "discord_channel_id": project.get("discord_channel_id"),
    }


def _project_names(w, but=None):
    return [p["name"] for p in w["projects"] if p["id"] != but] + ["Inbox"]


def _root_ok(w, root):
    """An existing directory, as far as this world knows: one it lists, one a
    listing names, or one a project already uses."""
    root = (root or "").rstrip("/")
    parent, _, name = root.rpartition("/")
    return (root in w["dirs"] or name in w["dirs"].get(parent, [])
            or any(p["root"] == root for p in w["projects"]))


def handle(h, mock, method, path, body) -> bool:
    """Answer one request if it is this feature's; False lets the mock go on."""
    w = _state(mock.world)
    query = {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items()}
    parts = [p for p in path.split("/") if p]
    hidden = _archived_ids(w)

    if method == "GET":
        if path == "/projects":
            h._json([p for p in w["projects"] if not p.get("archived")])
            return True
        if path == "/threads":
            pid = query.get("project")
            h._json([t for t in w["threads"] if not t.get("archived") and t["project_id"] not in hidden
                      and (not pid or t["project_id"] == pid)])
            return True
        if path == "/tasks":
            pid = query.get("project")
            h._json([k for k in w["tasks"] if k["project_id"] not in hidden
                      and (not pid or k["project_id"] == pid)])
            return True
        if path == "/schedules":
            h._json([s for s in w["schedules"] if s["project_id"] not in hidden])
            return True
        if path == "/archive":
            names = {p["id"]: p["name"] for p in w["projects"]}
            h._json({
                "projects": [{**p,
                              "threads": [t for t in w["threads"] if t["project_id"] == p["id"]
                                          and not t.get("task_id")],
                              "task_threads": 0,
                              "tasks": [{"id": k["id"], "brief": k["brief"], "state": k["state"],
                                         "worktree": k.get("worktree"), "branch": k.get("branch")}
                                        for k in w["tasks"] if k["project_id"] == p["id"]],
                              "schedules": sum(1 for s in w["schedules"] if s["project_id"] == p["id"])}
                             for p in w["projects"] if p.get("archived")],
                "threads": [{**t, "project_name": names.get(t["project_id"], "")}
                            for t in w["threads"] if t.get("archived") and t["project_id"] not in hidden],
                "trash": w["trash"],
            })
            return True
        if len(parts) == 3 and parts[0] == "projects" and parts[2] == "impact":
            found = impact(w, parts[1])
            h._json(found) if found else h._err(404, "no such project")
            return True
        return False

    if method == "POST":
        if path == "/projects":
            if not _root_ok(w, body.get("root", "")):
                h._err(400, "root must be an existing directory")
                return True
            rec = {
                "id": f"p{len(w['projects']) + len(w['deleted']) + 1}",
                "name": unique_name(body.get("name", ""), _project_names(w)),
                "root": body.get("root", ""), "created": "2026-09-15T00:00:00+00:00",
                "profile": body.get("profile", "auto"),
                "routing": {"chains": {}, "models": {}, "no_new_work": None},
                "discord_channel_id": None, "extra_dirs": body.get("extra_dirs", []),
                "always_ask": [], "inbox": False, "archived": None,
            }
            w["projects"].append(rec)
            h._json(rec, 201)
            return True
        if len(parts) == 3 and parts[0] == "projects" and parts[2] in ("archive", "restore"):
            project = next((p for p in w["projects"] if p["id"] == parts[1]), None)
            if project is None:
                h._err(404, "no such project")
                return True
            if parts[2] == "restore":
                if not project.get("archived"):
                    h._err(409, "not archived")
                    return True
                project["name"] = unique_name(project["name"], _project_names(w, project["id"]))
                project["archived"] = None
                for s in w["schedules"]:
                    if s["project_id"] == project["id"] and s.pop("paused_by_archive", False):
                        s["enabled"] = True
                h._json(project)
                return True
            if project.get("inbox"):
                h._err(409, "The Inbox cannot be archived: it is where unplaced chat lands")
                return True
            seen = impact(w, project["id"])
            if not query.get("expect"):
                h._err(400, "expect is required: read /impact first")
            elif query["expect"] != seen["token"]:
                h._err(409, "the project changed since you looked; review it again")
            elif seen["blockers"]:
                h._err(409, "cannot archive yet: " + "; ".join(seen["blockers"]))
            else:
                paused = []
                for s in w["schedules"]:
                    if s["project_id"] == project["id"] and s.get("enabled"):
                        s.update(enabled=False, paused_by_archive=True)
                        paused.append(s["id"])
                project["archived"] = "2026-10-06T12:00:00+00:00"
                h._json({"project": project, "paused_schedules": paused})
            return True
        if len(parts) == 3 and parts[0] == "threads" and parts[2] in ("archive", "restore"):
            thread = next((t for t in w["threads"] if t["id"] == parts[1]), None)
            if thread is None:
                h._err(404, "no such thread")
                return True
            if parts[2] == "archive":
                if thread.get("task_id"):
                    h._err(409, "task threads cannot be archived on their own")
                    return True
                thread["archived"] = "2026-10-06T12:00:00+00:00"
            else:
                taken = [t["title"] for t in w["threads"] if t["project_id"] == thread["project_id"]
                         and t["id"] != thread["id"] and t.get("title")]
                thread["title"] = unique_name(thread["title"], taken) if thread.get("title") else ""
                thread["archived"] = None
            h._json(thread)
            return True
        if path == "/trash/empty":
            removed = w["trash"]["entries"]
            w["trash"]["entries"] = 0
            h._json({"removed": removed})
            return True
        return False

    if method == "PATCH":
        if len(parts) == 2 and parts[0] == "projects":
            project = next((p for p in w["projects"] if p["id"] == parts[1]), None)
            if project is None:
                return False
            if w.get("refuse_project_patch"):
                h._err(409, w["refuse_project_patch"])
                return True
            if project.get("archived"):
                h._err(409, f"project {project['name']} is archived; restore it from the Archive first")
                return True
            if project.get("inbox") and ("name" in body or "root" in body):
                h._err(409, "inbox name and root cannot change")
                return True
            if "root" in body and body["root"] != project["root"] and not _root_ok(w, body["root"]):
                h._err(400, "root must be an existing directory")
                return True
            update = {k: v for k, v in body.items() if k in project}
            if "name" in update and _key(update["name"]) != _key(project["name"]):
                update["name"] = unique_name(update["name"], _project_names(w, project["id"]))
            project.update(update)
            h._json(project)
            return True
        if len(parts) == 2 and parts[0] == "threads" and "title" in body:
            thread = next((t for t in w["threads"] if t["id"] == parts[1]), None)
            if thread is None:
                h._err(404, "no such thread")
                return True
            if set(body) != {"title"}:
                h._err(400, "unknown fields: project_id")
                return True
            taken = [t["title"] for t in w["threads"] if t["project_id"] == thread["project_id"]
                     and t["id"] != thread["id"] and t.get("title")]
            thread["title"] = unique_name(" ".join(str(body["title"]).split()), taken)
            h._json(thread)
            return True
        return False

    if method == "DELETE":
        if len(parts) == 2 and parts[0] == "projects":
            project = next((p for p in w["projects"] if p["id"] == parts[1]), None)
            if project is None:
                h._err(404, "no such project")
                return True
            if not project.get("archived"):
                h._err(409, "archive the project first; only an archived project can be deleted")
                return True
            seen = impact(w, project["id"])
            if query.get("expect") != seen["token"]:
                h._err(409, "the project changed since you looked; review it again")
                return True
            pid = project["id"]
            gone_threads = [t["id"] for t in w["threads"] if t["project_id"] == pid]
            gone_tasks = [k["id"] for k in w["tasks"] if k["project_id"] == pid]
            gone_schedules = [s["id"] for s in w["schedules"] if s["project_id"] == pid]
            w["projects"] = [p for p in w["projects"] if p["id"] != pid]
            w["threads"] = [t for t in w["threads"] if t["project_id"] != pid]
            w["tasks"] = [k for k in w["tasks"] if k["project_id"] != pid]
            w["schedules"] = [s for s in w["schedules"] if s["project_id"] != pid]
            w["deleted"].append(pid)
            w["trash"]["entries"] += 1
            h._json({"deleted": pid, "name": project["name"],
                     "removed": {"threads": len(gone_threads), "tasks": len(gone_tasks),
                                 "schedules": len(gone_schedules)},
                     "left_on_disk": [x["path"] for x in seen["worktrees"]],
                     "trash": {"where": "linux", "location": w["trash"]["location"], "name": pid}})
            return True
        if len(parts) == 2 and parts[0] == "threads":
            thread = next((t for t in w["threads"] if t["id"] == parts[1]), None)
            if thread is None:
                h._err(404, "no such thread")
                return True
            if not thread.get("archived") and thread["project_id"] not in hidden:
                h._err(409, "archive the thread first; only an archived thread can be deleted")
                return True
            w["threads"] = [t for t in w["threads"] if t["id"] != thread["id"]]
            if w.get("trash_fails"):
                # The records left every list, but the move to the trash failed:
                # `_to_trash`'s staged result.
                h._json({"deleted": thread["id"], "trash": {
                    "where": "staged", "path": "/data/deleting/x-thread-" + thread["id"],
                    "error": w["trash_fails"]}})
                return True
            w["trash"]["entries"] += 1
            h._json({"deleted": thread["id"], "trash": {"where": "linux", "location": w["trash"]["location"]}})
            return True
        return False
    return False
