# Plan: Compact button for HUD chats (2026-10-19)

The owner asked for "a small compact button to compact the context… the same compact process as
claude code's /compact." Each provider runs its own native compaction. The transcript keeps every
row; only the model's context gets smaller. Planner: Opus (Plan agent); lead-reviewed.

## 1. Provider interface (narrow and optional)

Two optional methods go in the `Provider` protocol comment block (`jarvis/v2/provider.py:266-322`),
after `adopt`, following the `subagent_stop_refusal`/`stop_subagent` pair:

- `compact_refusal(h) -> str | None`: a cheap check that sends nothing. It returns one plain
  sentence when `compact` would not run.
- `compact(h) -> Iterator[Event]`: a turn with no message. It yields, in order:
  - `TURN_STARTED`;
  - a `TOOL_STARTED`/`TOOL_FINISHED` pair named `compact` (Claude already emits this);
  - any `USAGE`;
  - `TURN_FINISHED {"stop": "end"|"error"|"interrupted", "compacted": bool, "reason": str|None}`.

  `interrupt(h)` ends it.

If a provider has no `compact` method, the daemon refuses with: "This chat's provider cannot
compact its conversation."

**Claude** (`providers/claude.py`)
- `compact(h)` copies `adopt` (2062) and `_run_cli` (2081). In order, it:
  1. acquires `session.sending` without blocking, refusing if busy;
  2. calls `_take_stream`;
  3. clears `session.interrupted`;
  4. runs the existing `_compact(session, events)` (1781);
  5. calls `_end_foreground` and `_give_stream`;
  6. puts a `TURN_FINISHED`.
- It reuses `COMPACT_TIMEOUT_S`, `INTERRUPT_POLL_S` and the injected-result skip already in `_compact`.
- `compact_refusal` returns a sentence when the session is closed or `offer` is pending.
- Usage and cost flow through `_take`, which already counts a /compact turn's result once
  (`docs/notes/providers-runtime.md:246`).

**Codex** (`providers/codex.py`)
- `thread/compact/start {threadId}` returns `{}`, with no turn id. Per the 0.161.0 schema,
  `ThreadCompactStartParams` holds only `threadId`, and `thread/compacted` is deprecated in favour
  of the `contextCompaction` ThreadItem.
- Factor `send`'s loop (1110-1386) into `_drive(h, method, params, *, compaction)`. In compaction
  mode:
  - learn `s.turn_id` from the first `turn/started` on our `threadId`;
  - treat a `contextCompaction` item (or the deprecated `thread/compacted`) as success;
  - do not raise "completed without token usage";
  - keep the reader handshake, `_auth_lock`, accounting and cooldown.
- A `-32601` answer (`_NO_METHOD`, :106) sets `s.compact_unsupported`, a sibling of
  `steer_unsupported` (:639). The turn ends `error` with "This Codex has no thread/compact/start;
  update Codex to compact." From then on, `compact_refusal` returns that sentence without sending
  anything.
- `interrupt` (2438) is unchanged once `s.turn_id` is known. A Stop before `turn/started` sets
  `cancelled`, and the loop interrupts as soon as it learns the id (as with the start race at 1197).

**Fast path** (`providers/fastpath.py`, `jarvis/agent.py`)
- Factor `Agent.run_turn`'s `/compact` branch (`agent.py:578-592`) into
  `Agent.compact_now(cancelled=None) -> (removed, saved_tokens)`. `run_turn` keeps calling it,
  byte-identical, so the button and a typed /compact run the same process.
- `FastPathProvider.compact` acquires `native.busy`, clears `native.stop`, and runs on a worker:
  1. `copy = deepcopy(agent.messages)`;
  2. `context.manage(copy, policy, summarize=agent._summarize, force=True)`;
  3. only if `not native.stop.is_set()` and something was removed: `agent.messages[:] = copy`, then
     persist with a new `sessions.Session.save_messages(messages)`, which writes the messages file
     only and counts no turn.
- Stop cannot abort a summary call already in flight. Its result is thrown away, and the transcript
  stays byte-identical.
- Invariant 1 holds: the cut still comes from `find_cut_point` (`context.py:249`), and `compact`
  pins `messages[1]` (:296).
- When nothing was removed, the result is `compacted: false`, with the reason "Nothing to compact
  yet: the conversation is still short."
- `compact_context` stays in `FORBIDDEN_TOOLS` (`fastpath.py:247`).

## 2. Daemon integration (`jarvis/v2/daemon.py`)

**`_Session` (244):** gains `compacting: bool = False`.

