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
server.py`), re-homed; `POST /model` sets the **fast path's** global
default only.

The control bodies, exactly (2026-10-06, decisions A6 — the HUD sent `{id}`,
`{name}` and `{mute}` for weeks and every click reset the setting it meant to
change). An unknown key is a 400 that names it; it is never read as absent.

| route | body |
|---|---|
| `POST /model` | `{"model": id}` — `""` returns to the config default |
| `POST /models` | exactly one of `{"add": id}`, `{"remove": id}`, `{"model": id, "effort": level}` (`""` = AUTO) |
| `POST /voice` | `{"voice": name}` — `""` clears the override |
| `POST /mute` | `{"muted": bool}` |
| `POST /avatar` | `{"slug": slug}` |

## Projects (additions to WP7)

- `PATCH /projects/{id}` accepts `{profile, extra_dirs, always_ask,
  discord_channel_id, routing}` — already there; the HUD uses it for the
  profile switch and access folders.
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

## Additions 2026-10-06 — the model a chat thread runs on

Decisions in `docs/plans/2026-10-06-decisions.md` part A. A chat thread runs
on one of three providers — `fast` (OpenRouter, the fast path), `claude`,
`codex` — chosen when it is opened and fixed after. Its model and effort can
change at any time and apply **from the next message**, never in the middle
of a turn. The choice is stored on the Thread record (`model`, `effort`);
**`brief.json` is never rewritten.**

- `Thread.model = null` is **default**: OpenRouter follows the global Model
  picker (`models.tier("orchestrator")`, i.e. the owner's selection or
  `JARVIS_ORCHESTRATOR`) at the start of **every turn**; Claude is
  `claude-opus-5-5`; Codex is its routing default for the orchestrator role.
  `Thread.effort = null` is that model's default: `high`, or the roster's
  per-model effort on OpenRouter, clamped to the model's own levels; none for
  a model with no reasoning control.
- `POST /threads` for `role: "chat"` takes `provider` (default `fast`) and
  `brief: {model?, effort?}`. The model is checked **before** the record is
  created: 400 with the reason for a model that is not on the roster (or
  cannot call tools), not one of that CLI's models, or an effort off its
  ladder. A Claude or Codex chat thread opens in the project root under the
  project's profile and always-ask list, with the §6 permit as its gate.
  A provider the project cannot run is refused the same way, before any
  record exists (`thread_model.refusal`): Codex runs the `auto` profile only
  and cannot enforce always-ask additions; Claude and the fast path have no
  `strict` mode. A refusal only the provider sees (at start) deletes the
  never-started record before the error is returned.
- **A caller's `brief` cannot loosen its project** (any role, any provider;
  the runner's own `Brief` objects are not callers). `cwd` must be the
  project's root, `profile` the project's or stricter (`auto` < `ask` <
  `strict`), `always_ask` must keep every one of the project's commands
  (adding more is fine), and `mcp_servers` must be empty. Anything else is a
  400 naming the field. The HUD sends none of these.
- `PATCH /threads/{id}` `{model?, effort?}` (with `project_id` as before; not
  both in one request) → the thread record. `model: null` returns to the
  default and resets the effort; a new model resets the effort unless one is
  given with it; an effort alone on a default thread pins the default model
  it is an effort of. A model already pinned stays usable after it leaves
  the roster (shown "(not on roster)"). 400 with the reason on any refusal;
  409 for a task's thread or a provider that cannot switch. There is no
  `provider` field: a session cannot change provider.
- Every change writes a `model_set` record to the thread's log (`data.text`,
  e.g. `model → moonshotai/kimi-k3 · high (from the next message)`, or
  `(follows the default)` when a default thread's turn starts on a new global
  model) and publishes it, then `thread_updated {…thread, effective_model,
  effective_effort}`. `GET /threads/{id}/transcript` returns `model_set`
  records as `role: "system"`.
- A provider that refuses the switch at the turn's start does not cost the
  message: the record is rolled back to what the provider still runs (a
  default thread whose default moved on is pinned to it, so the switch is
  not retried every turn), a `model_set` line says `switch to X refused:
  <reason>; still on Y` (with `refused_model`, `refused_effort`), a
  `thread_updated` follows, and the message is sent on the old model. If the
  provider lost the session as well (Claude: neither the new model nor the
  old one reconnects), the turn ends with an `error` and the session is
  dropped; the next send resumes it on the old model.
- Each `usage` event in a thread's log carries `data.model` and
  `data.effort`: which model answered that turn.
- `GET /thread-models` → `{"effort_default": "high", "providers": {"fast" |
  "claude" | "codex": {"label", "default", "default_effort", "models":
  [{"id", "name", "efforts", "vision", …}], "note", "profiles",
  "always_ask"}}}`. `profiles` lists the permission profiles the provider can
  run a chat thread under and `always_ask` says whether it can enforce a
  project's always-ask commands; the HUD greys a provider out from these, and
  `POST /threads` refuses by the same table. A row without `efforts` (an
  `unlisted` roster model) has an unknown ladder, not none. OpenRouter lists the
  roster (`models.describe()` rows, with the roster's `effort`); Claude and
  Codex list `router.CLI_MODELS`, the table the router's capability filter
  reads. The full catalogue stays at `GET /models/catalog`; a model used from
  it is pinned to the roster (`POST /models {add}`) first.
- No tool reaches any of this. The agent cannot change its own model or
  provider.
