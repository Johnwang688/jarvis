"""Headless checks for the sidebar's status dots (2026-10-08, lib/activity.ts).

Called from `hud_v2_check.main()` right after the sidebar section. The mock's
`activity(of, id, status)` sets a row's status the way the daemon's tracker
does — in `GET /activity` and as an SSE `activity` record.

The checks worth keeping, each written to bite:
  - a dot follows the record, and an idle row is still the plain `·`;
  - a thread finishing **unread while the owner looks elsewhere stays blue**,
    and one finishing while it is open in a visible window is marked read at
    once — but not while the window is hidden;
  - opening a thread or task is what clears blue and red, and nothing else;
  - a task row draws its phase as a dot, never as the old phase word;
  - a folded project shows its most urgent row, and nothing when it is open;
  - a reload reads `GET /activity`, so the dots survive one.
"""
from __future__ import annotations


def activity_checks(page, mock, check, until, expand, boot):
    print("\nsidebar status dots")
    status = lambda tid: page.locator(f'[data-testid="activity-{tid}"]').get_attribute("data-status")  # noqa: E731
    seen = lambda path: len(mock.posted(path))  # noqa: E731

    # Somewhere neutral: a new thread in the chat tab, nothing open.
    page.locator('[data-testid="new-thread"]').click()
    page.locator('[data-testid="tab-chat"]').click()
    expand(page, "p1", "thread-t1")
    expand(page, "p1", "task-k1")

    check("an idle thread is the plain dot it always was",
          status("t1") == "idle" and page.locator('[data-testid="activity-t1"]').inner_text() == "·",
          str(status("t1")))

    mock.activity("thread", "t1", "working")
    until(lambda: status("t1") == "working", timeout=3)
    row = page.locator('[data-testid="thread-t1"]')
    check("a working thread pulses and its row sweeps",
          status("t1") == "working" and "act-working" in (row.get_attribute("class") or ""),
          f"{status('t1')} / {row.get_attribute('class')}")

    mock.activity("thread", "t1", "needs_input")
    until(lambda: status("t1") == "needs_input", timeout=3)
    check("waiting for the owner is yellow, and the sweep stops",
          status("t1") == "needs_input" and "act-working" not in (row.get_attribute("class") or ""),
          str(status("t1")))

    before = seen("/threads/t1/seen")
    mock.activity("thread", "t1", "unread")
    until(lambda: status("t1") == "unread", timeout=3)
    page.wait_for_timeout(300)
    check("finished while the owner looks elsewhere: blue, and nothing marked it read",
          status("t1") == "unread" and seen("/threads/t1/seen") == before, str(status("t1")))

    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: status("t1") == "idle", timeout=3)
    check("opening the thread reads it", status("t1") == "idle" and seen("/threads/t1/seen") > before,
          str(status("t1")))

    # With t1 open in the chat tab: a turn that finishes is watched.
    before = seen("/threads/t1/seen")
    mock.activity("thread", "t1", "unread")
    until(lambda: seen("/threads/t1/seen") > before and status("t1") == "idle", timeout=3)
    check("finished while open in a visible window: read at once",
          status("t1") == "idle" and seen("/threads/t1/seen") > before, str(status("t1")))

    # ...but not while the window is hidden.
    page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    before = seen("/threads/t1/seen")
    mock.activity("thread", "t1", "failed")
    until(lambda: status("t1") == "failed", timeout=3)
    page.wait_for_timeout(300)
    check("a hidden window reads nothing: the failure stays",
          status("t1") == "failed" and seen("/threads/t1/seen") == before, str(status("t1")))
    tri = page.locator('[data-testid="activity-t1"] svg path')
    check("failed is a red triangle with a !",
          tri.count() == 1 and page.locator('[data-testid="activity-t1"] svg rect').count() == 2)
    page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    until(lambda: status("t1") == "idle", timeout=3)
    check("and the owner coming back to it reads it", status("t1") == "idle", str(status("t1")))

    # Tasks: the phase is a dot now, its word the tooltip.
    page.locator('[data-testid="new-thread"]').click()
    page.locator('[data-testid="tab-chat"]').click()
    task = page.locator('[data-testid="task-k1"]')
    check("a task row has no phase word, only its dot (the word is the tooltip)",
          task.locator(".phase").count() == 0 and task.get_attribute("title") == "running"
          and page.locator('[data-testid="activity-k1"]').count() == 1)
    mock.activity("task", "k1", "needs_input")
    until(lambda: status("k1") == "needs_input", timeout=3)
    check("a task waiting on the owner is yellow", status("k1") == "needs_input", str(status("k1")))
    before = seen("/tasks/k1/seen")
    mock.activity("task", "k1", "unread")
    until(lambda: status("k1") == "unread", timeout=3)
    page.wait_for_timeout(300)
    check("a task done while elsewhere stays blue", status("k1") == "unread"
          and seen("/tasks/k1/seen") == before, str(status("k1")))
    task.click()
    until(lambda: status("k1") == "idle", timeout=3)
    check("opening the task reads it", status("k1") == "idle" and seen("/tasks/k1/seen") > before,
          str(status("k1")))

    # A folded project carries its most urgent row.
    page.locator('[data-testid="new-thread"]').click()
    page.locator('[data-testid="tab-chat"]').click()
    mock.activity("thread", "t1", "unread")
    mock.activity("task", "k1", "working")
    until(lambda: status("t1") == "unread" and status("k1") == "working", timeout=3)
    for _ in range(4):
        if page.locator('[data-testid="thread-t1"]').count() == 0:
            break
        page.locator('[data-testid="project-p1"]').click()
        until(lambda: page.locator('[data-testid="thread-t1"]').count() == 0, timeout=1)
    folded = lambda: page.locator('[data-testid="project-activity-p1"]').get_attribute("data-status") \
        if page.locator('[data-testid="project-activity-p1"]').count() else None  # noqa: E731
    check("a folded project shows its most urgent row (working over unread)", folded() == "working",
          str(folded()))
    mock.activity("thread", "t1", "failed")
    until(lambda: folded() == "failed", timeout=3)
    check("and failed over everything", folded() == "failed", str(folded()))
    expand(page, "p1", "thread-t1")
    check("an open project shows no dot of its own",
          page.locator('[data-testid="project-activity-p1"]').count() == 0)

    # A reload reads GET /activity: the dots survive it.
    boot(page, mock)
    expand(page, "p1", "thread-t1")
    until(lambda: page.locator('[data-testid="activity-t1"]').count() > 0
          and status("t1") == "failed", timeout=4)
    check("after a reload the dots are read back", status("t1") == "failed" and status("k1") == "working",
          f"{status('t1')} / {status('k1')}")

    # Leave the world as the rest of the suite expects it.
    mock.activity("thread", "t1", "idle")
    mock.activity("task", "k1", "idle")
    until(lambda: status("t1") == "idle" and status("k1") == "idle", timeout=3)
