# Plan: WS-5, the working set on the phone, and the phone's 1-hour unlock

**Status (2026-11-15): plan, revision 2.** It takes in the owner's answers (§2) and Sol's
review of revision 1 (§10). Written against `main` at `7215a5cf`. It uses the words of
`docs/plans/2026-11-14-inbox-working-set-plan.md`: entry, label, Needs you, D-n.

There are three packages (§8):
- WS-5a: the read route.
- WS-5b: the phone UI.
- **WS-5c: the unlock window plus Done and Undo.** This is the boundary package. It
  replaces PH-4's fresh assertion per action.

## 0. What the owner gets

- **An "In progress" list at the top of the phone's Inbox tab.**
  - **The Needs you line:** first comes "Needs you · N", where N is `waitingCount`,
    0 included. Tapping it scrolls to the rows.
  - **The threads:** then the first 5 threads, then "Show all".
  - **Each row:** title, project, label, age, and a hint such as "1 approval".
  - **Taps:** tapping a row opens the chat. Tapping the hint scrolls to that chat's first
    row below.
- **Today's rank groups stay below, unchanged:** every row, its card and its controls.
- **Unlock once, act for an hour (WS-5c).**
  - **What unlocks:** one passkey assertion unlocks every phone action: Allow, Always,
    Deny, cancel, PR actions, deferred Approve, and Done and Undo.
  - **How long:** the window lasts 60 minutes, and each action restarts the hour.
  - **When it lapses:** the next action asks for one passkey and reopens the window.
  - **Lock now** ends it at once.
  - **Reads** stay on the existing session (7 days, 12 h idle).
- **Unchanged:** push stays `{count, rank}`, with nothing from 23:00 to 07:00. Quiet hours,
  the morning review and NEVER_STEER are unchanged. Agents never reach `/m/*`.

## 1. What this builds on (current code)

