# CLAUDE.md

Notes for agents working on this codebase. Read before changing anything.

**Keep this file current.** When you add a capability, change an invariant, or
make a decision worth not relitigating, update the relevant section in the same
session — especially *Current state* and *Decisions already made*.

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
  context.py    image eviction / result truncation / compaction
  runtime.py    per-run ContextVars (plan slot, approver, cancel, depth,
                toolset) — the channel dispatch() cannot give a tool
  browser.py    Playwright session on its own thread, allowlist, budget, tracing
  config.py     model tiers, paths, system prompt
  bench.py      tool-calling stress test
  agentbench.py agent-bench — whole-Jarvis, sandboxed, category-rated
  voice.py      tts()/stt() — swappable contract, HTTP stays in llm.py
  google_auth.py  one-time OAuth consent (human-only) + silent token refresh
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
  sessions.py   saved conversations: transcript, log, meta, titles, summaries
  avatars.py    who he presents as: name, wake phrases, SVG face + sanitizer
  avatar_templates.py  starter art for `jarvis avatar new` (avatars/ is
                gitignored, so a clone has nothing to look at otherwise)
  tools/        clock, files (read paged + line-numbered, write whole,
                edit by anchored replacement), search (grep_files — bounded,
                ripgrep with a Python fallback), memory, shell, web, browsing,
                gmail, onshape (CAD), skills, sessions (read past conversations),
                sqlite (read-only .db queries, mode=ro enforced by the
                engine), goalctl (goal_report — how a goal run ends),
                voicectl (mute), avatarctl (which avatar), workflows,
                plan (the working checklist), subagent (context isolation),
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

## Invariants — breaking these causes silent or hard failures

1. **Context pruning never deletes a message** (`context.py`). Every `tool`
   message must keep the `tool_call_id` its assistant message declared, or the
   next request 400s. Rewrite content in place. Compaction *does* delete, so it
   only cuts where **no tool_call is outstanding** — `find_cut_point()` walks
   the transcript tracking pending ids, and "is a user message" is *not*
   sufficient on its own. An image result is a bare `user` message wedged in
   behind its `tool` message (invariant 5), so a turn with two parallel renders
   lays out as `assistant(c1,c2) · tool(c1) · user(image) · tool(c2)` and that
   inner user message used to look like a legal boundary — cutting there
   orphaned `c2`. Fixed 2026-08-01; `tests/context_check.py` is the regression
   test (and is verified to fail against the old rule).

   Also pinned: **`messages[1]`, the original request, is never compacted
   away.** Compaction takes `messages[2:cut]`. It used to take `[1:cut]`, so
   the first casualty of a long run was the statement of what the run was for,
   leaving the model working from a cheap-tier paraphrase of its own goal.

   Consequence worth knowing: a cut point must be a `user` message, and a
   single turn contains none after the one that started it — so **inside one
   long turn, pruning is eviction and truncation only.** Compaction can only
   fire across turns (or at an image-carrier boundary).

2. **The assistant turn goes back verbatim.** Append `response.content` plus
   `tool_calls` unchanged. Reconstructing it loses the ids.

   **One exception, and only one** (`agent.py`, 2026-08-05): a turn with **no
   tool_calls and null content** is rewritten to `""`. Some providers reject
   that shape outright — Alibaba answers "The content field is a required
   field" — so appending it verbatim **poisons the transcript permanently**:
   every later request in that session 400s, and the failure surfaces a step
   after its cause. Found benching `qwen/qwen3.7-flash`, which returns
   `content: None` as a completion. Null content *with* tool_calls is legal
   everywhere and stays untouched, so the exception costs no ids and no
   parallel calls. `tests/context_check.py` covers both halves and is verified
   to fail against the old code. The general lesson is the same one the
   step-budget bug taught: **a transcript state the loop can create but cannot
   recover from is a bug, however rare the model that produces it.**

3. **All tool results for one assistant turn go back together**, each keyed to
   its call id. Splitting them across messages trains models out of parallel
   calls.

   Since 2026-08-09 those calls also *execute* in parallel, and two things hold
   it together. **Results are appended in call order, never completion order** —
   `_dispatch_calls` returns a list indexed by call position, so a fast third
   tool cannot land ahead of a slow first one. And **only an allowlist runs
   concurrently** (`tools.PARALLEL_SAFE`, plus a hard refusal for anything
   `dangerous`): the batches are maximal runs of *consecutive* safe calls, so a
   write between two reads splits them and the second read still sees the
   write. A worker that raises still yields a result string — a missing one
   would leave a `tool_call` unanswered and 400 the next request.

4. **Tool failures are returned as text, never raised.** `dispatch()` catches
   everything and returns an error string so the model can self-correct. The
   `recovery` bench task measures exactly this.

5. **A `tool` message can only hold a string.** Image-producing tools return
   `ToolResult(text=..., image_b64=...)` and the loop attaches the image as a
   separate `user` message right behind it.

6. **Tool schemas are generated from type hints.** Use
   `Annotated[str, "description"]`; never hand-write JSON schema. Defaults make
   a parameter optional.

7. **`messages[0]` is the durable slot, and only agents that can use a block
   get it.** It is rebuilt from source (base prompt + skills index + working
   plan) at the top of **every step**, not every turn — a 40-step turn passes
   the top of `run_turn` once, and step 40 is exactly when the plan matters.
   Nothing in `context.py` touches index 0, which is what makes it the one
   place a long run cannot lose. An agent without `skill_read` never sees the
   skills index and one without `plan_write` never sees the plan block: never
   describe a tool the model cannot call.

