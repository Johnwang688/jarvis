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
| `POST /model` | `{"model": id}` — "Set as default"; `""` is "Reset to config default" (re-lists the env model if it was unpinned) |
| `POST /models` | exactly one of `{"add": id}`, `{"remove": id}`, `{"model": id, "effort": level}` (`""` = AUTO) |
| `POST /voice` | `{"voice": name}` — `""` clears the override |
| `POST /mute` | `{"muted": bool}` |
| `POST /avatar` | `{"slug": slug}` |

The fast path's default (2026-10-08). `POST /model {model}` is **the**
default the owner sees: it persists in `models.json`, beats
`JARVIS_ORCHESTRATOR` and is what every default-following fast-path thread
runs on (`GET /thread-models` names it as `providers.fast.default`). The env
model is the *config default*, used only while nothing is selected.
`GET /models` (and every `POST /model(s)` answer) carries `selected` (the
HUD's choice or `""`), `default` (the config default), `current` (what
answers now) and `default_source` (`"hud"` or `"config"`).

`{"remove": id}` unpins any roster model, the config default included (it
stays unpinned across restarts — `removed_default` in `models.json`), and
drops its effort pin. Two refusals, both **409** with a sentence the picker
shows verbatim: the last model on the roster, and any removal that would
leave the effective default unlisted ("choose another default first" —
the config default while nothing is selected, or the selection while the
config default is unpinned). Removing the selected model otherwise falls
back to the config default (the picker then says "Unpinned X; the default is
now the config default, Y."). Unknown id: 404 (`models.NotOnRoster` only — a
bug's `KeyError` is not a 404); ineligible `add`: 400 with the reason; an
`effort` beside `add` or `remove`: 400. Every change publishes one `model`
event; a refusal publishes none. `describe()` is built from one roster read,
and models.json is written atomically (temp file + `os.replace`, mode kept),
since the v1 face and the v2 daemon are separate processes.

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
- The port is read from `GET /status` → `workshop_port` (2026-10-08), never
  assumed: a hard-coded 8403 made the HUD suite's mock-served window load the
  owner's **live** daemon's preview for fixture project `p1`
  (`GET /p/p1/index.html -> 400` in the live log). Only a daemon too old to
  report one falls back to 8403.
- The preview origin carries **no** frame headers (it is framed by the HUD
  on purpose); the HUD and API listeners refuse to be framed (WP-E, below).

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
  *2026-10-09 (PR #20):* Codex's windows are also read without a turn,
  from the account's `account/rateLimits/read`. `GET /usage` and
  `GET`/`POST /thread-models` **start** a refresh — one short-lived
  app-server reading `model/list` and the quota — on a background thread
  when one is due, and answer at once from the last snapshot; a refresh
  that changed something publishes `codex_metadata` on `/events`, and the
  HUD re-reads `/usage` and `/thread-models`. At most one runs at a time,
  at most every 5 minutes, a failure waits the same 5 minutes, and a turn
  holding Codex's login lock makes it wait 30 s instead (`MetadataBusy`,
  decided before the CLI is probed). The whole app-server session has one
  10 s deadline, since it holds that lock. The two halves are independent:
  a failed `model/list` still lands the quota, and the reverse. A quota
  read is merged like a turn's notification: a null or empty report never
  clears a window, and a limit id learned from a turn is kept. `/usage`
  reads only `no_new_work` and `allowances` from `routing.json`, never the
  routing table, so a table at odds with the catalog cannot blank it.

## Schedules

- `GET /schedules` · `POST /schedules {project_id, brief, cron?: str,
  every_s?: int, enabled: true}` · `PATCH /schedules/{id}` · `DELETE
  /schedules/{id}` · `POST /schedules/{id}/run-now`. A schedule record:
  `{id, project_id, brief, cron, every_s, enabled, last_run_at,
  last_task_id, next_run_at, created, describe}`. `describe` is the
  scheduler's plain-English reading, computed on the way out and not
  stored. Cron is 5-field, evaluated in `America/Chicago`.

## Speech

- `POST /stt` body `audio/*` (WAV or webm) → `{"text"}` via `voice.stt`,
  which tries `STT_MODEL` then `STT_FALLBACK_MODELS`. When every model fails
  it is a **502** whose `error` names each model and its HTTP status (never a
  response body), and the HUD shows that sentence.
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

**Except a provider's `approval_requested` / `approval_resolved`** (2026-10-08):
those only mean "the gate was consulted" and fire for every tool call, so they
never reach `/events`; the thread log keeps them as `gate_requested` /
`gate_resolved` rows. The `approval_*` records on `/events` are the broker's
alone, always carry a `code`, and `approval_resolved` also follows a timeout or
shutdown.

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
  under the rules, it was opened with. The same route also takes a rename
  `{title}` and a model change `{model?, effort?}` (below); exactly one of
  the three shapes per request, or 400.
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
- **Only an explicit model pins** (A4 amendment, 2026-10-07). A default
  thread (`model: null`) with an `effort` keeps following the default model;
  the effort is stored on its own and clamped at the start of every turn to
  whatever the default supports then (down the ladder first, then up; as
  asked when the catalog cannot describe the model), and dropped altogether
  for a default with no reasoning control. The stored value is never
  rewritten by the clamp, so a default that moves back gets it again.
  `effective_effort` on `thread_updated` is the clamped value.
- `POST /threads` for `role: "chat"` takes `provider` (default `fast`) and
  `brief: {model?, effort?}`. The model is checked **before** the record is
  created: 400 with the reason for a model that is not on the roster (or
  cannot call tools), not one of that CLI's models, or an effort off its
  ladder (the default model's ladder when no model is given; the thread
  still opens on `model: null`). A Claude or Codex chat thread opens in the project root under the
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
- `PATCH /threads/{id}` `{model?, effort?}` → the thread record. This is one
  of the route's three request shapes, and they are mutually exclusive: a
  rename `{title}`, a move `{project_id}`, or a model change `{model?,
  effort?}`. Fields from two shapes in one body are a 400 ("rename, move and
  model change are separate requests"), and so is a body with none. `model: null` returns to the
  default and resets the effort — unpinning is a choice of model like any
  other, so it clears a stored effort too (`{model: null, effort}` keeps
  one); a new model resets the effort unless one is given with it; an effort
  alone on a default thread stores the effort and leaves `model: null`, so
  the thread keeps following the default. That effort is checked against the
  default model of the moment (what the chip offers): 400 for a level it
  lacks, or when it has no reasoning control. A model already pinned stays usable after it leaves
  the roster (shown "(not on roster)"). 400 with the reason on any refusal;
  409 for a task's thread, a provider that cannot switch, or an archived
  thread or one in an archived project ("restore the thread before changing
  its model"). There is no
  `provider` field: a session cannot change provider.
- Every change writes a `model_set` record to the thread's log (`data.text`,
  e.g. `model → moonshotai/kimi-k3 · high (from the next message)`;
  `effort → high (default model: X)` for an effort-only change on a default
  thread (`effort → default · high (…)` when it is cleared); or
  `(follows the default)` when a default thread's turn starts on a new global
  model, naming the re-clamped effort) and publishes it, then `thread_updated {…thread, thread_id,
  changed: ["model", "effort"], effective_model, effective_effort}`. A
  rename's `thread_updated` carries `changed: ["title"]` and no record; the
  HUD patches in place when the record is there and refetches otherwise. `GET /threads/{id}/transcript` returns `model_set`
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
  *2026-10-08:* each provider also carries `default_source` (`"hud"`, else
  `"config"` for OpenRouter, `"built-in"` for Claude, `"routing"` for
  Codex), `settable` (true for Claude and Codex), `hud_default` (`{model,
  effort}` as stored, `effort: null` = the model's own default; or null) and
  `builtin` (`{model, effort}`, what a reset returns to; null for
  OpenRouter); every model row carries `default_effort`.
  *2026-10-09 (PR #20):* the Codex list is **dynamic**: the signed-in
  account's own `model/list` catalog (hidden rows dropped; ids of slug
  characters only; names one cleaned line of at most 64 characters; only
  Codex's effort words; `vision` only where `inputModalities` lists
  `image`), installed whole — never a union — by a background refresh (see
  `GET /usage`), saved atomically to `config.CODEX_CATALOG_PATH`
  (`~/.local/share/jarvis/codex-models.json`) and loaded when `daemon2`
  starts, so a restart and a Discord-only daemon use it too. The file is
  protected state (no agent write tool may touch it) and is read only if it
  is a regular file, not through a symlink, and within its cap. Until then,
  or when the file is missing, corrupt or anything else (it is parsed with
  the same caps as the live response), the list is the built-in
  `router.CODEX_FALLBACK`,
  whose ladders match what the account advertised on 2026-10-08 (astra,
  both Sols and Terra reach `ultra`). `default_effort` is A4's — `high`,
  clamped to the model's ladder — never the default Codex advertises.
  `ultra` is a Codex level only: OpenRouter and Claude refuse it, the HUD
  never offers it for them, and a record that holds it anyway runs at the
  model's default. A HUD Codex default naming a model the catalog no longer
  lists runs routing's default for the turn and says so in `note`; the
  stored choice is never rewritten and applies again when the model returns.
  A pinned thread keeps its model; an effort its ladder no longer has is
  clamped down to the nearest one it has.
- `POST /thread-models` `{"provider": "claude" | "codex", "model": id,
  "effort"?: level}` → the new `GET /thread-models` payload. Sets the
  default every **default** Claude or Codex chat thread runs on from its
  next turn (pinned threads are untouched); `model: ""` is "Reset to
  built-in default" (Opus 5.5 at high for Claude, routing's orchestrator
  default for Codex). `effort` absent, null or `""` is the model's own
  default effort. Exact keys (A6): an unknown key is a 400 naming it;
  `provider` and `model` are required. Refusals are 400 with the reason: a
  model `router.CLI_MODELS` does not name for that provider, an effort off
  its ladder or on a model with none, an effort with a reset, `provider:
  "fast"` (that default is `POST /model`'s), an unknown provider.
  **Which wins:** a HUD default beats the built-in Claude default and,
  for Codex **chat threads only**, `routing.json`'s orchestrator default;
  routing.json is never written and still decides every task role. Stored
  atomically in `~/.config/jarvis/provider_defaults.json`
  (`config.PROVIDER_DEFAULTS_PATH`); like models.json and routing.json it is
  refused to every agent write tool — v2's permit (`protected_paths`) and v1's
  `write_file`/`edit_file` (`files._protected_state`, the same set); a
  stored model Jarvis no longer knows is ignored and named in `note`. A
  success publishes one `model` event `{provider, default, default_effort,
  default_source}` (the HUD re-reads `/thread-models` on it); a refusal
  publishes none.
- No tool reaches any of this. The agent cannot change its own model or
  provider, nor a provider's default.

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

`POST /projects` and Discord's `/project new` (B2) share one create path,
`projects.create_project(daemon, name, root, **values)`, so both number and
refuse identically and both publish `project_created`. The route itself is
unchanged; it never makes a folder. A folder made from Discord arrives as an
ordinary approval card (`tool: "project_folder"`, `args {action: "create" |
"adopt", path, name[, entries, git]}`, `allowlistable: false`); approval
payloads gain `discord_channel_id` (where Discord was asked, or `null`).

- `POST /projects` and `PATCH /projects/{id}`: `root` must be an existing
  directory (400). A `routing.models` override is held to `CLI_MODELS` on
  write, the rule `routing.json` and `/route` follow: a known role,
  `claude`/`codex` only, a model Jarvis knows for that CLI (or `roster`), and
  an effort it offers; anything else is 400 naming the entry
  (`routing.models.<role>.<provider>: …`). PATCH checks only when the body
  carries `routing`, so a project saved before the check stays editable. A root change pins every task that already has a
  worktree to the old root first (`Task.root`), so it finishes, commits and
  is removed there; existing threads keep their frozen `cwd`; new threads
  and tasks use the new root. A PATCH of an archived project is 409. They
  publish `project_created {…project}` and `project_updated {project_id,
  changed: [fields], project}`; a PATCH that changes nothing publishes
  nothing.
- `PATCH /threads/{id}` `{"title"}` → the thread record (title numbered).
  A rename is its own request: `{title}` with `project_id`, `model` or
  `effort` is 400, and the `{project_id}` move is unchanged (see the model
  section above for the third shape, `{model?, effort?}`). Publishes `thread_updated
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

## Additions 2026-10-07 (Discord slash core — S1)

- `GET /discord` → `{"connected": bool, "commands": {"state", "count",
  "synced_at", "error"}}`. `state` is `ok` (the registered commands match
  the code), `failed` (with `error`: an exception class and Discord's own
  message, the bot token redacted), `pending` (the bundle exists but the
  surface has not synced yet), or `off` (no `jarvis auth discord`).
  `synced_at` is epoch seconds or null. States, counts and times only: the
  route never carries a token, an interaction token or a request path. PR A
  extends it with the reporter, guild and permissions; the HUD light reads
  it then.
- Thread transcripts: a user turn sent with a skill (`/skill` on Discord)
  is shown as `/skill <name> <text>`. The log row is
  `{"kind": "user", "data": {"text", "skill"}}`; `skill` is absent on
  ordinary turns.

## Additions 2026-10-07 (Discord update poster — PR A)

- `GET /discord` gains `"reporter"`: `null` when no Discord surface is
  running, else `{"state": "ok" | "degraded" | "down", "reason": str,
  "counters": {name: int}, "dropped": int, "last_error": null | {"op": str,
  "status": int | null, "code": int | null, "at": epoch seconds}}`.
  `degraded` means the poster is working around something — a channel
  Discord calls broken (10003/50001/50013, so attention updates go to the
  owner's DM) or a recent transient failure — and `reason` says which in
  one sentence. `down` means the circuit breaker is open. `counters` are
  integers only (`posts`, `pings`, `edits`, `threads`, `dm`, `errors`,
  `reconciles`, `breaker_trips`, …); `dropped` is the bus's evicted-event
  count for the poster. The route copies these fields one by one: no URL,
  message body or token ever passes.
- SSE `discord_status` → `{"kind": "discord_status", "data": <the reporter
  object above>}`, published whenever `state` or `reason` changes. The HUD
  refetches `GET /discord` on it and redraws the Discord light.
- A Discord surface that failed to start answers `{"connected": false,
  "commands": {"state": "failed", "count": 0, "synced_at": null, "error":
  "<ExceptionClass>"}, "reporter": {"state": "down", "reason": "the Discord
  surface did not start (<ExceptionClass>)", …}}` — the class name only,
  never the exception's message. The light is red.

## Additions 2026-10-07 (Discord server and project channels — B1)

- `GET /discord` gains three fields, copied one by one:
  `"guild": {"configured": bool, "id": str | null}` (is `jarvis auth
  discord-guild` done), `"linker": null | {"state": "ok" | "degraded" |
  "unconfigured", "reason": str, "pending_renames": int,
  "awaiting_approval": int}` (`null` when no Discord surface is running) and
  `"permissions": null | {"missing": [name], "excess": [name],
  "administrator": bool, "checked_at": epoch seconds | null}` — the bot's
  server-wide permissions by name, from the linker's own periodic check.
  The HUD light goes amber on a missing permission, on Administrator, and
  while a rename is pending.
- `Project` gains `discord_channel_origin: "created" | "linked" | null`
  (display only).
- `POST /projects` and `PATCH /projects/{id}` refuse `discord_channel_id`
  (and `discord_channel_origin`) with 400 `"link a channel from the project
  dialog"`. `project_updated` gains `"by": "owner" | "api"` (the HUD's own
  listener and Origin, or anything else) and `"previous": {field: old
  value}` for each changed field. `project_deleted` gains
  `"discord_channel_id"`.
- `GET /projects/{id}/discord[?refresh=1]` → `{"channel_id": str | null,
  "origin": "created" | "linked" | null, "name": str | null, "category":
  "Jarvis" | "Jarvis Archive" | "Jarvis Archive N" | "another category" |
  null, "state": …, "missing": [name], "checked_at": epoch seconds | null,
  "rename_pending": bool}`. `state` is one of `linked_ok`, `unlinked`,
  `not_found`, `no_access`, `wrong_guild`, `missing_permissions`,
  `folder_missing`, `unconfigured`, `unreachable` (also when no Discord
  surface runs but the guild file exists). Cached 60 s; each Discord read
  is held to 5 s.
- `POST /projects/{id}/discord` **owner-only** (the HUD's listener and
  Origin, else 403) with `{"action": "create"}`, `{"action": "link",
  "channel_id": "<snowflake>"}` or `{"action": "unlink"}` → the view above.
  It publishes `project_updated` with `changed: ["discord_channel_id"]`,
  `by: "owner"`. Refusals: 400 for a bad id, an unseen or unknown channel,
  another server, not a text channel, a missing permission there, #ungrouped
  or a Jarvis category; 409 for a channel already linked elsewhere, an
  archived project, a missing folder, the Inbox (only setup changes
  #ungrouped), an already-linked create, no guild file, or no Discord
  surface; 502/503 when Discord fails or rate-limits. Unlink keeps the
  channel.
- `POST /discord/backfill` **owner-only**, body `{}` → `{"created": int,
  "results": [{"project_id", "name", "channel_id", "status": "created" |
  "failed", "error"?}], "skipped": [{"project_id", "name", "reason"}]}` —
  a channel for every live, unlinked project whose folder exists (never the
  Inbox), one create a second.
- Review fixes (PR #8): `GET /discord`'s `linker` gains `"pending_moves":
  int` (archive/restore moves waiting out a 429 or an outage), and
  `project_restored` gains `"previous_name"` (the name before PR #4's
  renumbering). A `create` adopts an unlinked Jarvis channel whose topic
  ends with `· <project id>` instead of making a second one.

## Additions 2026-10-07 (every chat is a Discord thread — PR C)

- SSE **`user_message`** `{kind, thread_id, project_id, turn_id, at, data:
  {text, typed, via, origin, images, attachments, spoken, discord_message_id,
  discord_channel_id}}` — the owner's message, published before its turn's
  first event. `text` is what the provider got (files inlined), `typed` the
  owner's own words, `via` one of `hud`, `discord`, `dm`, `system`, `peer`,
  `images` a count and `attachments` names only. The `user` record in
  `/threads/{id}/log` holds the same `data`. The HUD draws only `discord`
  and `dm` ones (its own message is already on screen).
- `POST /threads/{id}/send` accepts `"spoken": bool` (dictation); anything
  else is 400.
- SSE **`question_answered`** `{kind, thread_id, project_id, data: {req_id}}`
  after `POST /threads/{id}/answer` (or a Discord answer) reaches the
  provider, so every surface can stop showing the question as open.
- `GET /threads` (and every thread record) carries `"surface": null | "dm" |
  "dm:retired" | "discord:<id>"`, and, when set, `"discord": {"kind":
  "thread" | "dm", "channel": str | null, "name": str, "url":
  "https://discord.com/channels/<guild>/<id>" | null}`. `surface` is never
  accepted from the HUD; `thread_updated` with `changed: ["surface"]` says
  it moved.
- `GET /threads/{id}/transcript`: a user message typed in Discord carries
  `"via": "discord" | "dm"`.
- `GET /discord` gains `"mirror": null | {"state": "ok" | "degraded" |
  "down", "reason": str, "queued": int, "last_error": null | {op, status,
  code, at}}`; the light goes amber when it is not ok.

## Additions 2026-10-08 (steering a running turn)

A message sent while a turn runs is never a dead end. `POST
/threads/{id}/send` (the HUD) and a message typed in the chat's Discord
thread both go through `Daemon.deliver`, and so does the escape hatch's
result; `Daemon.send` (the task runner) still refuses with 409 while a turn
runs, because the runner retries on exactly that.

- `POST /threads/{id}/send` → **202** `{"status", "turn_id", ...}`:
  - `"started"` — no turn was running; `turn_id` is the new turn (as before).
  - `"steered"` — handed to the running turn, which takes it at its next safe
    point; `turn_id` is that turn, plus `"message_id"` and `"mode"`:
    `"native"` (the provider steers: Claude Code's own mid-turn message,
    Codex's `turn/steer`, the fast path's next step boundary) or
    `"interrupt"` (a provider that cannot steer had its turn interrupted for
    this message, which runs next with a note saying why).
  - `"queued"` — waits to run as its own turn when this one ends: the
    provider could not take it now (a turn waiting on an approval or a
    question, a turn just ending, a refused steer), or messages already wait
    and nothing overtakes them. `"message_id"`, `"position"` (1-based) and
    `"queued_turn_id"` (the id its turn will run under).
  - `"dropped"` (`"reason": "stopped"`, `"message_id"`) — the owner pressed
    Stop while the provider still held this steer, and it was not taken:
    stop means stop, so it is not sent and never starts a turn. It is a
    `queued_dropped` record and a `queue_cleared`, as Stop's other drops are;
    the HUD marks it *not sent* and puts its words back.
  - **409** only for a full queue (three waiting: `"Three messages are already
    waiting on this turn; send this one again once I've answered."`) or a
    task's thread with a turn running (task turns are the runner's).
  A steer has a send's authority and no more: it never answers or resolves an
  approval or a question the turn is waiting on — while one is open the
  message queues instead. An older daemon answered 409 for every send during
  a turn and sent no `status`.
- SSE **`user_message`** for such a message carries `data.message_id` and
  `data.steer: true` (handed to the running turn; `turn_id` is that turn) or
  `data.queued: true` (waiting; `turn_id` is its own future turn). A steer is
  logged and published *before* the provider has it, so nothing it causes
  comes ahead of it. The `user` log record holds the same `data`.
- SSE **`steer_queued`** `{thread_id, project_id, turn_id, data: {message_id,
  turn_id, interrupting}}` — a message published as a steer waits instead,
  under the same `message_id` and with no second `user` record: the provider
  refused it (`interrupting: true` for the interrupt fallback), or the turn
  ended before it was delivered (the fast path's final answer came first).
  `turn_id` is the turn it will run as. Also a log record.
- SSE **`queued_started`** `{thread_id, project_id, turn_id, data:
  {message_id, turn_id}}` — a waiting message has become its turn (logged as
  a `queued_started` record; the turn's own events follow under `turn_id`).
- SSE **`turn_finished`** carries `"next": n` when n owner messages run as
  the thread's next turns at once (queued, or steers the turn never took;
  absent when none, or after a Stop). The HUD stays on the thread — busy,
  Stop shown, no follow-up mic window — and the sidebar keeps it `working`
  across the gap.
- SSE **`queue_cleared`** `{thread_id, project_id, data: {reason, messages:
  [{message_id, typed, via, attachments, images}]}}` — messages that will not
  run: the owner pressed Stop (`reason: "stopped"`; stop means stop, and the
  HUD puts the words it sent back in that thread's box — held until the
  thread is opened if another is on screen), the thread was closed, could
  not resume, or Jarvis stopped. Each is also a `queued_dropped` log record.
- `POST /threads/{id}/interrupt` (the owner's Stop) now also drops what waits
  behind the turn (`queue_cleared`). Unchanged otherwise.
- Every thread record (`GET /threads`, `POST /threads`, `thread_updated`)
  carries live, never-stored `"running": bool` and `"queued": int`. The HUD
  reconciles its busy state against `running` on every SSE reconnect and
  every 15 s while busy, so a missed `turn_finished` cannot wedge it.
- `GET /threads/{id}/transcript`: a user message that reached a running turn
  carries `"message_id"` and, when it says something, `"mark"`: `"steering"`,
  `"queued"` (still waiting, in the live queue) or `"not sent"` (dropped — or
  left waiting by a crash or restart, after which nothing waits any more).

## Additions 2026-10-08 (sidebar status dots)

- `GET /activity` → `{"threads": {<id>: status}, "tasks": {<id>: status}}`,
  every thread and task that is **not** idle. `status` is one of `working`
  (a turn is running; a task in clarifying, planned, running or verifying),
  `needs_input` (an approval or provider question is open on the thread, or
  on any of a task's threads; a task that is blocked, or clarifying with a
  blocking question — outranks working), `unread` (a chat thread's last
  turn, or a task that is done, not yet opened), `failed` (the same for a
  turn that ended `stop: "error"`, or a failed task) or `idle`. An
  interrupted turn, a cancelled task and a task in intake (waiting for the
  owner to press Start) are idle. A task's own threads are never unread or
  failed: the task row carries the outcome. A provider question closes with
  its turn; a broker approval does not (the escape hatch raises one after
  `turn_finished`, and an interrupted turn can end with its permit still
  blocked), so it counts until its `approval_resolved` — published on a
  resolve, a timeout and a shutdown alike.
- `POST /threads/{id}/seen` and `POST /tasks/{id}/seen`, body `{}` →
  `{"status": <its status now>}`. The owner opened it: unread and failed
  become idle. The HUD calls them on opening a thread (in a chat pane) or
  a task (in a task pane), and when one finishes while open in a visible
  window. Since 2026-10-09 "open" means shown in **any** pane the window
  draws, not only the focused one (a split centre, design §18) — with
  several chat panes, every drawn chat pane's thread. The HUD
  draws the answer at once (the `activity` record may not reach a window
  whose stream is reconnecting) unless a record for that row, or a snapshot
  sent after the call, has arrived since. A sidecar that cannot be written
  is logged, never a 500, and never recreates a deleted thread or task.
- SSE **`activity`** `{kind: "activity", data: {of: "thread" | "task", id,
  project_id, status}}`, published whenever a status changes, after the
  record that caused it. A task is followed from `task_created`; a new
  task's idle (intake) is not published, since no window holds a status for
  a task that did not exist. `project_id` is
  where the thread or task is when the record goes out, so a moved thread's
  records name its new project. The ids are inside `data` on purpose: a
  stream filtered by `?thread=` or `?project=` never carries it. Since a
  status is published on change only, the HUD replays every record that
  arrives while a `GET /activity` is in flight over that snapshot when it
  lands; applied as-is, the older snapshot would undo them for good.
- Running turns and open asks are in memory (a restart ends both); what was
  last finished and what was seen are in `threads/<id>/activity.json` and
  `tasks/<id>/activity.json`. No sidecar is idle, so threads and tasks from
  before this shipped start read.

## Additions 2026-10-09 (PR #20 — routing that drifts from the Codex catalog)

The Codex model table is the account's catalog (see `GET /thread-models`),
so a model or effort saved in `routing.json` or a project's
`routing.models` can stop being one the table offers. Nothing that reads
the table fails because of it, and nothing saved is rewritten:

- `GET /route` gains `"notes": [str]`: every saved setting that does not
  run as written right now, in words — a model the catalog lacks (that
  entry runs the role's default until the model returns), an effort its
  model no longer offers (clamped down to the nearest one it has, `ultra`
  included), or a malformed part of the file (that part's default). A
  **built-in** routing default the catalog lacks is noted too: it still
  runs — there is nothing to fall back to — so a turn that fails on it is
  explained. The same notes are logged once per process. `table` and
  `resolved_models` show what actually runs. The HUD's Settings shows the
  notes as text.
- `PATCH /projects/{id}` with `routing` judges only the `routing.models`
  entries that differ from the stored project: an unchanged entry whose
  model the catalog has since dropped round-trips (it runs as the default
  until the model returns), and a changed one is held to the table.
- `POST /route` builds the table it writes from the file's well-formed
  parts, not from what runs, so it saves over a stale or corrupt table, and
  an entry it was not asked about is kept verbatim. A **new** choice is
  still held to the table as it is now (400 naming the model or effort).
  `roster/<effort>` takes only an effort some model of that CLI offers:
  `roster/ultra` on Claude is a 400.
- `GET /usage` never reads the routing table (only `no_new_work` and
  `allowances`, each degrading to its default).
- `/events` carries `{"kind": "codex_metadata"}` when a background
  refresh of Codex's catalog or quota landed; the HUD re-reads `/usage`
  and `/thread-models`.

## Additions 2026-10-09 (terminal backend — WP-C)

The owner's terminals (HUD plan §2.4, decisions W-2, W-3, W-5; code in
`jarvis/v2/terminals.py` and `jarvis/v2/ws.py`). Each is the owner's login
shell on a real PTY. **Every route is owner-only**: it answers only on the
HUD's listener (`FACE_PORT`) and only to a request whose `Origin` is present
and equals `http://<Host>`, with `Host` exactly `127.0.0.1:<port>` or
`localhost:<port>`. Anything else is 403: the API listener (8405, every
tool's client), a missing Origin, `null`, the workshop origin, any other
site. The preview listener (8403) serves only `/p/...` and answers 404. No
tool, MCP tool, fast-path tool or Discord verb names these routes. A
program running as the owner can still forge an Origin and fetch a ticket:
that is why every attach is announced (`terminal_attached`, below).

- `GET /terminals` → `[row]`, where `row` is `{id, title, folder,
  project_id, created, cols, rows, shown, exited, exit_code, readable,
  busy, integration, integrated, marked}`. `id` is eight hex characters. `title` is
  `<shell> · <project name | ~>`. `shown`: a window is attached.
  `exited`/`exit_code`: the shell ended (killed by signal n reads 128+n).
  `readable`: the owner's "Jarvis can read" switch (W-2), **on for every new
  terminal**; nothing reads it yet (WP-F's `terminal_read` will refuse a
  terminal with it off). `busy`: the PTY's foreground process is not the
  shell — the HUD asks before closing then. `integration`: `"bash"`
  (startup file, `sudo -k`, prompt and command marks), `"posix"` (an
  `sh`-family shell: startup file, `sudo -k`, prompt marks only) or
  `"none"` (zsh, fish, any shell that reads no `$ENV`: **no startup file —
  no `sudo -k` and no marks**; the HUD should say so). `integration` is
  what was **configured**; `marked` (2026-10-09, WP-D) is what **took**: true
  once the first signed prompt mark (A) arrived, so the startup file ran —
  a profile that `exec`s another shell configures `"bash"` and never marks,
  so it has no `sudo -k` either, and the HUD says so. `integrated`: the
  shell has marked a command (bash only).
- `POST /terminals` `{in, cols?, rows?}` → 201 `row`. `in` is `"home"`,
  `{"thread": id}` (that thread's own folder, `cwd`), `{"project": id}` (its
  root) or `{"task": id}` (its worktree if it exists, else the root it was
  started under). **The folder is resolved from ids, never from a path
  string.** `cols` 2–1000 (default 80), `rows` 1–500 (default 24); anything
  else, an unknown key or a missing `in` is 400; an unknown id 404; a folder
  that no longer exists 409. At most **6** terminals (exited ones count
  until closed): the seventh is 409 with a sentence. A bad
  `JARVIS_TERMINAL_SHELL` (relative, resolving under `/mnt/`, not
  executable) is 409. The shell's realpath is what runs, under the name it
  was given as its argv[0] (`env --argv0`), so `/usr/bin/rbash`, a link to
  bash, still runs restricted; `title` uses that name. **The checked
  realpath is always what runs** (2026-10-09): where `env` has no
  `--argv0`, it runs under its own name, and a name whose basename differs
  from the program's (`rbash`, `sh` for bash, a busybox applet) is 409 —
  running it under its own name would run something the owner did not
  name. The startup file must fit 8 KiB (a pipe's capacity even under
  `pipe-user-pages-soft`), is written without blocking, and a short write
  is a 409, never a cut file or a wedged route.
- `PATCH /terminals/{id}` `{readable: bool}` → `row`. Exactly that key.
- `POST /terminals/{id}/ticket` `{}` → `{ticket, expires_in: 30}`. **Single
  use, 30 seconds, valid for this terminal only**; it is spent the moment it
  is presented, whatever the answer. Fetch one right before each attach.
- `GET /terminals/{id}/attach?ticket=…` — the WebSocket. Build the URL from
  `location.host` (never a fixed port). The checks run in this order: the
  owner-only gate (a refused Origin does not spend the ticket), a
  well-formed version-13 upgrade (else 400), the ticket (else 403). Then
  `HTTP/1.1 101`. No extension (no compression) and no subprotocol is
  agreed. Client frames must be masked; a message is at most 64 KiB (send
  pastes in 16 KiB chunks); control frames are at most 125 bytes and never
  fragmented; the server pings every 20 s and drops a socket that has said
  nothing for 60 s; a socket more than 4 MiB behind on output is dropped
  (reattach to replay). Close codes: 1002 protocol error (also a close frame
  with a code that may not be sent — 1005, 1006, 1015, reserved or out of
  range), 1007 text or a close reason not UTF-8, 1009 too big, 1008
  refused, 1001 Jarvis stopped. A close is echoed with its code (an empty
  one with an empty one), and nothing is ever sent after the server's own
  close.
  - **Binary frames are bytes, both ways**: keystrokes and pastes in, PTY
    output out. Input goes through a queue (256 KiB) that a writer thread
    of the terminal's own drains, so a program that does not read its input
    never stalls the socket: resize, takeover answers and pongs still land.
  - **A dropped frame latches the socket's input.** When the queue is full
    the frame is dropped and the window gets `{"type": "input_dropped",
    "bytes": n, "latched": true, "reason"}`. From then on **every** input
    frame on that socket is refused the same way — a frame that is exactly
    Ctrl-C (`\x03`) excepted — so a paste reaches the program as a prefix,
    never a prefix with a later chunk spliced on (accepting the next frame
    once room freed is what cut a `cat > file` paste mid-line). The window,
    on the first `input_dropped`: **stop sending the rest of the paste**, say
    what happened, and send `{"type": "input_resume"}` only once the owner
    has acknowledged it (the HUD's "Resume typing"; WP-D below). The answer
    is `{"type": "input_resumed"}` once the
    queue has drained, else `{"type": "input_resume_refused", "reason"}`
    (send it again shortly). A program that never reads never drains, and
    **Ctrl-C is not a way out of that**: the `^C` byte waits behind the same
    full tty buffer as everything else, so the program never sees it. The
    ways out are a reattach (a new socket starts unlatched) or closing the
    terminal. **A paste must never continue onto a new socket**: whatever
    of it was not sent when its socket closed is dropped, and the owner is
    told, so a reconnect can never splice the tail of a paste onto a prefix
    the program already took. A Ctrl-C frame throws away whatever input is
    still queued and the paste chunk being written, keeping one Ctrl-C that
    was already waiting, so two quick ones are never collapsed into one. The
    latch is per socket: a reattached window starts unlatched.
  - **Text frames are JSON control.** Server → window, in order on attach:
    `{"type": "attached", "terminal": row, "replay": n}`, then `n` bytes of
    replay as binary frames (the last 1 MiB of output, starting just after
    a newline once anything has been dropped), then `{"type": "replayed"}` —
    send a `resize` then, so full-screen programs redraw — and
    `{"type": "exit", "code"}` if the shell has already ended. Later:
    `{"type": "marked"}` once, when the startup file's first signed prompt
    mark arrives (the row's `marked` turns true; `attached` carries it once
    it is);
    `{"type": "exit", "code"}` when the shell exits (the socket stays open;
    offer Restart and Close); `{"type": "exit", "code", "reason": "closed"}`
    then a close when the terminal is closed; `{"type": "exit", "code",
    "reason": "ended"}` then close 1001 when Jarvis stops (terminals do not
    survive a restart, W-3); `input_dropped`, `input_resumed` and
    `input_resume_refused` (above).
  - Window → server: `{"type": "resize", "cols", "rows"}` (same ranges;
    anything else is ignored), `{"type": "input_resume"}` (above) and the
    takeover answer below. Unknown messages are ignored.
  - **One window at a time.** Attaching to a terminal another window shows
    sends that window `{"type": "takeover_request", "id", "timeout_s": 20}`
    and the newcomer `{"type": "waiting", "timeout_s": 20}`. The holder
    answers `{"type": "takeover", "id", "allow": true | false}`. Allowed:
    the holder gets `{"type": "taken"}` and a close, and the newcomer
    attaches with the replay. Kept, or no answer in 20 s: the newcomer gets
    `{"type": "refused", "reason"}` and close 1008. A holder whose socket is
    gone is not asked. While one window is asking, a third is refused.
- `DELETE /terminals/{id}` → `{ok, id, exit_code}`. SIGHUP to every process
  in the terminal's session (background jobs included), SIGKILL after 3 s,
  then the PTY is released; the attached window gets the `closed` exit. Two
  DELETEs at once: one 200, the rest 404. The session's leader is matched on
  its start time, and a session once seen empty is never signalled again,
  so a pid the system has since reused is never hit.
- SSE **`terminal_attached`** `{kind: "terminal_attached", data:
  {terminal_id, at}}` — exactly those keys — whenever a socket attaches
  (a takeover included). It is the **only** terminal record on the bus,
  and it carries no output, command or ticket. A same-uid program can mint a
  ticket with a forged Origin and attach to a terminal no window shows, and
  over HTTP that cannot be told apart from the HUD; this makes it visible.
  WP-D shows a notice when the attach was not the window's own.

**Environment.** The shell starts from a clean login environment, not the
daemon's: `HOME`, `USER`, `LOGNAME`, `LANG`/`LC_*`, `TZ`, the WSL interop
and display variables, `TERM=xterm-256color`, `COLORTERM=truecolor`,
`SHELL`, and the daemon's `PATH` without any Python virtualenv. Never a name
`.env` defines, `OPENROUTER_API_KEY`, `HF_HUB_OFFLINE`, `VIRTUAL_ENV`,
`PYTHONPATH`, `BASH_ENV`, `PROMPT_COMMAND`, or anything `JARVIS_*`,
`ANTHROPIC_*`, `CLAUDE_*`, `CODEX_*`, `OPENAI_*`. SIGHUP, SIGINT, SIGQUIT,
SIGTERM and SIGTSTP start at their defaults (`env --default-signal`), even
if the daemon inherited them ignored.

**The startup files.** bash gets `jarvis/v2/terminal_rc.bash` as its
`--rcfile`; an `sh`-family shell (sh, dash, ash, ksh, mksh, …, and bash
called `sh`) starts interactive with `jarvis/v2/terminal_rc.sh` as `$ENV`;
any other shell runs as a plain login shell with neither. **The file never
touches disk**: the daemon writes it, nonce and all, into a pipe, closes the
write end, and hands the shell the read end as `/dev/fd/<n>`. The shell's
first read drains it, before it runs anything, so whatever opens that pipe
later — a program started from `~/.profile` reading the shell's own
descriptor through `/proc`, say — reads nothing; bash also closes the
descriptor at once, and the POSIX file unsets `ENV`. Each file then reads the
login files (`/etc/profile`, then `~/.bash_profile` / `~/.bash_login` /
`~/.profile` for bash, `~/.profile` for sh). What is left is a race only a
program running as the owner *outside* the terminal could run: read the
pipe before the shell does. It would get the nonce and leave the shell with
no startup file at all — the same-uid limit stated at the top, not a new
one. Both files then set `alias sudo='sudo -k'` (W-5: sudo never caches here) and
emit OSC 133 marks carrying `jarvis=<per-terminal nonce>`: A (prompt start)
and B (prompt end), and in bash also C with `cmdline_url=<percent-encoded
command line>` (the command runs) and D with its exit status. The nonce is
kept in an unexported shell variable; PS1 names it (`${__jarvis_nonce}`,
expanded at each prompt) rather than holding it, and bash keeps PS1
unexported, so even dotfiles that export PS1 hand no child the nonce. A mark
without it is ignored. xterm ignores OSC 133; the HUD needs to do nothing
with the marks.

**Spans are advisory — WP-F, read this.** The daemon turns the marks into
per-command spans over the output ring (`Terminal.history()`: the bytes, the
spans, `spans_from`, `integrated`, `readable`, `prompt`). They tell a reader
which command line probably produced which output, and they are **not a
boundary**: a program running in the terminal can write any bytes between
two real marks; a nested shell, `sudo -i`, `ssh`, `python` or anything else
that runs commands of its own puts all of their output under the outer
command's span; and a line kept out of history (a leading space under
`ignorespace`) records only its first simple command. Bytes before
`spans_from` lost their spans to the 2000-span cap and are unattributed. So
**`terminal_read`'s text-pattern refusals (W-2) must apply to every read**,
whether or not the terminal is `integrated`; spans can add refusals, never
remove one.

**Output never leaves memory**: on the bus there are lifecycle ids only
(`terminal_attached`), never output; the HUD lists terminals with `GET
/terminals` and learns of an exit over the socket. Output is not in any log
(the daemon logs opened / attached / exited / closed with the folder as a
project name or `~`, and logs `/terminals` paths without their query, so
never a ticket), not in a thread log, not on Discord, not on disk.

## Additions 2026-10-09 (terminal panel and view — WP-D)

What the HUD does with the terminal contract above (code in
`hud/src/components/Terminal.tsx`, rules in `hud/src/lib/terminal.ts`):

- **One session per terminal per window.** It is made the first time the
  window draws the terminal (the open panel's tab, or a drawn pane), and
  then stays attached while hidden — its output keeps landing — until the
  terminal is closed, taken by another window, or ends with Jarvis. A
  hidden panel or a pane the layout does not draw attaches nothing, so a
  reload never asks another window for a terminal nobody here is looking at.
- **A terminal another window shows is never taken unasked.** A row listed
  `shown` that this tab has not shown (a sessionStorage list,
  `jarvis.hud.terminals.mine`; `taken`/`refused` clear an id) is drawn as
  "in another window · Show it here"; only that click attaches (and so asks
  the other window). A first attach reads a fresh `GET /terminals` before
  deciding. The list names its holder — the live page's per-load nonce, set
  to null by its `pagehide` — and a new page inherits it only when the holder
  is null and the page was loaded by a `reload`, so a duplicated or reopened
  tab's copy of it never skips "Show it here".
- **Every attach** fetches `POST /terminals/{id}/ticket` first, builds the
  socket URL from `location` (`ws:` or `wss:` + `location.host`, never a
  port), starts from a clean xterm on `attached` (an in-band RIS), and sends
  `resize` only after `replayed` (and again on every fit). **Nothing is sent
  from a new socket until its replay has been parsed**: xterm answers some
  output (DA, a cursor-position report, an OSC 10/11 colour, DECRQSS)
  through the input channel, and the ring's old queries must not be
  answered into the program as if typed. Output is written one chunk at a
  time and tagged with its socket, so an older socket's queued output is
  never parsed into a newer session; past 8 MiB queued (output faster than
  xterm draws) the backlog is dropped and the window reattaches, so the
  replay shows the latest output, and says it skipped ahead. An unexpected
  close reattaches
  with a fresh ticket (backoff 0.3–10 s, then "lost" with Reconnect); a
  ticket answered 404 reads "ended".
- **Pastes** go out in 16 KiB chunks paced 8 ms apart from a queue that
  belongs to **one socket**. `input_dropped` stops the rest at once (the
  frames already on their way are refused too and counted into the same
  notice — never undoing a resume); typing then waits, shown, until the
  owner presses **Resume typing** (`input_resume`, retried every second
  while `input_resume_refused`, with **Reattach** and closing offered as the
  way out); a lone Ctrl-C still goes. While typing is paused the notice
  cannot be dismissed — it holds the only way to resume — and a held key
  brings it back. A Ctrl-C ends a paste still queued.
  **A socket that closes takes the rest of its paste with it**: nothing of
  it is sent on the next socket, and the owner is told. **A paste is inert
  as a control stream**: ESC and C1 are stripped before it is bracketed, so
  a pasted `ESC[201~` cannot end bracketed paste early. Keys or a paste
  dropped while the terminal connects (or is not running here) are said.
- **Under an authorization card nothing reaches the shell** (keys, pastes,
  a paste still going out); every key bubbles past the terminal, so Escape
  denies; output keeps drawing; every terminal button is disabled.
- **Takeover**: a `takeover_request` is a window-wide prompt (Let it /
  Keep it, the countdown; disabled under a card, so unanswered is kept).
  The newcomer shows "Asking the window that shows this terminal… Ns",
  then `refused` (Ask again) or the replay; `taken` offers Take it back.
- **`terminal_attached`**: the window notes each socket it opens and
  matches the events against them (40 s); one it cannot match is a quiet,
  dismissible notice naming the terminal and the time. Only
  `{terminal_id, at}` is read; a malformed id is ignored.
- **`marked`** (above): a configured shell (`bash`, `posix`) that has not
  marked a prompt 4 s after attaching reads "integration inactive"; `none`
  reads "no integration" at once. Both say sudo may cache there.
- **`readable`**: each terminal's bar carries the "Jarvis can read" switch,
  `PATCH /terminals/{id}` with exactly `{readable}`.
- **`busy`**: × reads a `GET /terminals` sent after the click (never one
  already on its way) and, when the foreground process is not the shell,
  asks before `DELETE`; if the listing fails it asks anyway, saying it could
  not check.
- **Output is hostile bytes**: an OSC 0/2 title is text in the pane header
  and the terminal's bar, one line, capped at 80 — never `document.title`;
  links (plain or OSC 8) open only for `http`/`https`, only on Ctrl+click,
  in a new window with `noopener`; there is no clipboard addon (no OSC 52)
  and window reports stay off. "Open in Preview" for loopback links is
  WP-E's (below).

## Additions 2026-10-10 (Preview and dev servers — WP-E)

Plan `docs/plans/2026-10-09-hud-workspace-plan.md` §2.5 items 1–4; decisions
W-4 (that file wins).

**No HUD or API response may be framed.** Every response on the HUD
listener (`FACE_PORT`, 8402) and the API listener (`DAEMON_PORT`, 8405)
carries

    Content-Security-Policy: frame-ancestors 'none'
    X-Frame-Options: DENY

— the HUD page and its assets, JSON, the SSE stream, every error and
refusal (403 Host/Origin, 400, 404, 409, 413), http.server's own refusals of
a malformed request (400, 414, 431, 501, 505 — answered as HTTP/1.0, never
as a header-less HTTP/0.9 body), and the terminal socket's hand-written
`101` and its 400/403 refusals. They are added in one place, the daemon
handler's `end_headers`; the `101` asks `hud_api.frame_headers` for them.
There is exactly **one** `Content-Security-Policy` header per response,
built by `hud_api.content_security_policy(*extra)`: a response's own
directives (`/avatar.svg`'s `default-src 'none'; style-src 'unsafe-inline'`)
ride in the same header, and none of them can change `frame-ancestors`. The
editor's full policy (ED-4, `'wasm-unsafe-eval'`) extends that function. The
**preview listener (8403) carries neither header**.

**`GET /status` gains three fields:**
- `hud_port` — the HUD listener's bound port;
- `api_port` — the API listener's bound port;
- `frame_hardened: true` — set by the handler class that adds the headers
  (`false` before the daemon has started). A HUD whose `/status` lacks it
  (an older daemon) never offers "keep its own origin".

**The Preview pane (`lib/preview.ts`, `PreviewTab.tsx`):**
- **Refused ports**: the HUD's (`hud_port`, 8402), the API's (`api_port`,
  8405) — the defaults are refused whatever `/status` says, since they are
  the owner's live daemon — and the window's own port; anything not loopback
  `http(s)`. The workshop (`workshop_port`) stays allowed. Nothing is
  restored or requested until `/status` has answered (the API port is only
  known then); five failed `/status` reads judge with the defaults and never
  keep an origin.
- **The sandbox** is exactly `allow-scripts allow-forms`; with the pane's
  "Keep its origin" switch on, exactly `allow-scripts allow-forms
  allow-same-origin`. Never `allow-top-navigation`, `allow-popups`,
  `allow-popups-to-escape-sandbox`, `allow-downloads` or `allow-modals`.
  The frame is keyed by its sandbox, so a change reloads the page under the
  new flags.
- **"Keep its origin"** (`preview-keep-origin`, a checkbox; `data-origin`
  names what it covers): off by default, per pane, offered only when the
  loaded URL is loopback `http(s)` on a port that is none of `hud_port`,
  `workshop_port`, `api_port`, 8402, 8403, 8405 nor the window's own, and
  only while `/status` says `frame_hardened: true`. Stored with the pane
  spec (`jarvis.hud.workspace`) as `keepOrigin: "<scheme://host:port>"` —
  the origin it was given for — so loading a URL on another port, host or
  scheme turns it off (`setPreviewUrl` drops it, and the frame applies it
  only while the origins match). Judged again on every load
  (`keptOrigin`) and **dropped from storage** when it no longer passes; the
  parser keeps only a grant that is exactly the stored URL's origin on a
  non-default loopback port. Disabled under an approval card (and inert
  behind its veil).
- **Why it is safe**: scripts plus same-origin amount to no sandbox only
  when the framed page is the parent's origin. The port check refuses the
  HUD's origin up front, and `frame-ancestors 'none'` on every HUD and API
  response stops a frame from navigating itself into the HUD later. A page
  on another origin cannot reach the parent's DOM, and `/approvals` still
  needs the HUD's own Origin and a JSON body.

**"Open in Preview" from a terminal.** A Ctrl+clicked link whose host is
loopback opens a small menu (`term-link-menu`: `term-link-url`,
`term-link-preview`, `term-link-browser`) instead of a new tab; any other
link still opens in a new window directly. "Open in Preview" is judged with
the ports above (refused, disabled, with `term-link-refused` saying why, for
the HUD's and the API's ports) and lands in a drawn Preview pane — the
focused one if it is one — else the focused pane switches to Preview (the
sidebar's click rule, refused for a File pane holding an unsaved edit).
The pane loads it as a request, judged again there, and stores it. Escape,
a click elsewhere or an approval card closes the menu.

Not in this package: §2.5 item 5, a "dev servers in your terminals" list
built from the listening sockets terminal sessions own.
