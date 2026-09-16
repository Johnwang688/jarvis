# WP5 — permissions v2

## Built
- `permissions.py` — `build_permit(ctx, asker)`: layer 1 DENY (never asked), 2
  always-ask, 4 Jarvis ALLOW **for human-backed askers only**, 5 profile (ASK
  asks, AUTO returns ALLOW = "no opinion, layer 3 decides", STRICT denies). Each
  call appends an `ApprovalRecord` to `V2_DATA_DIR/decisions.jsonl` naming
  **which layer answered**: ALLOW means "not refused by Jarvis", never
  "approved by the owner", and only the layer tells them apart.
- `approvals.py` — `PendingApprovals` (72-bit one-shot ids, 4-character code,
  120 s local / 600 s remote, DENY on timeout/shutdown/nowhere-to-ask/any error,
  second resolve refused, request/resolve callbacks) and `DenyAll`.
- `hatch.py` — `reviewer_declined` → wait for `turn_finished` → ask with the
  exact command → ALLOW **re-checks layer 1**, then `bash -c` in the task
  worktree, env stripped, output scrubbed, capped at 20 000, 600 s →
  `UserMessage(origin="owner-ran")`. Never mints an allowlist entry.
- `daemon.py` — `permit_factory=None` means the WP5 policy over a daemon-owned
  broker; `GET /approvals`, `POST /approvals/{id} {decision, always?}`; both
  approval events on the bus; `main()` runs the hatch. An injected factory still
  wins, so WP7 is unchanged. `config.py` gains the three new constants.

## Shipped always-ask list (`config.V2_ALWAYS_ASK_DEFAULTS`)
| id | matches | why |
|---|---|---|
| vercel-prod | `vercel … --prod/--production` | live for everyone the moment it lands |
| gh-release | `gh release …` except `list`/`view` | public, and others build on the tag |
| git-push-protected | a push whose refspec names `main`/`master`/`prod*`/`release*`, any spelling (`origin HEAD:main`, `+master`, `refs/heads/release-2`) | reaches production or everyone's checkout |
| git-push-force | `-f`/`--force*` on any push | destroys commits already on the remote |
| db-migrate-prod | `alembic upgrade`, `rails`/`rake db:migrate`, `flyway migrate`, `dbmate up` **plus** a prod/production/live marker in the segment | a migration is not undone by a revert |
| db-migrate-deploy | `prisma migrate deploy`, `supabase db push`, unconditionally | prod-shaped by design, no target to inspect |
| payments-stripe | stem `stripe` | moves real money |
| payments-purchase | `--pay*`, `--payment*`, `*purchase*` | spends the owner's money, rarely refundable |
| payments-domains | `vercel domains buy` | a billed, year-long commitment |
| credentials-login | `gh auth`, `vercel login`, `aws configure`, `ssh-keygen`/`ssh-copy-id`/`ssh-add` | changes what every later run can reach |
| credentials-path | any path under `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.config/jarvis`, `~/.gnupg` | a credential change however spelled |
| outward-send | tools `gmail_send`, `discord_send` | words in the owner's name, unrecallable |

The file and `Project.always_ask`/`Brief.always_ask` are **additions only**: an
empty or corrupt `always-ask.json` cannot disarm the shipped list, because a
layer a file can empty is not a layer. ALLOW here is the owner's yes; under AUTO
the provider still runs its classifier (WP3).

## Tests
Prefix `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python`;
all exit 0. `v2/permissions_check` → `all v2 permissions checks passed (349
checks)` · `rules_check` → `all rules checks passed` · `permissions_check` →
`all permission checks passed` · `v2/daemon_check` → `Ran 15 tests in 1.494s` /
`OK` · `v2/claude_provider_check` → `all claude-provider checks passed` ·
`v2/codex_provider_check` → `Ran 23 tests in 4.931s` / `OK` ·
`v2/fastpath_check` → `all fast-path checks passed`. Verified to bite: four
mutations each fail their own checks — layer 4 ignoring `human_backed`, the
hatch skipping its layer-1 re-check, always-ask never consulted, a DENY put to
the owner instead of refused.

## The hole found while testing (fixed here; no v1 file edited)
v1 `entry_for` allowlists by **stem** only for `run_command`/`run_readonly` and
by **whole tool** otherwise, and v1 has never heard of `Bash`/`shell` — so one
ALWAYS on `Bash: pnpm build` would have minted `{"tool": "Bash"}`, a
prefix-less blanket grant over every future command: the exact wildcard the
2026-08-17 round removed. `approvals.v1_request()` maps the name back to
`run_command` (joining a Codex argv array) before `allows`/`entry_for` see it,
so the entry is one stem with every segment still checked, and a hand-written
`{"tool": "Bash"}` authorises nothing. Pinned in `human_backed_checks`.

## R8 — still open, not solved here
Codex has no universal pre-tool callback under `auto_review`, so layer 2 cannot
be enforced from the requests its reviewer escalates; `codex_config` still
refuses a non-empty `Brief.always_ask`. The seam is
`permissions.always_ask_for(brief, project)` — one list, so whatever answers R8
(a 0.153.4 pre-execution hook, or a `jarvis-mcp` tool that asks with the native
equivalent removed) consumes it rather than growing a second copy. Until then a
project with always-ask additions routes to Claude.

## Known gaps and proposals (nothing fixed was edited)
- **A bare push with no refspec is deliberately not matched**: the branch is not
  in the command, and in a task worktree it is `jarvis/<slug>`, never main;
  asking about every push teaches approve-without-reading. Revisit if WP6 can
  hand the permit layer the worktree's current branch.
- `_command_writes_protected_state` is a narrow backstop, not a boundary — a
  shell has more spellings than any matcher has patterns. The file-tool refusal
  is the real protection. The brief's `rules.verdict` is spelled `rules.decide`.
- Propose `Brief` carry the project's `always_ask`, so a provider re-entering
  `permit` after a restart cannot lose the additions; and a cancellable
  `PermissionCallback` context (WP3 and WP4 both asked) — `shutdown()` releases
  waiters, but a `permit` inside a provider's own race finishes on its own.
