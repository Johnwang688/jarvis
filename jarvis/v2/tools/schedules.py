"""Schedule tools registered with v1; mutations go through the v2 daemon.

The daemon owns the scheduler lock and publishes HUD events. Using its HTTP
API also works in the separate MCP process without a second scheduler/store.
"""
from __future__ import annotations

import http.client
import json
import re
from typing import Annotated

from jarvis import config, runtime
from jarvis.tools import tool
from ..schedules import ACCEPTED_WHEN, parse_when
from ..stores import Stores


def _request(method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", config.DAEMON_PORT, timeout=10)
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None,
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        if response.status >= 400:
            raise ValueError(result.get("error", "schedule request failed"))
        return json.dumps(result)
    except OSError as exc:
        raise ValueError("cannot reach the v2 daemon") from exc
    finally:
        connection.close()


def _project(name):
    stores = Stores()
    if not isinstance(name, str):
        raise ValueError("project must be a name or id")
    name = name.strip()
    if not name:
        # Optional runtime accessors: current v1 runtime exposes neither.
        # Never infer a caller from a process-global session or its cwd.
        project_id = getattr(runtime, "project_id", lambda: None)()
        thread_id = getattr(runtime, "thread_id", lambda: None)()
        if not project_id and thread_id:
            thread = stores.threads.get(thread_id)
            if thread is None:
                raise ValueError("calling thread not found")
            project_id = thread.project_id
        if project_id:
            project = stores.projects.get(project_id)
            if project is None or project.archived:
                raise ValueError("calling project not found")
            return project.id
        return stores.projects.inbox().id
    # Archived projects are invisible to tools (decisions B1).
    projects = [p for p in stores.projects.list() if not p.archived]
    by_id = next((p for p in projects if p.id == name), None)
    if by_id:
        return by_id.id
    matches = [p for p in projects if p.name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError("project name is ambiguous; use its id" if matches else "project not found")
    return matches[0].id


@tool
def schedule_create(
    brief: Annotated[str, "The task to run at each scheduled time."],
    when: Annotated[str, "Five-field cron in America/Chicago, or: " + ACCEPTED_WHEN],
    project: Annotated[str, "Project name or id; empty uses the calling thread's project when available, else Inbox."] = "",
) -> str:
    """Create a recurring task schedule. Unknown timing is refused, never guessed."""
    timing = parse_when(when)
    if timing is None:
        return "Error: unrecognised when. Accepted forms: " + ACCEPTED_WHEN + "."
    if not isinstance(brief, str) or not brief.strip():
        return "Error: brief must be nonempty text."
    return _request("POST", "/schedules", dict(project_id=_project(project), brief=brief.strip(), **timing))


@tool
def schedule_list() -> str:
    """List recurring task schedules, including their ids and next fire times."""
    return _request("GET", "/schedules")


@tool
def schedule_delete(id: Annotated[str, "The schedule id returned by schedule_list or schedule_create."]) -> str:
    """Delete a recurring schedule. Tasks already started by it keep running."""
    if not isinstance(id, str) or not re.fullmatch(r"[0-9a-f]{8}", id):
        return "Error: invalid schedule id."
    return _request("DELETE", "/schedules/" + id)
