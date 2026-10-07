# Plan: choosing the model for a thread (Jarvis v2 HUD)

_Planner's proposal, kept as written. **The owner's decisions in `2026-10-06-decisions.md` override it wherever they differ.** Problem 1 below was re-checked in the code by the lead session: the HUD sends `{id}` to `/model`, `{name}` to `/voice` and `{mute}` to `/mute`, while the daemon reads `model`, `voice` and `muted`._

## In short

- **What it does:** a `model ▾` chip sits next to `in: <project> ▾` in the input bar. It picks a model for this one thread from the owner's roster, plus an optional reasoning effort.
- **Default:** a thread left on **default** follows the global Model picker on every turn.
- **Pinned:** a thread set to a named model stays on it until changed again.
- **When it can change:** while composing, the choice goes out with `POST /threads`. After the first message it goes through `PATCH /threads/{id}`.
- **Scope:** fast-path chat threads only. Claude and Codex chat threads are not part of this feature (decision 1).

## Problems found in the current code

1. **Clicking a model in the global Model picker resets it to the config default.**
   - `hud/src/api.ts` `setModel` posts `{id, effort}` to `/model`.
   - The daemon (`jarvis/v2/hud_api.py` `pickers`) reads `body.get("model", "")`, so every click calls `models.select("")`, which means "go back to the config default".
   - The effort sent to `/model` is ignored. The real effort route is `POST /models {model, effort}`.
   - The test mock (`tests/face/hud_v2_mock.py`) accepts `id`, so the suite passes.
   - The same mismatch hits `/voice`: the HUD sends `{name}` and the daemon reads `voice`, so the voice override is always cleared.
   - It also hits `/mute`: the HUD sends `{mute}` and the daemon reads `muted`, so the server's mute state is never set.
2. **In v2, a global model change never reaches a thread that is already open.**
   - `FastPathProvider._open` builds `Agent(model=brief.model or models.tier("orchestrator"))` once, when the session opens.
   - v1's face server reassigned `_agent.model` after `/model`. Nothing in `jarvis/v2` does.
   - So an open default thread stays on its old model until the daemon restarts.
3. **The model in a `POST /threads` brief is not checked at all.** Any string reaches OpenRouter and fails one turn later.
4. **A plain `ValueError` from a route reaches the client as `request failed (ValueError)`.** Refusing a model loudly therefore means raising `APIError(400, <reason>)`.

## Answers to the seven questions

### 1. Scope
- **Recommendation: the model within the fast path only, chosen from the roster shortlist, with optional effort.**
- **No provider choice.** A chat thread on Claude or Codex would be a full agent that can edit files and run commands, working in the project root at `auto`, with no task gate, worktree or reviewer. That goes against §8.1 and §12.1.
- **Claude models are still reachable.** Anthropic models can be added to the roster from the OpenRouter catalog and then chosen for a fast-path thread.
- **Roster only, not the full catalog.** This follows `models.select`. The chip's last entry, "manage roster…", opens the existing Model picker.
- **Effort fits**, with one rule: an effort can be chosen only together with a named model, and changing the model clears it.

### 2. When it can change
- **While composing:** the choice is kept in the HUD only (`Compose.model/effort`). It goes to the server as `brief: {model, effort}` on the first `POST /threads`, and nothing is sent before that. A default choice leaves `model` out of the body.
- **After the first message, fast threads:** allowed, at no cost. The model is read at the start of each turn. A change made during a turn applies from the next message, never in the middle of one.
- **Claude, Codex and task threads:** refused with 409. Their model is set by routing when the session starts.
- **The "brief frozen at open" rule:**
  - `brief.json` is never rewritten. That rule protects where a thread works and under which rules (cwd, profile, always_ask).
  - Which model answers belongs to the next request, not to the conversation.
  - So the **Thread record** (`thread.json`, `Thread.model` and `Thread.effort`, which already exist and are copied from the brief at open) becomes where the current choice is kept. `brief.model` only seeds it.
  - When the fast path opens a session it seeds its choice from `thread.model` and `thread.effort`, not from the brief, so resume picks up a changed model without touching `brief.json`.
- **New route:** `PATCH /threads/{id}` gains `model` and `effort`, any subset with `project_id`, and at least one field. Under `daemon._lock` it:
  - saves the stored Thread;
  - updates the live `session.thread`, as the move code already does;
  - calls `provider.set_model(handle, model, effort)` if the provider has that method.

  A PATCH during a running turn is safe.

### 3. The default
- **Default** means `Thread.model = null`. The model is `models.tier("orchestrator")`, worked out at the start of every turn, so it follows the global picker even on open threads (fixing problem 2).
- With no effort chosen, effort comes from `models.effort_for(model)`.
- **The UI shows it as** `model: default · gpt-5.6-luna`. The name after the dot is re-read on the SSE `model` event.
- **A pinned thread shows** `model: kimi-k3 · high`.
- **A pinned model that has since left the roster stays pinned** and shows as `kimi-k3 (not on roster)`.

