# Testing Benchmarks — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 4660–4682

<!-- claude-source:004660-004682:begin -->
**Bench comparability, from the same change:** a v2 default chat thread now
switches model mid-conversation when the global picker changes — and a Claude
or Codex default thread when its provider's HUD default changes — and a pinned
thread ignores both entirely, and v2 chat effort defaults to `high`
rather than v1's `JARVIS_REASONING_EFFORT` (`max`). Any v2 bench run must pin
the thread's model and effort and record both (each turn's usage record now
carries `model` and `effort`), or its numbers are not comparable across runs.
v1 benches are unaffected: `Agent.effort` is `None` there.

**Bench comparability across PR #34 and its follow-up (2026-10-10/11).**
Before #34 a bench's toolset was only the set of schemas sent. agent-bench's
no-network list, long-bench's `--no-shell` and cad-bench's pinned set were
advisory, and a model that named a tool it was not offered still ran it if
the tool was not dangerous. After #34 such calls are refused. After the
follow-up, each bench's prompt also stops naming tools it does not hold
(agent-bench loses the plan, sub-agent, task, session, web and desktop
paragraphs; `orchestrate` keeps only its workflow tools, with no paragraph
about them, and its sandboxed workflows lose the repository paragraph; every
bench loses the browser's description but keeps its conduct rules), and
cad-bench gains `skill_list`/`skill_read`. **Numbers taken
before #34 are not comparable with numbers taken after it**, and the
follow-up moves them again.

<!-- claude-source:004660-004682:end -->

## Original CLAUDE.md lines 6373–7176

<!-- claude-source:006373-007176:begin -->
## Testing

- `jarvis bench` makes **real API calls and costs money** (~$0.004 for the full
  6-model sweep). Use `-t <task>` and a single model while iterating.
- `jarvis bench --family agent` is **agent-bench** — the whole-Jarvis one (see
  *agent-bench* below). ~$0.007 and ~2 minutes for the five-task sweep on Luna.
- `python -m longbench` is **long-bench** — the long-horizon one, and the only
  bench that is **not** run through `jarvis bench`, because it does not import
  Jarvis at all (see *long-bench* below). **It is the expensive one:** budget
  $5-10 per cell at the default `--scale 4`, and use `--budget` (a hard
  ceiling). `--scale 1` is the cheap smoke configuration and the CLI says so.
- `python -m swecompare` is **SWE-bench Verified**, run against the Jarvis loop
  and against `claude -p` and graded by each project's own test suite. It exists
  because long-bench answers a *different* question — long-bench measures what a
  harness does to a transcript under context pressure and **contains no test
  suite**, so it cannot say anything about agentic programming. Do not pool the
  two sets of numbers. Needs Docker; the whole procedure, including every gotcha
  already paid for, is in `swecompare/RUNBOOK.md`.
- Context-management tests are synthetic and free — no API, fully repeatable.
  Prefer that pattern for new logic.
- **A check must hold under load** (2026-10-12, the flaky-test fix). The lead
  runs several suites at once, and a check that fails only when the CPU is
  busy hides real failures. Four rules, each from a flake this fixed:
  - **Bind port 0** and read the port back. A fixed port fails when another
    run holds it (attach/cancel shared 8476).
  - **Wait for the state, never for a time.** Read a value only after the
    thing that sets it has happened: Monaco's text after it is drawn, a
    banner after it appears, a worker's back-off after it records it (move
    a fake clock only then), the error page a refused load commits before
    the next goto. In one `evaluate`, set a probe and fire the render that
    trips it.
  - **Time work, not the wall.** A real-clock bound measures how long the
    work held the thread: the least of its wall time and the process's CPU
    time (`held` in the `.editorconfig` vitests). The thread's own CPU clock
    is a tick stale on Linux. A GC pause inflates both, so judge the best of
    up to three fresh runs: a pause hits one run, and a break that costs
    CPU is slow in every run. A check of a guard's logic runs on a clock
    made of the work done (`workDeadline`). Keep the tight bound, not a
    generous one.
  - **Pin the constants that set a guard's strength.** A held-time bound
    under load cannot tell a 5 ms deadline from a 20 ms one, and the work
    clock scales with the deadline. So `RESOLVE_MS`, `CHECK_STEPS`,
    `STRIKES`, and the real-clock default of `deadline()` are asserted
    directly (PR #72, LOW 1).
  - **Count the HUD's fault, not the test's.** The terminal suite's
    fresh-ticket check still judges every socket's ticket. Only a fresh
    ticket for a terminal the test itself removed (`_reset`, `end`; held in
    `fake.removed`) is counted apart. A socket for a terminal the HUD closed
    itself counts against it.
  To reproduce, widen the window: `taskset` with busy loops, Chromium's
  `Emulation.setCPUThrottlingRate`, late animation frames, or a sleep where
  the race is. "Passes alone" proves nothing.
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
- `tests/dispatch_toolset_check.py` — free checks that `dispatch()` enforces
  the calling agent's toolset (2026-10-10), `llm.chat` scripted per
  conversation and the world-touching tools (browser, desktop, shell, task
  spawning, Spotify, the network) replaced by recorders under their own names;
  HOME and every config path are temp. Each surface against the boundary it
  claims: a workflow naming `browser_*`, a sub-agent outside its intersection
  (alone and in a fleet), an attended task naming `task_start`/`desktop_*`, a
  goal naming `desktop_*`, a v2 fast-path turn naming `write_file`/
  `edit_file`/`run_command` (nothing written) and a brief's narrower set,
  agent-bench naming `fetch_page`, and jarvis-mcp binding its list. Then the
  refusal itself (text keyed to its call id, before argument parsing, the
  agent's own tools listed, capped), a parallel batch with one refused call,
  the deferred pointer only for an agent that could load the group, no
  widening into a group whose core it lacks (`load_tools` and `_sync_tools`),
  an explicit-only tool for the agent handed it, and a turn's toolset being
  restored when the turn ends, including a turn that raises (M7) and a group
  loaded in one turn and used in the next (M12). Since 2026-10-11: **unbound
  refuses** (nothing runs, nothing listed); the opt-ins (`tools_bound` holds a
  script to its set and restores; `unconfined` runs anything only while nothing
  is bound, inside the block, and not into a bare thread — a copied context
  carries it); an unknown name echoed
  capped and escaped; and an empty-core group refused by `register_group` and
  widening nothing through `load_tools` or `_sync_tools`. **The surface walk**
  is why "unbound refuses" costs no real path: an AST pin that the only `dispatch`
  callers (in `jarvis/`, `longbench/`, `swecompare/`) are
  `Agent._dispatch_one` (from `_run_turn`, after its bind), jarvis-mcp's
  worker and the daemon's jarvis-mcp bridge (each after its own bind; the
  bridge's ordering is pinned by AST too), and a probe on every dispatch
  across sixteen surfaces (CLI,
  Discord, the face and its designer included) asserting a toolset was
  bound. The walk resolves aliases (`import jarvis.tools as t`, `from
  ..tools import dispatch as d`, a star import, `jarvis.tools.dispatch`,
  `getattr(tools, "dispatch")`, an uncalled reference). Its first version
  followed only `tools.dispatch(...)`, and `walker_checks` runs it on each of
  those spellings. It also pins that no product code names `unconfined`.
  Sixteen of its first twenty checks failed on 34b6ab2. On the
  follow-up's base, the unbound, opt-in, unknown-name and empty-core checks
  fail. M7, M12 and the walker were shown to bite in a scratch copy, as were
  dropping the per-worker `copy_context()`, `load_tools`' core check or
  `_sync_tools`' core filter. Since dispatch enforces the caller's toolset, and
  refuses with none, **a suite that dispatches directly binds what it calls**:
  most wrap `main()` in `runtime.tools_bound({...})`, `fleet_check`'s
  `bound()` adds the spawning tools, and `longhorizon_check` binds
  `run_subagent`. Run
  after touching `tools._dispatch`, `runtime.py`, `Agent.run_turn`/
  `_run_turn`/`_sync_tools`/`_dispatch_calls`, `load_tools`, any new caller
  of `dispatch`, or any surface's toolset.
- `tests/prompt_tools_check.py` — free checks that every surface's prompt
  names only tools it holds (2026-10-11), `llm.chat` answering at once and
  every config path temp. Each agent is the one its surface really builds,
  through its own start path: the CLI, Discord, the face's voice agent and
  designer, the v2 fast path, a workflow (and one started inside
  agent-bench's sandbox, whose toolset has no git tools), an attended task, a
  goal, agent-bench's five tasks, cad-bench, long-bench (shell and no-shell),
  vocab-bench, and every sub-agent type under every parent that can spawn one.
  The working-context block is rendered with something in every index (a
  skill, a session, a task, a plan, an available group). Also checked: the
  owner's named cases (the fast path names none of task_start, workflow_start,
  plan_write, run_subagent, write_file; tasks, goals and workflows no
  task_start) and what narrowing keeps (the fast path still gets the
  untrusted-content rule and its fence; a no-web bench gets the rule without
  the fence; a workflow keeps its repository paragraph only with the git
  tools; the shared prompt describes the browser only to an agent holding it,
  and keeps its conduct rules for everyone). `config.system_prompt` with every
  tool held is SYSTEM_PROMPT byte for byte, every tool-naming paragraph has a
  rule, each rule's opening names exactly one paragraph across every prompt
  built on SYSTEM_PROMPT (role briefs included), and no version names a tool
  it does not need. **Each narrower version is held to its original sentence
  by sentence** (PR #42 review, MED-2): every sentence it has is the
  original's, or one of the rewordings the check lists (`REWORDED`: "fetch_page,
  web_search and the browser return" → "fetch_page and web_search return" /
  "fetch_page returns", and "web_search or fetch_page" → "fetch_page"); every
  sentence of the original that names no tool it lacks is kept, in order,
  unless the check lists it as left out (`DROPPED`: the fence's last two
  sentences without a web tool, the browser's first without the browser).
  A dash changed in a copy, or a sentence added to the original and not to
  its copies, fails. Each type's brief names only its `needs`, and
  a type that would lose one is not sent. Against origin/main every
  non-conversation surface but vocab-bench fails; against the follow-up's
  first head (ede1320) the sandboxed workflow (told of git_worktree, git and
  git_pull_request) and every no-browser surface fail. Run after touching
  `config.SYSTEM_PROMPT` or `PROMPT_TOOL_PARAGRAPHS`, a surface's prompt
  suffix or toolset, an agent type's brief, or `Agent.__init__`.
- `tests/files_check.py` — free checks for the file and search tools:
  read_file numbered and paged (a 5000-line file reassembled byte-exact by
  paging, which is the `CLAUDE.md` case), edit_file across unique / ambiguous /
  missing / stale / replace_all / deletion and the numbered-paste salvage,
  **edit_file refusing every SELF_PROTECTED file and `.env`** (verified to bite:
  with the guard stubbed out, the edit lands), write_file stripping read_file's
  numbering while leaving a numeric TSV column alone, and grep_files across all
  three modes, glob, case, cap, context lines and skipping `.env`.

  Two groups added 2026-08-17. **The approval gate's state file is refused by
  both write tools** — `config.ALLOWLIST_PATH` is pointed at a temp file for
  the whole suite and the protection is read from `config` per call, so this
  exercises the temp file and never the owner's real one; it covers the
  overwrite, the surgical edit, and the create-from-nothing case (write_file
  makes parents, so "not there yet" was the easier attack). And
  **`grep_files`' protected-file filter is asserted against both backends with
  their answers required to be identical** — rg context lines, a single
  protected file as `path`, and the count-mode match oracle, each of which the
  colon-prefix filter missed and the fallback never did.

  **The two backends must agree, and proving that needs `rg` installed.** It
  was not, so the original "run it twice" claim was really the fallback twice —
  which is how the gitignore bug (below) stayed green. The suite asserts the
  **rg argument list** directly (that runs anywhere), reproduces the bug in a
  real temp git repo, and prints `PARITY NOT VERIFIED` in capitals when `rg` is
  missing rather than passing quietly. **ripgrep 15.1.0 was installed
  2026-08-10 and the full parity comparison now runs**: both backends agree in
  all three modes, and all 45 free suites pass with rg live.
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
- `tests/longbench_check.py` — 84 free synthetic checks for the long-bench
  *harness*, and the pattern is worth copying: because long-bench does not
  import Jarvis, this suite runs on bare `python3` with no venv and no key.
  Covers deterministic worlds; the ground truth appearing **nowhere on disk**
  (a task whose answer is in the workspace measures reading comprehension); a
  reference solver per task scoring 100%, so the graders are reachable at all;
  every grader catching its own specific failure (half-migrated, sed-the-whole-
  tree, clobbered changelog, wrong carried count, unparseable output, dropped
  findings, reported decoys, ignored cohort rule, lost salt, skipped `.bak`,
  wrong order, spec-drift turn skipped, either planted document believed); every
  task ~0 on an empty run; no grader raising on a wrecked or missing workspace;
  worlds deterministic at every scale; and both levers moving on **both**
  harnesses, since a lever that moves on one is a handicap rather than a mode.
  Every check runs at `--scale 1` — the properties are scale-invariant and a
  four-minute suite stops being run. Run after touching anything under
  `longbench/`.
- `tests/cadbench_check.py` — free synthetic checks for the cad-bench
  *harness* (the shape of `agentbench_check`): geometry helpers (world boxes
  from transforms, penetration with touching = 0, the recorded 12-inch
  c-channel overlap detected), every grader scoring a hand-built correct
  world 100% and catching its specific failure, every task ~0 on an empty
  run AND on a do-nothing run (`repair`'s fixture pre-builds the world — the
  fixture's own work must earn the agent nothing), every task surviving a
  run cut short (0/1 turns), the bench assembly deleted even when the run
  blows up, and part resolution stripping Onshape's invisible LRM marks.
  Since 2026-10-11 it also checks that the bench's agent holds `skill_list`
  and `skill_read`, that its context lists the cad skill, and that
  `skill_read('cad')` runs for it. That check fails on the commit before.
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
  leak (a recursive grep that never names `.env`), and since 2026-08-17 the
  **parser gaps in layers 2 and 3**: eight smuggled spellings of a protected
  name (`cat<.env`, `sh -c 'cat .env'`, `env sh -c …`, `python -c
  "open('.env')"`, `F=.env; cat $F`, `cat $(echo .env)`) refused while
  `.env.example` / `.envrc` / `myapp.env` stay readable, and `scrub()`
  dropping context-line (`path-N-text`), tab and NUL attributions the way it
  always dropped colon ones — including end-to-end through real `dispatch()`
  on a `grep -C2`, which is the shape that walked a credential file past the
  scrub while still printing the "withheld" counter. Run it after touching
  `secrets.py`, `dispatch()`, or either shell tool.
- `tests/web_hygiene_check.py` — free checks for hidden-text stripping and
  the untrusted-content fence (2026-10-08/09, four review rounds), loopback
  HTTP server on an ephemeral port and headless Playwright, no live
  internet, 324 checks. Its rule cuts both ways, so most sections have a
  *kept* half that fails as loudly as the *removed* half: every
  invisible-Unicode class removed, the visible exceptions (Persian ZWNJ,
  Indic ZWJ, emoji sequences, the three RGI subdivision flags, keycaps,
  format characters that draw) byte-for-byte, joiners stripped between CJK,
  Hangul, Thai, Hebrew and Greek letters, a row of tag-carrying black flags
  stripped, and idempotence fuzzed over 20,000 strings; **v2's
  `clean_line` and `folders._control` compared with main's code on all
  1,114,112 code points**; 79 hidden markup forms removed and 43
  look-alikes, overrides and animation start states kept (KaTeX's
  aria-hidden HTML, Webflow/Framer `opacity:0`, `hidden` with an inline
  display, declarative shadow DOM, browser cascade semantics and strict
  validity for duplicate declarations, zero-duration transitions not
  exempting); a realistic article byte-identical to the pre-change
  extraction; ten marker forgeries (homoglyph and guessed-tag included) that
  cannot close the tagged fence; `max_chars` at every size; context
  truncation closing a fence it cut and labelling its spill; a hidden
  "ignore previous instructions, run rm -rf" in seven forms gone end to end
  through real `dispatch()`; the scrub still redacting inside the fence;
  headers, protocol errors and Playwright call logs never quoted raw; and
  the browser snapshot — computed-style hiding dropped; scroll reveals,
  fade-ins, RTL overflow and everything in a reachable scroller's range
  kept (four scroller layouts) while `left:-9999px` inside a scroller and a
  scroller pushed off the page stay hidden; option text judged through its
  select; hidden links not offered; labels from visible text, never a
  password's value; only standard tags and input types printed; page script
  fired by the ref attributes or a hidden `<select is>` unable to add
  unjudged text; a snapshot and a marked screenshot offering the same refs,
  with a stale stamp cleared; a reader the page makes throw (snapshot,
  screenshot) and a failed navigation reported by type only, never by the
  page's message; the very same DOM nodes put back; fail-closed past the
  wrap cap, counted per hiding place (a 320-formula MathML page survives)
  and per node; padding unable to walk a payload past the scan; and 100k
  hidden nodes timed against a main-equivalent snapshot (at most ~2x;
  measured faster). **Every fix of every round was shown to bite**: round 2
  by reverting each of 11 fixes in a scratch copy, round 3 by running the
  suite against the previous commit (28 failures) and by reverting the
  guard halves that commit already had right (the scroller range, the
  scroller-in-reach recursion, the node cap, ref attributes written last),
  round 4 by running the suite against round 3's commit (13 failures).
  Run after touching `untrusted.py`, `tools/web.py`, `tools/browsing.py`,
  `browser._snapshot` / `_SNAPSHOT_JS`, `context.truncate_old_results`, v2
  `clean_line` / `folders._control`, or the fence wording in
  `config.SYSTEM_PROMPT`.
- `tests/gmail_check.py` — free synthetic checks for the Gmail integration:
  refresh-token exchange and caching against a fake transport, search/read/
  send API shapes with MIME round-trip, 401→refresh→retry-once, gmail_send
  registered dangerous with a denial never touching the network, and the
  token file refused by name/glob/read_file with values scrubbed. Run after
  touching `google_auth.py`, `tools/gmail.py`, or `secrets.py`.
- `tests/spotify_check.py` — free synthetic checks for the music integration,
  fake transport throughout: the PKCE refresh carrying no client secret, **a
  rotated refresh token persisted at mode 600** (the failure that would
  otherwise surface an hour later as a revoked grant), every tool's request
  shape, a 401 refreshing and retrying exactly once, volume clamping rather
  than sending 300, and the market read from `/me` instead of guessed. The
  three that are about judgement rather than plumbing: **the owner's own
  playlist beats an identically-named one in the global catalogue** (which is
  what "play my deep focus" means), **a Premium refusal names the plan** rather
  than surfacing a 403 that reads as a Jarvis bug, and **no device launches the
  client and transfers playback to it** with the launcher stubbed, because a
  test must not open Spotify. Plus the group mechanism end-to-end (below) and
  the bundle refused by all three secrets layers.
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
- `tests/face/hud_delta_check.py` — free headless checks that the HUD renders a
  reply *while it is being written*: text appears mid-turn, the draft is plain
  text (half a markdown document is not markdown — rendering `**bold` mid-word
  flickers), the finished reply replaces the draft and is rendered once, a
  cancel mid-sentence removes the draft rather than leaving words he never
  finished saying, and streamed text cannot inject markup into the window that
  gates approvals. Harness note worth copying: **close each turn's `/converse`
  response with a `None`**, or the previous handler is still blocked on the
  queue and the next turn's lines are delivered to a dead connection.
- `tests/face/hud_capture_check.py` — free headless checks for the mic being
  open all the time, and for the detector that carves turns out of it. Two
  browser contexts, for two questions: one with a granted (deliberately
  **silent**) fake device, asserting the ring fills from boot with nothing
  held and nothing said; and one with the microphone **denied**, so the ring
  is written only by the test and the sample arithmetic is exact. Driving it
  needs no audio — a frame of N samples at a constant amplitude has exactly
  that RMS, so `__hud.mic.feedMs()` scripts a room through the real
  `onCaptureFrame`, ring, segmentation and upload. The three owner complaints
  are three assertions: speech at 0.02 opens an utterance (the old fixed 0.045
  never would), a 1.1s pause mid-sentence does not end one, and 20s of
  background talk lifts the threshold instead of wedging the recording to its
  cap. Plus: an unclaimed utterance is never sent, a late wake hit claims the
  one already in flight and uploads *more* audio than was spoken (the pre-roll
  that used to be lost), the follow-up window opens and closes, his own speech
  never becomes a turn, a 90ms noise is not a turn and does not consume the
  window, and push-to-talk keeps its pre-roll while a tap is still refused.
  Since 2026-08-20 it also owns the **MIC mute** (the owner's input-side mute):
  muted, speech opens and uploads nothing, wake hits and the follow-up window
  are inert, push-to-talk records nothing while the orb still interrupts, the
  threshold keeps tracking the room, the state survives a reload, and **audio
  captured while muted never leaves the machine** — the wake back-dating that
  would reach across an unmute is clamped at the unmute mark (verified to
  bite: with the clamp removed, 2.6s of muted-period audio uploads).
- `tests/face/speculation_check.py` — free checks for synthesizing ahead of the
  turn, `voice.tts` replaced by a recorder so nothing is rendered. The first
  one is the whole safety argument: six replies fed **character by character**,
  with every chunk ever declared settled required to survive byte-for-byte to
  the finished reply — if that fails, the speculator spends the TTS lock on
  strings that are never spoken and is slower than not speculating at all.
  Then: a hit is not also re-rendered and nothing is synthesized twice, guesses
  made before a tool call are dropped on `interim_text`, the limit holds on a
  40-sentence reply, a failing synthesis *and* a failing guess both leave the
  turn alone, and muted turns speculate nothing. Run after touching
  `_sentences`, `_stable_chunks`, `_Speculator`, or the delta sink.
- `tests/stream_check.py` — free checks for streamed completions, the HTTP
  layer faked: content and split tool calls reassembling by index, usage/model/
  finish_reason surviving, **a mid-stream cancel raising `Cancelled` and
  leaving the transcript wire-valid** (no assistant turn, nothing outstanding),
  retry allowed before the first token and refused after one, the fallback to a
  non-streaming call when streaming never starts, and `stream=False` taking the
  old path. Run after touching `llm.chat`, `_stream_once`, or the cancel path
  in `run_turn`.
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
  both new files being SELF_PROTECTED. Since 2026-08-17 it also owns the
  **separator and gluing** cases — a bare `&` splitting, glued redirects
  (`echo pwned>~/.bashrc`) and glued inline source (`python -c"import os"`)
  all falling to ASK, quote-aware segmentation keeping `git commit -m 'fix A &
  B'` at ALLOW, and `command_stems` refusing to enumerate a substitution — and
  a **`run_readonly` section**, because that tool is ungated and its allowlist
  is therefore the whole boundary: 45 escapes (newline and `&` separators,
  `env sh -c` wrappers, the writing git subcommands, `cat<.env`, and `find` in
  its `-exec`/`-delete` forms, and `rg`'s program-running flags) refused and 7 ordinary reads still unattended —
  and, since 2026-10-09, **every git form refused with a pointer to the `git`
  tool** (`git` left the allowlist; the grammar the old lists protected is
  asserted in `tests/gitops_check.py`).

  Since the same day it also owns **both narrowing suites**, because a
  boundary is only correct together with the ordinary work it lets through.
  `redirect_shape_checks` grades 44 redirect shapes on whether they write to a
  path — fd-duplication (spaced or not), a close, a quoted `>`, input, a
  here-doc, a here-string and a backslash-escaped `>` do not; `&>file`,
  `>&word`, `>|`, `<>`, fd-numbered and `{name}` forms, `2>/dev/null` and
  `2>&1>out.log` do — and pins `2>&1` and `>|` surviving segmentation intact.
  `run_readonly_narrowing_checks` pins 9 quoted
  metacharacters still running unattended (it pinned 21 git reads too, until
  they moved to the `git` tool). **Since 2026-10-09** (this PR)
  `reader_hazard_checks` pins the one `rules.READER_HAZARDS` table — 58 writing,
  reading or running forms of readers caught (sed `w`/`W`/`r`/`R`/`e` scripts,
  `sort -o`, `tree -o`/`-R`, `xxd`/`uniq` output operands, `less -o`/`+cmd`,
  `file -C`, `date -s`, `ss -D`/`-K`, `hostname NAME`, `env -S`, the find
  predicates and bfs's `-rm`, and ugrep's `--filter`/`--pager`/`--view`/`-Q`/
  `--save-config`/`--config`/`---`), 65 reads of the same stems still clean
  (grep reads, sed patterns with a bracket holding the delimiter), and
  **every** stem in
  `rules._READONLY`, `protected_state._READERS` and `shell.READ_ONLY` asserted
  to be in the table or in the audited-clean `_NO_HAZARD` set (a new reader
  stem in neither fails loudly). That test cannot catch a *mis-audited*
  `_NO_HAZARD` entry, so `audit_implementation_checks` runs each probe
  binary's `--version` by argv and prints `AUDIT NOT VERIFIED FOR <stem>`
  (informational) when it is not an implementation the audit read
  (`rules.AUDITED_IMPLEMENTATIONS`). `inline_source_checks` pins the inline-source
  rule generalised — the flag in a cluster (`python3 -Ic`), glued by `=`
  (`node --eval=`), a subcommand (`deno eval`), and source fed on stdin (a
  pipe or here-string into a bare interpreter, which bare `python3`/`node`/
  `deno` therefore ASK while a named script, `-m`, or `< file` stays ALLOW).
  `continuation_checks` pins backslash-newline joining and process
  substitution asking. `fails_against_old_code_checks` reconstructs each
  finding's pre-change verdict so the gap is proven real. Since 2026-10-08
  `gate_state_checks` owns
  **shell writes to the gate's own state**, under a throwaway HOME with an
  allowlist entry for every stem in sight: 85 spellings of a write onto the
  allowlist refused (allowlisted stem, mode "all", and no approver at all),
  23 gate-state writes asked and never run on a no, and 20 ordinary
  commands — reads and backups of those same files among them — unchanged.
  `gate_state_complexity_checks` pins the substitution bound: 5,000 openers,
  200-deep nesting and 2,500 spans each judged in well under a second as too
  complex to judge, never ALLOW.
  `gate_state_edge_checks` re-runs the old quoting/separator exploits as
  verdicts (none may move but toward stricter), pins the symlinked-folder,
  `ROUTING_PATH`-elsewhere and explicit-`cwd` cases, and (2026-10-09) pins
  the three new-grammar families — a cluster/`env -S`/`builtin` shell string,
  the drifted redirect forms, a sed `w`/`-i` script — each a certain write
  onto the allowlist, DENY, never asked, never run, with the allowlist file
  byte-identical afterwards.
  Run after touching `rules.py`, `tools/shell.py`, `command_review.py`,
  `protected_state.py`, `permissions.gate`, or `dispatch()`.
- `tests/permissions_check.py` — free checks for modes and the allowlist:
  gate ordering, stem vs whole-tool matching, persistence without
  duplicates, mode "all" bypass, and the broker's ALWAYS path writing an
  entry. Since 2026-08-17 also the two halves of the one-word-wide hole:
  **every segment must be covered** (a `git` entry covers `git push` and
  `nohup git push`, and covers none of `git status && rm -rf ~`, the `;`/`&`/
  `|`/newline spellings of it, a `$(...)`, or a `curl … | sh`) and the gate
  really handing a smuggled line to the surface approver; plus **no path mints
  a wildcard** — `entry_for` raises on an empty or compound command, and a
  prefix-less command entry written straight into the JSON authorises nothing.
  Plus the narrowing: a **full-path entry still matches** (`/usr/local/bin/
  mytool run`, wrapped, and beside a `git` entry on one line) while covering
  only itself — not the bare basename, not the same name under another path,
  not a string prefix of it, and not a second command riding behind it.
  Since 2026-10-09 `gate_state_coverage_checks` pins that **no allowlist entry
  covers a write onto the gate's own state** in any of the new spellings (a
  `bash -lc`/`env -S` wrapping a `cp`, a drifted redirect, a sed `w`/`-i`
  script, a continuation before the target, a ugrep `--save-config`), even
  with the stem allowlisted,
  while a genuine read of a gate file stays covered.
  Run after touching `permissions.py`, `rules.command_targets`, or
  `approvals.py`.
- `tests/skills_check.py` — free checks for skill tools (round-trip, bad
  names, starter skills parse) and the index: render/refresh/cap, plus
  agent-level injection against a faked `llm.chat` (present and per-turn
  fresh for skill-armed agents, absent for others, mid-session skills
  visible next turn).
- `tests/workflows_check.py` — free checks with a faked `llm.chat`:
  background lifecycle, status/log tools, deny-all approver, safe toolset,
  concurrency cap. Run after touching `workflows.py`.
- `tests/gitops_check.py` — free checks for the agent git tools; `llm.chat` is
  a recorder and `gh` a stand-in script, every config path and HOME a temp
  dir, nothing through a shell. Two layers because they fail differently.
  **`judge()` is pure, so its verdicts are tables** (placeholder names only):
  66 reads that run in every context, 68 routine writes that run only in an
  own worktree on an own branch (and are refused in the owner's checkout,
  and for the branch-moving half off an own branch), 8 reviewed forms, and
  289 refusals grouped by what each protects — including every abbreviation
  of the dangerous long options, an alias that tries to be a subcommand,
  unknown remotes, credential names, and the "shape" cases (global options,
  non-lists, oversize). **The runner is exercised against real git** in a
  temp repository with a bare origin: commits land in the worktree and the
  owner's checkout never moves; a hook that plain git runs does not run
  through the tool; an external-diff program from the environment or the
  owner's global config does not run; GIT_* is scrubbed; a repository's own
  program settings refuse everything; a sweeping commit that stages a
  credential is taken back without losing the work; a branch carrying one
  cannot be pushed; a PR needs commits and a worktree of ours, pushes only
  its branch, calls `gh` with the title and body as `=` arguments, and never
  merges; every way the reviewer can fail declines; the command cannot close
  its own fence. Also: `run_readonly` refusing git in every costume, and the
  write tools refusing `.git` internals (a symlink into one included) and the
  global git config while `.gitignore`, `.github/` and ordinary worktree
  files stay writable. **Verified to bite** by an in-process mutation sweep
  (disable each protection, the matching check must fail) — 14 of 15 at
  first, the 15th being a mutation that did not mutate, now a structural
  assertion. **Submodules** (2026-10-12, PR #65), against a real submodule cloned from
  a local bare repository into a tool-made worktree, every trap a script that
  writes one marker: `submodule_pin_checks` puts the drivers in the owner's
  global config, named only by the submodule's attributes (so the config check
  has nothing to refuse and only the pins are tested) — status, diff,
  diff-files/-index and commit -a never run its filter, a `.gitmodules`
  `ignore = none` included; log -p, show and diff A B never run its textconv
  under `diff.submodule=diff`; fetch, switch, grep and push never recurse (the
  push neither publishes the submodule's commit nor runs its pre-push hook);
  its own fsmonitor never runs — each against plain git that does.
  `submodule_config_checks`: a program in the submodule's own config, or a
  nested repository's, refuses every subcommand (add -A, describe --dirty and
  rm, which no pin keeps out, among them); a moved `core.worktree`, a broken
  config, a `.git` file pointing nowhere and the limits fail closed; a clean
  submodule and an unchecked-out gitlink are not refused. 16 of the scenarios
  go into the submodule on main, none here (fsmonitor and the hook were
  already held off by the propagated `-c`); dropping the pins, only the
  command-line flags, the config read, or judge's option refusal each fails
  its own check. `gh_git_checks` (review of #65): a stand-in `gh` that runs
  `git status` from PATH, as gh does, runs neither the repository's nor the
  submodule's fsmonitor through `open_pull_request`, and runs both in
  gitops' environment without the pins; dropping `_gh`'s pins fails it.
  The delta review's tables (a `--` that may be an option's value, signing,
  a flag read as taking a value, a repository named after `--`, stash's
  `-m`) and `signing_checks` (a trap `gpg.program` that plain git runs on
  `commit -m -- --gpg-sign`); each of its five fixes, removed, fails its rows,
  and a `judge()` differential against 5d5362b over 520,460 verdicts moved
  27,183, every one toward REFUSE, none looser. Run after touching `gitops.py`, `tools/gitctl.py`,
  `tools/files.py`'s protection, or `run_readonly`.
- `tests/tasks_check.py` — free checks for attended background tasks, with
  `llm.chat` scripted, `shell._run` a recorder, the broker on approval_check's
  FakeWindow and the DM sender injected. The headline: a task's dangerous call
  raises the card **with the task's origin label**, approve runs it, deny does
  not, and the same flow works remotely (DM carries the origin line, a typed
  yes runs it). The capture properties both ways: a real gate keeps its
  human-backed flag inside a task (an ALLOW `git commit` runs with **no
  card**), and an unbound context yields a deny-all task. The
  nothing-changed half: with no origin bound the SSE payload has no origin key
  and the DM body is byte-identical to the pre-task shape (verified to bite
  against unconditional attribution). Plus: toolset exclusions and task tools
  absent from `workflows.SAFE_TOOLS` / `SUBAGENT_TOOLS` / `goal_tool_names()`,
  the context-block section for armed agents only, the concurrency cap,
  cancel landing at the step boundary *without* resolving a pending card, the
  label sanitizer, and origin inheritance through `copy_context()` with no
  cross-context leak. Points `config.ALLOWLIST_PATH` at a temp file for the
  whole run. Run after touching `tasks.py`, `tools/tasks.py`, `runtime.py`,
  or `ApprovalBroker.request`.
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
- `tests/voice_pocket_check.py` — free checks for the Pocket TTS backend, a
  fake `pocket_tts` injected into `sys.modules` (its sample rate deliberately
  16000, not Kokoro's 24000, so a hardcoded rate fails). The cases that
  matter are the degradations, written to fail against the pre-pocket code:
  a raising backend, a missing store voice, the library absent, and a
  `JARVIS_TTS_VOICE=pocket:x` loop all land on a Kokoro name — the cloud
  never once sees a `pocket:` name. Also: routing at the backend's own rate
  with model/state cached, the speed-ignored-once warning, `set_voice`
  accept/reject/clear, an avatar speaking a pocket voice, the `_blend`
  mixing math on numpy (normalization, head-keeping seq trim, every loud
  refusal), clone/mix/rm store round-trips, and `/voices` + `POST /voice`
  against a real threaded server (grouped entries, `""` clears the override,
  404/403). Run after touching `pocket.py`, `voice.py`, or `_switch_voice`.
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
  heard — and the armed hint names whatever is live. Since 2026-08-22 it also drives
  the real `recog.onresult` through a scripted `SpeechRecognition`: a bare
  phrase fires **once** rather than once per delivery, carrying on talking does
  not re-fire, a new phrase in a later segment does, ordinary speech in a new
  segment stays silent, and a restarted recognition session (indices back to 0)
  fires again. Run after touching `WAKE_PATTERNS`, `matchesWake`, or
  `armRecognition`.
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
  Since 2026-08-17 it also pins `forbidden_title()` as a **substring** match
  (a suffixed or prefixed HUD title is still refused, because bridge.py finds
  windows by substring) and reads both `<title>` tags out of the real
  `jarvis.html` / `whiteboard.html` rather than trusting hardcoded copies —
  renaming a page used to disarm the check silently. Run after touching
  `desktop.py`, `tools/desktop.py`, `windows/bridge.py`, or
  `windows/uiatree.py`.
- `tests/models_check.py` — free synthetic checks for the model roster, with
  `llm.catalog` replaced by a fixture and both state files in a temp dir. The
  headline is the refusal: a model that cannot call tools, an invented id, an
  image-only model, `openrouter/auto` and a `:batch` variant all fail to reach
  the roster. Plus prices converted to dollars-per-million, unrated staying
  `None` rather than 0, the cache fetched once and surviving an outage with a
  stated reason, a corrupt roster file degrading to the default, removing the
  selected model clearing the selection, `tier()` moving the followers while
  `worker`/`cheap` stay, `Agent()` with no `model=` picking up the selection,
  and the routes end-to-end (`/model` moving the **live** agent without
  rebuilding it, 400/403/404). Since the same day it also owns **reasoning
  effort**: clamped down to the model's own ladder, absent for a model with no
  reasoning block, the requested level on a cold cache, `""`/`default`/a typo
  all sending nothing, and — the one that matters — the agent loop putting
  `reasoning: {effort}` on the wire while `delegate(tier="cheap")` does not.
  Since 2026-08-23 it also owns the **per-model effort pin** (beating the
  global, refused off-ladder and on a model with no reasoning control, not
  outliving its model, and both the setting and its consequence in
  `describe()`) and **BYOK cost accounting** — the measured credit-billed and
  BYOK usage shapes, and `Reply.cost_usd` carrying the upstream figure when
  the credit line reads zero. Run after touching `models.py`, `config.TIERS`,
  `config.REASONING_EFFORT`, `llm._cost`, or the `/model*` routes.
- `tests/face/hud_model_check.py` — free headless checks of the picker in the
  real `jarvis.html`: CORE opens the shortlist with the default marked, ADD
  MODEL browses the catalog, search matches name and id, the price and
  intelligence filters narrow it (and the unrated exclusion is stated), a row
  toggles roster membership, Escape backs out of the catalog to the shortlist
  before closing, selecting relabels CORE, the × removes without selecting,
  and an SSE `model` broadcast relabels live. The effort control is covered
  too: drawn only for models that publish a ladder, offering that model's own
  levels rather than one shared list, AUTO naming what the default resolves
  to, and — the one worth having — **setting it does not also switch him onto
  that model**, which is what clicking the row does. Two that are not
  cosmetic: **a model name carrying markup renders as text** (names come off the network and
  this window draws authorization cards), and **a space typed in the search box
  does not trigger push-to-talk**.
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

<!-- claude-source:006373-007176:end -->

## Original CLAUDE.md lines 7335–7494

<!-- claude-source:007335-007494:begin -->
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
  **Since 2026-09-03 the pin may be an ordered comma list**
  (`deepinfra,novita,z-ai` → OpenRouter's `order` array, `allow_fallbacks`
  still false), added for the trading firm and verified live: with
  DeepInfra 429ing upstream the list fell through to Novita, and a
  single-tag `deepinfra` pin failed outright. Fallback *within* the list is
  the point; fallback *outside* it is still refused. The tag is the bare
  provider slug — `novita`, not the endpoint's `novita/fp8`, which routes
  nowhere. `tests/stream_check.py` owns it (`provider_pin_checks`).

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

<!-- claude-source:007335-007494:end -->

## Original CLAUDE.md lines 9195–9286

<!-- claude-source:009195-009286:begin -->
**long-bench shipped (2026-08-15)** — `longbench/`, the ruler the *Not done*
list had been asking for since 2026-08-01. It is the first bench here that
**does not import Jarvis**: a task is a pure `plan(seed, scale)`, a
`materialize` that writes a directory, a prompt (plus optional follow-up turns),
and a grader that reads the directory back — so anything that can be handed a
prompt and a working directory can be measured: the Jarvis loop, `claude -p`, or
a person with an editor. That is what makes a cross-harness comparison possible,
and it is why the code lives outside `jarvis/`. Three tasks: `sweep` (a 166-site
migration across 422 files, then the spec changes), `audit` (483 files,
precision *and* recall against 238 planted violations), `thread` (a shard chain
with a cohort rule, a salt, and two truncated files recoverable only from a
`.bak`). Six categories: completeness, retention, discipline, recovery,
interference, correctness. Design, parity gaps and baselines in
`longbench/README.md`.

Four design rules, in increasing order of how much they cost to learn:

- **Ground truth is recomputed from the seed, never written to disk.** `plan` is
  pure, so the grader regenerates the answer at grading time. A bench that
  stores its answer key in the workspace measures reading comprehension.
- **Checks carry floats, not booleans.** agent-bench's checks are
  all-or-nothing, which is fine for the properties it tests; for a bench about
  *completeness*, "31 of 44 sites" and "0 of 44" scoring the same would throw
  away the measurement.
- **The rock passed the safety check three more times.** `sweep` shipped with
  "every edited module still parses" — an *untouched* module parses fine, so a
  do-nothing run collected 3 free points. Then "no reference to the old API
  left", which a **deleted** file satisfies perfectly. Then, a rewrite later,
  both *interference* checks: an agent that migrates nothing also never obeys
  the stale document it never read. All are conjunctive with having done the
  work now, and the suite caught each one on its first run — which is the whole
  argument for writing the empty-run assertion before the graders.
- **Every escape hatch is symmetric or it is a handicap.** The levers that
  remove shell (`--no-shell`) and set the compaction threshold (`--window`) move
  on **both** harnesses — Claude Code's `--autocompact` and Jarvis's
  `ContextPolicy.compact_at_tokens` — and the CLI refuses a window below
  Claude Code's 100k floor rather than quietly giving the two sides different
  numbers. Mode, window and scale are recorded on every run record and printed
  in the row label, because pooling runs from different configurations is the
  easiest way to publish a wrong number.

**The v1 bench saturated, and the diagnosis is the lesson.** At its first sizing
Luna scored 100% on all three tasks for $0.107 total. Two escape hatches, both
found only by running it: Haiku scored 100% on `thread` in **eight tool calls**
by writing a shell script (forty shards, none of which entered a context
window), and Luna scored 100% on `audit` in 21 calls with `grep_files`. But the
real defect was cruder — **the worlds were ~20x too small for compaction to ever
fire**, so the bench measured task success while claiming to measure context
management. Generalises past this bench: **a long-horizon test has to be
calibrated against the window it is meant to overflow, not against how long it
feels to write.**

The constraint that shapes every task here:

> Any task with mechanically-detectable ground truth can be solved by search,
> and any task whose ground truth is not mechanical cannot be graded
> deterministically.

Two ways out, both now used. `sweep` resists structurally, because **edits
cannot be grepped** — search finds the call sites, but each file still has to be
read and rewritten. `audit` resists by making its secret rule **cross-file**: a
literal secret is exempt if that service's `handler.py` signs it off, which one
grep cannot answer.

**Baseline at scale 4 (seed 1, `--no-shell --window 100000`): Luna scored 95% on
`sweep`** — $1.70, 32.5 min, 203 turns, 882 tool calls (95 repeated). Perfect
completeness (166/166 sites migrated and 166/166 tagged by the follow-up turn),
correctness, discipline and interference. **The single miss is the one worth
having built the bench for:** the changelog entry says 156 call sites when the
true count is 166 — a number derived in the middle of a 32-minute run, carried
to the last file written, and off by ten. No other bench here can see that
class of error.

Two operational lessons from the same run:

- **Do not grade mid-run.** A snapshot at ~17 minutes scored 48%, with 41 of 88
  modules unparseable: the first pass had rewritten `legacy_emit(` to
  `emit(name=` textually, which puts a positional argument after a keyword one.
  Luna found and fixed every one before finishing, and had also gone back to
  migrate both packages the stale `NOTES.md` calls frozen (0/19 sites at the
  snapshot, 19/19 at the end). An in-flight score measures a state the agent has
  not finished being in.
- **The Claude Code cell is the expensive one by a wide margin.** On `thread` at
  scale 1, Claude Code with *Haiku* cost 38x what Jarvis with Luna did ($0.2275
  vs $0.0060), and Sonnet 5 is dearer per token than Haiku. Always pass
  `--budget`; calibrate at `--scale 2` before committing to a full scale-4
  sweep.

`redundant_calls` (95 of 882 here, ~11%) is the repetition signal the *Not done*
list wants the loop to act on. The measurement now exists; the loop still does
nothing with it.

<!-- claude-source:009195-009286:end -->

## Load-proof bounds, round two (QA-LOAD, 2026-10-14)

Reproduce with load you own, and give it the shape of a sweep: each suite runs
in its own `systemd-run` scope, and cgroup fairness gives every scope an equal
share, so 24 busy loops in **one** scope barely deschedule a test in another.
48 single-process scopes (36 spinning, 12 copying 48 MB buffers), each a
`systemd-run --user --scope -p MemoryMax=… timeout N python3 -c '<loop>'`, did.
Kill them by a pattern that is not a regex trap (`pkill -f "x += 1"` matches
nothing: `+` is a quantifier); `timeout` is the backstop.

- **Thread CPU time still slows under load.** A busy SMT sibling and shared
  caches slow every instruction: `rules_check`'s cost line went 52-69 → 100-104
  CPU ms against 100 ms. `tests/loadclock.py` judges a missed CPU bound again
  at the machine's present speed: a fixed reference workload (none of
  Jarvis's code) is timed right after the run, fastest of three, against its
  idle cost (`REFERENCE_IDLE_S`, 10 ms here), and the run's CPU time divided by
  that slowdown, never below 1x, never above 3x. Idle, that is the exact
  bound. Under load it is looser: the QA-LOAD review measured the reference
  slowing 1.29-1.44x under 24/48 busy loops while the real work slowed
  1.06-1.27x, so a bound is relaxed by ~10-30% (the sniffer flood ~29%), and
  the best of three keeps the most favourable ratio. A regression up to ~30%
  past its bound can pass under load (it fails alone). Past 3x nothing is
  excused (work over 3x its bound fails under any load): the pinned mutants `charge()` a no-op (5.3x its 100 ms bound) and
  colour off the sniffer's bulk path (3.8x below its 28 MB/CPU-s floor) fail
  under any load; `vex_check`'s synthetic 8x `decide()` (2.0x its 1 s bound)
  is inside the cap and failed every run under 48 burner scopes, but is not
  guaranteed to.
  `rules_check` (cost), `vex_check` (floods), `terminal_check` and
  `devurl_check` (throughput) use it and pin their limits and its constants;
  `tests/loadclock_check.py` pins the clamps. Recalibrate with
  `python tests/loadclock.py` on an idle machine, rounding down.
- `devurl_check`'s flood still timed the wall: 15-25 MB/s against 28 under
  load, every run. It times the calling thread now, like `terminal_check`.
- `runner_check`'s escape-hatch collision was ordered by a 50 ms sleep: idle
  it never collided (the no-retry mutant passed), loaded it failed 7 of 10.
  It is ordered by state now and asserts the collision happened.
- Checked under that load and already fixed on main: `hud_backend_check`'s
  Claude quota (2da054d), `discord_mirror_check`'s create back-off (4b50ce3),
  `web_hygiene_check`'s error-page commit (62856fe), `hud_v2_editor_check`'s
  `_kinds_checks` and the `.editorconfig` vitests (ED-8), and the full vitest
  suite (2 of 2).

## Role bench (2026-10-20)

`python -m rolebench` picks the cheapest model that is good enough to review or
implement a pull request in this repository (plan:
`docs/plans/2026-10-20-role-bench-plan.md`, decisions and case schema:
`docs/plans/2026-10-20-role-bench-decisions.md`). Cases are JSON files in
`rolebench/cases/`. Its scores are **never pooled** with agent-bench,
long-bench or swecompare: it measures review recall and cost to merge-ready on
this repo's own history, not generic ability.

    python -m rolebench snapshot hint-r1
    python -m rolebench review --harness claude --model haiku --case hint-r1 --runs 3
    python -m rolebench judge --run ~/.cache/jarvis-lead/rolebench/runs/<run-id>   # or the runs/ directory
    python -m rolebench implement --harness claude --model sonnet --case impl-279 \
        --reviewer-harness claude --reviewer-model opus
    python -m rolebench report

Everything is written under `~/.cache/jarvis-lead/rolebench/` (`ROLEBENCH_HOME`):
`snap/<random>/` (one isolated repo per run, named at random so no path a model sees
carries the case, model or run id: `git archive` of the base, the PR diff as one
commit, no remote, no later objects, the bench's own files stripped, objects packed;
an implement snapshot has the hidden tests removed, restored only in a grading copy),
`runs/<run-id>/` (prompt, review text, findings, `run.json` with the snapshot path
and the build-time base/change shas, `judge.json`, `stdout.jsonl`), `log.jsonl`,
`spend.json` (the OpenRouter ledger), `hud-deps/node_modules` (the bench's own copy
of the HUD's dependencies, made once, mounted read-only, rebuilt if a case's
base carries a different `package-lock.json` hash than the recorded one),
`spotcheck/` and
`spotcheck-keys/` (kept apart), `valid_new.jsonl` and `report.md`.
`python -m rolebench clean --judged` removes the snapshots of judged and finished runs.

**Confinement (2026-10-20 fix round).** Every agent process (claude, codex and Jarvis
reviewers and implementers, every fix round, the judge, and every suite run over
agent-written code) runs in `rolebench.sandbox`: `systemd-run --user --scope
--unit=rolebench-<id>` (3 GB, no swap) around `bwrap`. Mounted: `/usr` and its
`/bin`/`/lib*` links, `/etc` read-only (`/etc/ssh` masked, `/etc/gitconfig` blanked),
`/opt/google` (Chrome, for the Browser suites), the venv read-only at
`/opt/rolebench/venv`, the work tree read-write (read-only for the judge), the bench's
`node_modules` copy read-only (vite's cache dirs get a tmpfs each), and a tmpfs HOME
and `/tmp`. Not mounted: the owner's checkout (so the venv's editable `.pth` points
at nothing and no code imports from it), `~/.config/gh`, `~/.gitconfig`, `~/.ssh`,
`/mnt` (but WSL's `resolv.conf` where the cell has network), `/run` (no user bus: a
cell cannot ask systemd to start anything outside), and the lead's `/tmp/claude-*`.
Logins are a **trimmed, in-memory view**, not a file bind (A2, decisions doc).
The claude cell and the judge get `{claudeAiOauth: {accessToken, expiresAt,
scopes, subscriptionType, rateLimitTier}}` — no refresh token, no MCP OAuth
block; the codex cell gets `auth.json` without its refresh token, in its own
`CODEX_HOME` on the tmpfs (no owner config, skills, memories or `trusted` entries
written back); the Jarvis cell gets no login (the OpenRouter key goes to the
worker on stdin). The trimmed JSON is written to a pipe and bound with `bwrap
--perms 0400 --ro-bind-data <fd>`, so it is never on disk and never written
back. A login that would expire within the run is refused, since a refresh in a
read-only cell could rotate the token without saving it. The environment is an
allowlist (`PATH`, `HOME`, `USER`, `LANG`/`LC_*`, `TERM`, `TZ`, `TMPDIR`, plus each
CLI's own), with `GH_CONFIG_DIR` empty, a `GIT_CONFIG_GLOBAL` with no credential
helper and no hooks, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0` and
`GIT_ASKPASS=/bin/false`. A timeout kills the whole unit (setsid children too).
Each cell also gets its own PID, IPC and UTS namespaces (`--unshare-all`): the
host's processes, its `/proc/1/root` and its SysV IPC are invisible.

Network (A1, pasta wired): a grade cell never has network. A
claude, jarvis or judge cell runs under **pasta** (`ROLEBENCH_NET=pasta`, the
default when unset): `systemd-run --scope … -- pasta <flags> -- python -I
rolebench/netcell.py <policy> -- bwrap …`. pasta (passt 0.0~git20260120) makes a
fresh user and network namespace with `--config-net --no-map-gw
--map-host-loopback none --map-guest-addr none -t none -u none -T none -U none
--dns-forward 192.0.2.53 --ipv4-only --no-icmp --no-dhcp --no-dhcpv6 --no-ra`
(all four forwards default to `auto`; `-T auto` alone would splice the cell's
127.0.0.1 to every host loopback port). pasta by itself still reaches a host
address that is not on its interface (WSL's `10.255.255.254` on `lo`, where
0.0.0.0 services answer), so `netcell`, running as root of pasta's namespace,
installs `ip rule`s before anything else starts: DNS only to the forwarder on
port 53; `0/8, 10/8, 100.64/10, 127/8, 169.254/16, 172.16/12, 192.0.0/24,
192.168/16, 198.18/15, 224/4, 240/4` and every address the host owns
prohibited; TCP 443 through; everything else prohibited; IPv6 prohibited and
switched off on every interface but `lo`. It reads the rules back, refuses
(exit 125, nothing started) unless every one is in place, re-creates the
credential pipes (pasta closes every inherited descriptor but 0–2, so the
trimmed logins reach netcell as a length-prefixed header on stdin and the rest
of stdin is left for the agent) and execs bwrap with `--share-net` (pasta's
netns) plus `--unshare-user --uid <host uid> --gid <host gid> --cap-drop ALL`:
the agent has no capabilities over the namespace and cannot change the rules.
The cell's `resolv.conf` names only the forwarder (`options no-aaaa`, no search
list). pasta and bwrap live in the run's unit, so a timeout kills both.
Measured against throwaway listeners (`tests/rolebench_check.py`,
`test_pasta_network`): host `127.0.0.1:<port>`, `0.0.0.0:<port>` on the eth0
address and on `10.255.255.254`, the gateway, `10/8`, `192.168/16`, `172.16/12`,
Tailscale's `100.100.100.100`, `169.254.169.254` and a host abstract unix socket
are all unreachable (zero accepts); DNS resolves and a TCP connect to
`api.anthropic.com:443` and `openrouter.ai:443` succeeds (Python and Node), TCP 80
is prohibited. `ROLEBENCH_NET=none` gives those cells no network at all, so real
runs of them are refused up front; any other value, or pasta missing, is refused
and never falls back to the host netns. Codex is the one exception: it keeps the
host netns, because its own sandbox keeps its *commands* off the network while
the codex process reaches its API. **That is the remaining exposure**: the codex
process shares the host netns (so a codex cell could reach this machine's
loopback ports, which is why its own sandbox and the network audit matter), and a
pasta cell can reach any public address on TCP 443 (so a public address that
loops back to this LAN, such as a router's hairpin NAT, is not closed). **Accepted
exposure (lead, review of #284):** a pasta cell can send to any public address on
TCP 443 and resolve any name through the host's resolver, so an agent could send
the snapshot's repo code out; the only credential in a cell is the trimmed login
(A2), never a refresh token, a gh login or a key. The network and outside-the-snapshot audits are best-effort detection
beside that, reported next to the score. Claude's tools are an allowlist
(`Bash,Read,Edit,Write,Grep,Glob`; the judge `Read,Grep,Glob`), and the Jarvis
cell offers no delegation tools.

**The memory cap and its sibling.** A `systemd-run --user --scope` started inside
another scope is a sibling of it under the user manager, not a child, so a nested
scope escapes the outer cap. That is why the prompts no longer tell agents to wrap
suites in `systemd-run` (a cell has no user bus anyway): everything an agent starts
stays in its run's 3 GB unit. The harness's own test and grading runs each get their
own unit.

**Spend.** `spend.json` is read and written only under an exclusive lock, atomically;
an unreadable ledger is "cap reached". A reservation is a pending entry in the file,
so concurrent processes share one cap; a pending entry whose process died is charged
in full. The Jarvis worker wraps `llm.chat`: every reply (any tier, any caller) is
added to a running total streamed on the worker's stdout (a `ROLEBENCH_SPEND` line,
so the harness has it even if the cell is killed mid-call); the call whose estimated
cost (prompt chars/4 plus `max_tokens`) at the model's **most expensive** OpenRouter
endpoint (`/api/v1/models/<id>/endpoints`, the highest prompt and completion price
over every endpoint, cached a day under `prices/`; if a refetch fails a cache at most
7 days old is used, raised 25%, else the model is refused), counting the tool schemas,
a completion allowance (the call's `max_tokens`, else the endpoint's maximum, else
16k) and Haiku 5.5's 5x price from 100k prompt tokens, would cross the budget is refused before it is
sent, no call is made once the total reaches the budget, and the call that still
crosses it raises (at most one can overshoot). The ledger settles on the usage
OpenRouter reports (`llm._cost`, BYOK upstream cost included), never on the
estimate; a timeout, OOM or crash is charged its whole reservation. Every tier runs
on the cell's model, nothing is provider-pinned, and every `moonshotai/` id is
refused (Kimi left the matrix, O-1 amendment 2026-10-21).

**Quota.** A Claude or Codex cell is skipped at 75% of a quota window, and also when
the quota is unknown (fail closed); the guard is checked again before every model
step (every implement round, both roles) and before each judge call, its retry
included. `--no-quota-guard` (before the subcommand) also needs
`ROLEBENCH_QUOTA_OVERRIDE=<reason>`, prints a warning, and marks every cell
`quota_guard: "off: <reason>"` in `run.json`, the log and the report. **An agent
never passes it without the owner's explicit OK for that run.** The override lifts
the 75% guard only: the hard ceiling `ROLEBENCH_QUOTA_CEILING` (default 97; a value
outside (0, 100] fails closed) refuses a Claude or Codex step or a judge call once
any live window is at or above it, override or not, and an unknown quota is refused
under the override too (the ceiling cannot be checked against it). Every run row
and `run.json` carry `quota_before` and `quota_after` (`{provider: {window: used
percent}}`, read for the providers the run's roles use), and `judge.json` the same
for the judge, so the lead can measure percent per run.

**Snapshot integrity.** After a reviewer the snapshot is checked against the
build-time shas (moved HEAD, extra refs or commits are recorded), then reset to the
change commit and pruned; an implementer's commits are folded back onto the base, so
every diff is against the base. The harness's own git passes `-c core.hooksPath=/dev/null
-c core.fsmonitor=false` and first rewrites `.git/config` and removes hooks and
attributes an agent could have planted. Grading copies restore the hidden tests from
the merged commit and the regression suites from the base, and an edit outside the
case's scope (test files aside) blocks merge-ready.

The judge is `claude -p --model opus` over shuffled, blinded findings plus the answer
key, reading an export of the change commit (no `.git`, its diff beside it) in a
random-named directory; its JSON is validated strictly (one retry). Judging a run
again reuses its verdicts unless `--force`. `spotcheck.md` is a 20% sample for the
lead, with the key in a separate directory. The report splits input, cached and
output tokens (Codex's `input_tokens` include the cached ones), and lists
overridden cells, runs that used more than one model and snapshots an agent changed.
Free checks: `PYTHONPATH=<checkout> OPENROUTER_API_KEY=sk-dummy-not-a-key HOME=<tmp>
.venv/bin/python tests/rolebench_check.py` under the usual `systemd-run` memory cap;
they need `/usr/bin/bwrap` and `/usr/bin/pasta` (the passt package) and run real cells with
harmless probes, against throwaway listeners only; the pasta checks make TCP connects (no
request) to `api.anthropic.com:443` and `openrouter.ai:443`, and nothing reads the live
daemon's quota or OpenRouter's price list (fakes are installed at import).