**`Daemon.compact(thread_id) -> dict`** calls `_live_session`, which refuses archived threads and
resumes without calling a model. Then, under `_lock`, it refuses with a `DaemonError` (HTTP 409) when:
- `_sendable` refuses (closing, archived, or a stop pending);
- the thread is not a chat, or it has a `task_id`: "Tasks are not chats; only a chat compacts.";
- `worker` is set: "A turn is running; compact when it ends.";
- `_queues[tid]` or `chat_relay.pending(tid)` is non-empty: "Messages are waiting to run first.";
- `_maybe_cli(session, eager=True)` started a CLI turn;
- `_waiting_on_owner` is true;
- the provider has no `compact`, or its `compact_refusal` returns a sentence (that sentence is used).

Otherwise it calls `_begin_compact` and returns `{"status": "started", "turn_id"}`.

**`_begin_compact`** is modelled on `_begin_cli` (1540). It:
- clears `cancelled`, `owner_stopped`, `steered` and `questions`;
- mints a `turn_id`, sets `compacting=True`, clears the relay fields and calls `_retire_drive`;
- appends and publishes `{"kind": "compact_started", "compaction": true, ...}`;
- starts `_turn(session, None, compact=True)`.

It sets no desk and does no `_apply_choice` and no DriveTurn.

**`_turn` (1945)** uses `source = provider.compact(handle)`. Its `finally`, under the lock:
- clears `compacting` together with `worker`;
- appends and publishes `{"kind": "compacted", "compaction": true, "data": {"ok": bool, "text": ...}}`,
  where `text` is "Conversation compacted", "Compaction failed: <reason>" or "Compaction stopped";
- then drains as usual: the owner's queue first, then relays.

The reason goes through `clean_line(scrub_text(...), 200)`, as `_idle_error` (1484) does. No summary
text is logged: Claude's boundary and Codex's item carry none, and the fast path's summary stays in
its v1 session file, as today.

**Records and counters**
- `_record` (1758) stamps `record["compaction"] = True` while `compacting`.
- `_finish` (1902) does not count a compaction in `thread.turns`.
- `thread_json` (1109) gains a live `compacting` flag.
- `activity.py` marks compaction records seen, so a compaction never makes a chat unread.

**Queues and Stop**
- `deliver` (~1210) also queues when `session.compacting`, so an owner message never steers into a
  compaction.
- In `chat_relay._hand_over` (~498), the steer test gains `and not session.compacting`. Replies are
  held and drained afterwards.
- Stop: `interrupt` (2111) is unchanged.

**Consumers skip `compaction` records:** `runner.py:125`, `discord/mirror.py:929` and
`hud/src/lib/speech/controller.ts:414`. `"compacted"` joins `chat_reads._SYSTEM_KINDS`
(`chat_reads.py:88`).

## 3. Route

`POST /threads/{id}/compact` in `hud_api.route`, beside relays/withdraw (~2203):
- `owner_only(handler, daemon)` runs first, before the thread lookup and before any body is read;
- then an empty query and an empty body;
- `202` with `daemon.compact(id)`;
- `405` for any other method.

There is no MCP tool, `bridge.py` route, Discord command or CLI verb.

## 4. Transcript, events and HUD

**Transcript** (`hud_api.py:2297`)
- `compacted` is a `system` row.
- `compact_started` only anchors the turn.
- The `compact` tool row shows, with `tools=1`.

**Bus events:** `compact_started`, the usual `turn_started` and `turn_finished` (so the thinking line
and Stop work), and `compacted`. App.tsx handles `compacted` beside `case "cli_turn"` (1754).

**The button** goes in `InputBar.tsx`, in the lower `.row`, right after `props.modelChip` (~391).
- Props: `compact?: {state, title, onCompact} | null`.
- Element: `<button type="button" className="chip compactbtn" data-testid="compact"
  data-state="ready|running|off" aria-label="Compact the conversation">`.
- Label: "Compact", or "Compacting…" while it runs. Graphite chip styling (`theme.css`).
- App `renderChat` (3842) passes it only for an opened thread with role chat and no `task_id`.
- Disabled when `runningHere(chat) || chat.busy || workBlocked || thread.archived`.

**Titles**
- Claude: "Run Claude Code's /compact: it summarises the conversation so the model's context
  shrinks. Every message stays here."
- Codex: "Run Codex's own compaction…"
- Jarvis: "Summarise older messages (your first request is kept)…"
- Disabled: "A turn is running; compact when it ends." / "Answer the card first." / "This chat is
  archived."

**Click:** `api.compact(id)`, beside `interrupt` (`api.ts:141`). A 409 shows the daemon's sentence in
`pane-status`.

## 5. Typed "/compact <focus>"

| Provider | Cost |
|---|---|
| Claude | Cheap: `_command_prompt(f"/compact {focus}")` |
| Codex | Not supported: the params hold only `threadId` |
| Fast path | Moderate: a preface to `COMPACTION_PROMPT` |

Deferred. The route takes an empty body now, so `{"focus"}` can be added later without breaking it.
Unverified: a typed "/compact" in a Claude chat today probably reaches the model as plain content.

