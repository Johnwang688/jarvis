# Role bench: owner decisions and the shared case schema (2026-10-20)

This file wins over `2026-10-20-role-bench-plan.md` where they differ.

## Owner decisions (John, 2026-10-20: "Accept the defaults for O-1 to O-6")

- **O-1 Model matrix:**
  - Claude Code: Haiku 5.5, Sonnet 5.5, Opus 5.5.
  - Codex CLI: gpt-6.1-sol, gpt-6-luna.
  - Jarvis loop via OpenRouter: Haiku 5.5 (the harness control), Kimi K3, gpt-6-luna.
  - *Superseded for the Jarvis loop by the O-1 amendment below (2026-10-21).*
- **O-2 Spend:** OpenRouter cells are capped at **$10 total and $0.50 per run**. Claude and
  Codex cells use plan quota.
- **O-3 Kimi K3** runs on **Moonshot direct** (the BYOK credit, $3 / $15). Budget each run at
  that price, not the catalog price. *Moot since the O-1 amendment: Kimi left the matrix.*
- **O-4 Chinese-hosted models are allowed for this bench only**, on repo code and sanitized
  fixtures. The owner's logs and chats never go to them.
- **O-5 Order:** the reviewer bench first. The implementer bench runs after the reviewer
  winner is known and uses it as the fixed reviewer.
- **O-6** The `rolebench/` build PR is **merge on review**: independent review passed,
  findings fixed, lead sweep green.

## Case schema (P0 writes these; P1 reads them)

Cases live in `rolebench/cases/`, one JSON file each, named `<id>.json`. Text fields are
plain strings. No owner chat text, logs or secrets go in a case. Ground truth is described
in the reviewer's terms (mechanism and location), never copied from the owner's messages.

```jsonc
// kind "review"
{
  "id": "c6-r1",
  "kind": "review",
  "pr": 275,
  "title": "C6 approval-card provenance",
  "base": "<sha the PR branched from>",
  "head": "<sha that was reviewed>",
  "brief": "What the PR claims to do, as the lead's review brief gave it (no findings in it).",
  "focus": ["display-only: decisions unchanged", "untrusted titles"],
  "suites": ["tests/v2/relay_provenance_check.py", "tests/v2/permissions_check.py"],
  "control": false,          // true = a clean PR whose review found nothing real
  "seeded": null,            // or {"description": "...", "patch": "<unified diff applied on head>"}
  "ground_truth": [
    {"id": "gt1", "severity": "MED",
     "mechanism": "provenance follows the running turn, not the turn that spawned the child",
     "location": "jarvis/v2/daemon.py:_relayed_from",
     "evidence": "a background child of a relayed turn asks after the turn ends: no line",
     "source": "review round 1 of #275"}
  ]
}

// kind "implement"
{
  "id": "impl-279",
  "kind": "implement",
  "pr": 279,
  "size": "S",
  "base": "<sha>",
  "merged": "<sha of the PR's final head>",
  "brief": "The original build brief, minus anything that reveals the solution.",
  "hidden_tests": ["tests/v2/browser_password_store_check.py"],
  "regression_suites": ["tests/v2/browser_profiles_check.py"],
  "scope": ["jarvis/v2/browser/password_store.py", "hud/src/components/BrowserProfile.tsx"],
  "fixtures": []             // sanitized inputs the brief may reference
}
```

Severity is one of HIGH, MED, LOW or NIT. NIT ground truth is recorded but not scored.

## A2 (2026-10-20 fix round): a cell gets a trimmed, in-memory login

