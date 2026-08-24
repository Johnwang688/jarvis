---
description: Use when the owner asks about schoolwork or assignments — what's due, "review my schoolwork", preparing for the next school day, Canvas or MySchoolApp
---

1. Everything goes through the `sw` CLI via `run_command` (a WSL wrapper at
   `~/.local/bin/sw` forwards to the Windows-side dashboard at
   `C:\myday\schoolwork`). Bare `sw` lists commands. The three that matter:
   - `sw prep --json` — the next school days: A/B letter, what is due, which
     classes meet, where each course's study material lives.
   - `sw review --json` — the facts for a review: data-health checks,
     per-course stats, every undated item with its signals. No AI, no network.
   - `sw list` — the next assignments, terminal-readable.
2. "What's due" / "what do I have tomorrow": read `sw prep --json` and report
   plainly. Name **locked** items (`locked: true`) — the source will not open
   them, so he cannot start them however close the date is. Check `health` in
   the review first: `needs_login` or `last_sync_failed` means part of the
   board is stale — say so and name the fix (`sw login-msa`, run by the owner
   on Windows; it opens a browser for a human sign-in). Never silently report
   data a failed sync left behind.
3. "Review my schoolwork": run `sw review --json`, judge which undated items
   look live, store verdicts with `sw triage-apply <file>` — one word each
   (`soon` / `later` / `ignore` / `unclear`) plus the evidence. Never invent a
   due date; prefer `unclear` to a guess. Verdicts land in the `triage` table
   only and change no school data. Full rules: `AGENTS.md` in the schoolwork
   repo.
4. Never run `sync`, `login-msa` or `msa-probe` unprompted — they reach the
   school's servers. Sync already runs on a Windows schedule; ask before
   forcing one.
5. The database cannot be read from WSL: it is WAL-mode SQLite on the 9p
   mount, and even a `mode=ro` open fails with `disk I/O error`. The CLI is
   the interface — never touch `data/` directly.
6. Study material goes where `sw prep` says (`courses[].kind`): a `repo`
   course has its own repository with its own CLAUDE.md (AP US History is a
   full study system; AP Physics 1 is `needs-setup` — say what is missing
   rather than inventing material). A `local` course uses
   `materials/<slug>/` inside the schoolwork repo.
7. The boundary: the dashboard reads school systems and never submits — and
   the same rule binds you. Track, plan, and build study materials when
   asked; never do or submit the schoolwork itself. Membean is on the board
   as an assignment; completing it is already declined in CLAUDE.md.
