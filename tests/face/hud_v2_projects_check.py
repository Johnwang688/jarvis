"""Headless checks for decisions part B: the project menu, inline rename with
numbering, the edit dialog, archive, restore and permanent delete.

Called from `hud_v2_check.main()` last, because it renames, archives and
deletes the very projects the earlier sections navigate. Driven against the
mock (`hud_v2_mock_projects.py` answers this feature's routes).

The checks worth keeping, each written to bite:
  - the archive confirmation's counts come from `/impact`, unfinished tasks
    block it with a Cancel button each, and **Enter does nothing** on it;
  - archive and delete send the token the confirmation was read from (the
    mock archives only when it matches);
  - a permanent delete is only reachable from the Archive view, for something
    archived, and goes through the trash route;
  - a double-click renames while a single click still expands the row;
  - a `project_updated` / `project_archived` from another window redraws.
"""
from __future__ import annotations

import time


def projects_checks(page, mock, check, until, expand):
    w = mock.world
    _menu_checks(page, mock, check, until, expand)
    _rename_checks(page, mock, check, until, expand)
    _edit_checks(page, mock, check, until, expand)
    _archive_checks(page, mock, check, until, expand)
    _thread_archive_checks(page, mock, check, until, expand)
    _other_window_checks(page, mock, check, until, expand)
    _ = w


def _close_dialogs(page, until):
    for _ in range(3):
        if page.locator('[data-testid="archive-view"], [data-testid="archive-confirm"], '
                        '[data-testid="picker"]').count() == 0:
            return
        page.keyboard.press("Escape")
        time.sleep(0.15)


def _menu_checks(page, mock, check, until, expand):
    print("\nprojects: the ⋯ menu")
    page.locator('[data-testid="tab-chat"]').click()
    before = page.locator('[data-testid="new-task-p3"]').count()
    page.locator('[data-testid="project-menu-p3"]').click()
    until(lambda: page.locator('[data-testid="project-menu"]').count() > 0)
    check("⋯ opens the project menu", page.locator('[data-testid="project-menu"]').count() == 1)
    check("with Edit, Rename and Archive",
          all(page.locator(f'[data-testid="pmenu-{k}"]').count() == 1 for k in ("edit", "rename", "archive")))
    check("focus goes to its first item",
          page.evaluate("document.activeElement && document.activeElement.dataset.testid") == "pmenu-edit",
          str(page.evaluate("document.activeElement && document.activeElement.dataset.testid")))
    check("and ⋯ did not expand or collapse the row",
          page.locator('[data-testid="new-task-p3"]').count() == before)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="project-menu"]').count() == 0)
    check("Escape closes it and focus returns to ⋯",
          page.locator('[data-testid="project-menu"]').count() == 0
          and page.evaluate("document.activeElement && document.activeElement.dataset.testid") == "project-menu-p3",
          str(page.evaluate("document.activeElement && document.activeElement.dataset.testid")))

    page.locator('[data-testid="project-p3"]').click(button="right")
    until(lambda: page.locator('[data-testid="project-menu"]').count() > 0)
    check("right-click opens the same menu", page.locator('[data-testid="pmenu-archive"]').count() == 1)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="project-menu"]').count() == 0)

    page.locator('[data-testid="project-menu-p1"]').click()
    until(lambda: page.locator('[data-testid="project-menu"]').count() > 0)
    check("on the Inbox, Rename is disabled", page.locator('[data-testid="pmenu-rename"]').is_disabled())
    check("and so is Archive", page.locator('[data-testid="pmenu-archive"]').is_disabled())
    why = page.locator('[data-testid="pmenu-inbox-why"]')
    check("and the menu says why", why.count() == 1 and "Inbox" in why.inner_text())
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="project-menu"]').count() == 0)


