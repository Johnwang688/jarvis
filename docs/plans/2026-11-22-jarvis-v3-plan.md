# Jarvis v3 — a second brain on T3 Code (plan, draft)

Status: **draft for owner review, nothing built.** Written 2026-11-22 from
two owner requests, the same day:

1. "Somewhat of Hermes agent capability … always on … use T3 Code instead of
   our own systems to handle agents … port the automatically saving skills,
   the always-on capability, and anything else, like a memory vault."
   This is **Part A** (§1–§10): the substrate.
2. "I don't have an agent that knows all my stuff: my schedule, my writing
   style, my projects … a second person that can make decisions
   independently (not dangerous or important ones) and run stuff for me …
   communicate clearly and concisely so I get all the context I need when
   decisions actually need me … proactive in carrying out tasks, or for now
   asking to carry them out … Google Calendar, and have Claude fill it."
   This is **Part B** (§11–§19): what v3 is for.

The owner asked to plan carefully before committing to anything.

**Build breakdown:** [`2026-11-22-jarvis-v3-pr-map.md`](2026-11-22-jarvis-v3-pr-map.md) lists every PR with its dependencies, spec and acceptance criteria. **v2 is archived first (§21):** `jarvis hud` stays v2, and `jarvis agent` is v3.

**The v3 goal in one line:** Jarvis is a chief of staff. It knows John's
world, handles the routine on its own, notices what needs doing before John
remembers, and brings him only real decisions, each one briefed so it can be
answered in one tap.

# Part A — the substrate

Research inputs: a read of Hermes Agent at commit `0f602c5` (docs + `agent/`,
`tools/`, `cron/`), and of T3 Code's public docs (upstream `pingdotgg/t3code`: `docs/user/`,
`docs/internals/`, `docs/orchestration-v2/`).

---

## 1. Owner decisions (2026-11-22)

| # | Decision | Notes |
|---|---|---|
| H1 | **The Jarvis HUD and T3 Code run together. T3 shows up as a panel inside the HUD.** T3's thread and project handling replaces the HUD sidebar. T3 replaces the inbox natively, since the inbox was modeled on T3 to begin with. | See §4 for the framing problem this runs into. |
| H2 | **Retire Jarvis's providers** (Claude/Codex sessions spawned by daemon2). T3 runs every agent session. | T3 sessions use T3's and the CLIs' own permission models (H10). |
| H3 | **Jarvis keeps everything else:** voice, PC controls, cron, persistent memory, self-improving skills, and the rest of the non-chat surfaces. | |
| H4 | **Skills the reviewer writes go live automatically after a scan.** Every change can be rolled back from a ledger and appears in a daily digest. | Hermes-style. The safeguards in §6.4 still apply; none of them need an approval. |
| H5 | **The always-on part runs on this PC** (daemon2 under WSL systemd). | A VPS can come later. |
| H6 | **How T3 Code is reached is the owner's choice and outside this plan.** | Deployment details stay out of this repository. |
| H7 | **Retire Discord** as a Jarvis surface. | Covers the bot, per-project channels and per-task threads. |
| H8 | **Keep Jarvis's phone surface for the agentic interface** (voice, PC control, memory, cron, Jarvis status). T3's mobile app handles chats. | The phone's thread and chat views retire. The passkey, the 1 h unlock and the loopback-behind-Tailscale rules stay for what remains. |
| H9 | **The background reviewer runs on the Claude subscription** (`claude -p`, Haiku tier), not OpenRouter. | It spends Claude limits, so it needs a budget (§6.3). |
| H10 | **No Jarvis gate hooks on T3 sessions** (owner, 2026-11-24). They run under T3's permission modes and the CLIs' own safeguards. | Drops Track G, S2 and OG5 (§5 is kept as the record of what was planned). Jarvis's own actions stay ask-first. |

Earlier decisions still in force: the fast path stays Jarvis's own loop (no
framework, D2). Skills and memory stay plain markdown files. The memory ladder
is still substring → FTS5 → embeddings, and this plan takes the FTS5 step.
Personal data never goes to Chinese-hosted models.

---

## 2. What Hermes has that Jarvis lacks

