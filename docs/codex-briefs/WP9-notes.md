# WP9 — Router and UsageLedger

## Built
- Pure Incoming classification: verbs, intake, task steering, fast path;
  spoken messages never become verbs or intake commands.
- Proposal placement, INTAKE creation, one-line replies, durable 60-second
  grace deadlines, replay protection by turn id, cancellation and admission query.
- Seven-step routing with named reasons, capability/availability drops,
  explicit overrides, role-specific steer overrides, survivor-only preferences,
  blocked journals, status.routing decisions, and 60-second health caching.
- New workers route fresh; orchestrator recovery offers an owner resume command
  plus spec, plan, status and worker reports; existing Thread.provider stays pinned.
- JSONL accounting by provider/task/Chicago day; Codex high-water token deltas,
  Claude equivalent_usd, fast-path spend_usd; cooldown/health/threshold states,
  restart recovery, lifetime task ceiling queries and optional measured windows.
- CLI route/view/set/models and GET/POST /route share the daemon's service;
  atomic global routing writes, project chains, last ten decisions with reasons.

## Shipped routing.json defaults (materialized on first global edit)
Path: ~/.config/jarvis/routing.json; JARVIS_ROUTING overrides the path.
| Role | Chain | Codex model/effort | Claude model/effort |
| --- | --- | --- | --- |
| orchestrator | claude,codex | gpt-6-astra/xhigh | roster/default |
| implementer | codex,claude | gpt-5.6-sol/high | roster/default |
| reviewer | claude,codex | gpt-6-astra/xhigh | roster/default |
| researcher | codex,claude | gpt-5.6-sol/high | roster/default |
`roster/default` resolves models.tier("orchestrator") and models.effort_for().
`no_new_work`: 0.85. `allowances`: codex.work_tokens=15000000,
claude.work_tokens=4000000, fast.spend_usd=5. R6 stays open: these are
local daily admission limits, not verified subscription quotas.
POST shapes: {action:set, role, chain, project?} or
{action:models, role, provider, model:"model/effort"}; JSON strings quoted normally.

## Validation (free; fake providers and temporary roots)
Prefix each command below with:
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python`
| Command | Last lines (exit 0) |
| --- | --- |
| tests/v2/router_check.py | Ran 21 tests; OK |
| tests/v2/ledger_check.py | Ran 8 tests; OK |
| tests/v2/daemon_check.py | Ran 15 tests; OK |
| tests/v2/fastpath_check.py | all fast-path checks passed |
HTTP suites needed sandbox escalation for loopback sockets. No live inference.
`git diff --check` passed.

## Workarounds / proposed interface changes
- Daemon changes stay within the two-route limit. WP11 must call
  Router.on_event() synchronously on the durable event path and deliver its
  proposal reply/NeedsProject result; bounded bus queues can drop usage.
  It must call ready_to_start() before CLI admission and cancel_proposal()
  for the grace-window cancel verb. No runner or automatic session startup added.
- WP7 deltas Thread.tokens, but publishes ORIGINAL Codex cumulative USAGE.
  Ledger differences those events itself (covered against actual Daemon._record).
  A future normalized publisher can use usage_mode="delta". Propose explicit
  usage semantics and durable event ids; optional event_id makes replay idempotent.
- Fixed Brief has no images: resolve accepts images= separately (or a future
  Brief.images); MCP requirements come from Brief or mcp_servers=. Both merged
  adapters accept MCP configs; optional supported_mcp_servers narrows support.
- RoutingDecision has no model/effort: model_settings() supplies them, and the
  decision journal records them. Propose adding these fields to the interface.
- Role-specific overrides and worker_report/report entries use the task journal;
  propose fixed fields/report storage for WP11. Ceilings and restarts remain WP11.
- Strict routes only to Codex per §8, but merged WP4 refuses strict/ask and
  always_ask: its missing permission integration remains an upstream limitation.
- Claude catalog checks use cached_info only, assuming vision if cache is absent.
  Preserve the requested roster model verbatim; no speculative model translation.
