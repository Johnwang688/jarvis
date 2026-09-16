# WP8 — shared skills and jarvis-mcp

## Built
- Converted all 11 flat skills with `scripts/convert_skills.py` using `git mv`.
  Bodies and descriptions are unchanged; four Jarvis-only skills are tagged.
- Converter preflights drift, refuses differing destinations, and is idempotent.
- Loader reads both layouts (new wins), warns once per flat path, preserves the
  index output/cache behavior, and writes directories with quoted descriptions.
- `jarvis skills link [--unlink] [--dry-run]` prints an action table for both
  consumers, skips Jarvis-only skills, refuses conflicts, and never creates
  missing CLI parents. Unlink also handles stale links pointing into this repo.
- `jarvis mcp`: stdlib JSON-RPC stdio transport, protocol 2025-06-18/2024-11-05,
  registry-derived schemas, explicit 20-tool allowlist, unavailable groups hidden.
- Calls use v1 dispatch and secret scrubbing. `_approver()` denies dangerous
  calls with the WP5 reason; no broker route was invented. Images are supported.
- `MCP_TOOL_TIMEOUT` defaults to 120 seconds. Timed-out daemon threads continue;
  subsequent requests and EOF remain responsive. Tool stdout goes to stderr.
- `jarvis mcp config` prints Claude JSON and Codex TOML registration snippets.

## Verified Codex format
- Installed docs: `/home/johnw/.codex/skills/.system/skill-installer/SKILL.md`
  and `skill-creator/SKILL.md` in the same `.system` directory specify
  `$CODEX_HOME/skills`, defaulting to `~/.codex/skills` (the requested target).
- Creator docs require `<name>/SKILL.md`, YAML `name` and `description`;
  names use lowercase letters/digits/hyphens, under 64 characters. No other
  frontmatter or companion file is required for these instruction-only skills.
- Optional Codex invocation policy is `agents/openai.yaml` with
  `policy.allow_implicit_invocation`; verified in installed
  `skill-creator/references/openai_yaml.md`. No optional policy was added.
- [Current online docs](https://learn.chatgpt.com/docs/build-skills) instead
  advertise `~/.agents/skills` and confirm symlink support. Kept the brief's
  installed-docs target; a future migration should reconcile these locations.
- [MCP transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
  and [tool results](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
  checked against the official protocol specification.

## Validation
Prefix each script below with:
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python`

| Script | Last lines (exit 0) |
| --- | --- |
| tests/v2/skills_layout_check.py | Ran 8 tests; OK |
| tests/v2/mcp_check.py | Ran 10 tests; OK |
| tests/skills_check.py | all skills checks passed |
| tests/v2/fastpath_check.py | all fast-path checks passed |
| tests/agentbench_check.py | 89 passed, 0 failed |
| scripts/convert_skills.py (second run) | Converted 0 skill(s). |

`git diff --check` passes. MCP tests use real pipes and patched child-process
transports, temporary HOME/memory/credentials; no live inference or mail sent.
Compared all 11 bodies and the full rendered index against the pre-WP8 HEAD.
Only fixture paths changed in tests/skills_check.py, as Part A explicitly permits.

## Workarounds / proposed interface changes
- Config adds absolute `PYTHONPATH` for this checkout to both snippets: the
  shared virtualenv's editable install can point elsewhere. Tested from temp cwd.
  Preserve the virtualenv executable path rather than resolving its symlink.
- Worktree git metadata lives outside the sandbox; git mv/commit need escalation.
- WP5 should replace `_approver()` with daemon approval routing and a reason API.
  v1 ToolResult has no error flag; MCP maps its error/refusal text prefixes.
- Out-of-scope `jarvis/agentbench.py:grade_recall` still checks flat skill paths;
  its fixture suite passes, but the live grader needs layout migration separately.
- Skill bodies intentionally retain old path wording (notably skill-creator);
  propose a later documentation pass. No live CLI inference acceptance was run.
