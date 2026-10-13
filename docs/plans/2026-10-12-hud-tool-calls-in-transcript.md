# HUD: tool calls in the chat transcript, collapsed (2026-10-12)

## The owner's request

> "Also I want to move the list of commands or tools that Jarvis used I to the chat transcript instead of being separate rn and they should like collapse so a sequence of 15 tools calls just collapse to used 15 tools and I can click open to see what was done."

## Today (main at d2c16bb)

- **The separate list is the chat pane's OPERATIONS ticker.**
  - `components/ChatTab.tsx` (~l.118) draws `chat.ops` in `.chatops` below the log (`theme.css` l.382, at most 132 px tall).
  - Its ids are `data-testid="ops"` and `"op"`. It shows the newest first and draws 30 of the 61 kept (`store.tsx` `op_start` keeps `.slice(-60)`).
  - Each row shows the name, `running`/`done`/`failed`, and a duration in seconds. `ToolOp.summary` is stored but never drawn.
- **It is fed only by SSE** (`App.tsx` ~l.941):
  - `tool_started` dispatches `op_start`, sets the status to `RUNNING · <name>`, turns the orb amber and calls `buffers.noteTouch`.
  - `tool_finished` dispatches `op_done` with `ok` and `summary`.
  - Durations come from the window's clock.
- **A reopened thread has no tool history at all.** `land` (~l.2006) clears `ops`. `GET /threads/{id}/transcript` (`hud_api.py` ~l.1810) keeps only `text`, `user` and `model_set` rows.
- **The log already holds every call.** `Daemon._record` appends every provider event except deltas to `threads/<id>/log.jsonl`, with `turn_id` and `at`, then publishes the same record. The shapes (`provider.py` l.86) are `tool_started {call_id, name, args}` and `tool_finished {call_id, name, ok, summary}`.
  - `args` is the model's raw input, and none of it is scrubbed. Claude's Write carries the whole file, Codex's `apply_patch` carries the diffs, and a Bash command is whatever the model typed.
  - `summary` is **result text**, which differs by provider:
    - fast path: the result's first 200 characters, scrubbed by dispatch;
    - Claude: the same, through `_safe`;
    - `terminal_read`: a fixed "read N lines";
    - Codex: "exit code N" or "N files, +a/-b lines";
    - Claude's `/compact` call: "conversation compacted".
  - **A denial is `ok: false`**, with the provider's own text in `summary`: "The user declined…", "Refused…", the Claude hook's "Jarvis denied this call", or the classifier's prefix. Codex sends no denial word.
- **Other places that show activity:**
  - the big orb, amber while `ops` holds a running call (`store.tsx` `turnOrb`);
  - the `RUNNING · <name>` status line;
  - the sidebar dots (`activity.py`);
  - TaskView's "last tool";
  - the Discord footer, which lists names only.
- **In flight:**
  - **PR #36 (WP-H)** puts a thinking line between the log and the ticker. Its `lib/thinking.ts` reads `chat.ops` to say "Running X…".
  - The Settings page (plan 2026-10-11) touches the title bar, the sidebar foot and the status column, never the chat pane.

## Decisions (planner's; the lead can redirect)

