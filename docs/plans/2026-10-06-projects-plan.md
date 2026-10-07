# Plan: edit, rename and delete projects (Jarvis v2 HUD)

_Planner's proposal, kept as written. **The owner's decisions in `2026-10-06-decisions.md` override it wherever they differ.**_

## What the code does today

These findings come from reading the code. Several of them change the design.

- **Delete has no route at all.** Projects have no `DELETE` route and no `delete` method. `_Store.delete` in `jarvis/v2/stores.py` raises "Projects cannot be deleted", and `tests/v2/stores_check.py:177` checks for that refusal.
- **A trap in the store.** Projects are stored flat (`projects/<id>.json`), but `_Store._delete` runs `shutil.rmtree(self.path(id).parent)`. Called on a project, that would wipe the whole `projects/` directory. Do not reuse `_delete` for projects.
- **`PATCH /projects/{id}` is looser than the contract says.**
  - It already accepts `name` and `root` (`docs/hud-api.md` lists neither).
  - It does not check that names are unique or that `root` exists.
  - It publishes no bus event, and neither does `POST /projects`, so other windows never refresh.
- **The top-bar profile `<select>` in `App.tsx` hides refusals.** It ends in `.catch(() => {})`, which breaks the "every refusal is shown" rule.
- **What a thread keeps when its project changes:**
  - `Thread.cwd` and the profile are frozen in `brief.json`. The permission layer reads `brief.profile`.
  - **`always_ask` is not fully frozen, which corrects the brief.** `Daemon._permit` re-reads the thread's *current* project each time a session opens or resumes, and `always_ask_for(brief, project)` combines that list with the brief's frozen copy. So:
    - a rule added to the project reaches existing threads the next time their session opens;
    - a rule removed from the project still applies to threads opened while it existed;
    - live sessions keep the list they started with.
  - **The task runner re-reads the project too.** It builds every worker's brief from `task.profile or project.profile` (`runner.py:615`). A task already in flight therefore gives the new profile to any worker it opens after the edit.
- **`extra_dirs` only affects the HUD.** The field is used only by `hud_api.scope()`: the file tree, file reads, preview and `@path` attachments. It is not in `Brief`, so it changes nothing an agent can reach. The New Project label "Access folders" suggests it does.
- **Worktrees are tied to the current root.**
  - They live inside the owner's folder (`<root>/.jarvis/worktrees/<task>`, plus a `jarvis/<id>-…` branch).
  - `worktrees.remove()` works out paths from the project's *current* root and runs `git branch -D` there. Changing the root while a task has a worktree would make it refuse, or act on the wrong repo.
- **Schedules:** `Schedules.fire` runs `require(project)` every 60 s. A schedule left pointing at a deleted project would log a 404 forever.
- **Name matching is inconsistent.**
  - `router.place(explicit)` matches a name exactly and needs exactly one match. With the owner's two `e2e-calc` projects, `in e2e-calc: …` from Discord answers "I don't know a project called…".
  - `tools/schedules.py` matches without case and reports "ambiguous".
- **Neither references a project:** the ledger (rows are per provider and thread) and the Discord gateway's `_chat_threads` (a stale key for a deleted project is never looked up again).
- **The owner's data today:**
  - Four projects, and no Inbox on disk yet (it is created on first use).
  - Both `e2e-calc` projects point at a `/tmp/.../scratchpad/e2e/repo` that no longer exists.
  - `d71b5c52` has 1 done task and 4 task threads. `571fbc98` has 1 blocked task and 1 task thread.
  - Their recorded worktrees no longer exist on disk.
  - Deleting must therefore work when the root is missing, which this design does: it never touches the root or git.

## Recommendations

