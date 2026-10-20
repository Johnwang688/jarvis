# Plan: Claude skills in Jarvis and Codex chats (2026-10-19)

## The owner's request

> Am I able to use my existing Claude Code skills in Jarvis? … Let's work on linking Claude skills
> to Jarvis and Codex chats.

## Owner answers (2026-10-19)

| # | Question | Answer |
|---|---|---|
| SK-1 | Which skills | **Your own and the claude.ai-synced ones.** Every top-level skill in `~/.claude/skills/<name>/SKILL.md`, including those reached through a symlink, plus the claude.ai-synced skills in `~/.claude/skills/synced/<bucket>/<name>/SKILL.md`. Plugin skills are out of scope: they are not files there. |
| SK-2 | Mechanism | **Live from `~/.claude`.** Jarvis reads `~/.claude/skills` as a second, read-only skill root, with no copies. `jarvis skills link` also links those skills into `~/.codex/skills`. A Jarvis repo skill with the same name wins. |

## Where things stand today (verified)

- **Claude chats** already load every skill in `~/.claude/skills`: `SETTING_SOURCES = ["user", "project"]` (`providers/claude.py:269`). The isolated quick-review session loads none.
- **Jarvis (fast path):**
  - `jarvis/tools/skills._paths()` reads only `config.SKILLS_DIR`, which is the repo's `skills/`.
  - `commands._catalogue()` drives the HUD's `/` menu and `check_skill`.
  - `skill_write` writes to `SKILLS_DIR/<name>/SKILL.md`.
- **Codex:** `jarvis/v2/skills_link.link()` links only the repo's skills into `~/.claude/skills` and `~/.codex/skills`, and never replaces a foreign entry.

## Design (lead defaults, reversible)

### 1. `config.CLAUDE_SKILLS_DIR`

The default is `~/.claude/skills`. `JARVIS_CLAUDE_SKILLS` overrides it, and an empty value turns the root off.

`skills._paths()` returns the union, with this precedence:
1. a repo skill;
2. a top-level `~/.claude/skills/<name>/SKILL.md`;
3. a synced `~/.claude/skills/synced/*/<name>/SKILL.md`. If two buckets hold the same name, the first in sorted order wins.

Rules for the external root:
- Entries in `~/.claude/skills` that are symlinks back into the repo's `skills/` are the same skill. They are not duplicated, and they resolve to the repo copy.
- An entry is refused when it fails any of these:
  - its resolved path must be a regular file named exactly `SKILL.md`;
  - it must not sit under any protected or credential path (use the shared path helpers);
  - its name must not be credential-like.

  A refused entry is skipped and noted in `jarvis skills link` output. The rest of the catalogue still loads.
- Directory names must match `^[a-z0-9][a-z0-9-]{0,63}$`. The skill's `name` comes from the directory name, as today.
- The `.bucket-*` file and `manifest.json` are ignored.

### 2. Read-only external skills

- `skill_read` and `skill_list` read external skills.
- `skill_write` never writes outside `SKILLS_DIR`. A name that exists only externally is refused with: "`<name>` lives in ~/.claude/skills; edit it there."
- No skill tool may follow a path out of the skill's own `SKILL.md`. `skill_read` reads only `SKILL.md`, as today.

### 3. Index, `/` menu and `jarvis-only`

- The skills index in the working-context block, the `/` menu (`commands.invocable_skills`) and `check_skill` include external skills.
- `jarvis-only: true` is still honoured.
- The index stays one line per skill. Note the extra token cost in the PR: about 25 more lines.

### 4. `index()` cache key

The key covers both roots, so adding or editing a skill in `~/.claude/skills` shows up without a restart, exactly as repo skills do.

### 5. `jarvis skills link`

It also links each external skill into `~/.codex/skills/<name>`, pointing at the resolved skill directory. It keeps today's rules:
- never replace a foreign entry;
- `--unlink` removes only links it made;
- report each row.

It never links into `~/.claude/skills`, since that is the source. For each skill, `--unlink` may remove a link into either the repo or the resolved external source.

### 6. Codex

Codex reads `~/.codex/skills` natively. Nothing else changes.

## Safety

- **Untrusted text:** skills are instructions the owner installed, or synced from the owner's claude.ai account. They are treated like repo skills. The decision to include the synced ones is the owner's (SK-1).
- **Credential paths:** no route to credentials. The resolved-path checks refuse protected and credential locations, and `skill_read` reads `SKILL.md` only.
- **Writes:** no write path into `~/.claude`. `skill_write` refuses external names, and `skills_link` never writes into `~/.claude/skills`.

## Work

One package, tests first: a test writer, then an implementer, then lead review and both sweeps. Tests use temporary roots only (`JARVIS_SKILLS`, `JARVIS_CLAUDE_SKILLS`, and a temporary HOME for linking) and never the real `~/.claude` or `~/.codex`.

## Rollout

- Jarvis chats see the new root after a daemon restart. The owner said not to restart until they say.
- Codex chats see the skills after the lead runs `jarvis skills link`, which writes symlinks only, then on their next session start.

**Done (2026-10-20).** [#272](https://github.com/Johnwang688/jarvis/pull/272) merged; on the owner's go
the lead installed main at 1c0c006 and restarted the daemon. The `/` menu lists 32 skills.
`jarvis skills link` made 25 links in `~/.codex/skills`, refusing nothing and replacing nothing.
Run it again after adding a skill to `~/.claude/skills`; Jarvis itself needs nothing.
