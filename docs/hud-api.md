# HUD ↔ daemon API contract (WP12)

Both WP12a (backend, Codex) and WP12b (frontend, Opus) implement this file
exactly. Everything already served by the v2 daemon (`docs/codex-briefs/
WP7-notes.md` route table, plus `/approvals` from WP5, `/route` from WP9,
`/tasks/{id}/start|steer|cancel|resume|answer` from WP11) stays as is. This
file adds what the HUD needs and nothing else. All JSON; loopback; the HUD
is served from the daemon on `FACE_PORT` (8402) and calls the API on the
**same origin** (the daemon serves the API on both listeners); previews
come from `WORKSHOP_PORT` (8403), a different origin by design.

## Static

`GET /` → the built HUD (`hud/dist/index.html`); `GET /assets/*` → its
assets. `GET /avatar.svg`, `GET /avatars`, `GET /voices`, `GET /models`,
`GET /models/catalog`, `POST /avatar`, `POST /voice`, `POST /model`,
`POST /mute`, `POST /say` — **v1 semantics and shapes** (`jarvis/face/
server.py`), re-homed; `POST /model` sets the **fast path's** model only.

## Projects (additions to WP7)

- `PATCH /projects/{id}` accepts `{profile, extra_dirs, always_ask,
  discord_channel_id, routing}` — already there; the HUD uses it for the
  profile switch and access folders. Since 2026-10-06 also `{name, root}`,
  with the rules under *Additions 2026-10-06* below.
- `GET /projects/{id}/platform` → `{"platform": "wsl"|"windows", "note":
  str|null}` — `windows` when the root is under `/mnt/<drive>/`, with the
  9p caution as the note.

## Files (scoped: project root + `extra_dirs`; anything else 403)

- `GET /projects/{id}/tree?path=<rel>&depth=2` →
  `{"path", "entries": [{"name", "kind": "file"|"dir", "size", "mtime"}]}`
  (`.git`, `.jarvis`, `node_modules`, `.venv` omitted; a `SKIP_DIRS` in the
  backend, the v1 grep list).
- `GET /projects/{id}/file?path=<rel>` → `{"path", "content", "mtime",
  "size", "protected": bool}`; protected credential names (v1 secrets
  layer) return `{"protected": true}` with **no content**, 200.
- `PUT /projects/{id}/file` `{"path", "content", "expected_mtime"}` →
  `{"mtime"}`; 409 on mtime mismatch (someone else wrote it); 403 on a
  protected name or a SELF_PROTECTED / allowlist path; 413 over 2 MB.
  Atomic write.
- `GET /tasks/{id}/diff` → `{"base", "head", "files": [{"path", "status":
  "A"|"M"|"D"|"R", "additions", "deletions"}], "patch": str}` from the
  worktree (`git diff <base>...HEAD` plus the working tree), capped at 1 MB
  with `"truncated": true`.
- `GET /tasks/{id}/diff/file?path=<rel>` → `{"before", "after"}` for the
  Monaco diff editor.

## Preview origin (`WORKSHOP_PORT`)

- The daemon also serves `http://127.0.0.1:8403/p/{project_id}/<rel>`
  read-only from the project root (same scope rules, same skip list,
  `Cache-Control: no-store`, correct content types). The HUD's Preview tab
  iframes that, or any `http://localhost:<port>/…` URL the owner types.
  Never the HUD origin: the backend refuses to proxy it, the frontend
  refuses to load it.

## Threads (additions)

- `GET /threads/{id}/log?after=N` exists; the HUD also needs
  `GET /threads/{id}/transcript` → `{"messages": [{"role", "text", "at"}]}`
  built from the log's `text`/`user` records (deltas excluded).
- `POST /threads/{id}/send` gains `{"text", "images"?, "attachments"?:
  [{"name","mime","data_b64"}]}` with the v1 `_assemble_turn` rules (8
  files, 4 MB each, text inlined and scrubbed, protected names refused as
  a bracketed note).

## Tasks (additions)

- `GET /tasks/{id}/threads` → `[{"thread_id", "role", "provider", "model",
  "state": "open"|"closed", "turns", "cost_usd"}]` — the orchestration
  view.
- `GET /tasks/{id}/journal?after=N` → the journal records.

## Usage

- `GET /usage` → `{"providers": {"claude": {...}, "codex": {...}, "fast":
  {...}}}` where each is `{"state": "available"|"over_threshold"|"cooling"|
  "unavailable", "reason", "today": {"work_tokens", "spend_usd",
  "equivalent_usd"}, "allowance": {...}, "quota": null | {"windows": [
  {"name", "used_percent", "resets_at"}]}}`. `quota` is filled only from
  a provider's own report, never computed: Codex rate-limit updates
  (5h and weekly), and Claude's 5h and week windows from Claude Code's
  own usage endpoint when a subscription login is present. The login
  token is not part of this response. A missing login, a 401, or a body
  without those windows is `quota: null`.

## Schedules