def _rename_checks(page, mock, check, until, expand):
    print("\nprojects: inline rename, numbered")
    # A single click still expands and collapses the row.
    had = page.locator('[data-testid="new-task-p3"]').count()
    page.locator('[data-testid="project-p3"]').click()
    until(lambda: page.locator('[data-testid="new-task-p3"]').count() != had, timeout=1.5)
    check("a single click still toggles the row", page.locator('[data-testid="new-task-p3"]').count() != had)

    page.locator('[data-testid="project-name-p3"]').dblclick()
    box = page.locator('[data-testid="rename-project-p3"]')
    until(lambda: box.count() > 0)
    check("a double-click opens the rename box", box.count() == 1)
    check("prefilled with the name", box.input_value() == "trading firm", box.input_value())
    box.fill("Schoolwork")
    preview = page.locator('[data-testid="rename-project-p3-saves-as"]')
    check("a taken name previews its number", preview.count() == 1 and "Schoolwork (1)" in preview.inner_text(),
          preview.inner_text() if preview.count() else "no preview")
    box.press("Escape")
    until(lambda: page.locator('[data-testid="rename-project-p3"]').count() == 0)
    time.sleep(0.2)
    check("Escape cancels and sends nothing", not mock.sent("PATCH", "/projects/p3"))

    page.locator('[data-testid="project-name-p3"]').dblclick()
    until(lambda: page.locator('[data-testid="rename-project-p3"]').count() > 0)
    box = page.locator('[data-testid="rename-project-p3"]')
    box.fill("")
    box.type("school work")
    check("a space typed in the box is text, not push-to-talk",
          box.input_value() == "school work" and page.evaluate("window.__hud.capture.ptt") is None,
          box.input_value())
    box.fill("Schoolwork")
    box.press("Enter")
    body = until(lambda: mock.sent("PATCH", "/projects/p3") or None)
    check("Enter saves the typed name", bool(body) and body[-1] == {"name": "Schoolwork"}, str(body))
    until(lambda: "Schoolwork (1)" in page.locator('[data-testid="project-p3"]').inner_text())
    check("the row shows the numbered name the backend saved",
          "Schoolwork (1)" in page.locator('[data-testid="project-p3"]').inner_text())
    note = page.locator('[data-testid="rename-note"]')
    until(lambda: note.count() > 0)
    check("and says the name was taken", note.count() == 1 and "Schoolwork (1)" in note.inner_text(),
          note.inner_text() if note.count() else "no note")

    print("\nthreads: inline rename, numbered within the project")
    mock.world["threads"].append({
        "id": "t8", "project_id": "p1", "role": "chat", "provider": "fast", "provider_session_id": None,
        "task_id": None, "title": "plan", "created": "2026-09-15T00:00:00+00:00",
        "updated": "2026-09-14T00:00:00+00:00", "turns": 1, "cost_usd": 0.0, "tokens": 0,
        "model": None, "effort": None, "cwd": "/home/johnw/projects/Jarvis"})
    mock.emit("thread_restored", {"thread_id": "t8", "project_id": "p1"}, thread_id="t8", project_id="p1")
    check("the thread is on screen", expand(page, "p1", "thread-t8") and expand(page, "p1", "thread-t1"))
    page.locator('[data-testid="thread-menu-t1"]').click()
    until(lambda: page.locator('[data-testid="tmenu-rename"]').count() > 0)
    check("the thread menu offers Rename and Archive",
          page.locator('[data-testid="tmenu-rename"]').count() == 1
          and page.locator('[data-testid="tmenu-archive"]').count() == 1)
    page.locator('[data-testid="tmenu-rename"]').click()
    tbox = page.locator('[data-testid="rename-thread-t1"]')
    until(lambda: tbox.count() > 0)
    tbox.fill("Plan")
    tbox.press("Enter")
    body = until(lambda: mock.sent("PATCH", "/threads/t1") and
                 [b for b in mock.sent("PATCH", "/threads/t1") if "title" in b] or None)
    check("⋯ → Rename saves the thread's title", bool(body) and body[-1] == {"title": "Plan"}, str(body))
    until(lambda: "Plan (1)" in page.locator('[data-testid="thread-t1"]').inner_text())
    check("numbered within its project", "Plan (1)" in page.locator('[data-testid="thread-t1"]').inner_text(),
          page.locator('[data-testid="thread-t1"]').inner_text())
    page.locator('[data-testid="thread-name-t8"]').dblclick()
    until(lambda: page.locator('[data-testid="rename-thread-t8"]').count() > 0)
    check("a double-click renames a thread row too", page.locator('[data-testid="rename-thread-t8"]').count() == 1)
    page.locator('[data-testid="rename-thread-t8"]').press("Escape")
    until(lambda: page.locator('[data-testid="rename-thread-t8"]').count() == 0)


