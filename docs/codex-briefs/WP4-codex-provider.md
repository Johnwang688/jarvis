# WP4 — CodexProvider (app-server), lifted from the trading firm

You are Codex, implementing work package 4 of the Jarvis v2 redesign, in a
git worktree on branch `jarvis/wp4-codex-provider`. Claude leads; this brief
is your entire scope. Read first, in this order:

1. `docs/jarvis-v2-design.md` — §2 D5/D6/D10, §5.1, §5.2, §5.4, §6 (the
   provider only *calls* the permission callback; the five layers live above
   it), §8.3 (what usage the ledger wants), §15 R4 and R7.
2. `jarvis/v2/provider.py` — **fixed**. `Provider`, `Brief`, `Event`,
   `EventKind`, `Decision`, `Usage`, `SessionHandle`, `UserMessage`,
   `PermissionCallback`, `BriefRefused`. `jarvis/v2/model.py` for `Thread`.
   Do not edit either; propose changes in your notes.
3. The trading firm, `/home/johnw/projects/jarvis-trading-firm` (read-only;
   never modify it): `docs/DESIGN-codex-executives.md`, `firm/codex_rpc.py`,
   `firm/codex_config.py`, `firm/codex_agent.py`, `firm/codex_retry.py`, and
   `tests/codex_rpc_check.py` / `tests/codex_agent_check.py` for the fake
   app-server pattern (`FakeRpc`, `Brain`).
4. `docs/codex-briefs/WP1-notes.md`, `WP6-notes.md` for house conventions.

## What to lift, and what to change

Lift the **transport, config preparation, retry/cooldown and usage
accounting** ideas. Copy `codex_rpc.py` into
`jarvis/v2/providers/codex_rpc.py` with a header line crediting its origin;
adapt freely. Do **not** lift the firm's confinement policy: this provider
runs Codex with its **native tools on** (shell, file, web, MCP, skills), in
its own `workspace-write` sandbox, with approval requests **routed to the
callback** instead of denied. The firm disables all of that because nobody
is awake there; here the owner is reachable.

## Deliverable: `jarvis/v2/providers/codex.py`

`class CodexProvider` implementing `provider.Provider` with
`name = ProviderName.CODEX`.

- `health()`: `codex` on PATH; version equals the pin `CODEX_PIN = "0.153.4"`
  (a constant in the module; mismatch → `(False, "codex X, pinned Y")`);
  `codex login status` reports a ChatGPT login. Never spends tokens.
- **Private config per thread**, the firm's `codex_config.prepare` idea: a
  directory under `config.V2_DATA_DIR / "codex" / <thread_id>` with a
  `config.toml` generated from the `Brief` (model, `model_reasoning_effort`,
  `sandbox_mode = "workspace-write"`, `approval_policy = "on-request"`,
  `approvals_reviewer = "auto_review"`, MCP servers from
  `brief.mcp_servers`, the worktree as the trusted project) and a link to
  the owner's existing login exactly as the firm does it. Forward **no**
  `OPENROUTER_API_KEY` or other Jarvis secrets in the environment. Refuse
  API-key billing. `brief.profile == STRICT` → raise `BriefRefused` (strict
  is a later package).
- `start`: `thread/start` in `brief.cwd`, `baseInstructions` **appended**
  to Codex's own defaults (not replacing them — AGENTS.md and skills must
  still load) with `brief.system_append`. Store `codex:<thread id>` in
  `SessionHandle.provider_session_id` and return the handle. `resume`:
  `thread/resume` with `excludeTurns=true` from `thread.provider_session_id`.
- `send`: `turn/start` with the message text (and images if the protocol
  takes them; otherwise note it), then yield `Event`s until the turn ends,
  translating the app-server stream to §5.2 kinds: agent message deltas →
  `TEXT_DELTA`, final agent message → `TEXT`, reasoning → `THINKING`,
  command execution / file change items begin/end → `TOOL_STARTED` /
  `TOOL_FINISHED` (name `"shell"` or `"apply_patch"`, `args` carrying the
  command or the paths, `summary` the exit code or the diff stat), plan
  updates → `PLAN_UPDATED`, token usage → `USAGE`, turn completed →
  `TURN_FINISHED` with `stop` mapped from the protocol's status. Yield
  `ERROR` with `fatal=True` on transport failure, then stop.
- **Server requests**: `item/commandExecution/requestApproval` and
  `item/fileChange/requestApproval` → call `permit(tool_name, args, brief)`
  on the *send* thread (the callback may block for minutes while an owner
  is asked; the RPC read loop must keep draining meanwhile) and answer the
  request with the mapped decision; also yield `APPROVAL_REQUESTED` before
  and `APPROVAL_RESOLVED` after. `item/permissions/requestApproval` → deny
  (an agent may not widen its own sandbox). `requestUserInput` (both
  spellings the firm handles) → yield `QUESTION` and block until `answer()`
  is called with the text. **Any other server request fails closed** with
  an error reply and an `ERROR` event, the firm's rule.
- `interrupt`: `turn/interrupt`. `answer`: resolves a pending approval or
  question by `req_id`; unknown id → `ValueError`. `close`: the firm's
  bounded SIGINT/TERM/KILL shutdown.
- `usage()`: a `Usage` with input/output/cached from the cumulative
  counters, using the firm's offset rules (resume may replay, counters may
  reset in a new process, decreasing counters fail closed). `cost_usd=None`
  always: a subscription reports no dollars.
- **Shared login serialization**: a process-level lock so two sessions'
  turns do not race the shared auth refresh (firm rule). Two sessions may
  be *open* concurrently; their `send` calls take turns.

## Tests: `tests/v2/codex_provider_check.py`

Free, no network, no real `codex`: a fake app-server in the `FakeRpc` /
`Brain` shape, driven over real pipes so the transport is exercised. Cover:
health on missing binary / wrong version / no login (patch `subprocess`);
config.toml content (native tools on, sandbox, reviewer, MCP servers, no
Jarvis secrets in env, strict refused); start/resume shapes; the full event
translation for a scripted turn (assert the exact `EventKind` sequence and
`data` conventions); an approval request blocking on a callback that
answers late while notifications keep flowing; deny path; the permissions
request denied without calling the callback; `requestUserInput` → QUESTION
→ `answer()` → turn continues; an unknown server request failing closed;
interrupt mid-turn; usage offsets across a resume; two sessions serialized;
transport death yielding `ERROR(fatal)` and leaving no stuck threads.

Also write `tests/spikes/r4_codex_live.py`: a **live** one-turn smoke the
owner runs by hand (prints "live; spends quota"): health, start in a temp
dir, one turn that runs `echo ok` and writes a file, prints the event kinds
seen, whether an approval request arrived, and the usage.

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/codex_provider_check.py`

## Rules

Stay inside `jarvis/v2/providers/` (create `__init__.py`), `tests/v2/`,
`tests/spikes/r4_codex_live.py`, and `docs/codex-briefs/WP4-notes.md`. Do
not edit `model.py`, `provider.py`, `stores.py`, `worktrees.py`, v1 code,
the firm, or CLAUDE.md. No new dependencies (stdlib only, as the firm). When
green, commit on this branch with a message starting `v2 WP4: CodexProvider`
ending with `Co-Authored-By: Codex <noreply@openai.com>`. Do not push.
Finish with `WP4-notes.md` (under 80 lines): what you built, protocol facts
you verified against the pinned client, the test command and its last lines,
workarounds, proposed interface changes.
