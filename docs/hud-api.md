# HUD ↔ daemon API contract (WP12)

Both WP12a (backend, Codex) and WP12b (frontend, Opus) implement this file
exactly. Everything already served by the v2 daemon (`docs/codex-briefs/
WP7-notes.md` route table, plus `/approvals` from WP5, `/route` from WP9,
`/tasks/{id}/start|steer|cancel|resume|answer` from WP11) stays as is. This
file adds what the HUD needs and nothing else. All JSON; loopback; the HUD
is served from the daemon on `FACE_PORT` (8402) and calls the API on the
**same origin** (the daemon serves the API on both listeners); the workshop origin
is `WORKSHOP_PORT` (8403), a different origin by design.

## Static

`GET /` → the HUD's page; `GET /assets/*` → its assets — from the newest
**installed build** (`config.HUD_DIR/<build-id>/`, decision C-1; see "the
HUD's CSP and installed build" below), or from `hud/dist` itself only under
`JARVIS_HUD_DEV=1`. With nothing installed, `GET /` is a 503 page naming
`jarvis hud install`. `GET /avatar.svg`, `GET /avatars`, `GET /voices`, `GET /models`,
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
- Every project the routes answer with (`GET /projects`, `GET`/`PATCH
  /projects/{id}`, `POST /projects`) carries **`root_real`** beside `root`
  (#50, 2026-10-11): where the root resolves (`projects.resolved_root`, the
  root the file routes read under), or `null` when it cannot be resolved.
  A file's `abs` sits under it even when `root` goes through a symlink, so
  a File pane's tree finds a buffer read through another project
  (`lib/dirtyMarks.ts` `treeRoot`, falling back to `root`). Display data
  only: never stored, never on the bus, and 400 if sent back on a POST or
  PATCH.

## Files (scoped: project root + `extra_dirs`; anything else 403)

**Every editor file route is owner-only, reads included** (ED-2, decisions
E-7): `projects.owner_only` — the HUD's listener and the HUD's own page (its
`Origin`, or a same-origin GET's `Sec-Fetch-Site`), else **403**. The API
listener every tool's HTTP client uses never reaches them. What the editor
may do with a file is one classifier, `jarvis/v2/editor_policy.py`, which
every one of these routes asks (`kind`):

| `kind` | What | The editor |
|---|---|---|
| `text` | everything else | editable |
| `env` | a credential env file: `.env` or `.env.<anything>`, any depth, not ending `.example`, case-insensitive, symlinks resolved both ways (`secrets.is_env_credential`, the owner's `.env*` rule). **Never demoted**: one whose bytes are not UTF-8 text (UTF-16 from PowerShell 5.1, Latin-1) stays `env`, with `editable: false` | **the owner's** to edit: "Jarvis can't read this file"; read-only when `editable` is false |
| `gate` | the gate's own state (`protected_paths()`: the allowlist, models.json, routing.json …), even inside `~/.config/jarvis` — only a path that **resolves to itself** (no symlink on its way) and is no credential by another rule; this exemption is the editor's routes' alone | readable, read-only |
| `safety` | SELF_PROTECTED, git's internals and global config, the served HUD build (`files._self_protected`) | readable, read-only |
| `binary` | not UTF-8, or a NUL in the first 8 KiB (not an env file) | readable (lossy), read-only; a PUT never overwrites it |
| `withheld` | a credential bundle by name, anything under `V2_CREDENTIAL_DIRS` (`~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.config/jarvis`, `~/.gnupg`) or v2's session carriers (`V2_DATA_DIR/claude`, `V2_DATA_DIR/codex`), Claude's and Codex's login files, git's credential store, `.netrc`, `~/.docker/config.json`, `~/.kube/config` (and `$KUBECONFIG`), `~/.npmrc`, `~/.pypirc` | **no content, ever** |

- `GET /projects/{id}/tree?path=<rel>&depth=2` →
  `{"path", "entries": [{"name", "kind": "file"|"dir", "size", "mtime",
  "policy"?}]}` (`.git`, `.jarvis`, `node_modules`, `.venv` omitted; a
  `SKIP_DIRS` in the backend, the v1 grep list). `policy` is present only on
  a row the editor treats specially: `"withheld"` (never opened; a withheld
  folder is never walked, and listing one is 403) or `"env"` (a file only: a
  virtualenv folder named `.env` is a folder). Inside a
  withheld folder only the gate's own state files — and the folders on the
  way to them — are listed (`~/.config/jarvis/allowlist.json` stays
  reachable from the Inbox).
- `GET /projects/{id}/file?path=<rel>` → `{"path", "abs", "content",
  "etag", "mtime", "mtime_ns", "size", "encoding": "utf-8"|"utf-8-bom"|null,
  "eol": "LF"|"CRLF"|"CR"|"mixed"|null, "kind", "editable": bool,
  "readonly": str|null, "protected": false}`. `abs` is where the file
  resolves (one editor buffer per file on disk, however many projects reach
  it); `etag` is the first 128 bits of the bytes' SHA-256, as hex;
  **`editable`** is whether the editor may save it — the HUD follows it,
  never the kind alone (an env file that is not UTF-8 is `kind: "env"`,
  `editable: false`, `encoding: null`), and a read without it is read-only;
  `readonly` is the sentence beside the editor's lock (null for an editable
  file Jarvis may read). A withheld file is `{"path", "abs", "kind":
  "withheld", "editable": false, "readonly", "protected": true}` — **no
  content and no etag**, 200. A `binary` (or non-UTF-8 env) file's content is
  decoded with replacement characters, for reading only. An unknown `kind`
  opens read-only and closed to Jarvis in the HUD.
- `PUT /projects/{id}/file` `{"path", "content", "expected_etag"}` →
  `{"etag", "abs", "mtime", "mtime_ns", "size"}`. `expected_etag` is the
  etag the edit was made against, or `null` for "the file must not exist"
  (create; also Save to recreate). Under the daemon's lock the bytes on disk
  are hashed again: a mismatch is **409 `{"error", "etag"}`** with the
  current etag (`null`: the file is gone), and the file is left
  byte-identical. A rewrite that restored the old mtime is still a 409; a
  `touch` alone is not one. 403 for every file that is not `editable` (every
  kind but `text` and `env`, and bytes on disk that are not text) and when
  the filesystem refuses the write; **500** for any other unexpected disk
  error (never a 409, which is the conflict bar); 413 over 2 MB; 400 for a
  malformed etag, for both or neither precondition, or any other key. Atomic
  (a temp file beside it, named `<name>.<random>.tmp` — so an env file's temp
  is an env file by name — 0600 from its first byte, renamed over), the
  file's mode kept;
  a new file is 0644, a new env file 0600. **`expected_mtime` is still
  accepted for one release** (exactly one of the two), then goes.
- `POST /projects/{id}/files/stat` `{"paths": [rel, …]}` (at most 100, else
  413) → `{"files": {rel: row}}`, each row `{"etag", "mtime_ns", "size",
  "kind"}`, `{"gone": true}`, `{"kind": "withheld"}`, or `{"error": status}`
  for a path the project cannot reach; `etag` is null over 2 MB. A file is
  hashed again only when its stat (device, inode, size, mtime_ns, ctime_ns)
  moved. The HUD asks every 2 s while it is visible, and on focus — inotify
  cannot see a write made from Windows on the 9p `/mnt/c` mount.
- **An env file's content travels only between the disk and the owner's
  editor**: never the bus, a log line, `log.jsonl`, Discord, an `@path`
  attachment, a dropped file, a diff, the workshop origin or any tool result
  (each refuses it: `hud_api.protected` asks `is_env_credential`, and
  withheld files too — with **no gate exemption**: the gate's readable state
  is the editor's routes' alone, so `@.config/jarvis/allowlist.json` is not
  attached; a dropped file named like a credential store, `.npmrc` or
  `.docker/config.json` among them, is not either). The HUD keeps it out of localStorage and
  add-to-chat, and gives its model a language of its own (`dotenv`) so its
  words are never offered as suggestions in another file.
- `GET /tasks/{id}/diff` → `{"base", "head", "files": [{"path", "status":
  "A"|"M"|"D"|"R", "additions", "deletions"}], "patch": str}` from the
  worktree (`git diff <base>...HEAD` plus the working tree), capped at 1 MB
  with `"truncated": true`.
- `GET /tasks/{id}/diff/file?path=<rel>` → `{"before", "after"}` for the
  Monaco diff editor.

## The workshop origin (`WORKSHOP_PORT`)

- The daemon also serves `http://127.0.0.1:8403/p/{project_id}/<rel>`
  read-only from the project root (same scope rules, same skip list,
  `Cache-Control: no-store`, correct content types). The HUD's Browser
  pane (Preview until BR-3a) iframes that, or any
  `http://localhost:<port>/…` URL the owner types.
  Never the HUD origin: the backend refuses to proxy it, the frontend
  refuses to load it.
- **What it refuses (403)**: a credential file (`hud_api.protected`: env
  files, withheld files, credential bundles), **any path with a hidden
  component inside the project** (a name starting with `.`) **except
  `.well-known`** — as written and where it resolves; its 403 says "hidden
  path: not served on the preview origin", every other one "protected file" —
  and **anything under
  Jarvis's own config and data folders** (`~/.config/jarvis`,
  `~/.local/share/jarvis`, `V2_DATA_DIR`, the sessions, spill, goals and
  voices folders; `editor_policy.jarvis_dirs`). It has no owner check by
  design and answers any loopback client — v1's `fetch_page` included — while
  the Inbox is rooted at `~` (review of PR #49, LOW3).
- The port is read from `GET /status` → `workshop_port` (2026-10-08), never
  assumed: a hard-coded 8403 made the HUD suite's mock-served window load the
  owner's **live** daemon's preview for fixture project `p1`
  (`GET /p/p1/index.html -> 400` in the live log). Only a daemon too old to
  report one falls back to 8403.
- The workshop origin carries **no** frame headers (it is framed by the HUD
  on purpose), but every response there carries one CSP, `sandbox
  allow-scripts allow-forms`, so every document it serves runs with an
  opaque origin; the HUD and API listeners refuse to be framed (WP-E,
  below).

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
- The WP11 verbs' bodies (`daemon.task_verb_body`, 400 for any other
  shape): `start` and `cancel` `{}`; `steer` `{"text", "spoken"?}`;
  `resume` `{"provider"?}`; `answer` exactly `{"index", "text"}`, where
  `index` is the question's place in `spec.questions` — every question,
  answered and assumable ones included — and only a `clarifying` task takes
  one (`control.answer_refusal`, 409 with its sentence otherwise). This
  file named the verbs without their bodies, and the HUD sent `{question,
  answer}` to `answer` until 2026-10-11.

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
HUD's `Origin` — or, for a plain GET/HEAD only, one carrying no `Origin` at
all that the browser marked `Sec-Fetch-Site: same-origin` (Chromium sends no
Origin on a same-origin GET; `projects.is_owner`, 2026-10-11); anything else
is 403. No tool reaches them (fast path, MCP,
Claude or Codex threads, Discord) — `tests/v2/archive_check.py` asserts
it. The gate keeps every tool's HTTP client off these routes; it is not a
boundary against a shell command the permission gate approved, the same
footing as `/approvals` (owner-only too since BR-0, below).

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
`localhost:<port>` — except a plain GET (the listing), which Chromium sends
with no Origin when the HUD's own page asks, and which is accepted with no
Origin only when the browser marked it `Sec-Fetch-Site: same-origin`
(a forbidden header no page can set; since 2026-10-11 — before it, the
HUD's own listing was refused). Anything else is 403: the API listener (8405,
every tool's client), a missing Origin on a write or the socket, `null`, the
workshop origin, any other site. The workshop listener (8403) serves only `/p/...` and answers 404. No
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
- `POST /terminals` `{in, cols?, rows?, readable?}` → 201 `row`. `in` is `"home"`,
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
  is a 409, never a cut file or a wedged route. `readable` (a boolean,
  default true; 2026-10-11, WP-G2) sets the "Jarvis can read" switch before
  the shell runs, so a split's copy of a terminal Jarvis may not read is
  never readable, not even for the round trip a `PATCH` would take.
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

**Output stays in memory unless Jarvis reads it**: on the bus there are
lifecycle records only (`terminal_attached`, and since WP-F `terminal_read`:
ids, a count, a time), never output; the HUD lists terminals with `GET
/terminals` and learns of an exit over the socket. Output is not in any log
(the daemon logs opened / attached / read / exited / closed with the folder as a
project name or `~`, and logs `/terminals` paths without their query, so
never a ticket), not in a thread log, not on Discord, not on disk. **The
exception is a `terminal_read` result (WP-F, below)**: what `terminal_read` returns goes where any tool result goes — the fast-path transcript (and so OpenRouter, with every later request), that conversation's v1 session `messages.json`, `config.SPILL_DIR` if context truncation cuts it later, and anything the model's reply repeats (a reply is mirrored to the chat's Discord thread and spoken).
Its `tool_finished` summary is fixed ("read N lines", "refused", "error"), so
the tool's own record on the bus and in the thread's `log.jsonl` carries none
of it. That is all the fixed summary promises: the model's reply is not
filtered — it goes to `log.jsonl`, the bus, Discord and speech like any reply,
so whatever it repeats of the terminal lands there too.

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
- **Split and × (WP-G2, 2026-10-11)**: splitting a pane that shows a terminal
  is one `POST /terminals` with the `in` the source was opened with (the
  window's remembered spec) and, unless the source's row says it is
  readable, `readable: false` — the copy is unreadable from its first byte,
  and a source the window does not know fails closed. A refused create (the cap's 409) leaves the copy on
  the picker with the daemon's sentence; nothing retries. × on a pane showing
  a terminal sends **nothing**: no `DELETE`, no new ticket — the socket stays
  attached and the terminal becomes the panel's selected tab.
- **Continuing after Jarvis restarted (2026-10-12, the owner's option (a))**:
  no route changed. A terminal that ended with Jarvis (`exit` with `reason:
  "ended"`, or a ticket answered 404) and that this tab showed reads
  "Jarvis restarted; this terminal ended. Type to continue in a new shell
  here." with **Continue here** beside **New terminal here**. A typing key
  (printable text, Enter, Backspace, Tab, an arrow), a paste or Continue
  here is one ordinary `POST /terminals` with the `in` it was opened with
  and, unless its row (kept current from every listing and `PATCH`) says it
  is readable, `readable: false`. The new terminal takes the old one's
  place everywhere the window tracks it (its panel tab position, its pane,
  the selected tab, `jarvis.hud.terminals`, `jarvis.hud.terminals.mine`)
  and attaches into **the same xterm**. On that attach and every later one
  the window writes, after the RIS and before the replay, the old text —
  rebuilt from xterm's cells (characters and SGR colours only, at most
  1 MiB, never the old bytes, so nothing in it is a query, a mode or an
  alternate screen) — and a separator, `── restarted HH:MM ──`, plain
  text (the old text walked from the bottom, stopped at the cap; one longer
  line keeps its tail). Keys and pastes made while it starts are held
  (4 KiB, a paste that does not fit is dropped whole) and, once the new
  replay has been parsed, **typed, never run** (PR #64 review L6): keys up
  to the first Enter, never it nor anything after, and no paste holding a
  line break — the notice says the line waits at a new local shell's
  prompt (check it, then press Enter, or paste again). **Any refusal drops
  the whole hold** (a key that does not type, past 4 KiB, a card raised
  meanwhile, the terminal no longer on screen — the panel closed, another
  tab, its pane switched away), said with the `unsent` notice. Never under
  a card, after a crash, on a load, a restored panel or a reattach, for a
  terminal this tab did not show (another window's), one whose `in` is not
  remembered (never guessed from its row), a shell that exited on its own
  (Restart and Close as before) or a program terminal; Ctrl+D and the HUD's
  own keys never continue one. Six already open: the daemon's 409 sentence
  in the strip, nothing held, and from then on only Continue here tries
  again. **The old text is memory only**: a page reload loses it, and
  nothing of it is written to disk or sent anywhere. The switch comes from
  this window's last-seen row, which another window can leave stale (a
  limit that predates this; it needs a daemon event).
- **`busy`**: × reads a `GET /terminals` sent after the click (never one
  already on its way) and, when the foreground process is not the shell,
  asks before `DELETE`; if the listing fails it asks anyway, saying it could
  not check.
- **Output is hostile bytes**: an OSC 0/2 title is text in the pane header
  and the terminal's bar, one line, capped at 80 — never `document.title`;
  links (plain or OSC 8) open only for `http`/`https`, only on Ctrl+click,
  in a new window with `noopener`; there is no clipboard addon (no OSC 52)
  and window reports stay off. "Open in Browser" for loopback links is
  WP-E's (below).

## Additions 2026-10-10 (Preview — now the Browser — and dev servers, WP-E)

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
ride in the same header. **None of them can change the frame rule**: a
response's own directives holding a comma (which starts a second policy in
the same header — `img-src 'self', frame-ancestors *` let a frame land on
the HUD, PR #31 review), anything but printable ASCII (CR or LF would split
the header; U+2028 used to fail after the status line was written), or
naming `frame-ancestors`, `sandbox`, `report-uri` or `report-to`, raise
`ValueError` (a programming error; `binary()` checks before writing a byte,
so the answer is **one 400** — the dispatcher's status for a
`ValueError` — never half a response or a second status line). The HUD's
full policy (ED-4, below) is built by that same function, in the same one
header. The **workshop listener (8403) carries neither frame
header**; instead it sends one CSP, `sandbox allow-scripts allow-forms`
(`hud_api.workshop_security_policy`, a response's own directives merged and
checked the same way), so a workshop page runs with an opaque origin
everywhere: inside the HUD (unchanged — the Browser frame was already
opaque), in a top-level tab (newly), and in a Browser frame whose page was
let keep its origin and then navigated itself to the workshop (it used to
get the real workshop origin there — its storage and same-origin reads of
any project's served files; PR #31 review). v1's whiteboard workshop is its
own server and is unaffected.

**`GET /status` gains three fields:**
- `hud_port` — the HUD listener's bound port;
- `api_port` — the API listener's bound port;
- `frame_hardened: true` — set by the handler class that adds the headers
  (`false` before the daemon has started). A HUD whose `/status` lacks it
  (an older daemon) never offers "keep its own origin".

**The Browser pane (`lib/browser.ts`, `BrowserTab.tsx`; the Preview pane,
`lib/preview.ts` and `PreviewTab.tsx`, until BR-3a):**
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
- **"Keep its origin"** (`browser-keep-origin`, a checkbox; `data-origin`
  names what it covers): off by default, per pane, offered only when the
  loaded URL is loopback `http(s)` on a port that is none of `hud_port`,
  `workshop_port`, `api_port`, 8402, 8403, 8405 nor the window's own, and
  only while `/status` says `frame_hardened: true`. Stored with the pane
  spec (`jarvis.hud.workspace`) as `keepOrigin: "<scheme://host:port>"` —
  the origin it was given for — so loading a URL on another port, host or
  scheme turns it off (`setBrowserUrl` drops it, and the frame applies it
  only while the origins match). Judged again on every load
  (`keptOrigin`) and **dropped from storage** when it no longer passes; the
  parser keeps only a grant that is exactly the stored URL's origin on a
  non-default loopback port. Disabled under an approval card (and inert
  behind its veil). A split copies a Browser pane's URL, judged again
  on load, and **never** its keep-origin grant (WP-G2).
- **Why it is safe**: scripts plus same-origin amount to no sandbox only
  when the framed page is the parent's origin. The port check refuses the
  HUD's origin up front, and `frame-ancestors 'none'` on every HUD and API
  response stops a frame from navigating itself into the HUD later. A page
  on another origin cannot reach the parent's DOM, and `/approvals` needs
  the HUD's listener, its own Origin and a JSON body (owner-only, BR-0).
  And a page that sends its frame to the workshop lands opaque there (the
  workshop's own sandbox).
- **A URL carrying a user name or password** is refused, as a terminal
  link is.

**"Open in Browser" from a terminal.** A Ctrl+clicked link whose host is
loopback opens a small menu (`term-link-menu`: `term-link-url`,
`term-link-pane` "Open in Browser", `term-link-tab` "Open in a Chrome tab";
`term-link-preview` and `term-link-browser` until BR-3a) instead of a new
tab; any other link still opens in a new window directly. "Open in Browser" is judged with
the ports above (refused, disabled, with `term-link-refused` saying why, for
the HUD's and the API's ports) and lands in a drawn Browser pane — the
focused one if it is one — else the focused pane switches to the Browser (the
sidebar's click rule, refused for a File pane holding an unsaved edit).
The pane loads it as a request, judged again there, and stores it. Escape,
a click elsewhere or an approval card closes the menu. **The menu is placed
inside the window** (`placeMenu`): below the click, else **above** it — a
dev server's `Local:` line is usually the panel terminal's last row — and
clamped on both axes, zoom-aware (PR #31 review: it opened below the window
at 100% and 160%).

Not in this package: §2.5 item 5, a "dev servers in your terminals" list
built from the listening sockets terminal sessions own.

## Additions 2026-10-10 (Jarvis reads a terminal — WP-F)

Decisions W-2 in `docs/plans/2026-10-09-hud-workspace-decisions.md`.

- **Tool `terminal_read(terminal="", lines=200)`** (`jarvis/v2/tools/
  terminal_read.py`, v1 registry). `terminal` is what the HUD shows — the
  id, the title (`bash · Calc`, or just its folder part), or the number in
  the panel's order; empty means the only one — resolved by the daemon,
  never a path. `lines` is clamped to 1–1000. It returns the terminal's
  normal buffer as plain text (no escape sequences, the alternate screen
  never read), fenced as untrusted web content from `HUD terminal <title>
  (id <id>)`, with notes after the fence: `[N line(s) withheld: possible
  secret]`, `[the oldest N line(s) left out to fit]` (48,000 characters at
  most), `[N long line(s) cut to 2,000 characters]`. Or exactly
  `Refused: possible credential in this output` (an exact known value or its
  encodings, a credential format, or a secret-printing command — never which,
  never where), `Refused: the owner has turned off reading for this terminal
  (<title>)`, or an `Error:` naming the open terminals.
- **Who holds it**: `FAST_TOOLS`, and only for an owner's chat turn the HUD's
  own window sent — the send route sets `UserMessage.desk` only on the HUD's
  listener with its Origin. A task's thread, a Discord or DM turn, the escape
  hatch, a schedule, a sub-agent and every v1 surface neither see nor can run
  it (`runtime.at_desk()` fails closed); jarvis-mcp does not offer it. It is
  not `dangerous`, asks nobody, and writes nothing.
- SSE **`terminal_read`** `{kind: "terminal_read", data: {terminal_id,
  lines, at, refused}}` — exactly those keys — for every read that reaches a
  terminal, refused or not (`lines` is how many it covered; 0 when the switch
  refused it). Never output, a command or a reason. The terminal's bar shows
  the last one: "Jarvis read 200 lines · 15:42", or "refused a read · 15:42".
  A record of another shape is dropped whole; any other key is never drawn.
  It is HUD chrome: nothing is written to the PTY or the ring.
- **Where the text goes** (corrected 2026-10-10): what `terminal_read` returns goes where any tool result goes — the fast-path transcript (and so OpenRouter, with every later request), that conversation's v1 session `messages.json`, `config.SPILL_DIR` if context truncation cuts it later, and anything the model's reply repeats (a reply is mirrored to the chat's Discord thread and spoken). The
  `tool_finished` record's `summary` for `terminal_read` is fixed — "read N
  lines", "refused" or "error" — never the result's first characters. That
  keeps the tool's own record clean, nothing more: the model's reply is not
  filtered, and reaches `log.jsonl`, the bus, Discord and speech like any
  reply (2026-10-11 re-review).
- **What a dev server prints stays readable** (2026-10-11 re-review): an
  `--env-file` loads a file (`node --env-file=.env server.js`), `wc -l <
  .env` counts one, `docker inspect --format '{{.State.Status}}'` prints no
  environment, and a reader's or an interpreter's name in prose (a commit
  message, a nodemon banner) is not a command. A screen dense with command
  candidates is refused past a parse budget rather than read slowly, and
  what any parse bound leaves unread refuses the read when it could hold a
  printer.
- **The limit**: a secret with no recognisable shape and no keyword beside it
  is not caught, nor is one an alias, a shell function, a script or a symlink
  prints, nor one a script prints from a credential file it was handed as an
  argument, nor a program piped in from anything but a literal
  `echo`/`printf` (rule 3 reads command lines by shape). A REPL or
  here-document input line (`> `, `>>> `, `... `) refuses only for an
  explicit environment read, never for the bare words `ENV`, `env:` or
  `getenv`, so an environment read spelled only with those is not caught
  there. The loading idioms
  `env $(cat .env | xargs) …` and `export $(grep -v '^#' .env | xargs)` stay
  refused: their error messages can echo part of a value. The "Jarvis can read"
  switch and the note are the backstops.

## Additions 2026-10-11 (the VEX backend — VX-1)

VEX V5 in the HUD (editor plan §2.9; decisions V-1 to V-5; code in
`jarvis/v2/vex.py`). The backend only: the VEX view and its panel tabs are
VX-2, the new-project dialog VX-3. **Every route is owner-only** on the HUD
listener with a strict Host: the API listener (8405) is 403, the workshop
listener (8403) 404, a missing or foreign Origin (the workshop's, `null`)
403, a rebound Host 403. The rule is the shared one, `projects.is_owner`
(PR #41): **a same-origin `GET` carries no Origin in Chromium** (measured
2026-10-10), so a `GET` with no Origin is accepted only when the browser's
own `Sec-Fetch-Site: same-origin` says the HUD sent it (`none`, `same-site`
and `cross-site` are 403); every other method needs the HUD's Origin. **No request names an executable, a path to run or an argument**:
the programs and their argv come from `vex.json` and `vex.py`'s tables, the
project folder from the project's id, and a body with any field not listed
(`path`, `args`, `program`) is 400. Every 409 is a sentence. Output lives in
the program terminal's ring and the window only, as WP-C's terminals do —
never on the bus or in the log. **Every action that spawns a VEX tool first
hashes it** (and, for a build, the five tools `make.exe` runs) against the
SHA-256 `jarvis vex setup` recorded, and is 409 "the VEX tools changed since
`jarvis vex setup` (<tool>); run it again" on a mismatch.

**SSE `vex_device`** `{action: "download"|"brain"|"info", at}` — one per
device action, when it starts; nothing else (no slot, name, path or output),
as `terminal_attached`. A device action the window did not cause (a
program running as the owner POSTing with a forged Origin) shows up here; the
HUD can flag one it cannot match to its own click.

- `GET /vex/status` → `{configured, hint, vexcom_version, toolchain,
  sdks: {cpp, python}, extension_version, templates, project_folder, device,
  brain}`. `configured` false with `hint` (a sentence ending "run `jarvis
  vex setup` again") when `vex.json` is missing, malformed, or names a path
  that fails the use-time check. `device` is the device action running
  (`"download"`, `"info"`, `"brain"` or null); `brain` the brain terminal's
  id.
- `GET /projects/{id}/vex` → detection: `{project_id, vex: true, name,
  language: "cpp"|"python", slot, sdk_version, printf_float, python_main,
  on_windows_drive, builds}` or `{project_id, vex: false, reason}`, plus
  `malformed: true` for a settings file that is not the extension's v2 shape
  (platform V5, language cpp/python, slot 1–8, `extension.json` 2; read
  within 64 KB, never through a symlink) and `pros: true` for a PROS project.
- `POST /projects/{id}/vex/build` `{action: "build"|"rebuild"|"clean"}` →
  202 `{build_id, terminal_id}`. C++ only, the project on a Windows drive,
  one build per project at a time (a lock file `vexbuild` shares: 409 "a
  build of this project is already running"), and refused (409, with the
  reason) when the project's make files name `vexcom` — or cannot all be read
  (more than 32, one past 256 KB, a symlink or FIFO among them, a symlinked
  `vex/`, an `include` named by a variable or outside the project). Runs `make.exe P=<name> T=<SDK>
  CC=… CXX=… OBJCOPY=… SIZE=… LINK=… ARCH=… [PRINTF_FLOAT=1]`, plus `-B` for
  rebuild and `clean` for clean, every path converted to `C:\…`, in the
  project folder, as a program terminal (`program: "vex-build"`); it replaces
  the project's previous ended build terminal. 5-minute timeout.
- `GET /vex/builds/{id}` → `{id, project_id, action, state:
  "running"|"succeeded"|"failed"|"timed_out", terminal_id, exit_code, errors,
  bin, started, ended}`. `errors` are `{file, line, col, severity:
  "error"|"warning", message, in_project}` parsed from the build's own
  `file:line:col: error|warning:` lines (notes skipped; a file under the
  project comes back relative, with `/`); `bin` is `{path: "build/<name>.bin",
  size, mtime}` when this build wrote one. The last 32 are kept.
- `POST /projects/{id}/vex/download` `{slot, name}` → 202 `{terminal_id}`.
  `slot` an integer 1–8 (not a string, not a bool); `name`
  `^[A-Za-z0-9_][A-Za-z0-9_-]{0,19}$` — both 400 otherwise. 409 `{error,
  code: "build_first"}` (`Vex._fresh`, judged under the project's build lock)
  when there is no `build/<name>.bin`; while a build of the project is
  unfinished (marked on disk before make runs, cleared only by a success — a
  cancelled, failed or timed-out build, or a daemon that died mid-build,
  refuses); when this daemon's last build wrote exactly this binary (its `P=`
  name, mtime_ns and size; a make that found nothing to do never counts) but
  the sources' digest (sorted path, mtime_ns, size) differs from when that
  build started; and otherwise when it is older than the newest source.
  Sources are only C/C++ sources and headers, assembly and make files
  (`SOURCE_SUFFIXES`), never notes or editor backups. The record is in
  memory: a restart falls back to the mtime rule. The settings file's name
  must pass the `P=` rule before it names a file; VX-2 chains Build (then one
  rebuild) then Download on the one click, on the `code`, never the sentence.
  Every other 409 here has no `code`. Also 409 while another device
  action runs, while the brain terminal holds the port, while a build of the
  project runs, and for a Python project (its download flag is a spike
  item). Runs `vexcom.exe --name <name> --slot <n> --write <bin as C:\…>
  --progress` (DishPy's flags) in the project folder (`program:
  "vex-download"`); 3-minute timeout. **No "run after download"** (V-3), and
  no other route, CLI or tool downloads.
- `POST /vex/brain` `{}` → `{terminal_id, started}`: the brain's user port,
  `vexcom.exe --user`, both ways (`program: "vex-brain"`, the only program
  terminal that takes typed input). One at a time: the open one comes back
  with `started: false`. 409 while a download or info read runs. Closing its
  terminal frees the device.
- `POST /vex/device/info` `{read?}` → `{text, exit_code, read}`: a device
  read, from the owner's `vexcom --help` (vexcom 1.0.2b2). `read` is
  `"system"` (the default) — exactly `vexcom.exe --json`, "system info in
  json format" — or `"programs"` — exactly `vexcom.exe --programs`, the
  programs on the brain; anything else (or another key) is 400, before
  anything is looked up. Both are the one `info` device action (`program:
  "vex-info"`, a terminal closed when it ends; `vex_device {action: "info"}`
  on the bus); 409 while a download runs or the brain terminal holds the
  port, and a download, the brain terminal or another read is 409 while
  one runs. `vexcom.exe` is hashed against `vex.json` immediately before the
  spawn. The output comes back as plain text: escapes and control characters
  removed, markup left as characters — draw it as text. Output that parses
  as JSON (from its first line opening `{` or `[`, the lines before kept, up
  to 256 KiB) is pretty-printed with a two-space indent; a JSON string's
  control characters stay escaped (`\u001b`) and invisible characters are
  removed. At most the last 16 KiB. A 10-second timeout (409 "did not
  answer"). A non-zero `exit_code` (no brain connected, say) still answers
  200 with vexcom's text.
- `GET /vex/templates` → `[{id, language, label}]`: the extension's
  `cpp_v5_vsc_*.zip` and `py_v5_vsc_*.zip` in the configured templates
  folder, labelled from its `index.json` where it names them.
- `POST /vex/projects` `{name, language, template, location?}` → 202 `{id,
  name, path, state: "asking", project_id: null, message}`. `name`
  `^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$`, `template` an id of that language,
  `location` the parent folder (default `vex.json`'s `project_folder`, V-4);
  the target is `<location>/<name>`, which must be on a Windows drive and pass
  `folders.check_project_folder` (409 with its sentence; a folder with
  something in it is 409 too). The template is checked whole before anyone is
  asked. Then the **`project_folder` card** (exact path, Approve/Deny, never
  Always, a timeout denies) unless the folder exists and is empty (O5); on a
  yes one `mkdir`, the template unpacked by Jarvis's own code, the settings
  file written in the extension's v2 shape, and the project created —
  `project_created` arrives as usual.
- `GET /vex/projects/{id}` → that request: `state` `"asking"`, `"created"`
  (with `project_id`), `"declined"` or `"failed"`, and a `message`. A
  template write that fails after the yes takes back what that request wrote
  (only files it created and folders it made, through the folder's fds, each
  judged by its `(st_dev, st_ino)` — a renamed-in entry is never removed — and,
  after a refusal by the store, only if the folder at that path is still the
  one the template was written through; VX-3 review LOW-2 and its delta
  review): `failed`'s message names the colliding file where there is one,
  and says what the folder holds now, read through its fd — empty again
  (Create uses it again without a card, O5), what it still holds (empty or
  remove it), or that the path is no longer the folder written to (nothing
  removed). The HUD reads this one read
  at a time and rides out a failed read (5xx, the network) with a backoff;
  only an answer, a 404/400, or three minutes of failures end its tracking.
- `POST /vex/projects/check` `{name, language, template, location?}` (VX-3,
  2026-10-12) → 200 `{path, windows_path, state, asks, describe}`: what
  `POST /vex/projects` would do with the same body, **without asking or making
  anything**. `path` is the exact folder the `project_folder` card will name,
  `windows_path` its `C:\…` spelling, `state`
  `"missing"` or `"empty"`, `asks` false for an existing empty folder (used
  without a card, O5), `describe` the card's words for it. Every refusal is
  Create's, word for word (400 for a bad name, language, template or location;
  409 for a folder off a Windows drive, refused by `check_project_folder`,
  **refused by `to_windows`** — a character a Windows command line could
  misread, such as `(`, `&` or anything outside ASCII, which a build or a
  download would refuse later (VX-3 review MED-1; Create refuses it too) — or
  not empty), less Create's closing "Nothing was made." Owner-only like every
  VEX route. `GET /vex/templates` answers a templates folder that cannot be
  listed with a 409 sentence. The HUD reads `/vex/status`'s
  `project_folder` as the dialog's default parent (V-4).

**Program terminals** are listed by `GET /terminals` like any other, with two
new row fields: `program` (`null` for a shell, else `"vex-build"`,
`"vex-download"`, `"vex-info"` or `"vex-brain"`) and `input` (whether typed
input reaches it). Attach, ticket, PATCH `readable` and DELETE work on them
unchanged; DELETE stops the program. A frame to a terminal with `input`
false is answered `{"type": "input_refused", bytes, reason}` (a lone Ctrl-C
still goes through). Program terminals count apart from shells: six of each.

### The VEX view (VX-2, 2026-10-11)

The download's 409 gained `code: "build_first"` (review round, above); the
HUD's side of the routes above (`hud/src/api.ts`, `lib/vex.ts`,
`state/vex.ts`, `components/VexView.tsx`):

- `GET /projects/{id}/vex` is read once per project a drawn pane is about, and
  again when the VEX view shows, when the window gets focus, and every 30 s
  for one older than that: the strip offers the `vex` view only for `vex:
  true`, `malformed: true` or `pros: true`.
- The terminal row's `program` and `input` are read (`lib/terminal.ts`
  `TerminalRow`): a program terminal's panel tab carries `data-program`, and one
  that ended offers Close, never Restart or "New terminal here" (they open a
  shell). A program terminal the window's own click started is listed, claimed
  as this window's and shown as the panel's tab (`terminals.showProgram`).
- Build: `POST …/vex/build {action}`, then `GET /vex/builds/{id}` every 0.6 s
  to its end. The last build id per project is kept in `localStorage`
  (`jarvis.hud.vex.builds`, ids only) so the errors list comes back after a
  reload.
- Download: `POST …/vex/download {slot, name}` **only from the Download
  button, on a trusted pointer click whose `detail` is exactly 1** (a double
  click is one download). **One chain per window**: no second download, for
  any project, while one runs. A 409 with `code: "build_first"` starts a
  build and, when it succeeds and no approval card has come up since the
  click (nor while the 409 was on its way), sends the download again; if that
  is `build_first` again (make found nothing to do, a header it does not list
  changed), one `rebuild` and the download again; a third `build_first`
  stops the chain with the daemon's sentence. While the brain terminal holds
  the port it is closed first (`DELETE /terminals/{id}`) and opened again
  afterwards (`POST /vex/brain`) — never after a chain a card, a crash or the
  owner's Cancel dropped, nor after a card that came and went while the
  download ran. **Cancel** (until the download is sent) closes the
  build's program terminal (`DELETE /terminals/{id}`, which ends the build)
  and sends nothing. The download's end is read from the terminal listing
  (`exited`, `exit_code`).
- SSE `vex_device`: matched against the device actions this window started
  (recorded before each request; 30 s); one it did not start is said in the
  VEX view.
- Test hooks (read-only): `window.__hudVex` (`blocked`, `cards`, `chain()`, `run(id)`),
  and `__hud.monacoEditors()` rows gain `line` and `column` (where the cursor
  is).

## Additions 2026-10-11 (BR-0 — only the HUD window approves)

- **`POST /approvals/{id}` `{decision, always?}` is owner-only**
  (`projects.owner_only`): it answers only on the HUD's listener
  (`FACE_PORT`, 8402) and only to a request whose `Origin` is the HUD's;
  anything else is **403**, and the request stays pending. The API listener
  (8405) serves the same HUD page, so a page loaded from it sent an Origin
  equal to its Host and used to pass `check_origin`. The HUD already calls
  it from its own listener (`api.resolveApproval`); Discord's buttons and
  `/yes`/`/no`/`/always`, and the escape hatch, resolve in-process and
  never call the route. **`GET /approvals` is unchanged** — open on both
  listeners; reading what waits is not a lever.
- **`POST /threads/{id}/answer` cannot approve.** Its body is exactly one
  of `{req_id, text}` (a provider question's answer, passed on as text) or
  `{req_id, decision}` (a provider's pending approval). For an approval,
  anything but a deny — as `decision` or as `text` — is **400** on every
  provider: a yes comes only from the card above (the broker), and this
  route only withdraws. (The Claude provider used to accept an
  `allow` here, even as `text`, and it beat a card the owner had not
  answered.) Provider questions are Codex's alone; the Claude provider and
  the fast path raise none.
- **`PATCH /projects/{id}`: a change to `profile`, `always_ask`,
  `extra_dirs` or `root` is owner-only** (the HUD's listener and Origin,
  else **403**, nothing changed). A field sent back with its current value
  needs no owner, and `name`, `routing` and the rest are as before. The HUD
  already PATCHes from its own listener; nothing else changes these fields
  over HTTP.
- **v1's browser and `fetch_page` refuse every Jarvis port**
  (`config.control_ports()`: 8402, 8404, 8405, 8406, configured and
  default; never 8403) in every loopback spelling — they used to refuse
  only 8402, so a v1 agent could open the HUD on 8405. v1's browser judges
  the page wherever it is, before every act and read (a page that moved
  itself onto the HUD listener after goto's check was read and its card
  clicked), and `fetch_page` judges each redirect hop before requesting it.
  A *cross-site* navigation onto the HUD or API listener was already a 403
  (`Sec-Fetch-Site`); a same-site page (another `localhost` port) was not.
- **The built HUD (`config.HUD_DIST`, what `GET /` and `/assets/*` serve) is
  not writable by any agent write tool**, v1's or a v2 provider's, nor by a
  Codex write grant: its script runs with the HUD's Origin. Rebuilding it is
  the owner's (`npm run build`).

## Additions 2026-10-11 (the HUD's CSP and installed build — ED-4)

Editor plan `docs/plans/2026-10-09-hud-editor-plan.md` §1.7 and §2.7;
decision C-1 (the decisions file wins).

**Every HUD and API response carries the full policy**, in the one
`Content-Security-Policy` header WP-E's frame rule rides in
(`hud_api.content_security_policy`, `hud_api.HUD_POLICY`):

    default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self';
    connect-src 'self'; style-src 'self' 'unsafe-inline';
    img-src 'self' data:; font-src 'self' data:; media-src 'self';
    frame-src http://localhost:* http://*.localhost:* http://127.0.0.1:* http://0.0.0.0:*;
    base-uri 'none'; form-action 'none'; object-src 'none';
    frame-ancestors 'none'

— the page, every `/assets/*` file (a worker takes its policy from its own
script's response), JSON, SSE, errors and the terminal's `101`. No inline
script, no script from another origin, no `eval`, no `blob:` worker, no
plugin, no `<base>`, no form posted anywhere. `'wasm-unsafe-eval'` came
with ED-5 (below): WebAssembly compiles, JavaScript from a string still does
not. `connect-src 'self'`
covers the same-origin `ws://` terminal socket in Chromium (CSP3). A CSP host
source cannot name an IPv6 literal or a 127/8 address other than 127.0.0.1,
so the Browser judge refuses `[::1]` and `127.0.0.2` with a sentence naming
`http://localhost:<port>/` instead. There is no `<meta>` policy; the header
is the only one.

A response's own directives may only **narrow** the policy: each must
restate a directive the policy sets, with a subset of its sources (`'none'`
always qualifies); a response that states `default-src` (necessarily
`'none'`) drops every fetch directive it does not restate. Anything else —
a new directive, a source the policy lacks, a directive twice — raises
`ValueError` (a 400 before a byte is written). `/avatar.svg` sends
`default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action
'none'; frame-ancestors 'none'`.

**The HUD is served from an installed copy (C-1).** `jarvis hud install` — a
CLI the owner or the lead runs, never a tool — runs `npm run build` in `hud/`
and copies `hud/dist` into `config.HUD_DIR/<build-id>/` (default
`~/.local/share/jarvis/hud/`, `JARVIS_HUD_DIR` overrides; ids are
`YYYYMMDDTHHMMSSffffffZ-<12 hex>`, so they sort by time), files 0444 and
folders 0555, with `manifest.json` (`format`, `build`, `installed_at`,
`source`, `git_head`, `dirty`, `files: {path: {sha256, size}}`), through a
temp folder renamed into place; the newest three builds are kept. It refuses
**uncommitted changes under `hud/`** (`git status --porcelain -- hud`, which
counts untracked files) unless `--allow-dirty` is given, looks again once the
build is done (an edit that landed mid-build is never recorded clean, and a
checkout that moved is refused), and records the commit and whether it was
dirty. Its git runs hardened, as `gitops` runs it: an absolute git, `GIT_*`
dropped, pager and editors pinned, hooks and fsmonitor off; a checkout whose
own config names a program git would run is not read at all ("cannot say"). It refuses an install root that is, or sits
under, a symbolic link, one inside the repository, not a folder or writable
by others, and a build output holding a link, a special file, an odd name or
no `index.html`.

The daemon serves the **newest intact** build — its manifest reads (a
malformed, oversized or hostile manifest is ignored, never an error) and its
`index.html` still matches it; a newer build whose page fails is passed over
for the one before. Only a file the manifest lists is served, read without
following a link, its size and SHA-256 checked **on every read** (no cache);
anything else is a 404, logged. An asset the newest intact build lacks is
served from an older kept build that has it (Vite names are content hashes),
so a window loaded before an install keeps its lazy chunks — **an install
needs a reload, not a daemon restart**. Serving an older build because a
newer one failed is never silent: `/status` says `"fallback"` and lists the
build passed over. With no intact build, `GET /` is a 503 page that says
which: "not installed", or "failed verification" when builds are there but
none passes. `config.HUD_DIST` is the install root (or
`hud/dist` under `JARVIS_HUD_DEV=1`), and BR-0's one write check refuses the
install root always, plus `hud/dist` in dev mode, to every agent write tool
and v2 grant.

**Jarvis never runs the installer.** `jarvis hud install` is DENY for agents
through one detector (`protected_state.owner_op`, its table `OWNER_OPS`,
asked by `rules.decide`, so v1's gate and v2's layer 1), and `run_command`
refuses it with no approver. It judges the **command word** after peeling
wrappers and runners (`env` incl. `-S`, `nohup`, `timeout`, `flock`,
`systemd-run`, `uv run`, `ssh host …`, `xargs` — whose input may complete
the path), `python -m jarvis`, a `$VAR` or an `ln -s` link the line made, a
Python run of `jarvis.v2.hud_install`, Python source that *invokes* the
installer (importing or reading from it is not), and shells, `eval`,
here-documents and substitutions that bash would run (never inside single
quotes or a quoted here-document). A jarvis argv word only known at run time —
an expansion the line did not assign (an unset one vanishes), `$@`/`$*`, a
glob, a brace — counts as any words, so a line that *could* expand to an op
is refused too. A refusal that came from a substitution says to single-quote
text that only mentions the command. A line that only mentions it — `echo
…`, a `grep` for it, a single-quoted commit message — is untouched: a DENY
cannot be approved, so a false one would block the owner too. Not seen: a
link made earlier under another name, `jarvis $(echo hud) install`, a
launcher or module name from the environment, a Makefile, alias or script.
Separately, **Jarvis's own CLI is never auto-approved** in any spelling:
`python -m jarvis…`, the venv's python, `uv run`/`poetry run`/`pipx run
jarvis` are ASK in `rules.decide`, as bare `jarvis` always was. The same detector refuses `jarvis hud rebuild`,
`jarvis restart` and `jarvis daemon2 restart` (2026-10-11, `v2/owner_ops.py`),
a `systemctl` verb (or a systemd D-Bus call) that changes `jarvis-daemon2` —
reads, `reset-failed` and `daemon-reload` stay untouched, and `systemctl
--user set-/unset-/import-environment` are asked — and, the owner's choice the
same day, a `pkill -f`, `killall -r` or `pgrep -f`/`pidof`-fed `kill` whose
pattern matches the daemon's command line at `daemon2` (`pkill -f jarvis`
stays ALLOW; a literal `kill <pid>` and kills by process name are not seen);
since review 2 also a `kill` fed the daemon's PID by `systemctl show|status`
of its unit, a D-Bus read of it, its cgroup's `cgroup.procs`, `lsof`/`fuser`
on its ports, `ps | grep`, a grep over `/proc/*/cmdline` or `ss -p`/`netstat
-p` on its ports, plus `fuser -k` on its ports and `pkill --cgroup|--pidfile`
naming its unit. A source is judged only when a fed `kill` is on the line,
and a kill pattern whose matching cost cannot be bounded is never matched
(`re` holds the GIL): it is too complex to judge, so asked — never DENY,
never covered by an allowlist entry, always-ask in v2.
A shell write **into** the install root (`cp`, `rsync`, a
redirect) is asked, never auto-approved or covered by an allowlist entry. A
shell can still write there in a form nobody sees — the accepted shell gap.

**`GET /status` gains `hud_build`, `hud_git_head`, `hud_state` and
`hud_passed_over`**: what `GET /` actually serves — the build's id (`"dev"`
under `JARVIS_HUD_DEV=1`, `null` when nothing intact), the commit it was
installed from, `"installed"`, `"fallback"`, `"dev"`, `"not installed"`,
`"failed verification"` or `"refused: …"`, and the newer builds passed over.
daemon2 logs the same at start-up, and **warns** in dev mode, on a fallback,
when nothing is served, when the build was installed with uncommitted
changes, and when `hud/` changed between its commit and the checkout's HEAD
(`git diff --quiet <commit> HEAD -- hud`, so a Python-only merge is quiet).


## Additions 2026-10-11 (the editor's navigation — ED-7)

Every route here is **owner-only** (`projects.owner_only`: the HUD listener
and the HUD's own page, never the API listener), checked before the body is
read; `jarvis/v2/editor_nav.py` holds them. None keeps a copy of a rule: what
a path is comes from `editor_policy`, where it may go from `hud_api.scope`,
a search's protected-file filter from `tools.search.refused` (the one
`grep_files` uses), and a delete goes to `trash.Trash.put`.

- `GET /projects/{id}/files` → `{"files": [rel, …], "env": [rel, …],
  "truncated": bool, "source": "git"|"walk"}`: what quick open may offer.
  In a repository, `git ls-files --cached --others --exclude-standard`
  (`gitops._git`, hardened: no hooks, fsmonitor or pager), so `.gitignore`d
  build output is not listed; elsewhere a walk. **Dotfiles are listed**
  (`.github/…`, `.clang-format`, env files — named in `env` for their lock).
  Never listed: a withheld file, anything in a credential folder (never
  entered), the gate's own state, `SKIP_DIRS` (`node_modules`, `.git`,
  `.venv`, `.jarvis` …), a symlinked folder (never entered), and a symlink
  whose target is outside the project or not a regular file. At most 50,000
  files, 20 folders deep and 5 s (`truncated: true` past any). 403 when the
  project's root is itself a credential folder.
- `POST /projects/{id}/search` `{"query", "regex"?, "case"?, "word"?,
  "glob"?, "ignore"?}` → `{"matches": [{"path", "line", "text", "ranges":
  [[start, end], …]}], "files", "truncated", "timed_out", "backend",
  "limit"}`. Find in files on `tools.search.search_bounded`, the core
  `grep_files` also uses, so **both refuse exactly the same files**:
  `secrets.PROTECTED_NAMES` and every credential env file
  (`secrets.is_env_credential`) — grep_files gained the env half with this
  route (`tools.search.refused`). On top of it a hit is shown only when
  `scope` places it in the project and `editor_policy` says it is not
  withheld, the gate's state or an env file. `query` 1–1,000 characters, a
  literal unless `regex`; `case` is case-sensitive (default off); `word`
  whole words; `glob` a ripgrep glob (`*.cpp`, `src/**`); `ignore` respects
  `.gitignore` (default **on**; off searches ignored files too). Hidden files
  are searched, `SKIP_DIRS` never. At most **2,000 matches and 10 s**
  (`truncated` / `timed_out`); a line is cut at 1,000 characters; `ranges`
  are UTF-16 offsets. **400 on the Inbox** (it is the whole home folder), on
  a pattern neither engine accepts, and on any other key.
- `POST /projects/{id}/fs` `{"op": "new_file"|"new_folder"|"rename"|"delete",
  "path", "to"?}` → `{"ok", "op", "path", "abs", "kind"?, "from"?,
  "from_abs"?, "dir"?, "trash"?}`. `path` and `to` are relative to the
  project; each must pass `scope` (no `..`, no `SKIP_DIRS` component, inside
  the project lexically and resolved — so a symlink out is refused) and every
  name must be a real one (no control or invisible character; on `/mnt/<drive>`
  nothing Windows reserves or cannot hold). **Refused (403)**: a path
  `editor_policy` calls the gate's state, withheld (a credential bundle by
  name, anything in a credential folder, a login file) or the safety layer
  (SELF_PROTECTED, git's internals and configuration, the HUD install root and
  `config.HUD_DIST`), and a **folder that holds any of those** — by absolute
  path wherever it sits below (the gate's files, the credential folders and
  login files, the HUD roots, Jarvis's SELF_PROTECTED files) and by a walk of
  at most 20,000 entries that never follows a symlink or enters `SKIP_DIRS`
  (past the cap the folder is refused rather than judged in part). A project
  root itself is never renamed or deleted.
  - `new_file` makes an empty file (0644; a credential env file 0600 — the
    owner may make one); `new_folder` a folder; both make missing parents and
    are 409 when something is there.
  - `rename` (a move too) **never replaces anything**: 409 when `to` exists
    (a case-only rename on a case-folding drive is allowed), 400 when its
    folder is missing or a folder would go inside itself. **An env file keeps
    an env name**: renamed to a name Jarvis can read it is 403 (the owner's
    own shell can do that on purpose). A file renamed *to* an env name becomes
    one. The answer's `abs`/`from_abs` let the window move its buffers.
  - `delete` **moves to the trash**, never an unlink: the freedesktop home
    trash with a `.trashinfo` (`$XDG_DATA_HOME/Trash`), or the Windows Recycle
    Bin for a `/mnt/<drive>` path (`trash.Trash.put`, under a `Trash` owned by
    the project, so Jarvis's own retention purge never empties it). 404 when
    nothing is there.
  - One explorer operation at a time (its own lock, not the daemon's: a
    Recycle Bin move can take PowerShell a while).

**The review of PR #53 (same day):** `POST …/search` also takes `key` (the
searching view's; its newer search cancels its older one, which answers
`cancelled: true`), answers **429** past four searches at once, and says
`partial: true` when ripgrep could not read some files. Every match and span
comes from ripgrep (`--json`); the daemon never runs the pattern itself, so
**without ripgrep `regex: true` is a 400** and a literal is matched as text.
`POST …/fs` refuses (403) a `~` path, anything in Jarvis's own folders
(`editor_policy.jarvis_dirs()`), and a rename or delete of any project's folder
(extra folders included, archived projects too) or an ancestor of one; it acts
through directory fds opened without following links, from a project folder
opened before anything is judged (a link on the way is a 403; a project folder
that moved while it was checked is a 409), renames without replacing, and moves
a deleted item into the trash through fds too (a trash folder reached through a
link is a 409, and nothing moves; a path on a Windows drive goes to the Recycle
Bin by path). A second deleted item under a protected or env name keeps its
name in a `jarvis-deleted[.<n>]` folder; anything else is numbered
(`notes.txt.2`). A bad file filter is a 400 that says so. The window matches an
open buffer to a row by the project's `root_real` (#50).

The window's half (`hud/src/components/QuickOpen.tsx`, `SearchView.tsx`,
`EditorStatus.tsx`, `FileTab.tsx`'s explorer): keys and rules in CLAUDE.md's
ED-7 paragraph. Find in files sends the query, the flags and the glob to the
search route and nothing else; quick open lists once per project for 15 s.

## Additions 2026-10-11 (`jarvis restart` and `jarvis hud rebuild` — PR #48)

**`GET /status` gains `approvals_pending`**: the number of broker approvals
waiting on the owner (`PendingApprovals.pending()`, a turn's and the escape
hatch's, which asks after its turn has finished), an integer ≥ 0 beside
`turns_running`; 0 when the broker cannot be read. It is read before the
daemon's own lock (the broker has one of its own). `jarvis restart` and
`jarvis hud rebuild --restart` refuse, exit 3, while it or `turns_running` is
non-zero (a restart denies what waits), unless `--force`. Read-only, open like
the rest of `/status`; the HUD does not use it.

**`GET /status` gains `pid`** (review 2 of PR #48): the daemon's process id.
`jarvis restart` compares it with systemd's `MainPID` for `jarvis-daemon2`
before trusting what `/status` says about running turns: a different v2
daemon answering on this shell's `JARVIS_DAEMON_PORT` (a test daemon, one
started by hand) is not the unit's, so its idle answer is "can't check"
(exit 3 unless `--force`), and after the restart a daemon that is not the
new MainPID does not verify it (exit 4). A `/status` without `pid` (a daemon
from before this change) is trusted as before.


## Additions 2026-10-11 (tree-sitter colour — ED-5)

Editor plan `docs/plans/2026-10-09-hud-editor-plan.md` §2.6. No new route;
two things change on the wire.

**The policy's `script-src` is `'self' 'wasm-unsafe-eval'`** (one header,
`hud_api.content_security_policy`, as ED-4 set out): WebAssembly may compile
in the page and in a worker — a worker takes its policy from its own
script's `/assets/*` response — and nothing else is widened: `eval`, `new
Function` and a string `setTimeout` are still refused, and a response may
narrow it away (`script-src 'self'`). The syntax worker is the only code
that compiles WebAssembly, and only the runtime and the grammars its loader's
map names (`hud/src/lib/syntax/assets.ts`), fetched from `/assets/` on the
HUD's origin; any other URL is refused before a request is made.

**`/assets/*.wasm`** — the tree-sitter runtime and thirteen grammars, under
their Vite content-hashed names — are served like every other installed
asset (hash-checked on every read, `jarvis hud install`), as
`application/wasm` with the full policy. They are fetched only when a file of
their language first opens.

The HUD's test hooks for it are read-only and not an API:
`window.__hudSyntax.states()`, `.probe(url)` (asks the loader's gate, never
fetches) and `.loaded()` (the grammars compiled).

## Additions 2026-10-12 (the git gutter and the explorer's git colours — ED-9)

Editor plan §2.8 ("Git gutter, explorer git colours"). Both routes are
**owner-only** (`projects.owner_only`: the HUD listener and the HUD's own
page), checked in `hud_api.route` **before anything else** — the project is
not even looked up for anyone else, so the API listener and a bare request
get 403 whatever the id. `jarvis/v2/editor_git.py` holds them. Git runs only
through `gitops._git` (an absolute system git, every `GIT_*` variable
dropped, hooks, fsmonitor, the pager and auto-gc off, `GIT_OPTIONAL_LOCKS=0`
so a status never rewrites the index), and **a repository whose own config
names a program for git to run, or moves where it works**
(`gitops._risky_config`: a filter, textconv, `core.sshCommand`,
`core.worktree`, `include.path` …), **is refused by both routes (403)**, as
`gitops` refuses it everywhere.

- `GET /projects/{id}/git/base?path=` → `{"path", "state", "head", "text",
  "size"?, "renamed"?}`: what HEAD holds for a project file, the gutter's
  base. `path` is placed by `hud_api.scope` and judged **before git runs**:
  the gate's state, anything `editor_policy` withholds (no gate exemption)
  and every credential env file (`secrets.is_env_credential`) are **403**,
  as written or where they resolve — **and so is a file whose name in git is
  one of those**: a staged rename (`git diff-index --cached -M HEAD`, which
  reports no copies — a staged copy is `new`) serves its source's HEAD text,
  so the source is judged too (`git mv .env notes.txt` makes `notes.txt` a
  403; its text is never sent). **The source must also be in the project**,
  placed as `hud_api.scope` places a path (under one of its roots, no
  `SKIP_DIRS` or `.git` component): a file moved in from outside the
  project, or out of `node_modules`, answers `new` and nothing of its source
  is sent. HEAD's blob is read **by object id** (`git cat-file blob <oid>`),
  so no path-based attribute (textconv, a filter, an eol conversion) applies;
  its size is read from `ls-tree -l` first and capped at the editor's own
  read cap (2 MiB), and its bytes are judged by the editor's own rule
  (`editor_policy.is_text`, a strict UTF-8 decode). `state`: `ok` (with
  `text`, as stored — CRLF kept, a byte-order mark kept; the window compares
  lines), `new` (HEAD does not hold it: untracked, newly added, a copy, or a
  rename from outside the project — no gutter), `no_repo`, `no_head` (no
  commit yet), `binary` (a NUL in the first 8 KiB or bytes that are not UTF-8
  — no gutter; a U+FFFD the file holds is text), `too_large`, `not_a_file`
  (a symlink, a submodule, a folder in HEAD) or `unavailable` (git could not
  say). `head` is HEAD's commit id; `text` is null unless `state` is `ok`.
  404 when the file is not there; 403 for a path outside the project.
  **Since 2026-10-12** (the Diff pane's uncommitted changes, below) a file
  **deleted from the working tree** — nothing at all at the path, not even a
  broken link — answers what HEAD holds, with `"deleted": true`; it is
  judged exactly as a file that is there (every refusal above, before git
  runs), and a path HEAD does not hold either is still 404 (so are a folder,
  a broken link and anything else there that is not a file).
- `GET /projects/{id}/git/status` → `{"repo", "head", "branch", "files":
  {rel: state}, "dirs": {rel: state}, "truncated", "note"?}`: the explorer's
  colours. `git status --porcelain=v2 -z --branch --untracked-files=normal
  --ignored=traditional --renames --ignore-submodules=all -- .` at the
  project's root — **never into a submodule**, whose own config `_risky`
  did not read (a status that recursed would run a filter it names). Paths
  are relative to the project's root (a repository path outside it is
  dropped); `dirs` are folders git reported whole (`untracked`,
  `ignored`), `""` the project's own folder. `state` is `modified`, `added`,
  `untracked`, `ignored`, `renamed` (a staged rename; its source is never
  returned), `deleted` or `conflict`. **Judged before they are returned**:
  a path under a `SKIP_DIRS` folder, withheld (a credential bundle by name,
  in a credential folder, a login file) or the gate's state is dropped.
  `head` is null before the first commit; `repo: false` outside a
  repository. **Cached for 2 s per root** — or three times what the status
  took, when that is longer (a slow `/mnt/c` repository rests between runs)
  — a refusal too, and run once at a time per root; at most 20,000 entries (`truncated`) and 5 s (`truncated`
  with a `note`). 403 when the project's root is itself a credential folder.

The window's half (`hud/src/lib/linediff.ts`, `lib/gitMarks.ts`,
`state/gitGutter.ts`, `state/gitStatus.ts`): the gutter diffs HEAD's text
against the **buffer** (Myers, bounded), only for a `text` or `safety`
buffer — never an env file — and asks for HEAD's text again when the status
poll's `head` moves, and every 10 s while the window is visible for a file
whose base `head` is not the project's (a nested repository, the Inbox, an
extra folder: the poll cannot see that HEAD move); the status is polled
every 3 s per project while a tree or a gutter shows it. Rules in CLAUDE.md's
ED-9 paragraph.

## Additions 2026-10-12 (folding from the tree — ED-6)

Editor plan §2.6, "Folding from the tree". No route and nothing on the wire
changes: the fold queries (nvim-treesitter's `folds.scm`, vendored at ED-5's
pin) are bundled text inside the syntax worker's own `/assets/` script, and
folds are computed in that worker from the tree it already holds. No new
`.wasm`, no policy change. `window.__hudSyntax.states()` gains `folds`: how
many fold answers from the tree were handed to Monaco for that model (every
other answer is indentation folding).


## Additions 2026-10-12 (editor settings and `.editorconfig` — ED-8)

Editor plan §2.8. **No new route.** The editor settings are the window's own
(localStorage `jarvis.hud.editor`: `{fontFamily, fontSize, tabSize,
insertSpaces, wordWrap, wordWrapMarkdown, minimap, stickyScroll, whitespace,
bracketPairs, cursorStyle, vim}`, each field checked alone on load); nothing
about them reaches the daemon.

**`.editorconfig` uses ED-2's owner-only routes**, as the editor's buffer
opens a file at `<dir>/<name>` in project `{id}`:
- `POST /projects/{id}/files/stat {"paths": ["<dir>/.editorconfig",
  "<parent>/.editorconfig", …, ".editorconfig"]}` — the file's folder and
  each folder above it **up to the project's root, never above** (at most 32,
  within the route's 100), once per folder every 3 s at most;
- `GET /projects/{id}/file?path=<dir>/.editorconfig` for each that stat says
  exists as `kind: "text"` — once per etag, every one on the walk; they are
  applied nearest first, stopping at one whose preamble says `root = true`. A
  withheld, non-text, missing or refused answer is no configuration, and one
  whose stat `size` is over 64 KB is never read. A path's resolution is kept
  per (path, each config's folder and etag), so restored tabs ask nothing
  twice. Judging happens in the window, one resolution per task, under a
  5 ms clock; a config that runs it out of time twice, or is over a cap,
  configures nothing until its etag changes.

Neither sends any file's content. What the files say sets the buffer's
indentation and, for a file with no line break, the line ending its new
lines take; **a save never carries anything `.editorconfig` asked for** — no
trimmed whitespace, final newline or converted line endings.

Test hook, read-only and not an API: `window.__hud.editorSettings()` →
`{settings, stored, editors: [{uri, fontFamily, fontSize, wordWrap, minimap,
stickyScroll, renderWhitespace, bracketPairs, cursorStyle, vim, model:
{insertSpaces, tabSize, indentSize, eol}}]}` — never any text.

## Additions 2026-10-12 (chat titles)

The owner: "right now each chat just displays its id … keep the id in the
three dots but have the main display be the title". No new route.

- **Threads carry `title_source`**: `""` (nobody named it yet, or a v1
  migration's title), `"auto"` (the daemon's titler) or `"owner"` (a
  rename). `PATCH /threads/{id}` `{title}` sets `"owner"` — a rename to the
  title it already has does too. Absent from an older daemon; the HUD reads
  absent as `""`.
- **Chat titles are generated** (`jarvis/v2/titles.py`): at a chat
  thread's first owner message, and at start-up for untitled chats with an
  owner message (archived ones included), the daemon names the chat from the
  owner's typed words — Codex `gpt-6-luna` (`JARVIS_TITLE_CODEX_MODEL`) when
  Codex is healthy and the account lists it, else the default OpenRouter
  model. Never a task's thread. An automatic title is written only onto an
  empty title (a rename always wins), unique in the project like a rename's.
- **`thread_updated`** for a title, from a rename or the titler, is
  `{thread_id, title, title_source, changed: ["title"]}` (no record: the HUD
  re-reads `/threads`). `title_source` is new on it. The Discord mirror
  renames the chat's Discord thread on it, **except while the chat or its
  project is archived**: then the new name waits and is applied on
  `thread_restored` / `project_restored` (a rename would unarchive the
  Discord thread).
- **The Archive view** names an archived chat by its title (or "Untitled"),
  its id as the tooltip (`archived-thread-name-<id>`).
- **The HUD** never shows a chat's id as its name: an untitled row reads
  "New thread" (`turns` 0) or "Untitled", and the id is the ⋯ menu's "Copy
  id" (`tmenu-copyid`, the id shown in `tmenu-id`). A title is text.

## Additions 2026-10-12 (the Browser's engine — BR-1a)

The owner's built-in Browser, backend only (plan
`docs/plans/2026-10-10-hud-browser-plan.md` §2–§4, amendment 1 §C): a
daemon-owned Google Chrome behind a SOCKS gate. The stream, input, dialogs,
downloads and the tab strip are BR-1b's; profile management is BR-1c's.

**Every `/browser` route is the owner's**: `projects.owner_only` (the HUD
listener, and the HUD's own page — its Origin, or a same-origin GET with no
Origin) plus a strict loopback `Host`, checked before a body is read, a tab
is looked up or Chrome is started; anything else is **403**. No route takes
a query string (**400**); a URL travels only in a JSON body, and no refusal
ever contains one. Nothing starts Chrome but `POST /browser/tabs`.

| Route | Body | Answer |
|---|---|---|
| `GET /browser` | — | `/status` → `browser` (below) plus `profiles: [{id, label, created}]` and `settings` |
| `GET /browser/settings` | — | `{lan, local_servers}` |
| `PATCH /browser/settings` | `{lan?: bool, local_servers?: bool}`, at least one | `{lan, local_servers}`; writes `browser.json` (0600, protected state), keeping keys it does not know |
| `GET /browser/tabs` | — | `[{tab_id, profile, ctx, url, title}]` — the title cleaned to one line of 120; `[]` while nothing runs |
| `POST /browser/tabs` | `{profile?, ctx?, url?}` | **201** `{tab_id, profile, ctx, loaded, error?}` |
| `DELETE /browser/tabs/<id>` | — | `{tab_id, closed: true}` |
| `POST /browser/tabs/<id>/navigate` | exactly `{url}` | `{tab_id, loaded, error?}` |
| `POST /browser/tabs/<id>/history` | exactly `{action: "back" \| "forward" \| "reload"}` | `{tab_id, loaded, error?}` |
| `POST /browser/profiles/<id>/forget` | `{}` | `{profile, forgotten: true}` |

- **`profile`**: omitted means the first profile (**Main**, which the first
  start creates), `"private"`, or a `p-<8 hex>` id from `GET /browser`
  (404 if unknown, 400 if not that shape). **`ctx`** (8 hex) names a Private
  context: `profile: "private"` with no `ctx` makes a new off-the-record
  context and answers its id; with one, the tab opens in it (404 if gone).
  A context closes with its last tab.
- **The URL judge** runs first on every `url` (typed, stored, "Open in
  Browser"): exactly `about:blank`, or an absolute `http`/`https` URL with a
  host. Refused (**400**, one of fixed sentences): any other scheme (`file:`,
  `chrome:`, `chrome-untrusted:`, `devtools:`, `view-source:`,
  `chrome-extension:`, `javascript:`, `data:`, any `about:` but blank, `ftp:`,
  `ws:` …), a user name or password, a host two parsers read differently
  (`config.ambiguous_authority`), a control or invisible character, more than
  16 KiB, and **one of Jarvis's own ports** (`refused_ports`) in any loopback
  spelling (`localhost`, `*.localhost`, `127.1`, `0x7f000001`, `[::1]`,
  `[::ffff:127.0.0.1]`, `0.0.0.0`, a trailing dot …).
- **A URL on this machine opens its port** (review of PR #69, the lead's
  decision): a `url` naming this machine in any loopback spelling, sent to
  `POST /browser/tabs` or `…/navigate`, opens that port in the gate from
  then until the Browser stops (never one of `refused_ports`). Loopback is
  otherwise closed like the LAN: a page's own move, `…/history`, and anything
  that is not these two owner routes never open a port. `local_servers: true`
  in the settings opens every loopback port that is not Jarvis's.
- **An unknown field** in any body (here and on every other route) is
  **400** `unknown fields (N)`: the count, never the names.
- **`loaded: false`** with **`error`** — Chrome's `net::ERR_…` code only,
  never its message (it names the URL) — when a load fails; a connection the
  gate refused reads `net::ERR_SOCKS_CONNECTION_FAILED`. Navigation answers
  once the load commits.
- **409** sentences: Chrome is not installed, the Browser's folder is a
  symlink, not ours or open to others (it must be 0700), `profiles.json` is
  unreadable, Chrome could not start, the tab cap (`JARVIS_BROWSER_TAB_CAP`,
  12) is reached, the Browser is stopping. **504** when the Browser does not
  answer in time.
- **forget** ("Sign out of everything", plan §3): that profile's Chrome
  stops, its folder and downloads are removed and an empty 0700 folder made;
  the label stays. `private` closes every Private context.

**`GET /status`** gains:

- **`browser`**: `{available, running, version, instances: [{profile, tabs,
  contexts}], tabs, tab_cap, lan, local_servers, loopback_opened,
  landing_guard, gate}` — `loopback_opened` is how many loopback ports the
  owner opened (never which); `gate` is `{port, connections, allowed,
  failed, open, refused: {class: count}}` while running, else null. Profile
  ids only: **no URL, title or label**.
- **`refused_ports`**: every port the Browser's gate refuses on this machine
  — `config.control_ports()` (8402, 8404, 8405, 8406, configured and
  default), this daemon's `hud_port` and `api_port`, and 8403 unless it is
  this daemon's `workshop_port`. The HUD's Direct (Preview) judge reads it.

**The gate** refuses, for every connection Chrome makes, Jarvis's ports and
its own on every address; a name that resolves to loopback; a loopback port
the owner has not opened (unless `local_servers`); private addresses unless
`lan` is true; a name answering with another kind of address than the one
it was first allowed on; multicast, reserved and unresolvable destinations;
a lookup past the per-domain cap. It counts each refusal by class
(`jarvis_port`, `own_port`, `name_to_loopback`, `loopback_closed`,
`private_network`, `rebinding`, `not_a_host`, `unresolvable`, `dns_busy`,
`bad_request`, `unsupported`, `busy`) and logs nothing else about it.
Chrome sends no UDP at all (no QUIC; WebRTC held to proxied transports, so
no ICE candidate, STUN or mDNS). **Nothing about browsing goes on the bus**
in BR-1a.

## Additions 2026-10-12 (File reads as the editor, Preview is the Browser — BR-3a)

The owner: "also rename "File" to "Editor" and "Preview" to "Browser"".
Browser plan §7 (`docs/plans/2026-10-10-hud-browser-plan.md`), cut by
amendment 1 to the rename and the stored-state migration. No route changes;
Python is untouched (`preview_route`, `preview_only` and `workshop_port` name
the workshop listener, 8403, which these docs now call **the workshop
origin**).

- **Preview → Browser, in name and id.** The view is `"browser"`; the pane
  spec's URL is `browserUrl` (`keepOrigin` unchanged); `lib/browser.ts`
  (`judgeBrowserUrl`), `BrowserTab.tsx`, `.browserframe`. Test ids:
  `tab-browser`, `browser-url`, `browser-go`, `browser-project`,
  `browser-refused`, `browser-frame`, `browser-keep-origin`. The terminal
  link menu's items are `term-link-pane` ("Open in Browser") and
  `term-link-tab` ("Open in a Chrome tab"). Which links get the menu is
  unchanged (loopback only; plan B-11 waits for Remote mode).
- **The stored layout migrates as it parses**, under the same
  `jarvis.hud.workspace` key: `view: "preview"` reads as `"browser"`,
  `previewUrl` as `browserUrl` (only when there is no valid `browserUrl`),
  and a `keepOrigin` is kept only if it is still exactly that URL's origin
  on an allowed port (`storableKeepOrigin`, then judged again on load as
  before). A save is a parse, so it writes the new names only. A rolled-back
  HUD reads such a pane as its default view and loses its URL; nothing worse.
- **File → editor, in name only.** The view id stays `"file"` — a stored
  `"file"` pane, `tab-file` and every `pane-N-…` id are unchanged.
  `lib/workspace.ts` `VIEW_LABEL` names each view (`file` → "editor",
  `browser` → "browser"); the strip's buttons, its compact menu and a pane's
  `aria-label` (`Pane N: editor`) read it, and the strip's CSS capitalises
  it. The palette's "Show Files" is "Show Editor", and "No room to open …"
  says "switch one to the editor".

## Additions 2026-10-12 (the Diff pane's uncommitted changes)

No new route. A Diff pane with no task to show lists its project's
uncommitted changes from ED-9's two owner-only routes and the editor's read:

- `GET /projects/{id}/git/status` (unchanged) is the list, through the one
  status poller (`state/gitStatus.ts`), watched only while the pane is
  drawn. `ignored` entries are not listed; a folder in `dirs` reported
  `untracked` is one row; `""` untracked (the whole project folder) is said
  in a sentence.
- `GET /projects/{id}/git/base?path=` is the left side, asked again when the
  status's `head` moves. It now answers a **deleted** file too (`"deleted":
  true`, above).
- `GET /projects/{id}/file?path=` (ED-2's read, unchanged) is the right side
  when no buffer holds the file — asked only after `git/base` answered
  without a refusal, so a file that is an env file only where it resolves is
  never read for a diff (`hud_v2_scm_check`'s `link.txt` pins the order:
  with the read no longer waiting on HEAD's answer it is sent). When a
  buffer holds it, the right side is that buffer's own Monaco model and
  nothing is read.
- **An env file by name** (`.env`, `.env.*`, never `.env.example`) is listed
  and **never asked about**: no `git/base`, no read. A `git/base` 403 naming
  an env file shows the same sentence and stops there.

Rules in CLAUDE.md's "The Diff pane shows the project's uncommitted changes"
paragraph.