def _edit_checks(page, mock, check, until, expand):
    print("\nprojects: the edit dialog")
    page.locator('[data-testid="project-menu-p3"]').click()
    until(lambda: page.locator('[data-testid="pmenu-edit"]').count() > 0)
    page.locator('[data-testid="pmenu-edit"]').click()
    until(lambda: page.locator('[data-testid="project-dialog-edit"]').count() > 0)
    name = page.locator('[data-testid="project-name"]')
    root = page.locator('[data-testid="project-root"]')
    check("Edit… opens the dialog prefilled",
          name.input_value() == "Schoolwork (1)" and root.input_value() == "/home/johnw/projects/jarvis-trading-firm",
          f"{name.input_value()} / {root.input_value()}")
    check("its impact was read from the backend", mock.saw("GET", "/projects/p3/impact"))
    check("Save is off until something changes", page.locator('[data-testid="save-project"]').is_disabled())
    check("the Discord channel is read-only", page.locator('[data-testid="project-discord"]').count() == 1
          and page.locator('[data-testid="project-discord"] input').count() == 0)
    root.fill("/home/johnw/notes")
    page.locator('[data-testid="project-profile"]').select_option("ask")
    effects = page.locator('[data-testid="project-effects"]')
    until(lambda: effects.count() > 0)
    text = effects.inner_text() if effects.count() else ""
    check("it says what stays on the old folder",
          "keep working in /home/johnw/projects/jarvis-trading-firm" in text and "/home/johnw/notes" in text, text)
    check("and that the profile applies from now on", "from now on" in text, text)
    page.locator('[data-testid="save-project"]').click()
    body = until(lambda: [b for b in mock.sent("PATCH", "/projects/p3") if "root" in b] or None)
    check("Save sends only what changed", bool(body) and body[-1] == {"root": "/home/johnw/notes", "profile": "ask"},
          str(body[-1] if body else None))
    until(lambda: page.locator('[data-testid="project-dialog-edit"]').count() == 0)
    check("and closes", page.locator('[data-testid="project-dialog-edit"]').count() == 0)

    # A refusal keeps the dialog open with the backend's words.
    mock.world["refuse_project_patch"] = "the project changed underneath you"
    page.locator('[data-testid="project-menu-p3"]').click()
    until(lambda: page.locator('[data-testid="pmenu-edit"]').count() > 0)
    page.locator('[data-testid="pmenu-edit"]').click()
    until(lambda: page.locator('[data-testid="project-dialog-edit"]').count() > 0)
    page.locator('[data-testid="project-name"]').fill("jarvis")
    saves = page.locator('[data-testid="project-saves-as"]')
    check("a taken name previews its number in the dialog too",
          saves.count() == 1 and "jarvis (1)" in saves.inner_text(), saves.inner_text() if saves.count() else "")
    page.locator('[data-testid="save-project"]').click()
    until(lambda: page.locator('[data-testid="project-error"]').count() > 0)
    check("a refused save is shown and the dialog stays open",
          "underneath you" in page.locator('[data-testid="project-error"]').inner_text()
          and page.locator('[data-testid="project-dialog-edit"]').count() == 1)
    page.locator('[data-testid="project-name"]').press("Escape")
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)

    # The top-bar profile select shows a refusal instead of snapping back.
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {compose: {projectId: 'p3'}, threadId: null}})")
    until(lambda: page.locator('[data-testid="profile"]').count() > 0)
    page.locator('[data-testid="profile"]').select_option("strict")
    until(lambda: page.locator('[data-testid="error"]').count() > 0)
    err = page.locator('[data-testid="error"]').inner_text() if page.locator('[data-testid="error"]').count() else ""
    check("a refused profile change is shown", "Could not change the profile" in err and "underneath" in err, err)
    mock.world["refuse_project_patch"] = None
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: ''}})")

    # The Inbox: name and root are fixed, and the dialog says so.
    page.locator('[data-testid="project-menu-p1"]').click()
    until(lambda: page.locator('[data-testid="pmenu-edit"]').count() > 0)
    page.locator('[data-testid="pmenu-edit"]').click()
    until(lambda: page.locator('[data-testid="project-dialog-edit"]').count() > 0)
    check("the Inbox's name and root are disabled in edit",
          page.locator('[data-testid="project-name"]').is_disabled()
          and page.locator('[data-testid="project-root"]').is_disabled()
          and page.locator('[data-testid="project-inbox-note"]').count() == 1)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)


