# Plan: the Inbox becomes your working set of threads (WS-0..WS-5)

**Status (2026-11-14): plan, revision 2; owner answered §9 Q1, Q2 and Q7 (the defaults) and said start WS-0 and WS-1.** Written against `main`
at `eb4f830f`. The owner asked for it on 2026-11-14 after a look at how t3code's
sidebar works (reference clone: `~/.cache/jarvis-lead/ref/t3code`, its
`docs/user/thread-sidebar.md` and `packages/client-runtime/src/state/threadInbox.ts`).
Sections:
- §2: the owner's answers;
- §3: the planner's defaults, each reversible;
- §9: the open questions, with defaults.

Revision 2 takes in an independent review (Codex gpt-6.1-sol, 12 findings, 3
HIGH). §10 says where each one landed.

## 0. What the owner gets

> "I'm envisioning the inbox being like the place where all the threads I'm
> currently working on live and once I've finished with the thread it gets
> automatically moved out and just goes back to the project it was in. I can
> access threads normally in their project as well if they are in inbox. So
> basically the inbox will just be a way to organize the work that I am
> CURRENTLY working on, kind of like how t3 code manages it." (owner, 2026-11-14)

- **The list.** The sidebar's **Inbox** (the aurora row at the top) opens into a
  list of the chat threads you are working on now, across every project.
  - Each row carries its project's name.
  - Each row has one label: *Needs you*, *Working*, *Done (unread)*, *Failed*,
    or nothing, meaning it is your move and already read.
- **Joining.** A thread joins on any activity:
  - you start it or message it;
  - an agent turn runs in it;
  - another chat relays to it, or a sub-agent of it reports;
  - it asks a question or an approval;
  - it fails.
- **Leaving on its own.** A thread leaves once it has been idle for a while
  (default 3 days, §9 Q1) with nothing running and nothing waiting on you.
- **Leaving at once.** It leaves straight away when you press **Done**, which
  has a 5-second Undo.
- **Leaving changes nothing else.** It only takes the thread out of the Inbox.
  It never moves, archives, closes or deletes the thread, and any new activity
  brings it straight back.
- **Every thread stays in its project** the whole time, exactly as today. The
  Inbox is a second view of the same threads, not a folder they move into.
- **Today's separate rows move onto their thread.** An approval or a question
  becomes the thread's *Needs you* label. Every row that has no thread entry to
  sit on goes in a small **Needs you** group at the top of the Inbox:
  - pull requests;
  - task items;
  - decisions and checks without a thread;
  - anything whose thread is archived, deleted or out of the set.

  Nothing that waits on you gets lost. The full **Needs you** page (today's
  Inbox page) stays one click away from the unfolded list (its "Needs you" line;
  the rainbow row folds the list, WS-3, §11).
- **Unsorted.** The catch-all project where chats with no project land is
  renamed **Unsorted**, so "Inbox" means one thing.

What it is not: a new place agents can reach you with authority, or a new way
to hide things that wait on you.
- Agent activity can put a thread in your Inbox (the owner's "any activity"
  answer), but only you can take one out.
- Nothing that today cannot be hidden (inbox ranks 1–4, quiet-hours deferrals
  included) can leave while it waits.

## 1. What this builds on (current code)