Jarvis v2 already has most of Hermes: an always-on daemon, a messaging gateway
(the phone and inbox), cron (`v2/schedules.py`), voice, an approval gate,
skills shared with both CLIs, and file memory. Three things are missing, and
none of them depend on the provider:

1. **A learning loop.** After enough tool-calling work, a background review
   replays the turn and patches an existing skill or creates a new one. Two
   things are worth copying: the review prompt and the write tool
   (read-before-write, a scan, and a ledger with blob rollback).
2. **Memory hygiene.** A small, capped profile plus a capped fact file. When
   the file is full, the write fails and returns the current entries; nothing
   is compacted automatically. Facts are written as statements, not commands.
3. **Search over past sessions.** SQLite FTS5 that returns raw message
   windows. Hermes removed the LLM-summary step because it was slow and costly.

**Do not port:** skipping approval inside containers; agent-written skills
going unscanned (Hermes's default when the agent writes a skill in a normal
turn); LLM consolidation of skills turned on by default (one run looped for
4 h and used 91M tokens); the full skill index in every prompt (Jarvis's
on-demand index is better).

---

## 3. Target architecture

```
 ┌──────────── HUD (Jarvis) ─────────────────────────────────────┐
 │ orb · voice · Browser · terminals · VEX · editor              │
 │ Jarvis panels: memory vault · skills + digest · cron · status │
 │ T3 panel (threads, projects, approvals, PRs) — see §4         │
 └───────────────────────────────────────────────────────────────┘
        │ voice / cron / desktop driver start work as T3 threads
        ▼  (`t3 thread start|send|show --json`, documented, host-only)
 ┌──────── T3 Code server ───────────────┐     ┌── daemon2 (Jarvis) ─────────┐
 │ spawns claude / codex with each CLI's │     │ fast path (voice/chat loop) │
 │ own login; updates CLIs; model list;  │     │ cron · voice · desktop      │
 │ worktrees; PR watch; T3 mobile        │     │ phone (agentic only)        │
 └──────┬────────────────────────────────┘     │ review queue + reviewer     │
        │ every session loads, via global config│ session indexer             │
        ▼                                       └─────────────────────────────┘
 global Stop hook ───────────▶ review queue (learning loop, L1)
 global MCP server ──────────▶ jarvis-brain (memory, search, skills, desktop,
                                cron, owner_post, Spotify, …)
 Stop / turn-complete hooks ─▶ review queue (files) ─▶ reviewer (claude -p)
```

The main idea: **Jarvis stops spawning agents and starts reaching every agent
through the CLIs' own global extension points.** These are hooks, MCP servers
and skills folders. T3 starts the sessions, but whatever a session loads comes
from `~/.claude` and `~/.codex`, which Jarvis owns. The same mechanism also
covers a plain `claude` run in a terminal.

---

## 4. Problem found: T3 refuses to be framed