### 1. Edit
| Field | Editable from the HUD | What happens to existing threads, tasks and schedules |
|---|---|---|
| `name` | Yes (not for the Inbox) | Label only, effective immediately. The Discord `in <name>:` placement follows the new name. The Discord channel's own name is not changed. |
| `root` | Yes, with guards (not for the Inbox): it must be an existing directory. Refused while the project has unfinished tasks or any task with a worktree recorded. | **Existing threads keep their folder** (their briefs are frozen) and show `↪ <old folder>`. New threads and tasks use the new root. The File tab, preview, `@path` attachments and the WIN badge follow the new root at once. |
| `profile` | Yes (already in the top bar) | Applies to threads opened from now on. Open and existing threads keep theirs. A task in flight uses it for the workers it opens from now on (see open decision 9). |
| `always_ask` | Yes: a list of plain strings (a tool name, or command tokens with globs) | Additions reach existing threads at their next session open. Removals do not reach threads that already carry the rule in their brief. |
| `extra_dirs` | Yes, through the DirPicker | HUD file scope only, effective immediately. Rename the label to "Folders the HUD may browse", or keep it and add a note. |
| `discord_channel_id` | Read-only display | A mistyped id would misroute the channel without any error. |
| `routing` | No; it stays in Settings / `POST /route` | — |

**Edit surface:** turn `NewProject` in `components/Pickers.tsx` into a `ProjectDialog` with `mode: "new" | "edit"` and an `initial?: Project`.
- It reuses the Shell, the DirPicker, the WIN preview and the error line that keeps the dialog open.
- In edit mode it adds an `always_ask` list editor and an **Effects** note built from the field changes and `GET /impact`. For example: "3 threads keep working in `/old/root`; new threads use `/new/root`" and "Open threads keep their profile."
- For the Inbox, Name and Root are disabled, with the reason shown.
- Save sends only the fields that changed.

### 2. Rename
- **Through the edit dialog.** The `⋯` menu's "Rename…" opens it with the Name field focused and selected. One editing path; the menu uses the owner's own word. Inline rename would conflict with the click that expands a row.
- **Refuse duplicate names** in the API, on POST and on PATCH:
  - comparison is case-insensitive and ignores surrounding spaces;
  - the check runs only when the name actually changes, so the existing pair of `e2e-calc` projects can still be edited until one is renamed or deleted;
  - "Inbox" is reserved for the Inbox.
- Enforce this in the daemon, not the store. Records already on disk must still load, and `schedules_tools_check.py` creates a duplicate name differing only in case on purpose.

### 3. Delete
**Semantics: re-home chat threads to the Inbox, remove the records of finished tasks and the project's schedules, and refuse while any task is unfinished. Never touch disk outside Jarvis's data folder.**

Why this over the alternatives:
- **Refuse while anything references the project:** the HUD has no way to delete threads or tasks, so the owner could never delete `e2e-calc`.
- **Pure cascade:** destroys the owner's conversations.
- **Archive:** spreads a `hidden` flag into every consumer (`router.place`, Discord `_locate`, `lastProject`, schedules, the MCP tools) and leaves the junk projects in place forever.
- **Re-homing chat threads** reuses the existing move semantics: the thread is re-labelled, never re-rooted, and can be moved again later.

Details:
- **Inbox:** cannot be deleted (409).
- **Unfinished tasks:** this means any non-terminal state, including intake, blocked and so on. They block the delete, and the confirmation lists each one with a **Cancel task** button (the existing `POST /tasks/{id}/cancel`).
  - For a blocked task the cancel takes effect immediately.
  - For a running task it takes effect at the task's next step, and the dialog re-reads when `task_status_changed` arrives.
