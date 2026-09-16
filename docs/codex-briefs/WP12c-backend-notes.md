# WP12c backend

Implemented the four Additions 2026-09-16 routes/tools from docs/hud-api.md.
PATCH /threads/{id} moves only chat threads; task-owned threads return the
specified 409. The record, title, log, provider handle and session id survive.
The live session's project is updated under the daemon lock so later usage
and finish events cannot restore the old project. Publishes thread_moved.

GET /fs/dirs requires an absolute path under HOME or /mnt/<drive>.
Both lexical and resolved paths are checked; escaping symlinks are excluded.
Returns sorted directory names only, omitting dot names; parent is null at
an allowed root. No file contents are read.

POST /schedules/preview validates exactly one cron/every_s and returns three
Chicago ISO times plus describe(). It never writes a schedule.
parse_when returns {cron: str} or {every_s: int}, or None without guessing.
Table-driven patterns cover daily/weekday/weekend/named-day clock times,
24-hour and am/pm times, positive integer intervals, and supported cron.
describe reads common clocks naturally and other cron as explicit fields.
The existing cron DST policy is preserved: skip gaps, fire once in folds.

Three @tool functions derive schemas from Annotated hints, are not dangerous,
and use the daemon HTTP API for schedule mutations/listing. Thus MCP and fast
path share the scheduler lock, persistent records and bus events.
Project lookup uses the configured v2 store, accepts id or case-insensitive
name, and refuses ambiguous names. Optional runtime.project_id()/thread_id()
accessors supply the caller; neither exists in today's runtime, so defaults
go to the real Inbox. Explicit project always wins.
Shared store save/delete publish schedule_created/updated/deleted with
data.schedule_id and project_id; schedule_fired remains unchanged.

Validation uses /home/johnw/projects/Jarvis/.venv/bin/python with
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.; loopback tests require escalation.
HTTP coverage includes moves, subsequent turns/resume, picker scope/symlinks,
18 timing phrases, eight refusals, exact next-three/DST and CRUD events.
Tool tests use real dispatch against a temporary loopback daemon and store,
including scheduler pickup, caller/inbox placement and fresh-process imports.
Full suite logs: /tmp/wp12c-check-logs/.
Registration imports pending permission to extend the two-frozensets edit rule;
the first sweep passed 13/16 suites, with only missing-registration failures.