T3's web app answers `content-security-policy: frame-ancestors
'none'` and `x-frame-options: DENY`, so a plain iframe in the HUD won't load.
Options, to settle in spike S1:

- **(a) Jarvis as a panel inside T3 instead.** Check whether T3 has a
  supported way to host a third-party panel. It has previews, themes and
  plugins in some form. If it does, the HUD's Jarvis-only panels live in T3,
  and the standalone HUD is used for the orb/voice view.
- **(b) Two windows, docked.** The HUD opens T3, either the desktop app or a
  Chrome app window, and snaps it beside itself through the
  existing desktop bridge. Nothing to maintain against T3's churn, but it is
  not one window.
- **(c) A HUD-side reverse proxy that strips the anti-framing headers.** It
  would have to carry T3's WebSocket and session auth too. Not recommended:
  it removes a clickjacking defence from an app that can run agents with
  bypass permissions, and every T3 nightly can break it.
- **(d) Render T3 natively in the HUD** over T3's WebSocket RPC
  (`packages/contracts`). Rejected for now: the API is private, TypeScript-only
  (Effect-RPC), pre-1.0, and ships several nightlies a day.

Recommendation: try (a), fall back to (b). If (a) or (c) ends up with T3 in a
HUD frame, the "no frame takes the keyboard without the owner's gesture" rule
needs an explicit owner carve-out for the T3 panel. That rule was written for
untrusted pages in the Browser.

---

## 5. Moving the gate (the go/no-go)

> **Superseded by H10 (owner, 2026-11-24): not built.**

Today Claude's gate is a `PreToolUse` hook that Jarvis puts into each session
it spawns (`v2/providers/claude.py` ~L1100). Codex's gate is the config
Jarvis writes per chat. T3 starts Claude with
`--permission-mode=bypassPermissions` in its default Full access mode, and it
has no outside gate hook. Its MCP endpoint is documented as not routing
approvals. **Providers are not retired until the following holds:**

- The judge (`permissions.py`, `rules.py`, the secrets and protected-state
  detectors) runs as a **global hook command** from `~/.claude/settings.json`
  and from Codex hooks (`codex features list`: `hooks stable true` on 0.169.1).
- Its decisions take effect in **every T3 permission mode**, including Full
  access. A DENY must block. What ASK does under bypass is unknown and is the
  main thing S2 measures. If ASK can't reach a human, T3 threads default to
  Supervised mode, or ASK becomes DENY with a reason the agent sees.
- The hook config, the hook script and the Codex hook/rules files are
  **protected state**: the judge denies agent writes to them ("permission
  state cannot authorize its replacement").
- **Fail closed.** If daemon2 is down, the hook still judges in-process. It
  doesn't depend on a socket for DENY.
- Before enforcing, run **shadow mode** for a few days: log every decision
  across all sessions, including this kind of T3 chat, and compare the logs
  with today's gate.
- Writes to the skills folders through generic Write/Edit/shell are judged.
  Only `skill_manage` writes them, so the ledger can't be bypassed. That
  bypass is a known Hermes weakness.

Approvals then appear in T3, which is in the HUD and on T3 mobile. The Jarvis
broker, its approval cards and the phone approval flow retire for agent
sessions. **This supersedes several CLAUDE.md safety summaries** ("Claude's
`PreToolUse` hook is its gate", Codex chat configs, "Only the broker asks the
owner"). They get rewritten in §9, not quietly dropped.

---

## 6. The learning loop

### 6.1 jarvis-brain MCP server

One stdio MCP server, registered globally for Claude and Codex, so T3
sessions, terminal sessions and the fast path (as plain tools) all get it.

- `memory_*`: read, add, patch and remove over the vault (§6.2).
- `session_search`: FTS5 with four call shapes, as in Hermes: discover (a hit
  plus ±5 messages), scroll, read, and browse recent sessions.
- `skill_view` and `skill_manage`: create, patch, write_file and archive.
  Read-before-write is enforced, and every write is scanned and ledgered.
- Later, moved over from v2's own tools: desktop/HUD driving, cron,
  owner_post, Spotify and the other integrations, each with its existing
  approval rule, enforced in the MCP server's own process (H10).

### 6.2 Memory vault

- **Location:** `~/.local/share/jarvis/vault/`, owner-only (0700). It is
  Obsidian-compatible and readable from Windows at `\\wsl.localhost\…`.
- **Contents:** one fact per file with frontmatter and `[[links]]`, the same
  format as today's memory. Add `USER.md`, a capped profile (a few KB) that
  is the only part injected at session start. Everything else is pulled on
  demand.
- **When full:** the write fails and returns the current entries. Nothing is
  compacted automatically. Facts are statements ("John prefers X"), never
  imperatives.
- **Writes are scanned** with the skill scanner's rules: injection patterns,
  invisible Unicode and credential shapes.
- **Migration:** import `memory/` (the repo copy) once. Claude Code's
  per-project auto-memory stays where Claude Code keeps it; it is indexed
  read-only, not moved.

### 6.3 Review queue and reviewer

- **Trigger:** a global Claude `Stop` hook and the Codex turn-complete hook
  drop a small JSON record into a queue directory: transcript path,
  tool-call count and cwd. They write a file and make no network call.
  daemon2 consumes the queue.
- **Thresholds:** a skill review after ≥25 tool calls since the last skill
  review of that session, and a memory review after ≥10 user turns since the
  last memory review. Skip short chats.
- **Runner:** `claude -p --model haiku` (H9) with a minimal tool allowlist:
  `jarvis-brain` skill and memory tools plus read-only file tools,
  `--strict-mcp-config`, and deny-all approvals. A recursion guard (an env
  var) stops the reviewer's own Stop hook from queueing it again.
- **Budget:** a daily cap on reviews. Skip when Claude usage is ≥75%, reusing
  the existing usage reading. Every run's cost/usage is logged.
- **Input:** a digest, as Hermes does when routed to a cheaper model: the
  last ~24 turns verbatim plus a summary of the rest, capped well under
  Haiku's 100k price step.
- **Prompt:** start from Hermes's skill-review prompt. Patch the loaded
  skill first, then an umbrella skill, then add a reference file, and only
  then create a new class-level skill. Write lessons, not logs. Don't capture
  environment failures or dead ends.

### 6.4 Safeguards under "all auto" (H4)

None of these need the owner's approval.

- **Scan every write.** A scan hit quarantines the write to
  `skills/.pending/` and flags it in the digest. It never goes live.
- **Append-only ledger** with before/after sha256 and a content-addressed
  blob store. One command rolls back any single change.
- **Taint flag:** a write from a turn that read fetched web or email content
  is marked in the digest. It still goes live, but it is listed first.
- **Pin** blocks archiving; **lock** blocks all agent edits. Hand-written
  owner skills are locked by default.
- **Curator:** deterministic only. Unused agent-created skills go stale at
  14 d and are archived at 30 d, never deleted. LLM consolidation is off.
- **Daily digest:** shown in the HUD skills panel and on the phone, and
  included in the morning briefing.

### 6.5 Session indexer

daemon2 indexes Claude JSONL (`~/.claude/projects`), Codex sessions
(`~/.codex/sessions`) and Jarvis fast-path sessions into
`~/.local/share/jarvis/index.sqlite` (0600, FTS5 plus a trigram table). It
does **not** read T3's `statev2.sqlite`: the schema is private and the CLI
transcripts hold the same content. **Every message goes through the existing
redaction (`v2/redact.py`, the credential patterns) before indexing**, and
search results are scrubbed again on the way out.

---

## 7. What moves where

| Today (Jarvis) | After |
|---|---|
| `v2/providers/` (claude, codex*, codex_config/rpc/login/usage), catalogs, `provider.py`, `model.py`, ~9k lines | T3 |
| Thread/project stores, sidebar, titles, sub-agent panes, chat-to-chat, compact button, fast-mode chip, running cost | T3 (parity checked in S6) |
| Inbox (WS-0..5), PR rows / merge flow, `chat_git` | T3 (PR watch, worktrees, git) |
| Approval broker and cards for agent sessions | T3 approvals (H10) |
| Discord bot, channels, per-task threads | Retired (H7) |
| Phone thread/chat views | T3 mobile; Jarvis phone keeps the agentic views (H8) |
| Fast path, voice, wake word, STT/TTS, speech | Jarvis (unchanged) |
| Desktop driver chat (CU-3d) | A T3 thread with jarvis-brain desktop tools |
| Cron (`v2/schedules.py`) | Jarvis. Agent jobs start T3 threads; pre-flight validation with no tokens spent |
| Gate (`permissions.py`, `rules.py`, secrets, protected state) | Jarvis, run as global hooks |
| Memory, skills | Jarvis, through jarvis-brain |
| Browser, terminals, editor, VEX | Jarvis HUD (unchanged) |

Several of these features shipped recently at the owner's request: inbox
WS-0..5, chat-to-chat, agent panes, the compact button, the fast-mode chip,
diff quick review and approval summaries. **S6 produces a checklist** of each
one: T3 has it natively, T3 has something close, or it is lost. The owner
signs off on that list before §9.

---

## 8. Phases

Nothing is deleted before phase 6. Phases 1–3 are useful even if the T3
switch is abandoned.

**Phase 0: spikes. They produce findings, not product code.**
- S1: how to show T3 with the HUD (§4, options a/b).
- S2: dropped (H10).
- S3: daemon2 drives T3 through `t3 thread start|send|show --json`, and gets
  "turn ended" and "waiting on you" signals, either through the MCP event
  stream or by polling the CLI.
- S4: T3 runs headless with `t3 service install` while the Windows app is
  closed. How do updates (`t3 update`) interact with the desktop app's own
  copy of the WSL runtime?
- S5: a globally registered MCP server and Stop hook are loaded by
  T3-started sessions. T3 passes `--allowedTools=mcp__t3-code__*`; check that
  this doesn't hide other servers.
- S6: the parity checklist (§7).
- Output: a findings section appended here, then a go/no-go for phases 4–6.

**Phase 1: jarvis-brain + vault + search** (§6.1, §6.2, §6.5). Usable right
away from this kind of chat.

**Phase 2: learning loop** (§6.3, §6.4) and the digest.

**Phase 3: dropped (H10).**

**Phase 4: HUD + T3** (the S1 outcome). Sidebar handover. Jarvis panels for
the vault, skills, digest and cron. Slim the phone to its agentic views.

**Phase 5: re-point work-starters.** Voice hand-off, the desktop driver and
cron jobs start T3 threads.

**Phase 6: retire.** Remove providers, Discord, the inbox, the broker for
agent sessions and the phone's chat views. Rewrite CLAUDE.md and the safety,
control-plane and phone notes. Run `scripts/slim_claude.py --check`, and
grep tests for the sentences that changed (docs text is tested).

---

## 9. Risks

- **T3 churn.** T3 is pre-1.0 with nightlies several times a day. Integrate
  only through documented surfaces: the `t3` CLI, the `/mcp` endpoint and the
  CLIs' own hooks and config. Never the WebSocket RPC or its sqlite.
- **Gate coverage.** T3 sessions are governed by T3's permission modes and the
  CLIs' safeguards (H10). S6 lists what T3 does itself (git operations,
  worktree creation, PR actions, previews).
- **Remote access.** T3 has its own sign-in, outside the passkey model the
  phone uses (H6).
- **Subscription burn.** The reviewer spends Claude limits (H9). The cap and
  the 75% cut-off matter.
- **Skill drift and bloat.** Hermes users hit 150+ skills with zero-use
  near-duplicates. The curator archives them, and the digest is where the
  owner notices drift.
- **Lost features.** S6 is where those get decided, not discovered afterwards.

## 10. Open questions (after phase 0)

1. If S1 option (a) works, does the standalone HUD survive as an orb/voice
   view, or does T3 become the main window?
2. Jarvis-originated notices (cron results, the digest, quiet-hours deferrals)
   used to land in the inbox. With the inbox retired, they go to a Jarvis feed
   panel plus the phone, or into T3 as a thread?
3. Should the vault replace Claude Code's per-project auto-memory over time,
   or only index it?

---

# Part B — the second brain

## 11. What is missing today

Jarvis can already do many things. What it lacks is **knowing John and acting
without being asked.** Every useful turn today starts with John remembering to
type a prompt, and every agent starts from almost nothing: a few memory files
and whatever the prompt says. Part B adds five things:

1. **A model of John's world:** schedule, projects, people, commitments,
   preferences and writing style. It is kept current automatically (§12).
2. **Decision rights:** a written policy for what Jarvis may decide alone,
   what it does and then reports, and what it must ask (§13).
3. **Briefs:** one fixed format for everything that reaches John, so a
   decision arrives with exactly the context it needs (§14).
4. **Initiative:** Jarvis watches for things that need doing and proposes
   them. It asks before acting for now, with autonomy added kind by kind
   (§15).
5. **Calendar:** Google Calendar as the schedule's source of truth, which
   Jarvis reads and fills (§16).

## 12. The personal model ("knows all my stuff")

It lives in the vault (§6.2), with a fixed layout so agents know where to
look:

```
vault/
  USER.md            capped profile, injected at session start
  people/<name>.md   who, relationship, how John talks to them, open threads, last contact
  projects/<slug>.md status, goal, repo/folder, open threads, next actions, last activity
  commitments.md     what John promised (to whom, by when), and what others owe him
  preferences/       how John likes things done (feedback, as in Claude's memory types)
  style/             writing-style guide per audience + pointers to examples (§12.2)
  facts/             everything else, one fact per file, as today
```

The schedule is **not** copied into the vault. Calendar is the source of truth
and is read live (§16).

### 12.1 Ingestion (read-only, incremental, scrubbed)

Daemon2 ingesters pull **new items since the last run** from each source,
scrub them with the shared redaction, and add them to the search index
(§6.5). They spend no tokens: the model only sees what a nightly or triggered
job selects.

| Source | What | How |
|---|---|---|
| Gmail | received + **sent** | existing Jarvis Google OAuth (`gmail.readonly`) |
| Calendar | events, changes | new scope (§16) |
| Drive / Docs | docs John authored | existing `drive` scope |
| Schoolwork | assignments, due dates, grades | the existing `sw` integration (Canvas, MySchoolApp) |
| GitHub | repos, PRs, issues, CI | `gh` (read-only calls) |
| Agent transcripts | Claude, Codex, T3, Jarvis | §6.5 |
| Owner notes | vault edits, voice memos | direct |

### 12.2 Writing style

- **Corpus:** John's sent mail and the docs he authored. Agent-written text is
  excluded, so Jarvis doesn't learn from itself.
- **Style guide per audience** (teachers, teammates, friends, family,
  formal/outside contacts). It covers greeting, sign-off, length, tone,
  common phrases and things John never says. The nightly job writes it and
  keeps it short.
- **Examples at draft time:** search pulls 2–3 of John's past messages to the
  same person or a similar audience, so the draft has real examples to match.
- **Learning from edits:** when John edits a Jarvis draft before sending, the
  diff goes to the reviewer as a lesson, the same way Part A's loop learns
  skills. This is what makes the style improve week over week.

### 12.3 Consolidation ("dreaming")

A nightly job (Claude subscription, H9 budget) reads the day's new items and
updates the project and people pages, `commitments.md` and `USER.md`. It
writes through `memory_*`, so the caps, scans and statement-form rule apply.
It is also when stale facts are retired: a fact contradicted by newer
evidence is archived with a note on what replaced it.

## 13. Decision rights ("decide independently, not dangerous or important")

Every action Jarvis can take falls into one of four levels:

| Level | Meaning | Examples (proposed defaults) |
|---|---|---|
| **L0 Do** | do it, log it | file/label/archive mail, update the vault, research, draft (not send), create time blocks on John's calendar with no attendees (§16), start read-only investigations |
| **L1 Do + tell** | do it, include it in the digest | reschedule own time blocks, open a draft PR in an owned worktree, fix a failing test in a T3 thread, add Canvas due dates to the calendar |
| **L2 Ask** | propose with a brief and wait for a tap | anything other people will see (send mail, accept or decline invites, events with attendees), spending money, moving or deleting events John created himself, merging, any new kind of action |
| **L3 Never** | owner only | the gate's DENY list, credentials, the policy itself, deleting others' data, schoolwork submissions (standing decision) |

Rules:

- **"Important" is decided by the policy, not the model.** The tests are
  reversibility, visibility to others, money, deadlines and the safety gate.
  When unsure, go up a level.
- **Untrusted content never lowers a level.** After outside text, actions
  that can leak data or leave lasting changes still need L2. The policy
  explicitly exempts local app and folder opens, Jarvis's calendar blocks
  and due dates, reversible mail filing and drafts, browser read, follow
  and saved sign-in, and opens of addresses the owner supplied in the current
  chat turn. Other hard rules still apply. The ingesting job holds no
  outward tools and only produces structured proposals.
- **Promotion is learned and then owner-approved.** After a kind of proposal
  has been approved N times without edits, Jarvis proposes moving it down a
  level. The policy file is protected state, so a change takes effect only
  through John's tap, never by the agent writing the file.
- **Start conservative.** For now, everything outward is L2, as the owner
  said ("for now just asking to carry out tasks").

## 14. Briefs ("clear, concise, with all the context I need")

Everything that reaches John uses one format:

```
<one-line ask or news>                         [due: Thu 5pm]  [rank]
Why now: <one sentence>
Recommend: <option>  — because <one clause>
Other options: <option>, <option>
If you ignore it: <what Jarvis does by default, and when>
▸ Context (collapsed): sources with links, the draft, the diff
[Approve]  [Edit]  [Other…]  [Not now]
```

- **One decision per brief**, answerable in one tap. "Edit" opens the draft
  or the thread.
- **"If you ignore it" is mandatory.** It forces Jarvis to have a safe
  default, and tells John what silence means.
- **Routing:**
  - Urgent and due today: a phone push (quiet hours 23–07 respected, push
    body `{count, rank}` only, as today).
  - Everything else: batched into the **morning brief** and the **evening
    wrap-up** (what Jarvis did today, what is waiting, what's tomorrow).
  - All of it in the HUD's Jarvis feed and on the Jarvis phone.
- **Feedback:** each brief has "too long" and "missing context" taps. Those
  go to the reviewer, so the brief format is learned like a skill.
- **Writing rules** come from the owner's existing preferences: lead with the
  answer, no padding, define terms, one question per decision.

## 15. Initiative ("proactive instead of waiting for me")

```
signals ──▶ cheap triggers (no tokens) ──▶ planner job (Claude) ──▶ proposals
                                                                    │
            L0/L1 ──▶ one-tap brief ──▶ John taps ──▶ do ──▶ digest │   (H10: every level asks)
            L2    ──▶ full brief ───▶ John taps ──▶ do ◀────────────┘
```

- **Signals:** new mail; calendar changes; a new Canvas assignment or a
  deadline within 72 h; PR review requests and CI failures; a commitment
  coming due; a project page with no activity for N days and open next
  actions; idle blocks in tomorrow's calendar.
- **Triggers are code, not prompts.** They compare snapshots and fire only on
  a change, so a quiet day costs no tokens. Hermes's pre-flight validation
  idea applies: a broken trigger is marked blocked without a model call.
- **Planner job:** runs on a trigger or on a light heartbeat during waking
  hours. It reads the signal plus the relevant vault pages and emits
  proposals as structured records (kind, level, brief fields, the action
  spec). The policy (§13) assigns the level in code; the model only proposes
  one.
- **Doing:** work happens in T3 threads that Jarvis starts (Part A, phase 5),
  with jarvis-brain loaded. Small actions (a calendar block, a label) run
  directly through jarvis-brain tools.
- **Example proposals:**
  - A new assignment is posted → block study time before the due date, and
    open a T3 thread with the rubric and a plan.
  - An email asks to meet → draft a reply in John's voice with two free
    slots.
  - CI fails on an open PR → open a fix thread, which yields a draft PR (L1).
  - John promised something by Friday and it's Wednesday → nudge, with a
    draft attached.
  - A robotics competition is next week → generate the prep checklist from
    last time's project page.
- **Budget:** a daily cap on planner runs and proposals; no pushes during
  quiet hours; similar proposals are grouped. Too many proposals is a
  failure mode in its own right, tracked by the "not now" rate.

## 16. Google Calendar

- **Integration:** add the Calendar scope to Jarvis's own Google OAuth and
  re-run `jarvis auth google` (a human-only step). Jarvis's own access is
  needed because triggers read the calendar with no model in the loop.
  claude.ai's connectors only exist inside model sessions. The tools are
  exposed through jarvis-brain, so T3 threads get them too.
- **Jarvis writes to John's main calendar directly** (owner, 2026-11-22).
  It may create and rearrange its own events (study and work blocks, due
  dates, reminders) at L0/L1. Each one is tagged in the event's private
  extended properties (`jarvis=1`) so Jarvis can tell its events from
  John's. Events with attendees (invites, accepting or declining) and moving
  or deleting events John created are L2.
- **What Jarvis fills:** Canvas/MySchoolApp due dates; work blocks sized from
  past effort; events found in mail (robotics meets, school events) proposed
  as L2; prep blocks before big events.
- **The morning brief** starts from today's calendar.

## 17. Phases for Part B

These follow Part A's phase 1 (jarvis-brain, vault, search), which they
depend on. They do not depend on the T3 switch.

- **B0: decisions.** The owner reviews §13's default matrix and §16's
  calendar rules.
- **B1: calendar + ingestion.** Calendar scope and tools. Ingesters for
  Gmail, Calendar, schoolwork and GitHub into the index.
- **B2: the personal model.** The vault layout, the nightly consolidation,
  project and people pages, commitments.
- **B3: briefs + feed.** The brief format, the HUD Jarvis feed, the phone
  feed, and morning brief and evening wrap-up v2.
- **B4: initiative, ask-only.** Triggers, the planner and the proposals
  queue. Everything L2 at first except vault writes and the Jarvis calendar.
- **B5: writing style.** Style guides, drafting with examples, learning from
  edits.
- **B6: autonomy.** The policy engine, learned promotion proposals, L0/L1
  execution.

## 18. Risks (Part B)

- **Prompt injection through mail and web pages.** It is the biggest risk
  for an agent that reads everything and can act. The mitigations are §13's
  untrusted-content rule, ingesters with no outward tools, and a level
  assigned by code.
- **Privacy.** The vault and index hold John's whole life. Both are
  owner-only (0700/0600), scrubbed of credentials, never sent to
  Chinese-hosted models, and kept out of repos and backups that leave the
  machine.
- **Wrong facts acted on confidently.** Every vault fact keeps its source and
  date. Briefs link their sources, and consolidation archives contradicted
  facts.
- **Nagging.** A proactive agent that proposes too much gets ignored. The
  caps, grouping and the "not now" rate are the defence.
- **Subscription burn.** Nightly consolidation, the planner and the reviewer
  all spend Claude limits. One shared budget, with the ≥75% cut-off.

## 19. Open questions (Part B)

Answered 2026-11-22; see §20.

## 20. Owner decisions, Part B (2026-11-22)

| # | Decision |
|---|---|
| V1 | **v3's goal is a second brain / chief of staff:** it knows John's schedule, writing style, projects and people, decides the routine alone, brings real decisions as concise briefs, and is proactive. For now it asks before carrying out tasks. |
| V2 | **Google Calendar on the account** Jarvis's Gmail/Drive access already uses. Add the Calendar scope and re-run `jarvis auth google` (a human step). |
| V3 | **Jarvis writes to the main calendar directly.** Its own blocks and due dates are L0/L1, tagged `jarvis=1`. Events with attendees, and changes to John's own events, are L2. |
| V4 | **Drafts in John's voice + one-tap send** from the brief. Gmail sends stay gated (unchanged safety rule); John's edits teach the style. |
| V5 | **Push urgent, batch the rest:** only due-today or time-critical items push (never 23–07). Everything else goes into the morning brief, the evening wrap-up and the HUD/phone Jarvis feed. |

Still open: §13's default matrix needs the owner's review line by line (B0).
The model for the planner and consolidation jobs is open too; it is Claude
subscription per H9, but which tier is undecided.

---

## 21. Owner decisions: archive v2 first (2026-11-23)

Before the v3 build starts, v2 is archived. It stays reachable through
`jarvis hud`, and v3 gets its own command, `jarvis agent`.

| # | Decision |
|---|---|
| A-1 | **v2 is frozen but fully working.** `jarvis hud` opens v2 exactly as today: chats, providers, inbox, Discord and tools all run. Only security and bug fixes, each in its own PR. Tagged `v2-final`. |
| A-2 | **One daemon at a time.** `jarvis agent` stops daemon2 and starts the v3 daemon. `jarvis hud` does the reverse. Whichever runs owns cron, the voice wake word, the phone listener (8408) and the desktop bridge. |
| A-3 | **v3 gets a new HUD (`hud3/`) built on shared parts.** It imports v2's frontend components (aurora glass, orb, Browser, terminals, editor) instead of copying them, and copies a component only when v3 must change it. |

What this changes in the rest of the plan:

- **"Retire" becomes "not included."** H2 (retire providers) and H7 (retire
  Discord) now mean v3 is built without them. They remain in v2, which runs
  whenever the owner switches to it. §7's "After" column describes v3, and
  §8 phase 6 becomes a cutover with no deletions: `jarvis-agent` becomes the
  unit enabled at login.
- **H1 (T3 as a panel in the HUD)** applies to the v3 HUD; v2's HUD is not
  modified.
- **H8 (phone)** is v3's own phone surface on the same port 8408, possible
  because only one daemon runs. v2's phone, chat views included, is
  unchanged inside v2.
- **New code** lives in `jarvis/v3/`, `tests/v3/` and `hud3/`. v3 imports
  from `jarvis.v2` only through an allowlist, and v2's toolsets are pinned
  so v3 tools can't leak into them.
- **The shared gate** (`rules.py`, `permissions.py`, `protected_state.py`,
  owner ops) serves both versions. It is the one place both may change, and
  it gains agent refusals for `jarvis agent` and the `jarvis-agent` unit.

PR map: Track A (A1–A6, wave 0), and the rewritten Tracks T and R.
