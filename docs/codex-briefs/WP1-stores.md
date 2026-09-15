# WP1 — domain stores + v1 session migration

You are Codex, implementing work package 1 of the Jarvis v2 redesign. You are
working in a git worktree on branch `jarvis/wp1-stores`. Claude leads the
project; this brief is your entire scope. Read, in this order, before writing
anything:

1. `docs/jarvis-v2-design.md` — §4 (domain model), §8.3 (what the ledger
   counts; you only store it), §13 (what migrates), §14 row WP1.
2. `jarvis/v2/model.py` and `jarvis/v2/provider.py` — **fixed interfaces.
   Do not edit them.** If you believe a change is required, write the
   proposed diff and reasoning to `docs/codex-briefs/WP1-notes.md` and work
   around it; do not apply it.
3. `jarvis/sessions.py` — the v1 session format you migrate from
   (`~/.local/share/jarvis/sessions/<id>/{messages.json,log.jsonl,meta.json}`).
4. `tests/sessions_check.py` — the house test style: a plain script run with
   the venv python, asserting, printing one line per check, exit 1 on failure,
   every filesystem side effect pointed at a temp dir.

## Deliverables

`jarvis/v2/stores.py`:

- `Stores(root: Path)` holding `projects`, `threads`, `tasks`. Root defaults
  to `config.V2_DATA_DIR` (add that one constant to `jarvis/config.py`:
  `~/.local/share/jarvis/v2`, env `JARVIS_V2_DATA`). Every test passes an
  explicit temp root; nothing in the suite may touch the real one.
- Layout: `projects/<id>.json`, `threads/<id>/{thread.json,log.jsonl}`,
  `tasks/<id>/{task.json,journal.jsonl}`. `log.jsonl` is the append-only
  user/reply text log (the v1 `log.jsonl` shape); `journal.jsonl` is the
  runner-written task journal (v1 goals' idea): one JSON object per line with
  `at`, `event`, and free fields.
- CRUD per store: `create(...) -> obj` (mints the id: 8 hex chars, collision
  checked), `get(id)`, `list(**filters)`, `save(obj)` (atomic: write to a
  temp file in the same dir, `os.replace`), `delete(id)` only for tasks in a
  terminal state and threads with no task — anything else raises.
- `TaskStore.transition(task_id, new_state, *, reason="")` enforces
  `model.TRANSITIONS`, refuses illegal moves with a clear error, bumps
  `updated`, and journals the move. State is never set any other way from
  outside the store.
- `TaskStore.journal(task_id, event, **fields)` and `ThreadStore.log(thread_id,
  role, text)` append lines; `read_journal` / `read_log` return them.
- **The inbox project** exists exactly once: `stores.projects.inbox()`
  returns it, creating it on first call with `inbox=True`, name `Inbox`,
  root = the owner's home.
- **No empty directories**: a thread or task directory is created on the
  first save/append, not on `create()` (v1's "a session with nothing said
  never touches disk" rule). A `create()` followed by nothing leaves no
  directory.
- Serialization only through `model.to_json` / `model.from_json`. A corrupt
  file raises a `StoreError` naming the path; it never returns a half object.

`jarvis/v2/migrate.py`:

- `migrate_v1_sessions(v1_sessions_dir: Path, stores: Stores) -> list[str]`:
  every v1 session becomes a `Thread` in the inbox project with
  `role=CHAT`, `provider=FAST`, `provider_session_id=None`,
  `migrated_from=<v1 id>`, title/turns/cost/timestamps carried from
  `meta.json`, and the v1 `log.jsonl` copied verbatim as the thread's log.
  **Never modifies or deletes anything under the v1 directory.** Idempotent:
  a second run finds the existing `migrated_from` and skips. The v1
  `messages.json` transcript is copied to `threads/<id>/v1_messages.json`
  with any image payloads stripped (v1's `record()` already strips them;
  strip again defensively).
- Returns the new thread ids. Malformed v1 sessions are skipped with a
  printed warning and do not abort the run.

`tests/v2/stores_check.py`:

- Round-trip every model type through the stores; illegal transition
  refused, legal chain walked end to end; delete rules; atomic save leaves
  no temp files; the inbox is a singleton; **no empty directories** after a
  bare `create()`; a corrupt JSON file raises `StoreError` with the path;
  journal and log append and read back in order; migration of a copied
  fixture v1 session dir (build it in the test from
  `jarvis.sessions` — do not read the owner's real sessions), verbatim log,
  idempotent second run, v1 dir byte-identical afterwards (hash it before
  and after).
- Run it as: `PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/stores_check.py`
  (the venv is in the main checkout; PYTHONPATH makes your worktree shadow
  the editable install). Set `PYTHONDONTWRITEBYTECODE=1`.

## Rules

- Stay inside `jarvis/v2/`, `jarvis/config.py` (one constant), `tests/v2/`,
  and `docs/codex-briefs/WP1-notes.md`. Do not touch v1 code or CLAUDE.md.
- No new dependencies.
- Style: match the repo — type hints, docstrings that say *why*, no
  framework. Keep functions small.
- When the suite is green, commit on this branch with a message starting
  `v2 WP1: stores + v1 session migration` and ending with the line
  `Co-Authored-By: Codex <noreply@openai.com>`. Do not push.
- Finish by writing `docs/codex-briefs/WP1-notes.md`: what you built, the
  test command and its output's last lines, anything you had to work around,
  and any interface change you would propose. Keep it under 60 lines.
