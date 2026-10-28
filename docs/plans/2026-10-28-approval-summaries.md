# Plan: approval summaries (AS)

**Status (2026-10-28): owner-approved direction, plan and build by a lead's
agent.** The owner's answers below are binding; the AS-n decisions are the
builder's defaults, each reversible. The morning review (MR, PR #329) is
covered in WP AS-E, applied once #329 is on `main`.

## What the owner asked (2026-10-28)

> "Can we rework approvals? There is no way that I can read a full paragraph
> of CLI command with a bunch of verbose arguments and stuff without getting
> lost and at that point it would be better to just have an agent do the
> approval. So what we should do is instead of making me read the full
> command each time we should have the auto mode classifier also generate a
> short summary when a command gets flagged of what the command does and I
> just read that. Most of the time commands don't have dangerous syntax
> errors it's just need permission to write to a db or to deploy or touch
> sensitive stuff or something like that so as long as its clear that is
> doing what and where its good. Also move the approve/always/deny buttons to
> the top where they are more accessible. Keep the long cli command collapsed
> but have the option to expand if i do want to read the whole thing."

### The owner's answers (binding)

1. **Card content.** A short model summary (plain words: what it does and
   where), and two code-built lines the model cannot influence: **Why
   asked** (the gate's existing reason/headline) and **Where** (project ·
   branch · folder, from the gate's judged cwd and the existing card
   fields). The model summary is visibly marked as an AI summary, styled
   apart from the code lines.
2. **Model.** Haiku 5.5, one-shot, following `jarvis/v2/speech_summary.py`:
   its own tier and env override (`JARVIS_APPROVAL_SUMMARY_MODEL`), never
   the owner's chat-model pick; never Chinese-hosted (`models.cn_hosted`);
   input fenced (`untrusted.fence`) and scrubbed (`scrub`, `strip_invisible`)
   before it leaves; `cache=False`. Claude Code's own classifier cannot be
   changed, so this is Jarvis's own call.
3. **Slow or failed summary.** The card appears at once with the code lines
   and never waits on the model. The summary fills in when ready (target
   2 to 3 s, hard timeout about 8 s). Failed or timed out: the full command
   is shown **expanded**. Otherwise the command block is **collapsed** with
   an expand control.