| Piece | Where it lives today | What the plan does with it |
|---|---|---|
| Thread record | `jarvis/v2/model.py:98` `Thread`; `threads/<id>/thread.json` (`stores.py:445`). No state, pin, seen or PR field. `updated` is stamped on usage, turn end and model change. | unchanged; the working set is its own store |
| Thread status | `jarvis/v2/activity.py`: `working`, `needs_input`, `unread`, `failed`, `idle`, from a synchronous bus observer. Running turns and asks are in memory; unread/failed are in `threads/<id>/activity.json`. There is no "unknown" state after a restart (`:117`). | labels come from it; eligibility does **not** rely on it alone (D-4) |
| Sidebar tree | `hud/src/components/Sidebar.tsx:374-669`. Each project shows its newest chats (`lib/recentChats.ts`) plus every open, running or non-idle one. `Dot` status at `:843-868`; drag-to-move at `:403-434`. | gains an Inbox section; project tree unchanged |
| Owner inbox rows | `jarvis/v2/inbox.py` `rows()`: ranks 1–8, the `_row` shape with `thread_id`/`task_id`, owner-only `/inbox*`. Marks in `inbox_marks.py`. PR rows in `inbox_prs.py` (no `thread_id`). Quiet-hours deferrals are rank 4 (`quiet_defer.py`). | the single truth for "needs you"; `/working` groups these rows and never changes `rows()` itself |
| Inbox page | `hud/src/components/InboxView.tsx`, `InboxPrRow.tsx`, `state/inbox.ts`, `lib/inbox.ts` (`GROUPS`, `waitingCount`, `anyPending`) | kept as the full **Needs you** page; decisions, PR merges and check steps still happen there |
| Inbox row and badge | `Sidebar.tsx:308-333` (`ownerinbox aurora`). The sidebar count and the app badge are both `waitingCount`: visible rank 1–5 rows (`App.tsx:4824`, `lib/inbox.ts:163`). Amber is `anyPending` (ranks 1–2). | becomes the Inbox section's header; the count rule is **unchanged** (D-6) |
| The catch-all project | `stores.py:278-295`: at most one, created lazily by `inbox()`, named "Inbox", rooted at home. The name is reserved in `projects.py:84` and `hud/src/lib/projects.ts:53`. The PATCH guard keys on `project.inbox` (`daemon.py:3666`). Discord shows it as `#ungrouped` and skips its renames (`discord/mirror.py:348`, `discord/linker.py:970`). | renamed **Unsorted** (WS-0) |
| Event stream | `/events` (`daemon.py:3599`) has no owner check and forwards records unchanged | `working_changed` carries no ids or data (D-5) |
| Phone and review | `remote_api.py` `/m/api/inbox` (`ROW_FIELDS`); `review.py` reads `inbox.rows()` | unchanged in WS-0..WS-4; WS-5 optional |

## 2. Owner's answers (2026-11-14)

- **O-1 Finished means:** idle for a while after its last turn, **or** the owner
  presses Done. Not "PR merged", not "pane closed".
- **O-2 Entering:** any activity at all, the owner's or an agent's.
- **O-3 Today's item rows:** shown on their thread. Items with no thread go in a
  small "Needs you" group at the top of the Inbox.
- **O-4 Naming:** the catch-all "Inbox" project is renamed "Unsorted".
- From the conversation:
  - threads stay reachable in their project while they are in the Inbox;
  - it should feel "kind of like how t3 code manages it".

## 3. Decisions (planner's defaults, each reversible)

- **D-1. Membership is daemon state.**
  - **Store:** one small store, `<data>/inbox/working.json`, inside the inbox
    data tree, which is already write-protected.
  - **Shape:** `{seeded_at, threads: {thread_id: {since, last, seq, done_at,
    done_seq, done_token, reason}}}`.
  - **Membership:** a thread is *in* when `done_at` is null.
  - **`seq`:** a per-thread counter, bumped under the store lock on every
    activity record (D-2).
  - **`last`:** the UTC time of that record.
  - **Writes:** every write goes through the existing atomic `_write_bytes`.
