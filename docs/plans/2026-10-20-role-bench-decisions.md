# Role bench: owner decisions and the shared case schema (2026-10-20)

This file wins over `2026-10-20-role-bench-plan.md` where they differ.

## Owner decisions (John, 2026-10-20: "Accept the defaults for O-1 to O-6")

- **O-1 Model matrix:**
  - Claude Code: Haiku 5.5, Sonnet 5.5, Opus 5.5.
  - Codex CLI: gpt-6.1-sol, gpt-6-luna.
  - Jarvis loop via OpenRouter: Haiku 5.5 (the harness control), Kimi K3, gpt-6-luna.
- **O-2 Spend:** OpenRouter cells are capped at **$10 total and $0.50 per run**. Claude and
  Codex cells use plan quota.
- **O-3 Kimi K3** runs on **Moonshot direct** (the BYOK credit, $3 / $15). Budget each run at
  that price, not the catalog price.
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
