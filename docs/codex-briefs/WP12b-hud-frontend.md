# WP12b — HUD v2 frontend

Work package 12b of the Jarvis v2 redesign, in a git worktree on branch
`jarvis/wp12b-hud-frontend`. Claude leads and you are Claude. The backend
(WP12a) is being built in parallel by Codex against the same fixed
contract, so you build against a **mock server** of that contract and the
integration check runs at the end against whichever is ready. Read first:

1. `docs/hud-api.md` — fixed. `docs/jarvis-v2-design.md` §12 (all of it:
   decisions, layout, invariants), §11.3 (what the status embed shows —
   the HUD shows the same record), §8.5 (the routing line), §6 (approval
   semantics), §17.
2. `jarvis/face/static/jarvis.html` — the v1 HUD. You are **rebuilding**,
   not porting the file, but these parts carry over as components with
   their behaviour intact, and their v1 tests (`tests/face/*.py`) tell you
   exactly what that behaviour is: the orb (canvas rings, `RING_SETS`,
   state palette, PTT), the avatar `<img>`, `WAKE_PATTERNS`/`matchesWake`
   and the recognizer de-dup (`hud_wake_check`), the always-open mic
   capture with ring buffer, adaptive threshold, pre-roll, follow-up
   window, his-own-speech suppression and the mute clamp
   (`hud_capture_check` — read it in full; it is the spec), the approval
   card (`hud_approval_check`: deny cheap, authorize deliberate, nothing
   keyboard-defaulted, PTT inert while up, chime), the markdown renderer's
   *rules* (`hud_markdown_check`: no `innerHTML`, scheme-checked links,
   owner text verbatim), the live draft (`hud_delta_check`), the pickers
   (`hud_model_check`, `hud_avatar_check`, `hud_session_check`), typed
   input + attachments (`hud_input_check`, `attach_check`).
3. `CLAUDE.md` *Face & voice*, *Voice round 2*, *The owner can mute
   themselves*, *Avatars*, *Model selector* — the reasoning behind each.

## Stack and layout

`hud/` at the repo root: Vite + React 18 + TypeScript, `npm` (node 22 is
installed), `monaco-editor` for the File and Diff tabs, `markdown-it` +
`dompurify` for docs and replies (rendered into a container after
sanitizing: no raw HTML, links `target=_blank rel=noopener` and
http/https/mailto only), no other runtime dependencies without a reason
in the notes. Build output `hud/dist/` (gitignored; add the ignore).
Theme: the v1 skin — cyan on black, angular panels, the orb — applied to
the §12.2 three-pane layout. Phone width is not a target (this is the
desk window) but nothing may overflow horizontally at 1280 px.

## Behaviours to ship (each is a test)

- **Sidebar:** projects (with the Windows badge from `/platform`),
  expanding to chat threads and tasks; a task expands to its threads with
  role and provider (the orchestration view). New project (name + root +
  access folders + profile), new chat thread, new task (brief). Schedules
  and Usage and Route entries at the bottom.
- **Chat tab:** rendered replies, tool stream (started/finished with
  durations, the v1 OPERATIONS ticker), live draft from `text_delta`
  (plain text while drafting, rendered once on `text`), the owner's
  messages verbatim, a `proposal_reply` shown as a system line with a
  60-second **Cancel** button that calls `/tasks/{id}/cancel`.
- **Input bar:** typed text + Enter, attachments (picker, drag, paste,
  `@path`), the **dictation mode control** (AUTO / REVIEW / OFF) beside
  the mic level; the orb is PTT. AUTO sends a finished utterance
  (`/stt` then `/send`); REVIEW puts the transcript in the box and
  focuses it; OFF is mute (v1 semantics incl. the wake recognizer
  stopped). Persisted in `localStorage`; a fresh window boots into REVIEW.
  Space in the box never triggers PTT.
- **Task tab / right pane:** spec (with open questions and an answer box
  per blocking question → `/answer`), plan with the current step marked,
  the status record (phase, step, elapsed clock, cost, last tool, last
  file), the routing line, steer box (`/steer`), cancel/resume buttons,
  the report in the §10.4 layout when done. `task_status_changed` drives
  it; never the model's prose.
- **File tab:** tree (`/tree`), open a file in Monaco, save with the
  mtime guard (409 → reload prompt), protected files shown as "withheld"
  with no content; markdown files open in a **split view** (Monaco left,
  rendered right, live) with a rendered-only toggle for reading docs.
- **Diff tab:** the task's changed files list and a Monaco diff editor
  per file (`/diff`, `/diff/file`), truncation stated.
- **Preview tab:** an iframe; a URL box that accepts the project's
  preview origin paths and `http://localhost:<port>/…`; **refuses the
  HUD's own origin** (8402) with a message. Sandboxed iframe
  (`sandbox="allow-scripts allow-forms allow-same-origin"` is *not*
  allowed together — use `allow-scripts allow-forms` only).
- **Approvals:** the card (v1 semantics, chime), fed by
  `approval_requested`; resolves via `POST /approvals/{id}`; the queue in
  the right pane when more than one is open; each shows the **whole
  command** and the task label; ALWAYS button when the request allows.
- **Usage panel:** both providers' state, today's figures, quota windows
  when present (else "not reported"), refreshed on `usage_updated`.
- **Schedules:** list, create (cron or every), enable/disable, run now,
  last/next run.
- **Pickers:** model (fast path only, with the routing table shown
  read-only beside it and a note that it does not change Claude/Codex),
  voice, avatar (relabel live on SSE), mute.
- **Route view:** the table, ledger states, last ten decisions with
  reasons (`/route`).

## Tests

- `hud/src/**/*.test.ts` with Vitest for pure logic (wake matcher, the
  segmenter math ported from `hud_capture_check`'s expectations, markdown
  sanitizing, dictation-mode reducer, diff list rendering).
- `tests/face/hud_v2_check.py`: **headless Playwright against a Python
  mock server** implementing `docs/hud-api.md` (`tests/face/hud_v2_mock.py`,
  scripted responses and a controllable SSE stream — the v1
  `hud_state_check` puppet pattern) serving `hud/dist`: every behaviour
  above, plus the safety ones verified to bite: a reply with markup
  renders as text, a `javascript:` link renders as text, the preview
  refuses the HUD origin, a hostile avatar SVG's `onload` never runs, the
  approval card ignores Enter, an SSE `approval_requested` from a task
  shows its label and full command, dictation REVIEW never sends on its
  own and OFF uploads nothing, a wake hit fires once per phrase.
- `tests/face/hud_v2_live_check.py` (manual): the same suite's smoke
  subset against the real daemon when WP12a has merged; write it, run it
  only if `jarvis daemon2` is already up on 8402, else print SKIPPED.

Run: `cd hud && npm ci && npm run build && npm test`, then
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/face/hud_v2_check.py`.
Playwright is installed in the venv (v1 uses it).

## Rules

Stay inside `hud/`, `tests/face/hud_v2*.py`, `.gitignore` (one line),
`docs/codex-briefs/WP12b-notes.md`. Do not edit the daemon, v1 code or
the fixed interfaces; contract changes are proposed in the notes. Commit
`hud/package-lock.json`. When green, commit with a message starting
`v2 WP12b: HUD frontend` ending with `Co-Authored-By: Claude Opus 5
<noreply@anthropic.com>`. Do not push or merge. Finish with
`WP12b-notes.md` (under 80 lines): what you built, the component map
(what was ported from `jarvis.html` and what was rewritten), test commands
and last lines, workarounds, proposed contract changes. Report branch,
worktree path and notes path as your final message.
