# Plan: the morning review (MR)

**Status (2026-10-28): plan by the lead; owner answered its four questions the
same day; built (MR-A, MR-B), see "MR as built" below.** Builds on the quiet-hours deferral (QH, PR #326,
merged, `docs/plans/2026-10-28-quiet-hours-deferral.md` and its "QH as
built"), the owner inbox (`2026-10-24-owner-inbox-design.md`) and the phone
surface (`2026-10-24-phone-surface-design.md`, §7, §10.4–§10.8).

## What the owner asked (2026-10-28)

> "the report should be clearly structured and i should be able to respond to
> each question individually or as a group that way I don't have to work
> through the entire thing in one super long prompt the scrolling back and
> forth sucks."

Binding choices that day:
- Group answers include approvals: each item shown in full with its own
  checkbox, nothing pre-checked, one "Approve selected" and ONE Face ID
  approve exactly those calls. An owner-chosen exception to inbox IN-3's and
  phone §9's "no bulk Allow".
- The 07:00 push stays one content-free summary push; the phone opens on the
  review.
- Overnight `owner_post` decisions and questions belong in the report.

Owner answers to this plan's questions (2026-10-28, all the defaults):
1. Overnight decisions are dismissed **one at a time**, no group dismiss.
2. At most **10** approvals per "Approve selected".
3. A **limited** phone (a §4.1 check failing) gets **no** group approval; per
   row only.
4. If any ticked item changed before the confirmation completes, **nothing is
   approved**; the changed items are unticked with a reason and the rest need
   a new confirmation.

Already settled and unchanged: quiet hours 23:00–07:00, nothing pushed, one
push at 07:00 if ranks 1–4 still wait (P-6); night approvals deferred as
rank-4 rows with "Approve and retry" (exact, single use, 30 minutes) and
"Leave it" (QH).

## Goal

One screen, on the phone and at the desk, showing everything that waited
overnight, grouped so it reads top to bottom once. Each question is answered
in place. Deferred approvals are approved or left as a set chosen item by
item; one confirmation covers that set and can never approve a call the
screen did not show.

## Decisions

- **MR-1. Overnight.** `quiet.last_window(now)` (new, pure) returns the
  (start, end) of the current or last ended quiet window, or None when quiet
  hours are off. The report holds (1) **Overnight**: rows at ranks 1–4 and open
  decisions (rank 6) asked in the window; (2) **Still waiting from before**:
  ranks 1–4 asked earlier (what the 07:00 push counted); (3) **For your
  information**: only a count of overnight rank-8 rows with a link to the
  Inbox. No PRs (5) or checks (7). Computed on every read (IN-1), nothing
  stored. Quiet hours off: only part 2, under "Quiet hours are off".
- **MR-2. Structure.** A summary line of counts by kind, then one jump chip per
  chat (scrolls to an anchor). Groups by project, then chat or task, ordered
  by most urgent item then oldest. Within a chat: deferred approvals,
  questions, decisions, blocked tasks and proposals. A live rank-1 approval
  sits alone at the top as "Live now", links to its card, never selectable
  (IN-3 unchanged for live cards). Titles, `blocks` and labels are code-built;
  agent and chat text is untrusted (`textContent`). Groups do not collapse (a
  collapsed group could hide a ticked item); the chips replace scrolling.
- **MR-3. Each item in full.** A deferred approval shows exactly its QH row:
  headline, tool, full command or arguments, reason, "asked N times", and why
  it cannot be granted if not. Never cut (QH's `grant_refusal` already refuses
  a card the rows cannot show whole). A card the phone's scrub would change
  (`WITHHELD`) has its checkbox disabled for Approve ("answer it at the
  desk"). Questions and decisions show full text, options, the default marked
  but not chosen.
- **MR-4. Group actions are for deferred approvals only.** A checkbox per
  deferred row, unticked on every render; nothing is ticked except by its own
  tap; no "select all". A sticky footer: "N selected", **Approve selected
  (N)** and **Leave selected (N)**, far apart. Approve is disabled while any
  ticked item cannot be approved, and the footer names which. "And N more"
  rows (QH M2) can be ticked for Leave only. A deferral whose chat is closed
  or archived is not approvable.
- **MR-5. Questions and decisions are answered one at a time, in place**, each
  with its own box or options and Send, through the existing routes. After
  sending, the row folds to a one-line receipt where it stood so the scroll
  holds. No group answer: an answer is the owner's own content, rank-2
  questions cannot be hidden (IN-8), and the phone cannot mark (B4). At the
  desk, decisions get a per-row "Dismiss" (B4's route); no group dismiss
  (owner answer 1).
- **MR-6. What a group approval binds.** Each approvable item carries `tag =
  HMAC-SHA256(key, "mr-v1" ‖ id ‖ digest ‖ sha256(canonical card))`, `key` 32
  random bytes per daemon process, never written. The tag binds QH's exact
  digest and the exact card shown and reveals neither. The client sends
  `items: [{id, tag}]`; the server recomputes each tag from the current
  record. Subject: `defers:` + sha256("jarvis-morning-review-v1\n" + sorted
  "id\ttag" lines)[:56], which fits `remote_auth._DEFER_SUBJECT`; decision
  "allow" only. The group route always uses the set form, even for one item;
  the per-row route keeps `defer:<id>`; neither accepts the other's
  assertion. QH's unused id-only list form of `remote_answer.defer_binding`
  is replaced by this one.
- **MR-7. All or nothing** (owner answer 4). Checked before the challenge is
  issued and again after the assertion and §4.1: any selected item no longer
  `waiting`, past 24 h, tag mismatch (other card, digest, or a restart), now
  withheld or HUD-only, task no longer `blocked`, or chat closed → 409
  `{changed: [{id, reason}]}`, no grant written, nothing delivered; the client
  unticks those, says why, and needs a new confirmation. Grants are written in
  **one** store transaction (`DeferredStore.settle_many`, every check under
  the store lock, all or none). Only delivery afterwards is per item. One
  message per chat (code's words, each tool and deferral id, "only the
  identical call, once, until HH:MM") and one resume per blocked task; all
  task checks before any resume.
- **MR-8. Limits.** At most **10** per Approve selected (owner answer 2), 25
  per Leave selected; more, a duplicate or a malformed item is 400. The report
  shows at most 60 items, then "and N more in the Inbox". Group approval uses
  the single answer's challenge budget (single use, 60 s); asking again for
  the same set replaces the earlier challenge.
- **MR-9. Grants are QH's, unchanged**: exact digest and thread, single use,
  30 minutes from approval, never Always, never renewable. An "Approved this
  morning" strip shows each: waiting (until HH:MM), used, or expired unused.
- **MR-10. The desk.** The HUD gets the same report as a "Morning review" view
  (sidebar row under Inbox, count = open review items). Approve selected needs
  the desk owner test (`owner_only`) plus an in-app confirmation dialog
  listing each selected item (chat, tool, the command's first line) with
  **Cancel focused**, Enter and Escape meaning Cancel. No passkey, matching
  QH's single desk Approve and retry. Inert under a live approval card. A desk
  answer stamps presence (QH-3).
- **MR-11. The phone's authority.** Approve selected needs the session, exact
  Host and Origin, a fresh assertion bound to the MR-6 subject, and **all five
  §4.1 checks passing**; a limited phone gets per-row Approve and retry only
  (owner answer 3). Leave selected needs the session only, per-item results,
  no tags. A verified group approval stamps presence.
- **MR-12. The 07:00 push and opening the phone.** Payload stays exactly
  `{count, rank}`; `sw.ts` unchanged. `Push` remembers in memory that the last
  push was the morning one; the next other push clears it. `GET /m/api/review`
  returns `opens_here` while that holds and the review has open items; a
  notification tap or launch that sees it opens the Review tab, otherwise the
  Inbox. A restart loses the flag (falls back to the Inbox). The Review tab
  sits beside the Inbox while the review has open items, until the next quiet
  window starts.

## Safety

| CLAUDE.md Safety design bullet | How MR keeps it |
|---|---|
| Enforced toolsets | no new tool; `owner_post` unchanged; no agent toolset gains a review path |
| Commands are judged in full | a grant is QH's exact digest (args, command, folder identity, layer, headline, sub-agent, relay); owner-only, widening, merge-gate, always-ask, escape-hatch, Codex, sub-agent and relayed asks stay never grantable; grouping widens no match |
| Permission state cannot authorize its replacement | `deferred.json` stays protected state; `review.py` joins SELF_PROTECTED; no Always from a group; the HMAC key is memory-only |
| Secrets are use-but-never-see | items are QH's public card projection through the phone's scrub (withheld means no Approve); the tag hides the digest; push stays `{count, rank}` |
| No operation of one's own gate | `/inbox/review*` is `owner_only` on the HUD listener; `/m/api/review*` behind `owner_status` plus a fresh assertion and §4.1 for approval; `rules.py`'s 8408 DENY and the `archive_check` grep extended to the new paths; only the broker asks |
| Public web does not open the LAN | no new listener or port; HUD-only Browser cards never grantable (QH) |
| Untrusted input stays data | code-built titles, labels, delivery words; agent and chat text via `textContent`; options are text, never actions |
| Delegation inherits limits | sub-agent and relayed calls are not grantable (QH LOW 6), so never in a group |
| Background git | merge-gate asks are owner-only, never deferred-granted |
| Inbox IN-3 / phone §9 "no bulk Allow" | the one recorded exception: deferred rows only, the exact displayed set, ≤10, one deliberate confirmation, all or nothing; live cards untouched |

## Surfaces and routes

| Listener | Route | Does |
|---|---|---|
| HUD (8403), `owner_only` | `GET /inbox/review` | the report, approvable items with `tag` |
| HUD, `owner_only` | `POST /inbox/review/approve` `{items:[{id,tag}]}` | MR-6/7, `via="inbox"` |
| HUD, `owner_only` | `POST /inbox/review/leave` `{items:[{id}]}` | per-item Leave it |
| Remote (8408), session, Host, Origin | `GET /m/api/review` | the report via `remote_api._detail` per kind, plus `opens_here` |
| Remote | `POST /m/api/review/approve/options` `{items}` | first check, §4.1, then `answer_options(session, subject, "allow")` |
| Remote | `POST /m/api/review/approve` `{items, assertion}` | `answer_verify` against the subject recomputed from the body, §4.1, second check, `approve_many(via="phone", verified=True)` |
| Remote | `POST /m/api/review/leave` `{items}` | session only |

The route-table test checks neither listener serves the other's paths and the
API listener (8402) serves none.

## Code shape

- `jarvis/v2/review.py` (new, SELF_PROTECTED): `report(daemon, *, phone)`,
  `item_tag`, `subject(items)`, `check_group(daemon, items, *, phone)`, route
  halves.
- `quiet_defer.py`: `DeferredStore.settle_many(ids, check)` (one `_change`;
  per-id `LookupError`/`ValueError` collected into `changed`) and
  `approve_many(daemon, ids, *, via, verified)` (grants, then one message per
  thread, one resume per task). `approve_and_retry` unchanged.
- `quiet.py`: `last_window(now)`, pure, DST through the existing helpers.
- `remote_answer.py`: `defer_binding`'s list form replaced by `review.subject`.
- `push.py`: the morning flag and `review_opens()`; `payload` untouched.
- Frontend: `hud/src/lib/review.ts` (grouping, empty selection, confirmation
  list), `hud/src/components/ReviewView.tsx` and a sidebar entry,
  `hud/src/mobile/review.ts` (tab, chips, sticky footer, passkey flow reusing
  `retryDeferred`'s shape), `app.ts` routing for the tap.

## Tests

Unit (`tests/v2/review_check.py`, fake clocks, temp HOME): window bounds
(22:59 before, 23:00 overnight, 07:00 end; quiet off; a DST night; decision
in; PR, check and rank 8 only counted; live rank 1 is "Live now" with no tag);
tags (change with id, digest, card, restart; no digest or argument substring
in any payload); subject (order-free, includes tags, ≤64 chars; one id or tag
different → another subject; set A's assertion refused for set B;
`defer:<id>` refused at the group route and a group assertion at the row
route); all or nothing (one of three left at the HUD between options and
answer → 409 `changed`, zero grants, file unchanged, write count asserted,
nothing delivered; likewise a resumed task, past 24 h, tag mismatch, closed
chat, withheld card); success (one write; each grant taken only for its exact
call, once, expires at 30 minutes; two deferrals in one chat → one message,
two in one blocked task → one resume); limits (11 items, duplicate, malformed
tag → 400); phone authority (no assertion 401; each of the five §4.1 failures
refuses a group but not a per-row retry; Leave needs no assertion);
owner-only (403 from the API listener; 401 without a session; 403 wrong
Origin; 400 wrong Host; tool and MCP sweep finds no path; shell probes DENY at
v2 layer 1 and in v1); push (payload byte-identical; `opens_here` true after
the morning push, false after the next push and after a restart).

Mutation targets (each must fail a test): subject from ids only; per-id
settles instead of `settle_many`; no second check after the assertion; the
10-item cap removed; a limited phone allowed a group; a box pre-ticked or a
"select all"; a message per record instead of per thread; an approvable item
whose chat is closed.

Phone browser check (mock mobile server, Playwright at an iPhone viewport,
fake WebAuthn): a tap with `opens_here` opens Review, without it the Inbox; no
box ticked at first; tick 2 of 3 → the confirmation lists exactly those 2 and
the POST carries exactly those ids and tags; a 409 `changed` unticks and
explains; a decision answered in place keeps `scrollY` within one row; chips
land on their chat; Approve disabled with a withheld item ticked.

HUD check (`tests/face/hud_v2_review_check.py`, headless): the dialog lists
the selection with Cancel focused and Enter cancels; inert under a live card;
`<title>` unchanged. Vitest: `lib/review.test.ts`, `mobile/review.test.ts`.

## Work packages

| WP | Owns | Acceptance |
|---|---|---|
| MR-A backend | `review.py` (new), `quiet_defer.py`, `quiet.py`, `remote_answer.py`, `remote_auth.py` (dispatch and docstring), `inbox.py`/`hud_api.py` (mount), `push.py`, `jarvis/tools/files.py` (SELF_PROTECTED), `tests/v2/review_check.py`, `archive_check.py`, `quiet_defer_check.py`, docs (control-plane, this plan's "as built", IN-3 note, phone §9/§10.10) | unit and mutation list above; boundary review |
| MR-B surfaces | `hud/src/lib/review.ts`, `ReviewView.tsx`, sidebar entry, `hud/src/mobile/review.ts`, `mobile/app.ts`, `mobile/views.ts` (tab only), CSS, the phone and HUD checks, vitest files | the checks above; Graphite tokens; touch targets ≥44 px; Approve and Leave far apart |

Order: MR-A (route contract frozen in its first commit) then MR-B. Independent
review of each before merging.

## MR as built (2026-10-28)

Module `jarvis/v2/review.py` (SELF_PROTECTED); the route contract is its
docstring, frozen in MR-A's first commit. Rules in one place:
`docs/notes/control-plane.md`, "The morning review".

- **MR-1.** `quiet.window_bounds` (pure) and `QuietHours.last_window(now)`
  (the setting in force; None when off). "Still waiting from before" holds
  every rank 1 to 4 row not asked in the night, including one asked since
  07:00 this morning (so with quiet hours off it is the whole list, under
  "Quiet hours are off"). For your information counts the night's rank-8
  rows other than done checks. A row hidden by a mark (a snoozed or
  dismissed decision) is not in the review.
- **MR-2.** The daemon sends flat items with a `place` key (`chat:<id>`,
  `task:<id>`) and `places`/`projects` maps; the grouping, chips and summary
  line are `hud/src/lib/review.ts`, shared by both surfaces. A place's title
  (the chat's title, the task's brief) is untrusted text, drawn as text and
  never upper-cased.
- **MR-3.** As planned; the phone's scrub applies (`remote_api.inbox_row`), and
  a card it would change is `why: "withheld"`, never ticked for Approve there
  (it may still be ticked for Leave).
- **MR-4.** As planned. Selection lives in the view's own state: empty when
  the view (or the app) opens; a re-read unticks what left the report.
- **MR-5.** As planned. Proposals take Withdraw (the phone asks once first);
  blocked tasks link to the task. An answered row stays as a one-line
  receipt where it stood until the view closes.
- **MR-6.** `item_tag` = HMAC-SHA256 under a module key made at import (one
  per daemon process) of `"mr-v1"`, the deferral id, its digest and the
  SHA-256 of its card's canonical JSON, NUL-delimited. Items carry row ids
  (`defer:<id>`). `remote_answer.defer_binding` takes one id only.
- **MR-7.** `quiet_defer.approve_many` → `DeferredStore.settle_many` (one
  `_change`; `GroupChanged` collects every refusal and nothing is written).
  Reasons: `gone`, `answered`, `not-grantable`, `hud-only`, `expired`,
  `changed`, `task-running`, `chat-closed`, `no-chat`, `withheld`. One
  message per chat (`group_message`; a single item gets QH's own
  `retry_message`), one resume per task.
- **MR-8.** As planned (10, 25, 60). A 400 for anything else in the body.
- **MR-10.** The HUD view is a pane view, `review` (`lib/workspace.ts`),
  opened by the sidebar's "Morning review" row under the Inbox (absent when
  the daemon has no review). The confirmation is in-pane; Enter and Escape
  cancel whatever has focus in it, so Approve is a click (or Space).
- **MR-11.** The phone's report carries `group: {ok, why}` from §4.1's last
  view, for display only; the options and the answer each run §4.1 in full.
  A verified group answer stamps presence before §4.1, as the per-row one.
- **MR-12.** `Push.review_opens()`; the flag is set by a successful push that
  was due because quiet hours ended, cleared by any later successful push
  (the desk's test push included) and lost on restart. The app reads `GET
  /m/api/review` at launch and on the service worker's `open-inbox`
  message; `sw.ts` is unchanged. The Review tab shows while `open > 0` and
  it is not quiet now.

Tests: `tests/v2/review_check.py` (34), `hud/src/lib/review.test.ts`,
`hud/src/mobile/review.test.ts`, `tests/face/hud_v2_review_check.py` (21),
`tests/face/hud_v2_phone_review_check.py` (19, a mock mobile server and a
fake WebAuthn). Mutants, each caught: subject from ids only
(`test_subject`, `test_set_a_assertion_is_refused_for_set_b`); per-id settles
(8 tests); no second check after the assertion (5); the 10-item cap removed
(3); a limited phone allowed a group (the five §4.1 tests); a message per
record (2); an approvable item whose chat is closed (2); a box pre-ticked
after a read (vitest 3, both browser checks) and a tap that ticks every
item (vitest 3, both browser checks).

Not done: no end-to-end browser check against a real daemon's remote
listener (the phone check uses a mock server, as the plan says); the
passkey flow against a real authenticator is covered by
`review_check.py`'s software authenticator.

### The review of #329's fixes (2026-10-28)

- **MED 1.** A re-read under an open confirmation that drops, retags or
  makes unapprovable any confirmed item closes it, unticks that item with
  "Unticked: answered elsewhere or gone." and asks for a new confirmation
  (`lib/review.ts` `confirmedChanged`). The confirmation's exact `{id, tag}`
  set is kept when it opens, and only it is ever sent (phone `app.ts`
  `reviewConfirmed`, desk `ReviewView` `confirmed`); a tick changed on the
  phone under the confirmation closes it too.
- **MED 2.** The phone's selection is emptied whenever the Review tab is
  entered from another tab and on a notification's tap; the desk view
  mounts empty (checked).
- **LOW 3.** The sweeps (`archive_check.DEFER_SOURCE`,
  `review_check.Boundary.REVIEW_SOURCE`) also see `v2.review`,
  `jarvis.v2.review`, `from <package> import review` and the module's
  attributes; a test holds them to eleven spellings.
- **LOW 4.** Answer challenges are budgeted per session and per family:
  an approval's, a task verb's, a deferral's row and a morning review's set
  (`remote_auth.ANSWER_FAMILIES`), four each. A new `defers:` challenge
  replaces the session's earlier ones; a row's replaces its own. A live
  card's Allow always gets a challenge.
- **LOW 5.** The vitest and browser fixtures hold three approvable items
  with two ticked; a stale tag at the options is 409 and no challenge is
  issued.
- **LOW 6 (the lead's decision).** The phone shows every argument beside the
  command (timeout, run_in_background, description and the rest), in the
  review and in QH's inbox row; the arguments come through the phone's
  scrub, and a secret in one withholds the card.
- **NIT 7.** Rows first asked after the night ended are their own part,
  `since`, "Waiting now"; only rows asked before the night are "Still
  waiting from before". The report gains `since: [item]`.
- **NIT 8.** The desk's confirmation is a focus trap (Tab cycles inside it)
  and everything behind it in the view is `inert` while it is open.

Each fix has a test and a mutant of it that fails the test: a shared
challenge budget and kept earlier sets (`review_check`), no tag comparison
at the options, `since` read as before, the absolute-import patterns
removed, no confirmed-set check on the phone and at the desk (vitest, the
HUD check), entering Review or a notification's tap keeping the ticks,
arguments hidden beside a command (review and inbox row), a confirmation
listing every approvable item, no focus trap, nothing inert behind it.

