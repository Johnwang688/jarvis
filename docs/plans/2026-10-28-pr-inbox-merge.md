# Plan: PR rows become "agent summary + OK to merge" (PM)

**Status (2026-10-29): plan; PM-A to PM-F built (each "As built" at the end).** Written against main `a5798ea2`. It builds on the
owner inbox (IN-4, IN-7, `jarvis/v2/inbox_prs.py`), the phone surface (PH-4, PH-7, §4.1),
the morning review (MR) and approval summaries (AS, PR #330, not merged). PM-n are the
planner's defaults, reversible. The owner's decisions below are binding.

## What the owner asked (2026-10-28)

> "we need different handling for prs on mobile or just in general im not going to GitHub
> to read that I just need to be able to get an agents summary of why it was blocked and
> get my ok to merge"

The owner's decisions:

- **Blocked PR + OK = fix, then ask once more.** If a PR has conflicts or failing checks,
  OK starts an agent that resolves them. When the PR is green, the owner gets one short
  card, "fixed: <summary of the fix> — merge?". The fix is new code the owner has not
  OK'd, so it gets its own yes.
- **The summary is written ahead of time.** Haiku 5.5 writes it when a PR appears or its
  code or checks change.
- **No GitHub.** The summary says, in plain words, what the PR does and why it is blocked:
  which files conflict, which checks fail, and the gist of each failure.
- **Q1, a limited phone:** it may Merge and Fix, the same as Discord, **except PRs that touch
  safety files** (PM-10). Those need the desk or a fully checked phone.
- **Q2, overnight:** a Fix pressed at night runs. Its card waits in the inbox with no
  push from 23:00 to 07:00.
- **Q3, hold:** yes. At the desk the owner sets "hold: <note>" on a PR, and Merge and Fix
  are hidden everywhere until the hold is cleared. This is for override PR #12, which waits
  on the robot.
- **Q4, merge commits turned off:** squash, **named on the card before the OK**.

## Summary input

- **PM-1. Reads, by the daemon only.** A new module, `jarvis/v2/pr_facts.py`, runs on its own
  worker after a `PrSource` poll. It never runs in an agent shell and never holds the poll's
  or the daemon's lock. Each call goes through `inbox_prs.run_bounded` with
  `chat_git._owner_env()`: an exact argv, no shell, cwd `/`, its own process group, and
  stderr never read.
  1. `gh pr view N --repo O/R --json number,title,body,baseRefName,baseRefOid,headRefName,headRefOid,files,isCrossRepository,mergeStateStatus,statusCheckRollup,reviewDecision`
  2. `gh pr diff N --repo O/R`, up to 2 MiB. Past that, only the file list is used.
  3. `gh api --method GET repos/O/R/commits/<head>/check-runs?per_page=100`: each failing
     check's app, `output.title` and `output.summary`.
  4. For at most 3 failing Actions jobs (job id from a validated `detailsUrl`):
     `gh run view --job <id> --repo O/R --log-failed`, keeping the last 80 lines.
  5. `gh repo view O/R --json mergeCommitAllowed,squashMergeAllowed,viewerPermission`, cached
     for an hour.
  6. Only when the PR is DIRTY: the conflicting files, from
     `git merge-tree --write-tree --name-only <base> <head>`.
     - This runs in a daemon-only bare mirror, `V2_DATA_DIR/pr-mirrors/<key>.git`, mode
       0700. It is not the Codex-readable `chat-git-mirrors`.
     - The mirror is filled by a credentialed `git fetch --no-tags` of exactly the base and
       `refs/pull/N/head`, with gitops hardening and `_credential_pins`.
     - No checkout is made and no owner repository is touched. The mirror's own config
       means no hook, merge driver or filter can run.
     - The conflict-marker regions of up to 3 files are included.
- **PM-2. Caps.**
  - Each call gets 30 s, and a PR pass gets 180 s in all. One PR is worked on at a time,
    with at most 50 waiting.
  - The model input is at most 20,000 characters:
    - body 2,000;
    - logs and check output 6,000 in all;
    - conflict regions 3,000;
    - the diff gets the rest, at most 4,000 per file. Lock and generated files are
      named only.
  - The diff is trimmed first. When anything is trimmed, the facts line says
    "summary read N of M files".
- **PM-3. Scrub (use-but-never-see).** Every text read goes through
  `untrusted.strip_invisible`, then `secrets.scrub` and `redact_inline`, before it is stored
  or sent.
  - A credential-named path keeps its name, but its patch is withheld.
  - Logs get the same scrub, because GitHub masks only the secrets registered with it.
  - Nothing read is written to a log line.
- **PM-4. Fence and output.**
  - Everything the author or a check controls goes inside one `untrusted.fence` with a fresh
    nonce. The prompt says, as AS's does, that the fenced text is data.
  - The model writes two labelled lines:
    - `Does:` (240 characters at most);
    - `Blocked:` (300 at most, left out when nothing blocks).
  - The model gives no verdict and no recommendation. Code parses and cleans both lines. A
    missing `Does:` counts as a failure.
- **PM-5. Cache.**
  - The key is sha256 of (repo in lower case, number, head SHA, checks summary, merge state,
    prompt version).
  - It is stored on disk at `V2_DATA_DIR/inbox/pr-summaries.json` (mode 600, the newest 200
    entries), so a restart pays nothing.
  - A changed key re-summarizes. The exception is a move to pending checks: the earlier
    summary stays, marked "checks re-running".
  - At most 6 summaries per PR per day.
- **PM-6. Model and cost.** Tier `pr_summary` (`JARVIS_PR_SUMMARY_MODEL`), default Haiku 5.5,
  never Chinese-hosted. The daily caps are 300 calls and $0.25, kept in the cache file so a
  restart does not reset them. Past a cap, the state is `off`. Tokens, cost and time are
  logged.
- **PM-7. States.**
  - The state is pending, ready, failed or off.
  - When it is failed or off, the row shows the facts line and "AI summary unavailable:
    <reason>". Merge is still offered, because what authorizes it is the facts line plus the
    binding (PM-13), never the model's text.
  - A read that failed shows as "not read" and is never guessed.
- **PM-8. No model at start.** Only daemon2's `main()` attaches the summarizer.

## What code says, apart from the model

- **PM-9. The facts line (trusted).** Code builds it from validated `gh` fields only, for
  example:
  `O/R #N · feat → main · checks: failing (test, GitGuardian [secret scanner]) · conflicts: a.py, b.py · 12 files +340 −20 · head 1a2b3c4 · merge commit`.
  It sits above the model's text and is styled apart from it. Paths and check names are
  `clean_line`d and set with textContent.
- **PM-10. High-risk diffs are flagged by code.** The row gets an amber
  "touches safety files: …" line when any changed path is:
  - in `files.SELF_PROTECTED`;
  - `CLAUDE.md`, `AGENTS.md` or under `.claude/`;
  - under `.github/workflows/`;
  - a secret-scanner config;
  - credential-named.

  The model cannot remove the flag. A flagged PR is never merged or fixed from a limited
  phone (Q1).
- **PM-11. Secret scanners.**
  - A code table (GitGuardian's app slug and check names) labels such a check
    "[secret scanner]".
  - The model is asked to say which files were flagged and whether they look like test
    fixtures. That is display only.
  - A scanner-only failure gets **no Fix**: an agent never silences a scanner. It gets
    "Merge as is" when GitHub allows it (UNSTABLE).
  - When the scanner is a required check (BLOCKED), there is no button, and the row says only
    an admin override could merge it. Jarvis never uses that override.

## The merge path

- **PM-12. Buttons, by state.** Code decides from a fresh read.

  | State | Button |
  |---|---|
  | CLEAN or HAS_HOOKS, checks passing or none | **Merge** |
  | DIRTY, or checks failing (BLOCKED or UNSTABLE) | **Fix**. UNSTABLE also gets **Merge as is**. A scanner-only failure gets only Merge as is (PM-11). |
  | BEHIND | **Update**: `gh pr update-branch N --repo O/R` (GitHub merges the base in). A fresh Merge is then needed for the new head. |
  | checks pending | none ("waits on its checks") |
  | held by reviews or rules | none, saying what GitHub requires |
  | on hold (Q3) | none; the hold note is shown |

  Jarvis never passes `--admin` or `--auto`.
- **PM-13. The merge.** A new module, `jarvis/v2/pr_merge.py` (SELF_PROTECTED).
  - Its input is `{id, head, card}`. `card` is sha256 of the facts line, the summary text,
    the method and the offered actions.
  - Under a per-PR lock it reads the PR again. It requires that the PR is open and not a
    draft, that the head equals `head`, that the state still allows the action, that no hold
    is set, and that the card key matches. Anything else is a 409 "changed since you read it",
    a re-summary is poked, and nothing runs.
  - It then runs exactly `gh pr merge N --repo O/R --merge --match-head-commit <head>`, with
    `--squash` instead only per PM-14.
  - The result comes from reading the PR again, never from stderr. A receipt goes to
    `inbox/pr-actions.jsonl`, and a poll is poked.
- **PM-14. Method.** The default is `--merge`. Where merge commits are off, it is a squash,
  only when the facts line said "squash merge" before the OK; the method is part of the card
  key. Rebase is never used.
- **PM-15. Surfaces and authority.**
  - **HUD:** `POST /inbox/pr/merge`, `/inbox/pr/fix`, `/inbox/pr/update`, `/inbox/pr/hold`.
    These are owner_only on FACE_PORT, with the id in the body because a PR id holds `/` and `#`.
  - **Phone:**
    - `POST /m/api/inbox/pr/options {id, action}` returns a challenge.
    - `POST /m/api/inbox/pr {id, action, head, card, assertion}` acts.
    - Every action needs a fresh passkey assertion, bound to the subject `pr:` + the first
      56 hex of sha256("jarvis-pr-v1\n" repo \n number \n head \n card \n action).
    - It has its own subject family and challenge budget. An assertion is never shared
      across families.
    - When the phone scrub would change the facts line or the file list, it answers "answer
      it at the desk".
    - There is no hold on the phone.
  - **Limited phone:** may Merge, Fix and Update, except on PM-10-flagged PRs (Q1).
  - **Morning review:** no PR is part of a group approval (MR-1).
  - **Discord:** nothing new, and no merge path.
- **PM-16. Not merge_gate, and not an approval.** These routes are the owner's own act. They
  never touch `PendingApprovals` or `remote_answer`'s approval paths. No tool, MCP entry or
  API-listener route reaches them.

## The fix path

- **PM-17. What Fix starts.**
  - A v2 task in the one non-archived project that owns the repository. With no such
    project, or more than one, there is no Fix, and the row says why.
  - The provider is pinned to Claude Code, because Codex holds no GitHub login (CG-0).
  - The owner's OK is the intake gate.
  - The ceilings are $5 and 1 hour.
  - A fork gets no Fix.
- **PM-18. Worktree.** `worktrees.ensure_pr` fetches the bound head from the PM-1 mirror into
  the project's repository with a local fetch that uses no credentials, checks the SHA, and
  adds a worktree on `jarvis/<task>-pr<N>`. The task pushes `HEAD:refs/heads/<PR branch>`.
- **PM-19. The brief is built by code from the facts, never from the model's summary.**
  - It carries the repo, PR number, branch, base, bound head, the failing checks with their
    job ids, and the conflicting files.
  - Its rules:
    - merge the base in, never rebase;
    - push plainly to the PR branch only, never force;
    - never change CI or scanner config, or skip a test, to make a check pass;
    - stop and report a rejected push.
  - Logs are not copied in; the task reads them itself.
- **PM-20. The fix scope in the gate.** `pr_scope {repo, branch}` applies in
  `permissions.build_permit` at layer 2 (SELF_PROTECTED, boundary review).
  - **DENY:**
    - a push anywhere but `refs/heads/<branch>` on that repository;
    - force or delete;
    - `gh pr merge`, `close`, `edit`, `ready` or `review`;
    - any non-GET `gh api`;
    - writes to scanner config.
  - **Ask:** a push the gate cannot read.
  - Everything else goes through the task's ordinary attended gate, deferred in quiet hours.
- **PM-21. One fix at a time.**
  - `inbox/pr-fixes.json` (mode 600) holds `{task_id, from_head, from_patches, started,
    state}` for each PR.
  - A second Fix is a 409.
  - While it runs, the row says "fix running", with Cancel (`TaskControl.cancel`; on the
    phone, the `task:` passkey).
  - There are at most 3 fixes per PR, each with its own OK.
- **PM-22. Done and green.** On the task's DONE, and on each poll of a PR with a fix record,
  four things must hold:
  - the head has moved;
  - `from_head` is an ancestor of the new head (by `compare`; if it is not, there is no fix
    card, but an amber "branch rewritten since your Fix");
  - the checks pass;
  - the PR is CLEAN or HAS_HOOKS.

  Haiku then reads the **interdiff**: for each file, the patch at `from_head` against the
  patch now, plus the commit subjects. This means a merge of the base reads as nothing.
  The card reads "fixed: <summary> — merge?". It shows "N commits since your Fix", the
  PM-10 flags, and an amber "the fix changed CI configuration" when code sees one. Its Merge
  is PM-13, bound to the new head. A fix card appears whenever the head moves after an OK'd
  Fix, including after a cancelled task that had already pushed.
- **PM-23. Other ends.**
  - A task question becomes the task's own rank-2 row, and the PR row says "fix waits on
    your answer".
  - Stopped with no push: "fix stopped: <state>", with Fix again.
  - Pushed but still failing: "fix pushed; checks still fail", with Fix again within the cap.
  - Pushed and pending: "fix pushed; checks running".

## Display

- **PM-24. Layout,** on the HUD and phone inbox, from top to bottom:
  1. the title (code);
  2. the buttons (AS-12);
  3. the facts line and flags (code);
  4. the AI summary, labelled and set apart, in a two-line slot reserved from the first frame.

  On the HUD the buttons are disarmed for 600 ms after mount and after any change of
  content, and the check also runs in the click handler. On the phone, each action has its
  own passkey. No button is the keyboard default.
- **PM-25. GitHub link: HUD only,** small, below the summary. It is the way out when
  Jarvis says "cannot". There is none on the phone.
- **PM-26. Phone fields.**
  - `remote_api.DETAIL_FIELDS["pr"]` lists: repo, number, title, branch, base, author, fork,
    head, checks, merge_state, facts, flags, failing, conflicts, files, summary,
    summary_state, method, actions, card, fix and hold.
  - `url` goes in `DROPPED`, with its reason.
  - Every field goes through `scrub_structure`.
- **PM-27. Quiet hours.**
  - Summaries push nothing.
  - A fix card, or a stopped fix, sends one `{count, rank}` push, never between 23:00 and
    07:00.
  - A Fix pressed at night runs (Q2).
  - The owner may merge at night.

## Safety review

| Boundary | Why it holds | Tests |
|---|---|---|
| Toolsets | No new tool or MCP entry; the routes are owner-only or need a passkey | archive_check grep for `pr_merge`, `pr_fix`, `/inbox/pr/`; tool sweep |
| Merge only by the owner | Pinned argv with `--match-head-commit`, no `--admin` or `--auto`, card key checked; the fix scope DENYs merges | mutants that drop the SHA or add `--admin` fail; a stale head or card is a 409 and runs nothing |
| Secrets | PM-3 on body, diff, logs and checks; credential patches withheld; stderr never read | a token in a log or `.env` patch never reaches the fake model or the row |
| Injection | Buttons, facts and flags come from code; the OK is bound to the head and card; the brief comes from the facts; the summary is display only | injection fixtures; mutants that feed the summary into buttons, flags or the brief fail |
| Phone | An assertion per action on the `pr:` subject; limited plus flagged is refused; scrub mismatch sends it to the desk | cross-family refusal; limited-flagged refusal |
| Hold | A hold hides every action, and the merge route re-checks it under the lock | a merge on a held PR is a 409 |
| Quiet hours / MR | No push 23:00 to 07:00; MR is unchanged | push and review checks |
| Codex | No login; Fix is Claude-only | Fix with Codex is refused |
| Model | Its own tier, attached only by `main()`, with caps that survive a restart | no summarizer by default |

## Work packages (one PR each)

| WP | Content | After | Tests |
|---|---|---|---|
| PM-A | Factor the one-shot engine out of `approval_summary.py` into `summary_engine.py`, with AS's behavior identical | #330 merged | AS's checks and mutants unchanged |
| PM-B | `pr_facts.py`: reads, caps, scrub, mirror and merge-tree, facts, flags, scanner table, buttons. No model. | parallel with A | fake `gh` argv log (GET only); caps and timeouts; forks; a merge driver in `.gitattributes` never runs |
| PM-C | `pr_summary.py`: prompt, parsing, disk cache, caps, states, row fields | A, B | fake model, fence, key changes, no model at start |
| PM-D | `pr_merge.py`, hold, HUD and phone routes, `remote_auth` subject | C | safety table; owner_only; the API listener refuses |
| PM-E | Fix: `pr_fix.py`, `ensure_pr`, `pr_scope`, tracking, interdiff card | C, parallel with D | gate matrix with a mutant per DENY; one at a time; forks; rewritten branch |
| PM-F | HUD and phone rows, arming, hold UI, link, `DETAIL_FIELDS`, fix push | D, E | vitest; `hud_v2_inbox_check`; phone checks |
| PM-G | Notes, CLAUDE.md's Inbox sentence, SELF_PROTECTED additions | all | `slim_claude --check` |

Verified locally: gh 2.86 has `--match-head-commit`, `baseRefOid` and `files` in `gh pr view`,
and `mergeCommitAllowed` in `gh repo view`. Git 2.53 has `merge-tree --write-tree`.

## As built: PM-C (2026-10-28)

`jarvis/v2/pr_summary.py` (SELF_PROTECTED), wired into `inbox_prs.PrSource`, attached by
daemon2's `main()` only (`Daemon.attach_pr_summaries`). Tests: `tests/v2/pr_summary_check.py`
(fakes only) and `tests/v2/pr_summary_mutants.py` (24 mutants, each killed by the test named
for it).

- **Engine.** `PrSummaryEngine(SummaryEngine)`: tier `pr_summary` (`JARVIS_PR_SUMMARY_MODEL`,
  Haiku 5.5, never Chinese-hosted); PM-4's prompt; input = the code-built facts line
  (`facts_text`) and `pr_facts.model_input(facts).text`, display-scrubbed again, inside the
  engine's fence (at most 24,000 characters); nothing is sent for a PR not read, not open or a
  draft. `parse_summary` keeps the first `Does:` (240) and `Blocked:` (300) lines, cleaned as
  `clean_summary` cleans; no `Does:` is a failure. Deadline 90 s, one call at a time, 2 tries.
- **One file** `<data root>/inbox/pr-summaries.json` (0600, folder 0700) holds the newest 200
  summaries (the engine's `get`/`put` cache), the day's ledger (`StoredLedger`: 300 calls,
  $0.25) and the per-PR counts (6 a day; a cached summary is free, an ask the day's cap refuses
  gives its count back, as does a job that never got the one slot). Fixed in review of #335
  (MED 2): a budget or per-PR block that cannot be read (one bad byte included) moves the file
  aside (`pr-summaries.json.bad-<time>`, 0600) and starts a new one with **today's budget used
  up**, so today stays closed and tomorrow opens, logged once; a cached summary that cannot be
  read is dropped. A link or a folder at the path, or a failed write, still closes the budget
  for the process. Writes are fsynced before `os.replace` (in this module, not `stores`). A day
  before the stored one counts as the stored day.
- **Cost.** At Haiku 5.5's listed price ($0.10/M in, $0.50/M out under 100k), a full 20,000-
  character input is about 6k tokens: about $0.0007 a call. 300 calls is about $0.20, so the
  call cap normally binds before the $0.25 one. An estimate; no paid call was made.
- **Row fields** (added to each PR row's `detail`, `pr_summary.ENRICH_KEYS`): `facts`
  (`FactPart.as_dict()` list), `flags` (`{flagged, items: [{name, families}], more,
  incomplete}`), `summary` (`{does, blocked}` or null), `summary_state`, `summary_note`
  ("checks re-running" or ""), `summary_reason` (code's words for `failed`/`off`), and
  PM-12's `actions`, `why`, `method` from `decide(facts)` (no hold, not limited; display only,
  no route acts on them). The row's existing `head` is the bound head: facts are shown only
  when their head equals it. When the poll's checks or merge state moved since the read,
  `actions` is empty and `why` says it is being read again.
- **Refresh.** A read or summary that lands re-builds the row under the snapshot's lock and
  publishes `inbox_changed` `{ids, reason: "pr_summary"}` (rank 5: no push).
- **Tidy.** After each poll, `tidy_mirrors(state, listing_complete=...)` with each repository's
  open PR numbers when its list was read whole, None when it failed, was cut off or skipped
  (`listing_complete` only when at least one repository is named).

Deviations from the plan, each the builder's call:

1. **Reads are not repeated on every poll.** A PR is read when it is new, when the poll's
   (head, checks, merge state) moved since it was queued, after `RETRY_S` (5 min) when the
   last read was incomplete, and after `REFRESH_S` (30 min) in any case. Each read is four to
   six gh calls; re-reading every open PR every 5 minutes buys nothing the key would use.
2. **"Only the checks moved to pending"** is read as: the same head, checks now pending, and a
   merge state that is unchanged or one pending checks explain (BLOCKED, UNSTABLE, UNKNOWN).
   A move to DIRTY or BEHIND while checks run is summarized again.
3. **The facts line goes to the model**, inside the fence, so it can say why GitHub holds a PR
   (merge state, reviews) without a second source. It is code's text, fenced as data.
4. **The phone is unchanged:** a `pr` row is still a generic kind in `remote_api`, which keeps
   none of the new keys; `DETAIL_FIELDS["pr"]` waits for PM-F. The desk HUD reads only `repo`,
   `number`, `title` and `url` of a PR row (`hud/src/lib/inbox.ts` `prOf`).
5. **`pr_summary.py` is in SELF_PROTECTED now**, not in PM-G.
6. `PrSource.stop()` closes the engine for good; a daemon is not restarted in-process.

**Review of #335 (2026-10-28), fixed on the branch:** MED 1 the head-match, moved-list and
`REFRESH_S` rules now have tests and mutants; MED 2 above; LOW 3 no summary from a read that
missed its diff, checks, logs or conflicts ("read in part"; the queue reads it again after
`RETRY_S`; "too_large" counts as whole); LOW 4 the parser takes a label's text from the next
lines, the first label with text, and cuts `Does:` at an inner `Blocked:`; LOW 5 author and
check names go to the model quoted, as names; LOW 6 a summary that lands after the watchdog is
put on the row when the key still matches; LOW 7 a moved list marks the shown summary
"reading again"; NITs: the day never goes back, the poll's spelling of a repository is used,
and unread checks keep `decide`'s words. **Left, as the review allowed:** a symlinked
`inbox/` folder's ancestors are not checked (the folder itself is), and the engine still calls
the cache's `put` (a small file write) under its lock.

**Delta review of #335, fixed:**
- A bad file's bytes are hard-linked aside first. A whole new file then replaces it, so a write that fails leaves the bad file in place and a restart keeps today closed. The newest 3 copies are kept.
- When the same parts stay missing for the same PR state for `REFRESH_S`, the PR is summarized anyway:
  - the missing parts are in the key;
  - `summary_note` names them ("written without the CI logs, which could not be read");
  - until then, the row's reason names them too ("the CI logs could not be read; it is read again in 5 minutes").
- A late answer for an older head never lands; this is pinned by a test and a mutant.

## As built: PM-D (2026-10-29)

`jarvis/v2/pr_merge.py` (SELF_PROTECTED). Tests: `tests/v2/pr_merge_check.py` (fakes only;
real daemons on ephemeral ports for the routes and the phone) and `tests/v2/pr_merge_mutants.py`
(39 mutants, each killed by the test named for it).

- **Card (PM-13).** `pr_summary.card_key(detail)`: SHA-256 of canonical JSON (keys sorted)
  `{v: 1, facts, summary, method, actions}`, exactly the row's values. Every PR row carries
  `card`; the act recomputes it from a fresh `pr_facts.read_pr` (90 s budget) and the row's
  current summary.
- **Routes.** HUD, behind `inbox.route`'s `owner_only` plus the HUD listener's own Host:
  `POST /inbox/pr/merge {id, head, card, action?}` (`merge` or `merge_as_is`),
  `/inbox/pr/update {id, head, card}`, `/inbox/pr/hold {id, note}` or `{id, clear: true}`,
  `/inbox/pr/fix` (501 until PM-E; now PM-E's Fix). Phone, mounted in `remote_auth.api_route`:
  `POST /m/api/inbox/pr/options {id, action, head, card}` (the head and card the phone
  showed; 409 when the row has moved since; nothing echoed back) and `POST /m/api/inbox/pr {id, action, head, card, assertion}`.
- **The act.** Under a per-PR lock (a second action on the PR while one runs is a 409, never a
  second run): no hold (before and after the read), open, not a draft, the same head, the
  action still offered, a merge method, the card equal; a limited phone and a flagged PR, or a
  phone whose scrub would change the facts or file list, is "answer it at the desk". Then
  exactly PM-13's argv; the result from `gh pr view N --repo O/R --json state,headRefOid`;
  receipts in `inbox/pr-actions.jsonl` (0600): an `intent` line before gh runs and a `result`
  line after (gh's exit word and the result); a re-read and a poll poked.
- **Hold.** `inbox/pr-holds.json` (0600), `{"owner/repo#N": {note, set_at}}`, the note
  scrubbed, one line, at most 200 characters; at most 500 holds. A file that cannot be read
  holds every PR and refuses set and clear (503) until it reads again; it is never
  overwritten.
- **`remote_auth`.** `PR_DECISIONS` (`merge`, `merge_as_is`, `update`, `fix`: Fix's subject is
  the same family, for PM-E), `_PR_SUBJECT` `pr:[0-9a-f]{56}`, family `pr` with its own
  per-session budget.

Deviations, each the builder's call:

1. **Row fields.** The row now carries PM-26's `base`, `failing`, `conflicts` and `files`
   (`pr_summary.files_detail`, at most 100 paths), plus `hold` and `card`.
2. **Phone fields.** `DETAIL_FIELDS["pr"]` is PM-26's list minus `fix`, plus `why`,
   `summary_note` and `summary_reason` (code's words for why there is no button, which the
   phone needs). `url` is in `DROPPED["pr_detail"]`.
3. **`remote_api.scrub_pr`.** `scrub_structure`'s pairs pass reads a facts part's
   `"key": "branch"` as a credential label and withheld every part, so each part's `key`
   (code's word, from `PR_FACT_KEYS`) is set aside and put back; an unknown one is withheld.
4. **Merge as is is its own action** on both surfaces, never implied by `merge`.
5. **Update is head-bound** (review of #336, LOW 3): `gh api --method PUT --include
   repos/O/R/pulls/N/update-branch -f expected_head_sha=<head>`, not `gh pr update-branch`,
   which takes no head. Only the status line is read: 202 is "update requested" (GitHub
   updates asynchronously), 422 a 409 "changed since you read it". It is the daemon's own
   owner route, not an agent command, so `pr_facts`' GET-only reads and PM-E's `pr_scope`
   are untouched.
6. **No merge queue, no auto-merge** (review of #336, MED 1). gh 2.86 has no
   `isMergeQueueEnabled` or `isInMergeQueue` field (its "Available fields" list, checked
   offline), so before gh runs: `gh pr view --json autoMergeRequest` (every action) and
   `gh api --method GET repos/O/R/rules/branches/<base>?per_page=100` (merges). Auto-merge
   on, a `merge_queue` rule, or either unread or possibly cut off is a 409 "its base uses a
   merge queue or auto-merge: merge it at GitHub". The result read includes
   `autoMergeRequest`, so a PR gh queued anyway reports "queued". Merge queues exist only on
   an organization's repositories, and the rules GET answers 403 on a user's private
   repository without GitHub Pro (live probe), so `gh api --method GET repos/O/R` is read
   first (`owner.type`, cached an hour): "User" skips the rules read; "Organization" reads
   them and fails closed (a 403 included); anything else refuses. Residual, organizations
   only: a queue set only in classic branch protection, if the rules endpoint does not
   list it, is not seen.
7. A row whose detail fails to build now also drops its actions (it kept them before).

## As built: PM-E (2026-10-29)

`jarvis/v2/pr_fix.py` (SELF_PROTECTED), `worktrees.ensure_pr`, the PR scope in
`permissions.build_permit`, attached by daemon2's `main()` only (`Daemon.attach_pr_fixes`).
Tests: `tests/v2/pr_fix_check.py` (fakes for gh, the runner and the model; real git on temp
repositories) and `tests/v2/pr_fix_mutants.py` (a mutant per DENY family and per guard, each
killed by the test named for it).

- **Start (PM-17).** `PrFixes.start_fix(id, head, card, via, limited=None)`: the row's head and
  card (PM-D's `pr_summary.card_key`, the row's `card`) must match; the facts held for that
  head (`PrSummaries.facts_for`) must make `decide(facts, hold, limited)` offer Fix, the hold
  read from PM-D's `pr-holds.json`; `limited` is True for any `via` but "hud" unless the caller
  says otherwise. Auto-merge must be off (PM-D's `PrActions.queued`, failing closed): with it
  on, the fix's push would merge code the owner has not OK'd. Exactly one non-archived project
  owns the repository, its profile is not strict, Claude Code is in the roster. Routes: PM-D's
  `pr_merge.hud_route` hands `POST /inbox/pr/fix` and `/inbox/pr/fix/cancel` to
  `pr_fix.hud_route` after `owner_only` and the HUD listener's own Host; the phone's action
  `fix` (`pr_merge.phone_options` / `phone_act`, the `pr:` subject) reads the hold and the
  phone's scrub again and calls `pr_fix.start(..., via="phone", limited=...)`. One call creates
  the task (`pr_scope`, ceilings $5 and 1 hour, `provider_override`
  Claude), fills the mirror, runs `ensure_pr` and `TaskControl.start`; a step that fails cancels
  the task and records "fix could not start" (not counted against the cap).
- **Claude only.** `Task.pr_scope` is set at create and kept by every save (`TaskStore.save`).
  The runner refuses to open a fix's thread on another provider (`routing_refused`, BLOCKED) and
  to resume one there; the gate refuses every call of a Codex session for it.
- **Worktree (PM-18).** `fill_mirror` (PM-1's credentialed, hardened fetch into the daemon's
  mirror, held under its lock; the pull ref must be the bound head) then `ensure_pr`: a local
  fetch with no credential helper, the file transport only, gitops' hardening, onto
  `jarvis/<task>-pr<N>`; a wrong head deletes the branch and makes nothing; a repository whose
  own config names a program is refused.
- **The gate (PM-20).** `pr_fix.judge`, right after layer 1. DENY (layer `pr-scope`, asked of
  nobody): another destination (branch, tag, any ref), a URL, path or other remote (push URLs read
  from config: `pushurl` first), `--all/--branches/--mirror/--tags/--follow-tags` and a mirror
  remote, force (`-f`, `--force*` in git's abbreviations, `+` refspecs), delete (`-d`, `--delete`,
  `:ref`, `--prune`), `--repo`, `--receive-pack`, `--exec`, `--recurse-submodules`, `send-pack`,
  `http-push`, `subtree push`; `gh pr merge|close|edit|ready|review`; a non-GET `gh api` (explicit,
  or implied by fields, `--input`, `graphql`); `gh repo sync`, `gh workflow disable`, `gh
  secret|variable set|delete`; writes to CI or scanner config (file tools, redirects, writers,
  `git rm|mv|checkout|restore`); Codex. ASK (always-ask, no Always): no refspec, a bare HEAD,
  expansions, `-c`/`--git-dir` and kin, a line that changes a remote or config, `include`/`insteadOf`
  config, an unknown git subcommand or gh command, unreadable text that spells a push or gh, a
  line too complex to walk. A task whose record cannot be read takes its fix record's scope; a
  record that cannot be read either refuses every call. v1 has no tasks with a scope, so there
  is no v1 half.
- **Tracking (PM-21).** `inbox/pr-fixes.json` (0600 in 0700) per PR: `{task_id, from_head,
  from_patches, started, state, ended_at, project_id, branch, note, scope}`. A second Fix is a 409
  naming the task; a blocked task still holds the PR (Cancel it first); at most 3 started.
- **Ends (PM-22, PM-23).** `detail.fix` = `{state, task_id, from_head, commits, ci_changed, note,
  summary, summary_state, count, max}`; states starting, running, question, blocked, stopped,
  checking, rewritten, failing, pushed, ready, start_failed. A running task shows "fix running"
  even after a push; the card waits for the task's end. Ancestry and "N commits since your Fix"
  come from one GET compare (`--jq`, first-parent chain from the new head, so a merged base's
  commits are not counted). The interdiff compares each file's changed lines only (no hunk
  headers, context or index lines), so a base merge reads as nothing; `ci_changed` is code's
  (True, False, or None when not every file could be compared). The summary is one `Fixed:` line
  from a `SummaryEngine` on the PR summaries' day ledger, fenced and scrubbed.
- **Push (Q2).** An ended fix (not a failed start) puts its row at rank 4 for 24 hours, so the
  phone's sender pushes `{count, rank}` once, never in quiet hours (the morning push carries it).

Deviations, each the builder's call:

1. **Rank 4 is the push.** `push.py` pushes only for rows new to ranks 1 to 4, so the fix's row
   rises there for a day after the fix ends (as a deferred approval's does), rather than a second
   push path.
2. **The card covers decide's actions.** While a fix runs, Fix is taken out of the row's
   `actions` after the card is made, so the card (and PM-D's recomputation from a fresh read)
   still covers the actions `decide` gives; the phone's options refuse Fix because the row
   no longer offers it.
3. **A blocked fix holds the PR.** It can be resumed and push again, so Fix again needs it
   cancelled first.
4. **The base is fetched by the task** (`git fetch origin <base>`), not seeded from the mirror.
5. `fix` is in `ENRICH_KEYS` (None when the PR has none) and `DETAIL_FIELDS["pr"]`.
6. **Not built here (PM-F):** the HUD and phone rendering of `detail.fix`, and Cancel on the phone
   beyond the existing `task:` route.

**Review of #337 (2026-10-29), fixed on the branch.** The owner's decisions: a fix keeps his
GitHub access, with added blocks (not the daemon-pushes design), and Fix stays offered on any
PR. Each item has a test in `pr_fix_check.py` and a mutant in `pr_fix_mutants.py`.

- **H-1, the credential.** In a scoped task: a read tool, file tool or line under any
  `config.credential_dirs()` folder; `git credential*`, credential settings, `gh auth`; git and
  gh traces; fetchers or inline code aimed at GitHub's hosts: DENY. Inline source: asked.
- **M-1, another repository.** `merge_gate._git_dirs` follows git's order (a `.git`, then the
  folder itself as a bare repository); `--bare` makes merge_gate's folder unknown. A fix
  pushes only from its own worktree's git dir; `--bare`, `--git-dir`, `--work-tree`,
  `--namespace`, `GIT_DIR` and kin: DENY.
- **M-2, CI.** `.github` itself and anything under it counts; removals, moves and restores
  (`rm`, `mv`, `git rm|mv|clean`, `git checkout -- <path>`, `git restore`) are read against
  the line's folders, the worktree itself counting; pathspec magic and unplaceable writes ask;
  `ci_changed` reads old names (`Patch.flags`).
- **M-3.** Only `<ref>:refs/heads/<branch>`, written in full.
- **M-4.** gh is an allowlist of reads; `gh api` abbreviations and method-override headers
  are read.
- **M-5.** `git config`/`git remote` writes ask; a push under `core.hooksPath`,
  `core.sshCommand`, `core.gitProxy`, `remote.*.receivepack|uploadpack` or `url.*`, or under
  credential helpers other than at the Fix (`Task.pr_scope["helpers"]`, a digest), is DENY;
  so are writes to `.git/config`, `.git/hooks`, `.git/info`, the gitfile and `.gitmodules`.
  (The owner's own config sets `credential.helper`, so a helper's presence cannot be the test;
  a change since the Fix is.)
- **L-1** a head branch that is `main`, `master`, `release*` or the remote's default gets no
  Fix; **L-2** the task id is in the record before the worktree and the start, and a record
  that cannot take it is a failed start; **L-3** any failed start cancels the task and removes
  its worktree and branch; **L-4** programs that run a line or outlive the call, and pager or
  editor overrides, ask; **NIT** the permit's fallback reads the fixes root `PrFixes` writes.

**Residual, accepted by the owner.** The task runs the PR's own code (tests, scripts, the
build) as the owner's OS user; that code can read the owner's GitHub token from disk and use
it however it likes, and the gate reads no script file. Fix on a PR whose code the owner does
not trust can act with his GitHub access. The fix card's ancestry check and its CI amber
check what reaches the branch, not what such code does elsewhere.

**Delta review of #337 (2026-10-29), fixed.** H-1 reached the token by spellings the first
fix did not place; now, in a scoped task, every argument is placed (home and `$HOME`
expanded, braces expanded, globs matched component by component against each credential
folder, relative paths against the line's folders after `cd`, same-line `NAME=value`
variables put in): inside a credential folder is DENY, an ancestor of one under a program that
reads, copies, archives or searches a tree (or the Grep and Glob tools) is DENY, a path built
by a command substitution is asked. A file copied elsewhere is not traced back; the copy is
what is refused. Fetchers aimed at GitHub by any spelling, or forcing an address or a Host
header, are DENY; to an address literal or an expansion, asked. Inline code is read by a token
parser per interpreter (clustered flags, `--eval`, `-Command`, awk programs, stdin), and a
shell fed a program on stdin or from a file (`sh < x`, `| bash`, `bash x.sh`, `source x`) is
asked. The credential helper's program is pinned at the Fix by real path and content hash
(`Task.pr_scope["helpers"]`, `["helper_paths"]`); a push after either changed is DENY, as is a
write to that program, to a file of its name in a PATH folder ahead of it, to `~/.local/bin`
or `~/bin`, or to the owner's global git config. `git apply|am|cherry-pick|reset
--hard|stash pop|apply`, `patch`, `tar -x`, `unzip`, `7z x` and `cpio -i` are asked (the tree
always holds `.github`); the card's CI amber stays the backstop. `gh … --web` is DENY; L-1
adds `prod*` and `production*`.

**Last item (2026-10-29).** `Task.pr_scope["base"]`: a fix merges in only `origin/<base>` (or
`FETCH_HEAD` after `git fetch origin <base>` on the same line) and pulls only `origin <base>`
without rebasing; any other source, or none, asks. `git read-tree -u|--reset` and `git
checkout-index -a|-f` ask.

**Final delta review of #337 (2026-10-29), patched; shipped as an accident guard, not a wall.**
The owner's decision: the PR scope refuses what an agent fixing a PR might do by mistake or on a
hint, by every spelling the reviews found, and asks where it cannot read the line; a
deliberately hostile agent could still find a way past it, as with the PR's own scripts. The
last patches: xargs' replace string is read as a wildcard and every xargs is asked; a
reader's path from a command substitution or a variable (`read`, `mapfile`, IFS tricks) is
asked, and paths are resolved through symlinks, `/proc/self/cwd/..` and `~user`; writes to
`~/.curlrc`, `~/.netrc`, `~/.wgetrc`, `~/.config/curlrc` and anything under `CURL_HOME` are
DENY, and `curl -K|-n|--netrc*`, `wget -i|-e`, `aria2c -i` and a `CURL_HOME`/`NETRC`/`WGETRC`
setting are asked; `sed` with `e`, `s///e` or `-f`, and any awk program (inline or `-f`), are
asked; `git update-ref`, a tag or branch named `origin/…`, a fetch from a local path or URL or
into a remote-tracking ref or tag from anything but `origin` + the base or the PR's branch,
`git rebase`, and `-c pull.*|rebase.*|branch.*` are asked.

## As built: PM-F (2026-10-29)

UI only: no daemon route, field or rule changed. The shared rules are `hud/src/lib/prRow.ts`; the
HUD row is `hud/src/components/InboxPrRow.tsx` (in `InboxView`), the phone's `prBlock` in
`hud/src/mobile/views.ts` with `PhoneApp.prAct` and `cancelFix` in `mobile/app.ts`. #338's
interim phone row ("Merge it at the desk for now") is gone; the read-only view says "Merge at
the desk".

- **Layout (PM-24), both surfaces:** the row's title (code), the PR's own title (labelled, in a
  `<bdi>`), the buttons, the row's one notice slot, code's `why`, squash named before the OK
  ("squash merge: merge commits are off for this repository", Q4), the hold, the fix, code's
  facts parts (each name a boxed `<bdi>`), the amber "touches safety files" line, then the AI
  summary in an `.as-ai` slot held two lines high from the first frame ("summarizing…", or
  "AI summary unavailable: <reason>"), then `summary_note` as code's line under it. A scanner's
  "[secret scanner]" is drawn as code's label only when the row's own `failing` marks that
  check as a scanner; a check merely named "x [secret scanner]" stays one name.
- **Binding:** every action sends the `head` and `card` the row drew (`actionBody` of the view
  built at render); nothing is read again at click time. A 409 is said in the daemon's words in
  the row, the inbox is read again, and nothing is retried.
- **HUD:** Merge, Merge as is, Fix, Update post once to `/inbox/pr/<route>`; armed 600 ms after
  anything the row shows changes (`prArmKey`: the whole view, so the PR title, the summary
  caveat and the unavailable-summary reason re-arm too) through `useArmed`, and the click handler checks again. "Hold…"
  opens a note box (200 characters) and Set hold posts `{id, note}`; a held row shows "On hold:
  <note>" and Clear hold (`{id, clear: true}`), and no action button. The GitHub link (PM-25) is
  last, small, `prOf`'s validated URL only.
- **Phone:** Fix and Update at the left, Merge and Merge as is at the right, far apart (the
  audit's H4 plan). Each action asks `/m/api/inbox/pr/options {id, action, head, card}`, then
  the passkey, then `/m/api/inbox/pr` with the assertion; the row's fixed notice slot says
  "Asking for Face ID…", then "Merging…" / "Starting fix…" / "Updating…", then the result
  ("Merged.", "Not merged: head moved.", the refusal). A limited phone (`/m/api/me`'s
  `secure.ok` false) hides every action on a PR the daemon does not mark unflagged and says
  "touches safety files: answer it at the desk or on a fully checked phone" (Q1); the server
  refuses it anyway. Held: the note, nothing to press, no hold control. No link, no head SHA.
- **Fix (PM-21 to PM-23), from `detail.fix`:** "fix starting/running (task <id8>)" and "fix
  waits on your answer" with **Cancel the fix** (HUD: `/inbox/pr/fix/cancel`; phone: the task's
  own `cancel/options` and `cancel` passkey flow) and, for a question, "Answer its question"
  (the task's `tq:` row, focused; "Open its task" when that row is not in the inbox); "fix
  stopped: …", "fix pushed; checks still fail", "fix could not start: …" with **Fix again**
  where `actions` offers Fix; "fix pushed; checks running"; amber "branch rewritten since your
  Fix". The fix card reads code's "Fixed — merge?", the fix's summary in its own AI slot, "N
  commits since your Fix", amber "the fix changed CI configuration" (or, `ci_changed` null,
  "not every file of the fix could be checked for CI changes"), the row's flags, and the row's
  Merge, bound to the row's (new) head and card. Rank 4 rows group under "Waiting on you".
- **Tests:** vitest `lib/prRow.test.ts`, `components/InboxPrRow.test.ts`,
  `mobile/pr.test.ts` (the interim H4 tests in `views.test.ts` removed), with mutants run by
  hand (read again at click on each surface, the handler's arming check, the disabled
  binding, Q1's pre-hide, Q3, READY's note, the scanner label, a cancelled passkey, the 409
  re-read): each killed. Face: `hud_v2_inbox_check.py` section `pr_action` (the mock's
  `/inbox/pr/*`), `hud_v2_phone_pr_check.py` (a real daemon, PM-C/PM-D on fake gh, the virtual
  authenticator: one PR merged end to end, a moved card's 409, every state, Cancel, limited),
  states in `tests/face/hud_v2_pr_rows.py`.

Deviations, each the builder's call:

1. **The fix card's daemon note is never drawn.** `fix.note` for `ready` is "fixed: <the model's
   summary> — merge?"; the row draws code's "Fixed — merge?" and the summary only in its AI slot.
2. **Cancel stays on a held row.** Q3 hides Merge and Fix; cancelling a running fix acts on
   nothing the hold protects, so Cancel the fix is still offered.
3. **A limited phone hides Update too** on a flagged PR, not only Merge and Fix: the server's
   `phone_options` refuses every action there.
4. **The fix card is in the card key (fixed after review).** `card_key` (version 2) covers the
   fix card's state, note, summary, summary state, commits and `ci_changed` once its summary has
   settled (a pending "summarizing…" binds nothing, so the landing always changes the card).
   `pr_fix.enrich` recomputes the row's card after the fix is in (over the actions `decide`
   offered), and `pr_merge.judge` recomputes it from the row's current `fix`; a fix summary
   that landed after the drawing is a 409 "changed since you read it".
   `hud_v2_pr_fix_binding_check.py` pins it.
5. The legacy row (no `card`, a daemon before PM-C) offers no action, only Hold… and the link.
