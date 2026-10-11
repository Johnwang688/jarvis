# Decisions: Claude and Codex chats read the HUD terminal (2026-10-10)

These are the owner's answers to the open decisions in `2026-10-10-terminal-read-mcp-plan.md` §10. **Where this file and
the plan disagree, this file wins.** Decision W-2 in `2026-10-09-hud-workspace-decisions.md` still governs what
`terminal_read` itself may do.

## Answered by the owner

| # | Decision | Answer |
|---|---|---|
| D-3 | Do Claude and Codex chats get today's other jarvis-mcp tools (memory, sessions, Gmail, Discord DM, Spotify, CAD, schedules)? | **Yes, all of them.** This overrides the plan's "terminal only". |
| D-6 | Codex sub-agents | **Claude first, then Codex.** If live check L4 shows a Codex sub-agent's calls can't be told apart, turn off Codex's sub-agents in *chat* sessions only, so `terminal_read` stays desk-only. |
| — | Merging | **Merge on review.** WP-1, WP-2 and WP-3 each merge once their independent review passes, its findings are fixed and the lead's clean-worktree sweep is green. |

## D-3 in detail: what "all of them" means

The owner chose this knowing two things:
- seven of these tools are not marked `dangerous` and so run with no approval;
- what Gmail returns goes to Anthropic or OpenAI as part of the chat.

How the build must apply it:
- **Owner chats only.** The tools go to an owner's chat thread on Claude or Codex, through the daemon's per-session list (`session_tools`). Task workers, reviewers, orchestrators and runner briefs never get them, exactly as with `terminal_read`.
- **They run in the daemon,** through the bridge's single `tools.dispatch`, with `runtime.caller()` bound. They do not run in a local jarvis-mcp process.
- **Dangerous tools ask the owner.** `gmail_send` is currently the only one. It goes through the session's own permit and the approval broker, so the owner gets a card or DM like any other approval; it is never auto-approved. jarvis-mcp's old deny-all `_approver` stays for the owner's external `jarvis mcp` use.
- **Only `terminal_read` is desk-only.** The other tools are offered on every turn of an owner chat, Discord and DM included, as the fast path's own tools are. `terminal_read` keeps every W-2 rule: desk turns only, the guard, the switch, the note.
- **Claude sub-agents** may use the non-terminal tools, as the CLI's own sub-agents use its other tools. Only `terminal_read` is refused for a sub-agent, via `agent_id`.
- **The private summary applies to `terminal_read` only.** The other tools keep today's 200-character summaries.
- **Where the text goes** (plan §5) is extended in the docs: Gmail, memory and session text read by a Claude or Codex chat goes to Anthropic or OpenAI.

## Defaulted (the plan's recommendation stands; the owner did not object)

| # | Decision | Default |
|---|---|---|
| D-1 | Transport | Per-session tokens plus the jarvis-mcp relay to daemon routes. In-process tools are the fallback for Codex if live check L3 fails. |
| D-2 | Token carrier | A private 0600 file for Claude (`--mcp-config <path>`) and the private `config.toml` for Codex. Never on a command line. The variable is named `JARVIS_MCP_TOKEN`. |
| D-4 | Listing | `terminal_read` is always listed in an owner chat, and refused cleanly off the desk. |
| D-5 | Claude gate | An explicit allow for read-only Jarvis tools under both profiles, via the new `jarvis-tool` permit layer. Other tools go through the permit as usual. |
| D-7 | Model switch | No token rotation. |
| D-8 | Steer carry | Yes. The turn after a non-desk steer also starts off the desk. |

## Order of work

1. PR #33 (`terminal_read` on the fast path), with its review fixes. These include the private summary.
2. **WP-1:** Phase 0, the per-session tokens, the relay and the bridge routes. No behaviour change.
3. **WP-2:** Claude chats get `terminal_read` plus the D-3 tools.
4. **WP-3:** Codex chats, then live checks L3 and L4 per D-6.

The fix that makes `dispatch()` enforce each agent's own toolset (its own PR, in progress) lands before WP-2.
