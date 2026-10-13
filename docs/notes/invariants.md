# Invariants — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 4780–5018

<!-- claude-source:004780-005018:begin -->
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
   fire across turns (or at an image-carrier boundary). **Except under
   steering** (v2's fast path, 2026-10-08): `Agent.take_steering` appends an
   `[owner steering]` user message at a step boundary, mid-turn, and that is
   a legal cut point, so compaction can now cut *inside* a turn. Still safe for
   the same reason as everywhere: the steer lands only where every tool_call
   already has its result, and `find_cut_point` walks the pending ids anyway,
   so the cut never orphans one; `messages[1]` stays pinned. What it can cost
   is the turn's own request being summarised while the turn still runs.

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

7. **Durable state is rebuilt from source every step, and it lives in two
   places for two different reasons** (split 2026-08-09; it was all
   `messages[0]` before).

   `messages[0]` holds the **stable** half: the base prompt with the avatar
   rename applied. It changes only when the owner changes avatar.

   The **volatile** half — skills index, working plan, recent-session titles —
   is a `user` message carrying `CONTEXT_BLOCK_PREFIX`, lifted to the **tail**
   of the transcript before every request. `agent.working_context(messages)`
   finds it; `agent.durable_state(messages)` is both halves together, and is
   what tests should assert against rather than an index.

   **Why it moved: prompt caching.** Providers cache on a shared prefix, so a
   single `plan_write` at step 3 rewrote byte 0 and made every later step
   re-pay for the entire transcript. At the tail, the same write invalidates
   only what follows it — which is nothing. This is exactly why Claude Code
   puts its volatile state in `<system-reminder>` blocks on the latest user
   message rather than in the system prompt.

   **It is still prune-proof, for a better reason.** Index 0 was safe because
   nothing in `context.py` touches it; the block is safe because it is *rebuilt
   from source every step*, so a compaction that eats it costs nothing — the
   next step writes it again. Rebuilt per **step**, not per turn: a 40-step
   turn passes the top of `run_turn` once, and step 40 is exactly when the plan
   matters.

   Three things that fall out and are pinned by `tests/longhorizon_check.py`:
   the block is matched by **identity**, not equality, when it is lifted (two
   agents can hold blocks with identical text, and `list.remove` would take the
   wrong one); there is never more than **one** of it, within a turn or across
   turns; and it is **dropped before `session.record()`**, because persisting
   it would freeze one run's plan and skills index into the file and resume
   into a stale copy of both — the same reasoning that already kept
   `messages[0]` out of the save.

   Unchanged: an agent without `skill_read` never sees the skills index and one
   without `plan_write` never sees the plan block. Never describe a tool the
   model cannot call.

   **That now holds for the system prompt too** (2026-10-11, the owner's
   answer 5 on #34's follow-up). `config.SYSTEM_PROMPT` is written for the
   full conversation toolset and names plan_write, run_subagent, task_start,
   write_file and the rest, which the fast path, workflows, tasks, goals and
   the benches do not hold. Since #34 each such mention costs a refused step.
   `config.PROMPT_TOOL_PARAGRAPHS` lists every paragraph that names a tool,
   keyed by its opening words, with the versions it may take.
   `config.narrow_prompt(text, names)` picks, per paragraph, the first version
   whose tools are all held, and drops the paragraph when none is
   (`config.system_prompt(names)` is it applied to SYSTEM_PROMPT).
   `Agent.__init__` applies it to the whole of any prompt that starts with
   SYSTEM_PROMPT — a surface's own paragraphs after it included, so a
   surface paragraph that names tools gets a rule in the same table (the
   workflow's "For a change to a repository" paragraph, which agent-bench's
   sandboxed workflows hold no git tools for) — and no surface needs code of
   its own. "The browser" counts as a tool (`browser_goto` and
   `browser_snapshot`): its paragraph and the fence's "and the browser" are
   narrowed for agents without it, its conduct rules kept. The narrower
   versions are the original's own sentences, with a sentence or a tool's
   name taken out. With every tool held, the result is the text byte for byte
   (the wording is tuned). A paragraph that names a tool but has no rule fails
   `tests/prompt_tools_check.py`, so **adding a tool name to the prompt means
   adding its rule** — and the narrower versions are copies, held to their
   original sentence by sentence there, so **a change to a paragraph that has
   them is a change to each of them** (or the check fails, naming the
   sentence). Sub-agent types do the same through
   `AgentType.needs`: a child that lost a tool its brief names to the parent
   intersection is not sent, and says which. The case was a builder under
   long-bench, which holds no git tools.

   Consequence worth knowing: the block is a real message, so **anything
   counting messages must exclude it** — `tests/longhorizon_check.py` has a
   `_n()` helper for exactly this, because scripts branching on
   `len(messages)` silently took the wrong branch when it appeared.

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

10. **A deferred tool group is absent from the request, and `tool_names=None`
   no longer means the whole registry** (`tools.ToolGroup`, 2026-08-21).

   Every registered tool's JSON schema is sent on **every** request, and the
   registry costs ~11k tokens before a word of conversation. That is the right
   trade for tools any turn might need and the wrong one for an integration
   that is idle in most conversations and *absent entirely* on a machine that
   never connected it. So a group is the skills index moved down a level: a
   one-line pointer in the working-context block, and the bulk on `load_tools`.

   Three states, and the first is the one that pays. **Unavailable** — the
   owner never ran the auth command — costs nothing at all: no schemas, and no
   pointer advertising a service that would only answer with a setup error.
   **Available** offers `core` only, which must be small but not empty: a round
   trip spent expanding a toolset is a round trip the owner waits through, and
   on the voice surface that is the whole latency budget. **Loaded** is
   everything, for the life of that agent. Measured on Spotify: 0 / 525 / 1318
   tokens per request, against 1318 unconditionally.

   `Agent.__init__` is where this binds: `tool_names=None` now resolves to
   `tools.default_names()`, not `REGISTRY`. **Anything that builds a toolset by
   iterating `tools.REGISTRY` silently re-includes the deferred half** —
   `tasks.task_tool_names` and `goalrunner.goal_tool_names` were both doing
   exactly that and were changed with it. That is the failure mode to watch
   for: it is not an error, it is the mechanism quietly not working.

   Three properties hold it together:

   - **The set is mutable and per-agent, reached through `runtime.py`** — the
     working plan's mechanism, for the working plan's reason. `load_tools`
     writes into a set the agent owns; `_sync_tools` folds it in on the next
     step and **re-binds `tool_names`**, so a sub-agent spawned after an
     expansion inherits it and one spawned before does not retroactively gain
     it. Expanding costs a one-time prefix-cache miss (tools are part of the
     cached prefix — invariant 7's reasoning, applied to the other end of the
     request), which is why `core` exists at all.
   - **An agent handed an explicit toolset cannot widen itself.** `loadable()`
     requires the group's core tools to already be in the agent's own set, the
     same intersection rule that stops a workflow reaching the browser through
     a child — and since 2026-10-10 that is enforced, not only advertised:
     `load_tools` refuses a group whose core the caller does not hold, and
     `_sync_tools` folds in no such group however it reached `loaded_groups`.
     It had to be: the set `_sync_tools` re-binds is the one dispatch enforces.
   - **The toolset is enforced at dispatch** (2026-10-10; see *A toolset is a
     boundary* in Safety design). The bound `tool_names` is the agent's
     *current* toolset, expansions included, and `dispatch` refuses any tool
     outside it — as text, before the arguments are read. A deferred tool the
     agent *could* load (it holds `load_tools` and the group's core) gets the
     pointer to `load_tools`, because a model guessing a deferred tool's *name*
     has guessed its *arguments* too; anything else gets "not available to
     this agent". This paragraph used to say the opposite — "dispatch has
     always let any agent call any registered tool by name (the approver is
     the actual gate)" — which was only true while every tool that mattered
     to a boundary was `dangerous`, and it never was. **Unbound refuses**
     (2026-10-11; it failed open until then): no real path dispatches unbound
     (`tests/dispatch_toolset_check.py` walks them), so an unbound dispatch is
     a bug and runs nothing. Scripts and tests opt in with
     `runtime.tools_bound(names)` or `runtime.unconfined()`. A group with an
     **empty core** is refused by `register_group`, `load_tools` and
     `_sync_tools` alike — an empty core is "held" by every agent.

<!-- claude-source:004780-005018:end -->
