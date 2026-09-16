# WP12c backend — thread move, directory picker, schedule preview, schedule tools

You are Codex, in a git worktree on branch `jarvis/wp12c-backend`. Read
`docs/hud-api.md` — the **Additions 2026-09-16** section is your entire
scope, and the rest of the file is context. Also read `jarvis/v2/hud_api.py`,
`jarvis/v2/schedules.py`, `jarvis/v2/stores.py`, `jarvis/v2/providers/fastpath.py`
(`FAST_TOOLS`, `_validate_toolset`), `jarvis/v2/mcp.py` (`MCP_TOOLS`),
`jarvis/v2/tools/propose.py` (how a v2 tool registers with the v1 `@tool`
decorator and reaches its slot), `docs/codex-briefs/WP12a-notes.md`.

Deliver: the four additions exactly as specified; `jarvis/v2/tools/schedules.py`
with the three tools (schemas from type hints, `Annotated` descriptions,
not dangerous); the plain-English `when` parser in `schedules.py`
(`parse_when(text) -> cron | every_s`, table-driven, returning `None` on
anything it does not recognise, with a `describe(cron|every_s)` inverse
used by `/schedules/preview`); `FAST_TOOLS` and `MCP_TOOLS` extended; the
tools resolving the project by name or id, defaulting to the calling
thread's project when the runtime exposes it, else the inbox.

Tests in `tests/v2/hud_backend_check.py` (extend) and
`tests/v2/schedules_tools_check.py`: move a chat thread (record, log and
session id intact; event published), a task thread refused 409; `/fs/dirs`
inside and outside the allowed roots, hidden dirs omitted; preview next-3
and describe for ≥ 12 `when` phrasings plus 4 refusals; each tool through
real `dispatch()` against a temp store, with the scheduler picking up a
tool-created schedule; `FAST_TOOLS`/`MCP_TOOLS` validation still passing.
Then every `tests/v2/*_check.py`.

Rules: stay inside `jarvis/v2/hud_api.py`, `jarvis/v2/schedules.py`,
`jarvis/v2/tools/schedules.py`, `jarvis/v2/providers/fastpath.py` and
`jarvis/v2/mcp.py` (the two frozensets only), `tests/v2/`,
`docs/codex-briefs/WP12c-backend-notes.md`. Stdlib only. Commit with a
message starting `v2 WP12c backend:` ending with
`Co-Authored-By: Codex <noreply@openai.com>`. Do not push. Notes under 50
lines.
