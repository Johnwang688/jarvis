# Decisions — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 7199–7334

<!-- claude-source:007199-007334:begin -->
## Decisions already made — don't relitigate

- **The owner's Browser keeps this machine closed like the LAN
  (2026-10-12, the lead's call on PR #69's MED 2, recorded for the owner).**
  The gate admits a loopback port only after the owner opened it in a tab
  through the owner-only routes, for the host's lifetime; `local_servers` in
  `browser.json` (off) opens them all. Pages and agents never open one.
  Auto-open never opens one (amendment 2, L2). Details in the BR-1a
  paragraph.
- **No Browser frame takes the keyboard without the owner's gesture
  (2026-10-12, R-1).** One that gains focus otherwise is removed at once
  (removal is the only thing that works); never under a card or from a
  hidden pane. Details in the "A framed page never takes the keyboard"
  paragraph.
- **A toolset is enforced at dispatch, not only by what the request offers
  (2026-10-10).** An agent's bound toolset is the set it may call; naming a
  registered tool outside it is refused, dangerous or not. A deferred tool
  gets the `load_tools` pointer only when the agent could load it. **With no
  toolset bound, dispatch refuses** (2026-10-11, owner's answer 4). Every
  real caller binds first, and a new caller of `dispatch` must too. Scripts
  and tests opt in with `runtime.tools_bound(names)` or
  `runtime.unconfined()`, in code only. Details in *A toolset is a boundary*
  (Safety design).
- **A runner is judged by what it starts; an allowlist entry for its stem is
  still a yes to what it runs (2026-10-12).** `decide`, the owner ops, the
  gate-state check and v2's always-ask rules read through `uv run`, `npx`,
  `npm exec -c` and their kin from one table (`rules.starts`); the
  allowlist's stems (`command_targets`, `unwrap`) deliberately do not, so a
  `uv` entry covers `uv run rm -rf build` as it always did. A DENY still
  wins before any entry is read.
- **Killing the v2 daemon by pattern is DENY for agents (2026-10-11, the
  owner's "yes add the daemon2 kill rule" on PR #48).** A `pkill -f`,
  `killall -r` or `pgrep -f`/`pidof`-fed `kill` whose pattern matches the
  daemon's command line at the word `daemon2`, and (review 2) a `kill` fed
  the daemon's PID by `systemctl show|status`, a D-Bus read, its cgroup,
  `lsof`/`fuser` on its ports or `ps | grep`, plus `fuser -k` on its ports
  and `pkill --cgroup` naming its unit (and, since the delta review, a grep
  over `/proc/*/cmdline` or `ss -p`/`netstat -p` on its ports). A source is
  judged only when a fed kill is on the line, and a kill pattern whose
  matching cost cannot be bounded is never matched: too complex to judge, so
  asked, never DENY (`kill_unsure`). `pkill -f jarvis` stays ALLOW (the
  2026-08-09 matrix); a literal `kill <pid>` and kills by process name
  (`pkill python`) are not seen. Details with the restart commands above.
- **Each agent's prompt names only the tools it holds (2026-10-11, owner's
  answer 5).** `config.narrow_prompt` narrows any prompt built on
  SYSTEM_PROMPT paragraph by paragraph, a surface's own paragraphs included
  (invariant 7). cad-bench holds `skill_list` and `skill_read`,
  so the CAD skill it measures can load. runtime.py and toolgroups.py are
  **not** SELF_PROTECTED: the owner declined it (answer 3).
- **Auto-opened dev servers are Direct only, and never open a port in the
  Remote browser's gate (2026-10-12, the lead's decision L2,
  `docs/plans/2026-10-12-hud-browser-plan-amendment-2.md`).** Since the
  owner asked for servers that Jarvis, Claude and Codex start to open too,
  an agent can trigger an auto-open. Counting one as the owner's own
  navigation would hand agents a lever on the gate. Only the owner's typed
  URL, click or "Open in Remote" opens a loopback port there. This
  **reverses** the note BR-1a's review first carried: that BR-8b's auto-open
  counted as the owner's opening. #69 merged with that note already
  corrected, in the BR-1a paragraph and the "closed like the LAN" bullet.