- **D-2. What counts as activity: a fixed producer table.** A bus observer reads
  these records synchronously inside `publish`, as `activity.py` does. Each
  record bumps `seq`/`last` on the named thread, and brings it back in if it
  was out (subject to D-4's `done_seq`).

  | Record | Thread it lands on |
  |---|---|
  | `user_message`, `turn_started`, `turn_finished`, `error` (thread-scoped) | that thread; **skipped** when the record is flagged as compaction (`daemon.py:2546`) |
  | `approval_requested`, `question` | that thread |
  | `question_answered` (HUD or Discord answer, `discord/gateway.py:1095`) | that thread; an answer from the owner is the owner's activity |
  | `relay_in`, `relay_delivered` (`chat_relay.py:395`, `:401`) | the receiving thread |
  | `relay_reply` (`chat_relay.py:502`) | the sender thread |
  | `subagent` start/finish ops (`subagents.py:296`, `:362`); not `progress` | the parent thread |
  | decision or check posted with a `thread_id` (`decisions.py:720`, `checks.py`) | that thread |
  | `thread_opened` for a newly created chat | that thread |

  - **Not activity:** `approval_resolved`, `queue_cleared`, renames, model
    changes, moves, `seen`, archive/restore/delete, Discord mirroring, compaction.
  - **Chat threads only.** Records on a task's threads never touch membership:
    tasks stay in Needs you (D-7).
  - **Labels.** The table drives membership only. Labels come from
    `activity.py` unchanged, so a sub-agent outliving its parent's turn makes
    the parent *join* but does not make it read *Working* (§8).
- **D-3. Leaving on its own (O-1).** A daemon timer takes a thread out
  (`reason: "idle"`) when **all** of these hold:
  1. `last` is older than the idle limit (default 3 days, setting 1–30, §9 Q1);
  2. `activity.thread_status` is not `working` or `needs_input`;
  3. D-4's eligibility check passes.

  Unread and failed do not hold a thread in (§9 Q2). An open pane does not
  hold a thread in (§9 Q3): that would need a pane-presence protocol, and
  closing a pane was not chosen as a signal.

  Timer mechanics:
  - It runs hourly plus once a few minutes after start, after the inbox
    sources are up.
  - Scheduling is injectable, so tests use a fake clock with relative expiry.
  - Expiry compares stored UTC `last` to wall-clock now. A clock jump forward
    can expire early, which is harmless because any activity brings the
    thread back.
- **D-4. Done and Undo, with one eligibility rule.**
  - **Eligibility.** A thread may leave, by Done or by the timer, only if a
    fresh `inbox.rows()` read, done under the working-set lock, shows **no row
    of rank 1–4 linked to it**. "Linked" means its `thread_id`; a quiet-hours
    deferral counts. Its status must also not be `working`/`needs_input`. A
    failed `rows()` read means "not eligible" (fail closed). This is IN-8
    carried over to threads.
  - **Done:** `POST /working/<id>/done {seq}`.
    - Owner-only.
    - `seq` is the one the HUD drew.
    - If the stored `seq` is higher, the request is stale: 409 with the
      current entry, so the HUD redraws.
    - Otherwise, check eligibility and write `done_at`, `done_seq = seq` and a
      fresh random `done_token`, all under the lock in one step.
    - Done then calls `seen_thread` once, so a finished thread is not left
      blue in its project.
  - **Undo:** `POST /working/<id>/undo {token}`.
    - Undo restores the entry only if `token` equals the stored `done_token`
      and the deadline has not passed.
    - The deadline is 10 s on the daemon's monotonic clock; the HUD's toast
      shows 5 s.
    - An old token after a later dismissal is a 409.
  - **Coming back.** Only a record that bumps `seq` past `done_seq` brings a
    Done thread back in. A late `turn_finished` from the turn that was already
    running is refused by eligibility, because the status was `working` and
    Done was blocked. So a Done thread can only come back on new activity.
- **D-5. Reading the list, and privacy.**
  - **The read:** `GET /working` → `{entries: [{thread_id, project_id, since,
    last, seq, status, rows: [row ids linked to it]}], needs_you: [every rank
    1–8 row not attached to an entry], count, pending}`.
  - **Owner-only**, like `/inbox`, because it carries inbox rows.
  - **Rows are attached by `thread_id`.** Every row of `inbox.rows()` appears
    exactly once in the response: on its thread's entry, or in `needs_you`.
    `inbox.rows()` itself, the phone and the morning review are unchanged.
  - **The signal:** a `working_changed` record with **no ids and no data**
    tells HUD windows to re-read. `/events` is not owner-checked, so the record
    reveals only *that* something changed.
  - **Privacy of membership itself:** the list is mostly derivable from
    `/threads` + `/activity`, which local clients can already read. So the
    plan does not treat it as a secret; the boundary is on **writes** and on
    the inbox rows (§9 Q7).
  - **File protection:** `working.json` sits in the protected inbox data tree,
    so agent writes are refused. Agent reads of it are refused the same way
    other `<data>/inbox/` reads are, with a negative test that pins whichever
    holds.
- **D-6. The count and badge are unchanged:** visible rank 1–5 rows
  (`waitingCount`), amber when any is rank 1–2 (`anyPending`). The same
  number appears on the Inbox row and the app badge. Folding rows onto
  threads changes where they are *drawn*, not what is *counted*. So
  several asks on one thread count several times, as today, and a failed
  chat does not count, as today. The aurora wave stays.
- **D-7. Tasks are not entries (revision 2).**
  - A task's rows (blocked rank 4, failed rank 8, task questions rank 2) stay
    in the Needs you group, exactly as on today's Inbox page.
  - Task threads never join.
  - A typed task entry, with its read and Done semantics, is left for later
    (§8). This avoids the double-listing and the reappearing rank-8 row the
    review found.
- **D-8. Lifecycle.**
  - **Archive** takes a thread out (`reason: "archived"`). Its rows still show
    in `needs_you`, because approvals deliberately survive an archived project
    (`inbox.py:288`).
  - **Restore** does not rejoin by itself.
  - **Delete** drops the entry.
  - **Move and rename** publish `working_changed` so project chips and titles
    redraw. They do not bump `seq`.
  - Archived threads and threads in archived projects never join.
- **D-9. Order inside the Inbox:**
  1. the Needs you group;
  2. entries, ordered by label: needs you, failed, done-unread, working, your
     move;
  3. within a label, `last`, newest first.

  There is no drag order (§9 Q5). Working threads are not folded away as in
  t3code: the Inbox is short by design.
- **D-10. Rows behave like project rows.**
  - Click opens the thread, with the same `seen` rules.
  - The ⋯ menu adds **Done**. It is greyed out, with the reason, while the
    thread is not eligible.
  - Drag onto a project still moves it.
  - The row shows its project's name as a chip.
  - In the project tree, an Inbox thread gets no extra mark (§9 Q4).
- **D-11. Startup and seeding.**
  - **Order:** load the store, then start the observer, then start the timer
    (its first run comes after the inbox sources are up).
  - **Seeding.** If `seeded_at` is absent, seed once and set it. Seeded:
    - every visible chat thread whose `updated` is within the idle limit;
    - every thread with a linked rank 1–4 row.
  - **Restart.** Membership and `last` come from the store, so nothing
    expires that was recent. A turn interrupted by the restart reads `idle`
    afterwards, but its recent `last` keeps it in until the idle limit.
- **D-12. Unsorted (O-4).**
  - **What stays:** the internal flag `Project.inbox`, its id, its root and
    its Discord channel, which is already `#ungrouped` and skips renames.
  - **The rename:** the reserved name becomes "Unsorted" in `stores.py`,
    `projects.py:84` and `hud/src/lib/projects.ts:53`. A one-time migration
    renames the stored project.
  - **Collision.** If an existing project, archived or not, already uses the
    name "Unsorted" (case-insensitively), the migration leaves the catch-all
    as "Inbox" and logs it. The HUD's project menu then says why. The
    owner's own project is never renamed.
  - **The PATCH guard** is not relaxed: the migration writes through the
    store, not the route.

## 4. Boundaries this must keep

- **Owner-only routes.** `/working*` routes are owner-only through
  `projects.owner_only`, never the phone listener and never an agent.
  `projects.is_owner` is unchanged. No chat tool names them; extend
  `archive_check.py`'s route-name scan, and add negative tests for agent
  writes to `working.json`.
- **Protected files.** `jarvis/v2/working.py` joins SELF_PROTECTED
  (`jarvis/tools/files.py`); its store is under the protected `<data>/inbox/`.
- **IN-1 / IN-2 / IN-8.** The working set is a grouping of the same projection,
  with no new answering path. Rank 1–4 rows never disappear: they block a
  thread from leaving (D-4), and every row is drawn somewhere (D-5).
  Needs-you rows keep their existing mark rules.
- **Model text stays data:** titles and row text are rendered as text, as today.
- **Sidebar performance.** The sidebar stays memoized (`Sidebar.tsx:87-93`,
  `useStableValue`); the Inbox list gets stable callbacks and row arrays.
- **Untouched:** quiet hours, push, the morning review and the phone allowlists.

## 5. Work packages (one PR each)

| WS | What | Size | Notes |
|---|---|---|---|
| **WS-0** | Rename the catch-all project to **Unsorted**: the reservation in three places, the one-time migration with collision handling, the `INBOX_WHY` text, HUD labels, docs and tests (`archive_check.py`, projects face check). | S | independent; can merge first; Python, so a daemon restart |
| **WS-1** | Daemon working set: `jarvis/v2/working.py` (observer with the D-2 table, store, timer, seeding, D-4 eligibility, D-8 lifecycle); `GET /working` with rows attached; `POST /working/<id>/done\|undo`; `working_changed`; idle-limit setting with backend validation; SELF_PROTECTED entry. Tests in `tests/v2/working_check.py`, below. | M–L | the boundary PR: Opus build, then Opus review plus Sol |
| **WS-2** | HUD Inbox section, **additive**: `state/working.ts` (one read in flight, refresh on `working_changed`/reconnect); Inbox list under the aurora row with labels, project chip and attached-row hints; Done + Undo toast; the Needs you group. Today's Inbox page and its navigation stay. Face check `hud_v2_working_check.py` with a mock. | M | Sonnet build, Sol verify |
| **WS-3** | Presentation only: remove the duplicates. Thread-attached rows render on their thread (approval, question, decision, check). Today's page is renamed **Needs you** and opened from the group. Update `InboxView` tests and the inbox face check. | M | Sonnet build, Sol verify |
| **WS-4** | Idle-limit control in the HUD settings view, docs (`hud-workspace.md`, `control-plane.md`, `hud-api.md`), CLAUDE.md's Inbox section, decision records. | S | docs text is tested: grep tests before editing |
| WS-5 | *(optional, later)* Phone: show the working set on the phone's Inbox tab through a new `remote_api` allowlist. | M | needs its own owner OK |

WS-1's tests (`tests/v2/working_check.py`) cover:
- the producer table, row for row;
- compaction skipped;
- a fake clock, restart, and seeding once;
- eligibility blocked by an approval, a question and a quiet-hours deferral;
- a failed `rows()` read failing closed;
- stale `seq`, repeated Done, an old Undo token, and Undo after the deadline;
- archive, restore, delete and move;
- owner-only routes, and agent-write refusal;
- `inbox.rows()`, `remote_api` and review output unchanged.

Order: WS-0 ∥ WS-1 → WS-2 → WS-3 → WS-4. Each PR needs:
- Sol verification, plus mutation evidence for the boundary checks;
- tests covering its own in-between state (phone allowlists and morning-review
  output unchanged);
- the owner's OK before merge;
- then install, and a daemon restart for WS-0 and WS-1 when no turn is running.

## 6. Tests that pin today's behaviour (will change)

- **Sidebar:**
  - unit: `lib/recentChats.test.ts`, `lib/activity.test.ts`, `lib/threads.test.ts`;
  - face: `hud_v2_activity_check.py`, `hud_v2_recent_chats_check.py`,
    `hud_v2_projects_check.py`, `hud_v2_multichat_check.py`.
- **Inbox:**
  - unit: `lib/inbox.test.ts`, `state/inbox.test.ts`, `InboxView*.test.ts`,
    `InboxPrRow.test.ts`;
  - face: `hud_v2_inbox_check.py`;
  - backend: `inbox_check.py`, `inbox_marks_check.py`, `archive_check.py`,
    `remote_api_check.py`. The phone allowlists must not change in WS-0..WS-4.

## 7. Risks

- **Two drawings of "needs you"** (thread label and group row). Both come from
  one `inbox.rows()` read per `/working` response, and each row is drawn once.
- **Noise from "any activity".** Relays, sub-agents and Discord answers all
  bring threads back. If that gets noisy, the fix is narrowing D-2 or a
  per-thread "keep out", never hiding needs-you rows.
- **Cost of the eligibility read.** `inbox.rows()` is computed fresh. Done and
  the hourly timer call it once per decision (the timer once per sweep, for
  all candidates), not per record.

## 8. Not in scope

- Auto-leave on PR merge. It was not chosen, and chat threads have no
  branch/PR link today.
- Task entries in the working set (D-7).
- A *Working* label for a parent while only its sub-agent runs.
- Snooze for threads; Needs-you rows keep their existing snooze.
- Drag ordering, pinning, archiving from the Inbox, t3code's "boring mode".

## 9. Open questions (defaults in bold)

1. Idle limit before a thread leaves on its own: **3 days** — owner, 2026-11-14.
2. Does an unread or failed result keep a thread in past the idle limit? **No** — owner, 2026-11-14.
3. Does a thread open in a HUD pane stay in? **No** (avoids a pane-presence protocol) / yes.
4. In the project tree, mark threads that are in the Inbox? **No** / a small dot.
5. Let the owner drag-order the Inbox? **No (label, then recency)** / yes.
6. ~~Rename the Discord channel?~~ Settled by the code: it is already `#ungrouped` and skips renames.
7. May agents see *which* threads are in your Inbox? **Yes: it is derivable from threads + activity anyway; only changes and inbox rows are owner-only** — owner, 2026-11-14. (So D-5's agent-read test pins today's `<data>/inbox/` read behaviour; no new read refusal.)

## 10. Review record (revision 2)

Independent review: Codex gpt-6.1-sol, 2026-11-14, on revision 1.

| # | Finding | Where it landed |
|---|---|---|
| 1 HIGH | status alone does not enforce IN-8 (deferrals) | D-4 eligibility from a fresh `inbox.rows()` read, failing closed |
| 2 HIGH | `working_changed {ids}` leaks on the unchecked `/events` | D-5: no ids or data; §9 Q7 for membership privacy |
| 3 HIGH | linked rows with no visible parent get lost | D-5: every row drawn exactly once; D-8: archived threads' rows go to `needs_you` |
| 4 MED | the activity list misses producers | D-2 producer table (relays, sub-agents, decisions/checks, Discord answers, compaction excluded) |
| 5 MED | `done_key`/Undo concurrency | D-1 `seq`; D-4 stale-`seq` 409, atomic check-and-write, token-checked monotonic Undo |
| 6 MED | task rows underspecified | D-7: tasks deferred, their rows stay in Needs you |
| 7 MED | restart "first status read" does not exist | D-11 startup order, `seeded_at`, restart reasoning |
| 8 MED | open-pane retention needs a protocol | §9 Q3 default flipped to No; D-3 |
| 9 MED | lifecycle rules missing | D-8 |
| 10 MED | count/badge undefined | D-6: unchanged `waitingCount`/`anyPending` |
| 11 MED | rename coverage and collisions | D-12 |
| 12 MED | PR dependencies | §5: projection and guards in WS-1, WS-2 additive, WS-3 presentation |
| facts | "exactly one", "five newest", D-8 sub-agents, "unknown after restart" | corrected in §1, D-2, D-11 |

## 11. WS-3 owner decisions (2026-11-14)

- The rainbow Inbox row becomes the In progress list's header: clicking it folds/unfolds the list (WS-2's separate "IN PROGRESS" header goes); its count badge and amber are unchanged.
- A "Needs you" line is always shown at the top of the unfolded list, with its count (0 included), and opens the full page.
- Today's Inbox page is renamed "Needs you" and still lists every row (ranks 1–8), with a thread's rows grouped under that thread and a link to open it.