- `GET /schedules` · `POST /schedules {project_id, brief, cron?: str,
  every_s?: int, enabled: true}` · `PATCH /schedules/{id}` · `DELETE
  /schedules/{id}` · `POST /schedules/{id}/run-now`. A schedule record:
  `{id, project_id, brief, cron, every_s, enabled, last_run_at,
  last_task_id, next_run_at, created, describe}`. `describe` is the
  scheduler's plain-English reading, computed on the way out and not
  stored. Cron is 5-field, evaluated in `America/Chicago`.

## Speech

- `POST /stt` body `audio/*` (WAV or webm) → `{"text"}` via `voice.stt`.
- `POST /say` as v1 → audio bytes (`RIFF` or MP3; the client sniffs).
- The HUD keeps v1's `/converse`-style streaming for **chat turns** as a
  client convenience? **No.** v2 chat is `POST /threads/{id}/send` (202)
  plus `/events?thread=` for the stream; TTS is the client calling
  `/say` per settled sentence (the speculator ported to the client is
  optional; not in this package).

## Events (`GET /events`, SSE)

Already: every provider `Event` plus lifecycle records (`thread_opened`,
`thread_closed`, `task_created`, `task_status_changed`, `task_question`,
`approval_requested`, `approval_resolved`, `proposal_reply`). WP12a adds:
`schedule_fired {schedule_id, task_id}`, `usage_updated {provider}`,
`mute`, `avatar`, `model`, `voice` (v1's broadcast kinds, so every open
window relabels).

## Errors

Every error is `{"error": str}` with 400/403/404/409/413; never a stack
trace; never a credential value.

## Additions 2026-09-16 (WP12c — owner's first-use feedback)

- `PATCH /threads/{id}` `{"project_id"}` → the thread record. Moves a
  **chat** thread to another project (its log, provider session and title
  travel with it). A thread that belongs to a task (`task_id` set) → 409
  `{"error": "task threads move with their task"}`. Publishes
  `thread_moved {thread_id, from_project_id, to_project_id}` on the bus.
  **A move re-labels a thread; it never re-roots it.** The brief saved at
  open (cwd, permission profile, always_ask) is not rewritten, and every
  resume reads it back, so a moved thread keeps working in the folder,
  under the rules, it was opened with.
- Thread records carry `cwd`, the folder the thread was opened in, set
  from the brief at open. `GET /threads` fills it from the saved brief for
  threads that predate the field, without writing anything.
- `GET /fs/dirs?path=<abs>` → `{"path", "parent", "dirs": [names]}`, for a
  directory picker when creating a project or adding an access folder.
  Lists directories only, under `$HOME` or `/mnt/<drive>/` only, hidden
  dirs omitted, no file contents. 403 elsewhere.
- `POST /schedules/preview` `{"cron"?, "every_s"?}` → `{"next": [iso × 3],
  "describe": str}` — the next three fire times in America/Chicago and a
  plain-English reading ("weekdays at 09:00"), for the manual popup.
