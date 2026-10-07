"""Rename, edit, archive, restore and permanently delete (decisions part B).

**Archive instead of delete (B1).** Archiving a project or a chat thread sets
an `archived` stamp and nothing else: every record, log, journal and cost
stays. What changes is visibility. An archived project is dropped from
`GET /projects|threads|tasks|schedules`, `router.place` (Discord's
`in <name>:`), Discord's channel placement and the schedule tools, and its
schedules are paused (not deleted). Restoring undoes exactly that.

**Permanent delete is the only delete, and it goes to a trash (B2).** Only an
archived project or thread can be deleted. Its records are first moved, one
atomic `os.replace` each, into a staging folder under the data root (so they
leave every list at once), and the staging folder then goes to the trash as
one entry (`trash.py`). Nothing outside Jarvis's data root is touched: not the
project's folder, not a worktree, not a branch. Worktrees are listed in the
confirmation and left on disk (B3).

**The store trap the plan found.** `_Store._delete` rmtrees the parent of a
record's path. Projects are stored flat (`projects/<id>.json`), so on a
project it would wipe the whole `projects/` directory. Nothing here calls it:
every move below names its exact path.

**Only the owner, in the HUD (B11).** The routes that archive, restore or
delete are owner-only: they answer only on the HUD's own listener
(`FACE_PORT`) and only to a request that carries the HUD's `Origin`. That is
a speed bump, not a boundary — the boundary is that **no tool reaches these
routes** (`tests/v2/archive_check.py` asserts it), and a shell command an
agent might use to call one goes through the permission gate like every other
command, the same footing as `/approvals`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .model import TERMINAL_STATES, Project, Role, Thread, to_json, utcnow
from .stores import StoreError, _write_bytes
from . import worktrees

_SUFFIX = re.compile(r"^(.*?) \((\d+)\)$")
TITLE_CAP = 200


# -- names (B4 + B10) ---------------------------------------------------------

def name_key(name: str) -> str:
    """What two names are compared on: case and surrounding spaces ignored."""
    return (name or "").strip().casefold()


def unique_name(wanted: str, taken) -> str:
    """`wanted`, or `wanted (1)`, `(2)`, … — the first one nobody has.

    A name that already ends in ` (n)` is numbered from its stem, so renaming a
    second project to `site (1)` while that exists gives `site (2)`, not
    `site (1) (1)`. Mirrored by `uniqueName` in `hud/src/lib/projects.ts`.
    """
    base = (wanted or "").strip()
    keys = {name_key(t) for t in taken if t}
    if name_key(base) not in keys:
        return base
    match = _SUFFIX.match(base)
    stem = match.group(1) if match else base
    n = 1
    while name_key(f"{stem} ({n})") in keys:
        n += 1
    return f"{stem} ({n})"


def project_names_taken(stores, *, but: str | None = None) -> list[str]:
    """Every project's name, archived ones included, plus the Inbox's — which
    is reserved whether or not the Inbox has been created yet."""
    from .daemon import safe_list
    return [p.name for p in safe_list(stores.projects) if p.id != but] + ["Inbox"]


def thread_titles_taken(stores, project_id: str, *, but: str | None = None) -> list[str]:
    from .daemon import safe_list
    return [t.title for t in safe_list(stores.threads, project_id=project_id)
            if t.id != but and t.title]


# -- visibility -----------------------------------------------------------------

def archived_project_ids(stores) -> set[str]:
    from .daemon import safe_list
    return {p.id for p in safe_list(stores.projects) if p.archived}


def visible_thread(thread: Thread, hidden: set[str]) -> bool:
    return not thread.archived and thread.project_id not in hidden


def is_archived(stores, project_id: str | None) -> bool:
    if not project_id:
        return False
    try:
        project = stores.projects.get(project_id)
    except StoreError:
        return False
    return bool(project and project.archived)


def refuse_archived_project(project: Project) -> None:
    from .daemon import DaemonError
    if project.archived:
        raise DaemonError(f"project {project.name} is archived; restore it from the Archive first")


# -- the owner-only gate ----------------------------------------------------------

def owner_only(handler, daemon) -> None:
    """The HUD's listener and the HUD's Origin, or 403. See the module note."""
    from .daemon import APIError
    port = handler.server.server_address[1]
    host = handler.headers.get("Host", "")
    origin = handler.headers.get("Origin")
    if port != daemon.face_port or not origin or origin != f"http://{host}":
        raise APIError(403, "only the owner can do this, from the HUD")


# -- reading what an action would touch ------------------------------------------

def _threads_of(stores, project_id):
    from .daemon import safe_list
    return safe_list(stores.threads, project_id=project_id)


def _tasks_of(stores, project_id):
    from .daemon import safe_list
    return safe_list(stores.tasks, project_id=project_id)


def _schedules_of(daemon, project_id):
    return [s for s in daemon.schedules.list() if s.get("project_id") == project_id]


def _running_turns(daemon, project_id):
    with daemon._lock:
        return [s.thread for s in daemon._sessions.values()
                if s.worker is not None and s.thread.project_id == project_id]


def _label(thread) -> str:
    return thread.title or thread.id


def impact(daemon, project_id: str) -> dict:
    """What archiving, deleting or re-rooting this project would touch. Read-only:
    it never creates the Inbox and never writes a record."""
    from .schedules import describe
    stores = daemon.stores
    project = daemon.require(stores.projects, project_id)
    threads = _threads_of(stores, project.id)
    tasks = _tasks_of(stores, project.id)
    schedules = _schedules_of(daemon, project.id)
    active = [t for t in tasks if t.state not in TERMINAL_STATES]
    running = _running_turns(daemon, project.id)
    trees = []
    for task in tasks:
        if task.worktree:
            trees.append({"task_id": task.id, "path": task.worktree, "branch": task.branch,
                          "exists": Path(task.worktree).exists()})
    blockers = [f"task {t.id} is {t.state.value}: {' '.join(t.brief.split())[:80]}" for t in active]
    blockers += [f"a chat turn is running in thread {_label(t)}" for t in running]
    root = project.root.rstrip("/")
    chat = [t for t in threads if not t.task_id]
    counted = sorted([("thread", t.id, bool(t.archived)) for t in threads]
                     + [("task", t.id, t.state.value) for t in tasks]
                     + [("schedule", s["id"], s.get("enabled")) for s in schedules]
                     + [("project", project.id, project.root)], key=lambda r: (r[0], r[1]))
    token = hashlib.sha256(json.dumps(counted).encode()).hexdigest()[:16]
    rows = []
    for s in schedules:
        try:
            text = describe(s.get("cron"), s.get("every_s"))
        except (TypeError, ValueError):
            text = ""
        rows.append({"id": s["id"], "brief": s.get("brief", ""), "describe": text,
                     "enabled": bool(s.get("enabled"))})
    return {
        "project_id": project.id, "name": project.name, "root": project.root,
        "inbox": project.inbox, "archived": project.archived, "token": token,
        "blockers": blockers,
        "chat_threads": {"count": len(chat), "archived": sum(bool(t.archived) for t in chat)},
        "tasks": {"total": len(tasks), "finished": len(tasks) - len(active),
                  "active": [{"id": t.id, "brief": t.brief, "state": t.state.value} for t in active]},
        "task_threads": len(threads) - len(chat),
        "running_turns": [{"thread_id": t.id, "title": _label(t)} for t in running],
        "schedules": rows,
        "worktrees": trees,
        # What stays on the current folder if the root changes (B5).
        "on_root": {
            "threads": sum(1 for t in threads if (t.cwd or "").rstrip("/") == root),
            "tasks": [{"id": t.id, "brief": t.brief, "state": t.state.value}
                      for t in active if (t.root or project.root).rstrip("/") == root],
        },
        "discord_channel_id": project.discord_channel_id,
    }


# -- projects -------------------------------------------------------------------

def _expect(expect, current):
    from .daemon import APIError, DaemonError
    if not isinstance(expect, str) or not expect:
        raise APIError(400, "expect is required: read /impact first")
    if expect != current["token"]:
        raise DaemonError("the project changed since you looked; review it again")


def _close_idle(daemon, thread_ids) -> None:
    """Close every open session among these threads. A running turn refuses."""
    from .daemon import DaemonError
    for thread_id in thread_ids:
        with daemon._lock:
            session = daemon._sessions.get(thread_id)
            if session is None:
                continue
            if session.worker is not None:
                raise DaemonError(f"a chat turn is running in thread {_label(session.thread)}")
            if session.handle is None or session.closing:
                raise DaemonError(f"thread {_label(session.thread)} is opening or closing; try again")
        daemon.close_thread(thread_id)


def archive_project(daemon, project_id: str, expect) -> dict:
    from .daemon import DaemonError
    stores = daemon.stores
    project = daemon.require(stores.projects, project_id)
    if project.inbox:
        raise DaemonError("The Inbox cannot be archived: it is where unplaced chat lands")
    if project.archived:
        raise DaemonError(f"project {project.name} is already archived")
    seen = impact(daemon, project_id)
    _expect(expect, seen)
    if seen["blockers"]:
        raise DaemonError("cannot archive yet: " + "; ".join(seen["blockers"]))
    _close_idle(daemon, [t.id for t in _threads_of(stores, project_id)])
    paused = []
    with daemon.schedules.lock, daemon._lock:
        daemon._active()
        again = impact(daemon, project_id)
        _expect(expect, again)
        if again["blockers"]:
            raise DaemonError("cannot archive yet: " + "; ".join(again["blockers"]))
        project = daemon.require(stores.projects, project_id)
        for record in _schedules_of(daemon, project_id):
            if record.get("enabled"):
                record.update(enabled=False, next_run_at=None, paused_by_archive=True)
                daemon.schedules._save(record)
                paused.append(record)
        project.archived = utcnow()
        stores.projects.save(project)
    for record in paused:
        daemon.schedules._changed("schedule_updated", record)
    daemon.bus.publish({"kind": "project_archived", "project_id": project.id,
                        "data": {"project_id": project.id, "name": project.name,
                                 "paused_schedules": [r["id"] for r in paused]}})
    return {"project": to_json(project), "paused_schedules": [r["id"] for r in paused]}


def restore_project(daemon, project_id: str) -> dict:
    from .daemon import DaemonError
    stores = daemon.stores
    resumed = []
    with daemon.schedules.lock, daemon._lock:
        daemon._active()
        project = daemon.require(stores.projects, project_id)
        if not project.archived:
            raise DaemonError(f"project {project.name} is not archived")
        project.name = unique_name(project.name, project_names_taken(stores, but=project.id))
        project.archived = None
        stores.projects.save(project)
        now = daemon.schedules.clock()
        for record in _schedules_of(daemon, project_id):
            if record.pop("paused_by_archive", False):
                record["enabled"] = True
                record["next_run_at"] = daemon.schedules._next(record, now)
                daemon.schedules._save(record)
                resumed.append(record)
    for record in resumed:
        daemon.schedules._changed("schedule_updated", record)
    daemon.bus.publish({"kind": "project_restored", "project_id": project.id,
                        "data": {"project_id": project.id, "name": project.name,
                                 "resumed_schedules": [r["id"] for r in resumed]}})
    return to_json(project)


def _staging(daemon, label: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = daemon.stores.root / "deleting" / f"{stamp}-{label}-{uuid.uuid4().hex[:6]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _stage(source: Path, dest: Path) -> bool:
    """Move one record (file or directory) into the staging folder, atomically."""
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, dest)
    return True


def _to_trash(daemon, staging: Path) -> dict:
    try:
        return daemon.trash.put(staging)
    except Exception as exc:                      # the records are already out of every list
        return {"where": "staged", "path": str(staging),
                "error": f"{type(exc).__name__}: {exc}"}


def delete_project(daemon, project_id: str, expect) -> dict:
    from .daemon import DaemonError
    stores = daemon.stores
    project = daemon.require(stores.projects, project_id)
    if not project.archived:
        raise DaemonError("archive the project first; only an archived project can be deleted")
    seen = impact(daemon, project_id)
    _expect(expect, seen)
    if seen["blockers"]:
        raise DaemonError("cannot delete yet: " + "; ".join(seen["blockers"]))
    _close_idle(daemon, [t.id for t in _threads_of(stores, project_id)])
    with daemon.schedules.lock, daemon._lock, worktrees._lock:
        daemon._active()
        again = impact(daemon, project_id)
        _expect(expect, again)
        threads = _threads_of(stores, project_id)
        tasks = _tasks_of(stores, project_id)
        schedules = _schedules_of(daemon, project_id)
        staging = _staging(daemon, f"project-{project_id}")
        _write_bytes(staging / "manifest.json", json.dumps({
            "kind": "project", "deleted": utcnow(), "project": to_json(project),
            "threads": [t.id for t in threads], "tasks": [t.id for t in tasks],
            "schedules": [s["id"] for s in schedules], "left_on_disk": again["worktrees"],
        }, indent=2).encode())
        # Order: what references the project first, the project record last, so
        # a crash midway leaves an archived project a second delete finishes.
        for record in schedules:
            _stage(daemon.schedules._path(record["id"]), staging / "schedules" / f"{record['id']}.json")
        for task in tasks:
            _stage(stores.tasks.path(task.id).parent, staging / "tasks" / task.id)
            stores.tasks._pending.pop(task.id, None)
        for thread in threads:
            _stage(stores.threads.path(thread.id).parent, staging / "threads" / thread.id)
            stores.threads._pending.pop(thread.id, None)
        _stage(stores.projects.path(project_id), staging / "projects" / f"{project_id}.json")
    for record in schedules:
        daemon.bus.publish({"kind": "schedule_deleted", "project_id": project_id,
                            "data": {"schedule_id": record["id"]}})
    trash = _to_trash(daemon, staging)
    result = {"deleted": project_id, "name": project.name,
              "removed": {"threads": len(threads), "tasks": len(tasks), "schedules": len(schedules)},
              "left_on_disk": [w["path"] for w in again["worktrees"] if w["exists"]],
              "trash": trash}
    daemon.bus.publish({"kind": "project_deleted", "project_id": project_id,
                        "data": {"project_id": project_id, "name": project.name,
                                 "removed_thread_ids": [t.id for t in threads],
                                 "removed_task_ids": [t.id for t in tasks]}})
    return result


# -- threads ----------------------------------------------------------------------

def _chat_only(thread, verb):
    from .daemon import DaemonError
    if thread.task_id is not None or thread.role != Role.CHAT:
        raise DaemonError(f"task threads cannot be {verb} on their own; they go with their task's project")


def rename_thread(daemon, thread_id: str, title) -> dict:
    from .daemon import APIError, DaemonError
    if not isinstance(title, str) or not title.strip():
        raise APIError(400, "title must be a nonempty string")
    title = " ".join(title.split())[:TITLE_CAP]
    stores = daemon.stores
    with daemon._lock:
        daemon._active()
        thread = daemon.require(stores.threads, thread_id)
        if thread.archived or is_archived(stores, thread.project_id):
            raise DaemonError("restore the thread before renaming it")
        title = unique_name(title, thread_titles_taken(stores, thread.project_id, but=thread.id))
        if title == thread.title:
            return daemon.thread_json(thread)
        thread.title = title
        session = daemon._sessions.get(thread.id)
        if session is not None:
            # The live session writes its own Thread on usage and finish; a
            # rename it did not hear would be undone by the next turn.
            session.thread.title = title
        stores.threads.save(thread)
    daemon.bus.publish({"kind": "thread_updated", "thread_id": thread.id,
                        "project_id": thread.project_id,
                        "data": {"thread_id": thread.id, "title": thread.title, "changed": ["title"]}})
    return daemon.thread_json(thread)


def archive_thread(daemon, thread_id: str) -> dict:
    from .daemon import DaemonError
    stores = daemon.stores
    thread = daemon.require(stores.threads, thread_id)
    _chat_only(thread, "archived")
    if thread.archived:
        raise DaemonError("the thread is already archived")
    if is_archived(stores, thread.project_id):
        raise DaemonError("its project is archived already")
    _close_idle(daemon, [thread.id])
    with daemon._lock:
        daemon._active()
        if thread.id in daemon._sessions:
            raise DaemonError(f"thread {_label(thread)} was reopened; try again")
        thread = daemon.require(stores.threads, thread_id)
        thread.archived = utcnow()
        stores.threads.save(thread)
    daemon.bus.publish({"kind": "thread_archived", "thread_id": thread.id,
                        "project_id": thread.project_id,
                        "data": {"thread_id": thread.id, "project_id": thread.project_id}})
    return daemon.thread_json(thread)


def restore_thread(daemon, thread_id: str) -> dict:
    from .daemon import DaemonError
    stores = daemon.stores
    with daemon._lock:
        daemon._active()
        thread = daemon.require(stores.threads, thread_id)
        if not thread.archived:
            raise DaemonError("the thread is not archived")
        project = daemon.require(stores.projects, thread.project_id)
        if project.archived:
            raise DaemonError(f"restore project {project.name} first; this thread is archived with it")
        if thread.title:
            thread.title = unique_name(thread.title, thread_titles_taken(stores, thread.project_id, but=thread.id))
        thread.archived = None
        stores.threads.save(thread)
    daemon.bus.publish({"kind": "thread_restored", "thread_id": thread.id,
                        "project_id": thread.project_id,
                        "data": {"thread_id": thread.id, "project_id": thread.project_id,
                                 "title": thread.title}})
    return daemon.thread_json(thread)


def delete_thread(daemon, thread_id: str) -> dict:
    from .daemon import DaemonError
    stores = daemon.stores
    thread = daemon.require(stores.threads, thread_id)
    _chat_only(thread, "deleted")
    if not thread.archived and not is_archived(stores, thread.project_id):
        raise DaemonError("archive the thread first; only an archived thread can be deleted")
    _close_idle(daemon, [thread.id])
    with daemon._lock:
        daemon._active()
        if thread.id in daemon._sessions:
            raise DaemonError(f"thread {_label(thread)} was reopened; try again")
        staging = _staging(daemon, f"thread-{thread.id}")
        _write_bytes(staging / "manifest.json", json.dumps({
            "kind": "thread", "deleted": utcnow(), "thread": to_json(thread)}, indent=2).encode())
        _stage(stores.threads.path(thread.id).parent, staging / "threads" / thread.id)
        stores.threads._pending.pop(thread.id, None)
    trash = _to_trash(daemon, staging)
    daemon.bus.publish({"kind": "thread_deleted", "thread_id": thread.id,
                        "project_id": thread.project_id,
                        "data": {"thread_id": thread.id, "project_id": thread.project_id}})
    return {"deleted": thread.id, "trash": trash}


# -- the archive view ---------------------------------------------------------------

def archive_view(daemon) -> dict:
    from .daemon import safe_list
    stores = daemon.stores
    projects = safe_list(stores.projects)
    archived = {p.id: p for p in projects if p.archived}
    names = {p.id: p.name for p in projects}
    threads = safe_list(stores.threads)
    tasks = safe_list(stores.tasks)
    rows = []
    for project in sorted(archived.values(), key=lambda p: p.archived or "", reverse=True):
        mine = [t for t in threads if t.project_id == project.id]
        rows.append({**to_json(project),
                     "threads": [daemon.thread_json(t) for t in mine if not t.task_id],
                     "task_threads": sum(1 for t in mine if t.task_id),
                     "tasks": [{"id": t.id, "brief": t.brief, "state": t.state.value,
                                "worktree": t.worktree, "branch": t.branch}
                               for t in tasks if t.project_id == project.id],
                     "schedules": len(_schedules_of(daemon, project.id))})
    loose = [dict(daemon.thread_json(t), project_name=names.get(t.project_id, ""))
             for t in threads if t.archived and t.project_id not in archived]
    loose.sort(key=lambda t: t.get("archived") or "", reverse=True)
    return {"projects": rows, "threads": loose, "trash": daemon.trash.describe()}


def recover_staging(daemon) -> int:
    """Send any staging folder a crash left behind to the trash. Startup only."""
    root = daemon.stores.root / "deleting"
    moved = 0
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.is_dir() and "error" not in _to_trash(daemon, path):
                moved += 1
    return moved


# -- routes --------------------------------------------------------------------------

def route(handler, daemon, parts, query):
    """The archive, restore, delete and trash routes. None for anything else."""
    from .daemon import _object
    method = handler.command
    if parts == ["archive"] and method == "GET":
        _object(query, ())
        return 200, archive_view(daemon)
    if parts == ["trash"] and method == "GET":
        _object(query, ())
        return 200, {**daemon.trash.describe(),
                     "items": daemon.trash.public(daemon.trash.entries())}
    if parts == ["trash", "empty"] and method == "POST":
        owner_only(handler, daemon)
        _object(query, ())
        _object(handler._body(), ())
        return 200, {"removed": daemon.trash.empty()}
    if len(parts) == 3 and parts[0] == "projects" and parts[2] == "impact" and method == "GET":
        _object(query, ())
        return 200, impact(daemon, parts[1])
    if len(parts) == 3 and parts[0] == "projects" and parts[2] in ("archive", "restore") and method == "POST":
        owner_only(handler, daemon)
        _object(handler._body(), ())
        if parts[2] == "archive":
            _object(query, ("expect",))
            return 200, archive_project(daemon, parts[1], query.get("expect"))
        _object(query, ())
        return 200, restore_project(daemon, parts[1])
    if len(parts) == 2 and parts[0] == "projects" and method == "DELETE":
        owner_only(handler, daemon)
        _object(query, ("expect",))
        return 200, delete_project(daemon, parts[1], query.get("expect"))
    if len(parts) == 3 and parts[0] == "threads" and parts[2] in ("archive", "restore") and method == "POST":
        owner_only(handler, daemon)
        _object(query, ())
        _object(handler._body(), ())
        if parts[2] == "archive":
            return 200, archive_thread(daemon, parts[1])
        return 200, restore_thread(daemon, parts[1])
    if len(parts) == 2 and parts[0] == "threads" and method == "DELETE":
        owner_only(handler, daemon)
        _object(query, ())
        return 200, delete_thread(daemon, parts[1])
    return None
