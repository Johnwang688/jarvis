# Design: the owner inbox (Jarvis v2)

**Status (2026-10-24): draft for the owner's review.** Nothing here is built.
Written against `main` at `2229645e`. Paths are relative to the repo root. The
decisions in §2 are the planner's defaults; each can be reversed, and the open
questions in §10 have their defaults in bold. The companion design
[2026-10-24-phone-surface-design.md](2026-10-24-phone-surface-design.md)
builds its main screen on this inbox, so the inbox comes first.

## 0. What the owner gets

- **One list of everything waiting on you**, in a new **Inbox** view in the
  HUD sidebar, with a count on the app icon. Items are sorted by what they
  hold up, not by when they arrived. Each row says in one line what is
  waiting: "task 3f2a is stopped until you answer", "Codex wants to push to
  main", "PR #301 is ready to merge".
- **Everything that already asks you shows up here without changing how it
  asks:**
  - approval cards, live, with their countdown;
  - questions from chats and from tasks in `clarifying`;
  - task proposals still inside their grace window;
  - tasks that ended `blocked` or `failed`;
  - pull requests that only you can merge (the owner-only merge gate,
    `jarvis/v2/merge_gate.py`).
- **Two new kinds of item replace hand-written files:**
  - **Decisions:** the "answer whenever" questions that today live in files
    like `docs/plans/2026-10-12-questions-for-owner.md`. An agent posts the
    question with its options and its default; you pick one or type an
    answer; the answer goes back to the chat or task that asked, and is kept.
  - **Live checks:** the owner-only acceptance runbooks, like
    `docs/plans/2026-10-16-owner-live-checks.md`. Each check is a list of
    steps with "do" and "expect". You mark each step pass or fail, with a
    note. The result is the receipt, recorded as yours, and only you can
    record it.
- **Nothing is lost while you are away.** An approval that timed out while
  nobody was watching stays as a "denied while you were away" row, linked to
  its chat, so you can see what was stopped and ask again from the chat.
- **Answering from the inbox is the same act as answering where the item
  came from.** An approval answered here is the same card, with the same
  rules. A question answered here reaches the chat exactly as if you had
  typed the answer there. Discord answers clear the inbox row as well.

What it is not: a new place agents can reach you with authority. Agents can
add a decision or a check (bounded, scrubbed, untrusted text). They cannot
read the inbox, answer it, dismiss it, or put an approval in it.

## 1. What this builds on (current code)

| Source | Where it lives today | How it reaches the owner today |
|---|---|---|
| Approvals | `jarvis/v2/approvals.py` `PendingApprovals`: in memory, a blocked waiter per request, `LOCAL_TIMEOUT_S` 120 s or `REMOTE_TIMEOUT_S` 600 s, then deny | `approval_requested` / `approval_resolved` on the bus; the card in the thread's pane; Discord Approve/Deny (never Always) |
| Chat questions | provider `EventKind.QUESTION` events; `daemon._Session.questions`; `daemon.answer`; `question_answered` in the thread log | the thread's pane; the Discord mirror re-posts open ones from the log |
| Task questions | `model.OpenQuestion` on the task (`blocking`, `answer`); state `CLARIFYING`; `TaskControl.answer_question` | `task_question` event; the task view; plain text in the task's Discord thread |
| Proposals | `runner.py` proposal with a grace window; `proposal_reply` | the chat that proposed it |
| Task outcomes | `task_status_changed`; states `BLOCKED`, `FAILED`, `DONE` | the task view; Discord milestone posts with one `<@owner>` ping |
| Merges | `merge_gate.judge`: merging or closing a PR, and pushes that amount to one, are always the owner's | an approval card, only when an agent tries; a PR simply waiting for you appears nowhere |
| Owner decisions | hand-written Markdown under `docs/plans/` | you open the file |
| Live checks | hand-written runbooks under `docs/plans/`; results go into the checkpoint by hand | you open the file |

The last three rows are the gap. The first five already have owners, stores
and events; the inbox only needs to **project** them.

## 2. Decisions (planner's defaults, each reversible)

- **IN-1. The inbox is a projection first, a store second.** Rows for
  approvals, questions, proposals, task outcomes and PRs are computed from
  the sources above every time; the inbox stores none of them. Only what
  has no home today is stored: decisions, checks, "denied while away"
  records, and the owner's snoozes and dismissals. One source of truth per
  fact; the inbox can never disagree with the card.
