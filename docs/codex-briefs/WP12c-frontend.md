# WP12c frontend — project creation, drag threads between projects, quota bars, schedule popup

Work package 12c (frontend), in a git worktree on branch
`jarvis/wp12c-hud-frontend`. You are Claude. The owner used the HUD for the
first time and asked for four things; this brief is exactly those. Read
`docs/codex-briefs/WP12b-notes.md` (the component map), `hud/src/` as it
stands, `docs/hud-api.md` including the **Additions 2026-09-16** section
(the backend for those is being built concurrently by Codex; extend the
mock server in `tests/face/hud_v2_mock.py` to serve them), and
`tests/face/hud_v2_check.py` for the test pattern.

1. **Creating a project must be obvious and easy.** A prominent
   `+ New project` button at the top of the sidebar (not a tree row that
   reads as an entry). The dialog: name, root chosen with a **directory
   picker** built on `GET /fs/dirs` (breadcrumb, double-click to descend,
   `..`, a typed path box that validates), access folders added the same
   way, profile (auto default), and a preview of the Windows badge if the
   root is under `/mnt/`. Enter submits, Escape cancels, errors inline.
2. **Drag threads between projects.** Chat threads in the sidebar are
   draggable (HTML5 DnD, with a keyboard alternative: a "Move to…" item in
   the thread's context menu listing projects); dropping on a project
   header calls `PATCH /threads/{id}` and re-parents on success, reverts
   with the error inline on 409/4xx. Task threads are not draggable and
   the context menu says why. Tasks themselves do not move in this
   package.
3. **Quota as a bar.** In the Usage panel each provider shows, per
   reported window, a horizontal bar with used percent, the window name
   and the reset time ("resets in 3h 12m"); the local allowance
   (`today.work_tokens / allowance.work_tokens`) is a second, thinner bar
   labelled "local allowance" so the two are never confused. Colour steps
   at 70 % and 90 %. "Not reported" stays for a provider with no quota.
   Refreshes on `usage_updated`.
4. **Schedules: an intuitive popup.** Replace the inline form with a
   dialog: pick a project, write the brief, then choose *when* from
   presets — Daily at HH:MM, Weekdays at HH:MM, Weekly on <day> at HH:MM,
   Every N minutes/hours — or an Advanced tab with a raw cron field. Every
   change calls `POST /schedules/preview` and shows the plain-English
   reading and the next three fire times, so the owner sees what they are
   creating before saving. Same dialog edits an existing schedule. Also
   note in the empty state that Jarvis can create schedules when asked in
   chat ("schedule a morning briefing at 8 on weekdays").

Tests: extend `hud/src/**/*.test.ts` (cron preset → cron string mapping,
quota bar math, move reducer) and `tests/face/hud_v2_check.py`: the
button is visible without scrolling at 1280×800; the picker lists dirs
from the mock and refuses a path outside the roots; a project is created
and appears; a chat thread drags to another project and the PATCH is
sent, a 409 reverts it, a task thread cannot be dragged and the menu says
why; keyboard move works; quota bars render the mock's windows with the
right widths and labels and "not reported" otherwise; the schedule
dialog's presets produce the expected cron and the preview text and times
render from the mock; Escape/Enter behave. All previous checks still pass.

Rules: stay inside `hud/`, `tests/face/hud_v2*.py`,
`docs/codex-briefs/WP12c-frontend-notes.md`. Run `cd hud && npm ci && npm
run build && npm test` then
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/face/hud_v2_check.py`.
Commit with a message starting `v2 WP12c frontend:` ending with
`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Do not push or
merge. Notes under 50 lines; report branch, worktree path and notes path.