- **Chat turns in progress do not block.** A move is already allowed mid-turn. The live `_Session.thread.project_id` must be updated, exactly as the move route does. Otherwise `_record` saves the stale thread and undoes the re-home.
- **Sessions of finished tasks' threads:** close each one with `daemon.close_thread` before removing its records. If closing fails, answer 409.
- **Worktrees:** left on disk, branch included, because they sit inside the owner's folder and may hold unmerged work. The confirmation and the response list them by path and branch. The ones that still exist are what the owner may want to remove by hand.
- **Schedules:** deleted. Each publishes `schedule_deleted`.
- **Discord:** the mapping disappears with the project record. The channel and any task threads in it are never deleted, and the confirmation says so. Messages posted there afterwards are treated as `other`.
- **Thread logs:**
  - Re-homed chat threads keep their logs.
  - Finished tasks' journals and their worker threads' logs go to a trash folder, `~/.local/share/jarvis/v2/trash/<stamp>-<project_id>/`, moved there with `os.replace` on the same filesystem. They can be restored by hand; there is no restore UI (open decision 2).
- **Ledger and `decisions.jsonl`:** left alone. They are history, not references.
- **Order, so a crash midway is safe:**
  1. re-home the chat threads;
  2. delete the schedules;
  3. move the finished tasks and their threads to trash;
  4. delete the project record last.

  Each step is atomic. Running the delete again finishes the job.