def _archive_checks(page, mock, check, until, expand):
    print("\nprojects: archive, restore, delete permanently")
    w = mock.world
    # The project gets a blocked task with a worktree, and a schedule.
    w["tasks"].append({**w["tasks"][0], "id": "k7", "project_id": "p2", "state": "blocked",
                       "brief": "tidy the schoolwork db",
                       "worktree": "/mnt/c/myday/schoolwork/.jarvis/worktrees/k7", "branch": "jarvis/k7"})
    w["schedules"].append({"id": "s9", "project_id": "p2", "brief": "nightly", "cron": "0 2 * * *",
                           "every_s": None, "enabled": True, "describe": "every day at 02:00",
                           "last_run_at": None, "last_task_id": None, "next_run_at": None,
                           "created": "2026-09-15T00:00:00+00:00"})
    page.evaluate("localStorage.setItem('jarvis.hud.lastProject', 'p2')")
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {compose: {projectId: 'p2'}, threadId: null}})")
    active = [k["id"] for k in w["tasks"] if k["project_id"] == "p2" and k["state"] not in ("done", "failed", "cancelled")]
    check("the project to archive has unfinished work", bool(active), str(active))
    page.locator('[data-testid="project-menu-p2"]').click()
    until(lambda: page.locator('[data-testid="pmenu-archive"]').count() > 0)
    page.locator('[data-testid="pmenu-archive"]').click()
    until(lambda: page.locator('[data-testid="archive-summary"]').count() > 0)
    summary = page.locator('[data-testid="archive-summary"]').inner_text()
    check("the confirmation reads its counts from /impact",
          mock.saw("GET", "/projects/p2/impact") and "chat thread" in summary and "task" in summary, summary)
    check("and says the folder is not touched", "/mnt/c/myday/schoolwork is touched" in summary, summary)
    btn = page.locator('[data-testid="archive-confirm-btn"]')
    check("unfinished tasks block it", btn.is_disabled()
          and page.locator(f'[data-testid="archive-task-{active[0]}"]').count() == 1)
    page.locator(f'[data-testid="archive-cancel-{active[0]}"]').click()
    until(lambda: mock.posted(f"/tasks/{active[0]}/cancel"))
    check("each one has a Cancel button that cancels it", bool(mock.posted(f"/tasks/{active[0]}/cancel")))
    for k in w["tasks"]:
        if k["id"] in active:
            k["state"] = "cancelled"
    mock.emit("task_status_changed", {"task_id": active[0]}, task_id=active[0], project_id="p2")
    until(lambda: not btn.is_disabled())
    check("it re-reads when the task changes, and unblocks", not btn.is_disabled())

    btn.focus()
    n = len(mock.calls)
    page.keyboard.press("Enter")
    time.sleep(0.3)
    check("Enter does nothing, even on the focused Archive button",
          not [c for c in mock.calls[n:] if c[0] == "POST" and c[1].endswith("/archive")]
          and page.locator('[data-testid="archive-confirm"]').count() == 1)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="archive-confirm"]').count() == 0)
    check("Escape closes it and archives nothing", not w["projects"][1].get("archived"))

    page.locator('[data-testid="project-menu-p2"]').click()
    until(lambda: page.locator('[data-testid="pmenu-archive"]').count() > 0)
    page.locator('[data-testid="pmenu-archive"]').click()
    until(lambda: page.locator('[data-testid="archive-summary"]').count() > 0)
    until(lambda: not page.locator('[data-testid="archive-confirm-btn"]').is_disabled())
    page.locator('[data-testid="archive-confirm-btn"]').click()
    until(lambda: any(p["id"] == "p2" and p.get("archived") for p in w["projects"]))
    check("a click archives it, with the token it read",
          any(p["id"] == "p2" and p.get("archived") for p in w["projects"]))
    until(lambda: page.locator('[data-testid="project-p2"]').count() == 0)
    check("its row leaves the sidebar", page.locator('[data-testid="project-p2"]').count() == 0)
    check("the new thread aimed at it is re-aimed",
          page.evaluate("window.__hud.state().compose && window.__hud.state().compose.projectId") not in (None, "p2"),
          str(page.evaluate("window.__hud.state().compose")))
    check("and the remembered project forgets it",
          page.evaluate("localStorage.getItem('jarvis.hud.lastProject')") is None)
    check("its schedules are paused", [s["enabled"] for s in w["schedules"] if s["project_id"] == "p2"] == [False])

    # The Archive view: restore.
    page.locator('[data-testid="open-archive"]').click()
    until(lambda: page.locator('[data-testid="archived-project-p2"]').count() > 0)
    check("the Archive lists it", page.locator('[data-testid="archived-project-p2"]').count() == 1)
    page.locator('[data-testid="archive-restore-p2"]').click()
    until(lambda: mock.posted("/projects/p2/restore"))
    until(lambda: page.locator('[data-testid="project-p2"]').count() > 0)
    check("Restore brings it back to the sidebar", page.locator('[data-testid="project-p2"]').count() == 1)
    page.locator('[data-testid="archive-close"]').click()
    until(lambda: page.locator('[data-testid="archive-view"]').count() == 0)

    # Archive again, then delete permanently — only from the Archive.
    page.locator('[data-testid="project-menu-p2"]').click()
    until(lambda: page.locator('[data-testid="pmenu-archive"]').count() > 0)
    check("the project menu offers no delete", page.locator('[data-testid="project-menu"]').inner_text().count("Delete") == 0)
    page.locator('[data-testid="pmenu-archive"]').click()
    until(lambda: page.locator('[data-testid="archive-summary"]').count() > 0)
    until(lambda: not page.locator('[data-testid="archive-confirm-btn"]').is_disabled())
    page.locator('[data-testid="archive-confirm-btn"]').click()
    until(lambda: page.locator('[data-testid="project-p2"]').count() == 0)
    page.locator('[data-testid="open-archive"]').click()
    until(lambda: page.locator('[data-testid="archive-delete-p2"]').count() > 0)
    page.locator('[data-testid="archive-delete-p2"]').click()
    until(lambda: page.locator('[data-testid="delete-summary"]').count() > 0
          and "trash" in page.locator('[data-testid="delete-summary"]').inner_text())
    text = page.locator('[data-testid="delete-summary"]').inner_text()
    check("the delete confirmation says where it goes and what stays",
          "trash" in text and "Left on disk" in text and "Nothing inside /mnt/c/myday/schoolwork" in text, text)
    page.locator('[data-testid="delete-confirm-btn"]').focus()
    n = len(mock.calls)
    page.keyboard.press("Enter")
    time.sleep(0.3)
    check("Enter does not delete", not [c for c in mock.calls[n:] if c[0] == "DELETE"])
    page.locator('[data-testid="delete-confirm-btn"]').click()
    until(lambda: mock.saw("DELETE", "/projects/p2"))
    check("a click deletes through the trash route", mock.saw("DELETE", "/projects/p2"))
    until(lambda: page.locator('[data-testid="archive-notice"]').count() > 0)
    check("and says it is in the trash", "trash" in page.locator('[data-testid="archive-notice"]').inner_text())
    until(lambda: "1 item" in page.locator('[data-testid="trash-info"]').inner_text())
    check("the trash counts it", "1 item" in page.locator('[data-testid="trash-info"]').inner_text())
    page.locator('[data-testid="archive-close"]').click()
    until(lambda: page.locator('[data-testid="archive-view"]').count() == 0)