| # | Decision |
|---|---|
| T-1 | **Each tool call becomes a transcript entry, and the ticker goes.** Consecutive calls *of one turn* with nothing between them are **one group row** in the log, at the place they happened. |
| T-2 | **The grouping rule** is pure, in `lib/toolCalls.ts`. <ul><li>A call joins the last transcript entry if that entry is a group with the same `turn_id`. Anything else starts a new group.</li><li>A group ends at text that settles, at any user message (a steer, a queued message, a Discord message), and at a system line (`model_set`, a proposal). It never spans two turns.</li><li>A `tool_finished` finds its call by `call_id` among that turn's groups, since a steer may have split them. A finish with no start is dropped, as today.</li><li>`fold(entries)`, used for history, and `applyStart` / `applyFinish` / `stopTurn`, used live, are one rule. A property test holds them equal.</li></ul> |
| T-3 | **Wording.** <ul><li>Collapsed: "Used 1 tool", "Used 15 tools".</li><li>Only the open group of a running turn (the last entry, while the turn runs) says "Using tools… 3". The row therefore never flickers between Using and Used as calls start and finish.</li><li>Suffixes: "· 2 failed" (the red token), "· 1 denied" and "· 1 stopped" (muted).</li><li>It never uses the words Authorize, Approve or Deny.</li></ul> |
| T-4 | **Opened, the group shows one line per call:** <ul><li>the name, cleaned and capped at 40 (#36's `toolName`);</li><li>the label (T-5);</li><li>a status word: `running` (amber), `ok` (muted), `failed` (red), `denied`, or `stopped`. It is always a word, never only a colour;</li><li>the duration, in #36's time format.</li></ul> **No result text is shown**: neither `summary` nor `args` is ever drawn (owner question 1). |
| T-5 | **The label is the daemon's, from one rule** (`jarvis/v2/tool_calls.py` `label(name, args, cwd)`). <ul><li>It takes the first present of `command`/`cmd`, `file_path`/`notebook_path`/`path`/`paths`, `pattern` (plus ` in <path>`), `url`, `query`, `description`, `terminal`, `name`.</li><li>It never reads `content`, `old_string`, `new_string`, `edits` or `changes`.</li><li>A path under the thread's folder is shown relative to it.</li><li>The text goes through `secrets.scrub`, then `approvals.clean_line(…, 120)`.</li><li>It is computed **once, in `_record`, before the daemon's lock is taken** (scrub reads files), and written onto the `tool_started` record, so it reaches the log and the bus.</li><li>For a row from before this change, the route computes it at read time, for the newest 500 calls per request. Older calls show their name only.</li></ul> **Flag for the lead:** this adds one derived field to the log and the bus. It is a scrubbed, capped subset of `args`, which the same record already carries. The alternative is bus-only plus recomputing on every transcript read, which means 2,000 scrubs, each reading the env files, per open. |
| T-6 | **The outcome comes from the provider.** `tool_finished` gains `outcome: "ok" \| "failed" \| "denied"`. <ul><li>The fast path uses its own `_NOT_OK` prefixes: declined or refused means denied.</li><li>Claude uses its classifier prefix and its hook's deny message, constants it owns. The builder checks the CLI's real tool_result text.</li><li>Codex uses the item's `declined` status, read from the generated schema. If the schema has no such status, a Codex denial reads as failed.</li><li>An old record maps `ok` to ok or failed.</li></ul> The window never matches result text. |
| T-7 | **Live behaviour.** <ul><li>The group grows in place.</li><li>At `tool_started`, the pane's unsettled draft is dropped. On the fast path that draft is interim text, which the daemon logs as `thinking` and history never shows; today it was overwritten at settle. Claude and Codex settle text before a call starts.</li><li>`turn_finished` marks that turn's running calls `stopped`. This must happen before its `next` early-out. The 15 s reconcile does the same when it frees a pane.</li><li>**Scroll:** the log follows new entries only if it was at the bottom (within 24 px). A reader who has scrolled up is never moved, and opening or closing a group never scrolls the log. Today's effect jumps to the bottom on every change, which would yank the reader on every call.</li><li>A call heard while that thread's transcript is still loading is replayed onto it when it lands, by `call_id`, so a call is never counted twice.</li></ul> |
| T-8 | **Open state is in memory only**, window-wide, keyed `threadId\|firstCallId`. <ul><li>It lives in `state/toolGroups.ts`, outside React like `state/buffers.ts`, with at most 500 keys; the oldest is dropped.</li><li>Each row reads its own key through `useSyncExternalStore`, so a toggle redraws that row and nothing else.</li><li>Every group starts closed, the running one included. A reload closes them all. Nothing goes in localStorage.</li></ul> |
| T-9 | **History.** <ul><li>`GET /threads/{id}/transcript?tools=1` returns, in log order among the existing entries, `{role: "tool", call_id, name, label, outcome, turn_id, at, finished_at?}`.</li><li>A call with no finish is `running` only if its `turn_id` is the turn the daemon is running for that thread now. Otherwise it is `stopped`, which covers a restart.</li><li>Without `tools=1` the response is byte-identical to today's, so an older window is untouched. The HUD sends it, and on a 400 (an older daemon, since `_object` is strict) it loads again without it.</li><li>`summary` and `args` are never in the response.</li><li>The HUD folds the entries with T-2's rule.</li></ul> **Covered:** <ul><li>all three providers;</li><li>steers and queued messages, which are logged where they were sent, so history matches what was seen live;</li><li>a stopped turn;</li><li>Claude's `/compact`, a call named `compact` like any other.</li></ul> |
| T-10 | **What goes and what stays.** <ul><li>**Goes:** the ticker (`.chatops`, `data-testid="ops"` and `"op"`, its CSS including the dead `#ops`) and `ChatTab`'s `ops` prop.</li><li>**Stays:** `ChatState.ops`, as the live running-call state that the orb, `RUNNING · name` and #36's thinking line read. The same two reducer cases write it and the transcript, so the two cannot disagree.</li><li>**Unchanged elsewhere:** the orb, the status line, #36's line ("Running pytest… · 3s"), the sidebar dots, TaskView, the Discord footer.</li></ul> **The transcript is the record; the thinking line is the present tense.** A group shows no approval or question state: the card, the thinking line and the dot already say it. |
| T-11 | **Keys, accessibility, and the approval card.** <ul><li>The row is `<button type="button" aria-expanded aria-controls data-testid="tool-group">`. It has no `.msg` class, so existing `.msg` selectors are untouched.</li><li>Enter and Space toggle it, natively.</li><li>Ctrl+D stays push-to-talk: the window takes it in capture, so it never toggles a group.</li><li>`noPointerFocus`: a click leaves focus in the chat box, while Tab still reaches the row.</li><li>The calls are a `<ul>` of `<li data-testid="tool-call" data-status>`.</li><li>Under an approval card the log is inert (WP-B), so nothing toggles.</li></ul> |
| T-12 | **Performance.** <ul><li>`ChatTab` stays memoized, and its rows become memoized `MessageRow` and `ToolGroupRow`.</li><li>The reducers replace only the group that changed, so a call redraws one row and a keystroke redraws none.</li><li>An opened group with more than 40 calls is a fixed-height list (22 px rows, at most 16 shown) windowed with `lib/scm.ts` `scmWindow(count, scrollTop, viewPx, row, over)` as it is. `ScmList` is not edited.</li><li>The loader folds the transcript once.</li></ul> |
| T-13 | **Safety.** <ul><li>Text nodes only, never `innerHTML`.</li><li>The HUD cleans the name and the label again (one line, invisible characters out, capped), as a second layer; the full label appears only as `title`.</li><li>No new Python log lines and no new bus kinds.</li><li>The `__hud.toolGroups()` hook is read-only and returns keys only.</li></ul> |

## Checks, and the mutation each must catch

**vitest**

- `lib/toolCalls.test.ts`:
  - **Grouping:** text, a user message, a system line or a new turn each split a group. Must catch: the turn check dropped, so a queued turn's calls join the group before them.
  - **A finish across a steer split** reaches its call. Must catch: the finish searched in the last group only.
  - **`stopTurn`** stops only its own turn. Must catch: it stopping every turn.
  - **A finish with no start** is dropped.
  - **Live equals fold** over 1,000 random event sequences, including steers, queued messages, stops and compacts.
  - **Wording:** singular and plural, "Using tools…", the suffix counts, and none of the card's three words. Must catch: "Used 1 tools".
  - **Cleaning:** markup, invisible characters and newlines in a name or label come out as one plain capped line.
- `components/ChatTab.test.ts`, jsdom, with a render counter as in `ProjectDiff.test.ts`:
  - **Setup:** 2,000 calls in 200 groups, with 200 replies.
  - **Redraws:** twenty parent redraws with the same props render 0 rows, one new call renders 1 row, and a toggle renders 1 row. Must catch: `memo(` dropped from the rows, or the open state passed as a `ChatTab` prop.
  - **Windowing:** an opened 2,000-call group has at most 16 + 2 × overscan rows in the DOM. Must catch: no windowing.
- `state/store.test.ts`:
  - `op_start` writes both `ops` and the transcript;
  - `tool_started` clears the draft;
  - `turn_finished` with `next` still stops that turn's running calls.

**Python**

- `tests/v2/tool_calls_check.py`:
  - **The label table**, per tool and per provider:
    - Write's `content`, the `apply_patch` diffs and Edit's strings never appear in a label;
    - a fake secret value in a command is scrubbed (temp HOME and folder holding a `.env.production`);
    - relative paths, the cap and invisible characters.

    Must catch: `scrub` dropped, or `content` added to the keys.
  - **`_record`** writes `label` to the log and the bus, outside the lock: a blocking fake scrub does not hold `_lock`. Must catch: the label computed under the lock.
  - **The route without `tools=1`** gives a golden answer equal to main's.
  - **The route with `tools=1`:**
    - calls sit in log order among text and user rows, and steers and queued messages are placed correctly;
    - a call is running for the live turn and stopped after a finish or a restart;
    - `compact` appears as a call;
    - pre-feature rows are labelled, up to the 500 cap;
    - no `summary` value and no `args` key appears anywhere in the JSON.

    Must catch: the summary leaked, stopped reported as running, or the old shape changed.
- Provider checks:
  - `fastpath_check`: declined means denied, and "Error" means failed;
  - `claude_provider_check`: the hook's deny and the classifier mean denied;
  - the Codex suite: a declined item means denied.

  Must catch: each mapping turned off.

**Headless:** `tests/face/hud_v2_toolcalls_check.py`, on a `MockDaemon(0)` of its own, under `guard_live` and `watch_csp`, run by `hud_v2_check` after the multichat section, or alone.

1. Three `tool_started` give one `tool-group` after the last message, reading "Using tools… 3", and there is no `[data-testid=ops]` anywhere in the page.
2. `text` and then a call: the first group reads "Used 3 tools", and a second group follows the text.
3. Opening and keys:
   - a click sets `aria-expanded=true` and shows `tool-call` rows with name, label, status and duration;
   - with focus on the row, Enter and Space each toggle it;
   - Ctrl+D held on the focused row records (`TALK_PROBE`) and does not toggle;
   - a click leaves focus in the chat box.

   Must catch: `noPointerFocus` dropped.
4. `failed` and `denied` give "· 1 failed" in the red token and "· 1 denied".
5. A steer mid-turn splits the group. A queued message splits it, and the queued turn's calls start a group of their own.
6. `turn_finished {stop: "interrupted"}` makes the running call read `stopped`.
7. After a reload, the log's text is identical (groups collapsed), from a mock transcript built from the same events. Must catch: the draft not cleared at `tool_started`.
8. Scrolling:
   - scrolled up, 20 new calls leave `scrollTop` unchanged;
   - at the bottom, the log follows;
   - opening a group leaves `scrollTop` unchanged.

   Must catch: the log always scrolling to the bottom.
9. With a card up:
   - a click on a group does nothing;
   - Tab never reaches the row;
   - the card's buttons are topmost (`elementFromPoint`) at 160%.
10. A name and a label holding `<img src=x onerror=…>` and a bidi override render as text, with no element created.
11. A 2,000-call transcript in two panes: 30 keystrokes in the box give no long task over 50 ms (the `_long_task_checks` observer).
12. An opened 300-call group has a bounded number of DOM rows, and End reaches the last call.
13. Pane 2's thread's calls never appear in pane 1 (ported from multichat).

**Ported checks:**
- `hud_v2_check.chat_checks` (l.210–219) and `hud_v2_multichat_check` (l.411–425, l.1739–1741) read `tool-group` and `tool-call` instead of `ops` and `op`.
- #36's placement checks ("before the ticker") read "above the input bar".

## Packages and order

- **TC-1, daemon (Python):**
  - `jarvis/v2/tool_calls.py`, `daemon._record`, the three providers' `outcome`, and the `hud_api` transcript route's `?tools=1`;
  - `docs/hud-api.md`;
  - `tests/v2/tool_calls_check.py`, the provider checks, and the `hud_backend_check` golden.

  It is safe on its own: today's HUD sends no `tools=1` and ignores the new fields. It can start now. It touches `daemon.py` and `hud_api.py` beside PR #79; whichever merges second merges the other first. Deploy with `jarvis restart`.
- **TC-2, HUD:**
  - `lib/toolCalls.ts` and its test, and `state/toolGroups.ts`;
  - `types.ts` and `store.tsx`;
  - `App.tsx`: the two tool cases, `turn_finished`, the reconcile, and the loader with its 400 fallback;
  - `ChatTab.tsx` and its test, and `theme.css`;
  - the mock (`?tools=1` and tool entries), the new headless suite and the ported checks;
  - CLAUDE.md and design §18.

  It starts **after #36 merges**: both edit `ChatTab`, the store and App's event cases. TC-2 keeps `ops`, so #36's `thinking.ts` needs no change, and it reuses #36's `toolName` and time format. It also starts after TC-1. Its only overlap with the Settings page is `hud_v2_check.py`'s section list and the docs. Deploy with `jarvis hud rebuild` and a window reload. If TC-1's daemon was never restarted, the 400 fallback still loads transcripts, without the tool history.

## For the owner

1. **Should an opened call also show the first line of what came back** ("exit code 1", an error's first line)? Recommend: **not now.** It is result text the window has never drawn. Codex's counts could follow on their own.
2. **Should the running group start collapsed too?** Recommend: **yes, as asked.** The thinking line under the chat already names the call running now.
3. **Should the collapsed row name the kinds** ("Used 15 tools · Read 8 · Bash 5")? Recommend: **not now.** It is a one-line follow-up if wanted.
