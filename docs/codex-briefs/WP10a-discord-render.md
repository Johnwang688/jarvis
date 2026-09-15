# WP10a — Discord threads, status embeds and milestone templates

You are Codex, implementing the rendering half of work package 10 of the
Jarvis v2 redesign, in a git worktree on branch `jarvis/wp10a-discord-render`.
Claude owns the other half (guild routing, verbs, thread-scoped approvals)
and will build it on what you deliver. This brief is your entire scope. Read
first:

1. `docs/jarvis-v2-design.md` — §11 in full (11.1 topology, 11.3 update
   protocol), §10.3 (the status record is runner-written, never
   model-written), §10.4 (report schema), §4, §14 row WP10.
2. `jarvis/v2/model.py` (fixed: `Task`, `Status`, `Report`, `Spec`,
   `RoutingDecision`), `jarvis/v2/stores.py`, `jarvis/v2/bus.py` and
   `jarvis/v2/daemon.py` (merged; the EventBus and the lifecycle records
   you subscribe to).
3. `jarvis/tools/discord.py` (v1 REST: `_load_bundle`, `_api`, `_fail`,
   `owner_dm_channel`) — reuse the bundle loader and the httpx call shape;
   do not duplicate credential handling. `tests/discord_check.py` for the
   fake-transport style. `docs/codex-briefs/WP7-notes.md`.
4. Discord's REST docs for: create thread from a channel
   (`POST /channels/{id}/threads`, type 11 public thread), create/edit
   message with embeds, attachments (multipart), archived-thread reopen
   by posting, and the per-channel rate limits on message edits. Record
   the exact endpoints and limits you relied on.

## Deliverables (`jarvis/v2/discord/`)

- `rest.py`: `DiscordRest(transport=None)` with `create_channel(guild_id,
  name)`, `create_thread(channel_id, name) -> thread_id`,
  `post(channel_id, content=None, embed=None, files=()) -> message_id`,
  `edit(channel_id, message_id, content=None, embed=None)`,
  `unarchive(thread_id)`. All calls through the v1 bundle's bot token via
  an injected transport so tests never touch the network. 429s honoured
  with `retry_after`; any other failure → `DiscordError` with the v1
  `_fail` text.
- `render.py`, pure functions, no I/O: `status_embed(task, project) ->
  dict` (title, phase, step `i/n`, elapsed, cost, last action, open
  question, routing line — §8.5 — every field from `Task.status`, never
  from model text); `milestone(kind, task, **ctx) -> str` for
  `started|question|approval|blocked|verified|done|failed`, each ≤ 400
  characters **enforced by truncation with an ellipsis, never by
  raising**; `report_text(report) -> str` in the §10.4 layout, ≤ 1500
  characters with the overflow returned separately as an attachment body;
  `approval_text(tool, args, code, origin)` shows the **entire** command
  (this one may exceed the cap: it is the one message that must not be
  truncated — attach it as a file if over 2000). Discord limits: message
  2000, embed description 4096, title 256, 25 fields.
- `reporter.py`: `Reporter(stores, rest, bus)` — subscribes to the bus;
  on `task_created` creates the task's thread in the project channel (or
  adopts `task.discord_thread_id`), posts `started`, posts the first
  status embed and stores its message id on the task (add nothing to the
  model: keep a `discord_status_message_id` in a sidecar JSON beside the
  task, the WP7 brief-sidecar pattern); on every status change coalesces
  edits to **at most one per 5 s per channel** (a timer, the last state
  wins); posts milestones on transitions (`BLOCKED` → `blocked`,
  `VERIFYING → DONE` → `verified` then `done` with the report, `FAILED` →
  `failed`); posts nothing else — no tool chatter, no deltas. Projects
  without a `discord_channel_id` are skipped silently and counted. A
  Discord failure never raises into the bus: log, count, continue.

## Tests: `tests/v2/discord_render_check.py`

Fake transport recording every request. Cover: each endpoint's exact
method/path/body; 429 retry; thread created once and adopted after;
status embed fields from a hand-built `Task` (and that changing
`task.spec.goal` text changes nothing in the embed — runner-written
only); every milestone ≤ 400 chars on a 5 000-char input; report at the
cap with overflow attached; approval text never truncated and attached
when long; edit coalescing (five status changes in 1 s → one edit, the
last state); milestone posts on each transition; project without a
channel skipped and counted; a transport exception logged and swallowed;
no credential value in any request body or log line.

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/discord_render_check.py`
then `tests/discord_check.py` and `tests/v2/daemon_check.py`.

## Rules

Stay inside `jarvis/v2/discord/` (new package), `tests/v2/discord_render_check.py`,
and `docs/codex-briefs/WP10a-notes.md`. Do not edit the fixed interfaces,
merged v2 modules, v1 code, or CLAUDE.md. No new dependencies (httpx is
already one). No gateway, no message parsing, no approval resolution —
those are Claude's half. When green, commit with a message starting
`v2 WP10a: Discord threads, embeds, templates` ending with
`Co-Authored-By: Codex <noreply@openai.com>`. Do not push. Finish with
`WP10a-notes.md` (under 60 lines): what you built, the Discord endpoints
and limits you verified, test commands and last lines, workarounds,
proposed interface changes.
