# WP5 — permissions v2: the five layers, always-ask, and the escape hatch

Work package 5 of the Jarvis v2 redesign, implemented in a git worktree on
branch `jarvis/wp5-permissions`. This is the safety layer; Claude leads and
you are Claude. This brief is your entire scope. Read first, in this order:

1. `docs/jarvis-v2-design.md` — §6 in full (the five layers, 6.1 the escape
   hatch, 6.2 profiles), §2 D6/D7, §5.3 (the hook is the gate on Claude),
   §15 R2 (answered: hook may block; 660 s), R3 (answered: no sandbox on
   Claude workers), R8 (open: always-ask on Codex — **do not solve R8
   here**; leave the seam and say so).
2. `CLAUDE.md` *Safety design* — the command rules, the allowlist rounds,
   the human-backed rule, the secrets layers. Every property there is one
   this package must keep: read the *Gate round 3* and *Narrowing* sections
   especially.
3. v1 code you build on and must not weaken: `jarvis/rules.py`
   (`verdict`, `segments`, `command_stems`/`command_targets`, DENY/ALLOW
   sets), `jarvis/permissions.py` (`gate`, `allows`, `entry_for`,
   `jarvis_human_backed`), `jarvis/tools/secrets.py`
   (`protected_in_command`, `scrub`), `jarvis/tools/files.py`
   (`SELF_PROTECTED`, `_self_protected`), `jarvis/face/approvals.py`
   (`ApprovalBroker` — the one-shot id and deny-on-every-failure design),
   `jarvis/discord_approvals.py` (the 4-char code rule). Tests:
   `tests/rules_check.py`, `tests/permissions_check.py`.
4. v2, merged: `jarvis/v2/provider.py` (`PermissionCallback`, `Decision`,
   `Brief.always_ask`, `EventKind.REVIEWER_DECLINED`), `providers/claude.py`
   (how the hook calls `permit` and what it does with ALLOW under AUTO vs
   ASK — read `docs/codex-briefs/WP3-notes.md`), `providers/codex.py`
   (refuses non-empty `always_ask`; `WP4-notes.md`), `providers/fastpath.py`,
   `daemon.py` (`permit_factory` seam, `placeholder_permit_factory`,
   `answer`, the EventBus, routes) and `WP7-notes.md`.

## Deliverables

### `jarvis/v2/permissions.py` — `build_permit(ctx, asker) -> PermissionCallback`

`ctx` carries project, task (may be None for chat), thread id, brief,
provider name. The callback evaluates, in order, and the first layer to
answer wins:

1. **DENY.** For command tools — `Bash` (Claude), `shell` (Codex),
   `run_command`/`run_readonly` (fast path, MCP) — `rules.verdict(cmd)` is
   DENY, or `secrets.protected_in_command(cmd)`. For file tools — `Write`,
   `Edit`, `MultiEdit`, `NotebookEdit` (Claude), `apply_patch` (Codex),
   `write_file`/`edit_file` — any target path that is SELF_PROTECTED,
   `config.ALLOWLIST_PATH`, a protected credential name, or the v2
   `routing.json`/`models.json`/allowlist trio under `~/.config/jarvis`.
   DENY returns `Decision.DENY` with a reason string the model can act on,
   and is **never** asked of anyone.