- **IN-2. No new answering path.** Each answer button calls the function the
  original surface calls: `PendingApprovals.resolve(..., hud=True)`,
  `daemon.answer`, `TaskControl.answer_question`, the proposal's withdraw.
  The inbox routes are owner-only (`projects.owner_only`) and no tool
  reaches them (`tests/v2/archive_check.py`'s rule, extended).
- **IN-3. Approvals render with the existing card component, unchanged.**
  Deny cheap, authorize deliberate, nothing keyboard-defaulted,
  `textContent` only, and the card stays the loudest thing on screen. The
  inbox does not change timeouts or turn approvals into durable items; an
  approval is still a live question with a live waiter.
- **IN-4. Ranking is by what an item blocks**, in this order:
  1. a live approval (it is denied when its timer runs out);
  2. a blocking question on a running turn or a `clarifying` task;
  3. a proposal inside its grace window;
  4. a `blocked` task;
  5. a PR waiting on the owner's merge;
  6. decisions, oldest first;
  7. live checks, the ones a merged PR names first;
  8. `failed` and "denied while away" records, which are information only.

  Ties go to the older item. The ranking is code, not a setting.
- **IN-5. Agents may post two kinds, through one bounded tool.**
  `owner_post` creates a decision or a check. It is bound only in turns
  whose toolset includes it, capped per thread and per day, length-capped,
  and every field is `secrets.scrub`bed and shown as untrusted text, with
  its provenance line (`approvals.label`). A decision's options are text,
  never actions. A check's steps are text; "copy" puts a step's command on
  the clipboard, and nothing in a check runs anything.
- **IN-6. A check's result is the owner's alone.** Only the owner HUD route
  marks a step. No tool can record, edit or clear a result, so "only what
  you saw counts" stays true. A check whose PR has not been deployed says
  so and cannot be marked.
- **IN-7. PRs come from `gh`, read-only, on a timer.** The daemon lists open
  PRs in each project's repository that are not drafts, with the head SHA
  and checks state, every 5 minutes and on `task_status_changed`. It uses
  the owner's `gh` login in the daemon process, never in an agent's shell,
  and the result shows only number, title, branch and checks. Merging stays
  where it is: the owner's own `gh`, or an agent's card under the merge gate.
- **IN-8. Snooze and dismiss are per row and only for kinds 5 to 8.** A live
  approval or a blocking question cannot be hidden, because hiding it is
  how it times out unseen.

## 3. Data model

### 3.1 The row (what `GET /inbox` returns)

```jsonc
{
  "id": "appr:Xk2...",            // kind prefix + the source's own id
  "kind": "approval | question | proposal | task | pr | decision | check | missed",
  "rank": 1,                       // IN-4 band
  "title": "Codex wants to push claude/x to Johnwang688/jarvis",
  "blocks": "task 3f2a waits on this",   // one line, built by code, never by a model
  "project_id": "...", "thread_id": "...", "task_id": "...",
  "asked_at": "2026-10-24T15:02:11Z",
  "expires_at": "2026-10-24T15:04:11Z",  // approvals only
  "origin": "Codex · chat ‹title› (id)",  // approvals.label, sanitized
  "detail": { }                    // per kind; for an approval, exactly ApprovalRequest.to_json()
}
```

`title` and `blocks` are built by the inbox from fields the source already
validated. Agent-written text appears only inside `detail`, rendered as
untrusted text. A HUD-only (private) approval appears with its private
projection, as it does now.

### 3.2 Stored kinds

Under the data root, `inbox/` beside `threads/` and `tasks/`, written with
`stores._write_bytes` (atomic, mode 600):

- `inbox/decisions/<id>.json`: `{id, thread_id, task_id, project_id,
  question, options: [text], default: index|null, why, asked_at, answer:
  {choice|null, text|null, at}|null}`.
- `inbox/checks/<id>.json`: `{id, title, prs: [n], deployed_sha|null,
  steps: [{do, expect}], results: [{step, verdict: pass|fail|skip, note,
  at}], created_by: origin, imported_from: path|null}`.
- `inbox/missed.jsonl`: one line per approval whose waiter ended with
  `timeout` or `shutdown`: `{req_id, tool, origin, thread_id, task_id,
  at, resolution}`. These are public `to_json` fields only, never `owner_json`.
- `inbox/marks.json`: `{row_id: {snoozed_until|null, dismissed_at|null}}`.

### 3.3 Events

One new bus kind, `inbox_changed`, carrying only `{ids, reason}`. The HUD
re-reads `GET /inbox` on it. It is published by the inbox when any source
event it listens to arrives (`approval_requested`, `approval_resolved`,
`task_question`, `question_answered`, `task_status_changed`,
`proposal_reply`) and when a stored kind changes. The event carries no text,
so whoever listens to the bus learns nothing it could not already see.

## 4. API (owner-only, HUD listener)

| Route | Does |
|---|---|
| `GET /inbox` | the rows, ranked, with marks applied; `?all=1` includes snoozed and dismissed |
| `POST /inbox/<id>/answer` | dispatches to the source's own function (IN-2). An approval takes `{decision, always}`, exactly as `POST /approvals/<id>` does today, and is refused with 409 if already resolved |
| `POST /inbox/decisions/<id>` | `{choice}` or `{text}`; records it and delivers it to the asking thread or task (§5) |
| `POST /inbox/checks/<id>/steps/<n>` | `{verdict, note}` |
| `POST /inbox/<id>/mark` | `{snooze_until}` or `{dismiss: true}`; refused for kinds 1 to 4 |
| `POST /inbox/checks/import` | `{path}` under `docs/plans/`; parses a runbook into a draft check and returns it for the owner to confirm (§6) |

The routes answer only to `projects.is_owner` on `FACE_PORT`. The phone
surface adds a second listener with its own owner test; see the companion
design, §4.

## 5. Delivering an answer

- **Approval, chat question, task question, proposal:** the source's own
  function, so delivery is already correct.
- **Decision:** if the asking thread has a live session, the answer is
  delivered as an owner message, `UserMessage(origin="owner", via="inbox")`
  (a new `via` value beside "hud", "discord", "dm", "system" and "peer"), worded
  `Owner's decision on "<question>": <choice or text>`. It queues behind a
  running turn and never steers it, by the same rule as chat-to-chat (C3).
  If the asking thread is gone or archived, the answer is still recorded and
  the row says "recorded; the asking chat is closed".
  `via="inbox"` does **not** carry desk authority (fast-path F-1..F-7):
  answering a question is not typing an instruction at the desk.
- **Check:** nothing is delivered. When every step has a verdict, the check
  is done and the row moves to the done list. The owner can export the
  receipt as Markdown to paste into a checkpoint.

## 6. Importing what already exists

The existing runbooks are Markdown with a stable shape: `##` per feature,
numbered steps, "Expect:" sentences. `POST /inbox/checks/import` parses one
file into draft checks, one per `##` heading, and returns them **unsaved**.
The owner confirms each in the HUD. The parser runs in the daemon on a path
under `docs/plans/`, refuses anything else (`folders.py`-style lexical and
realpath checks), and never runs a model. The open
`questions-for-owner` files get the same treatment as decisions. Lines
already marked **Answered** are imported as answered.

## 7. HUD

- **Sidebar:** an `Inbox` row above projects with a count of ranks 1 to 5.
  The installed app sets `navigator.setAppBadge(count)`. `<title>` stays
  `J.A.R.V.I.S.` (invariant).
- **Inbox view:** a pane like the others. Rows in rank order, grouped under
  thin headers: "Waiting now", "Waiting on you", "When you have time",
  "For your information". An approval row is the existing card. A question
  row has the answer box. A decision row has its options as radio buttons
  with the default preselected **but not submitted**. A check row opens a
  step list with pass, fail and skip per step and a note field.
- **Click-through:** every row links to its thread or task pane. Clicking a
  row never answers anything.
- **Under an approval card,** the rest of the inbox is inert, as the HUD is
  today.
- **Theme:** Graphite. Amber for rank 1 to 2 (pending), red for `failed`,
  the glacier-cyan accent for selection only.

## 8. Safety: each Safety-design bullet this touches

| Rule | How the inbox keeps it |
|---|---|
| Approvals are the owner's; deny cheap, authorize deliberate | IN-3: same component, same `resolve`; no bulk Allow, no "allow all", no keyboard default |
| No tool reaches owner routes | every `/inbox` route is `owner_only`; the archive-check grep extends to `/inbox` |
| Labels and fetched text are untrusted | rows' own `title`/`blocks` are code-built; agent text only in `detail`, `textContent`, scrubbed |
| HUD-only Browser cards | private projection only; answering still needs `hud=True` (renamed `owner_verified` by the phone design, PH-7, where the HUD route and a verified phone are its only two setters) |
| Nothing pings but one line | the inbox sends no Discord posts and no notifications of its own; the badge is a count only |
| Credentials never in status or logs | `missed.jsonl` holds `to_json` fields only; `owner_post` scrubs every field before storing |
| Unknown or malformed fails closed | an `answer` whose source has gone away is a 409, never a guess; an `owner_post` with an unknown kind is refused |

## 9. Work packages

| WP | Package | Size | Acceptance |
|---|---|---|---|
| IN-0 | `jarvis/v2/inbox.py` projection over the five live sources; `GET /inbox`; `inbox_changed` | M | fake stores and a fake broker: every source state yields the row and rank in IN-4; resolving at the source removes the row in the same event cycle; a private approval shows only its private fields |
| IN-1 | HUD Inbox view, sidebar row, app badge | M | headless suite: the card in the inbox behaves exactly as the pane's card (reuse the card suite); rows are inert under a card; title unchanged |
| IN-2 | `POST /inbox/<id>/answer` dispatch | S | each kind answered from the inbox reaches the same function, with the same arguments, as from its own surface; a second answer is a 409; the routes refuse the API listener |
| IN-3 | Missed approvals: write `missed.jsonl` on timeout or shutdown resolutions | S | a timed-out ask leaves one record with public fields only; a shutdown with five asks leaves five |
| IN-4 | PR source via `gh` (IN-7) | S | fake `gh`: drafts excluded; a merged PR leaves at the next poll; `gh` failure shows "PR list unavailable", never an empty list presented as "nothing waiting" |
| IN-5 | Decisions: store, `owner_post` (decision), answer and delivery | M | caps enforced; scrubbed fields; delivery queues behind a running turn and never steers; `via="inbox"` has no desk authority (fast-path suite) |
| IN-6 | Checks: store, `owner_post` (check), step results, import of existing runbooks | M | no tool path writes a result (grep test plus a tool sweep); import of `2026-10-16-owner-live-checks.md` yields one draft per `##` with its steps, unsaved until confirmed |
| IN-7 | Marks (snooze, dismiss) and docs | S | marks refused for ranks 1 to 4; CLAUDE.md gets a short Inbox section pointing here |

Order: IN-0 → IN-2 → IN-1 → IN-3 → IN-5 → IN-6 → IN-4 → IN-7. IN-0 to IN-3 is
the useful core: everything that already asks, in one ranked place. The
SELF_PROTECTED list gains `jarvis/v2/inbox.py`, which decides what the owner
sees as waiting.

## 10. Open questions for the owner (defaults in bold)

1. **Who may post decisions and checks?** **Claude and Codex chats and task
   orchestrators only**; the fast path and sub-agents may not.
2. **Daily cap for `owner_post`:** **5 per thread, 20 per day overall.**
3. **Should a "denied while away" row offer "ask again"?** It cannot re-run
   the call itself (the turn has moved on). The offer would send the chat a
   message: "the owner wants you to retry <tool>". **No, link to the chat
   only.**
4. **PR scope:** **only PRs whose branch a Jarvis chat or task pushed**, or
   every open PR in each project's repo.
5. **Import the existing runbooks and questions files once at launch?**
   **Yes, on the owner's confirm, file by file.**
6. **Desktop notification when the HUD is not focused** (Windows toast via
   the installed app's `Notification`)? **Only for rank 1 to 2, and off by
   default.** The phone design covers notifications properly.

### Critical files for implementation

`jarvis/v2/approvals.py`, `jarvis/v2/daemon.py` (`answer`, the session
question set, event fan-out), `jarvis/v2/control.py`/`runner.py`
(`answer_question`, proposals), `jarvis/v2/model.py` (`OpenQuestion`,
`TaskState`), `jarvis/v2/projects.py` (`owner_only`), `jarvis/v2/stores.py`,
`jarvis/v2/merge_gate.py`, `hud/src` (the approval card component and the
sidebar).