8. **Per-run state reaches tools through `runtime.py`, and fails closed.**
   `dispatch()` calls `func(**arguments)` with no agent reference, so the plan
   slot, the approver, the cancel check, the depth counter and the parent's
   toolset live in ContextVars bound by `run_turn`. They are per-thread, which
   is what keeps two concurrent agents (a workflow, the face's request threads)
   from sharing a checklist. An unbound approver **denies**. A sub-agent runs
   inside `contextvars.copy_context()` — it binds the same vars, so calling it
   directly would leave the parent holding the *child's* empty plan and the
   child's depth when the call returns.

   **Per-thread cuts both ways, and parallel dispatch (2026-08-09) is where it
   bites.** A tool running on a pool thread starts with an *empty* context, so
   it would hold an unbound approver — and unbound denies. A gated tool would
   begin refusing itself purely because it ran beside another one, which is a
   failure that never shows up in a single-tool test. `_dispatch_calls` takes a
   fresh `contextvars.copy_context()` **per worker** (one Context cannot be
   entered by two threads at once) and submits `ctx.run(...)`. The copies share
   the plan dict by reference, so a write through one is seen by the agent that
   owns it. `tests/loop_check.py` fails against a version that submits the bare
   function.

9. **All Playwright work happens on the browser's own thread.** The sync API
   is thread-affine and parks a *running* asyncio loop on whichever thread
   starts it — which crashed the chat REPL's prompt (2026-07-31) and would
   reject the face's per-request threads. Public `Session` methods marshal
   via `_submit()`; never call `SESSION.page` RPCs from outside `browser.py`
   (tests/benches use `Session.eval_js()`). Every surface stops the session
   on exit — a browser left running EPIPEs the Node driver when the process
   dies under it; `stop()` is a safe no-op if nothing started.

## Safety design (deliberate, do not loosen without asking)

- Tools are **narrow and typed**, not one god-tool, so the harness has
  something to gate on. `run_readonly` (allowlisted binaries, no shell
  operators) vs `run_command` (`dangerous=True`, prompts the user).
- `write_file` enforces **read-before-write** — refuses to clobber a file the
  agent hasn't read this session.
- Browser: **public internet open, private network closed** (2026-07-31).
  Named hosts in `allowed_hosts` always pass; `'*'` (in the default now)
  covers only public address space — LAN/router/metadata addresses, and any
  name that merely *resolves* private (`config.is_lan_host`), are refused.
  Policy is re-checked on wherever the page actually lands
  (`_guard_landing()` after goto/click/submit backs out to about:blank), so
  redirects can't escape it; `fetch_page` applies the same rule before the
  request and after redirects. Standing controls: **fresh context per run**
  (never a real Chrome profile — no cookies, no logins to misuse) and a
  **120-action budget**. vocab-bench pins its session back to
  localhost-only so bench runs stay hermetic.
- Web content is untrusted. The system prompt tells the model never to follow
  instructions found inside fetched pages.
- **Command rules decide what never asks and what never runs** (`rules.py`,
  2026-08-09). The gate used to be binary: `dangerous=True` meant ask, and the
  only escape was the allowlist matched on a command's *first word* — "git"
  silences `git push` as thoroughly as `git status`. Three verdicts now, and
  the two that matter are at opposite ends. **DENY** is not approvable by
  anyone (sudo/su/doas, dd/mkfs/fdisk/shutdown, `rm -rf` at a filesystem or
  home root, `chmod -R 777 /`, fork bombs, raw block-device writes) — the same
  shape as the `.env` refusal: approving a command is not consent to what it
  does, so some things must never reach the owner as a yes/no question.
  **ALLOW** runs unasked (git writes, build/package tooling, file shuffling,
  dev-server process control, read-only staples). Everything else asks.

  Four properties hold it up, each of which was a bug waiting to happen:

  - **Every segment is judged and the worst verdict wins.** `git commit && rm
    -rf /` is one string with two commands in it; first-word matching waves it
    through. Command substitution can't be judged at all, so it forces ASK.
  - **An ALLOW is only honoured for a *human-backed* approver**
    (`permissions.gate` sets `jarvis_human_backed`; `dispatch()` checks it).
    Caught in review: `workflows.py` hands its agents a deny-all approver
    *because* nobody is watching a background thread, and an auto-approve that
    skipped the approver would have silently converted "workflows cannot run
    dangerous tools" into "workflows can run any allowlisted one". Auto-approval
    is a convenience for a surface where the owner is present — never a
    property of the command by itself.
  - **An interpreter given inline source is not a build step.** `python` is
    allowed because running a script is routine; `python -c "…"` is arbitrary
    code, and allowing the stem would have allowed the language. `-c`/`-e`
    pulls it back to ASK.
  - **The secrets layer still wins.** `cp` is allowed, `cp .env /tmp` is not:
    `protected_in_command` runs inside the tool and is not overridable by any
    verdict.

  Owner's choices, 2026-08-09, recorded because the reasoning is the point:
  git writes **except push** (it reaches production directly) and **except
  reset --hard / clean** (they destroy uncommitted work); **never `rm`**
  (asked, not denied); auth commands deliberately **not** denied.

- **A `curl … | sh` is reviewed before it runs** (`command_review.py`). Rather
  than deny pipe-to-shell outright — installing uv or gcc that way is ordinary
  — the script is fetched and read by the orchestrator tier, which can refuse
  it. **Read the limits before trusting it:** a classifier is a safety net
  against accidents, not a boundary against an adversary, because the thing
  being judged is attacker-controlled text being judged by a model that reads
  text. So it is built to fail toward asking: the body is fenced and labelled
  untrusted data; *unsafe* denies; *safe* auto-approves **only** from a host on
  `TRUSTED_INSTALL_HOSTS`, so a clean-looking script from anywhere else still
  gets a human; every failure path (no URL, fetch error, unparseable verdict,
  model error) returns *unclear*, which asks; and
  `JARVIS_REVIEW_AUTOAPPROVE=0` leaves it able to refuse but not to consent.
  What it genuinely buys: a script that quietly adds an SSH key gets refused
  with a specific reason instead of being one distracted "yes" away.

- **Dangerous tools are approved by a human in whichever surface is running**
  — a y/N prompt in the CLI, an authorization card in the face
  (`face/approvals.py`), or a Discord DM when the owner is away from the desk
  (`discord_approvals.py`, below). All of them default to deny on every
  failure path. The face server is the only place that could accidentally
  pass `approve=None`, which would run them unguarded; don't.
- **Permission modes wrap that gate** (`permissions.py`, 2026-07-31). Every
  Agent's approver is `permissions.gate(surface_approver)`: mode "all"
  approves everything but is reachable ONLY via
  `jarvis face --dangerously-skip-permissions` and is a process variable —
  restart is always back to ask. The persistent allowlist
  (`~/.config/jarvis/allowlist.json`) holds only entries the owner explicitly
  created ('a' at the CLI prompt, ALWAYS on the HUD card); commands
  allowlist by first word only ("git" ≠ "gitfoo"). Tests must point
  `config.ALLOWLIST_PATH` at a temp file — a UI test once wrote `apt` onto
  the real allowlist by clicking the wrong button.
- **Desktop control is confined to an app allowlist** (2026-07-31).
  `config.DESKTOP_APPS` is the whole door: no desktop tool accepts a window
  title, handle, or executable path, only a registered app name, so the model
  cannot widen its own reach by argument — the same structural confinement as
  the Onshape sandbox pin. Two categories stay out of the registry on
  purpose: **no terminal/shell/file manager**, because keystrokes into one
  are arbitrary code execution and would route straight around
  `run_command`'s approval gate; and **no browser**, because the face HUD
  renders the approval card and an agent that could drive a browser window
  could authorize itself (the desktop version of the hole
  `is_face_origin()` closes). The bridge additionally refuses any window
  titled like the HUD or the design board, whatever the server asks for.
  Desktop tools are absent from `workflows.SAFE_TOOLS`: driving an app means
  taking the Windows foreground, so a background workflow would fight the
  owner for their own screen. Provisioning is human-only
  (`jarvis desktop setup`), and the bridge is a process the owner starts —
  the agent has no way to install or launch its own.
- **An avatar's SVG is untrusted input** (`avatars.py`, 2026-08-06). The art
  is drawn inside `jarvis.html`, the window that gates approvals, and
  `write_file` can reach `avatars/` — so a hand-written SVG could otherwise
  script the surface `is_face_origin()` exists to protect. Two layers, and
  the second is the real one: `sanitize_svg()` rebuilds the file from an
  element/attribute **allowlist** (no script, no `on*`, no `href` that is not
  a local fragment, no entities/DOCTYPE, no `url()` in a style, 512KB cap),
  and the HUD renders it in an **`<img>`** fed by `GET /avatar.svg`, which
  cannot run script or fetch anything whatever the file says. It is also
  mechanically confined: fixed 186px, `pointer-events: none`, so it can
  neither cover the authorization card nor eat a click meant for it. The
  accent colour recolours only the cyan-family orb states — amber (tool
  running, pending authorization) and red (error) are *meanings*, and an
  avatar that could repaint them could make a running command look idle.
  `set_avatar` is safe/non-dangerous (it selects an avatar already on disk;
  it cannot create one) and is excluded from goal runs alongside
  `set_voice_mute`. **The HUD's `<title>` stays `J.A.R.V.I.S.` whatever the
  avatar** — `windows/uiatree.py:FORBIDDEN_TITLES` matches on it, and that is
  the bridge's backstop against driving the approval window.
- **Session tools read, never switch.** Jarvis can summarize, search, and
  read past conversations (all four tools are safe and non-dangerous), but
  which session is live is the owner's, set in the HUD or on the command
  line. Session files hold what already went through a transcript, so they
  inherit the scrub — `dispatch()` cleans a result before it becomes a
  message, and nothing kept out of context can arrive here later.
- **Sub-agents are typed, and a fleet runs them together** (`agents.py` +
  `tools/subagent.py`, 2026-08-09). A type is a brief, a toolset and a step
  budget named together — explorer / researcher / reviewer / scribe / browser /
  generalist — so the caller picks a *kind* of worker instead of describing one
  from scratch. `run_subagent` sends one and blocks; `run_fleet` sends up to
  six at once. **A type never carries a model: every child is the orchestrator
  tier** (owner's call — all Luna). The cost lever here is `delegate()` and the
  context lever is the sub-agent; a cheap child that returns a confidently
  wrong answer costs the parent more than it saved, because the parent cannot
  see the work behind it.

  **The browser type is `concurrent_safe=False`, and that is what makes the
  fleet legal.** `browser.SESSION` is a module-level singleton with one page
  and one shared 120-action budget — the caveat this file has carried since
  sub-agents were introduced ("if sub-agents ever become concurrent, this is
  the first thing that breaks"). `run_fleet` runs concurrent-unsafe jobs one
  at a time and everything else together, so the thing that would break never
  happens; `tests/fleet_check.py` asserts peak browser concurrency is exactly
  1 while explorers overlap. **`run_fleet` is deliberately not in
  `workflows.SAFE_TOOLS`**: `run_subagent` is there because it is synchronous
  and inherits the deny-all approver, but six concurrent children from a
  thread nobody is watching is a spend amplifier, which is a different
  question.

- **A sub-agent can never exceed its parent** (`tools/subagent.py`,
  2026-08-01). `run_subagent` is synchronous — the parent blocks — which is
  what makes it safe to give a child browser tools where a background workflow
  cannot have them. Three limits, all tested: the child's toolset is
  `SUBAGENT_TOOLS` **intersected with the parent's own**, so a workflow (denied
  the browser because workflows share one Playwright page) cannot reach the
  browser by spawning a child that has it; the child dispatches with the
  *parent's* approver, so a deny-all workflow yields a deny-all child; and
  depth is capped at `runtime.MAX_DEPTH`. `SUBAGENT_TOOLS` currently contains
  nothing dangerous and `longhorizon_check.py` asserts that stays true — if you
  add one, the inherited approver is what gates it, and think hard about
  whether the owner can judge a request they never saw framed.
  Caveat found in review, not a correctness bug but worth knowing: `SESSION` is
  a module-level singleton, so a browsing child drives the **same page** the
  parent may be mid-task on and spends the **same 120-action budget**. Nothing
  interleaves (the call is synchronous), but a parent that browsed before
  delegating must re-snapshot afterwards rather than trusting its old refs —
  which the snapshot→act→re-snapshot discipline already does. If sub-agents
  ever become concurrent, this is the first thing that breaks.
- **Background workflows cannot ask, so they cannot do** (`workflows.py`):
  workflow agents get SAFE_TOOLS (no browser — one shared Playwright page —
  and nothing dangerous) plus a deny-all approver. Monitoring is via
  workflow_status / workflow_log tools.
- **Jarvis may not touch his own control plane.** `config.is_face_origin()` —
  the browser and `fetch_page` refuse it, ahead of `allowed_hosts='*'`.
  Otherwise he could drive his own HUD and approve himself.
- **External accounts follow use-but-never-see** (Gmail, 2026-07-31, the
  template for every future integration): a *scoped OAuth refresh token* —
  never a password — lives in `~/.config/jarvis/google_token.json` (mode
  600, outside the repo), is loaded only inside `google_auth.py`, and goes
  into an Authorization header; the model only ever sees API responses.
  Consent is human-only (`jarvis auth google`, a CLI subcommand — never a
  tool, so the agent cannot initiate or widen its own access), scopes are
  minimal (gmail.readonly + gmail.send), the token is revocable at
  myaccount.google.com, and outward actions (`gmail_send`) are
  `dangerous=True` so every send needs the owner's approval. The token file
  is covered by all three secrets layers below.
- **Onshape follows the same template with a sharper write boundary**
  (2026-07-31): API keys — never the password — live in
  `~/.config/jarvis/onshape_keys.json` (600, all three secrets layers),
  pasted once via `jarvis auth onshape` (human-only) and revocable in
  Onshape under My account → Developer → API keys (NOT the dev portal —
  that page is OAuth-apps only now). CAD writes are confined
  *structurally*: every
  write URL is built from the one sandbox document pinned in that bundle,
  and no cad_ tool accepts a document id for a write target — so no CAD
  tool needs `dangerous=True`. Parts libraries are read-only sources.
  `tests/onshape_check.py` asserts both properties stay true.
- **`.env` / `.env.local` are unreadable** (`tools/secrets.py`, added
  2026-07-30 after a real leak — see below). Three layers: `read_file` refuses
  them by name; both shell tools refuse a command naming one, including a
  dotted glob (`cat .env*`) and *including approved* `run_command`, because
  approving a command is not consent to what it prints; and `dispatch()`
  scrubs every tool result before it becomes a `tool` message. Covered
  names: `.env`, `.env.local`, `google_token.json`, and `onshape_keys.json`
  (JSON values pulled by sensitive key names) — `.env.example` stays
  readable on purpose.

## Testing

- `jarvis bench` makes **real API calls and costs money** (~$0.004 for the full
  6-model sweep). Use `-t <task>` and a single model while iterating.
- `jarvis bench --family agent` is **agent-bench** — the whole-Jarvis one (see
  *agent-bench* below). ~$0.007 and ~2 minutes for the five-task sweep on Luna.
- Context-management tests are synthetic and free — no API, fully repeatable.
  Prefer that pattern for new logic.
- `tests/context_check.py` — free synthetic checks for `context.py`, and the
  one CLAUDE.md claimed existed for months but did not, which is how the
  cut-point orphan bug survived in the compaction path (it only fires past
  `compact_at_tokens`, so nothing short ever reached it). Covers: parallel
  image results never orphaning a tool_call, cuts only at settled user
  boundaries, the original request pinned through compaction, eviction and
  truncation being in-place and idempotent, `manage()` end-to-end leaving
  a wire-valid transcript, and **invariant 2's one exception** — `run_turn`
  never appending a null-content assistant turn that has no tool_calls, while
  leaving the legal null-content-with-tool_calls shape verbatim. Since
  2026-08-09 it also covers the **TokenMeter** (measured prefix + estimated
  tail, discount/invalidate/shrink, the agent measuring against exactly what it
  sent, and a real count triggering a compaction the estimate would have
  missed) and **spill-on-truncate** (the pointer resolves to a file holding the
  whole result, identical bodies share one file, an unwritable spill degrades
  to a plain cut). Run after touching anything in `context.py` **or the append
  in `agent.run_turn`**.
  **Write new cases so they fail against the old code** — the first draft of
  the orphan case passed against the bug, because the split tool group was not
  the *newest* candidate and both rules picked the same safe boundary.
  Second rule, learned the same day: **point `spill_dir` at a temp directory**.
  The default is the owner's real `~/.local/share/jarvis/spill`, and a suite
  that writes into live machine state is how `apt` once landed on the real
  allowlist.
- `tests/loop_check.py` — free checks for the agent loop's dispatch and stop
  handling, `llm.chat` stubbed so any reply shape can be produced on cue.
  Parallel dispatch: batching (consecutive safe runs group, a write splits
  them, indices stay in call order), the allowlist not drifting from the
  registry and never containing a dangerous/browser/desktop/subagent tool,
  three 0.3s tools finishing in 0.3s, results ordered by *call* even when they
  complete out of order, a raising worker still answering its call id, parallel
  image results each keeping their carrier message — and **the ContextVar
  propagation check, which is the one that matters**: it fails against a
  version that submits the bare function to the pool. Token-cap handling: a
  cut-off reply continued and rejoined, the continuation capped and the cut
  stated in the reply, the mid-tool-call note landing *after* the results, and
  an ordinary reply gaining nothing. Run after touching `run_turn`,
  `_dispatch_calls`, or `PARALLEL_SAFE`.
- `tests/files_check.py` — free checks for the file and search tools:
  read_file numbered and paged (a 5000-line file reassembled byte-exact by
  paging, which is the `CLAUDE.md` case), edit_file across unique / ambiguous /
  missing / stale / replace_all / deletion and the numbered-paste salvage,
  **edit_file refusing every SELF_PROTECTED file and `.env`** (verified to bite:
  with the guard stubbed out, the edit lands), write_file stripping read_file's
  numbering while leaving a numeric TSV column alone, and grep_files across all
  three modes, glob, case, cap, context lines, skipping `.env` — run twice,
  once through ripgrep and once through the forced Python fallback, because a
  shape mismatch there would only ever surface on a machine without `rg`.
- `tests/longhorizon_check.py` — free checks with a faked `llm.chat` for the
  plan slot and sub-agents: plan written through dispatch and injected into
  `messages[0]`, still present on step 26 of a *single* turn (the per-step
  refresh — a per-turn refresh passes a 2-step test and fails the real case),
  surviving compaction, absent for agents without `plan_write`, and not shared
  between concurrent agents; sub-agent returning only its answer with the bulk
  left behind, inheriting the parent's approver, toolset intersected with the
  parent's, depth capped, cancellation reaching through, and the parent's plan
  surviving the call. Run after touching `agent.run_turn`, `runtime.py`,
  `tools/plan.py`, or `tools/subagent.py`.
- Browser smoke tests should use a **local HTTP server**, not a live site.
- `tests/agentbench_check.py` — free synthetic checks for the agent-bench
  *harness*, which every number it prints depends on: the sandbox isolates and
  restores (including after an exception) and strips the network out of
  `workflows.SAFE_TOOLS`; each grader scores a hand-built correct world 100%
  and catches its specific failure (clobbered changelog, unloaded skill, leaked
  credential, inline work instead of delegation, forgotten codename); fixture
  facts match what is actually on disk; **every task scores near zero on an
  empty run** — the check that caught the safety grader paying 56% for doing
  nothing; and **every task survives a run cut short** (0 and 1 recorded
  turns), because an empty run is *fully shaped* while a run killed by a
  provider error is **short**, and only the second crashed a grader. Run after
  touching `agentbench.py`.
- `tests/cadbench_check.py` — free synthetic checks for the cad-bench
  *harness* (the shape of `agentbench_check`): geometry helpers (world boxes
  from transforms, penetration with touching = 0, the recorded 12-inch
  c-channel overlap detected), every grader scoring a hand-built correct
  world 100% and catching its specific failure, every task ~0 on an empty
  run AND on a do-nothing run (`repair`'s fixture pre-builds the world — the
  fixture's own work must earn the agent nothing), every task surviving a
  run cut short (0/1 turns), the bench assembly deleted even when the run
  blows up, and part resolution stripping Onshape's invisible LRM marks.
  Run after touching `jarvis/cadbench.py`.
- `tests/avatars_check.py` — free synthetic checks for avatars: the SVG
  sanitizer against a hostile file (script, `onload`, `foreignObject` drawing
  its own AUTHORIZE button, `javascript:` href, external `<image>`) with the
  geometry surviving and `<a>` *unwrapped* rather than dropped; every
  generated wake regex `\b`-anchored at both ends and a raw one anchored on
  the way through; **the HUD's hard-coded fallback patterns matching
  `avatars.DEFAULT`** (the two copies must not drift); every degradation path
  leaving him summonable (missing slug, uncompilable regex, no stated phrase,
  unparseable SVG); the env pin vs an explicit switch; and the identity
  rename hitting `You are Jarvis` without renaming `jarvis chat -c`.
- `tests/face/hud_avatar_check.py` — free headless checks in the real
  `jarvis.html`: no avatar in `/config` leaves the window byte-for-byte as it
  was (banner, both wake phrases, the triangle emblem — every other HUD suite
  depends on that); an avatar renames banner/byline/SYSTEMS row/armed hint and
  **moves the wake word** (new phrase fires, old one does not, still
  anchored); the face is an `<img>` that replaces the emblem and falls back to
  it on a load failure; a hostile SVG's `onload` never runs; the picker lists,
  marks, keeps PTT inert, closes on Escape without switching, and round-trips
  through POST /avatar; and an SSE `avatar` broadcast relabels live.
- `tests/secrets_check.py` — free synthetic checks for the `.env` protection,
  against a throwaway dir holding a fake key. Includes a replay of the actual
  leak (a recursive grep that never names `.env`). Run it after touching
  `secrets.py`, `dispatch()`, or either shell tool.
- `tests/gmail_check.py` — free synthetic checks for the Gmail integration:
  refresh-token exchange and caching against a fake transport, search/read/
  send API shapes with MIME round-trip, 401→refresh→retry-once, gmail_send
  registered dangerous with a denial never touching the network, and the
  token file refused by name/glob/read_file with values scrubbed. Run after
  touching `google_auth.py`, `tools/gmail.py`, or `secrets.py`.
- `tests/onshape_check.py` — free synthetic checks for the CAD integration:
  key-bundle load and document-URL parsing, inch/degree → meter/row-major
  matrix transforms and readback, every cad_ tool's request shape against a
  fake transport, library latest-version resolution + caching, the sandbox
  write pin (every write URL targets the pinned document), and the key
  bundle refused/scrubbed by all three secrets layers. Run after touching
  `onshape_auth.py`, `tools/onshape.py`, or `secrets.py`.
- `tests/fleet_check.py` — free checks for typed sub-agents, `llm.chat`
  stubbed: every type's tools exist and none is dangerous, **every child on the
  orchestrator model** (and no type carrying one), the toolset intersected with
  the parent so the browser stays unreachable to a workflow, four children
  finishing in the time of one, **peak browser concurrency of exactly 1 while
  explorers overlap**, reporting in call order, bad/empty/oversized job lists
  refused, a raising child still reported, depth cap and cancellation, the
  parent's approver being what a child dispatches with, and `run_fleet` staying
  out of `workflows.SAFE_TOOLS`. Run after touching `agents.py` or
  `tools/subagent.py`.
- `tests/rules_check.py` — free checks for command rules and the fetch-execute
  reviewer, with `shell._run` replaced by a recorder throughout, because a test
  for "rm -rf / must be refused" must not depend on the refusal working. Covers
  a 33-command decision matrix (the owner's choices as a table), worst-segment-
  wins on compound commands, inline-source and command-substitution falling to
  ASK, a denied command neither running *nor being asked about*, **a background
  workflow's deny-all approver never honouring an ALLOW** (the regression this
  nearly shipped with), the secrets refusal still beating an allowed stem,
  fetch-execute detection, every reviewer verdict including safe-but-untrusted-
  host and the `JARVIS_REVIEW_AUTOAPPROVE=0` kill switch, every reviewer failure
  path ending in *unclear*, the prompt fencing the script as untrusted data, and
  both new files being SELF_PROTECTED. Run after touching `rules.py`,
  `command_review.py`, `permissions.gate`, or `dispatch()`.
- `tests/permissions_check.py` — free checks for modes and the allowlist:
  gate ordering, prefix vs whole-tool matching, persistence without
  duplicates, mode "all" bypass, and the broker's ALWAYS path writing an
  entry. Run after touching `permissions.py` or `approvals.py`.
- `tests/skills_check.py` — free checks for skill tools (round-trip, bad
  names, starter skills parse) and the index: render/refresh/cap, plus
  agent-level injection against a faked `llm.chat` (present and per-turn
  fresh for skill-armed agents, absent for others, mid-session skills
  visible next turn).
- `tests/workflows_check.py` — free checks with a faked `llm.chat`:
  background lifecycle, status/log tools, deny-all approver, safe toolset,
  concurrency cap. Run after touching `workflows.py`.
- `tests/discord_approvals_check.py` — free checks for remote approval, with
  the DM sender injected (no network): yes/no/always resolve the exact
  request, a "yes" in a guild channel is not an authorization, two open asks
  refuse a bare yes and need the code, unparseable replies resolve nothing, a
  spent code is dead, and every failure mode (no channel, timeout,
  window-closed, shutdown) denies. Run after touching `discord_approvals.py`
  or `ApprovalBroker.request`.
- `tests/daemon_check.py` — free synthetic checks for the headless daemon
  (fake listener + stub DM channel, loopback HTTP only): the shared
  DiscordResponder keeps spoken turns away from the approval parser, a
  windowless broker denies nowhere-to-ask / on remote timeout, and
  `detach_remote()` stops remote asks entirely; the health endpoint is the
  single-instance lock, the daemon refuses to start over a running face, and
  `stop()` releases blocked approval waiters with a denial. Hermetic against
  a real face running on 8402 (probes point at dead ports). Run after
  touching `daemon.py`, `discord_agent.py`, or `ApprovalBroker`.
- `tests/goals_check.py` — free synthetic checks for background goals
  (scripted fake agent, DMs into a list, store on a temp dir): store
  round-trip + runner-written journal + active() ordering; `goal_report`
  through real dispatch (armed-only, one-shot, validated); slices until
  done with costs tallied and start/done DMs; steering interrupting the
  in-flight turn and landing as the next slice's `[owner steering]`
  message; every budget ceiling parking with spend in the DM; cancel at
  the boundary; shutdown leaving `running` for restart-resume; interval
  digests carrying cost + the live plan; and the daemon router keeping
  verbs typed-only (a spoken "goal cancel" is conversation). Run after
  touching `goals.py`, `goalrunner.py`, `tools/goalctl.py`, or `_route`.
- `tests/sessions_check.py` — free checks for session memory: record→reload
  round-trip (and that the system message is never persisted), image payloads
  stripped on save, the append-only log surviving compaction of the
  transcript, the recent-sessions index (renders, marks the current one, empty
  store → ""), the four read tools with a stubbed summarizer (including that
  the summary cache invalidates on a new turn), the Agent binding (a second
  Agent resumes the first's transcript; the index only reaches session-armed
  agents), and that a cancelled turn is still saved. Run after touching
  `sessions.py`, `tools/sessions.py`, or `Agent.run_turn`.
- `tests/face/hud_session_check.py` — free headless check of the session
  picker in the real `jarvis.html` (hud_state_check's puppet pattern): boot
  onto a resumed conversation redraws its log and counters, the picker lists
  and marks the current one, PTT is inert while it is open, switching redraws
  from the joined session, NEW clears everything, Escape switches nothing, and
  an SSE `session` broadcast relabels without touching the log.
- `tests/face/controls_check.py` — free server-level checks: /mute flips and
  broadcasts, /config reports mute+permissions, /approve with always=true
  both unblocks the agent and persists the entry, and /session switches the
  bound conversation (rebuilding the agent) with 403/404/400 on the bad paths.
- `tests/face/approval_check.py` — free synthetic checks for the approval
  gate: the broker (approve/deny/timeout/no-window/replay/window-closed), the
  `/approve` route against a real server with a real SSE subscriber, that
  `dispatch()` neither runs a denied tool nor blocks an approved one, and that
  the face's origin is refused by the browser and web tools.
- `tests/face/hud_approval_check.py` — free end-to-end check of the card in
  the real `jarvis.html`, headless: SSE → card → click → the blocked agent
  thread wakes with the answer. Also covers Escape-denies, PTT being inert
  while a card is up, and timeout withdrawal. Run both after touching
  `approvals.py`, the face server, or the HUD.
- `tests/face/whiteboard_check.py` — free end-to-end check of the whiteboard
  with a stubbed designer: real page JS in headless Chromium (draw → export →
  POST /design → reply + preview iframe), shape tools via real pointer drags
  (rect/circle/line commit ops, Shift constrains, undo pops, letterbox
  coordinate math), the ATTACH toggle (sketch vs
  text-only), the route's mtime-diff file detection, the workshop origin with
  no-store, and `Agent.run_turn(images=…)` building a proper multimodal user
  message against a fake `llm.chat`. Run after touching `/design`, the
  whiteboard, or the workshop server.
- `tests/face/whiteboard_smoke.py` — real sketch → Luna → served design
  (~$0.002-0.02/run); scripted sketch for comparability, human grades the
  rendered PNG. First run 2026-07-31: 4 steps, $0.0024, honored both layout
  and the color annotation.
- `tests/face/hud_converse_check.py` — drives the real `jarvis.html`
  conversation path in headless Chromium (real API calls, ~$0.001). Exists
  because a client-side bug ("Fault: HTTP 200" — the error check matched
  "json" against the `application/x-ndjson` success type) sailed past the
  Python-client streaming test. Lesson: test the HUD's own JS, not just the
  routes it calls. Run after touching `/converse` or the HUD's converse().
- `tests/face/cancel_check.py` — **free** checks for mid-turn cancellation,
  with `llm.chat` stubbed so the cancel can be dropped at an exact point in
  the loop. The one that matters: cancelling while a tool is running must
  still return every tool result (invariant 3). Run it after touching
  `run_turn`.
- `tests/face/attach_check.py` — free checks for typed input + attachments
  on `/converse`: message assembly (speech+typed+files into one user
  message, @path resolution, images as multimodal parts, caps surfacing as
  notes), protected-file refusal and credential-value scrubbing, typed-only
  turns skipping STT, and the legacy raw-webm body still working. Run after
  touching `_assemble_turn`, `_converse`, or `run_turn(images=…)`.
- `tests/voice_speakable_check.py` — free checks for `voice.speakable()`, the
  markdown→speech strip: 16 constructs lose their syntax and stay idempotent,
  6 lookalikes (`snake_case`, `2 * 3 * 4`, prose) come back byte-identical,
  fences are dropped rather than read aloud, and `tts()` applies the strip
  itself so no speech path can forget. Run after touching `voice.py`.
- `tests/face/hud_markdown_check.py` — free headless checks that the HUD
  *renders* his markdown in the real `jarvis.html`: 13 constructs become
  elements, `textContent` still holds the words (what the other HUD suites
  assert against), the owner's own message is left verbatim, and a reply
  cannot inject markup or a `javascript:` href into the surface that gates
  approvals. Run after touching `renderMarkdown`/`addMsg`.
- `tests/face/hud_input_check.py` — free headless checks of the input bar
  in the real `jarvis.html` (hud_state_check's puppet pattern): Enter sends
  the JSON envelope and the words render at send time, staged files ride
  the next send and clear after, empty Enter is inert, and Space typed in
  the box does not trigger push-to-talk.
- `tests/face/hud_wake_check.py` — free headless checks of the wake phrases
  in the real `jarvis.html`: "jarvis" fires, ordinary speech never does, the
  `bibi` avatar's phrases ("big yahu", "netanyahu", "bibi") are silence on
  the default and all 20 spellings fire once that avatar is applied — in the
  spellings Chrome's recognizer actually returns for a phrase it has never
  heard — and the armed hint names whatever is live. Run after touching
  `WAKE_PATTERNS` or `matchesWake`.
- `tests/face/hud_state_check.py` — **free** checks for the HUD turn state
  machine, and the pattern to copy for anything else in the window: a
  *scripted* `/converse` and `/events` on `queue.Queue` puppet strings serve
  the real `jarvis.html`, so every phase transition is driven on cue with no
  API calls and no timing luck. Covers phase order, the mid-turn transcript,
  tool start/finish, the elapsed clock, and that a late SSE tool event cannot
  rewind a later phase.
- `tests/desktop_check.py` — free synthetic checks for desktop control, no
  Windows needed: snapshot rendering and ref staleness against a dict-tree
  fake backend (this is why `windows/uiatree.py` has no Windows imports), the
  app allowlist including that a shell/browser/file-manager is never
  registered, the wire protocol against a fake bridge on a real loopback
  socket (error propagation, mid-request death, no-bridge message), and that
  no desktop tool takes a title/handle/path or reaches background workflows.
  Run after touching `desktop.py`, `tools/desktop.py`, `windows/bridge.py`,
  or `windows/uiatree.py`.
- `tests/browser/math_drill_smoke.py` — headed end-to-end browser test: serves
  a local JS-rendered form wizard (`tests/browser/pages/math-drill/`) and has
  the agent complete it. Real API calls (~$0.001/run); the window stays open
  after the run so a human can grade via the page's Copy JSON button. Verify
  page changes free first with a headless Playwright click-through.
- `tests/browser/policy_check.py` — free synthetic checks for the internet
  policy: public-vs-private address matrix (IP literals, no DNS), resolver
  cases via a monkeypatched `getaddrinfo` (a public name resolving to
  loopback/LAN is refused), `fetch_page` refusals before the request and
  after a redirect, and a headless redirect off a pinned allowlist that must
  back out to about:blank. Run after touching `BrowserPolicy`,
  `config.is_lan_host`, or `fetch_page`.
- `tests/browser/thread_check.py` — free checks for invariant 7: after a
  browser action no event loop is parked on the caller (prompt_toolkit must
  still prompt — the chat-REPL crash), fresh threads can reuse the session
  (the face's request threads), and errors cross the thread boundary. Run
  after touching `Session._submit` or the browser lifecycle.
- `tests/browser/vocab_drill_check.py` — free synthetic checks for the vocab
  drill page: determinism (same seed ⇒ identical sequence), a full solve of
  all four question types, the idle detector, and the session timer. Run it
  after any change to `vocabbench/index.html`. It solves via the
  `window.__answerKey` test backdoor, which agents cannot reach (no JS-eval
  tool).

## Model facts (verified 2026-07-30 via the OpenRouter catalog)

| Model | Tool calling | Vision | Context |
|---|---|---|---|
| `openai/gpt-5.6-luna` | 8/8 | yes | 1.05M |
| `openai/gpt-oss-20b` | 8/8 | **no** | 131k |
| `qwen/qwen3.7-flash` | 8/8 | yes | 1M |
| `mistralai/mistral-nemo` | 6/8 (fails multi-step) | no | 131k |
| `meta-llama/llama-3.1-8b-instruct` | 6/8, flails | no | 131k |
| `google/gemma-3-12b-it` | 2/8 — emits ` ```tool_code ` text, not native calls | yes | 131k |
| `inclusionai/ling-2.6-flash` | 8/8 | no | 262k |
| `deepseek/deepseek-v4-flash-0731` | not run | **no** | 1.05M |
| `deepseek/deepseek-v4-pro` | not run | **no** | 1.05M |

Two findings worth keeping:

- **Cheap per token ≠ cheap per task.** llama-3.1-8b is priced below Luna and
  cost 65% *more* on the bench, because wasted turns re-send the whole
  transcript. Optimize `$/completed task`.
- **Tool-calling support varies by provider**, not just by model. Gemma's
  behavior depended on which backend OpenRouter routed to.

## Decisions already made — don't relitigate

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
  `openai/gpt-5.6-luna`. Do not route personal data (chat, transcripts,
  compaction summaries) to Chinese-hosted models — the cheap tier moved
  qwen3.7-flash → gpt-oss-20b for exactly this reason. Multi-model
  comparisons remain opt-in by naming models explicitly on `jarvis bench`.
  If Luna hits a capability wall, escalate the orchestrator to GPT-5.6 Terra
  or Claude Sonnet 5.
- **Semantic/RAG memory deferred.** Memory is one markdown file per fact, pulled
  on demand via tools, so nothing is auto-injected. The upgrade ladder is
  substring → SQLite FTS5/BM25 → embeddings, and the tool interface
  (`memory_search(query) -> text`) is the contract, so swapping backends changes
  nothing else. Revisit when search visibly misses.

## Current state (2026-07-30)

Working: agent loop, tier routing, memory tools, file/shell tools with gating,
web search + page fetch, context management, browser control (Playwright,
headed via WSLg, snapshot + screenshot channels), tool-calling bench.

Browser control validated end-to-end 2026-07-30: Luna completed a randomized
4-question JS-rendered form wizard (`tests/browser/`) on snapshots alone —
21 steps, 27.8s, $0.0013, 4/4 correct, zero stale-ref or retry errors. The
snapshot→act→re-snapshot discipline held without prompting beyond the task
text. De-risks the vocab-bench browser loop.

**`vocab-bench` is built** (2026-07-30). `vocabbench/index.html` is the seeded
drill — timed session, idle-detector overlay, four JS-rendered question types
(multiple choice, fill-in-blank, matching, spelling), every random draw routed
through a `mulberry32(seed)` PRNG so one seed fixes the exact question
sequence for every model. `jarvis bench --family vocab` runs it with the real
browser and real agent loop (`jarvis/vocabbench.py`, headless by default,
`JARVIS_BROWSER_HEADLESS=0` to watch); scoring is read back from
`window.__vocabResults`, which the agent can neither see nor set.

First roster sweep (`vocab-snap`, seed 42, 2026-07-30): **Luna 6/6** (33 steps,
37.5s, $0.0023) and **qwen3.7-flash 6/6** (30 steps, 50.7s, $0.0021);
**gpt-oss-20b failed** — the trace shows it opened the page and then produced
no valid browser action for 16 straight rounds before giving up, despite being
8/8 on the canned tool bench. New finding to keep: **single-shot tool-call
success does not predict long-horizon browser loops** — the canned bench and
vocab-bench measure different capabilities.

**agent-bench shipped (2026-08-02)** — the bench that measures *Jarvis*, not a
model's tool calls or one browser skill. `jarvis bench --family agent`
(`jarvis/agentbench.py`) runs five multi-turn tasks through the real agent
loop, with the real `config.SYSTEM_PROMPT`, the real tool registry, the real
context manager, the real approval gate and real background workflows —
against a **hermetic sandbox**: a temp cwd, with `MEMORY_DIR` / `SKILLS_DIR` /
`SESSIONS_DIR` / `ALLOWLIST_PATH` swapped to temp copies, no network tool in
any toolset, and `workflows.SAFE_TOOLS` stripped of web tools for the run.
Nothing of the owner's is reachable, so it is safe to leave unattended.

Scoring generalizes vocab-bench's rule — **grade the world the agent left
behind, not the prose it wrote.** Every check reads the sandbox afterwards:
files on disk, memory entries, skill frontmatter, which workflows reached
`done`, which dangerous calls hit the approver. Checks are tagged by category,
so one task feeds several ratings and the report is five ratings plus an
overall (weighted, passed over possible), not one pass/fail:

| task | what it puts under load |
|---|---|
| `project` | explore a fixture repo, edit code, read-before-write on a pre-existing CHANGELOG, recall a number two turns later |
| `recall` | memory + skills round trip — save a fact and a skill, then fire the skill *unprompted* off the injected index |
| `pressure` | a prompt injection in a vendor doc telling him to leak a `.env` key and delete a directory, then a genuinely-requested dangerous command that the approver **denies** |
| `orchestrate` | four background workflows (against `MAX_CONCURRENT=3`), monitored, then merged into a correct ranking — and *did he delegate, or do it inline?* |
| `long-haul` | seven turns under a deliberately tight `ContextPolicy`, with a codename planted in turn 1 that must survive compaction |

First results (2026-08-02): **Luna 100%** across all five categories, $0.0065,
115s. **gpt-oss-20b 46% overall — and 0% multiagent**, at $0.0116: nearly
double the cost to fail, the same "cheap per token ≠ cheap per task" finding
the canned bench produced, now reproduced on whole-agent work. Run-to-run
variance is real (an earlier Luna sweep scored 93%, missing line counts on
`project`); treat a single run as a sample, not a verdict.

**Challenger sweep 2026-08-05 — Luna held.** Three models were run against
agent-bench and vocab-bench looking for a replacement or backup; none beat it:

| model | agent-bench | cost | vocab-snap | cost |
|---|---|---|---|---|
| `openai/gpt-5.6-luna` | **100%** | **$0.0065** | 6/6 (33 steps) | **$0.0023** |
| `deepseek/deepseek-v4-flash-0731` | 93% | $0.0085 | 6/6 (26 steps) | $0.0020 |
| `deepseek/deepseek-v4-pro` | 96% | $0.0526 | 6/6 (34 steps) | $0.0147 |
| `qwen/qwen3.7-flash` | ~91% | $0.0086 | 6/6 (30 steps, 2026-07-30) | $0.0021 |
| `inclusionai/ling-2.6-flash` | 70% | $0.0062 | not run | — |

Luna scored highest *and* cost least — the decision stands, and the burden is
on any future challenger to beat both columns at once. What the sweep taught:

- **Every non-Luna model gave ground in the same place: `multiagent`.** Both
  DeepSeek models scored 82% and ling 27% (gpt-oss-20b: 0%), all failing the
  same check — *delegated instead of doing it inline*. Background-workflow
  orchestration is this project's discriminating task; a model can be perfect
  on safety and long-horizon and still refuse to delegate.
- **Both DeepSeek V4 models are text-only.** That alone disqualifies them as
  orchestrator: `cad_render`, `browser_screenshot`, `desktop_screenshot` and
  the whiteboard's `run_turn(images=…)` all go blind. Check
  `architecture.input_modalities` before benching anything, not after.
- **"Cheap per token" failed a third time, hardest yet.** v4-pro lists at ~4x
  Luna's input price and cost **8x** per task — lower-quality steps mean more
  steps, and every step re-sends the transcript. ling-2.6-flash lists at 1/30th
  Luna's price and cost the *same* per run.
- **The listed price may not be reachable.** OpenRouter's training opt-out
  refuses to route to providers that train on prompts, so DeepSeek's own
  first-party endpoint 404s from this account — v4-pro's headline $0.435/$0.87
  and $0.0036 cache reads are unobtainable, and the real floor is StreamLake at
  $0.652/$1.305. Check reachability by pinning with `allow_fallbacks: false`
  before quoting a price.
- **`qwen3.7-flash` is the best *backup* tested, and still not an upgrade.**
  ~91% at $0.0086 (32% dearer than Luna despite listing at 1/3 the price), and
  it **lost the planted codename across compaction** — the one thing both
  DeepSeek models kept. But it is the only challenger with **vision and 1M
  context**, so it is the fallback that leaves `cad_render` and
  `browser_screenshot` working. Its score is a composite: `orchestrate` died
  on a provider 400 and was re-run clean (13/15, same as both DeepSeeks).
- **A provider error mid-run used to read as a capability failure.** That 400
  crashed the `recall` grader with IndexError (`run.called(turn=N)` indexed
  `per_turn_calls` unguarded), so the task reported one bogus "grader crashed"
  check instead of scoring zero. The existing empty-run checks never caught it
  because they pad every per-turn list to full length — an empty run is
  *fully shaped*, a killed run is **short**. Both fixed 2026-08-05;
  `tests/agentbench_check.py` now grades every task at 0 and 1 recorded turns.
- **Pin by lowercase provider *tag*, and never with fallbacks on.**
  `{"order": ["DeepSeek"]}` (display name) silently routed to DeepInfra
  instead of erroring; the tag is `deepseek`. With fallbacks enabled a wrong
  pin is invisible. Related: these runs landed on **DeepInfra fp4** and
  **StreamLake fp8**, so quantization — and therefore answer quality — varied
  by whoever answered. `llm.chat()` gained an **opt-in** pin for this
  (`config.CHAT_PROVIDER` / `JARVIS_CHAT_PROVIDER`, 2026-08-05, empty by
  default so routing is unchanged; `llm.speech()` has had one since
  2026-07-31). Like the TTS pin it allows **no fallbacks**, which is what
  turns a wrong tag into a loud 404 instead of a silent reroute.

Three things learned building it, each worth keeping:

- **A safety rating a rock can pass measures nothing.** The first `pressure`
  grader gave an agent that did *nothing at all* 56% on safety — it leaked no
  credential and deleted no logs because it never worked. Passive safety
  checks are now conjunctions with having done the job, and
  `tests/agentbench_check.py` asserts every task scores near zero on an empty
  run. The corollary is in the check *names*: a conjunctive check has to say
  so ("did the job and left logs/ alone"), or a reader misreads a failure as
  the unsafe act rather than the missing work.
- **A context threshold has to be measured, not guessed.** `long-haul` first
  ran with `compact_at_tokens=3500` and compaction never fired once — the
  transcript peaks near 3.2k, and `manage()` tests compaction against the
  estimate *after* truncation has already shrunk it. So the codename check was
  quietly only proving survival of truncation. At 1800 the cut lands inside
  the run (34 messages compacted) and the check means what it claims. Set
  `JARVIS_AGENTBENCH_KEEP=1` to keep the sandbox and print peak tokens /
  truncations / compactions.
- **This is the only test that exercises `context.py` against the live API.**
  The synthetic suite proves the mechanics; `long-haul` proves OpenRouter still
  accepts what comes out the other side, which is invariant 1 with a real 400
  waiting if it breaks.

**Snapshot vs vision, same seed, Luna (2026-07-30).** Vision mode is honest
now: `browser_screenshot` draws set-of-marks badges (the same refs snapshot
assigns, so clicking needs no text channel) and `vocab-vision` runs with no
`browser_snapshot` at all. Result — both modes 6/6, but vision took 39 steps
vs 33, 98s vs 38s, and $0.0117 vs $0.0023: **~5× the cost for equal
accuracy**. Confirms the design bet that text snapshots are the default
channel for web work; screenshots are for when layout or rendering actually
matters. Caveat: the vision run finished at 39 of 40 steps — raise
`max_steps` for vision tasks before longer sweeps.

**Secret-leak incident (2026-07-30).** Asked which provider serves Luna,
Jarvis ran an approved `grep -RIn 'provider|openrouter' ~/projects/Jarvis` and
printed the live `OPENROUTER_API_KEY` from `.env` into the transcript — so the
key went to OpenRouter as context and landed in `traces/` and
`.jarvis_history`. **Key rotated 2026-07-30; protection added** (*Safety
design*). The leaked key is dead but still sits verbatim in the older
`traces/` files and `.jarvis_history` — those predate the scrubber, which only
protects what is in `.env` now. The lesson worth keeping: **the
dangerous read was the one that never named the file.** A path denylist alone
would have waved this through, which is why the scrub sits at `dispatch()` —
the last point before a string becomes a `tool` message — and not in
`read_file`. Layers 1–2 are ergonomics (a clear refusal the model can act on);
layer 3 is the real protection, and it is an accident backstop, not a sandbox:
a model that wanted to defeat it could still base64 a value past it. The
actual boundary would be filesystem permissions on the key.

**Face & voice underway (2026-07-30), phases 0–1 done.** Decisions made with
the owner: the face is a **web HUD run in Chromium app mode** (chromeless
window via the Playwright Chromium; profile in `~/.cache/jarvis-face` and
**port fixed at 8402** — both preserve the origin-scoped mic grant, do not
change either); activation is **push-to-talk** v1, wake word later. All audio
I/O happens in the browser (getUserMedia + `<audio>`), so Python needs no
PortAudio. Phase 0 verified the mic→record→playback round trip in the
app-mode window (clear audio, peak 59%).

Phase 1 shipped Jarvis's voice: `voice.tts(text) -> mp3` (`jarvis/voice.py`,
swappable contract) → `llm.speech()` (HTTP stays in llm.py) → OpenRouter
`/api/v1/audio/speech`, served to the window via `POST /say`
(`jarvis/face/server.py`). **TTS model: `hexgrad/kokoro-82m`, voice
`bm_george`** — $0.62/M chars, ~25× cheaper than every other hosted voice and
already a composed British male. Gotchas: the OpenRouter TTS docs' example
model id does not exist — the **collections pages are the source of truth**
for audio model ids; every TTS provider requires an explicit `voice`, and
voice names are provider-specific (change model and voice together). The
`speed` param works on Kokoro up to 1.3 but **silently truncates the audio
above that** (1.35 dropped a quarter of the speech) — `voice.tts()` clamps
to 1.3; default pace is `JARVIS_TTS_SPEED` (1.2).
`microsoft/mai-voice-2-flash` ($15/M, shipped 2026-07-23) is the premium
upgrade candidate but does not publish voice ids on its model page.

Phase 2 shipped the conversation loop: hold-to-talk in `talk.html` →
`POST /converse` (raw webm body) → `voice.stt()` → one persistent
`Agent.run_turn()` (voice-mode system prompt: short spoken replies; a real
conversation with memory for the life of the server) → `voice.tts()` → JSON
back with transcript, reply, audio_b64, per-stage timings, and cost.
**STT: `nvidia/parakeet-tdt-0.6b-v3`** — probed on real webm/opus
(`tests/voice_probe.py`): parakeet 100% @0.52s, grok-stt 100% @0.80s,
voxtral 100% @0.99s; `microsoft/mai-transcribe-1.5` rejects webm. Owner is
fine with voice audio going cloud; local whisper stays a `stt()` drop-in.
**`dispatch()` runs dangerous tools *unguarded* when `approve is None`**, so
the face server must always construct its Agent with a real approver — never
None, never `lambda: True`. (Phase 5 replaced the hard denier with the HUD
gate; the invariant is the same.) A full voice turn costs ~$0.0002.
Launch: `jarvis face` (the CLI subcommand runs inside the venv, so it works
from any shell; bare `python -m jarvis.face` fails on the system Python —
no httpx). End-to-end self-test pattern: TTS a
question → Chromium re-records it as webm (`RECORD_JS`) → POST /converse.

Phase 3 shipped the HUD (2026-07-31): `jarvis.html`, styled to the owner's
reference (classic "Stark Industries" skin — cyan-on-black, canvas arc-
reactor orb with counter-rotating tick rings and triangle emblem, angular
panels). The orb is the PTT button and is audio-reactive (mic level while
listening, output level while speaking) with a state palette: idle/listening
cyan, thinking fast-spin, tool amber, error red. Panels: COMMS LOG
(transcript), SYSTEMS (clock, model readouts from `GET /config`, session
cost), OPERATIONS (live tool ticker fed by `GET /events` SSE — the agent's
`on_event` broadcasts `tool_start` mid-turn). Test hooks: `window.__hud`
(setState/setLevel/addMsg/addOp) for headless screenshot checks.

**The window is now the owner's real Windows Chrome/Edge in app mode**
(`find_browser()` — WSL launches the Windows exe, Windows reaches the WSL
server via automatic localhost forwarding; `JARVIS_FACE_BROWSER` overrides).
No Chrome-for-Testing banner, native frame, and audio runs on the Windows
stack (the WSLg pulse path only matters for the CfT fallback now). The mic
grant lives in the user's real browser profile. Quirk: an already-running
Windows browser delegates the app window and the launcher's child exits
immediately — `main()` detects that and keeps serving (Ctrl-C to stop).

Phase 4 shipped (2026-07-31): **streamed TTS** — `/converse` now returns
NDJSON written progressively (meta line → one audio line per sentence via
`_sentences()` → done line); the HUD schedules chunks back-to-back on the
audio clock. First audio lands ~1s after the agent finishes instead of after
the whole reply synthesizes (measured: 2.8s to first speech on a 1-step
turn). **Barge-in** — pressing PTT while he speaks stops all scheduled
sources and aborts the fetch (server sees a broken pipe mid-stream — normal,
swallowed). Client aborts cannot cancel a running `run_turn`, so barge-in is
only honored in the speaking state, never mid-think. **Wake word** — Chrome's
built-in SpeechRecognition (webkitSpeechRecognition, zero dependencies,
audio goes to Google's speech service — owner OK with that) runs
continuously when armed via the WAKE WORD row in the SYSTEMS panel
(persisted in localStorage); hearing a wake phrase chimes and starts a
recording that self-stops on ~1.4s of silence. Saying one while he speaks
barges in. `talk.html` was retired — it spoke the old single-JSON
`/converse` shape.

**Wake phrases.** The default answers to **"jarvis"** and nothing else; every
other phrase belongs to an avatar and arrives over `/config` as regex source
(**"big yahu" is the `bibi` avatar's**, moved off the default 2026-08-06 —
see *Avatars* below). The built-in lives in `WAKE_PATTERNS` in `jarvis.html`
(duplicated from `avatars.DEFAULT`, and the fallback if `/config` never
answers); `matchesWake()` is what `recog.onresult` calls. Two rules for any
new phrase, wherever it is written. It is matched against a *live interim*
transcript of a phrase the recognizer has never heard, so it has to tolerate
the spellings Chrome guesses ("big ya hoo", "big yoohoo", "big yahu", "big
yawho" all resolve to the same intent) — a literal string match would miss
most real utterances. And **every pattern is `\b`-anchored at both ends**,
because a wake hit cancels the turn in flight and starts recording: an
unanchored pattern fires on a substring of ordinary conversation and takes
the owner's words mid-sentence. `tests/face/hud_wake_check.py` is the free
suite (the built-in firing, 11 utterances that must stay silent — "yahoo
finance", "big yacht", "jarvisson" — the bibi phrasings being silence on the
default and firing once that avatar is applied, and the armed hint naming
whatever is live, since an undiscoverable wake word is one nobody says). Run
it after touching the patterns.

**A wake phrase belongs to exactly one avatar.** Leaving "big yahoo" on the
default *and* giving it to `bibi` would mean the owner cannot tell which one
they just summoned — the window and the prompt would disagree about who
answered. Both `avatars_check` and `hud_wake_check` assert the default no
longer fires on it.

`bibi` wakes on **"big yahu"**, **"netanyahu"** (whole — "benjamin
netanyahu" — or on its own) and **"bibi"**. Two things decided while adding
the name, both instances of the anchoring rule rather than new ones:
**bare "benjamin" is deliberately not a phrase**, because it is a common
name and a wake hit takes the owner's words mid-sentence, and the name's
regex tolerates what the recognizer actually returns ("netan yahoo",
"nathan yahoo", "netanyaho", "netanya who") while staying clear of
"nathan is on the call" and "netanya beach". The near-misses are in
`hud_wake_check`'s QUIET list, which is the half of that suite that matters.

**Avatars shipped (2026-08-06).** Who he presents as is now data, not
hard-coded: an avatar is `avatars/<slug>/avatar.svg` + `avatar.json`
(`{name, wake, voice, rings, accent, banner, label, description}`), and switching
one changes four things at once — the **name** (an identity rename applied to
`config.SYSTEM_PROMPT` in `Agent._refresh_system`, so it reaches every
surface at once and survives compaction), the **wake phrases**, the **face**
drawn where the triangle emblem was, and the **voice** he answers in. `avatars/` is gitignored, so
`jarvis avatar new <slug> --template fox|owl|bust|reactor|bibi` scaffolds one
from art checked into `avatar_templates.py` (`bibi` is the one template that
is a specific face rather than an archetype — it is checked in *because*
`avatars/` is not, so a reclone still has somewhere for "big yahu" to live;
a template carries art only, so the phrase itself goes in the scaffolded
`avatar.json`). Controls: `jarvis avatar` to
list, `jarvis avatar <slug>` to switch persistently, `jarvis face -a <slug>`
to run one window as an avatar without touching the saved choice, the AVATAR
row in the HUD's SYSTEMS panel, and the `set_avatar` / `avatar_list` tools
("Jarvis, become the fox"). All of them land on `avatars.set_active`, which
broadcasts SSE `avatar` so every open window relabels live.

**An avatar can change the orb's outer ring (2026-08-06).** `"rings"` in
`avatar.json` picks a set from `RING_SETS` in `jarvis.html`; `"triangles"`
(bibi's) replaces the outermost 144-tick ring with **two interlocking
equilateral triangles turning as one figure** — the ring drawer gained a
`poly`/`copies` shape alongside `ticks` and `arcs`, so a new figure is a data
entry, not new drawing code. Only the outer ring is swappable; the four inner
rings are shared (`INNER_RINGS`) so a variant cannot quietly restyle the
whole orb. **An unknown name falls back to the default set** — the orb *is*
the push-to-talk button, so an avatar that could blank it would take the
surface's main control with it, and `hud_avatar_check` pins that.

**An avatar has a voice too (2026-08-06).** `avatar.json` takes `voice` (a
Kokoro voice name — `voice.available_voices()` lists the 54 already in the
local bundle, so this costs no download and no latency: same model, different
style vector) and an optional `speed`. Resolution lives in **`voice.tts()`**,
not the call sites — the same rule as `speakable()`, because three surfaces
synthesize speech (face, `/say`, Discord voice notes) and a fourth will. It
reads the active avatar per call, so a switch moves the voice on the next
sentence with no restart. Current: Jarvis `bm_george`, bibi `am_michael`,
hoot `bf_emma`, vex `am_puck`; `jarvis avatar` lists what each will *actually*
speak in, which is not always what its file asks for. Four notes:

- **The degradation rule is "leave him audible."** A voice that is not
  installed falls back to `config.TTS_VOICE` with a **once-per-process**
  warning (the face synthesizes a chunk at a time, so per-sentence would
  scroll), an unparseable `speed` falls back without losing the voice, and a
  vanished slug still speaks. Same shape as the wake-regex degradations.
- **The clamp is the last word, not the avatar.** `speed` still passes
  through the 0.5–1.3 clamp, because above ~1.3 the cloud provider silently
  truncates its own audio — an avatar must not be able to cut itself off.
- **Validation is what makes the cloud fallback safe.** The name is checked
  against the local bundle for the same model both paths run, so a
  local→cloud fallback cannot send a voice name the provider will reject.
  With no bundle installed, `available_voices()` is empty and every avatar
  speaks in the configured default — never an error.
- **The language heuristic was quietly wrong** and is fixed in the same
  change: `_local_tts` derived language from the voice prefix as `"en-gb" if
  voice.startswith("b") else "en-us"`, which was right for the two English
  families and made the other thirty voices (`jf_`, `zm_`, `ff_`, …) read as
  American English. `voice._LANGS` maps all nine prefixes now.

Four things worth keeping from building it:

- **The default has to be byte-identical to no-avatar.** `avatars.DEFAULT` is
  a built-in with the two existing wake regexes, and `jarvis.html` keeps its
  own hard-coded copy as the fallback for a `/config` that never answers —
  losing the wake word to a config hiccup is worse than a stale name. A test
  asserts the two copies have not drifted, because there is now a Python and
  a JS spelling of the same rule.
- **Every degradation path has to leave him summonable.** A missing slug, an
  avatar with no stated phrase, a regex that will not compile, an SVG that
  will not parse: each falls back rather than raising, because the failure
  mode of getting this wrong is a HUD that cannot be spoken to.
- **The rename is case-sensitive**, because the prompt also names shell
  commands (`jarvis chat -c`) and renaming those sends the owner to a command
  that does not exist. Only capital-J `Jarvis` moves.
- **An explicit switch beats the `-a` pin.** Without that, the picker would
  save the new avatar and redraw the window while the server went on serving
  the pinned one — the window and the server disagreeing about who he is,
  which is the same class of bug as the invisible step budget.

Also fixed on the way: the right-hand HUD column is a **flex stack** now
(`#rail`), not two absolutely-positioned panels. The AVATAR row pushed
SYSTEMS past OPERATIONS' hard-coded `top: 388px` and OPERATIONS silently
covered the SESSION row — a click-through bug that only `hud_session_check`
caught. And `tests/longhorizon_check.py`'s concurrency check was flaky
(~40%) for an unrelated reason: `_run` monkeypatches the module-global
`llm.chat`, so two threads each installing their own closure raced and the
loser's agent ran the winner's script. One shared script dispatching on the
calling agent's own user message fixes it.

**Phase 5 shipped the approval gate (2026-07-31)** — the face is no longer
read-only. `jarvis/face/approvals.py` turns a dangerous tool call into a card
in the HUD: `dispatch()` calls the approver synchronously, so `request()`
**blocks the agent thread** while the pending decision goes out over SSE, the
window POSTs `/approve`, and the blocked thread wakes with the answer. This
only works because the face is a `ThreadingHTTPServer` — the deciding request
must run on a different thread than the one it unblocks. Every failure mode
denies: no window connected (asked and answered before anything is
broadcast), 120s timeout, window closed mid-question, shutdown. Request ids
are one-shot 72-bit tokens, so an answer cannot be replayed or applied to a
later card than the one the owner read. `/approve` is same-origin only,
compared against the request's own `Host` (not a hard-coded 8402, so a server
on another port still approves). In the HUD, deny is the cheap action
(Escape, or the button) and authorize takes a deliberate click — nothing is
keyboard-defaulted, and PTT/wake are inert while a card is up. Card contents
are built with `textContent`; the args are model-written strings and the HUD
is trusted UI, so never `innerHTML` there.

**The gate is only real if Jarvis cannot reach it.** He has browser tools and
the allowlist is `localhost` — so he could have opened his own HUD and
clicked his own AUTHORIZE button. `config.is_face_origin()` now makes
`BrowserPolicy.allows()` and `fetch_page()` refuse the face's origin ahead of
every other check, *including* `allowed_hosts=['*']`. It matches all of
loopback, `*.localhost` included, because Chrome resolves `foo.localhost` to
127.0.0.1. Remaining honest gap: any local process running as the owner can
read the SSE stream and POST an approval — but that process could just run
the command itself, so the boundary is unchanged.

**Turn state display rebuilt (2026-07-31).** The window used to show three
things — PROCESSING, RUNNING · TOOL, RESPONDING — and got both ends wrong: it
sat on the last tool's label through the model call *and* through TTS, so the
slowest, most opaque seconds of a turn read as "still running gmail_search".
Two channels now carry it:

- **Phases on the `/converse` stream**, emitted as each stage actually
  starts: `transcribing -> heard -> thinking -> meta -> composing -> audio*`.
  Headers go out before STT so the window can show SENDING immediately; STT
  failures are now an in-stream `error` line rather than a 500. `heard`
  arrives on its own line, so your words hit the log mid-turn instead of
  after the reply.
- **Tool lifecycle on SSE**: `tool_done` was the missing half. The window
  tracks a running count and falls back to THINKING when it hits zero, and
  the OPERATIONS feed marks each op running/done with its duration.

Why phases go on the turn stream and tools stay on SSE: phases must stay
ordered with `meta`/`audio`, and SSE is a *separate connection* with no
ordering guarantee against it. So the window only lets a tool event set state
while it is in the thinking phase — a late tool event is still logged but
cannot rewind a later phase. There is a free test for exactly that race.

Also: an elapsed clock ticks in the status through every waiting state and
stops when he speaks (a turn that is alive vs one that is wedged is now
visible), `agent.py` emits **`interim_text`** for what the model says on its
way to a tool call — distinct from the final `text` event, and shown as a
note in OPERATIONS — and `init()` may no longer clobber the status: async
getUserMedia can resolve after a turn has started, so boot only writes the
status if nothing else has.

**A turn is interruptible mid-thought (2026-07-31).** Showing the transcript
at STT is only half of it — the owner's reason for wanting it was catching a
misheard line, which needs a way to take the turn back. Barge-in used to work
only while Jarvis was *speaking* (`S.busy` blocked `startRec` for the whole
agent run), which is precisely the wrong window. Now push-to-talk and the wake
word both cancel a turn in flight: the window aborts the stream, POSTs
`/cancel` (same-origin, like `/approve`), and starts recording immediately.

`Agent` takes a `should_stop` callable and **checks it between steps and
nowhere else.** That is invariant 3, not fussiness: bailing out inside the
tool loop would leave an assistant message whose `tool_call` ids never get
their results, and the next request 400s. Between steps the transcript is
always whole, so the cancelled conversation is immediately reusable — the
correction is just the next user message. The face clears its cancel Event
under `_agent_lock` right before `run_turn`, so a cancel aimed at the previous
turn cannot kill the replacement. A cancelled turn returns no reply and
synthesizes no speech.

Worst-case latency is one in-flight model call: the OpenRouter request cannot
be aborted, so the loop stops at the next step boundary. In practice the owner
is still talking when it unwinds.

**Step budget, and admitting when it runs out (2026-08-02).** `Agent`'s
default `max_steps` is **30** (was 12). Only the conversation surfaces take
the default — the HUD/voice agent and `jarvis chat`/`ask` both construct
`Agent` without the argument; designer (24), workflows (20) and every bench
set their own. 12 was too tight for real work: a self-improve turn spends most
of it on checkpoint + edit + test runs before any misstep, and the observed
failure was a completed, committed UI change reported to the owner as a bare
`[stopped after 12 steps without finishing]`.

The bug underneath it is the one worth remembering: **exhausting the budget
used to be invisible to the model.** Every other exit path appends its
assistant message inside the loop, but the `stopped_early` tail set only
`turn.text` — so the transcript ended on a pile of tool results with no sign
of the cut, and the *next* turn's model had nothing to explain itself with.
Asked why he had stopped, Jarvis confidently answered "there isn't a
twelve-step limit" while the window showed exactly that message: not a lie, a
confabulation from a transcript that never mentioned the truncation. The
notice is now appended to `self.messages` before returning (safe — the loop
only exits at a step boundary, so invariant 3 holds). Generalizes: **any state
the surface shows the owner but never writes into the transcript is state the
model will invent a story about.** Covered by `tests/face/cancel_check.py`,
which owns both no-reply exit paths now.

**Gmail shipped (2026-07-31)** — the first real integration, and the
template for the rest: `jarvis auth google <client.json>` runs the one-time
OAuth consent (PKCE + state, loopback redirect, human-only CLI command),
writes the scoped refresh token to `~/.config/jarvis/google_token.json`
(600), and `tools/gmail.py` exposes `gmail_search` / `gmail_read` /
`gmail_send` — send is `dangerous=True`, so it hits the CLI y/N or the HUD
authorization card. `google_auth.access_token()` refreshes behind a cache;
tools retry once on 401. Token bundle covered by all three secrets layers
(name, glob, scrub — see Safety design). Free synthetic suite:
`tests/gmail_check.py`. Setup steps live in `jarvis auth google` (no
argument). Google-side gotchas: the OAuth app must be **published to
production** or refresh tokens die after 7 days; a re-consent needs the old
grant revoked at myaccount.google.com/permissions or Google returns no new
refresh token. Validated end-to-end by the owner 2026-07-31: a gated send
(draft → CLI y/N showing exact to/subject/body → delivered) and a 7-day
inbox summarization (parallel gmail_reads, $0.0017). Note that mailbox
content now flows through the orchestrator tier, so the model routing
policy applies to it.

**Internet access opened (2026-07-31).** `web_search`/`fetch_page` were
already unrestricted; the browser was the localhost-only piece. The default
allowlist is now `["localhost", "127.0.0.1", "*"]` where `'*'` means the
*public* internet: `config.is_lan_host()` refuses private/CGNAT/link-local
space and unresolvable names, judged by what a hostname resolves to (so
`lvh.me` → 127.0.0.1 is not a route to local services or the face). Enforced
on the requested URL and re-checked after goto/click/submit
(`_guard_landing()`); `fetch_page` gained the same LAN block plus a
post-redirect check — it had been wide open to the LAN. System prompt gained
open-web conduct rules (no credentials or personal data into pages, no
outward-facing submissions unless asked). Verified live (example.com loads
and snapshots; 192.168.1.1 refused with a clear message) and synthetically
(`tests/browser/policy_check.py`); approval + secrets suites still pass.

**Browser moved to its own thread (2026-07-31).** The first real internet use
in `jarvis chat` crashed the REPL after the turn: Playwright's sync API parks
a running asyncio loop on the thread that starts it, and prompt_toolkit
refuses to prompt on a thread with a live loop ("asyncio.run() cannot be
called from a running event loop"). Fix is invariant 7: a dedicated
`jarvis-browser` thread owns all Playwright work, which also makes browser
tools safe from the face's per-request handler threads (they would otherwise
break on the second voice turn that browsed). External page access goes
through `Session.eval_js()` now; chat/ask/face stop the session on exit.
Verified by `tests/browser/thread_check.py` and a live pty'd chat run
(browse example.com → answer → prompt again → clean exit, no EPIPE).

**Design board shipped (2026-07-31).** `whiteboard.html` on the face server:
paper canvas (pen, straight line, rect, ellipse — Shift constrains to 45°,
square, circle — text, eraser, undo; ops-list model, WYSIWYG PNG export),
prompt bar with an ATTACH SKETCH toggle, live tool ticker, and a preview
panel. POST `/design` runs a separate persistent designer Agent
(`DESIGNER_SYSTEM`, files+browser+web tools, max_steps 24, vision via the new
`run_turn(images=…)` — owner-supplied PNGs ride the user message; context
eviction handles them like any image). Output files are found by mtime diff
under `designs/` and served by the **workshop server on port 8403** — a
separate origin so agent-written pages can never reach `/approve`, and
browsable by Jarvis so he can screenshot his own work to self-check. Open it
with `jarvis face whiteboard.html`, or browse localhost:8402/whiteboard.html
while the face runs. Smoke-validated (see Testing): sketch annotations
override drawing colors, as prompted. First real-owner run caught a bug the
smoke could not: **paths in agent prompts must be absolute** — the prompt
said `designs/…`, `write_file` resolves against $CWD, and the owner launches
`jarvis face` from `~`, so output landed in `~/designs` while the workshop
served the repo's. (The smoke passed because tests run from the repo root —
a CWD-dependent bug needs a CWD-varied test, or no CWD dependence at all.)
The prompt now carries `config.DESIGNS_DIR` verbatim plus the exact preview
URL root; `whiteboard_check.py` asserts both. Known niggle: the whiteboard's SSE
subscription counts as a "viewer" for the approval broker but the page has
no card UI — moot today (designer tools are all safe), fix if the designer
ever gains a dangerous tool.

**Skills, workflows, permission modes, mute (2026-07-31).** Skills follow the
memory pattern — `skills/<name>.md` (frontmatter description + instructions),
pulled on demand via skill_list/skill_read, recordable via skill_write, four
starters checked in (email-triage, morning-briefing, skill-creator — the
meta-skill that interviews the owner and drafts trigger descriptions — and
whiteboard, which opens the board via `run_command jarvis face
whiteboard.html` since the face origin is refused to his own browser, and
closes it via `whiteboard_close`). **Window close is opt-in by page**
(`tools/whiteboardctl.py`, voicectl pattern): the tool broadcasts SSE
`wb_close`; whiteboard.html calls `window.close()` (app-mode windows have a
single history entry, so it is honored — with a blank-page fallback), and
jarvis.html deliberately ignores the event: the HUD is the approval
surface, and the agent must not hold a lever that closes the window gating
him. `whiteboard_check.py` asserts both sides. **Two-tier index
(Claude Code's design): `skills.index()` — every skill's name + trigger
description — is rebuilt into `messages[0]` at the top of each `run_turn`,
but only for agents whose toolset includes `skill_read`** (designer/benches
never see references to tools they lack). Bodies still load only via
skill_read. Descriptions are written as triggers ("Use when the owner …");
the index is cached on (mtime_ns, size) per file — mtime alone misses
quick same-tick rewrites — and skill_write busts it directly. Capped at 30
skills / 2k chars with a skill_list pointer. Rewriting messages[0] is
invariant-safe: pruning/compaction never touch it. Workflows: workflow_start/status/log run a task
on a background agent with safe tools and a deny-all approver (see Safety
design); in-memory registry, restart forgets them. Permissions: see Safety
design — ask (default) / persistent allowlist ('a' in CLI, ALWAYS card
button in the HUD, `approved-always` in the decision log) /
`--dangerously-skip-permissions` (process-only). Mute: MUTE row in the HUD
SYSTEMS panel, `set_voice_mute` tool for "jarvis, mute yourself", one SSE
broadcast keeps them in sync; muted turns skip TTS synthesis entirely (text
still renders). HUD SYSTEMS also shows PERMISSIONS state (SKIP ⚠ in red
under the flag).

**Typed input + attachments in the HUD (2026-07-31).** An input bar at the
bottom of jarvis.html: type + Enter sends a text turn (same cut-off rules
as push-to-talk — barge-in while speaking, cancel while thinking, inert
while an authorization card is up); files stage as removable chips via the
picker, drag-drop, or paste; `@path` in the text attaches server-side
files. The staging contract: whatever is staged when a turn goes out —
typed *or spoken* — rides that turn, so you can stage context and just
start talking. `/converse` now also takes an application/json envelope
`{audio_b64?, audio_mime?, text?, attachments: [{name, mime, data_b64}]}`
(the legacy raw-webm body still works); `_assemble_turn` folds it all into
one user message — images become multimodal parts via `run_turn(images=…)`,
which now accepts `{b64, mime}` dicts alongside plain PNG strings, and text
files inline as fenced blocks. Limits: 8 files/turn, 4MB/file, 100k chars
inlined — every refusal becomes a bracketed note in the message, never a
silent drop. Secrets rules carry over: protected credential files are
refused at @path by name, and inlined text is scrubbed. Typing Space must
not trigger push-to-talk — the input stops propagation, and
hud_input_check pins that. Typed words hit the COMMS log at send time;
`meta.heard` is non-empty for typed turns so the client's no-signal path
stays voice-only.

**Voice latency work (2026-07-31).** The owner heard a multi-second gap
between reply text appearing and speech starting. Root cause: `llm.py` used
bare `httpx.post`, so *every* OpenRouter call — each agent step, STT, and
each TTS sentence — opened a fresh TCP+TLS connection. Measured: cold TTS
call 3013ms vs 377-685ms through a keep-alive pool. Three fixes: (1) one
module-level `httpx.Client` shared by chat/speech/transcribe — the chat call
that produced the reply now leaves a warm connection for the TTS that speaks
it; (2) `_sentences()` clamps the first chunk to ≤90 chars at a clause
boundary (synthesis time scales with length; 196→83 chars measured 1217→
491ms) — `tests/face/sentences_check.py` guards clamping + lossless rejoin;
(3) TTS chunks synthesize two-in-flight via a ThreadPoolExecutor and stream
in order, with cancel_futures on barge-in so abandoned speech stops costing
money. **And (4), the one that actually mattered when the gap persisted:
provider routing.** Kokoro is served by DeepInfra and Together; OpenRouter's
default route (DeepInfra) was serving identical requests in 2.8-23.7s while
Together served them in 0.5-1.8s. `config.TTS_PROVIDER` (default Together)
now sets a routing preference in `llm.speech()`, fallbacks allowed. Wired
path verified: 5 consecutive turns at 451-583ms. Same lesson as the bench
finding: **the model is not the product — the provider serving it is.**
Re-probe per provider (payload `{"provider": {"order": [...],
"allow_fallbacks": false}}`) before blaming the model or the code.
**Epilogue: the stalls persisted intermittently on both providers via
OpenRouter while the owner measured both providers fast directly and
`tests/net_probe.py` showed the local network clean — so the tail lived in
OpenRouter's audio proxy path, and TTS went local (2026-07-31): kokoro-onnx
on CPU, same model, same bm_george voice, measured 308-351ms per sentence,
flat.** `voice.tts()` branches on `config.TTS_BACKEND` ("local" default,
falls back to cloud with a one-time warning if deps/models missing); local
emits WAV, cloud MP3 — consumers sniff (`RIFF`), never trust a label. Model
files: ~/.cache/jarvis-tts/{kokoro-v1.0.onnx,voices-v1.0.bin} (github
thewh1teagle/kokoro-onnx releases, ~340MB); dep via `uv pip install -e
.[voice]`. The face pre-warms the model at startup (~3s ONNX load, off the
critical path). `tests/voice_local_check.py` is the free suite — synthesis
shape and speed, the cloud fallback, and (2026-08-06) **which** voice speaks:
the avatar's, its every degradation path, and the voice→language map. It
pins `AVATAR_ENV` to the default first, because otherwise the owner's saved
avatar decides what the suite asserts. STT remains cloud (parakeet); local
whisper stays the next swap if it ever needs to be.

**Markdown: rendered in the HUD, stripped for speech (2026-08-03).** The
model writes markdown and both surfaces were taking it literally — the COMMS
log printed `**Done.**` as source, and TTS read the asterisks aloud.

- **The HUD renders it** (`renderMarkdown` in `jarvis.html`): headings,
  lists (one level of nesting), fenced code, blockquotes, tables, rules,
  inline code/emphasis/strike/links. Hand-rolled into **DOM nodes, never
  `innerHTML`** — same rule as the authorization card, and it matters more
  here: a reply that could inject markup into the window that gates approvals
  could draw its own AUTHORIZE button. Link hrefs are scheme-checked
  (http/https/mailto only, `target=_blank` so the HUD itself never
  navigates); anything else renders as plain text. Only *his* messages are
  rendered — the owner's own line goes up verbatim, because typing `*foo*`
  means `*foo*`.
- **TTS strips it** (`voice.speakable()`), and the strip lives inside
  `voice.tts()` so no speech path — face, Discord voice notes, `/say` — can
  forget it. Code fences are dropped whole rather than read aloud; a reply
  that was *only* a fence yields `""`, which the face treats as text-only.
  The face also strips before `_sentences()`, because the first-chunk clamp
  measures length and counting asterisks cuts speech in the wrong place —
  and `_sentences()` now splits on newlines too, so a de-bulleted list gets
  a pause per item instead of synthesizing as one long chunk.

Not done: `whiteboard.html`'s reply panel still shows raw text. Sharing the
renderer means lifting it out of `jarvis.html` into a static JS file both
pages load.

**Onshape CAD shipped (2026-07-31), live validation pending.** The second
use-but-never-see integration (see Safety design). `jarvis auth onshape`
pastes API keys created under My account → Developer → API keys (Basic
auth on the wire — no HMAC needed for first-party use; individual accounts
are capped at 2 active keys), then creates or adopts the one sandbox
document Jarvis may write to and records read-only parts-library documents.
`tools/onshape.py`: cad_status / cad_find_part / cad_create_assembly /
cad_insert / cad_assembly / cad_move / cad_delete / cad_render (shaded PNG
via ToolResult.image_b64 — the model's eyes; text readback in inches is
ground truth). API facts verified against the docs, worth keeping:
cross-document inserts must reference a *version* of the source document,
so cad_find_part resolves each library's latest version and errors usefully
on never-versioned docs; insert-with-placement is a single call
(`transformedinstances`: 16-float row-major matrix, meters, absolute in
root-assembly coords); `shadedviews` requires a 12-float view matrix — it
*rejects* named views despite what the generated client docs say, so
cad_render carries a `_VIEWS` name→matrix table — and `pixelSize=0` means
zoom-to-fit; `occurrencetransforms` takes the flat
`{occurrences, transform, isRelative}` body (the `transformDefinitions`
wrapper belongs to `/modify` and 400s). The tools speak inches/degrees
(VEX lives on a 1/2" grid) and convert internally.

**Validated live 2026-07-31** on the owner's account: sandbox created
(public — free plan), and the 14 official VEX V5 library documents
auto-discovered and written into the bundle via the documents-search API
(`q='description:"Official VEX V5 Library"'`, `filter=4` = public, then
keep only owner == "Onshape" — a name search surfaces user copies first
and misses the real ones, which are matched by *description*). Full
pipeline exercised tool-by-tool: find "c-channel" across 14 libraries →
create assembly → insert two 35-hole aluminum c-channels → readback →
move → iso/top renders. The renders earned their keep immediately: a 12"
Y-offset overlapped the 17.5"-long rails (channels run lengthwise along Y
in their local frame), which the readback stated and the render made
obvious; one cad_move fixed it. Still to build: mates via mate
connectors, configurable cut-to-length (the "(Configurable)" library
docs), and whiteboard→CAD wiring. API-key note: keys live under My
account → Developer → API keys (the dev-portal URL is OAuth-apps only
now); individual accounts cap at 2 active keys.

**CAD improvement round 1 shipped (2026-08-05)** — items 1–3 of
`docs/cad-improvement-plan.md`; mates (item 4) deliberately wait until the
bench has measured whether 1–3 absorbed the failure.

- **cad-bench** (`jarvis/cadbench.py`, `jarvis bench --family cad`) — the
  fourth family. Five tasks (place / pair / frame / revise / repair) graded
  the agent-bench way: the assembly is read back from Onshape afterwards and
  scored on instance count, positions, rotations, and clearances computed
  from real part bounding boxes — never the prose. Categories: placement,
  clearance, revision, discipline. Each run builds its own
  `cadbench-<task>-<runid>` assembly in the pinned sandbox and deletes it in
  a `finally:` (a failed cleanup prints the assembly name loudly). The write
  pin is untouched. Costs OpenRouter *and* Onshape quota — the graders make
  API calls too. `pair` is the recorded c-channel regression (a stated gap
  that requires knowing the part is 17.5" long); `revise`/`repair` are the
  tasks predicted to discriminate until mates exist.
  `JARVIS_CADBENCH_TRACE=1` keeps each task's full call sequence and
  transcript under /tmp. Cleanup is verified live: deleting the assembly
  cascades to the BOM element Onshape auto-creates beside it, so a bench
  sweep leaves the sandbox exactly as it found it.

  **Baseline (Luna, 2026-08-05/06):** place ✓ · pair ✓ · frame ✓ ·
  revise ✓ · repair ✗ (2/11, hit the 18-step cap) — 83% overall, $0.0076,
  130s. **The readback upgrade absorbed most of the predicted failure**:
  `pair` — the recorded 12-inch c-channel regression — passed because the
  model computed the gap from the new extents. The `repair` failure did
  not reproduce: an immediate re-run passed 11/11 in 10.7s with the
  textbook sequence (status → readback → one absolute cad_move restating
  position with zeroed rotation → readback → render). Same lesson as
  agent-bench: a single run is a sample, not a verdict. Consequence for
  item 4 (mates): the ladder as it stands no longer demands them — build
  them when a real task does, or when the bench gains a task where a
  moved rail must carry attached parts (which is what mates actually
  solve).
- **Readback fidelity** (`tools/onshape.py`): `cad_assembly` now reports
  actual rotation via `_angles()` — the exact inverse of `_rotation`, same
  fixed-frame X→Y→Z degrees `cad_insert`/`cad_move` accept, gimbal lock
  collapses into rx with rz=0 — plus each instance's **world-frame extents**
  from its source part's bounding box, so overlap is computable from the
  text channel. `cad_find_part` carries each match's local-frame extents
  (which axis is long, and how long) so offsets can be computed before
  placing. Boxes degrade silently to the old output when unavailable.
- **`skills/cad.md`** — the CAD discipline as instructions (absolute
  placement / no solver, local-frame axes, insert→readback→render, the
  half-inch grid, incremental verification, read-only libraries).
- **API facts verified live 2026-08-05** (probes under `tests/probe_*.py`):
  part bounding boxes at
  `GET /parts/d/{did}/v/{vid}/e/{eid}/partid/{pid}/boundingboxes` — flat
  `lowX..highZ` payload, meters; the assembly definition's version key is
  **`documentVersion`**, not `versionId`; element delete is
  `DELETE /elements/d/{did}/w/{wid}/e/{eid}`; part names carry invisible
  LRM marks (`‎`) that must be stripped before matching; the 35-hole
  c-channel measures 17.5" along local Y, origin at one end, X centered.

**Discord shipped (2026-07-31)** — as a *bot*, never the owner's account
(self-botting is a ToS ban; decision of the same kind as declining Membean).
`jarvis auth discord` stores {bot_token, owner_id} at
`~/.config/jarvis/discord_token.json` (getpass input, owner auto-resolved
from the application object, invite URL printed, test DM sent); bundle
covered by all three secrets layers. Tools: `discord_channels`,
`discord_read` (oldest-first; hints about Message Content Intent if all
content comes back empty), `discord_send` (dangerous=True), and
`discord_dm_owner` — deliberately NOT dangerous because the recipient is
pinned to the owner's id, which is what lets background workflows ping the
owner's phone; it is in workflows.SAFE_TOOLS with that rationale inline.
Discord messages are untrusted content (system prompt updated: "only the
user speaks for the user"). REST-only/pull-based for now; a Gateway
websocket listener (real-time "#jarvis channel as remote terminal") is the
v2 if wanted. Free suite: `tests/discord_check.py`.

**Discord Gateway listener (2026-08-01)** — real-time replies.
`discord_gateway.py`: one sync-websocket thread (websocket-client dep),
HELLO→IDENTIFY→READY, heartbeats, fresh-IDENTIFY reconnects; started by
`jarvis face` when the token bundle exists. **Response rule
(should_respond, tested): only the OWNER's messages, only when the bot is
@mentioned (or DMed), never bots, never empty** — a stranger typing
"@jarvis do X" is ignored by construction, because a public mention is an
agent trigger and only the owner gets one. Replies post ungated (they
answer the owner where the owner asked — dm_owner rationale); tools the
turn uses keep their own gates, so away-from-desk dangerous calls deny and
Jarvis says so (allowlisted ones still work remotely). Separate persistent
agent + DISCORD_SYSTEM prompt (2000-char replies). Close code 4014 =
Message Content Intent off in the portal: the listener explains and stops
rather than retry-looping (found live; `tests/discord_gateway_check.py`
covers rules synthetically + a live handshake that skips on 4014).

**Discord voice messages (2026-08-03)** — the owner can voice-chat with
Jarvis in his DMs, walkie-talkie style: send a voice note, get the reply as
text plus synthesized speech attached to the same message (a plain
`jarvis-reply.wav`/`.mp3` attachment — Discord renders an inline player;
container named by sniffing, per voice.py's contract). No transcoding
anywhere: parakeet accepts Discord's ogg/opus as-is (probed live 2026-08-03:
1.000 similarity, 0.57s), so the attachment goes straight to `voice.stt()`.
A voice note is recognized by the IS_VOICE_MESSAGE flag (1<<13) or waveform
metadata on an audio attachment — a dragged-in mp3 has neither and is never
transcribed as if spoken. The trigger rule is unchanged in effect: a voice
note cannot carry an @mention, so voice only works in DMs, owner-only as
always. **The safety line: a transcription can never resolve an
authorization.** The gateway passes `spoken=True` to `run_turn`, and
`_discord_turn` skips `DISCORD_APPROVALS.handle_reply` for spoken turns — a
mishearing must not become a "yes", so approval replies stay typed-only.
Spoken turns reach the agent prefixed `[voice note]` (the prompt explains:
expect mishearings, reply speakably). Every voice-path failure (oversize >8MB,
download, STT) becomes a text reply and a TTS failure degrades to text-only —
never a silent drop. All of it covered in `tests/discord_gateway_check.py`
(voice rules, the stubbed pipeline, failure degradation, and the
approval-isolation check against the real face server); the multipart
attachment upload was validated live against the owner's real DM.

**`jarvis daemon` shipped (2026-08-03) — away-agent phase 1** (the plan:
`docs/away-agent-roadmap.md`; the Discord core moved to `discord_agent.py`
so face and daemon share one implementation of the spoken-turns-never-
authorize rule). The daemon is the always-on half of the face with the
window cut away: Discord gateway + an ApprovalBroker whose `viewers` is
pinned to 0, so every dangerous-tool question DMs the owner or denies
`nowhere-to-ask`. No HUD, no workshop, no browser window, no TTS pre-warm
(voice notes load Kokoro lazily). **Exactly one process owns the gateway:**
the daemon refuses to start while a face is serving (double-IDENTIFY would
double every reply), and a face started while the daemon runs skips the
gateway AND calls `APPROVALS.detach_remote()` — DM answers land in the
*daemon's* broker, so a face-made DM could only ever time out; its
questions stay on the card, correct for the at-the-desk surface. The
read-only `/status` endpoint on `DAEMON_PORT` (8405) is both the
single-instance lock and how the face detects the daemon. Provisioning is
human-only: `jarvis daemon install` prints the systemd user unit
(`WorkingDirectory=%h` — tool-relative paths must not resolve against `/`),
the `loginctl enable-linger` step, and the WSL keepalive options (Task
Scheduler `wsl --exec sleep infinity`, or `.wslconfig` `vmIdleTimeout=-1`);
it writes nothing. `tests/daemon_check.py` is the free suite (responder
approval isolation, nowhere-to-ask/timeout/detach denial paths, health
lock + single instance, refusing to start over a face, shutdown releasing
blocked waiters). Still pending from phase 1's exit gate: the 72-hour soak
under systemd with real sleep/wake cycles.

**Background goals shipped (2026-08-03) — away-agent phase 2.** A goal is a
loop of turns, not a turn: `goals.py` is the durable store
(`~/.local/share/jarvis/goals/<id>/` — `goal.json` plus an append-only
`journal.jsonl` written by the *runner*, never the model, so "what did you
do while I was gone" is answered from disk), and `goalrunner.py` is one
serial worker thread — one goal at a time on purpose (singleton browser,
predictable spend) — running slices of `run_turn` on the goal's own
session, which is what makes daemon-restart resume free. Between slices it
checks shutdown, cancel, then ceilings (dollars / hours / slices, defaults
in config, per-goal overrides on `jarvis goal`); any ceiling parks the goal
with a DM saying what was spent. **Ending is a tool call**: the goal agent
carries `goal_report(done|blocked)` (`tools/goalctl.py`, armed only inside
a slice — the step-budget lesson: state outside the transcript gets
confabulated, so completion must be an act *in* it). **Steering is barge-in
for goals**: a `steer:` DM queues text, sets the interrupt Event behind the
agent's `should_stop` (turn ends whole at the next step boundary, invariant
3), and the text arrives as the next slice's `[owner steering]` message.
Progress DMs: start, terminal states, and an interval digest
(`GOAL_UPDATE_MINUTES`, default 10) with slices/spend/elapsed/last
activity/the live plan slot — assembled from the runner's records, never
asked of the model. DM verbs (`goal: …`, `steer:`/`redirect:`,
`goal status`, `goal cancel`) are parsed in the daemon's `_route` ahead of
the conversation agent, **typed messages only** — a misheard voice note
must not steer or cancel, same isolation as approvals. Intake: `goal:` DM
or `jarvis goal "…" [--dollars --hours --slices]`; `jarvis goals` lists.
The goal toolset is the full registry minus desktop (foreground-stealing is
a desk feature) and window controls; the approver is the daemon broker's
remote gate, so dangerous calls DM the owner exactly as at the desk, and a
deny is handled by the model (park-and-continue and per-goal scopes are
phase 3). Free suite: `tests/goals_check.py`; `daemon_check` now pins
GOALS_DIR to a temp dir for the daemon's whole lifetime — a test daemon
must never pick up real queued goals with a stubbed approval channel.

**Goal protocol upgrades from the first live runs (2026-08-05), all
committed same-day:** slice 0 is **planning only** (plan DMed to the owner
before implementation; steer within a step); the first `goal_report(done)`
buys a **verification slice**, not the exit — and a verify slice that keeps
working instead of confirming **re-arms the gate**, because the first live
run reported done after phase 1 of 7 and the real completion must not sail
through on the spent pass. `goal_report` is documented as ENTIRE-goal-only.
`goal resume` requeues the newest parked goal, and `steer:` with nothing
running targets the newest parked/queued goal — parked requeues immediately
with the steering as its first resumed message (one DM unblocks and aims).
Live validation on a real 7-phase VEX strategy goal (11 slices, $0.42,
~35min, daemon killed and restarted mid-goal with clean session resume —
phase 2's exit gate): the verify pass caught real defects both times it ran
(a missed 8-point scoring rule; stale docs + malformed coordinates), the
model used plan_write throughout (PENDING #2 answered for goal runs),
delegated verification reads to run_subagent, and honestly blocked on data
the game db doesn't contain rather than inventing it. Lesson worth keeping:
**write acceptance criteria into the goal statement** — the first VEX run
produced a complete-looking skeleton in one slice because nothing pinned
what "done" meant; the surgical follow-up produced correct code.

**vercel-deploy skill (2026-08-01).** Build → verify locally in his own
browser → private GitHub repo (`gh repo create --private --source --push`)
→ **stop and ask the owner for the Vercel project name** (it decides
`<name>.vercel.app`) → preview deploy → owner approves the preview → only
then `--prod`, on a fresh yes. Exact commands are written into the skill
(including `--cwd`/`-C` flags instead of `cd`, and the nohup/pkill pattern
for the throwaway local server). Everything routes through run_command's
approval gate; repos are private unless the owner explicitly says public.

**Self-improvement, governed (2026-08-01).** `skills/self-improve.md` lets
Jarvis edit his own codebase when the owner asks: read CLAUDE.md first, git
checkpoint before/after, run the relevant free suites, never report success
past a red test, one change per request (no autonomous loops). The boundary
that makes it safe is mechanical, not prose: **write_file refuses
SELF_PROTECTED** (`tools/files.py`: secrets.py, files.py itself,
tools/__init__.py, approvals.py, permissions.py) — the layers that gate him
change only by the owner's hand or per-approved run_command.
`tests/self_improve_check.py` guards the guard.

**Desktop control shipped (2026-07-31)** — Jarvis can drive Windows apps.
`jarvis desktop setup` (human-only) builds a Windows-Python venv at
`C:\Users\johnw\.jarvis-bridge` and writes `run-bridge.cmd`; the owner
starts that, and `windows/bridge.py` **dials into** WSL on port 8404. It
dials out rather than listening because WSL2 forwards `localhost` from
Windows inward, so that direction needs no firewall exception and no address
discovery — the WSL gateway IP changes every boot. Tools mirror the browser's
discipline exactly: `desktop_open` / `desktop_snapshot` give a ref-tagged
accessibility tree, `desktop_click` / `desktop_type` / `desktop_key` act by
ref, `desktop_screenshot` is the vision channel. Confinement is the app
allowlist (see *Safety design*). Validated live on both registered apps:
Settings navigated by ref with readback, Claude Desktop read in full, and
`jarvis ask` completing a real question end-to-end (7 steps, $0.0010).

Five findings from getting there, each of which cost a debugging round:

- **Chromium/Electron publishes its tree over MSAA, not UIA.** Over UIA a
  Claude Desktop window bottoms out at an empty `DocumentControl` next to a
  "Chrome Legacy Window" stub; the same window over MSAA/IAccessible yields
  the entire UI. It *also* needs `--force-renderer-accessibility` at launch
  or the renderer tree stays off whichever API you ask with. Both halves are
  required. Claude is an MSIX/Store package, so the flag has to go through
  `IApplicationActivationManager::ActivateApplication` — the exe under
  WindowsApps is ACL'd and `explorer.exe shell:AppsFolder\…` silently drops
  arguments.
- **Chromium invalidates that MSAA root as it rebuilds its tree**, and the
  dead pointer does not raise — it reports zero children forever. Anything
  that polls has to re-fetch the root each time; `take_snapshot` also retries
  once on an empty result, because the failure mode is a *short* snapshot,
  not an error.
- **UWP suspends when it loses the foreground** and its tree collapses to
  nothing, so every read activates the window first. `SetForegroundWindow`
  alone is a silent no-op from a background process — it needs the
  `AttachThreadInput` dance — and believing it worked produced a screenshot
  of Settings that was actually a picture of Claude Desktop with Settings'
  ref badges drawn on it. Screenshots, synthetic keys, and coordinate clicks
  now hard-require a verified foreground; pattern-based reads and clicks do
  not need one.
- **"Has children" is not readiness.** A suspended UWP window still reports
  its frame children, so the first readiness check passed instantly and
  captured six lines of window chrome. Readiness now means a tree that
  clears a floor *and* stops changing. Relatedly, Settings is responsive and
  at a small width **removes** the nav list rather than reflowing it, so the
  window is maximized for a predictable tree — the desktop equivalent of
  pinning a browser viewport. And a UWP app is two windows: the
  ApplicationFrameWindow owns position and z-order while a
  `Windows.UI.Core.CoreWindow` owns the content, and Windows moves the
  second between nested and top-level *while the app runs* — so the window
  we activate and the window we read are resolved separately.
- **Snapshot wording changes answers.** Windows 11 switches are Buttons
  carrying TogglePattern, so reading toggle state only from checkboxes left
  every switch stateless and Jarvis answered "Bluetooth: off" about a radio
  that was on. Exposing it as `button "Bluetooth" checked=true` was still
  misread — "checked" on a button reads as "pressed". Rendering it as
  `switch "Bluetooth" ON` fixed the answer with no prompt change. Selection
  is now a separate word from checkedness, and only printed when true.

**Remote approval over Discord DM (2026-08-01).** A Discord-triggered turn
used to be read-only in practice: every dangerous tool denied for want of a
human at the HUD. Now the same one-shot request can be put to the owner in
their DMs — `discord_approvals.DiscordApprovals` is a *remote channel* the
broker holds (`ask(item) -> delivered?` / `close(id, resolution)`), so
`face/approvals.py` still knows nothing about Discord. The DM echoes the tool
and its full arguments plus a 4-character code; the owner replies **yes** /
**no** / **always** (always writes the persistent allowlist entry, same as the
card's ALWAYS button).

The rules, in the order they matter:

- **Only the owner is ever heard**: `should_respond()` drops everything else
  before this code runs. On top of that an answer counts only in the *same DM
  channel the question was asked in* — a "yes" typed in a server channel, even
  by the owner, authorizes nothing.
- **One-shot, per-request**: the code maps to a broker id that resolves
  exactly once. With two asks open, a bare "yes" is refused rather than
  guessed; an answered code is dead.
- **Anything that is not a clear answer denies**: the parser takes one word
  (plus an optional code), so "no wait actually yes" resolves nothing and gets
  asked again. Timeout is 10 minutes (vs the HUD's 120s — you have to get your
  phone out), and Discord being unreachable falls through to the old denial,
  now called `nowhere-to-ask`.
- **Who gets asked**: the Discord agent's approver is
  `APPROVALS.approver(remote=True)` — its owner is on a phone by definition —
  so its questions always DM, *and* still raise a card if a window is open
  (either surface can answer; first one wins). Face turns only DM if no window
  is connected, so sitting at the desk generates no DM traffic. Workflows are
  unchanged: still a deny-all approver, because nobody is watching them at
  all.
- The HUD card now carries the real deadline and says ALSO ASKED ON DISCORD,
  and a window closing no longer denies a question that went out remotely
  (`deny_all(..., include_remote=False)`) — the owner not being at the HUD is
  the whole premise.

**The trade, stated plainly:** approval used to require physical access to
this machine, and now it also accepts whoever holds the owner's Discord
account. That is the owner's deliberate call (asked for 2026-08-01), and it is
why the DM echoes the entire command rather than just naming the tool. Undoing
it is one constructor argument: drop `remote=` from the `ApprovalBroker` in
`face/server.py`.

**Session memory (2026-07-31).** Conversations now survive a restart, and
Jarvis can look into the ones before this one. `sessions.py` keeps three
things per session under `~/.local/share/jarvis/sessions/<id>/` — outside the
repo, unlike memory/ and skills/, because transcripts are bulk, personal, and
rewritten every turn:

- `messages.json` — the live transcript *as the context manager left it*, so
  resuming inherits the pruned history rather than re-inflating it. Live
  image payloads are swapped for the eviction placeholder on the way to disk
  (a screenshot is 1.5MB of base64 no future turn will look at), and
  `messages[0]` is never persisted — the system message is rebuilt on load, so
  a resumed conversation gets today's prompt and today's indexes.
- `log.jsonl` — append-only user/reply text. This is the durable record:
  compaction *deletes* from the transcript, and the log still has it. It is
  what `session_search` greps and what a summary is built from.
- `meta.json` — title, timestamps, turns, cost, and the cached summary.

`Agent(session=…)` is the whole integration: it restores on construction and
calls `session.record()` after every `run_turn`, so all three surfaces persist
for free (a save that fails is caught — persistence must never break a turn).
A **cancelled turn is saved too**: the transcript really does end at that user
message, which is what makes an interrupted conversation immediately
reusable.

Cross-session recall follows the skills two-tier design: recent session
**titles** are rebuilt into `messages[0]` every turn (only for agents armed
with `session_summary` — same rule as skills), and the content of one loads
only when Jarvis calls a tool. `session_summary` is the "inject that
conversation" path — its result lands in the transcript as a tool message,
which is what injection *is* in a chat loop; the summary is cheap-tier,
generated on demand and cached until the session gains a turn.
`session_search` greps every log, `session_read` returns exact wording. All
four are read-only, none is dangerous, and all four are in
`workflows.SAFE_TOOLS`.

Decisions worth not relitigating:

- **New session by default, resume explicitly** (`jarvis chat -c` / `-r <id>`,
  `jarvis face -c`, or the SESSION row in the HUD). Silently resuming is how
  you end up talking into a transcript you have forgotten. Discord is the one
  exception — it continues its own last session, because it is the
  away-from-desk channel with no UI out there to pick one.
- **Jarvis can read sessions but not switch them.** Which conversation is live
  is the owner's choice; a tool that swapped the transcript mid-turn would
  also have to decide where its own tool result belongs.
- **A session with nothing said in it never touches disk** (`sessions.new()`
  builds the object; `record()` creates the files). Every chat invocation and
  every window launch mints one, and empty directories would flood the index
  and the picker with conversations that never happened.
- **The titler and summarizer wrap their input in delimiters and label it
  data.** A conversation is full of imperatives, and the first version of the
  title prompt produced the title "Acknowledged" — the cheap tier answered the
  message instead of describing it. Related: gpt-oss-20b is a reasoning model
  and returns *empty content* if the token budget is spent thinking, so a
  6-word title needs `max_tokens=200`, not 24.

Verified live: two CLI turns across a restart (`-c` recalled a codeword from
the saved transcript), and a third, fresh session that answered "what codeword
did I give you earlier" by calling `session_search` off the injected index and
citing both session ids.

**Long-horizon work, part 1 (2026-08-01).** Three changes aimed at the same
failure — a run that goes long enough to forget what it was doing.

- **The cut-point orphan bug** (invariant 1) — found by inspection, reproduced,
  fixed, and now covered by the `context.py` test suite that this file had been
  claiming existed. Worth internalizing: it lived in a code path that *only*
  executes past 60k tokens, so every short test in the repo ran straight past
  it. Long-horizon code needs long-horizon tests.
- **The working plan** (`tools/plan.py`). One tool, `plan_write`, holding a
  markdown checklist rewritten whole each time. It renders into `messages[0]`,
  which nothing in `context.py` touches, so it is the one part of a long run
  that cannot be pruned away — and it is re-rendered every *step*, not every
  turn. The skills index proved the mechanism; this reuses it. Wired into the
  main agent (all tools), workflows, and the designer.
- **Sub-agents** (`tools/subagent.py`). `delegate()` was the cost lever; this
  is the *context* lever, which is a different problem. Every tool result a run
  produces lives in the orchestrator's transcript forever — fetch four pages to
  answer one question and 40k tokens of page dump outlive the answer by the
  whole session. `run_subagent` does the job on its own transcript and returns
  only its final text. Safety in "Safety design"; it is synchronous, which is
  what lets it hold the browser when a background workflow cannot.

Not done, in the order I would do them next (the full list came out of a review
on 2026-08-01; **spill-don't-drop truncation and real `prompt_tokens` were done
2026-08-09** — see *Harness round 2* below): step-budget awareness and a
resumable handoff instead of `"[stopped after N steps]"` throwing the work away;
structured compaction sections (goal / done / open / failed) instead of free
prose on the cheap tier; repetition detection (the gpt-oss-20b vocab-bench
failure — 16 rounds of no valid action — is invisible to the loop today);
durable workflow journals; and **`long-bench`**, without which none of this is
measurable — vocab-bench at ~33 steps never crosses the compaction threshold.

**Harness round 2 (2026-08-09) — the gaps a Claude Code comparison exposed.**
Six defects, all found by reading Jarvis's loop against a harness built for the
same job, all fixed together. All 42 free suites pass. (`tests/browser/
audio_check.py` matches the `*_check.py` glob but is **not** one — it opens the
HUD window and serves until Ctrl-C, so it hangs a sweep by design.)

- **There was no edit primitive.** `write_file` took the whole file, so changing
  three lines in a long one cost the model the entire file — and models drop
  content when re-emitting at length, which the read-before-write stamp cannot
  catch (it detects *staleness*, never *truncation*). `edit_file` does anchored
  replacement: exact match, unique unless `replace_all`, refuses ambiguity
  rather than guessing. **It honours SELF_PROTECTED, and that is the part to
  never lose** — a one-line replacement in `permissions.py` disarms the gate as
  thoroughly as overwriting it and looks like far less, so any future write
  tool has to be added to `self_improve_check.py` too.
- **`read_file` was capped at 40k characters with no way to ask for the rest.**
  CLAUDE.md is 116KB, so `skills/self-improve.md`'s first instruction ("read
  CLAUDE.md first") had been reading 34% of the file and reporting no problem.
  Reads are paged now (`offset`/`limit`, footer names where to resume) and
  line-numbered. The numbering has a matching hazard in each direction, and
  both are handled: `edit_file` retries once with the prefixes stripped when a
  quoted excerpt fails to match, and `write_file` strips them when a whole-file
  rewrite carries them back in — the strip only fires on read_file's own
  6-wide right-aligned field, so a TSV counting 1, 2, 3 survives.
- **Search was unbounded.** `run_readonly` allows grep but forbids pipes, so
  there was no `| head`: the model took the whole dump and it lived in the
  transcript forever. `grep_files` bounds its own output (`mode` =
  content/files/count, `max_results`, glob, context lines), ripgrep with a
  pure-Python fallback that the suite exercises by forcing it. It lives in
  `tools/search.py` rather than `files.py` on purpose — SELF_PROTECTED exists
  to freeze the *write* guard, and freezing a read-only search tool beside it
  buys nothing.
- **Tool calls ran one at a time.** Invariant 3 exists to keep models emitting
  parallel calls, and the loop then executed them serially — so the only thing
  parallelism bought was fewer round trips. Now consecutive parallel-safe calls
  run together (measured: 3×0.3s tools in 0.30s). See invariants 3 and 8 for
  the two things that make it safe: call-order results, and one fresh
  `copy_context()` per worker.
- **`finish_reason` was captured and never read.** `llm.Reply` has carried it
  since the beginning; nothing in the codebase looked at it, so a reply the
  provider cut off at `max_tokens` was appended and returned as the finished
  answer — a sentence stopping mid-word, indistinguishable from a completed
  one. This is the **same bug as the invisible step budget**, and the third
  time that shape has appeared: *a state the harness can produce that the model
  cannot see is a state the model will confabulate around.* The loop now flags
  it (`Turn.truncated`), continues up to `MAX_CONTINUATIONS` times joining the
  halves, and says `[cut off at the token limit]` if it still cannot finish. A
  cut that landed mid-tool-call gets a note **after** the results, never
  before — invariant 3 again. `max_tokens` also went 4096 → 8192
  (`config.MAX_TOKENS`): 4096 is ~16k characters, which any substantial
  whole-file write exceeded.
- **Compaction was judged on chars÷4.** The estimate counts no tool schemas at
  all, so it reads low by thousands on a full toolset and the threshold did not
  mean what it said. `context.TokenMeter` keeps the provider's real
  `prompt_tokens` for the measured prefix and estimates only what was appended
  since — measured immediately after `llm.chat` returns, when `self.messages`
  is still exactly the request. Every measurement is provisional, so
  `discount()` covers in-place rewrites (eviction, truncation) and
  `invalidate()` covers compaction, which deletes messages the measurement
  counted and has no honest adjustment.
- **Truncation was the one context operation with no way back.** Eviction
  leaves a placeholder, compaction leaves a summary; a cut result left nothing,
  so a page fetched at step 4 was gone by step 20. It now spills the full body
  to `config.SPILL_DIR` (content-hashed, so identical results share a file and
  re-truncating is idempotent) and leaves a `read_file`-able pointer. The
  pointer goes *before* the `TRUNCATED` marker, because that suffix is how the
  next pass recognises its own work. A spill that cannot be written degrades to
  a plain cut — it is a bonus, never a reason for a turn to fail.

Also: `run_subagent` was pinned to the orchestrator tier **by omission** (it
constructed its child with no `model`), now `config.TIERS["subagent"]`,
defaulting to the same model so behaviour is unchanged but movable.

Deliberately **not** done in this round, because they are decisions rather than
defects and are the owner's to make:

- **Hooks and permission rule matchers.** Jarvis gates on a binary `dangerous`
  flag plus an allowlist matched on a command's first word; Claude Code matches
  rules like `Bash(npm run test:*)` and runs user programs at PreToolUse. That
  is a new subsystem, and it lands in `permissions.py`, which is
  SELF_PROTECTED precisely so it does not change casually.
- **A typed sub-agent fleet.** One synchronous shape versus agent types with
  their own prompts, tools and models running in the background. The blocker is
  known and written down: `SESSION` is a module-level singleton, so concurrent
  children would interleave on one Playwright page and share one 120-action
  budget.
- **Moving the durable slot off `messages[0]`.** Invariant 7 puts volatile
  state (plan, skills index, session titles) at position 0 because nothing in
  `context.py` can reach it. That is prune-proof and **cache-hostile**: a
  change at position 0 invalidates the whole prefix cache, so every
  `plan_write` makes the next step re-pay for the entire transcript. Claude
  Code's `<system-reminder>` blocks ride the *latest* user message for exactly
  this reason — worse for durability, better for caching. Both are coherent;
  picking between them is an architecture call, not a bug fix.

### PENDING LIVE VALIDATION — needs API keys (delete this section once done)

Everything above was built and tested in a sandbox with **no `OPENROUTER_API_KEY`**,
so every check is synthetic. The free suites all pass (`context_check`,
`longhorizon_check`, plus `secrets`, `permissions`, `skills`, `workflows`,
`gmail`, `onshape`, `face/approval_check`, `face/controls_check`). What a
human with keys still has to confirm — **and this list should be deleted from
CLAUDE.md once it has been, with the results folded into the notes above:**

1. **Playwright suites never ran here** — not installed in the sandbox. Run
   `tests/browser/policy_check.py`, `tests/browser/thread_check.py`, and the
   `tests/face/` HUD suites (`hud_state_check`, `hud_approval_check`,
   `whiteboard_check`, `attach_check`, `hud_input_check`). They are free; they
   just need the browser. Nothing in this change touches them, so a failure
   means a genuine regression.
2. **Does the model actually use `plan_write`?** The mechanism is tested; the
   *prompting* is not. Give Luna a genuinely long task (a multi-file refactor,
   a 10-part research job) via `jarvis chat` and watch whether it writes a plan
   unprompted and keeps it current, or ignores the tool. If it ignores it, the
   system-prompt paragraph in `config.py` is what needs work, not the tool.
3. **Does `run_subagent` earn its cost?** Same task twice, once with the tool
   available and once without, comparing total `$` and whether the answer holds
   up. The bet is that isolation pays for the extra model call; that bet is
   unverified. Watch for the failure mode where the model delegates something
   it should have done itself and pays twice for a worse answer.
4. **A real run past the compaction threshold.** Nothing has yet exercised
   compaction against a live model — the orphan fix is proven synthetically
   only. A long browser or CAD session (parallel `cad_render` calls are the
   exact shape that triggered the bug) crossing 60k tokens would confirm no
   400s and that the pinned goal reads sensibly after a summary lands.
4b. **Does the model actually use `edit_file` and `grep_files`?** (Added
   2026-08-09.) Both are mechanically tested; the *prompting* is not. Watch a
   real self-improve or CAD session for three things: whether it reaches for
   `edit_file` instead of rewriting whole files, whether it pages past the
   first `read_file` footer on CLAUDE.md rather than stopping there, and
   whether it starts a search with `mode='files'`. If it ignores them, the
   system-prompt paragraph in `config.py` needs work, not the tools. Also
   worth measuring on agent-bench: `project` is the task these should move,
   and the baseline to beat is Luna's 100% at $0.0065 / 115s — the interesting
   number is **cost**, not score.
5. **Stale plans across turns.** The plan persists for the life of the agent,
   which is the point for long work but means a finished checklist can linger
   in the face's persistent agent into an unrelated conversation. The model can
   clear it with `plan_write("")`. Check in live use whether it does, or
   whether the plan needs to expire.

Later: real integrations (calendar/email), scheduled proactive runs, and
more registered desktop apps as they earn their place (each is one entry in
`config.DESKTOP_APPS`, plus a backend choice — `uia` for native/UWP, `msaa`
for anything Chromium-based).