| Piece | Where |
|---|---|
| Phone reads | `jarvis/v2/remote_api.py` (SELF_PROTECTED): route table `:915-936`; GET only; session re-checked before an answer leaves (`:976-978`); `*_FIELDS`/`DROPPED` `:140-262`; stream `stream_record` `:740-770`, `STREAM_KINDS` `:276`; `/m/api/inbox` cut at `INBOX_ROWS = 200` (`:115`, `:548-551`) |
| Owner test | `remote_auth.owner_status` `:1109-1131` (exact Host 400, write Origin 403, live session 401); `api_route` `:1173`, dispatch `:1222-1256` |
| Per-action assertions today | `answer_options` `:746-790`; `answer_verify` `:792-838`. The subject recognizer is `:755-762` (`_TASK_SUBJECT`/`_DEFER_SUBJECT`/`_PR_SUBJECT`); the decision tuples are `:149-168`; the family is set at `:779-781`, with `ANSWER_FAMILIES` and `MAX_ANSWER_CHALLENGES` at `:176-177`; `CHALLENGE_S` 60 s is monotonic (`:130`, `:350-355`). The callers are: `remote_answer.py:319/330/401/435` (Allow and Always, deferred Approve), `remote_write.py:497-519` (cancel), `pr_merge.py:849-891` (PR), `review.py:531-537` (Approve selected) |
| Sessions | in memory plus `sessions.json`: `login_verify` `:673-723`; `_expired` (wall clock, `IDLE_S`/`ABSOLUTE_S`, `:893`); `session_for` `:909` (touches last-seen); `session_live` `:938` (does not); `logout` `:961`; desk End `end_session` `:968`; `revoke` `:980` |
| Working set | `working.py`: `view()` `:1078-1103` (one `inbox.rows(marks="flag")` read); `_changed` `:635-640` publishes bare `{"kind":"working_changed"}`; Done/Undo `:813`/`:865`; `UNDO_S` 10 s |
| Phone app | `hud/src/mobile/model.ts:189` `StreamEvent`/`parseEvent` (drops unknown kinds and `thread_changed`'s reason); `app.ts` `handle()` `:1322-1340`, `later` 120 ms `:1345`, `loadInbox` `:312`; `net.ts:20` maps **any 401 to signed-out** |
| Desk store pattern | `hud/src/state/working.ts:80-96`: one read in flight, plus one pending re-read |

## 2. Owner's answers (2026-11-15)

- **Build WS-5:** yes, backend first, then the phone UI.
- **Stream:** add `working_changed` with no ids and empty data.
- **Placement:** top. The Needs you line, the first 5 threads with Show all, then today's
  rows unchanged.
- **Actions: a 1-hour unlock window.**
  - One assertion unlocks every phone action. Each action slides the hour, and a lapse
    asks for one passkey.
  - Reads stay on the session.
  - This replaces the per-action fresh assertion: CLAUDE.md "Phone" and PH-4.
- **Not asked, so defaults (not owner-confirmed):** no "leaves in" time; the desk's order.

## 3. Decisions for WS-5a/b (read path)

- **P-1. Additive.** Today's rank groups are untouched; the In progress list sits above
  them. WS-3's regrouping (rows under threads) is not done on the phone.
- **P-2. The read sends no inbox row data.** Hints come from the phone joining its own
  `/m/api/inbox` rows to entries by `thread_id`, the daemon's D-5 rule. Hidden rows never
  reach the phone (`marks="hide"`). A hint can lag a row by one refresh; the row itself is
  always drawn.
- **P-3. Order and cap (Sol 4).** The server orders entries as the desk does before it
  caps them (default, not owner-confirmed):
  - first by label: needs_input, failed, unread, working, idle (`LABEL_ORDER`);
  - then by `last`, newest first;
  - then it takes the first `WORKING_ENTRIES = 100`.

  So an old *Needs you* thread can never be cut in favour of newer idle ones. The
  response carries `total`. When `more` is true, the expanded list ends with "Showing 100
  of N: the rest are at the desk". There is no pagination. Show all reveals only what was
  received.
- **P-4. Freshness (Sol 2, 5).** `loadWorking` follows the desk store's rule: one read in
  flight, and a request made during it sets one pending re-read (`state/working.ts:80-96`).
  A response is applied only if it belongs to the latest read. The list re-reads on:
  - `working_changed`;
  - `inbox_changed`, because `approval_resolved` changes a label without bumping working
    activity;
  - `thread_changed` with reason `turn_started`/`turn_finished`;
  - opening the tab, stream reopen, coming back online, and `resync`.

  All of these share one 1 s debounce key. **The lag that remains:** status-only changes
  with no phone event (a desk `seen`, `queue_cleared`) show at the next read.
- **P-5. 375 px.**
  - **Rows:** each row is one 44 px target. The title is one line with an ellipsis; under
    it, label · project · age. The hint is its own 44 px chip.
  - **Colours:** amber only for pending (ranks 1–2), red only for *Failed*.
  - **Text:** text nodes only, never HTML.
  - **Empty state:** "Nothing in progress".
  - **Offline:** dimmed with the view.
  - **Show all** state is kept per device in `localStorage`, wrapped in try/catch.
- **P-6. IN-8, qualified (Sol 6).**
  - **What WS-5 keeps:** today's mark and group behaviour exactly. Ranks 1–4 have no mark
    control on the phone, no row moves, and nothing is hidden.
  - **An inherited gap, not a WS-5 regression:** `/m/api/inbox` sends at most 200 rows
    and the app ignores `more` (`app.ts:315`). More than 200 rows leaves some unreachable
    on the phone.
  - **The fix, an optional item in WS-5b:** when `more` is true, the app draws "More
    waiting than the phone shows: open the Inbox at the desk", with a test at 201
    blocking rows.

## 4. `GET /m/api/working` (WS-5a)

- **Where:**
  - `remote_api.working(daemon)`, added to `_match`. A query string is a 400; any method
    but GET is a 405.
  - Its data comes from a new read-only `WorkingSet.entries()`: the visible in-set
    entries, with no `inbox.rows()` read.
  - `view()` builds its entries through `entries()`, and keeps its single
    `marks="flag"` read, the exactly-once row attachment and `why_not` unchanged.
- **Errors:** 503 when `daemon.working` is None; 409 "the In progress list cannot be read
  now" when `entries()` raises.
- **Response:** `{"entries": [...], "more": bool, "total": int}`.

| Field (`WORKING_ENTRY_FIELDS`) | Why it is safe |
|---|---|
| `thread_id` | an 8-hex id the phone already holds (`THREAD_FIELDS`), needed to open the log; `_ID.fullmatch` or the entry is dropped |
| `project_id` | already in `/m/api/projects` and `/m/api/threads`; `_ID.fullmatch` or null |
| `project` | the name `/m/api/projects` already sends, here also through `_scrub` (stronger than today's route) |
| `title` | `thread_json(t)["title"]` through `_scrub`, exactly as `remote_api.thread` (`:565-569`); `""` if untitled |
| `status` | one of the five activity words, anything else `idle`; membership and status are not secret (WS plan §9 Q7) |
| `last` | an ISO UTC time (only what `parse_time` accepts, else `""`), the same kind of fact as `updated` |
| `seq` | **WS-5c only** (Sol 3): the version Done must carry back; an integer ≥ 0 |

- **`DROPPED`:**
  - `working_entry`: `since`, `rows`, `why_not`, plus `seq` until WS-5c;
  - `working`: `needs_you`, `count`, `pending` (the badge stays `waitingCount` over
    `/m/api/inbox`).
- **Never sent, whatever `working.py` gains** (tests plant each one):
  - `done_at`, `done_seq`, `done_token`, `reason`;
  - the idle limit;
  - `cwd`, `root`, a worktree;
  - `provider_session_id`;
  - any row or detail text.
- Task threads never appear.
- **Parity (narrowed, Sol 4):** the phone's entries equal the HUD's `/working` entries
  re-ordered by P-3, cut to the first 100, minus `DROPPED`, plus `title` and `project`.
- **Stream:** `stream_record` maps `working_changed` to exactly
  `{"kind":"working_changed","data":{}}`. Ids and data are dropped, and `_Hidden` does
  not apply.

## 5. The unlock window (WS-5c)

- **Where it lives:** server-side, in `RemoteAuth`, as
  `_unlocks: {token_sha256: {"passkey": key, "until": mono}}`, beside the session row.
  - It is never a client flag and never written to `sessions.json`. A daemon restart
    locks every phone.
  - It is valid only while the session row exists and is not `_expired`, and only while
    `passkey` is still that session's unrevoked passkey.
- **Clock:** `RemoteAuth.mono` (monotonic, as challenges), with `UNLOCK_S = 3600`,
  pinned by a test. Open means `mono() < until`.
- **Sliding:** every authorised phone action sets `until = mono() + UNLOCK_S`.
  - Reads, the stream, `me`, stt and push subscribe never slide it.
  - The actions that today need only the session (Deny, Withdraw, Start, Resume, a
    message, a question or decision answer, Leave in the review) slide an open window.
    They never need one, so Deny never gets harder than today.
- **What ends it early:**
  - logout (`:961`), the desk's End (`:968`), revoke of its passkey (`:980`), session
    expiry or prune;
  - an unreadable passkeys or sessions file;
  - a daemon restart;
  - **Lock now**, the recommended default: `POST /m/api/lock {}` and a button on the
    phone's header.

  Each of these drops `_unlocks[digest]` in the same locked section.
- **Challenge and verify:**
  - **New routes:** `POST /m/api/unlock/options {}` calls `unlock_options(session)`, and
    `POST /m/api/unlock {assertion}` calls `unlock_verify(session, credential)`.
  - **Subject and family:** the subject is `unlock:<session id>` and the decision is
    `"unlock"`, a new recognizer `_UNLOCK_SUBJECT` and `UNLOCK_DECISIONS`. The family is
    `"unlock"`, with `ANSWER_FAMILIES` and the `MAX_ANSWER_CHALLENGES` bound updated
    (`:176-177`).
  - **Re-asking** replaces the session's earlier unlock challenge (at most 1 per session).
  - **Verify** works as `answer_verify` does today. The challenge is spent first, so a
    replay is 409. Then it checks the subject, decision, session and the session's own
    passkey, the credential id, user handle, UV, origin, rpId and the counter rule, and
    saves the counter before it counts. Only then is the window opened.
  - **The same assertion can do nothing else:** another subject, decision, session or
    passkey is 401.
  - `me` gains `unlock_s`, the seconds left or 0. It is read-only and does not slide.
- **Gating:** one helper, `require_unlocked(session)`, replaces every `answer_verify`
  call:
  - `remote_answer` Allow, Always and deferred Approve and retry;
  - `remote_write` cancel;
  - `pr_merge.phone_act`;
  - `review` Approve selected;
  - the new Done and Undo.

  Closed, it answers **423** `{"error": "locked", "unlock": true}`. It is never 401,
  because `net.ts:20` reads 401 as signed out. `api_route` passes 423's word through. The
  phone then runs the unlock flow and re-sends the action once, with the same body.
- **Per-action checks move to the act (all of them).** The `/options` routes did more than
  issue challenges: they ran refusals and bound what was shown. Each act route now checks
  everything its `/options` used to, in full, at act time:
  - PR: head, card, `actions`, hold and withheld text against the row now
    (`pr_merge.py:862-873`);
  - cancel: not terminal;
  - Allow: still pending, the card shown;
  - deferred: its id and tag;
  - review: exactly the set the body names.

  §4.1 (`remote_secure`) still runs in full before every Allow, Always and PR action.
  Its "passkey" condition becomes "the session's passkey is paired and unrevoked, and its
  unlock window is open".
- **Removed (default):** the per-action `/options` routes and their families (`approval`,
  `task`, `defer`, `review`, `pr`) become 404. If the owner answers §9 Q1 "yes" for some
  actions, those families stay exactly as today, and the window does not cover them.
- **Confirmation sheets stay** (they are not passkeys): Always shows its rule first, and
  cancel has its confirm tap.
- **Done and Undo** (Sol 3):
  - `POST /m/api/working/<id>/done {seq}`: `require_unlocked`, then
    `working.done(id, seq)`. The `seq` is the one the phone drew from the WS-5c read;
    stale, missing or another thread's is 409. IN-8 eligibility (`why_not`) is checked as
    on the desk. The response is `{thread_id, done, token, undo_s}`; the `entry` is
    stripped.
  - `POST /m/api/working/<id>/undo {token}`: the session plus `require_unlocked` plus Done's
    token, within the daemon's 10 s.
  - **Parser:** the phone gets its own parser in `mobile/model.ts`. `lib/working.parseEntry`
    is not reused, because it requires `seq`.
- **Audit:**
  - every phone action stays recorded as the phone's: `via="phone"` on answers and PR
    acts, `remote_write.record` on task verbs, and a "Done from the phone" log line;
  - every unlock is logged ("passkey k-… unlocked phone actions"), and so is each lock
    and its reason;
  - `owner_seen` (QH-3 presence) is called on an authorised action, as it is on a
    verified one today;
  - the desk's Settings > Phone shows each session's "unlocked until hh:mm"
    (`status()`); its End ends the window too.
- **Docs:**
  - **CLAUDE.md, line 114.** Replace `a passkey session; each Allow, cancel, defer and PR
    action a fresh assertion.` (77 bytes) with
    `a passkey session; actions a sliding 1 h unlock opened by one assertion.` (72 bytes).
    The file goes from 39,994 to 39,989 bytes. No test or `slim_claude.py` check pins that
    line (grepped).
  - **The phone design:** PH-4 (`:121`), plus `:228`, `:251`, `:274` and `:344`. Each gets
    an amendment pointing here, as P-0's PH-4 amendment does.
  - **`docs/notes/control-plane.md`:** the per-answer assertions paragraph (`:1609-1622`),
    the cancel paragraph (`:1915`), the PR paragraph (`:2198`), the phone read API and the
    working-set section.
  - **Module docstrings:** `remote_auth` (`:72-78`), `remote_answer` (`:20-23`),
    `remote_write` (`:66-77`), `remote_secure` (`:25`).
- **Tests that pin the old behaviour and must be rewritten:**
  - backend: `tests/v2/remote_answer_check.py` (PH-4 section `:235-336`),
    `remote_auth_check.py`, `remote_write_check.py` (cancel), `quiet_defer_check.py`
    (`:22`, `:1318`), `review_check.py`, `pr_merge_check.py`, `pr_merge_mutants.py`;
  - face: `tests/face/hud_v2_phone_answer_check.py`, `hud_v2_phone_pr_check.py`,
    `hud_v2_phone_review_check.py`, `hud_v2_phone_write_check.py`;
  - unit: `hud/src/mobile/answer.test.ts`, `card.test.ts`, `pr.test.ts`, `review.test.ts`,
    `write.test.ts`.

## 6. Unchanged, explicitly

- **Push:** `push.py` observes only `inbox_changed` (`:564-565`). The payload stays
  `{count, rank}`, nothing goes out from 23:00 to 07:00, and no push is sent for
  `working_changed` or an unlock.
- **Quiet hours:** the deferral and the Review tab are unchanged.
- **NEVER_STEER:** nothing new sends. Phone messages keep `NEVER_STEER`.
- **Agents:** `rules.remote_reading` DENY covers `/m/api/working`, `/m/api/unlock*`,
  `/m/api/lock` and the Done/Undo paths with no rule change. The API and HUD listeners 404
  `/m/*`. The remote listener serves no `/working*`. `is_owner` is never true there.
- **`working_changed` carries no ids:** not on `/events`, not on the phone stream.
- **Existing field allowlists:** `ROW_FIELDS`, `*_DETAIL_FIELDS`, `PROJECT_FIELDS`,
  `THREAD_FIELDS`, `TASK_*` and the existing `DROPPED` tables stay byte for byte. The
  stream's existing kinds keep their shapes.

## 7. Tests

**WS-5a** (`tests/v2/remote_api_check.py`, `working_check.py`):
- **Allowlist:**
  - the keys equal `WORKING_ENTRY_FIELDS` exactly, with top level `{entries, more, total}`;
  - the narrowed parity (§4) holds;
  - planted `done_token`, `done_at`, `reason`, `cwd`, `root`, `worktree`,
    `provider_session_id`, `idle_days`, `rows` and `why_not` never appear, and no byte of
    the token's value does.
- **Scrub:** a credential in a title or a project name comes back withheld.
- **Visibility:** archived threads, threads in archived projects, task threads and
  entries that are out are absent.
- **Cap:**
  - 101 entries, where the oldest is `needs_input`: it is included and first;
  - `more` is true and `total` is 101.
- **`entries()`:**
  - repeated reads call no `inbox.rows()` and change no membership, token or `seq`;
  - `view()["entries"]` equals `entries()` with `rows` and `why_not` added.
- **Gate:**
  - no cookie, after logout, after a revoke, or a session ended mid-read: 401;
  - a wrong Host: 400;
  - POST, PUT, DELETE or HEAD: 405 (403 first without the Origin);
  - a query string: 400.
- **Agent path:** the path joins `reads()` and the DENY test. The API and HUD listeners
  404 it.
- **Stream:**
  - a planted `thread_id` or `data.ids` arrives as exactly
    `{"kind":"working_changed","data":{}}`;
  - flip **both** assertions at `working_check.py:334-336` (the shaped record and `_wanted`).
- **Unchanged:** `working_check.py:1052` and `:1069` still pass.
- **Mutants** (each must fail a test): a field added; title scrub removed; the session
  re-check removed; `data` forwarded; the cap taken before ordering.

**WS-5b** (unit `mobile/*.test.ts`, face `tests/face/hud_v2_phone_working_check.py`):
- **Events:** raw SSE through `onmessage` (Sol 1): `parseEvent` keeps `working_changed`
  and `thread_changed`'s `reason`.
- **Approval resolved:** resolving the last approval with no further thread activity
  updates the label (Sol 2).
- **Serialization:** reversed response completion never restores an older list, and an
  event mid-read gives exactly one re-read (Sol 5).
- **The list:**
  - order;
  - the hint join (rows with no entry, several rows);
  - the 5-row fold and Show all;
  - the truncation notice;
  - hostile titles drawn as text;
  - stale dimming;
  - the badge still `waitingCount`;
  - optional: the 201-row notice.
- **Face check:**
  - the list draws above the unchanged groups;
  - a row tap opens the chat;
  - the hint scrolls to the row;
  - 44 px targets at 375 px;
  - no horizontal scroll;
  - the CSP is unwidened.

**WS-5c** (`remote_auth_check.py`, `remote_answer_check.py`, `remote_write_check.py`,
`pr_merge_check.py`, `review_check.py`, `quiet_defer_check.py`, the face checks), on a fake
monotonic clock with relative expiry:
- **The window:**
  - an action at 59 min passes and slides the window;
  - an action 61 min after the last action is 423 and asks again;
  - an action exactly at `until` is 423.
- **Reads never slide it:** a read, the stream or `me` at 30 min does not extend the
  window.
- **What ends it:** logout, End, revoke, session expiry and Lock now each end it. A
  restart (a new `RemoteAuth`) is locked.
- **Assertions:**
  - one from another session or another passkey is 401;
  - a replayed one is 409;
  - an old per-action challenge cannot open the window;
  - an unlock assertion cannot be used as an answer.
- **Per-action checks still run at act time** with the window open:
  - a PR with a changed head or card, or on hold, is 409;
  - a §4.1 failure is "limited" for Allow, and PR safety files are at the desk;
  - a cancel on a terminal task is 409;
  - a resolved approval is 409.
- **Done:**
  - the wrong thread (another thread's `seq`) is 409;
  - a wrong action (Undo with no Done, or Done's token on another thread) is 409;
  - a stale `seq` is 409;
  - a thread with a pending approval or deferral is 409 with `why_not`, and stays in;
  - Done while locked is 423 and nothing changes.
- **Undo:** a wrong or old token, after 10 s, or after an archive is 409.
- **Recording:** every action is recorded `via="phone"`; Done's log line; `owner_seen`
  is called.
- **Unchanged:** agents are still DENY on every new path; NEVER_STEER is unchanged; there
  is no push on unlock or Done.
- **Mutants:** a read slides the window; the window survives logout or revoke; the
  challenge is not spent; `<` becomes `<=`; the window is not bound to the passkey; the
  act-time PR check is dropped.

## 8. Work packages

| WS | What | Files | Size | Tier |
|---|---|---|---|---|
| **WS-5a** | the read route, `entries()`, stream kind, docs for the read | `jarvis/v2/remote_api.py`, `working.py`; `tests/v2/remote_api_check.py`, `working_check.py`; `docs/notes/control-plane.md` (read API, working set); phone design §5 table | S–M | Sonnet build, **Opus review + Sol**, mutation evidence |
| **WS-5b** | the phone In progress list | `hud/src/mobile/model.ts`, `views.ts`, `app.ts`, `mobile.css`, their `*.test.ts`; `tests/face/hud_v2_phone_working_check.py` | M | Sonnet build, Sol verify |
| **WS-5c** | the unlock window, Lock now, Done and Undo, removal of per-action challenges | backend: `jarvis/v2/remote_auth.py`, `remote_answer.py`, `remote_write.py`, `pr_merge.py`, `review.py`, `remote_secure.py`, `remote_api.py` (`seq`); `tests/v2/*` above. UI: `hud/src/mobile/net.ts`, `card.ts`, `write.ts`, `review.ts`, `app.ts`, `views.ts`, their tests; the face checks above; HUD Settings > Phone. Docs: CLAUDE.md, phone design, `control-plane.md` | L | **Opus build, Opus review + Sol**, mutation evidence |

**Dependencies:**
- **WS-5a merges first.**
- **WS-5b and WS-5c's backend can build in parallel:** no shared files (`hud/src/mobile`
  against `jarvis/v2` + `tests/v2`). 5c's `remote_api.py` change rebases on 5a.
- **WS-5c's UI touches `app.ts` and `views.ts`**, so it goes on top of 5b.
- **WS-5c merges as one PR, backend and UI together.** The per-action `/options` routes
  disappear, so a split would break the installed phone.

Each PR needs: Sol verification, then the owner's OK, then the merge, then
`jarvis hud install`. 5a and 5c also need a daemon restart when no turn is running (for 5c
this locks every phone, as designed).

## 9. Owner questions (recommended default first)

1. Inside the unlock window, should PR merge, Always, or Allow on a command the gate flags as dangerous still ask for the passkey each time? **(Recommended) No extra passkey: full permissions, as you asked** / yes for those three.
2. A "Lock now" button on the phone? **(Recommended) Yes** / no, the hour lapses on its own.
3. Should a window that keeps sliding stop after a hard limit? **(Recommended) No: it is bounded by the 7-day session and ends on logout, End or revoke** / yes, after 12 h of use.
4. Show each phone's unlock on the desk (Settings > Phone, "unlocked until 14:05")? **(Recommended) Yes** / no.
- *Defaults, not owner-confirmed:*
  - no "leaves in" time. A server-computed departure time is possible later without
    sending the idle limit.
  - order: the desk's, applied before the 100 cap.

## 10. Review record (revision 2)

| # | Sol finding (cx-ws5) | Where it landed |
|---|---|---|
| 1 MED | `parseEvent` drops `working_changed` | WS-5b changes `StreamEvent`/`parseEvent` (`model.ts:189`), with raw SSE tests (§7) |
| 2 MED | `approval_resolved` leaves labels stale | P-4: re-read on `inbox_changed` too; test (§7) |
| 3 MED | no drawn `seq` for Done | `seq` added to the read in WS-5c only; a phone parser of its own; recognizer, family and dispatch spelled out (§5) |
| 4 MED | the 100 cap against "same threads" | P-3: desk order before the cap, `total`, a truncation notice, narrowed parity, the 101 test |
| 5 MED | refresh concurrency | P-4: one in flight plus a pending re-read, latest-read check; tests |
| 6 LOW | IN-8 absolute claim | P-6: qualified; the 200-row gap recorded; optional notice in WS-5b |
| Q1 wording | "costs nothing that matters" | moot: the owner chose the unlock window (§2) |
| Q5/Q6 | overstated, uncapped | §9 defaults, reworded |