### 4. UI
- **The control:**
  - a `ModelChip` in `InputBar.tsx`, next to the project chip, using the same `.chip.projchip` style;
  - a native `<select>`, so option labels can only be text;
  - options: "default · <current>", then each roster model shown as its short id plus price (`$in/$out per M`, "free" when zero), with a "(not on roster)" row if needed, then "manage roster…";
  - a second, small select for effort, shown only when a model is pinned and that model has effort levels.
- **When it can be edited:** while composing, and on any fast-path chat thread. On task threads and Claude or Codex threads it is read-only.
- **Keyboard:** on both selects, `onKeyDown` and `onKeyUp` call `stopPropagation()`, so Space never starts push-to-talk. Both are disabled while an approval is pending.
- **Feedback:** the tooltip says "applies from the next message". A refused change reverts the chip and shows `Could not change model: <backend message>`.
- **Sidebar:** a thread row gets a small model badge only when pinned. The row tooltip says "runs on X (pinned)" or "follows default".
- **Chat:** a mid-thread change writes a `model_set` record to the thread's log. The transcript shows it as a system line, "model → kimi-k3 · high (from the next message)".
- **Safety of names:** model names come off the network and are rendered only as React text and `<option>` text. The mock's `evil/model` (an `<img onerror>` name) must render as literal text.

### 5. Backend changes
- **`jarvis/models.py`:**
  - pull the effort checks out of `set_effort` into `check_effort(model_id, level)`;
  - add `check_pin(model, effort) -> (model|None, effort|None)`: the model must be on the roster, an effort needs a model, empty strings become None;
  - `set_effort` reuses `check_effort`.
- **`jarvis/agent.py`:** an optional `effort: str | None = None` setting. Both `llm.chat` calls use `self.effort or models.effort_for(self.model)`. With `None`, v1 behaves exactly as now.
- **`jarvis/v2/providers/fastpath.py`:**
  - `_Native` gains `model` and `effort`, seeded in `_open` from `thread.model`/`thread.effort`, falling back to the brief;
  - `_run` sets `agent.model = native.model or models.tier("orchestrator")` and `agent.effort = native.effort` at the start of each turn;
  - new `set_model(h, model, effort)` stores the values only;
  - add `model` and `effort` to the `usage` event's `provider_reported`, so each turn records which model answered;
  - update the docstring and design §8.1.
- **`jarvis/v2/daemon.py`:**
  - `open_thread` runs `check_pin` when the provider is FAST, before `stores.threads.create`, turning any refusal into `APIError(400, msg)`;
  - new `Daemon.set_thread_model(thread_id, model, effort)` does the locked update, the provider call, the `model_set` log record, and publishes `thread_updated {thread_id, model, effort}`.
- **`jarvis/v2/hud_api.py`:**
  - `PATCH /threads/{id}` accepts `("project_id", "model", "effort")` with at least one present. Task threads and non-fast threads get 409.
  - The transcript endpoint maps `model_set` records to `role: "system"`.
- **`docs/hud-api.md`:** document all of the above, plus the correct body keys for `/model`, `/models`, `/voice` and `/mute`.
- **Mock (`tests/face/hud_v2_mock.py`):**
  - match the real daemon's body keys;
  - `/models` returns `current` and `default`, with prices and effective effort;
  - `POST /threads` stores `brief.model/effort`;
  - `PATCH /threads/{id}` handles model and effort with the real refusals and emits `thread_updated`.

### 6. Tests (each fails against the current code)

**Vitest: new `hud/src/lib/threadmodel.ts` and `threadmodel.test.ts`.** Pure helpers:
- `shortId`;
- `modelLabel`, which gives the default, pinned and "(not on roster)" labels;
- `modelOptions`, which keeps an off-roster pinned model;
- `effortOptions`;
- `applyChoice`, where changing the model clears the effort;
- `threadBody(compose)`;
- `chipEditable(thread)`.

**Headless: new `thread_model_checks` in `tests/face/hud_v2_check.py`.**
1. The compose chip reads "default · gpt-5.6-luna".
2. Choosing a model while composing makes no server call.
3. Sending makes `POST /threads` carry `brief.model`, and a default send carries no model.
4. After the first message, a change sends `PATCH /threads/{id}` with `{model}`.
5. Choosing default sends `{model: null}`.
6. A refused PATCH reverts the chip and shows the error.
7. The markup-carrying name renders as text.
8. Space on the focused chip does not start push-to-talk.
9. A task thread shows a read-only chip.
10. A pinned thread's sidebar row shows the badge.

