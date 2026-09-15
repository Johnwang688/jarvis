# WP8 — one skills folder for three consumers, and `jarvis-mcp`

You are Codex, implementing work package 8 of the Jarvis v2 redesign, in a
git worktree on branch `jarvis/wp8-skills-mcp`. Claude leads; this brief is
your entire scope. Read first, in this order:

1. `docs/jarvis-v2-design.md` — §2 D8, §9 (both halves), §14 row WP8.
2. `jarvis/tools/skills.py` (v1 loader: `skills/<name>.md`, frontmatter
   `description:`, `index()` cached on mtime+size, `skill_write`),
   `tests/skills_check.py`, and every file in `skills/`.
3. `jarvis/tools/__init__.py` — `REGISTRY`, `ToolEntry.spec()` (a JSON
   schema per tool already exists; never hand-write one), `dispatch()`
   (returns error strings, scrubs secrets, honours `dangerous`),
   `default_names()`, invariant 10 (deferred groups).
4. `jarvis/v2/daemon.py` (merged) for how the daemon is reached locally;
   `docs/codex-briefs/WP7-notes.md`.
5. The Agent Skills format both CLIs load: `<dir>/<name>/SKILL.md` with
   YAML frontmatter (`name`, `description`; optional `allowed-tools`,
   `disable-model-invocation`). Claude Code reads `~/.claude/skills/`;
   Codex reads `~/.codex/skills/` (yours — verify the exact path and any
   frontmatter it requires against your own installed docs, and record it).

## Part A — skills folder

- **Convert** every `skills/<name>.md` to `skills/<name>/SKILL.md`,
  frontmatter `name: <name>` + the existing `description`, body unchanged.
  Do it with a script committed as `scripts/convert_skills.py` (idempotent,
  refuses to overwrite a differing SKILL.md), and run it. `git mv` so
  history follows.
- **Loader**: `jarvis/tools/skills.py` reads the new layout (and, for one
  release, still the flat layout, warning once per flat file). `skill_write`
  writes the new layout. `index()` unchanged in output. `tests/skills_check.py`
  must pass unmodified except for fixture paths.
- **Jarvis-only skills**: `whiteboard`, `self-improve`, `manual-compaction`,
  `permission-allowlist` drive Jarvis-only surfaces. Add frontmatter
  `jarvis-only: true` to those; the linker skips them.
- **`jarvis skills link`** (subcommand in `jarvis/__main__.py`, additive):
  for each non-jarvis-only skill, create symlinks `~/.claude/skills/<name>`
  and `~/.codex/skills/<name>` → `<repo>/skills/<name>`. Idempotent
  (existing correct link: skip); a real directory or a link elsewhere is
  **refused and reported**, never replaced; `--unlink` removes only links
  that point into this repo; `--dry-run` prints the plan. Print a table of
  what was done. Never create `~/.claude` or `~/.codex` if absent — report
  instead.

## Part B — `jarvis-mcp`

`jarvis/v2/mcp.py`: an MCP server over **stdio**, JSON-RPC 2.0, stdlib
only, protocol version `2025-06-18` with `2024-11-05` accepted. Methods:
`initialize` (capabilities: tools), `notifications/initialized`, `ping`,
`tools/list`, `tools/call`. Everything else → JSON-RPC method-not-found.
One request at a time is fine; never block stdout on a slow tool longer
than `MCP_TOOL_TIMEOUT` (default 120 s; then an error result, the tool
left running on its thread).

- **Exposed tools** = `MCP_TOOLS`, a frozenset in the module: the memory
  tools, the four `session_*` read tools, `get_datetime`, `gmail_search`,
  `gmail_read`, `gmail_send`, `discord_dm_owner`, `spotify_play`,
  `spotify_status`, `spotify_search`, `spotify_pause`, `cad_status`,
  `cad_find_part`. Validated at import like `FAST_TOOLS` (a missing name
  raises). Schemas come from `ToolEntry.spec()`; tool names are exposed
  unchanged. A deferred-group tool whose group is *unavailable* (no auth
  bundle) is omitted from `tools/list` — the invariant-10 rule.
- **Dangerous tools** (`gmail_send` today) are called through v1
  `dispatch()` with an approver that **denies with the reason "approval
  routing for jarvis-mcp lands in WP5"**. Do not invent a broker path; the
  seam is one function, `_approver()`, documented as the WP5 hook. The
  denial reaches the CLI as a normal tool result, never an exception.
- `tools/call` results are `{"content": [{"type": "text", "text": ...}],
  "isError": bool}`; an image-producing tool returns an `image` content
  block. `dispatch()` already scrubs secrets; assert in the tests that a
  planted credential value never appears in a response.
- **`jarvis mcp`** subcommand runs it; **`jarvis mcp config`** prints the
  two registration snippets: the JSON Claude Code takes on `--mcp-config`
  (`{"mcpServers": {"jarvis": {"command": "<abs venv python>", "args":
  ["-m", "jarvis", "mcp"]}}}`) and the `[mcp_servers.jarvis]` TOML for
  Codex. Absolute paths, no `cd`.

## Tests

- `tests/v2/skills_layout_check.py`: conversion idempotent and refusing
  drift; loader reads both layouts, warns once; `skill_write` writes the new
  layout; index byte-identical before/after conversion; linker against a
  temp `HOME` (create fake `~/.claude/skills` and `~/.codex/skills`): links
  made, jarvis-only skipped, existing real dir refused, foreign link
  refused, `--unlink` removes only ours, `--dry-run` writes nothing, absent
  parent reported not created.
- `tests/v2/mcp_check.py`: the server driven over real pipes as a
  subprocess (`python -m jarvis mcp` with `PYTHONPATH` set) — initialize
  handshake for both protocol versions, `tools/list` schemas equal to
  `spec()`, a `tools/call` on `get_datetime` and on `memory_search` against
  a temp `MEMORY_DIR`, a dangerous call denied without running (patch the
  gmail transport to raise if reached), unknown method → -32601, malformed
  JSON → -32700 and the server survives, the timeout path, a planted secret
  scrubbed, and an unavailable deferred group absent from the list.

Run each as `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python <file>`,
then `tests/skills_check.py`, `tests/v2/fastpath_check.py` (it names skill
tools), and `tests/agentbench_check.py` (it copies `SKILLS_DIR`).

## Rules

Stay inside `skills/` (conversion), `scripts/convert_skills.py`,
`jarvis/tools/skills.py`, `jarvis/__main__.py` (two additive subcommands),
`jarvis/v2/mcp.py`, `jarvis/v2/skills_link.py`, `tests/v2/`, and
`docs/codex-briefs/WP8-notes.md`. No CLAUDE.md edits (Claude will record
the layout change), no new dependencies. When green, commit with a message
starting `v2 WP8: skills folder + jarvis-mcp` ending with
`Co-Authored-By: Codex <noreply@openai.com>`. Do not push. Finish with
`WP8-notes.md` (under 70 lines): what you built, the Codex skills path and
frontmatter facts you verified, test commands and last lines, workarounds,
proposed interface changes.
