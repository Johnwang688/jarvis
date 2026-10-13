# Orientation — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 1–24

<!-- claude-source:000001-000024:begin -->
# CLAUDE.md

Notes for agents working on this codebase. Read before changing anything.

**Keep this file current.** When you add a capability, change an invariant, or
make a decision worth not relitigating, update the relevant section in the same
session — especially *Current state* and *Decisions already made*.

## Chat git worktrees (2026-10-13)

Claude and Codex chats share the project checkout with the owner and must use
separate worktrees for real edits. Codex chat configs explicitly grant only
validated repository git/common directories and config.CHAT_WORKTREES_DIR
(default ~/.local/share/jarvis/chat-worktrees, override JARVIS_CHAT_WORKTREES);
the root is created privately (0700). Task briefs get no extra grants.
Symlinked/malformed metadata, credential directories, protected state and their
ancestors are excluded; uncertain credential-name scans fail closed. Sandbox
networking stays off, including effective-config and returned-policy checks.
Push/gh therefore request escalation through the existing Jarvis gate; a
sandbox-widening request still asks the owner. Writable git metadata permits
hooks/config writes, an explicit owner-approved risk. Provider regressions
are in tests/v2/codex_provider_check.py; deployed/live status belongs in the
work checkpoint, not an inference from unit tests.

<!-- claude-source:000001-000024:end -->

## Original CLAUDE.md lines 4683–4779

<!-- claude-source:004683-004779:begin -->
## What this is

A personal agent ("Jarvis") with a **hand-rolled** tool-calling loop, routed
through OpenRouter so any model is a config change. The owner is a CS student
building it to learn agent internals, so **do not replace the loop with a
framework** (LangChain, the SDK tool runners, etc.). The from-scratch loop is
the point of the project, not an accident.

## Environment

- **WSL2 (Ubuntu 26.04) on Windows 11.** WSLg is active, so headed GUI apps
  render on the Windows desktop.
- Code lives on the Linux filesystem at `~/projects/Jarvis`. **Never move it to
  `/mnt/c`** — 9p is slow and mangles permissions/line endings. Windows can
  already reach it at `\\wsl.localhost\Ubuntu\home\johnw\projects`.
- **System Python 3.14 has no `ensurepip`, so `python3 -m venv` fails.** Use
  `uv` (already installed): `uv venv`, `uv pip install -e .`.
- `jarvis` is symlinked from `~/.local/bin` into `.venv/bin/jarvis`, so source
  edits take effect with no reinstall.
- `sudo` requires a password — you cannot run it from a tool call. Print the
  command and ask the user to run it.
- `gh` (2.86) and `vercel` (58.4) are installed **user-local in
  ~/.local/bin** (2026-08-01; npm's global prefix is /usr = sudo, so vercel
  went in via `npm install -g --prefix ~/.local`). A Windows-side vercel
  also exists on PATH at /mnt/c/... — the native one shadows it; don't
  "fix" that. Both CLIs auth per-user (`gh auth login`, `vercel login`),
  human-only.
- **WSLg audio is full duplex and verified** (2026-07-30): playback via
  `RDPSink`, mic via `RDPSource`, socket at `/mnt/wslg/PulseServer`. Two
  gotchas: client apps need `libpulse0` (now installed, with
  `pulseaudio-utils`) — Chromium dlopens `libpulse.so.0` at startup and
  silently has *no* audio devices without it; and a Chromium launched before
  that library existed must be fully restarted to see devices.

## Architecture

```
jarvis/
  agent.py      the loop — ~50 lines, read it first
  llm.py        OpenRouter client; the only file that knows about HTTP
  models.py     the model roster: the eligible OpenRouter catalog, the
                owner's shortlist, and tier() — which every model call
                resolves through instead of reading config.TIERS
  context.py    image eviction / result truncation / compaction
  runtime.py    per-run ContextVars (plan slot, approver, cancel, depth,
                toolset) — the channel dispatch() cannot give a tool
  browser.py    Playwright session on its own thread, allowlist, budget, tracing
  untrusted.py  web text hygiene: hidden markup and invisible Unicode stripped,
                the rest fenced as untrusted data (see *Safety design*)
  config.py     model tiers, paths, system prompt
  bench.py      tool-calling stress test
  agentbench.py agent-bench — whole-Jarvis, sandboxed, category-rated
  voice.py      tts()/stt() — swappable contract, HTTP stays in llm.py
  google_auth.py  one-time OAuth consent (human-only) + silent token refresh
  spotify_auth.py Spotify OAuth (PKCE, no client secret) + rotating refresh
  onshape_auth.py Onshape API keys + the pinned CAD sandbox/libraries
  desktop.py    WSL half of the desktop bridge (listener, session, allowlist)
  discord_approvals.py  the approval gate, asked in the owner's DMs
  discord_agent.py  the Discord conversation core (persistent agent + the
                spoken-turns-never-authorize rule), shared by face and daemon
  daemon.py     `jarvis daemon` — headless always-on service: gateway + DM-only
                approvals + health/lock endpoint on port 8405
  goals.py      the durable goal store: goal.json + runner-written journal
  goalrunner.py works goals in slices: steering, budgets, progress DMs
  permissions.py  modes (ask/all) + the persistent dangerous-tool allowlist
  workflows.py  background agents on their own threads (safe tools only)
  gitops.py     git for agents that cannot ask: judge(), the hardened runner,
                own worktrees, pull requests (tools/gitctl.py exposes it)
  tasks.py      attended background tasks: full-toolset agents on their own
                threads whose approvals reach the owner's surface, attributed
  sessions.py   saved conversations: transcript, log, meta, titles, summaries
  avatars.py    who he presents as: name, wake phrases, SVG face + sanitizer
  avatar_templates.py  starter art for `jarvis avatar new` (avatars/ is
                gitignored, so a clone has nothing to look at otherwise)
  tools/        clock, files (read paged + line-numbered, write whole,
                edit by anchored replacement), search (grep_files — bounded,
                ripgrep with a Python fallback), memory, shell, web, browsing,
                gmail, onshape (CAD), skills, sessions (read past conversations),
                sqlite (read-only .db queries, mode=ro enforced by the
                engine), spotify (music: search, playlists, playback — a
                deferred tool group, see invariant 10), toolgroups
                (load_tools), goalctl (goal_report — how a goal run ends),
                voicectl (mute), avatarctl (which avatar), workflows,
                tasks (attended background delegation), plan (the working
                checklist), subagent (context isolation),
                desktop (drives Windows apps via the bridge)
  runtime.py    per-run state (plan, approver, depth) over ContextVars
  windows/      bridge.py — runs on Windows Python, owns all UI Automation;
                uiatree.py — tree rendering + refs, no Windows imports so the
                tests drive it on Linux
  skills/       one markdown file per skill — see tools/skills.py
  face/         server.py (HUD + speech + design routes), approvals.py (the
                gate), static/jarvis.html (the HUD), static/whiteboard.html
                (sketch → design board; output served from designs/ on
                WORKSHOP_PORT 8403 — its own origin, never the face's)
```

<!-- claude-source:004683-004779:end -->
