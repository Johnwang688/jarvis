# WP7 — daemon v2 and local API

## Built
- Injected providers and per-thread permission callbacks; no Router or WP5 policy.
- Lazy roster loader catches import/SDK failures and exposes unavailable health.
- Per-thread workers reject overlapping sends; interrupt, answer, close, resume.
- Brief sidecars preserve resume settings; structured event logs exclude text deltas.
- USAGE updates dollars/work tokens; turns count once on completion, including errors.
- Codex cumulative counters use high-water deltas; FastPath events are increments.
- Bounded EventBus queues drop oldest, expose `dropped`, isolate subscriber copies.
- Loopback ThreadingHTTPServer, JSON failures, SSE filtering and lifecycle records.
- Single-instance socket lock; v1-compatible health marker; bounded shutdown joins.
- Daemon-only safe_list warns with each corrupt path and preserves healthy records.
- CLI candidate is prepared and tested at `/tmp/wp7-cli.patch`, pending scope reply:
  this checkout uses `jarvis/__main__.py`; the brief permits nonexistent `jarvis/cli.py`.
  Candidate adds only daemon2 -> v2 daemon.main(), retaining v1 run/install dispatch.

## Routes implemented
| Method | Route | Result |
| --- | --- | --- |
| GET | /status | version, uptime, provider health, open/running counts |
| GET / POST | /projects | list / create (201) |
| GET / PATCH | /projects/{id} | read / update |
| GET / POST | /threads?project=… | list / open with structured Brief (201) |
| POST | /threads/{id}/send | text/images -> turn_id (202) |
| POST | /threads/{id}/interrupt | interrupt active turn |
| POST | /threads/{id}/answer | req_id + decision or text |
| GET | /threads/{id}/log?after=N | log with first N records skipped |
| GET / POST | /tasks?project=… | list / create INTAKE task (201) |
| GET | /tasks/{id} | task record |
| POST / GET / DELETE | /tasks/{id}/worktree?force=… | ensure / status / remove |
| GET | /events?thread=…&project=… | SSE, conjunctive filters, final shutdown |
Lists/logs return arrays; creates/updates return records. Worktree ensure/remove
return Task; status returns WorktreeStatus. force accepts true/false/1/0.
Lifecycle kinds: thread_opened/closed, task_created, task_worktree_ensured/removed.
Events carry project_id and turn_id alongside the fixed Event fields.

## Validation
Prefix every command below with:
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python`
| Script | Last lines (exit 0) |
| --- | --- |
| tests/v2/daemon_check.py | Ran 15 tests in 1.637s; OK |
| tests/v2/stores_check.py | all v2 store checks passed |
| tests/v2/worktrees_check.py | all v2 worktree checks passed |
| tests/v2/fastpath_check.py | all fast-path checks passed |
| tests/v2/codex_provider_check.py | Ran 23 tests in 5.442s; OK |
| tests/daemon_check.py | all daemon checks passed |
CLI candidate: mocked dispatch passes for daemon2, daemon, daemon install.
`git diff --check` passes. Socket suites required sandbox escalation for loopback;
all providers are fake, V2_DATA_DIR is temporary, no live inference or Discord.

## Workarounds / proposed interface changes
- Fixed stores expose only text log(); use their _append/_write_bytes/_validate
  primitives without editing them. Propose public append_event and validation APIs.
- Propose explicit USAGE increment/cumulative semantics (Claude integration must
  confirm its convention); current provider names select the merged conventions.
- Propose a durable Brief store and cancellable permission callback context for WP5.
  Migrated threads without brief.json refuse automatic resume rather than guess.
- Stop waits at most two seconds plus HTTP poll/close overhead. Python cannot kill
  arbitrary provider/callback code: warn, detach daemon threads, suppress late events.
- The main placeholder uses v1 permissions.gate; fallback logs "WP5 not landed" and
  denies. Existing v1 gate allowlist/mode behavior remains until WP5 replaces it.
- Same-origin/Host enforcement is deferred to WP12 at the handler comment.