## 6. Work packages (each: test writer, then implementer)

**WP0, spike.** One live `thread/compact/start` on a throwaway two-turn Codex 0.161.0 thread, recorded
to `tests/v2/fixtures/compact/codex-0.161.0.jsonl`. It answers: does `turn/started` arrive, which item
types come back, is there `tokenUsage`, and what `turn/completed` status. The Codex fake replays this
recording.

**WP-A: daemon, route, fast path and Claude (M).** Tests go in `tests/v2/compact_check.py`, with a
real daemon on ephemeral ports, fake providers and a temporary HOME. Negative tests:
1. An API-listener POST, even with the HUD Origin, gets 403, and nothing starts.
2. A source guard: no `jarvis/tools`, `jarvis/v2/tools`, `bridge.py`, `mcp*.py` or `discord/` file
   references `/compact` or `.compact(`; `compact_context` stays forbidden.
3. A running turn gets 409, and the provider's `compact` is not called.
4. A pending approval on the thread gets 409.
5. Archived, task and closing threads get 409.
6. Stop during compaction: the turn ends `interrupted`, the record says "Compaction stopped", and the
   queue is dropped with `queue_cleared`.
7. An owner message during compaction is queued (`steer` is never called) and runs after.
8. A chat_relay message, and a reply, to a compacting chat are held, then run after the owner's queue.
9. A provider without `compact` gets 409 with the sentence.
10. Records carry `compaction: true`; the mirror, the runner and the titler ignore them.
11. The reason is scrubbed.

Fast path (`tests/v2/fastpath_check.py`, `tests/context_check.py`):
- On the transcript `assistant(c1,c2)·tool(c1)·user(image)·tool(c2)`, no `tool_call_id` is orphaned
  and `messages[1]` is unchanged.
- Stop during a slow fake `_summarize` leaves `messages` deep-equal to before.
- The `run_turn` "/compact" output is unchanged.

Claude (`tests/v2/claude_provider_check.py`, replay fake):
- a boundary means ok;
- a timeout means interrupted plus failed;
- Stop takes effect within `INTERRUPT_POLL_S`;
- a pending `offer` is refused.

**WP-B: Codex (M).** In `codex_provider_check.py`, with the fake app-server replaying the WP0 fixture:
- success;
- `-32601` gives a refusal sentence, after which `compact_refusal` sends nothing;
- Stop after `turn/started` sends `turn/interrupt`;
- a completion with no usage is not fatal;
- a steer during compaction is never reached.

**WP-C: HUD (S).**
- Vitest, `InputBar.test.ts`: the states, the titles, the test id, and that the button is absent for
  compose and for tasks.
- `tests/face/hud_v2_mock.py` gains the route.
- A face check: a click posts once; the button is disabled while running and under a card; the
  system line shows; the 409 sentence shows in `pane-status`.
- The owner-only test stays in WP-A, against the real daemon (`control-plane.md:176`).

## 7. Docs

- `providers-runtime.md` (near 164): `compact()` for each provider.
- `control-plane.md` (near 399): the owner-only route; not a tool.
- `hud-workspace.md`: the button, its states and its test ids.
- The `provider.py` protocol comment.
- A one-line rule in CLAUDE.md, under "Working on v2".

## 8. Risks

- Claude's idle reader (SV-6) racing the stream. Mitigated by `_take_stream` and the injected skip.
- The Codex sequence is unverified, so WP0 comes first.
- A fast-path summary that was stopped still costs money.
- A compaction's `turn_finished` could mark the chat unread; `activity.py` marks compaction records
  seen.

## 9. Open questions for the owner (defaults in brackets)

1. Should Stop during compaction also drop the messages queued behind it? [yes, Stop means stop]
2. Confirm before compacting? [no, like Claude Code]
3. Approve the WP0 Codex probe? [yes, one probe]
4. Post "Conversation compacted" to Discord? [no]
5. Typed "/compact <focus>"? [a later PR; Claude only at first]

## Owner answers (2026-10-19)

| # | Question | Answer |
|---|---|---|
| CB-1 | Stop during compaction | **Drop the queued messages** too (Stop means stop), as Stop does for any turn. |
| CB-2 | Confirm before compacting | **Confirm first.** A click on Compact shows an inline "Compact this conversation?" with Compact / Cancel in the pane, not a blocking `window.confirm`. Cancel, Escape, or a card arriving dismisses it, and nothing is sent. Only the confirm click posts. The face check covers both paths. |
| CB-3 | WP0 Codex probe | **Yes, one probe.** |
| CB-4 | Focus text | **Later.** A follow-up PR, Claude only at first. |
| CB-5 | Discord | Not asked; the lead's default is no post. |

Lead: no summary text is logged, which deliberately departs from the brief's default. The merge rule for these PRs is not yet set, so the lead asks at PR time.