- **Agentic schedules:** tools `schedule_create(brief, when, project="")`,
  `schedule_list()`, `schedule_delete(id)` in the v1 registry
  (`jarvis/v2/tools/schedules.py`), where `when` is either a 5-field cron
  or plain English the backend parses for the common shapes ("every day at
  9", "weekdays at 8:30", "every 2 hours", "mondays at 10", "every 15
  minutes"); unparseable → an error string listing the accepted forms,
  never a guess. Present in `FAST_TOOLS` and `MCP_TOOLS`. The tools write
  through the same store the scheduler reads and publish the same events,
  so the HUD list updates live.

## Additions 2026-10-06 (projects: rename, edit, archive — decisions part B)

The owner's decisions are `docs/plans/2026-10-06-decisions.md` part B; the
backend is `jarvis/v2/projects.py` and `jarvis/v2/trash.py`.

**Names.** A name that collides is **numbered, never refused**: `name (1)`,
`name (2)`, …, compared ignoring case and surrounding spaces. Project names
are unique across all projects, archived ones included, and `Inbox` is
reserved. Thread titles are unique within their project. Applied on
`POST /projects`, `PATCH /projects/{id}` (only when the name really changes,
so existing duplicates stay editable), `PATCH /threads/{id}` `{title}`, and
restore. Read the name from the response, not the request.

- `POST /projects` and `PATCH /projects/{id}`: `root` must be an existing
  directory (400). A root change pins every task that already has a
  worktree to the old root first (`Task.root`), so it finishes, commits and
  is removed there; existing threads keep their frozen `cwd`; new threads
  and tasks use the new root. A PATCH of an archived project is 409. They
  publish `project_created {…project}` and `project_updated {project_id,
  changed: [fields], project}`; a PATCH that changes nothing publishes
  nothing.
- `PATCH /threads/{id}` `{"title"}` → the thread record (title numbered).
  A rename is its own request: `{title, project_id}` together is 400, and
  the `{project_id}` move is unchanged. Publishes `thread_updated
  {thread_id, title, changed: ["title"]}`. An archived thread is 409.
- Task records carry `root`: the project root the task was started under,
  or null before its worktree exists.

**Impact.** `GET /projects/{id}/impact` is read-only (it never creates the
Inbox) and is what every confirmation is built from:
`{project_id, name, root, inbox, archived, token, blockers: [str],
chat_threads: {count, archived}, tasks: {total, finished, active: [{id,
brief, state}]}, task_threads, running_turns: [{thread_id, title}],
schedules: [{id, brief, describe, enabled}], worktrees: [{task_id, path,
branch, exists}], on_root: {threads, tasks: [...]}, discord_channel_id}`.
`token` hashes what was counted; archive and delete send it back as
`?expect=` and get 409 if anything changed.

**Archive instead of delete.** Project and thread records carry `archived`
(an ISO time, or null). An archived project is hidden from `GET /projects`,
`/threads`, `/tasks`, `/schedules`, from `router.place` (Discord `in
<name>:`, now matched ignoring case), Discord channel placement and the
schedule tools; its schedules are paused (`paused_by_archive`) and resumed
on restore. Opening a thread or task in it, sending to one of its threads,
moving a thread into or out of it, and editing it are refused (409). Every
record, log, journal and cost is kept.

No task is ever created in an archived project, by any door: the refusal
lives in the task store (`ProjectArchived`), under the store lock the
archive takes for its final check, so `POST /tasks` (409), Discord intake,
a fast-path proposal and a schedule all meet it, and an archive cannot land
between a create's check and its write. Discord answers in an archived
project's channel or its old task threads with a note pointing at the
Archive view, and opens, starts, steers and resumes nothing there.
`DELETE /schedules/{id}` on an archived project's schedule is 409: it is
paused, not deleted, and goes only with its project's permanent delete.

- `POST /projects/{id}/archive?expect=<token>` → `{project,
  paused_schedules}`. 400 without `expect`; 409 for the Inbox, an
  unfinished task, a running chat turn (both named), or a stale token.
  Idle sessions in the project are closed first. Publishes
  `project_archived {project_id, name, paused_schedules}`.
- `POST /projects/{id}/restore` → the project (name numbered if taken
  meanwhile). Publishes `project_restored`.
- `POST /threads/{id}/archive` → the thread. Chat threads only (a task's
  threads go with their project); refused while a turn runs. Publishes
  `thread_archived`.
- `POST /threads/{id}/restore` → the thread (title numbered if taken). 409
  while its project is archived. Publishes `thread_restored`.
- `GET /archive` → `{projects: [project + {threads, task_threads, tasks:
  [{id, brief, state, worktree, branch}], schedules: n}], threads: [thread +
  {project_name}], trash: {location, retention_days, entries}}`. The
  transcript of an archived thread is the usual
  `GET /threads/{id}/transcript`.

**Permanent delete goes to a trash.** Only something archived can be
deleted. The records are moved out of the stores into one staging folder
under the data root, which then goes to the trash as a single entry with a
`manifest.json`: the Windows Recycle Bin (PowerShell `SendToRecycleBin`)
for a `/mnt/<drive>/` path, else the freedesktop home trash
(`$XDG_DATA_HOME/Trash/{files,info}`). Nothing in the project's folder, no
worktree and no branch is touched; the ledger and decision log are kept.

- `DELETE /projects/{id}?expect=<token>` → `{deleted, name, removed:
  {threads, tasks, schedules}, left_on_disk: [worktree paths], trash:
  {where: "linux"|"windows"|"staged", …}}`. `staged` (with `path` and
  `error`) means the records left every list but the move to the trash
  failed; the daemon retries it at start and every six hours, and the HUD
  says so rather than "in the trash". 409 unless archived. Publishes
  `schedule_deleted` per schedule and `project_deleted {project_id, name,
  removed_thread_ids, removed_task_ids}`.
- `DELETE /threads/{id}` → `{deleted, trash}`. Archived chat threads (or
  one in an archived project) only. Publishes `thread_deleted`.
- `GET /trash` → `{location, retention_days, entries, items: [{name, path,
  deleted}]}` — Jarvis's entries only. `POST /trash/empty` → `{removed}`.
  Jarvis's entries are purged after `JARVIS_TRASH_DAYS` (default 30) by the
  daemon; entries the owner trashed from elsewhere are never touched. An
  entry is Jarvis's only if its recorded path, with no `..` in it, lies
  under the data root. One entry that cannot be removed is logged and
  skipped; it never stops the rest. The Recycle Bin path reaches PowerShell
  as base64 data decoded by the script, never as quoted text.

**Owner only.** Archive, restore, both deletes and the trash empty answer
only on the HUD's listener (`FACE_PORT`) and only to a request carrying the
HUD's `Origin`; anything else is 403. No tool reaches them (fast path, MCP,
Claude or Codex threads, Discord) — `tests/v2/archive_check.py` asserts
it. The gate keeps every tool's HTTP client off these routes; it is not a
boundary against a shell command the permission gate approved, the same
footing as `/approvals`.