- **Soft delete or archive:** not worth it. The trash folder gives a manual undo for a fraction of the cost.
- **Confirmation text,** read back from `GET /impact`. For example:
  > Delete project **e2e-calc** (`/tmp/…/repo`)?
  > · 0 chat threads move to the Inbox (they keep their folder)
  > · 1 finished task and its 4 worker threads are removed (kept in Jarvis's trash)
  > · 0 schedules are deleted
  > · Left on disk: nothing (recorded worktrees are already gone)
  > · Nothing inside `/tmp/…/repo` is touched.

### 4. Backend
**Store changes (`jarvis/v2/stores.py`):**
- `ProjectStore.delete(id)`:
  - refuses the Inbox;
  - refuses while any thread or task, saved or pending, still references the project;
  - then `unlink`s only `projects/<id>.json`.
- `TaskStore.trash(task_id, dest)`: terminal tasks only. It moves the task directory and the directories of its threads (those listed in `thread_ids` plus those with `task_id` set). This bypasses `ThreadStore.delete`'s refusal of task threads, on purpose and in one place.

**Daemon changes (`jarvis/v2/daemon.py`):**
- Factor the move logic out of `hud_api.py`'s `PATCH /threads/{id}` into `Daemon.move_thread(thread_id, project_id)`: save, update the live session, publish `thread_moved`. Both the route and the delete use it.
- `Daemon.project_impact(pid)`: read-only. It must not call `projects.inbox()`, which would create the Inbox.
- `Daemon.delete_project(pid, expect)`:
  - close the task-thread sessions first, outside the locks;
  - then take, in this order: `schedules.lock` → `daemon._lock` → `_worktree_lock` → stores `_lock` (`Schedules.fire` already takes `schedules.lock` before the stores);
  - recompute the impact and compare its token;
  - then run the ordered steps.

**Routes, with their `docs/hud-api.md` additions:**
- `GET /projects/{id}/impact` returns:
  ```
  {project_id, name, root, inbox, token,
   blockers: [str],
   chat_threads: {count},
   tasks: {finished: n, active: [{id, brief, state}]},
   task_threads: n,
   schedules: [{id, describe, enabled}],
   worktrees: [{task_id, path, branch, exists}],
   discord_channel_id, discord_task_threads: n}
  ```
  `token` is a hash of the sorted ids and states it counted.
- `DELETE /projects/{id}?expect=<token>`:
  - success, 200: `{deleted, inbox_id, rehomed: [thread ids], removed: {tasks, threads, schedules}, left_on_disk: [paths], trash}`;
  - 400 when `expect` is missing;
  - 409 for the Inbox, for blockers (the message lists them), and for "changed since you looked";
  - 404 for an unknown project.
- `PATCH /projects/{id}`:
  - name rules: unique ignoring case, refused with 409;
  - root rules: must be an existing directory (400), and is refused while unfinished tasks or recorded worktrees exist (409);
  - a request that changes nothing returns 200 and publishes nothing.
- `POST /projects`: uniqueness and existing-directory checks.
- Bus events, each with a top-level `project_id` so `/events?project=` can filter them:
  - `project_created`;
  - `project_updated {project_id, changed: [fields]}`;
  - `project_deleted {project_id, name, inbox_id, rehomed: [ids], removed_task_ids: [ids]}`.
- No delete tool in the fast path, MCP or Discord: deletion is an owner action from the HUD only.

### 5. HUD
**Sidebar (`components/Sidebar.tsx`):**
- Each project row gets a `⋯` `<button>` (`project-menu-{id}`) styled like the thread menu, plus `onContextMenu` on the row.
- Generalise the `menu` state into a union, `{kind: "thread"} | {kind: "project"}`, still rendered with `.threadmenu`.
- Items: **Edit…**, **Rename…**, then a separator and **Delete…** in `var(--red)`.
- On the Inbox, Rename and Delete are disabled, with a `.mwhy` explaining why ("The Inbox is where unplaced chat lands…").
- Keyboard: `⋯` can be reached with Tab; focus goes to the first item when the menu opens and back to `⋯` when it closes; Escape closes it (the existing handler).

**Confirmation (`components/DeleteProject.tsx`, new):**
- A modal that fetches `/impact` and renders `deleteSummary(impact)` as plain text.
- When there are blockers: the Delete button is disabled, the blockers are listed, each unfinished task gets a Cancel button, and the dialog re-fetches on `task_status_changed`.
- The Delete button is never auto-focused. A keydown handler on the card stops Enter from doing anything (the approval-card rule). Escape cancels.
- A 409 is shown, and the dialog re-reads the impact.

**After a delete,** whether this window did it or `project_deleted` arrived from another, a pure helper `afterProjectDeleted(...)` (in `lib/projects.ts`) works out the result:
- Projects, threads, tasks and schedules are refetched, and `platforms[id]` is dropped.
- A compose row in the deleted project is re-aimed to `lastProject(...)`. A compose row whose `openedId` thread was re-homed follows that thread to the Inbox.
- An open re-homed chat thread stays open; its project is now the Inbox, derived as usual. A notice reads "Project X was deleted; this thread moved to the Inbox."
- An open deleted task thread or task returns the window to `newThread()`.
- `jarvis.hud.lastProject` is cleared when it holds the deleted id (`forgetLastProject`).
- An open edit, NewTask or Schedule dialog for that project closes with a message.

**Other HUD changes:**
- On `project_created` or `project_updated`: refetch the projects, and that project's platform (the root may have changed).
- The top-bar profile select shows refusals, and gets the tooltip "Applies to threads opened from now on."
- Add `api.ts` methods `projectImpact` and `deleteProject`, `types.ts` types `ProjectImpact` and `ProjectDeleteResult`, and the `store.tsx` picker values `"editProject" | "deleteProject"`.

### 6. Tests (each fails against the current code)
**Backend, in `tests/v2/hud_backend_check.py` (or `daemon_check.py`):**
- A delete with a chat thread, a done task plus its thread, a schedule and a recorded worktree directory:
  - returns 200;
  - the chat thread is now in the Inbox with its `cwd` and `brief.json` byte-for-byte unchanged;
  - the task, its thread and the schedule are gone from the lists and present under `trash/`;
  - a sentinel file in the root and the worktree directory still exist;
  - the bus saw `project_deleted`.
- A delete of the Inbox returns 409; with an unfinished task, 409 naming it; with a stale `expect`, 409; with no `expect`, 400.
- `/impact` matches what the delete then does, and creates no Inbox (the project count is unchanged).
- An idle open session on a re-homed thread: a later usage save keeps the Inbox id (this guards against the stale-session undo).
- PATCH:
  - a duplicate name differing only in case returns 409;
  - a profile change on an existing duplicate-named project returns 200;
  - a root that is not a directory returns 400;
  - a root change with a recorded worktree returns 409;
  - a successful root change leaves an old thread's `cwd` alone;
  - `project_updated` and `project_created` are published.
- Store:
  - `ProjectStore.delete` leaves the other `projects/*.json` files in place (the rmtree trap);
  - it refuses while references remain.
  - Update `stores_check.py:177` accordingly.

**Vitest, beside the libs (`hud/src/lib/projects.test.ts`):**
- `afterProjectDeleted`, for every case listed in section 5.
- `projectNameTaken`: ignores case and spaces, excludes the project itself.
- `projectEditBody`: only changed fields; the Inbox's name and root are never sent.
- `deleteSummary`: singular and plural forms, the "nothing in `<root>` is touched" line, the worktrees listed.
- `forgetLastProject`.

**Headless, in `tests/face/hud_v2_check.py`, with the mock extended for `/impact`, `DELETE /projects/{id}`, the PATCH name and Inbox refusals, and the new events:**
- The `⋯` menu and the context menu open the project menu; on the Inbox, Rename and Delete are disabled with the reason.
- Edit:
  - the dialog is prefilled, and PATCH sends only the changes;
  - a duplicate-name 409 is shown and the dialog stays open.
- Delete, confirmation:
  - `/impact` is fetched and its counts appear;
  - Enter sends nothing;
  - Escape sends nothing.
- Delete, confirmed:
  - a click sends `DELETE …?expect=`;
  - the row disappears;
  - the compose row is re-aimed;
  - localStorage is cleared.
- Blockers: Delete is disabled, and Cancel task posts `/tasks/{id}/cancel`.
- A `project_deleted` or `project_updated` emitted as if from another window updates the sidebar and the open conversation.
- A refusal from the top-bar profile select is shown.

### 7. Risks
1. Using `_delete`'s rmtree on the flat project store would wipe every project. Write a dedicated `unlink`.
2. A re-home that misses the live `_Session.thread` gets reverted at the next usage or finish save.
3. Lock ordering between `Schedules.fire` (`schedules.lock` → stores → `runner.start`) and the delete. Use one documented order, and close sessions outside the locks.
4. A partial cascade after a crash. The order above plus re-running the delete covers it.
5. A root change with worktrees recorded makes `worktrees.remove` act on the wrong repo. Hence the guard.
6. Adding the existing-directory check to `POST /projects` may break tests that pass fake roots. The ones checked (`daemon_check`, `permissions_check`) use real directories; grep the rest.
7. The new uniqueness rule (ignoring case) and `router.place` (exact match) still disagree. Optionally make `place` ignore case too, which `tools/schedules.py` already does.
8. Already present today, not fixed here: `@path` attachments resolve against the project's *current* root, not the thread's `cwd`. A re-rooted or moved thread's `@path` points at the new folder.
9. The bus is capped at 256 events. The HUD refetches whole lists on each event, so a dropped event costs nothing.

## Files to change
- **Backend:** `jarvis/v2/stores.py`, `jarvis/v2/daemon.py` (PATCH/POST rules, `project_impact`, `delete_project`, `move_thread`, the DELETE route), `jarvis/v2/hud_api.py` (the thread PATCH route calls `daemon.move_thread`), `jarvis/v2/schedules.py` (a `delete_for_project` helper called under its lock), optionally `jarvis/v2/router.py` (`place` ignoring case).
- **HUD:** `hud/src/components/Sidebar.tsx`, `hud/src/components/Pickers.tsx` (`ProjectDialog`), `hud/src/components/DeleteProject.tsx` (new), `hud/src/App.tsx`, `hud/src/api.ts`, `hud/src/types.ts`, `hud/src/state/store.tsx`, `hud/src/lib/projects.ts` plus its test (new), `hud/src/lib/compose.ts` (`forgetLastProject`), `hud/src/theme.css` (a `.mrow.danger`, the delete card).
- **Tests:** `tests/v2/hud_backend_check.py`, `tests/v2/stores_check.py`, `tests/face/hud_v2_mock.py`, `tests/face/hud_v2_check.py`.
- **Docs:** `docs/hud-api.md`, design §18, and CLAUDE.md (the semantics: deleting a project never touches its folder, and chat threads go to the Inbox).

## Verification
1. `uv run python tests/v2/hud_backend_check.py`, `stores_check.py`, `daemon_check.py`, `schedules_tools_check.py` (the case-duplicate test must still pass).
2. `cd hud && npx vitest run && npm run build`, then `uv run python tests/face/hud_v2_check.py`.
3. Live, against `jarvis daemon2`:
   - Open the `⋯` menu on `571fbc98`. The confirmation shows 1 blocked task; cancel it, then delete.
   - Delete `d71b5c52`; the counts read 1 task and 4 threads.
   - Confirm that `~/.local/share/jarvis/v2/trash/` holds both and that the Inbox was created.
   - Rename `test` to `e2e-calc` and confirm the refusal while another `e2e-calc` still exists.
   - Change a project's root and confirm an old thread still shows `↪ <old folder>`.
   - Run `tests/face/hud_v2_live_check.py`.

## Open decisions for the owner
1. **Delete semantics.** I recommend re-homing chat threads to the Inbox, removing finished tasks and schedules, and refusing while any task is unfinished. The alternatives are a pure cascade or an archive.
2. **Trash or hard delete for Jarvis's own records.** I recommend moving them to `v2/trash/` with no restore UI.
3. **Worktrees.** I recommend leaving them in place and listing them. The alternative is refusing the delete until they are removed with the existing worktree route.
4. **Duplicate names.** I recommend refusing them on create and rename, ignoring case, and letting existing duplicates stand until edited.
5. **Is `root` editable?** I recommend yes, guarded: an existing directory, no unfinished tasks, no recorded worktrees.
6. **`discord_channel_id`.** I recommend read-only in the HUD.
7. **Routing in the edit dialog.** I recommend no; it stays in Settings.
8. **Type the project name to confirm?** I recommend no. A deliberate click on a button Enter can't trigger, the counts read back from the backend, and the stale-token check are enough, since nothing on disk is lost.
9. **A profile change while a task is in flight.** I recommend allowing it and saying so in the dialog. The alternative is a runner change that fixes `task.profile` when the task starts.
10. **Inline rename in the sidebar.** I recommend not now; "Rename…" opens the dialog.
11. **Agents or Discord deleting projects.** I recommend no.

## Overlap with the "model per thread" plan
Files both features will likely touch:
- `hud/src/components/Sidebar.tsx`: the thread `⋯` menu. This plan turns the `menu` state into a union, and a "Model…" item would land in the same code.
- `hud/src/App.tsx`: the event switch, the pickers, the top bar.
- `hud/src/components/Pickers.tsx`: `NewProject` becomes `ProjectDialog`, next to `ModelPicker`.
- `hud/src/state/store.tsx` (the picker union), `hud/src/api.ts`, `hud/src/types.ts`.
- `jarvis/v2/hud_api.py`: `PATCH /threads/{id}`, which this plan factors into `Daemon.move_thread`. A per-thread `model` field would most likely be added to that same body.
- `jarvis/v2/daemon.py`: `make_brief`/`open_thread`, next to the project routes.
- `tests/face/hud_v2_mock.py` (`do_PATCH` for threads and projects) and `tests/face/hud_v2_check.py`.
- `docs/hud-api.md`, design §18, CLAUDE.md.

Suggested sequencing: land this feature's backend refactor first (`move_thread` plus the project routes), together with the Sidebar menu union. The model feature then adds its menu item and PATCH field on top. Both features must keep `brief.json` untouched when re-homing a thread.