2. **ALWAYS-ASK.** `config.V2_ALWAYS_ASK` (new; env-overridable JSON path
   with a shipped default list) + `project.always_ask` additions, matched
   against the command's segments (`rules.segments`) and against tool
   names. Shipped defaults, each with a one-line reason in the file:
   `vercel --prod`, `vercel deploy --prod`, `gh release`, `git push` to
   `main`/`master`/`prod*`/`release*` (any spelling incl. `origin HEAD:main`
   and `--force` anywhere), `git push --force*` to anything, database
   migrations against a production target (`alembic upgrade`, `prisma
   migrate deploy`, `rails db:migrate`, `flyway migrate`, `dbmate up`,
   `supabase db push` — asked when any env var or flag in the segment names
   prod/production/live, and asked *unconditionally* for `prisma migrate
   deploy` and `supabase db push`, which are prod-shaped by design),
   payments/purchases (`stripe`, `vercel domains buy`, anything with
   `--pay`/`purchase`), credential creation or rotation (`gh auth`,
   `vercel login`, `aws configure`, `ssh-keygen`, writes under `~/.ssh`,
   `~/.aws`, `~/.config/gh`, `~/.config/jarvis`), and the outward tools
   `gmail_send`, `discord_send`. A match → `asker.ask(request)` → its
   Decision. ALLOW here means the owner said yes; the provider still runs
   the classifier under AUTO (WP3's convention) — document that.
3. **CLI reviewer** — not here. Return `Decision.ALLOW` to mean "no
   opinion, let the provider's own gate decide". Under AUTO the Claude
   hook maps that to `{}`; under ASK it is an allow. Make the semantics
   explicit in the docstring: **ALLOW from this callback is "not refused
   by Jarvis", not "approved by the owner"**, except when layer 2 or 5
   produced it — carry which in the `ApprovalRecord` you log.
4. **Jarvis ALLOW.** `permissions.allows(tool, args)` (the owner's
   persistent allowlist) or `rules.verdict == ALLOW`, honoured **only when
   `asker.human_backed` is True** — the v1 rule, kept verbatim: a deny-all
   asker (strict profile, an unbound context) never auto-approves.
5. **Human.** Everything else under profile `ASK` → `asker.ask`. Under
   `AUTO` → ALLOW (layer 3). Under `STRICT` → DENY.

Every decision appends an `ApprovalRecord` (tool, args digest, layer,
decision, reason, thread, task, provider, at) to
`config.V2_DATA_DIR/decisions.jsonl` — the v1 decision log's role.

### `jarvis/v2/approvals.py` — `PendingApprovals`

The v2 broker: `ask(request) -> Decision` blocks up to `timeout` (default
120 s local, 600 s when a remote surface is attached — a constructor flag
WP10 will set) and returns DENY on timeout, shutdown, or any error;
`resolve(req_id, decision, *, always=False)` resolves exactly once (a
second resolve is refused); ids are 72-bit one-shot tokens; each request
also carries a 4-character **code** for the Discord half; `pending()`
lists open requests; `on_request`/`on_resolve` callbacks publish
`approval_requested`/`approval_resolved` as bus events with `origin`
(task label, sanitized as v1 `_label` does) and the **entire command**.
`always=True` writes the persistent allowlist via v1 `entry_for` (which
raises on compound commands — keep that: the approval stands, the entry
is not minted). `human_backed` is True for this broker. A `DenyAll` asker
with `human_backed=False` is the strict/unbound one.

### Daemon wiring (edit `jarvis/v2/daemon.py`, minimally)

Replace `placeholder_permit_factory` with one that builds `build_permit`
over a daemon-owned `PendingApprovals`; add `GET /approvals` and
`POST /approvals/{req_id} {decision, always?}`; publish the two approval
events on the bus. Keep every WP7 test green.

### `jarvis/v2/hatch.py` — the escape hatch (§6.1)

`EscapeHatch(daemon, approvals)` subscribes to the bus for
`reviewer_declined`. When one arrives for a thread whose turn has finished
(wait for `turn_finished`), it asks via `PendingApprovals` with reason
`"reviewer declined: <exact command>"` and `origin` the task. On DENY: send
the thread a `UserMessage("[owner declined to run: <cmd>]", origin=
"system")`. On ALLOW: re-check layer 1 (**DENY is unreachable through the
hatch** — a DENY here sends `"[refused by Jarvis rules: <reason>]"` and
logs it); otherwise run the command with `subprocess.run(["bash", "-lc",
cmd])` — no, **not** `-l`: `["bash", "-c", cmd]` — in the task worktree
(or project root), env stripped of Jarvis secrets, output capped at v1's
limit and passed through `secrets.scrub`, timeout 600 s; then send
`UserMessage("[owner ran: <cmd>]\n<output>", origin="owner-ran")`. Log
`reviewer-declined-owner-ran`. Never mint an allowlist entry from this
path.

## Tests: `tests/v2/permissions_check.py`

Free. Point `config.ALLOWLIST_PATH`, `V2_DATA_DIR`, `V2_ALWAYS_ASK` at temp
files. Cover: a decision matrix of ≥ 40 commands across all four profiles
(as `rules_check` does) with the expected layer and decision each;
DENY never calling the asker; every shipped always-ask default matching
its intended spelling *and* not matching the ordinary form (`git push
origin feature`, `alembic upgrade head` with no prod marker, `vercel`
without `--prod`); project additions; file-tool DENY on each protected
target incl. the v2 config trio and a path spelled relative/with `..`;
layer 4 honoured only human-backed (a DenyAll asker never auto-approves an
ALLOW command — the regression the v1 round nearly shipped); layer 5 under
each profile; the decision log written per call; `PendingApprovals`: ask/
resolve/timeout/double-resolve/shutdown, code one-shot, `always` via
`entry_for` incl. the compound-command refusal, events published with the
whole command and the sanitized origin; daemon routes end to end with a
fake provider whose fake hook calls `permit`; the hatch: declined → ask →
ALLOW → runs in the worktree → output reaches the thread as `owner-ran`;
DENY → not run, message says so; a DENY-rule command through the hatch →
refused, not run, logged; timeout → not run; no allowlist entry minted.
Then run `tests/rules_check.py`, `tests/permissions_check.py`,
`tests/v2/daemon_check.py`, `tests/v2/claude_provider_check.py`,
`tests/v2/codex_provider_check.py`, `tests/v2/fastpath_check.py`.

## Rules

Stay inside `jarvis/v2/permissions.py`, `jarvis/v2/approvals.py`,
`jarvis/v2/hatch.py`, `jarvis/v2/daemon.py` (wiring + two routes only),
`jarvis/config.py` (the one constant + default list), `tests/v2/`, and
`docs/codex-briefs/WP5-notes.md`. Never edit `jarvis/rules.py`,
`jarvis/permissions.py`, `jarvis/tools/secrets.py`, `jarvis/tools/files.py`
or `jarvis/face/approvals.py` — they are the v1 gate and stay as they are.
No new dependencies. Never print a credential. When green, commit with a
message starting `v2 WP5: permissions — five layers, always-ask, escape
hatch` ending with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
Do not push or merge. Finish with `WP5-notes.md` (under 80 lines): what
you built, the shipped always-ask list with reasons, test commands and last
lines, what R8 still needs, proposed interface changes. Report branch,
worktree path and notes path as your final message.