- **Agents that cannot ask get a typed git tool, not a broader shell
  (2026-10-09).** `git` left `run_readonly`; workflows and sub-agents reach
  git through `gitops.py`, which judges the whole invocation and writes only
  in worktrees it made, on `jarvis/` branches. The foreground still has
  `run_command` (asks) for anything else, and v2 tasks (a worktree each, Claude
  Code's own classifier) remain the better home for large changes — this is for
  the v1 loop's background agents. Plain push is allowed for the worktree's own
  branch only; force, delete, tags and anything to `main` are not, and a pull
  request is the only way work lands.

- **No framework.** See "What this is".
- **Did not fork Grok Build** (xAI's open-sourced Rust coding agent). Useful to
  *read*; wrong base — 845k lines of Rust, coding-agent-shaped, and inheriting
  it defeats the learning goal.
- **Will not use leaked Claude Code source.** Proprietary code obtained without
  authorization; the Agent SDK and public docs cover the same design ground.
- **Membean bench was declined.** Membean is teacher-assigned, engagement-
  monitored schoolwork; an agent completing it produces a false report to a
  teacher, and every eval run would be a real submission. Building
  **`vocab-bench`** instead — a local, seeded, deterministic replica of the same
  difficulty profile. That is also the better benchmark: repeatable, shareable,
  CI-able, no bans.
- **Model routing policy (2026-07-30).** Default all real work to
  `openai/gpt-5.6-luna`. That Luna default is superseded by the owner's
  choice of Haiku 5.5 on 2026-10-12. Do not route personal data (chat, transcripts,
  compaction summaries) to Chinese-hosted models — the cheap tier moved
  qwen3.7-flash → gpt-oss-20b for exactly this reason. Multi-model
  comparisons remain opt-in by naming models explicitly on `jarvis bench`.
  If Luna hits a capability wall, escalate the orchestrator to GPT-5.6 Terra
  or Claude Sonnet 5.
- **Model default update (2026-10-12).** The owner chose
  `anthropic/claude-haiku-5.5`. Below 100k prompt tokens it lists at
  `openai/gpt-6-luna`'s price: $0.10 in and $0.50 out per M, cache reads $0.01.
  From 100k, the whole request costs 5x: $0.50 in and $2.50 out per M.
  Compaction therefore defaults to 90k (`JARVIS_COMPACT_AT_TOKENS` overrides;
  a non-empty value must be an integer of at least 1000; empty means unset).
  A turn can grow past the threshold before a safe cut point exists (invariant 1).
  Default threads, sub-agents, compaction summaries and the review tier now
  run on Haiku 5.5. Saved HUD selections still take precedence. A bench given
  `-m` still runs its children and summaries through `models.tier()`; pin
  `JARVIS_ORCHESTRATOR` for comparable runs. Numbers taken before this PR
  are not comparable with numbers after it.
- **Claude prompt caching (2026-10-12).** Up to four explicit ephemeral breakpoint
  positions: the system message, separate image and full-tool-result window
  boundaries from `context.manage()`, and the newest markable message before
  the volatile tail. The earlier middle prefix is already truncated/evicted
  and stays unchanged on subsequent steps. The later middle prefix can be
  invalidated by the other window, but reuses more result text between sparse
  image evictions; a single minimum boundary let old live images hold it back.
  Compaction can invalidate either prefix. Each window contributes only once
  its keep count is reached; coincident boundaries use one point.
  Breakpoints walk back to nonblank text only, never image parts or assistant
  messages with tool calls. Caller boundaries are deduped and capped at four,
  dropping oldest middle points while preserving the system and tail.
  The caller's transcript stays verbatim. One-shot summaries, delegation and
  reviews opt out with `cache=False`. The handoff prompt follows the context
  block, so its `volatile_tail` is 0.
  The old top-level breakpoint landed on the working-context tail, rebuilt
  every step (invariant 7), so it never hit for real agents with that block.
  A live Haiku 5.5 probe with the default toolset, about 20k prompt tokens and
  six steps read zero cached tokens each step and paid a 1.25x cache write,
  $0.0026 per step. Explicit breakpoints read about 19.2k of 20k tokens from
  step 2 on, $0.00035 per step. That was a short turn with small results;
  in long turns with big results, middle breakpoints preserve reusable prefixes
  as context management rewrites older messages. A breakpoint on a tool
  message worked. The separate-window fix has synthetic regression coverage;
  its provider cache-read improvement still needs a live long-turn probe.
  A live three-step tool turn on Haiku 5.5 at a reasoning effort ran without
  error although `_stream_once` does not pass `reasoning_details` back.
  Nothing changed for reasoning.
- **Semantic/RAG memory deferred.** Memory is one markdown file per fact, pulled
  on demand via tools, so nothing is auto-injected. The upgrade ladder is
  substring → SQLite FTS5/BM25 → embeddings, and the tool interface
  (`memory_search(query) -> text`) is the contract, so swapping backends changes
  nothing else. Revisit when search visibly misses.

<!-- claude-source:007199-007334:end -->
