# Plan: WS-5, the working set on the phone, and the phone's 1-hour unlock

**Status (2026-11-15): plan, revision 6, plus Sol's revision-6 delta (the op outcome
mapping, §5.3a).** Revision 6 takes in Sol's revision-5 review: the §5.3a op-token details
only. The r6 delta (built into WS-5c's backend): "update requested" is not "Done", and the
PR code list is complete.
- **Revision 3** took in the owner's WS-5c answers (§2) and Sol's revision-2 review.
- **Revision 4** took in Sol's revision-3 review.
- **Revision 5** takes in Sol's revision-4 review (§10). It replaces only §5.3a, the
  operation-id contract, and its tests. Everything else Sol confirmed as fixed.
- **Baseline:** written against `main` at `7215a5cf`.
- **Vocabulary:** the words of `docs/plans/2026-11-14-inbox-working-set-plan.md` (entry,
  label, Needs you, D-n).
- **Packages (§8):** WS-5a, the read route; WS-5b, the phone UI; **WS-5c, the unlock window
  plus Done and Undo** (the boundary package; it replaces PH-4's fresh assertion per
  action).

## 0. What the owner gets

- **An "In progress" list at the top of the phone's Inbox tab.**
  - The first line is "Needs you · N" (`waitingCount`, 0 included). Tapping it scrolls to
    the rows.
  - Next come the first 5 threads, then "Show all". Each row shows title, project, label,
    age and a hint such as "1 approval".
  - Tapping a row opens the chat; tapping the hint scrolls to that chat's first row.
- **Today's rank groups stay below, unchanged.**
- **Unlock once, act for an hour (WS-5c).**
  - **What it unlocks:** one passkey assertion unlocks every gated action: Allow, Always,
    cancel, PR actions, deferred Approve and retry, review Approve selected, Done and Undo.
  - **Sliding:** each action restarts the hour.
  - **Unlock-and-act:** when the window has lapsed, the first gated action raises Face ID,
    and the action goes through in the same gesture.
  - **Lock now** ends the window. The desk shows "unlocked until HH:MM".
  - **Session-only actions stay session-only:** Deny, Withdraw, Start, Resume, messages and
    answers.
  - **Reads** stay on the existing session (7 days, 12 h idle).
- **Unchanged:** push stays `{count, rank}`, with nothing from 23:00 to 07:00. Quiet hours,
  the morning review, NEVER_STEER, and agents never reaching `/m/*`.

## 1. What this builds on (current code)

| Piece | Where |
|---|---|
| Phone reads | `jarvis/v2/remote_api.py` (SELF_PROTECTED): routes `:915-936`, GET only, session re-checked `:976-978`, `*_FIELDS`/`DROPPED` `:140-262`, `stream_record` `:740-770`, `STREAM_KINDS` `:276`, `INBOX_ROWS = 200` `:115`, `:548-551` |
| Owner test | `remote_auth.owner_status` `:1109-1131`; `api_route` `:1173`, dispatch `:1222-1256` |
| Per-action assertions today | `_issue`/`_spend` `:575-590` (spend pops before checks); `answer_options` `:746-790` (recognizers `:755-762`, family `:779-781`); `answer_verify` `:792-838`, entirely under `self._lock` with the counter saved before success; `ANSWER_FAMILIES` `:176-177`; `CHALLENGE_S` monotonic `:130`. Callers: `remote_answer.py:319/330/401/435`, `remote_write.py:504/517`, `pr_merge.py:875/891`, `review.py:531/537` |
| Local error catches | `remote_answer.py:494`, `remote_write.py:630`, `pr_merge.py:940`, `review.py:552`: each turns `AuthError` into `{"error": word}` and maps any status other than 409/429/503 to "unauthorized" |
| Always's rule | only from `answer/options` (`remote_answer.py:321-331`: `always_rule` → `rule`, `rule_words`, `resolution`); the phone reads it there (`app.ts:640-657`) |
| Deferred binding | `defer_binding` = `defer:<id>` (`remote_answer.py:277-287`), with no tag. The review's set binds ids plus tags (`review.subject`). A deferred row's phone detail has no tag (`remote_api.py:191`) |
| §4.1 | `remote_secure.check_passkey` `:258-264` = `passkey_ready(session)`: readiness only. Used for reads too (private-card detail, `remote_answer.py:228-236`) |
| Sessions | `_load` caches once it succeeds (`:400-404`); `_require_usable` `:486-492`; `_expired` `:893`; `session_for` `:909`; `session_live` `:938`; `logout` `:961`; `end_session` `:968`; `revoke` `:980` |
| Working set | `view()` `:1078-1103` orders by `(last, thread_id)` descending (`:1101`); `done` `:813` (400 for a malformed id or `seq`, 409 when stale or out); per-thread `seq` that starts at 0 or 1 (`:1041`, `:501`); `UNDO_S` 10 s |
| Phone app | `model.ts:189` `parseEvent`; `app.ts` `handle()` `:1322-1340`; `net.ts:20` maps any 401 to signed out |
| Isolation scan | `tests/v2/archive_check.py:1020-1025` blacklists `answer_verify`, `answer_options`, `owner_verified` outside SELF_PROTECTED |

## 2. Owner's answers

**2026-11-15, first round:**
- Build WS-5: backend first.
- Add `working_changed` (no ids, empty data) to the stream.
- Placement: top (the Needs you line, 5 threads with Show all, then today's rows).
- A 1-hour sliding unlock window replaces the per-action fresh assertion. Reads stay on the
  session.

**2026-11-15, second round (WS-5c):**
- No extra passkey inside the window for PR merge, Always, or Allow on a command the gate
  flags as dangerous.
- A Lock now button: yes.
- No hard stop beyond the session.
- The desk shows "unlocked until HH:MM".
- Unlock-and-act: when the window has lapsed, the first action raises Face ID and then
  goes through in the same gesture.
- Deny and the other session-only actions stay session-only. They slide an open window but
  cannot reopen one.
- Undo is gated by the window.

**Defaults, not owner-confirmed:** no "leaves in" time; the desk's order.

## 3. Decisions for WS-5a/b (read path)

- **P-1. Additive.** Today's rank groups are untouched. WS-3's regrouping is not done on the
  phone.
- **P-2. No inbox row data in the read.** Hints join the phone's own `/m/api/inbox` rows by
  `thread_id`, the D-5 rule. A hint can lag one refresh; the row is always drawn.
- **P-3. Order, then cap** (Sol r1-4, r2-7).
  - **Visible set:** the server builds the visible, valid entries and counts them (`total`).
  - **Order:** the desk's.
    1. By label: needs_input, failed, unread, working, idle.
    2. Then by `last`, newest first.
    3. Then by `thread_id`, descending. This is the daemon's own tie-break (`working.py:1101`);
       the desk's sort keeps it for ties.
  - **Cap:** the server takes the first `WORKING_ENTRIES = 100`, and `more = total > 100`,
    from that same collection.
  - **When cut:** the expanded list ends with "Showing 100 of N: the rest are at the desk".
    There is no pagination.
- **P-4. Freshness.**
  - **One read at a time:** one read in flight, plus one pending re-read
    (`state/working.ts:80-96`); only the latest read's response is applied.
  - **What triggers a re-read:** `working_changed`, `inbox_changed`, and `thread_changed`
    when the reason is `turn_started` or `turn_finished`; also opening the tab, the stream
    reopening, coming back online, and `resync`.
  - **Debounce:** one key, 1 s.
  - **Remaining lag:** a desk `seen` and `queue_cleared` show at the next read.
- **P-5. 375 px.**
  - **Rows:** each row is a 44 px target. The title takes one line with an ellipsis; the
    second line is label · project · age. The hint is its own 44 px chip.
  - **Colour:** amber only for pending, red only for *Failed*.
  - **Content:** text only.
  - **Empty and offline:** "Nothing in progress" when empty; dimmed when offline.
  - **Show all:** its state is kept per device, with storage access wrapped in try/catch.
- **P-6. IN-8, qualified.**
  - **What WS-5 keeps:** today's mark and group behaviour exactly.
  - **An inherited gap:** `/m/api/inbox` cuts at 200 rows, and the app ignores `more`
    (`app.ts:315`).
  - **Optional fix in WS-5b:** a "More waiting than the phone shows: open the Inbox at the
    desk" notice, tested at 201 blocking rows.

## 4. `GET /m/api/working` (WS-5a)

**Where it lives.**
- `remote_api.working(daemon)` is added to `_match`. A query string is a 400; anything but
  GET is a 405.
- It reads from a new read-only `WorkingSet.entries()`, which does no `inbox.rows()` read.
  `view()` builds on `entries()`, with its single `marks="flag"` read, the exactly-once
  row attachment and `why_not` all unchanged.

**Errors.** 503 when `daemon.working` is None. 409 "the In progress list cannot be read now"
when `entries()` raises.

**Response.** `{"entries": [...], "more": bool, "total": int}`.

| Field | Why it is safe |
|---|---|
| `thread_id` | an 8-hex id the phone already holds, needed to open the log; the entry is dropped unless `_ID.fullmatch` |
| `project_id` | already sent by `/m/api/projects` and `/m/api/threads`; null unless `_ID.fullmatch` |
| `project` | the name `/m/api/projects` sends, here also through `_scrub` |
| `title` | `thread_json(t)["title"]` through `_scrub`, as `remote_api.thread` does (`:565-569`) |
| `status` | one of the five activity words; anything else becomes `idle` (§9 Q7 of the WS plan: not secret) |
| `last` | ISO UTC (what `parse_time` accepts, else `""`) |
| `seq` | **WS-5c only:** the thread's own version for Done, an integer ≥ 0 |

**What is left out.**
- `DROPPED["working_entry"]`: `since`, `rows`, `why_not`, and `seq` until WS-5c.
- `DROPPED["working"]`: `needs_you`, `count`, `pending`.
- **Never sent** (each one planted in a test): `done_at`, `done_seq`, `done_token`, `reason`,
  the idle limit, `cwd`, `root`, worktree, `provider_session_id`, row or detail text.
- Task threads never appear.

**Parity.** The phone's entries are the HUD's `/working` entries, ordered by P-3, cut to 100,
minus `DROPPED`, plus `title` and `project`.

**Stream.** `working_changed` becomes exactly `{"kind":"working_changed","data":{}}`.

## 5. The unlock window (WS-5c)

### 5.1 State and clock

- **Where it lives:** `RemoteAuth._unlocks: {token_sha256: {"passkey": key, "until": mono}}`,
  plus `_unlock_gen: {token_sha256: int}`.
  - In memory only; never a client flag, never in `sessions.json`.
  - A daemon restart locks every phone.
- **When it counts:** only while the session row exists, `_require_usable()` passes, the
  row is not `_expired`, and `passkey` is still that session's unrevoked passkey.
- **Clock:** `RemoteAuth.mono`, `UNLOCK_S = 3600` (pinned). Open means `mono() < until`.
- **No hard stop.** The 7-day session bounds it (owner).
- **Sliding:**
  - A gated action (§5.3) runs `require_unlocked`, which slides the window.
  - A session-only action (Deny, Withdraw, Start, Resume, a message, a question or decision
    answer, review Leave) slides an **open** window through `touch_unlock` and never opens
    one.
  - Reads, the stream, `me`, stt and push subscribe never slide it.
- **What ends it:**
  - Lock now (`POST /m/api/lock {}`, a header button on the phone);
  - logout, the desk's End, a revoke, session expiry or prune;
  - a restart.

  Every ending runs `_end_unlock(digest)` under `self._lock`. **The fix for Sol r2-1 (HIGH):**
  `_end_unlock` drops `_unlocks[digest]`, **deletes every outstanding `unlock` challenge of
  that session**, and bumps `_unlock_gen[digest]`.

### 5.2 Unlock challenge and verify

- **Routes:**
  - `POST /m/api/unlock/options {}` → `unlock_options(session)`.
  - `POST /m/api/unlock {assertion}` → `unlock_verify(session, credential)`.
- **Challenge:** kind `"unlock"`, subject `unlock:<session id>`, decision `"unlock"`.
  - A new `_UNLOCK_SUBJECT` and `UNLOCK_DECISIONS`.
  - Family `"unlock"` in `ANSWER_FAMILIES`, with the `MAX_ANSWER_CHALLENGES` bound
    recomputed.
  - The challenge records the session's `_unlock_gen` at issue.
  - Re-asking replaces the session's earlier unlock challenge (at most 1 per session).
- **Verify** holds `self._lock` from start to finish, as `answer_verify` does.
  1. It spends the challenge first (unknown, used, expired or replaced: 409).
  2. It checks the subject, decision, session, generation (`_unlock_gen` still the issued
     value, else 409), the session's own passkey, credential id, user handle,
     `crossOrigin`, origin, rpId, UV, signature and counter rule.
  3. It saves the counter. A failed save is 503 and leaves the window **closed**.
  4. Only then does it open the window.
- **Lock now racing verify.** Both hold `self._lock`, so there are two orderings, and both
  end locked unless a new assertion is made:
  - Lock first: the challenge is gone, so 409, even for an assertion signed before the
    Lock and delivered after it.
  - Verify first: the window opens, then Lock closes it.

  The generation check is defence in depth, should the lock scope ever change.
- **`me`:** gains `unlock_s` (seconds left). It is read-only and does not slide the window.

### 5.3 Gated actions and the 423 contract

- **The gated routes:**
  - Allow and Always (`remote_answer`);
  - deferred Approve and retry;
  - cancel (`remote_write`, PR-fix cancel included);
  - every PR action (`pr_merge.phone_act`, Fix included);
  - review Approve selected;
  - Done and Undo.

  Each calls `require_unlocked(session)` in place of `answer_verify`.
- **Order inside each.** It is explicit, and each path is reordered to match.
  1. Body shape, including the op token's shape (§5.3a): 400.
  2. `require_unlocked`: 423 when closed. Nothing else runs: no lookup, no rate token, no
     dedup key.
  3. The op token's signature and session (§5.3a), outside the lock: 400 for another
     session's token, 410 for a forged or pre-restart one.
  4. Admission under the `Ops` lock, as one step, with **one** `now = mono()` snapshot, in
     this order:
     - **the token's time:** `t ≤ now + 1` and `now < t + OP_TTL_S`, else 410 (Sol r5-1);
     - **pruning,** with the same `now`;
     - **a duplicate:** 409, with no rate token spent;
     - **the rate limit and capacity:** checked by `Rate.take_if(digest, now, ok)`, a new
       method (Sol r5 note). Under `Rate`'s lock it checks the window (429) and then calls
       `ok()`, the `Ops` capacity check (429), and records the write only when both pass.
       So a capacity refusal spends no rate token. Today's `Rate.take`
       (`remote_write.py:216`) records at once. `remote_write.py:615`, which today takes
       the rate before cancel, moves here.
     - **admission:** the op is recorded as `running`.

     The lock order is always `Ops` then `Rate`. **Since #374's fix round** (Sol's MED,
     a barrier probe: End retired the session while a Cancel waited between its
     signature check and admission, and the Cancel then ran and left an `Ops` record
     for the ended session): admission runs **under the auth lock**, immediately after
     a final check that the session is still live, its passkey unrevoked (401) and its
     window still open (423), which never slides it. Every retirement holds the same
     lock and cleans the session's `Ops` in it, so it lands wholly before admission
     (401, nothing admitted) or wholly after (its cleanup takes the new record). The
     lock order is auth → `Ops` → `Rate`; nothing holding `Ops` or `Rate` takes auth.
  5. The act-time checks (§5.4): 409. That includes today's approval, deferral and task
     lookups (`_request`, `_deferral`, `_task`), which today run before verification.
  6. The mutation. `ticket.mutating()`, called immediately before it, checks once
     more under the auth lock, without sliding, that the session is live and the window
     open (Opus LOW-1 on #374: End, a revoke or Lock now landing during a slow act-time
     check or a PR's fresh read). A refusal there is 401 or 423, passes through every
     family's catch-all, and the op settles `refused` (a retired session's record is
     already gone, and nothing makes it again).
- **The 423 contract (Sol r2-2).** `remote_auth.Locked(AuthError)` has status 423, and
  `remote_auth.locked_reply()` is exactly `(423, {"error": "locked", "unlock": true}, [])`.
  - **Who catches it:** the local catches at `remote_answer.py:494`,
    `remote_write.py:630`, `pr_merge.py:940` and `review.py:552`, the new working routes,
    and `api_route`. Each catches `Locked` **before** `AuthError` and returns
    `locked_reply()`.
  - **Never 401:** `net.ts:20` reads 401 as signed out.
- **Removed:** the per-action `/options` routes and the families `approval`, `task`,
  `defer`, `review` and `pr` (404). `answer_options` and `answer_verify` go.
- **`verified=True` on `review.approve`** is renamed `unlocked=True`.

### 5.3a Operation tokens: replay protection without per-action challenges (Sol r3-1, r4-1, r4-2)

**Why.**
- A spent challenge made a second identical PR Update a 409, so `gh` ran once
  (`pr_merge.py:891`, `pr_merge_check.py:1070`). Head and card checks cannot replace that.
- **The rule:** forgetting a record must never turn an old operation into a new one.

**The choice: (b), server-issued tokens that expire and die with the process.** These are
the reasons:
1. **Retention is bounded by the token's own life**, not by the session's. A sliding window
   can stay open for days, and so can a session; per-session tombstones (option a) would
   grow with both.
2. **Restart protection comes for free.** The signing key is random per process, so every
   pre-restart token fails verification. Option (a) would need tombstones persisted, or a
   persisted epoch, written on every action.
3. **The server sets the issued-at time**, so the client cannot extend a token's life by
   claiming a later time.

**Issuing.** `GET /m/api/ops/tokens` returns `{tokens: [4 tokens], ttl_s}`.
- **The request:** session only, a read. It never slides the window. It is rate-limited
  separately (`OP_ISSUE_RATE = 30` per minute per session) and has its own final
  `session_live` re-check.
- **Token** = `"o1." ‖ b64url(payload) ‖ "." ‖ b64url(HMAC-SHA256(OP_KEY, "jarvis-phone-op-v1\0" ‖
  payload))`.
  - The payload is `{sid: session row id, n: 16 random bytes, t: mono() at issue}`.
  - `OP_KEY` is 32 random bytes made when `RemoteAuth` starts, held only in memory.
- **Issuing stores nothing.** Only use does.
- **`OP_TTL_S = 600`.** It is pinned.

**On the phone.**
- It keeps a pool of up to 4 tokens. It refills the pool when one is used, when the oldest
  passes `OP_TTL_S / 2`, when the app becomes visible, and after a 410.
- **A tap takes a token from the pool,** so there is no round trip before Face ID. The
  token is frozen with `{method, URL, body}`.
- **The unlock never refreshes the frozen token.** An unlock that outlives the token gets
  410, and the phone shows "tap again".

**Verification.** Each check runs in order, and the first failure answers. Checks 1–3 are
step 3 of §5.3; check 4 is step 4.
1. The token is well formed (else 400).
2. The HMAC verifies, with `compare_digest`. Otherwise 410 "this tap is from before Jarvis
   restarted, or is not genuine: nothing was done by this request".
3. `sid` is this session's id (else 400).
4. **Inside the admission critical section,** with the snapshot that pruning also uses, the
   token must be in date: `t ≤ now + 1` and `now < t + OP_TTL_S`. Otherwise 410
   `{"error": "this tap expired: nothing was done by this request", "expired": true}`.
   - **Why inside the lock:** a replay checked just before expiry and then left waiting
     for the lock could otherwise find its expired record pruned and its nonce unused.
   - **Pinned tolerance:** `t = now + 0.5` passes; anything past `now + 1` is refused.

**The `Ops` store.**
- **Key and digest.** The key is `(session digest, n)`. The digest is sha256 of the method,
  the path and the canonical body without the token.
- **Record:**
  `{state, digest, reservation, expires: t + OP_TTL_S, outcome, code}`.
  - `state` is the execution state: `running`, `completed`, `refused` or `uncertain`.
  - `reservation` is 16 random bytes, made at admission.
  - `outcome` and `code` are the confirmed result, set at completion (Sol r5-2,
    recommended over a bare "Finished"):
    - **`outcome` is `ok`, `failed` or `unknown`.** It is `ok` only when the route's own
      result says so: a 2xx with `ok` true, or a 2xx for a family whose answer has no
      `ok` field (Allow, Always, cancel, Done, Undo, review).
    - It is `failed` for `ok: false`, for example the PR result "not updated"; and for
      "changed", which is not a normal return but the 422 exception path
      (`pr_merge.py:723`), answered 409 after gh returned.
    - It is `unknown` for "not read again" (`pr_merge.py:625`), for the 409 uncertain
      reply, and whenever the result cannot be read. **`unknown` for "not read again"
      takes precedence over the generic `ok: false` → `failed`** (Sol r6-2).
    - **`code`** comes from a fixed per-family allowlist of code-written words (PR, every
      current `PrActions._outcome` result (Sol r6-2): `merged`, `updated`,
      `update_requested`, `not_updated`, `not_read_again`, `changed`, `queued`,
      `head_moved`, `closed`, `not_mergeable`; approval: `allowed`, `always_written`;
      task: `cancelled`; working: `done`, `undone`; review: `approved`). Anything else is
      `other`. The HUD's `resultOk` (`prRow.ts:364`), which also accepts `queued`, is not
      reused.
    - **An accepted Update is not "Done" (Sol r6-1, MED).** An HTTP 202 can give
      "update requested" with `ok: true` while the head is unchanged or the follow-up
      read failed (`pr_merge.py:628`, `:645`, `:726`): that confirms acceptance, not
      completion. It is stored as `outcome: ok`, `code: update_requested`, and the status
      route and the phone show "Update requested; check at the desk" for it.
    - **No model text and no response body is stored.**
- **One lock** serialises init, admission, state changes and pruning.
- **Bounds:** `OPS_PER_SESSION = 64` and `OPS_MAX = 64 × MAX_SESSIONS = 2048`.
  - At either bound, admission is 429 "too many actions are still recorded; wait a
    minute".
  - **Nothing is evicted, and nothing changes.** For scale, `RATE` allows 30 writes a
    minute.
- **Admission (step 4):**
  - **No record:** the op is admitted as `running`.
  - **The same key with the same digest:** 409
    `{"error": "already sent", "duplicate": true, "state": <state>}`. The duplicate never
    runs.
  - **The same key with another digest:** 409 "op token reused".
- **States, all terminal except `running`.** A route marks `mutating` immediately before
  its mutation call.
  - An exception, or a definite refusal (an act-time 409, a lookup failure), **before**
    `mutating` → `refused`.
  - The mutation returned → `completed`. This includes an exception afterwards, while the
    response is built (`_after`): that reply stays the 409 uncertain it is today, but the
    state is `completed`.
  - An exception or interruption **after** `mutating`, before the mutation returned →
    `uncertain`.

  **No state ever releases the token.** A refused op is retried only by a new tap with a
  new token. A 400, 423, 410 or 429 never admits, so the token stays usable for the
  authorised retry.
- **Reservation identity.** A state change names its record's `reservation`. One that does
  not match (a record already pruned, or a different record) changes nothing.
- **Pruning** runs at admission time, under the lock, with admission's `now`.
  - It drops a record only when `now ≥ expires` **and** its state is not `running`.
  - A `running` record is never pruned and never ages into permission: its token is
    refused (410) by the same snapshot's time check before pruning runs.
  - It also drops every record of a retired session (§5.8). That session's tokens can
    never verify again, because `sid` no longer exists.
- **Op status (Sol r5-3).** `GET /m/api/ops/<n>` returns `{state, outcome, code, words}`.
  `words` (added as built, #374) is `remote_auth.op_words(state, outcome, code)`: one of
  the six fixed phrases listed under "What it shows" below, never model text.
  - **Lookup:** only by the **authenticated session's** digest plus `<n>`. Another session's
    nonce is `unknown`, exactly as a missing one is.
  - **Final re-check:** `session_live`, before the answer leaves. End or revoke during the
    read is 401.
  - **Never acts:** it is read-only, never executes, never slides the window. A query string
    is 400 and any other method is 405.
  - **`unknown`** means pruned, restarted, never admitted, or not this session's.

**On the phone, after a lost response or a duplicate.**
- **Never success:** the phone never treats `duplicate` as success.
- **Never a fresh token:** it never mints a new token for the same tap.
- **What it shows:** it asks op status and shows one of:
  - "Done" — only for `completed` plus `ok`, and never for `update_requested`;
  - "Update requested; check at the desk" — for `completed` plus `ok` plus
    `update_requested` (Sol r6-1);
  - "Not done" — for `refused`, or `completed` plus `failed`; the code is shown in fixed
    words;
  - "Still running";
  - "Finished; check at the desk" — for `completed` plus `unknown`;
  - "Unknown: check at the desk" — for `uncertain` or `unknown`.
- **Uncertainty is kept** across a 410 as well.

**Every gated family carries a token** (recommended): Allow, Always, deferred Approve and
retry, cancel, PR actions including Fix, review Approve selected, Done and Undo.

**What it is not.** This is replay and accident protection, not authority. An unlocked
client with the cookie can get fresh tokens: that is the window's authority (§5.4).

### 5.4 Act-time checks that replace what `/options` did (Sol r2-3)

Every refusal and binding the `/options` routes made now runs in full at act time:
- **PR:** `pr_merge.py:862-873` (head, card, `actions`, hold, holds file, §4.1 flags,
  withheld text), against the row now. Then `PrActions.act`'s own fresh read and judge.
  - The body is the **tapped** `{id, action, head, card}`, as `app.ts:746` already
    builds it.
  - Neither the client nor the server ever substitutes freshly fetched head or card
    values.
- **Cancel:** not terminal.
- **Allow:** still pending, `_gate`.
- **Review:** exactly the set the body names, ids and tags (`review.subject`'s input), with
  its locked-store recheck.
- **Deferred Approve and retry (recommended: narrow the tag to the review).**
  - It binds the deferral id from the URL. At act time, the full `_deferral` lookup runs
    (still waiting, grantable, not expired), then `_gate_deferral` (private or limited).
    `_gate_deferral` alone is not enough.
  - The review keeps its locked-store tag and all-or-nothing checks
    (`review_check.py:582`, `:595` kept).
  - No tag is added to the individual deferred read, so `DETAIL_FIELDS["deferred"]` stays
    frozen.
  - This matches today: `defer_binding` carries no tag either.
- **Always: a challenge-free rule preview.**
  - **Routing (Sol r3-2).** `GET /m/api/inbox/<id>/answer/rule` is matched in
    `remote_answer._match`, as `"rule"`. It sits beside `detail` in the GET branch:
    `want = "GET"` for both. It has its own final
    `auth.session_live(...)` re-check before the answer leaves (401 otherwise), exactly as
    `detail` has (`remote_answer.py:471-484`).
    - Any other method is 405.
    - Any query string is 400.
    - It needs only the session. It works while locked and never slides the window.
  - **Checks.** It runs the checks `options` ran: `_request` (pending), §4.1, then `_gate`
    (private while limited, Always while limited, not allowlistable, scrub-changed card).
    A deferred row is 409 `NO_ALWAYS`.
  - **Response allowlist:** exactly `RULE_FIELDS = ("rule", "rule_words", "resolution",
    "preview")`.
  - **The `rule` entry:** keys only from `RULE_ENTRY_FIELDS = ("tool", "prefix")`, scalar
    strings, or null when no rule can be written.
  - **Withholding:**
    - An entry with any other key is not shown: 409 "this rule is shown only at the desk".
      The desk keeps the full entry.
    - The same holds when `_scrub` would change `tool`, `prefix`, `rule_words` or
      `resolution`.
    - So the phone never shows a partial rule.
  - **`preview`** = sha256(`"jarvis-phone-always-v1\0"` ‖ req_id ‖ canonical JSON of the
    **raw** entry ‖ `"\0"` ‖ resolution), 64 hex.
  - **The Always act** sends `{decision: "always", preview, op}`.
    - The server recomputes `always_rule` for that request.
    - A missing or malformed `preview` is 400.
    - A different digest is 409 "the rule changed: read it again".
  - **What the preview binds (Sol r3 §9-2, stated plainly).** The preview binds **rule
    freshness**: the rule written is the rule that this request has now. It does **not**
    prove that a human saw it. The digest is public, and an unlocked client can fetch or
    compute another valid preview and submit it.
    - That is the authority the unlock window transfers, by the owner's choice: inside the
      hour, the session is the owner's authority for every gated action.
    - Restoring signed per-action confirmation would need the per-action assertion the
      owner declined.

### 5.5 §4.1 split by reads and actions (Sol r2-4)

- **`remote_secure` is unchanged:** `check_passkey` stays `passkey_ready(session)`, which is
  readiness. It serves reads (private-card detail), `me`, background checks and actions
  alike.
- **The window is enforced only by `require_unlocked`**, separately from the monitor.
  `remote_secure`'s docstring (`:25`) loses "the assertion is fresh" and points to it.
- **Limited authority** (§4.1 failing) behaves as today, inside or outside the window.

### 5.6 Done and Undo (Sol r2-5)

- **`POST /m/api/working/<id>/done {seq}`.**
  - **The binding:** the URL's thread id plus **that thread's** drawn `seq`, both captured
    at the tap and frozen across 423 → unlock → retry.
  - **What the binding claims:** `seq` is per thread and carries no provenance. The
    binding claims no more than "Done on the thread in the URL, as drawn". The owner's own
    tap names the thread, so an opaque thread-bound version is not added.
  - **Statuses:** as `working.done`. A malformed id, or a missing or malformed `seq`, is
    400. Stale, or not in the set, is 409 with the daemon's words. A linked rank 1–4 row
    or a running turn is 409 `{error, why_not}`: both the daemon's reason through the
    phone's scrub (`why_not` added in #374's fix round, Sol LOW-2). A stale or out
    refusal is `{error}` only.
  - **Response:** `{thread_id, done, token, undo_s}`.
- **`POST /m/api/working/<id>/undo {token}`.** Session, window, and Done's token within
  10 s.
- **Phone parser:** the phone has its own parser. It does not reuse
  `lib/working.parseEntry`.

### 5.7 Unlock-and-act on the phone (owner)

- **Frozen request:** each gated tap freezes `{method, URL, body}`, with its op token from the pool. The
  URL carries the thread or row id captured at the tap.
- **When the phone thinks it is locked** (`unlock_s` from `me`, plus a local timer), it
  skips the doomed action: options → `navigator.credentials.get` (Face ID) → unlock → the
  frozen action. That is the same number of awaits before the passkey as today's
  options-then-passkey.
- **When it thinks it is unlocked:** it sends the action. A 423 (the desk ended the
  session's window, a restart, Lock elsewhere) runs the same unlock, then **one** retry of
  the frozen request.
- **No retry** after a cancelled Face ID, a 409 or 503 from unlock, an ambiguous network
  failure (shown as "may not have gone through", as `MAYBE_SENT`), or a second 423.
- **SSE updates and navigation** during the unlock never change the frozen request. The
  server's act-time checks refuse what went stale.
- **Gesture risk:** iOS Safari may refuse `credentials.get` if the user gesture's
  activation has lapsed after the 423 round trip. The locked-path shortcut avoids this in
  the common case; the 423 path needs a real-iPhone check (§8).
  - **If a fallback "Tap to unlock" is needed,** it carries the **same** frozen operation
    and op token, and sends it once.

### 5.8 Audit, desk, docs

- **Audit:** every gated action stays recorded as the phone's: `via="phone"`,
  `remote_write.record`, and a "Done from the phone" log line. Each unlock, each lock and
  its reason are logged. `owner_seen` (QH-3) runs on each authorised gated action.
- **Desk:** Settings > Phone shows "unlocked until HH:MM" per session (`status()`); End ends
  the window too.
- **Runtime file damage.** `_load()` caches, so damage to `passkeys.json` or
  `sessions.json` while the daemon runs is not noticed, for sessions as for windows. That
  is today's rule: memory is authoritative.
  - The revision-2 claim that "an unreadable file ends it" is withdrawn.
  - The window follows the in-memory session and passkey. `_require_usable()` (`_broken`,
    host) is checked on every `require_unlocked`.
  - A loaded-instance test pins this policy (§7).
- **Every ending path calls `_end_unlock`** (Sol r3 ending list):
  - `_session_passkey` (`:739`, the session has ended);
  - `_prune_sessions` (`:898`, expiry, revoked passkey and `MAX_SESSIONS` eviction);
  - `session_for` (`:928`, an expired or revoked row deleted);
  - `session_live` (`:950`, finding it ended);
  - logout (`:961`), End (`:968`), revoke (`:1003`, every session of the key);
  - Lock now.

  A restart starts with empty unlock, challenge and `Ops` state and a new `OP_KEY`.
- **Retiring a session** (every path above except Lock now) also removes that session's
  `_unlock_gen` entry and its `Ops` records (admission holds the same lock: §5.3 step 4),
  in the same locked step that deletes its
  challenges. Lock now keeps the session, so it bumps `_unlock_gen` instead.
- **Revoke ordering (Sol r4).** `revoke` does three things in memory, under the lock,
  **before** `_save_passkeys`:
  1. it marks the passkey revoked;
  2. it removes every session of that key;
  3. it runs `_end_unlock` and the retirement cleanup for each.

  The save can fail with 503. The revoke then stays in force in memory, as today's
  `unsaved` rule already does.
- **CLAUDE.md, line 114.** Replace
  `a passkey session; each Allow, cancel, defer and PR action a fresh assertion.`
  (77 bytes) with `a passkey session; gated actions a sliding 1 h passkey unlock.` (62 bytes).
  The file goes from 39,994 to 39,979 bytes. No test or `slim_claude.py` check pins the
  line.
- **Phone design:** PH-4 `:121`, plus `:228`, `:251`, `:274`, `:344`, each amended to point
  here.
- **`control-plane.md`:** `:1609-1622`, `:1915`, `:2198`, plus the read API and
  working-set sections.
- **Module docstrings:** `remote_auth` `:72-78`, `remote_answer` `:20-23`, `remote_write`
  `:66-77`, `remote_secure` `:25`.

## 6. Unchanged, explicitly

- **Push:** `push.py` observes only `inbox_changed`. It sends `{count, rank}`, nothing from
  23:00 to 07:00, and no push on `working_changed`, an unlock or Done.
- **Quiet hours:** the deferral and the Review tab are unchanged.
- **NEVER_STEER:** nothing new sends.
- **Agents:** `rules.remote_reading` DENY covers every new `/m/api` path. The API and HUD
  listeners 404 `/m/*`, and `is_owner` stays false.
- **`working_changed`:** carries no ids anywhere.
- **Existing field allowlists:** unchanged byte for byte. That includes
  `DETAIL_FIELDS["deferred"]`, thanks to §5.4's narrowing.

## 7. Tests

**WS-5a** (`tests/v2/remote_api_check.py`, `working_check.py`):
- **Allowlist:** the keys are exact; the narrowed parity holds; planted fields never appear;
  credentials in titles and project names come back withheld.
- **Visibility:** archived threads and projects, and task threads, are absent.
- **Order and cap:**
  - among 101 entries, an old `needs_input` entry is included and first;
  - with equal `last` at the boundary, the entry kept is the higher `thread_id`;
  - `total` and `more` come from the counted visible set.
- **`entries()`:** it calls no `inbox.rows()` and mutates nothing; `view()` is unchanged.
- **Gates:**
  - no cookie, logout, revoke, or mid-read end: 401;
  - a bad Host: 400;
  - a write method: 405, or 403 first without the Origin;
  - a query string: 400.
- **Agent DENY** covers the path, and the API and HUD listeners 404 it.
- **Stream:** `working_changed` is exactly `{}`; flip both of `working_check.py:334-336`.
- **Mutants:** a field added; the scrub removed; the session recheck removed; `data`
  forwarded; the cap before ordering; the tie-break dropped.

**WS-5b** (unit `mobile/*.test.ts`, face `tests/face/hud_v2_phone_working_check.py`):
- **Events:** raw SSE through `onmessage`; `parseEvent` keeps `working_changed` and
  `thread_changed`'s reason.
- **Approval resolved:** resolving the last approval with no further thread activity
  updates the label.
- **Read order:** reversed completion never restores an older list; an event mid-read gives
  one re-read.
- **The list:** order, the hint join, the fold, the truncation notice, hostile text,
  dimming, the badge unchanged, and optionally the 201-row notice.
- **Face:** the list is above the groups; a row opens its chat; a hint scrolls to its row;
  targets are 44 px at 375 px; no horizontal scroll; the CSP is unwidened.

**WS-5c** (`remote_auth_check.py`, `remote_answer_check.py`, `remote_write_check.py`,
`pr_merge_check.py`, `pr_merge_mutants.py`, `review_check.py`, `quiet_defer_check.py`, the
phone face checks, `tests/face/hud_v2_pr_fix_binding_check.py`), on a fake monotonic clock
with relative expiry:
- **The window:**
  - an action at 59 min passes and slides;
  - 61 min after the last action is 423;
  - exactly at `until` is 423.
- **What slides it:**
  - reads, the stream and `me` do not;
  - stt and push subscribe do not;
  - a session-only action slides an open window and does not reopen an expired one (a
    Deny at 61 min succeeds, and the next Allow is 423).
- **What ends it:**
  - logout, End, revoke, expiry, prune (`MAX_SESSIONS` eviction too), and Lock now, each
    **on the existing instance**;
  - a restart (a new `RemoteAuth`) is locked;
  - `_broken` set refuses with 503.
- **Lock now racing verify (Sol r2-1):**
  - Lock, then verify: 409. That includes an assertion signed before the Lock and
    delivered after it.
  - Verify, then Lock: locked at the end.
  - A generation bumped between issue and verify: 409.
- **Unlock failures:** each of UV, signature, origin, `crossOrigin`, rpId, credential id,
  user handle, counter, another session and another passkey is 401. Replay is 409. A failed
  counter save is 503, and the window stays closed.
- **Concurrency:**
  - two verifies of one assertion: one opens the window, the other gets 409;
  - options asked twice: the first assertion gets 409.
- **The 423 contract:** each family through the real `api_route` (Allow, Always, deferred,
  cancel, every PR action including Fix, review Approve, Done, Undo) returns exactly
  `423 {"error":"locked","unlock":true}`, with nothing mutated: the approval is still
  pending, the task not cancelled, nothing merged, the entry still in.
- **Act-time checks with the window open:**
  - a PR's changed head, card or hold: 409;
  - a resolved approval: 409;
  - a terminal cancel: 409;
  - limited §4.1 behaves as today.
- **Deferred:** with the window open, each `_deferral` case is 409: not waiting, not
  grantable, expired. `review_check.py:582` and `:595` (tags, all or nothing) are kept.
- **Always preview** (Sol r3 §9-2). Each of these is 409, or 400 where marked:
  - a stale preview;
  - another card's preview;
  - a digest computed with only the rule changed;
  - a digest computed with only the resolution changed;
  - a missing or malformed preview (400);
  - an Always sent with an Allow card's confirmation state.
- **The rule read** (Sol r3-2):
  - it is 200 while locked and does not slide the window;
  - End or revoke during the read is 401;
  - the keys are exactly `RULE_FIELDS` and `RULE_ENTRY_FIELDS`;
  - a planted extra entry key is 409 at the desk;
  - hostile `tool`/`prefix` text is withheld, never partly shown;
  - POST, PUT and DELETE are 405;
  - a query string is 400;
  - private while limited, and not allowlistable, are 409.
- **Op tokens** (Sol r3-1, r4-1, r4-2), on a fake monotonic clock:
  - **Replay:**
    - a sequential replay of Update is 409 `duplicate` and `gh` runs once (keep
      `pr_merge_check.py:1070`, rewritten);
    - two concurrent identical Updates: one runs, the other is 409, and `gh` runs once;
    - the same token with another body is 409 "reused".
  - **Expiry:** an Update is replayed at 601 s and at 3,660 s while other actions keep the
    window open: 410 each time, and `gh` runs once.
  - **Expiry racing admission (Sol r5-1), with a barrier:**
    1. A completed Update's replay passes the signature and session checks just before
       expiry, then is held at the `Ops` lock by a barrier.
    2. The clock moves past `expires`, and another admission prunes.
    3. The replay is released: 410, and `gh` runs once.
    - The mutant that moves the time check outside the lock must fail this test.
  - **Restart:** a new `RemoteAuth` with the same surviving session and a fresh unlock;
    replaying the old body and token is 410, and `gh` runs once.
  - **Tokens:**
    - a token from another session is 400;
    - a forged or altered token is 410;
    - the future tolerance (Sol r5-4): `t = now + 0.5` passes, `t = now + 1.01` is 410;
    - issuing tokens never slides the window and is rate-limited;
    - token issuance's own final `session_live` re-check: End or revoke during issuance is
      401, and no tokens are returned.
  - **Capacity:**
    - at `OPS_PER_SESSION` and at `OPS_MAX`, admission is 429;
    - `completed`, `refused` and `uncertain` records are all kept;
    - nothing changes: no record, no mutation, and **no rate token** (`Rate.take_if`; the
      mutant that records first must fail).
  - **Running (Sol r5-4):**
    - before expiry, a duplicate is 409 `running`;
    - at and after `expires`, a duplicate is 410, while the running record stays reserved
      (not pruned);
    - the late completion sets `completed` on its own reservation;
    - a completion with a stale reservation changes nothing.
  - **States by failure point:**
    - an exception before `mutating` → `refused`;
    - after `mutating`, before the mutation returned → `uncertain`;
    - during response construction → `completed`, with the 409 uncertain reply;
    - in all three, a replay is 409 `duplicate`, and **none releases the token**;
    - a 400, 423, 410 or 429 never admits, and the 423 → unlock → retry with the same
      token runs once.
  - **Lost response:**
    - a duplicate never executes;
    - op status reports the state, and `unknown` after a restart or prune;
    - **outcomes (Sol r5-2):**
      - a PR action that returns normally with `ok: false` ("not updated") reads back as
        `completed` + `failed` and is shown "Not done";
      - a `gh` timeout giving "not read again" reads back as `completed` + `unknown` and is
        shown "Finished; check at the desk";
      - only `ok` is ever shown "Done";
    - **the status route (Sol r5-3), through the router:**
      - another session's nonce is `unknown`;
      - End or revoke during the read is 401;
      - a query string is 400, and POST is 405;
    - the client never shows `duplicate` as success, never mints a new token for the tap,
      and keeps "unknown" uncertainty.
  - **Order:**
    - rate exhausted plus a duplicate is 409 `duplicate`, with no rate token spent;
    - rate exhausted plus a new token is 429 and not admitted.
  - **Retirement:**
    - logout, End, revoke and prune drop that session's `_unlock_gen` and `Ops` records;
    - revoke ends the sessions even when `_save_passkeys` fails (503).
  - **Families:** each family is tested once through the router.
- **Composed status order** (one request carrying several faults; each pair and the
  triple): malformed plus locked plus stale gives 400; locked plus stale gives 423; rate
  exhausted plus locked gives 423, with no token spent; duplicate plus stale gives 409
  duplicate.
- **Endings delete pending challenges:** for every ending path in §5.8, an unlock
  challenge issued before it is refused afterwards (409).
- **Runtime damage policy:** on a loaded instance, corrupting `sessions.json` and
  `passkeys.json` leaves the open window as it was, matching sessions. End or revoke still
  ends it.
- **Reads while locked (Sol r2-4):** a private card's detail is 200 while locked; then the
  action is 423; then unlock; then the act is 200.
- **Done:**
  - a stale `seq` on the URL thread: 409;
  - Done's token used on another thread's Undo: 409;
  - Undo with no Done: 409;
  - a missing or malformed `seq`: 400;
  - a linked rank 1–4 row: 409 with `why_not`, and the thread stays in;
  - locked: 423 with no change.
- **Undo:** a wrong token, after 10 s, or after an archive: 409.
- **Existing working-set tests kept** under the phone wrappers: running turns, Undo
  deadline equality, restart, lifecycle invalidation.
- **Client** (unit and face):
  - the frozen method, URL and body survive 423 → unlock → one retry, despite SSE
    updates and navigation;
  - no retry after a cancelled Face ID, a 409 **or 503 from unlock**, an ambiguous network
    failure, or a second 423;
  - the locked shortcut skips the doomed POST;
  - two threads sharing a `seq`: navigating to the other thread during the unlock never
    retargets the frozen Done URL;
  - a fallback tap sends the original op once, with the same token;
  - the token pool refills as specified, and an expired frozen token gives "tap again",
    never an automatic re-send;
  - `hud_v2_pr_fix_binding_check.py` waits for the **final** rejection, never the first 423.
- **Isolation:** extend `archive_check.py:1020-1025` with `require_unlocked`,
  `touch_unlock`, `unlock_options`, `unlock_verify`, `_end_unlock`, `locked_reply`,
  `_unlocks`, `OP_KEY`, `issue_ops`, `Ops`, `op_status`.
- **Recording and the rest:** recorded `via="phone"`; `owner_seen` runs; agents are DENY;
  NEVER_STEER and push are unchanged.
- **Mutants:**
  - a read slides the window;
  - a session-only action reopens one;
  - Lock keeps the challenge;
  - the generation check is dropped;
  - the challenge is not spent;
  - `<` becomes `<=`;
  - the window opens before the counter save;
  - the binding to the passkey is dropped;
  - a local catch loses `unlock`;
  - the act-time PR check is dropped;
  - the preview check is dropped;
  - op dedup is skipped;
  - admission runs before `require_unlocked`;
  - the token TTL check is dropped;
  - `OP_KEY` survives a restart;
  - full-store admission evicts instead of refusing;
  - a `running` record is pruned;
  - a refused op releases its token;
  - a reservation mismatch is ignored;
  - the token time check moves outside the `Ops` lock;
  - `Rate` records before the capacity check;
  - `completed` is shown as Done without `ok`;
  - op status looks up by `n` alone, across sessions;
  - the rule read loses its `session_live` re-check.

## 8. Work packages

| WS | What | Files | Size | Tier |
|---|---|---|---|---|
| **WS-5a** | read route, `entries()`, stream kind, read docs | `jarvis/v2/remote_api.py`, `working.py`; `tests/v2/remote_api_check.py`, `working_check.py`; `control-plane.md` (read API, working set); phone design §5 row | S–M | Sonnet build, **Opus review + Sol**, mutants |
| **WS-5b** | phone In progress list | `hud/src/mobile/model.ts`, `views.ts`, `app.ts`, `mobile.css`, their tests; `tests/face/hud_v2_phone_working_check.py` | M | Sonnet build, Sol verify |
| **WS-5c** | unlock window, Lock now, unlock-and-act, rule preview, Done and Undo, removal of per-action challenges | backend: `jarvis/v2/remote_auth.py`, `remote_answer.py`, `remote_write.py`, `pr_merge.py`, `review.py`, `remote_secure.py` (docstring), `remote_api.py` (`seq`); `tests/v2/*` above, `archive_check.py`. UI: `hud/src/mobile/net.ts`, `card.ts`, `write.ts`, `review.ts`, `app.ts`, `views.ts`, their tests; the face checks above; HUD Settings > Phone. Docs: CLAUDE.md, phone design, `control-plane.md` | L | **Opus build, Opus review + Sol**, mutants |

**Dependencies.**
- WS-5a merges first.
- WS-5b and WS-5c's backend build in parallel: they share no files.
- WS-5c's UI goes on top of 5b, because both touch `app.ts` and `views.ts`.
- WS-5c merges as one PR, because the per-action `/options` routes disappear.

**After each merge.** Sol verify, then the owner's OK, merge, and `jarvis hud install`. 5a
and 5c also need a daemon restart when no turn is running; for 5c, the restart locks every
phone.

**Before 5c merges, the owner checks unlock-and-act on the real iPhone** (Sol r3-3):
- **The locked path.**
- **The stale-client 423 path:** open the phone app in two tabs on the same session.
  Unlock in tab A, then press Lock now in tab B, so tab A's local estimate still says
  unlocked. In tab A, tap Allow: 423, then Face ID, then one retry.
- **The desk's End, separately:** it deletes the session, so expect 401 and the sign-in
  screen, not 423.

## 9. Open items

**Owner questions:** none open. Every WS-5c question is answered (§2).

**Safety-boundary items the owner's answers do not cover.** These are planner decisions,
flagged for the lead and the owner to see:
1. **The individual deferred Approve and retry binds the deferral id only,** with the full
   `_deferral` checks (§5.4). This is equivalent to today.
2. **Always's preview binds rule freshness, not human confirmation** (§5.4). An unlocked
   client can submit another valid preview. That is the authority the window transfers;
   only the per-action assertion the owner declined would restore signed confirmation.
   **Worth the owner reading once.**
3. **PR head and card are checked at act time against the tapped values,** never fresh ones
   (§5.4). Replay protection moves to op ids (§5.3a). An unlocked client can deliberately
   submit another current card: that is the window's authority.
4. **Auth-file damage while running does not end a window.** This is equivalent to
   sessions today, and a test pins it (§5.8).
5. **Done binds the URL thread plus that thread's `seq`.** This is equivalent to the desk's
   Done.
6. **iOS gesture risk on the 423 path** (§5.7). The device check (§8) decides whether a
   "Tap to unlock" fallback is needed. It would keep the same op and send it once, but it
   is a second tap.
7. **Op tokens on every gated family** (§5.3a): server-issued, 10-minute, dying at restart;
   a duplicate is refused, never replayed. This is recommended, and it is replay
   protection, not authority. Its cost: a tap more than 10 minutes after the token was
   fetched, or one that spans a restart, says "tap again" rather than acting.

**Defaults, not owner-confirmed:** no "leaves in" time; the desk's order and tie-break
before the cap.

## 10. Review record

| Rev | # | Finding (Sol, cx-ws5) | Where it landed |
|---|---|---|---|
| r1 | 1–6 | parser; stale labels; `seq`; cap; concurrency; IN-8 | FIXED in rev 2 (Sol r2) |
| r2 | 1 HIGH | Lock now leaves a pending unlock challenge usable | §5.1 `_end_unlock` deletes the session's unlock challenges and bumps a generation that verify checks; §5.2 orderings; §7 race tests |
| r2 | 2 MED | local catches swallow 423 | §5.3 `Locked` / `locked_reply` caught first in all four catches plus the new routes; tested per family through the router |
| r2 | 3 MED | `/options` held Always's rule and the deferred binding | §5.4: the `answer/rule` preview plus a `preview` digest; deferred narrowed to the id (tags stay the review's) |
| r2 | 4 MED | §4.1 for reads and actions | §5.5: `check_passkey` unchanged (readiness); the window only in `require_unlocked`; the private-card-while-locked test |
| r2 | 5 MED | `seq` is per thread | §5.6: binding is the URL thread plus that thread's `seq`, frozen; the cross-thread claim removed; 400 and 409 spelled out |
| r2 | 6 MED | missing tests | §7: `pr_fix_binding_check`, unlock failure modes and counter save, concurrency, the frozen retry, slide rules, the existing-instance prune and damage policy (§5.8), `archive_check` names |
| r2 | 7 LOW | tie-break before the cap | P-3: `thread_id` descending; `total`/`more` from the counted set; boundary test |
| r2 | CLAUDE.md | "actions" too broad | §5.8: Sol's wording, 77 → 62 bytes |
| r3 | 1 MED | PR Update replay lost with challenges | §5.3a op ids on every gated family, deduped atomically after `require_unlocked`; sequential and concurrent tests |
| r3 | 2 MED | the rule read's security contract | §5.4: routed beside `detail`, final `session_live`, `RULE_FIELDS`/`RULE_ENTRY_FIELDS`, scrub-or-withhold; §7 tests |
| r3 | 3 LOW | the iPhone 423 test used End (401) | §8: Lock now from a second tab with tab A's estimate stale; End tested separately as 401 |
| r3 | §9 1–6 | per-item treatment | §5.4 full `_deferral` and review tests kept; §5.4 authority transfer stated, preview substitution tests; tapped PR values only; §7 loaded-instance damage test; shared-`seq` client test; fallback keeps the op |
| r3 | extra | ending paths, composed order, 503, working tests | §5.8 the ending list; §5.3 explicit order including rate; §7 composed-order, ending-deletion, 503 no-retry, existing working-set tests kept |
| r4 | 1 MED | expiry and restart reopen a completed op | §5.3a option (b): server-issued HMAC tokens, `OP_TTL_S = 600`, a per-process `OP_KEY` (restart = 410), issued-at set by the server; records kept until the token expires |
| r4 | 2 MED | `Sent`'s eviction and in-flight rules | §5.3a: numeric bounds, 429 never evicts, running/completed/refused/uncertain, running never pruned, reservation identity, one lock; §5.8 `_unlock_gen` and `Ops` cleanup on retirement |
| r4 | 3 | missing op tests | §7: 3,660 s and restart replays, capacity, running and late completion, failure points, lost response and op status, rate vs duplicate |
| r4 | 4 | revoke before persistence | §5.8 revoke ordering, tested with a failing save |
| r5 | 1 MED | expiry vs admission race | §5.3 step 4 / §5.3a check 4: the time check moves inside the admission lock, with one `now` shared with pruning; barrier test |
| r5 | 2 MED | `completed` ≠ Done | §5.3a: `outcome` (ok/failed/unknown) plus an allowlisted `code`; only `ok` shows Done; tests for `ok:false` and "not read again" |
| r5 | 3 MED | status route lookup | §5.3a: lookup by the authenticated session digest plus `n`; final `session_live`; router tests; issuance re-check test |
| r5 | 4 LOW | running-op test | §7: 409 `running` before expiry, 410 at and after with the record kept; the 0.5 s / 1 s tolerance pinned |
| r5 | note | the rate check spent a token | §5.3: `Rate.take_if` checks then commits only when capacity also passes |
| r6 | 1 MED | "update requested" became "Done" | §5.3a: stored `ok`/`update_requested`, shown "Update requested; check at the desk"; lost-response tests for a 202 with an unchanged head and with an unread follow-up |
| r6 | 2 LOW | PR code list incomplete | §5.3a: `queued`, `head_moved`, `closed`, `not_mergeable` added; every current result tested; `not read again` → `unknown` first; `changed` documented as the exception path |
| r6 | note | `take_if`'s capacity callback | it reads the `Ops` records with the `Ops` lock already held and never takes it again |

## Addendum (lead, 2026-11-15, from Sol's revision-6 review: report-r6.md)
- **Update requested is not Done.** HTTP 202 → `outcome:ok`, `code:update_requested`. Status and the phone show "Update requested; check at the desk". "Done" is only for confirmed completion. Tests: lost response after a 202 with an unchanged head, and after a 202 whose follow-up read failed.
- **PR code mapping is total.** Add `queued`, `head_moved`, `closed` and `not_mergeable`, and test every current pr_merge result. `not read again` → `unknown` takes precedence over `ok:false` → `failed`. `changed` is an exception path (pr_merge.py:723), not a normal return. Never reuse prRow.ts `resultOk`, which accepts `queued`.
- **`Rate.take_if`'s capacity callback** inspects Ops under the already-held Ops lock and never re-acquires it.
Every other revision-5 item is FIXED per Sol r6. The plan is ready to build with this addendum.

## As built: WS-5c backend (#374), the builder's deviations

Judged by Sol (cx374) and Opus; updated in #374's fix round.

1. `Rate.take_if` samples `remote_write.clock()`, not the `Ops` snapshot. Both are
   `time.monotonic` in production. Accepted.
2. Every gated family spends a rate token; before, only `remote_write`'s routes did.
   Accepted.
3. The op token's full shape is checked at step 1, so a malformed token is 400 before
   423. Accepted.
4. A malformed task id on cancel is 400 (was 404). Accepted.
5. **Marked `mutating` before a call that can still refuse, so a definite refusal inside
   it reads back `uncertain`** (the safe direction: never a false "Done"). This covers
   Fix (`pr_fix.start`) and cancel (`task_verb`), and also (Opus LOW-5, disclosed in the
   fix round) Allow and Always (`_dispatch`, which can still refuse inside) and the
   deferred Approve and retry (`quiet_defer.answer`, whose `APIError` is answered as its
   own status while the op settles `uncertain`). Accepted.
6. The act-time PR checks refuse before the fresh gh read. Accepted.
7. ~~A Done refusal returns `{"error": why}` with no separate `why_not`.~~ Fixed in the
   fix round: a linked rank 1–4 row or a running turn is 409 `{error, why_not}`
   (`WorkingSet.done` names the reason; `remote_write.done` scrubs it).
8. `review.subject`, `pr_merge.subject` and `remote_answer.defer_binding` are deleted.
   Accepted.
9. ~~Retiring a session cleans its `Ops` records in the same locked step as
   `_end_unlock`; admission's pruning does not look up retired sessions.~~ Insufficient
   under concurrency (Sol MED). Replaced in the fix round by admission under the auth
   lock after a final session-and-window check, and `Op.mutating()`'s own final check
   (§5.3 steps 4 and 6).
10. cancel, Done and Undo call `owner_seen` (QH-3). Accepted.

Also from the fix round: Approve selected has the catch-all every other family has (an
exception is the 409 "may not have gone through", the op `uncertain` once `mutating`);
the `Ops` and token-issue 429s carry `Retry-After: 60`; a Lock now after a retirement
never makes the session's generation entry again; unlock challenges stay per session
(tested with two sessions pending at once).