Fix the existing `picker_checks` so it asserts:
- clicking a row posts `/model` with `{"model": id}`;
- choosing an effort posts `/models` with `{model, effort}`;
- the voice picker sends `{"voice": ""}`.

**Backend: `tests/v2/hud_backend_check.py`.**
- `POST /threads` with a roster model: the record carries it. An off-roster model gets a 400 naming it, and no thread record is left behind. An invalid effort gets a 400.
- PATCH model: 200; `thread.json` is updated; `brief.json` is byte-identical; `session.thread.model` is updated; `set_model` was called; `thread_updated` is published; the transcript has the system line.
- PATCH during a running turn: after it settles, the saved Thread still has the new model.
- After `close_thread` and a resend, `resume` received the new model.
- Claude or Codex thread: 409. Task thread: 409. `{}`: 400.

**`tests/v2/fastpath_check.py`:**
- A default handle follows `models.select` between turns. This fails today.
- A pinned handle ignores a global change.
- `set_model` during turn 1 leaves every call in turn 1 on the old model, and turn 2 uses the new one.
- An effort pin reaches `llm.chat`.
- `_open` seeds from `thread.model`.
- Guard: no fast-path or MCP tool can set a thread's model.

**`tests/models_check.py`:** `check_pin` refuses off-roster models and invalid efforts, and `set_effort` behaves as before.

### 7. Risks
- **Cost surprises.** A pricier model pinned on a long-lived thread keeps costing money without anyone noticing. Mitigations:
  - the chip is always visible and shows the price;
  - the transcript records each switch;
  - the fast path's $5/day allowance still applies.
- **What doesn't follow the thread's model.** The fast path cannot spawn sub-agents. Compaction and session summaries follow the global choice. A `task_propose` opens a task routed to Claude or Codex.
- **Bench comparability.**
  - v1 benches are unaffected (`Agent.effort` defaults to None).
  - Fixing problem 2 means a default thread now switches model mid-conversation if the global picker changes.
  - v2 benches must pin model and effort.
- **Discord.** A pin applies to Discord turns on the same thread, and Discord has no control to change it, by design.

## Implementation order
1. **Prerequisite commit:** fix the HUD body keys for `/model`, `/models`, `/voice` and `/mute`, update the mock to match the real daemon, and fix `picker_checks`. Small and independent.
2. **Shared validation:** `models.check_effort` and `check_pin`.
3. **Fast path:** per-turn model and effort, `set_model`, `provider_reported.model`.
4. **Daemon and route:** `open_thread` checks, `set_thread_model`, PATCH and transcript.
5. **HUD:** `threadmodel.ts`, `ModelChip`, compose model and effort, `threadBody`, the PATCH handler, the `thread_updated` event, the sidebar badge, theme spacing, headless checks.
6. **Docs:** `hud-api.md`, design §8.1, §12.1, §18, and CLAUDE.md.

## How to verify
- `cd hud && npx vitest run` passes, including `threadmodel.test.ts`.
- `npm run build`, then `python tests/face/hud_v2_check.py`.
- `python tests/v2/hud_backend_check.py`, `python tests/v2/fastpath_check.py`, `python tests/models_check.py`, plus `daemon_check`, `stores_check`, `context_check` and `fleet_check`.
- **Live:**
  - (a) In the global picker, choose model B; `models.json` `selected` changes. That proves problem 1 is fixed.
  - (b) An open default thread answers on B without a restart. That proves problem 2 is fixed.
  - (c) Pin model C on a thread; a global change doesn't move it.
  - (d) Restart the daemon: still C, and `brief.json` is unchanged.
  - (e) An off-roster model gets a 400 naming it.

## Decisions for you
1. **Provider choice per chat thread (Claude or Codex chat)?** I recommend **not now**.
2. **Which models the chip offers.** I recommend **roster only**, with "manage roster…" leading to the full catalog.
3. **A pinned model later removed from the roster.** I recommend **keep it pinned and visible**.
4. **Effort per thread**, allowed only with a named model and cleared when the model changes? I recommend **yes**.
5. **Should a new thread inherit the previous thread's model?** I recommend **no**.
6. **Fix the `/voice` and `/mute` body-key bugs in the same prerequisite commit?** I recommend **yes**.
7. **Mark each change in the chat?** I recommend **yes**.

## Overlap with the projects feature
Shared files: `App.tsx`, `api.ts`, `types.ts`, `Sidebar.tsx`, `InputBar.tsx`, `Pickers.tsx`, `lib/compose.ts`, `theme.css`, `daemon.py`, `hud_api.py` (`PATCH /threads/{id}`), the docs, the mock and both test suites.

Suggested order: the picker-key fix first (tiny), then the projects feature, then this one rebased on top.