def _thread_archive_checks(page, mock, check, until, expand):
    print("\nthreads: archive, read-only transcript, restore")
    check("the thread is on screen", expand(page, "p1", "thread-t1"))
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    page.locator('[data-testid="thread-menu-t1"]').click()
    until(lambda: page.locator('[data-testid="tmenu-archive"]').count() > 0)
    page.locator('[data-testid="tmenu-archive"]').click()
    until(lambda: mock.posted("/threads/t1/archive"))
    until(lambda: page.locator('[data-testid="thread-t1"]').count() == 0)
    check("⋯ → Archive hides the thread", page.locator('[data-testid="thread-t1"]').count() == 0)
    check("and the open conversation becomes a new thread in its project",
          page.evaluate("window.__hud.state().threadId") is None
          and page.evaluate("window.__hud.state().compose.projectId") == "p1")
    page.locator('[data-testid="open-archive"]').click()
    until(lambda: page.locator('[data-testid="archived-thread-t1"]').count() > 0)
    check("the Archive lists the thread", page.locator('[data-testid="archived-thread-t1"]').count() == 1)
    page.locator('[data-testid="archive-open-t1"]').click()
    until(lambda: page.locator('[data-testid="archive-transcript"]').count() > 0)
    transcript = page.locator('[data-testid="archive-transcript"]')
    check("Open shows its transcript, read-only",
          "what is the plan" in transcript.inner_text() and transcript.locator("input, textarea").count() == 0,
          transcript.inner_text())
    page.locator('[data-testid="archive-back"]').click()
    until(lambda: page.locator('[data-testid="archive-restore-thread-t1"]').count() > 0)
    page.locator('[data-testid="archive-restore-thread-t1"]').click()
    until(lambda: mock.posted("/threads/t1/restore"))
    page.locator('[data-testid="archive-close"]').click()
    until(lambda: page.locator('[data-testid="archive-view"]').count() == 0)
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {}})")
    check("Restore brings the thread back", expand(page, "p1", "thread-t1"))


def _other_window_checks(page, mock, check, until, expand):
    print("\nprojects: changes made in another window")
    w = mock.world
    p3 = next(p for p in w["projects"] if p["id"] == "p3")
    p3["name"] = "renamed elsewhere"
    mock.emit("project_updated", {"project_id": "p3", "changed": ["name"]}, project_id="p3")
    until(lambda: "renamed elsewhere" in page.locator('[data-testid="project-p3"]').inner_text())
    check("a project_updated from another window redraws the row",
          "renamed elsewhere" in page.locator('[data-testid="project-p3"]').inner_text())
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {compose: {projectId: 'p3'}, threadId: null}})")
    p3["archived"] = "2026-10-06T12:00:00+00:00"
    mock.emit("project_archived", {"project_id": "p3", "name": "renamed elsewhere"}, project_id="p3")
    until(lambda: page.locator('[data-testid="project-p3"]').count() == 0)
    check("a project_archived from another window removes the row",
          page.locator('[data-testid="project-p3"]').count() == 0)
    check("and re-aims a new thread that was aimed at it",
          page.evaluate("window.__hud.state().compose.projectId") not in (None, "p3"),
          str(page.evaluate("window.__hud.state().compose")))
    _close_dialogs(page, until)
