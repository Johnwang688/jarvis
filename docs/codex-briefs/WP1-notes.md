# WP1 — domain stores + v1 session migration

## Built
- Added `config.V2_DATA_DIR` (`JARVIS_V2_DATA`, default `~/.local/share/jarvis/v2`).
- Project/thread/task stores use the fixed model serializers, collision-checked
  eight-hex ids, atomic JSON replacement, ordered JSONL logs and journals.
- Projects persist on create; threads/tasks remain in memory until save/append.
  `get()` returns a detached object or None; `list()` returns saved objects only.
  Filters match model fields exactly. Missing-id mutations raise `StoreError`.
- Inbox is singleton, named Inbox, rooted at the owner's home. Project deletion
  is refused; task/thread deletion follows terminal-state and attachment rules.
- Task transitions enforce the matrix, update timestamps and status.phase, and
  journal old_state/new_state/reason. `save()` refuses direct state changes.
- Migration preserves v1 logs byte-for-byte, converts Unix timestamps to UTC
  ISO strings (microsecond precision), retains metadata and strips image blocks
  recursively without dropping the first saved transcript message.
- Malformed sessions warn and skip; migrated ids make reruns idempotent.
  Failed imports clean up their thread; metadata publishes after both copies.
- Synthetic fixtures come from `jarvis.sessions` under temporary roots only.
  Checks cover all domain dataclasses, all 81 state pairs, failed atomic writes,
  append order, corruption, deletion, collisions, migration and source hashes.

## Validation
Command (exit 0):
```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/stores_check.py
```
Output's last lines:
```text
ok  migration: copied v1 fixture, metadata, verbatim log, images stripped, malformed skipped
ok  migration: rerun idempotent, source hashes unchanged, destination outside v1 enforced
ok  migration: failed copy leaves no partial thread and can be retried

all v2 store checks passed
```
`git diff --check` also passed.

## Workarounds / interface notes
- Fixed dataclass decoding does not validate field types; stores validate the
  decoded object recursively to reject malformed nested data with its path.
- V1 logs may lack a final newline; the next append supplies a separator.
- Writers are serialized within one process. JSON files replace atomically;
  task metadata and journal are separate writes, not a crash-atomic transaction.
- No interface changes proposed. `model.py` and `provider.py` are unchanged.
