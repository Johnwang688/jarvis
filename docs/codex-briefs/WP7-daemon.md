# WP7 — daemon v2 and the local API

You are Codex, implementing work package 7 of the Jarvis v2 redesign, in a
git worktree on branch `jarvis/wp7-daemon`. Claude leads; this brief is your
entire scope. Read first, in this order:

1. `docs/jarvis-v2-design.md` — §3 (one daemon, thin surfaces), §4, §5.1–5.2,
   §8 (you build the seams the Router plugs into, not the Router), §14 row
   WP7, and the WP1 follow-ups in the merge commit `git log --grep WP1`.
2. `jarvis/v2/model.py`, `provider.py` (**fixed**), `stores.py`, `worktrees.py`,
   `providers/fastpath.py`, `providers/codex.py` (all merged; use, do not edit).
3. `jarvis/daemon.py` (v1) — the shape to replace: single-instance lock on
   `config.DAEMON_PORT` (8405), `/status`, clean shutdown releasing waiters.
   `jarvis/face/server.py` for the v1 SSE broadcast pattern
   (`ThreadingHTTPServer`, `/events`). `tests/daemon_check.py` for the test
   style (fake listener, loopback only, dead ports for anything real).
4. `docs/codex-briefs/WP1-notes.md`, `WP4-notes.md`, `WP2-notes.md`.

## Deliverables

### `jarvis/v2/daemon.py`

One process owning everything with state. `class Daemon(stores: Stores,
providers: dict[ProviderName, Provider], permit_factory, port: int)`:

- **Providers are constructed by the caller** and passed in (tests pass
  fakes; `main()` builds fast + codex + claude lazily, importing each
  provider module only when the roster asks for it — a missing SDK must not
  stop the daemon, it must mark that provider `unavailable` in `/status`).
- **Threads**: `open_thread(project_id, role, provider, brief)` creates the
  `Thread` (WP1), starts or resumes the provider session, keeps the
  `SessionHandle` in memory keyed by thread id. `send(thread_id, message)`
  runs `provider.send` on a per-thread worker thread, appends every `Event`
  to the thread's log (text and tool events; not deltas), updates
  `Thread.turns/cost_usd/tokens` from `USAGE`, saves, and **fans every
  event out** to subscribers. One turn per thread at a time; a second
  `send` while one runs is refused with a clear error. `interrupt`,
  `answer`, `close_thread` pass through.
- **Fan-out**: an in-process `EventBus` — `subscribe(filter) -> queue`,
  `publish(Event)` — that the HTTP layer and, later, Discord (WP10) and
  the task runner (WP11) attach to. A slow subscriber never blocks a
  turn (bounded queue, drop-oldest with a `dropped` counter the subscriber
  can read).
- **Permission callback**: `permit_factory(thread_id, brief) ->
  PermissionCallback`. The daemon itself supplies **no policy**: `main()`
  wires v1's `permissions.gate` over a placeholder that denies with reason
  `"WP5 not landed"`. The five layers are WP5's; you provide the seam and
  the test proves a callback bound for thread A is never called for B.
- **Lock and lifecycle**: bind `port` on loopback only; a live `/status`
  answering there means another instance → refuse to start, same as v1.
  `stop()` interrupts running turns, closes handles, drains subscribers
  with a final `{"kind": "shutdown"}` and joins workers within a bound.
- **The WP1 follow-up**: wrap `stores.*.list()` so one corrupt file logs a
  warning naming the path and is skipped, instead of hiding every record.
  Do it in the daemon, not the store (the store's contract stays strict).

### The local API (`ThreadingHTTPServer`, loopback, JSON; SSE for events)

```
GET  /status                         -> {version, uptime, providers: {name: {ok, reason}}, threads_open, turns_running}
GET  /projects · POST /projects · GET /projects/{id} · PATCH /projects/{id}
GET  /threads?project=… · POST /threads {project_id, role, provider, brief}
POST /threads/{id}/send {text, images?}   -> {turn_id}  (202; events on /events)
POST /threads/{id}/interrupt · POST /threads/{id}/answer {req_id, decision|text}
GET  /threads/{id}/log?after=N
GET  /tasks?project=… · POST /tasks {project_id, brief} (INTAKE only) · GET /tasks/{id}
POST /tasks/{id}/worktree (ensure) · GET /tasks/{id}/worktree (status) · DELETE …?force=
GET  /events?thread=…&project=…      -> SSE stream of Event as JSON, plus thread/task lifecycle records
```

No `/model`, `/approve`, `/avatar`, Discord or HUD routes — those are
other packages. Every handler catches everything and returns JSON errors
with the right status; a handler exception must never kill the server
thread. Same-origin is not a concern yet (loopback only, no browser
surface); say so in a comment where WP12 will need to add it.

### `jarvis/cli.py` — one additive entry point

`jarvis daemon2` (leave v1 `jarvis daemon` untouched) → `daemon.main()`.

### `tests/v2/daemon_check.py`

Free: fake providers (scripted `Event` generators, one that blocks until
told, one that raises), stores on a temp root, `V2_DATA_DIR` never the real
one, a dead port for the "other instance" probe. Cover: two SSE clients see
the same events in order; a client disconnecting mid-turn kills nothing and
the turn completes; per-thread serialization (second `send` refused);
`USAGE` updates the thread record; the log holds text and tool events and
no deltas; `interrupt` reaches the provider and the turn ends
`interrupted`; a raising provider yields `ERROR` and the thread stays
usable; permit isolation between threads; slow subscriber dropped-oldest
counter; single-instance refusal; `stop()` releases a blocked SSE client
and a blocked turn within the bound; every route's happy path and 400/404/
409 paths; the corrupt-file skip logs the path and returns the rest; a
provider whose import fails shows as unavailable in `/status`.

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/daemon_check.py`
Then the four existing `tests/v2/*_check.py` and `tests/daemon_check.py`.

## Rules

Stay inside `jarvis/v2/daemon.py`, `jarvis/v2/bus.py` (if you split it),
`jarvis/cli.py` (one subcommand, additive), `tests/v2/daemon_check.py`, and
`docs/codex-briefs/WP7-notes.md`. Do not edit the fixed interfaces, the
merged v2 modules, v1 code beyond the subcommand, or CLAUDE.md. Stdlib only.
When green, commit with a message starting `v2 WP7: daemon and local API`
ending with `Co-Authored-By: Codex <noreply@openai.com>`. Do not push.
Finish with `WP7-notes.md` (under 70 lines): what you built, the route
table as implemented, test commands and last lines, workarounds, proposed
interface changes.
