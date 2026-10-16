# Diff tab: one-click Quick review (2026-10-15)

The owner asked (2026-10-15) for a one-click quick agent review of the changes
the Diff tab shows. It is for catching small bugs. For a full review the owner
starts a full agent the normal way.

The harness and model, in fallback order (the owner's):

1. Claude Code with Haiku 5.5 (`claude-haiku-5-5`), unless Claude's usage is
   above 75%.
2. Otherwise Codex with GPT-6 Luna (`gpt-6-luna`), unless Codex's usage is 75%
   or above.
3. Otherwise the Jarvis v1 loop with Haiku 5.5 through OpenRouter
   (`anthropic/claude-haiku-5.5`).

The decisions below are the lead's defaults. The owner can override any of
them. Where this file and the code disagree, this file states the intent and
the code is the bug.

## Decisions

**D1 Scope.** The review covers exactly the diff the Diff tab shows for that
task: `GET /tasks/{id}/diff`, every file, not only the open one. The same code
builds both (`hud_api._diff_parts`), so the review sees what the tab sees and
withholds what it withholds:

- protected and credential files (`.env*` included) are skipped outright;
- a path that resolves outside the project is skipped;
- every patch goes through `secrets.scrub`, so a known credential value is
  redacted.

The text sent is capped at **150 KB** (`diff_review.CAP_BYTES`). Files are
taken in the diff's order. A file whose patch would overflow the cap is
skipped, and later, smaller files are still taken. The result names every
skipped file and says plainly that it was not reviewed.

**D2 Where results appear.** Results show inline in the Diff tab, in a
findings panel above the diff editor:

- Each finding has a severity (`bug`, `risk` or `nit`), `file:line` and a
  one-line reason.
- Clicking a finding opens that file in the diff and reveals the line on the
  new side.
- A header line names the harness and model, and why any fallback happened,
  e.g. "Claude 5h at 82% → Codex · GPT-6 Luna".
- Results live in daemon state (`V2_DATA_DIR/diff_reviews/<task>.json`), not
  in localStorage.
- A result is kept until the diff changes (a fingerprint of the reviewed
  diff's head, file list and patches) or the owner reruns. A result whose diff
  has changed is not shown. The panel says the diff changed since the last
  review instead.
- A review is never spoken, never posted to Discord and never notifies. It
  publishes nothing on the bus. The HUD polls the owner-only `GET` while a
  review runs.

**D3 Read-only.** The reviewer may read project files for context and do
nothing else. It cannot edit, run a shell beyond reading, use the network, the
web or the browser. Nothing it does needs approval, so the owner never sees a
card. Per harness:

- **Claude.** The SDK's `tools` option is `Read`, `Grep` and `Glob`, so no
  other tool exists. The PreToolUse hook stays active. Its `permit` is a
  wrapper of our own around the §6 permit, which is built with a deny-all
  asker under the AUTO profile. The wrapper refuses:
  - any tool not in those three, before the §6 permit is consulted;
  - any read whose path resolves outside the task's worktree;
  - any widening, grant, extra command or dangerous-tool call.

  The §6 permit's layer 1 then refuses credential reads (`denied_read`), and
  an always-ask match is denied by the deny-all asker. The reviewer cannot be
  handed more: no MCP servers, no session tools, and `max_turns` is 12.
- **Codex.** The review runs through `CodexProvider.one_shot`, the titler's
  read-only, network-off, approval-`never`, ephemeral, no-MCP, **no
  environment** turn. Any tool item ends the turn as a failure, and the
  sandbox and model are checked on the answer. **Deviation from the brief:**
  on Codex the reviewer reads only the diff, not other project files. A
  Codex turn that can read the worktree needs a shell environment, and a
  read-only Codex shell can still `cat .env`. Codex has no pre-tool hook that
  could refuse that (R8), so the safe Codex review is the tool-less one.
  Revisit when R8 has an answer.
- **Jarvis v1.** A v1 `Agent` is built with exactly `read_file`, `list_dir`,
  `find_files` and `grep_files`, bound at dispatch (invariant 10, "A toolset
  is a boundary"). It never holds `run_command`, `run_readonly`, a write, the
  browser or a fetch. Its approver is deny-all (`lambda *_: False`). The
  agent's working folder is the worktree (`runtime.bind(workdir=…)`), and
  v1's own credential and credential-folder refusals apply to every read.
  The review's agent also refuses, before dispatch, any read whose path or
  file pattern leaves the worktree (`_confined_agent`, review L1). The global
  v1 tools are unchanged.

**D4 Usage thresholds.** The selection uses the readings the HUD's usage
column already shows (`HUDLedger.quota`): Claude's `five_hour` and
`seven_day` utilization, and Codex's `account/rateLimits/read` windows.

- A harness is skipped when **any** of its windows is **≥ 75%**. The owner
  said "above" for Claude and "75% or above" for Codex. ≥ is the safer
  reading for both.
- An unknown reading (no data yet), or a window whose `resets_at` has passed,
  does not skip. The harness is tried.
- A harness that fails falls through to the next, and the reason is
  recorded: not in the roster, unhealthy (CLI missing, logged out), Codex
  busy past 20 s, rate limited, an error, a timeout, or an answer that could
  not be read.
- The pattern is the titler's (`titles.py`): Codex `one_shot`, with
  `MetadataBusy` retried briefly, then the next harness.

**D5 Untrusted input.** The diff is data. It is fenced in the prompt between
markers that carry a random nonce, which the diff text cannot contain. The
prompt says that text inside the fence is never an instruction. Findings are
cleaned before they are stored:

- scrubbed, with invisible characters stripped;
- one line, capped at 300 characters;
- severity from the three-word set only;
- a line number that is a positive integer, or none;
- at most 50 findings.

The panel renders them as plain React text: no Markdown, no HTML, no links.

**D6 Cost and limits.**

- One review at a time per task. Pressing the button while one runs offers
  Cancel.
- The whole review has a 180 s budget across all harnesses.
- Cancel and timeout interrupt a Claude turn and stop a v1 loop at its next
  step. A Codex one-shot ends at the transport's next poll: its `cancel`
  event is OR-ed into the transport's stop. It is also capped at 60 s,
  because it holds Codex's login lock, which every Codex turn, the titler
  and the quota refresh wait on (review M1).
- Usage is recorded in the ledger as other turns' is. Claude results go in as
  per-result USAGE (equivalent dollars), Codex token counts as a delta row,
  and v1 OpenRouter spend as `spend_usd` under `fast`. All carry the task id.
  The result also shows what it cost.

**D7 Owner-only routes.** All three routes are owner-only. `owner_only` runs
first, before the task is looked up or the body read:

- `GET /tasks/{id}/diff/review` returns the result;
- `POST /tasks/{id}/diff/review` starts a review;
- `POST /tasks/{id}/diff/review/cancel` cancels one.

Agents, MCP and Discord use the API listener and cannot start or read a
review. No tool reaches these routes.

**D8 UI.** One button, "Quick review", sits in the Diff tab's header (the
toolbar). It is a plain `button`, so it takes PR #210's 4px
`--radius-btn` rectangle, not a pill, in Graphite colours.

- While a review runs, the button reads "Cancel", with a small spinner and
  the harness it is trying.
- Its hover help says what it does: a quick, read-only review that catches
  small bugs, not a full one.
- Severity colours keep Graphite's meanings single: bug red (an error),
  risk steel (`--badge-win`; amber means "pending" and is not used), nit
  muted.

## Review fixes (#219 review, 2026-10-15)

- **H1. The Claude reviewer is isolated.** `ClaudeProvider.start(...,
  isolated=True)` is set only by this module; every other session is
  unchanged. An isolated session works as follows:
  - **Settings.** It runs with `setting_sources=[]`, so it reads no user,
    project or local `settings.json`. Hooks, `env`, `apiKeyHelper` and
    `enabledPlugins` from the worktree are therefore never read.
  - **CLAUDE.md.** It loads no CLAUDE.md. CLI 2.1.295 gates CLAUDE.md
    discovery on the same setting sources, and
    `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` turns it off as well.
  - **Login.** The OAuth credentials file is not a setting source, so the
    login still works. The empty list was chosen over `["user"]`, which
    would bring back the user's own hooks and plugins.
  - **Environment.** The SDK can only add to the inherited environment, so
    every variable outside a small keep-list is set to `""`. Jarvis's
    `.env` keys (`OPENROUTER_API_KEY` and the like) and `ANTHROPIC_*` are
    blanked this way. The keep-list is `PATH`, `HOME`, user and locale,
    `TMPDIR`, `CLAUDE_CONFIG_DIR`, the proxy and CA variables, `LC_*`,
    `XDG_*` and `WSL*`.
  - **Limit.** The CLI authenticates with the subscription login only. An
    owner who used `ANTHROPIC_API_KEY` instead would get a failed Claude
    review that falls through to Codex.
- **M1.** `one_shot(cancel=)` and the 60 s cap, as in D6 above.
- **M2.** Codex usage is recorded in a `finally`, so a failed or cancelled
  turn's tokens still reach the ledger.
- **L1.** v1 reads are confined to the worktree, as in D3 above.
- **L2. A Claude `Grep` that could read a credential file is refused.**
  - The rule applies when the search root holds a credential file (a `.env`,
    a key file, anything `secrets.is_credential` names) and no `glob` keeps
    it out.
  - A negated glob (`!x`) never counts as keeping it out, since it selects
    everything else.
  - Brace lists are expanded first.
  - The walk skips `.git`. It is bounded to 20,000 entries and 1 s, and a
    walk that hits the bound is refused.
  - **Why refuse rather than rewrite the input:** the hook can allow or deny
    a call but not change its arguments. Jarvis also never sees Claude's
    tool results, so nothing can be scrubbed afterwards. The prompt tells
    the reviewer to Grep a folder or pass a glob such as `*.py`.
  - **Cost:** a Grep of the whole worktree with no glob, or with a glob that
    could select a credential name, is refused while any `.env` sits in it.
    A gitignored `.env` counts too: the walk does not read `.gitignore`, so
    the guard does not depend on ripgrep's ignore rules.
  - **Delta review (L-a, L-b).** Globs are now read the way ripgrep reads
    them (gitignore-style):
    - A glob with no `/` matches the name at any depth.
    - A leading `**/` is also tried bare.
    - A `/**/` also matches zero folders.
    - `*`, `**` and `**/*` select every file, dotfiles included.

    So `**/*`, `**/*.production`, `**/.env*` and `sub/**` are refused when a
    root or `sub` `.env*` exists.

    A glob whose every variant is `*.<source extension>` (`*.py`, `*.ts`,
    `src/**/*.{py,ts}`) skips the walk, so a Grep in a large repository stays
    usable. The stated limit: such a glob could still select a file literally
    named `.env.py`, which the guard does not look for.

    The walk bound (20,000 entries, 1 s) is pinned by a test.
- **L3. Only Luna's Codex limits count.** The Codex reading is the
  account's default `codex` limit, plus any limit whose id or name names
  the review's model (`gpt6luna`, compared without punctuation). Another
  model's limit never skips Luna.
- **L4.** A failed poll no longer stops polling. It retries with backoff:
  1.5 s, doubling, up to 15 s. The panel says it is retrying.
- **NIT.** Brace lists in a Glob pattern are expanded before the `..` and
  absolute-path checks. One too large to expand is refused.

## Files

- `jarvis/v2/diff_review.py` (new): input building and the cap, fencing, the
  prompt, parsing findings, quota checks, the three harness runners, the
  per-task job manager with cancel and timeout, and persistence.
- `jarvis/v2/hud_api.py`:
  - `_diff_parts` is factored out of `diff()`, and `diff()` is unchanged on
    the wire;
  - the three routes are mounted before the task GET block.
- `jarvis/v2/providers/codex.py`: `one_shot(..., on_usage=None)` reports the
  turn's `thread/tokenUsage/updated` counts to the caller. Nothing else
  changes.
- `jarvis/v2/daemon.py`: `stop()` cancels running reviews.
- `jarvis/config.py`: the three model names (env-overridable) and nothing
  else.
- `hud/src/api.ts`, `hud/src/types.ts`: the routes and the record type.
- `hud/src/lib/diffreview.ts` (new): pure helpers for the header line,
  ordering, clickability and the scope sentence.
- `hud/src/components/DiffReview.tsx` (new): the button, the panel and
  polling.
- `hud/src/components/DiffTab.tsx`: mounts it and opens a finding's
  file:line.
- `hud/src/components/Editor.tsx`: `DiffEditor` takes an optional `reveal`
  line.
- `hud/src/theme.css`: `.qreview*` rules.
- `tests/v2/diff_review_check.py` (new): routes, owner-only, selection
  matrix, read-only toolsets per harness, fencing, cap, parse, persistence,
  ledger.
- `hud/src/components/DiffReview.test.ts`, `hud/src/lib/diffreview.test.ts`
  (new): vitest.
- `tests/face/hud_v2_mock.py`: the mock serves the review routes, for
  screenshots.
- `docs/hud-api.md`, `docs/notes/editor.md`: the contract and a note.

## Not done here

- No review for the project-uncommitted Diff view (`ProjectDiff`). D1 scopes
  the review to a task's diff.
- Codex reads no project files (see D3).
- No live run. Every test uses fakes. A paid probe under 2 cents is allowed,
  but none was needed to build this.