The independent review (PR #283 delta) flagged that binding the owner's whole
`~/.claude/.credentials.json` into a cell hands the agent's shell the refresh
token and the MCP OAuth block, not only the access token the CLI needs. Lead
decision: pass a **narrower in-memory view**, not a new grant, and the owner can
veto it.

- The claude and judge cells get `{"claudeAiOauth": {accessToken, expiresAt,
  scopes, subscriptionType, rateLimitTier}}` — no `refreshToken`, no `mcpOAuth`.
- The codex cell gets `auth.json` without `refresh_token`.
- The trimmed JSON is written to a pipe and bound with `bwrap --perms 0400
  --ro-bind-data <fd> <dest>`, so it is never on disk, never writable, and never
  written back. The `login_problem` pre-flight still refuses a run whose access
  token would expire within it (the cell cannot refresh a read-only file).
- Values are never printed or logged; tests check key names only.

This is strictly less than the cell saw before, so it needs no new approval; it
is recorded here so the owner can object if even the access token in a cell is
too much (the alternative is a short-lived minted token, which the Max plan does
not offer).

## O-1 amendment (owner, 2026-10-21): the matrix, budgeting, quota ceiling

This replaces O-1's Jarvis-loop row and O-3. **Kimi K3 was dropped by the owner**, and
gpt-6-luna leaves the Jarvis loop (it stays in Codex).

| Harness | Models |
|---|---|
| Claude Code CLI | Haiku 5.5, Sonnet 5.5, Opus 5.5 |
| Codex CLI | gpt-6.1-sol, gpt-6-luna |
| Jarvis loop via OpenRouter | `anthropic/claude-haiku-5.5` (harness control), `deepseek/deepseek-v4.1-flash`, `z-ai/glm-5.3-flash` |

`rolebench.config.MATRIX` holds it, and it is enforced: a model outside it for its harness (the
reviewer's too) is refused before any quota read or spend, unless the run is started with
`--off-matrix`, which every run row records as `off_matrix: true`.

- **No Kimi special cases.** The Moonshot provider pin and the $3 / $15 price floor are gone.
  The refuse-unknown-Kimi guard stays, with no Kimi id allowed: every `moonshotai/` id is
  refused before any reservation, in the harness and again in the worker.
- **Budget estimate at the most expensive endpoint.** The worker's refuse-before-sending
  estimate prices each call at the highest prompt and highest completion price over the
  model's OpenRouter endpoints (`/api/v1/models/<id>/endpoints`, cached a day; a model with no
  price is refused, never estimated at zero). The ledger still settles on reported usage
  (BYOK upstream cost included). O-2's caps are unchanged.
- **Quota override with a hard ceiling.** The owner OK'd running Claude cells on the
  remaining weekly quota, above the 75% guard, with `--no-quota-guard` and
  `ROLEBENCH_QUOTA_OVERRIDE=<reason>`. That lifts the 75% guard only. `ROLEBENCH_QUOTA_CEILING`
  (default **97**) is a hard ceiling: a Claude or Codex model step or a judge call is refused
  once any live window is at or above it, override or not, checked before every model step
  and every judge call. Under the override an unknown quota is refused too, since the ceiling
  cannot be checked. Each run records `quota_before` and `quota_after` per window.

## A1 update (2026-10-21): pasta wired

passt (`pasta 0.0~git20260120.386b5f5-1`) is installed, so claude, jarvis and judge cells
reach their APIs through pasta, and `ROLEBENCH_NET=pasta` is the default. `none` still has
no network, so real runs under it are refused. The composition, the egress policy and the
measured reachability are in `docs/notes/testing-benchmarks.md` ("Role bench", Network).
Three choices go beyond the original `--config-net --no-map-gw` plan, each because pasta
alone left a path open or a guarantee implicit:

- **An egress policy inside the namespace** (`rolebench/netcell.py`, `ip rule`s installed as
  root of pasta's namespace before bwrap starts). Pasta alone still reached WSL's
  `10.255.255.254` (an address on the host's `lo`, so every 0.0.0.0 service) and the gateway.
  The policy prohibits private, link-local, CGNAT, multicast and reserved space and every host
  address, allows TCP 443 and DNS to pasta's forwarder, and prohibits the rest.
- **bwrap in its own user namespace with no capabilities** (`--unshare-user --uid --gid
  --cap-drop ALL`), because it starts as root of pasta's namespace.
- **Logins over stdin to netcell**, because pasta closes inherited descriptors. They stay
  in memory (A2): netcell writes them into fresh pipes for `--ro-bind-data`.
