# Owner decisions: per-thread model, and project rename/edit/archive

Recorded 2026-10-06. Both plans in this folder are approved **as amended
here**: where this file and a plan disagree, this file wins. Lines marked
*Interpretation* are where the lead filled a gap in the owner's words. They
are the owner's to overrule at PR review.

- Plans: [`2026-10-06-thread-model-plan.md`](2026-10-06-thread-model-plan.md)
  and [`2026-10-06-projects-plan.md`](2026-10-06-projects-plan.md).

---

## A. Choosing the model for a thread

**A1. Three providers per chat thread: OpenRouter, Claude, Codex.** This
overrides the plan's "fast path only". A chat thread can run on:
- **OpenRouter**: today's fast path, any eligible OpenRouter model;
- **Claude**: Claude Code via the Agent SDK provider;
- **Codex**: the Codex app-server provider.

How that works:
- *Interpretation:* the provider is chosen while composing and is fixed once
  the first message is sent, because a session cannot change provider. The
  model and effort can still change mid-thread, within that provider.
- A Claude or Codex chat thread is a full agent working in the thread's
  folder. It stays behind the same gate as everything else:
  - the project's permission profile;
  - the §6 `PreToolUse` / permit path;
  - approvals that reach the owner.

  Nothing runs ungated.
- This amends design §8.1 ("the fast path cannot change anything" no longer
  describes every chat thread) and §12.1 ("the picker can never touch Claude
  or Codex"). Update both.

**A2. The model list.**
- The chip shows the owner's pinned models (the roster) and a **"search
  catalogue…"** entry that opens the full catalogue search.
- A model found there can be used for this thread, and pinned to the roster.
- Eligibility is still a refusal: a model that cannot call tools is never
  offered.
- Claude and Codex threads list their own providers' models, from the same
  source of truth the router's model validation uses.

**A3. A pinned model later removed from the roster** stays pinned on its
threads and is shown as "(not on roster)". It never silently falls back.

**A4. Reasoning effort defaults to high for every model**, unless that
model has its own effort set (the roster's per-model `efforts`). Then that
setting is that model's default.
- It is limited to what the model supports.
- A model with no reasoning control gets none.
- Effort can be changed per thread, and changing the model resets the effort
  to the new model's default.
- *Interpretation:* this is the default for HUD threads in v2. v1's global
  `JARVIS_REASONING_EFFORT` (max), used by `jarvis chat` and the face, is left
  alone.

**A5. Every new thread starts on the default.** A new thread does not
inherit the previous thread's choice.
- **Chat (OpenRouter):** `deepseek/deepseek-v4-flash-0731`, which is the
  current orchestrator default. The owner may change it later, so it stays
  one setting (the global Model picker / `JARVIS_ORCHESTRATOR`), not a value
  in the code.
- **Work thread: Claude Opus 5.5 at high effort.**
  - *Interpretation:* "work thread" means a chat thread on the Claude
    provider; that provider's default model is `claude-opus-5-5`, effort
    `high`.
  - A Codex thread defaults to Codex's existing routing default.
  - Task roles keep the routing table.
- Flagged, not blocking:
  - CLAUDE.md's routing policy (2026-07-30) says personal chat should not go
    to Chinese-hosted models. Before relying on DeepSeek V4 Flash as the
    chat default, check which providers OpenRouter actually routes it to
    (DeepInfra, StreamLake).
  - DeepSeek V4 is text-only, so images attached in such a thread cannot
    be seen. The HUD should say so rather than fail silently.

**A6. Fix the body-key bugs first.** The HUD sends `{id}` to `/model`,
`{name}` to `/voice` and `{mute}` to `/mute`; the daemon reads `model`,
`voice` and `muted`. Fix the mock to match the real daemon, so the suite can
catch this class of bug.

**A7. Show every model change and every effort change in the chat** as
system lines, and record them in the thread's log.

---

## B. Projects: rename, edit, archive

**B1. Archive instead of delete.**
- Archiving a project hides it from the sidebar and from everything that
  places work:
  - `router.place` and Discord `in <name>:`;
  - `lastProject`;
  - schedules (paused, not deleted);
  - the MCP and agent tools.
- Its threads and tasks keep all their metadata: project, times, provider,
  model, cost.
- An **Archive** view in the HUD lists archived projects and threads. The
  owner can:
  - open an archived thread's transcript (read-only);
  - **restore** it;
  - **delete permanently**. This is the only way anything is deleted.
- *Interpretation:* a single chat thread can also be archived from its `⋯`
  menu, and it shows up in the same Archive view.
- Archiving a project is refused while it has unfinished tasks. The dialog
  lists them, each with a Cancel button. It is also refused while a chat turn
  is running in the project; the refusal names the thread.
- The confirmation counts come from the backend (the plan's `/impact` read),
  and Enter cannot trigger it.

**B2. Permanent deletion goes to a trash, never straight to unlink.**
- Files on a Windows path (`/mnt/<drive>/…`) go to the **Windows Recycle
  Bin**, through PowerShell's `SendToRecycleBin`.
- Files on the Linux filesystem, which in practice is all of Jarvis's own
  records under `~/.local/share/jarvis/`, go to a **Linux trash** that follows
  the freedesktop Trash spec (`~/.local/share/Trash/{files,info}`).
  - It is purged automatically after a retention period (default 30 days,
    configurable), and can be emptied by hand.
  - *Interpretation:* the 30-day default.
- Permanent delete never touches a project's own folder or a task's
  worktree. Only Jarvis's records are deleted.

**B3. Worktrees** are left on disk with their branches and listed in the
confirmation.

**B4 + B10. Names are unique, and duplicates are auto-numbered rather than
refused.**
- A name that collides becomes `name (1)`, then `name (2)`, and so on. The
  comparison ignores case and surrounding spaces.
- This applies on create, inline rename, the edit dialog and restore from
  the archive.
- *Interpretation:* project names are unique across all projects, archived
  ones included. Thread titles are unique within their project.
- **Inline rename**, for both project rows and thread rows: double-click the
  name, or choose ⋯ → Rename. Enter saves, Escape cancels. It must not
  collide with the click that expands a row.
- The owner's two existing `e2e-calc` projects stay as they are until one is
  renamed.

**B5. A project's folder can change, and nothing already working is cut
off.**
- Running chat sessions and existing threads keep the folder they were
  opened in (frozen `cwd`).
- **Tasks pin the project root they were started under.** Worktree
  creation, commits and removal use the pinned root, so a task already
  running or blocked finishes and commits into the original repo.
- The edit dialog lists what stays on the old folder.
- The new root must be an existing directory. It applies to new threads and
  tasks only.
- This replaces the plan's "refuse while unfinished tasks or worktrees
  exist".

**B6. Discord is deferred.** It is the logical next feature, not part of
this one. The channel id shows read-only in the edit dialog.

**B7.** Routing stays in Settings.

**B8.** No "type the name to confirm".

**B9.** A profile change while a task is running is allowed, and the dialog
says it applies to work started from now on.

**B11. Only the owner, in the HUD, can archive or permanently delete
projects or threads.** No tool reaches it: not the fast path, MCP, Claude or
Codex threads, or Discord.

---

## Execution

- Two build agents, each in its own git worktree on a branch from `main`.
- Each opens a PR when its code passes every check.
- The lead reviews the PRs and the owner accepts them before anything
  merges.
- Merge order: projects first, then the model PR, rebased onto it. The
  body-key fix (A6) is the model PR's first commit.
- Both agents touch `App.tsx`, `Sidebar.tsx`, `api.ts`, `types.ts`, the mock
  and the headless suite. They keep their changes to those files additive
  and avoid refactoring code the other one needs.
- The headless suite takes `HUD_V2_CHECK_PORT`, so the two worktrees can
  run it at the same time.