4. **Scope: everywhere.** HUD cards (`Approvals.tsx`, `BrowserApproval.tsx`),
   phone cards (`hud/src/mobile/*`), inbox rows, and the morning review's
   items (each with its summary and code lines, command expandable;
   supersedes MR's "each item shown in full").
5. **Buttons at the top.** Allow, Always and Deny (whatever the card offers)
   go at the top of each card.

## Decisions

- **AS-1. One module, protected.** `jarvis/v2/approval_summary.py`
  (SELF_PROTECTED in `jarvis/tools/files.py`). Tier `approval_summary` in
  `config.TIERS` (`JARVIS_APPROVAL_SUMMARY_MODEL`, default
  `anthropic/claude-haiku-5.5`), read from `TIERS`, not `models.tier()`. An
  override naming a Chinese-hosted model falls back to the default; with no
  acceptable model there is no summary (`off`), never a quiet downgrade.
- **AS-2. What the model reads.** The card's own redacted projection
  (`card_text` command, `card_args` arguments: typed credentials already
  withheld, the owner's 2026-10-15 rule), the tool's name, the provider and
  the judged folder (for a shell, the command only: an agent's own
  `description` of its call is not sent); then `scrub` and `strip_invisible`; then
  `untrusted.fence` with a fresh nonce. A call whose prepared text is longer
  than `INPUT_MAX_CHARS` (8,000) gets **no** summary (`off`, so the command
  shows expanded): a summary must never describe only the first part of a
  call. Reason and headline are not sent: they are the code lines.
- **AS-3. What the model may say.** One line, at most 160 characters:
  markdown and link syntax removed, `scrub`, then `approvals.clean_line`
  (whitespace, control and unseen characters collapse, backticks go, capped
  with "…"). Empty after cleaning is a failure. It is **display text only**:
  it never feeds the gate, the allowlist, Always, a QH digest or grant, an MR
  tag, the merge gate or any other decision (AS-13).
- **AS-4. Never in the way.** The broker's `on_request` publishes the card
  with `summary_state: "pending"` and only then starts the summary on a
  worker thread, outside the broker lock. A watchdog ends it at
  `DEADLINE_S` (8 s) as `failed`; a late answer is still cached for the next
  identical call, never shown on the card it missed.
- **AS-5. Cost and abuse.** Cached by the SHA-256 of the exact model input
  (LRU, 512): a re-ask or an identical call reuses it, and identical calls
  in flight share one request. At most 3 calls run at once and 12 wait;
  past that, `off`. At most 1,000 model calls per local day (in memory, a
  restart resets it); past that, `off`. Each call's model, tokens, cost and
  time are logged, and `stats()` keeps the day's calls and cost: there is no
  shared accounting hook for one-shot calls (the speech summaries and titles
  log the same way).
- **AS-6. States.** `summary_state` is `pending`, `ready`, `failed` or `off`
  ("" when no summarizer is attached: tests, embedded daemons, an older
  daemon). Collapsed only for `pending` and `ready`; every other value
  (unknown ones included) is expanded.
- **AS-7. Fields and events.** `ApprovalRequest` gains `summary`,
  `summary_state` and `where`, in the public `to_json` only. A HUD-only
  (Browser) card gets none of them: its private detail never leaves for a
  model, and its body is already code-built (`browserBody`). The arrival is
  the bus record `approval_summary` (`{req_id, summary, summary_state}`,
  `thread_id` on the record), published only while the card is pending; the
  inbox observes it (`inbox_changed` `appr:<id>`), which is also what makes
  the phone read the card again.
- **AS-8. Where.** Code-built by the daemon before the broker's lock
  (`PendingApprovals.describe`, set in `Daemon.__init__`): the thread's
  project name, the branch read from the judged folder's `.git/HEAD` (a
  worktree's `gitdir:` followed; at most 512 bytes read; no `git` process),
  and the folder (`request.cwd`, else the project root), the home directory
  shown as `~`. One line, `clean_line`d, at most 200 characters. Display
  only; not in the QH digest.
- **AS-9. Why asked** is the existing `[layer] reason` line, now shown under
  the summary with its own label. A headline (a sandbox widening, the merge
  gate's) stays first, above the buttons, as before.
- **AS-10. The phone.** `remote_answer.phone_card` judges `withheld` on the
  card **without** `summary` and `summary_state` (they arrive later, and a
  model's words must not change what the owner may answer), then adds the
  summary scrubbed by itself; if the phone's scrub would change it, it is
  dropped and the state becomes `failed` (expanded). `where` is part of the
  judged card. The inbox list's approval detail carries the three fields
  through the same `scrub_structure`.
- **AS-11. Deferred approvals (QH).** `QuietDesk.deferred` starts the
  summary after the record is written; on `ready` the record's new optional
  `summary` field is set (`DeferredStore.set_summary`) and `inbox_changed`
  names the row. The field is display text, outside `card`: not in
  `request_digest`, `grant_refusal` or anything MR tags. A record without
  it (written before AS) stays valid. `summary` and `summary_state` are
  dropped from the record's `card`; `where` stays in it.
- **AS-12. Buttons first.** On every card the button row comes right after
  the title (and the headline, when there is one). On the HUD it is sticky
  at the top of the scrolling card, so it is always in reach. **This
  supersedes** the HUD workspace plan's "button row stays unsticky" and the
  layout check's "AUTHORIZE is below the fold for a long command": that rule
  made reaching AUTHORIZE mean scrolling past the whole command, and the
  owner has now asked for the opposite. What replaces it: the Why-asked line
  (code), the collapsed preview's first line with the command's line and
  character count, the expand control, and AS-13's arming.
- **AS-13. Arming (HUD).** AUTHORIZE and ALWAYS are disabled for 600 ms
  after the card mounts and again whenever the button row moves: a card
  that appears under the pointer, or a click that lands as the layout
  changes, never authorizes. DENY is never disarmed (it is the cheap
  action). The phone needs no timer: each Allow and Always already takes its
  own passkey assertion. Its DENY-first spacing (120 px to ALWAYS and
  AUTHORIZE) moves to the top as one block.
- **AS-14. Nothing moves under the pointer.** The veil anchors the card at
  the top (it was centred, so any growth moved the buttons). The summary's
  slot is reserved (two lines high) from the first frame, and the command
  block opens below the buttons.
- **AS-15. The expand control** is a plain button, out of the Tab order and
  never focused by a pointer (DENY stays the only Tab stop on the card). Its
  label says what it hides: "show full command · N lines · M chars". The
  owner's own toggle wins over the automatic expand on failure.
- **AS-16. Discord is unchanged.** Its post carries the whole command, as
  today, and is sent before any summary exists. Editing posts later is out of
  scope; the owner may ask for it.
- **AS-17. No model in tests.** Only daemon2's `main()` attaches the
  summarizer (`Daemon.attach_approval_summaries`), as it does the titles: no
  test or embedded daemon calls a model. Tests pass fakes.

## Where the summary attaches (flow)

```
permit (_ask)  →  PendingApprovals.ask(request)
                    describe(request)        # AS-8, daemon code, before the lock
                    lock: settle (QH) / pend
   deferred  →  QuietDesk.deferred → store.add → summaries.begin → set_summary
   asked     →  on_request = Daemon._approval_requested
                    summary_state = "pending" (if attached and eligible)
                    publish approval_requested (to_json)
                    summaries.begin(request, done)   # worker thread, no lock
                    done → request.summary/_state → publish approval_summary
                         → inbox_changed appr:<id> → HUD inbox, phone card re-read
HUD:   approval_requested → approval_add;  approval_summary → approval_patch
Phone: GET /m/api/inbox/<id>/detail (phone_card: summary judged apart)
```

## Safety table

| Rule | How it holds | Test |
|---|---|---|
| The summary decides nothing | Only `request.summary`/`summary_state`, the bus record and the QH record's `summary` field hold it; no decision path reads them | digest, Always rule, grant refusal and phone authority equal with and without a summary; mutants feeding it into the digest or the allowlist fail; a grep test over the decision modules |
| Text only, one line, capped | `clean_summary` (server), `summaryText` (HUD and phone, again) | unit and vitest: markup, newlines, bidi, 400 characters in |
| Input fenced and scrubbed | `prepare`, `untrusted.fence`, card redaction | the fake model sees the fence, no secret, no zero-width |
| Nothing executed is hidden | the command and every argument one tap away; expanded when no summary | vitest and browser checks |
| Never waits on the model | card published first; worker thread; watchdog | timeout test with a fake that never answers |
| Not under the broker lock | `begin` runs from `on_request`, after the lock | a fake that asserts the lock is free |
| Caps | cache, in-flight share, 3 running, 12 waiting, 1,000 a day | unit tests |
| Phone authority unchanged | `withheld` judged without the summary | a summary carrying a secret-looking string neither withholds nor changes `can` |
| Card protections kept | Enter does nothing, DENY only Tab stop, ALWAYS rule first, HUD-only cards, widening headline first, merge gate asks, phone passkey per answer | existing suites, vitest |
| No model in tests | summarizer attached by `main()` only | daemon test: no summarizer by default |

## Tests

- `tests/v2/approval_summary_check.py` (new, fake model): candidates and
  the tier, the fence and the scrub, the input cap, output cleaning, the
  deadline, failure means `failed`, the cache and in-flight sharing, the
  concurrency and daily caps, the broker lock free during `begin`, the
  daemon publishing order, the phone's judgment, QH's record field, and
  the never-decides tests with their mutants.
- vitest: the shared card lib (`lib/approvalSummary.ts`), the HUD card
  (buttons first, collapsed with a summary, expanded without, arming), the
  phone card and rows.
- Browser checks with mocks: the HUD card (`hud_v2_check`), layout
  (`hud_v2_layout_check`: buttons in reach at every zoom), the inbox
  (`hud_v2_inbox_check`), the phone (`hud_v2_phone_check`,
  `hud_v2_phone_answer_check`).
- Existing suites: approval_label, quiet_defer, inbox, remote_answer, push,
  archive, tool_calls, v2 permissions, claude_provider, codex_provider,
  `slim_claude --check`, HUD tsc/vitest/build.

## Work packages

- **AS-A.** The module and its unit tests.
- **AS-B.** Broker (`describe`, fields), daemon (attach, publish, where),
  inbox observer, phone projection, QH record field.
- **AS-C.** HUD: the shared lib, the ordinary and Browser cards, the queue,
  the inbox's approval and deferred rows.
- **AS-D.** Phone: the answering card and the inbox rows.
- **AS-E.** MR (#329): each review item with its summary, code lines and
  expandable command, once #329 is on `main`.
- **AS-F.** Docs: control-plane notes, this plan's as-built section,
  CLAUDE.md (one sentence at most).

## As built (2026-10-28)

All of AS-A to AS-F, in `claude/approval-summary`; #329 (MR) and #314 were
merged from `main` first, and MR's frozen confirmed set, tags and subjects are
untouched. The rules as they stand are in `docs/notes/control-plane.md`,
"Approval summaries (AS)". Beyond the decisions above:

- **An agent's own words about its call are not sent** (AS-2): Claude's
  `description` and any `explanation`, `justification` or `reason` argument.
  The summary says what the call does, not what the agent says it does.
- **Bold and link syntax only are removed** from the answer; a lone `*` (a
  glob) and `__dunder__` names stay, since they are part of what is touched.
- **Arming is in the click handler too**, not only the `disabled` attribute,
  and re-arms when the summary changes (the owner reads the line that just
  landed). The Browser card arms when its private detail lands.
- **The toggle is shown whenever there is a command block** on the HUD and
  phone cards (also to close an expanded one); on inbox and review rows only
  when the record has a summary. On the phone it keeps its state across a
  re-read of the card (`CardState.expanded`, the app's `opened` map).
- **The layout check changed meaning**: "AUTHORIZE is below the fold for a
  long command" is now "AUTHORIZE is in reach before any scrolling", at the
  same zooms and grids (AS-12).
- **`tests/face/hud_v2_phone_answer_check.py`'s button-box read waits** for a
  box: a re-read of the card between two reads made it `None` (the known
  flake; it failed 3 of 3 runs on `main` here).
- **A deferred record with a summary reads as "not ours" to a daemon from
  before AS** (its field set is exact), so a downgrade shows no deferred rows
  until the file is rewritten or removed.

Tests: `tests/v2/approval_summary_check.py` (44, a fake model; 17 mutants,
each killed); vitest (the shared lib, the HUD card, the Browser card, the
phone card and rows, the reducer); the AS sections of the HUD, inbox, phone
answer and both morning-review face checks.

Open: Discord's post still shows the whole command and no summary (AS-16);
the 2–3 s target is unmeasured (no paid probe was run); the daily cap is in
memory, so a restart resets it.
