# Plan: Claude and Codex chats read the HUD terminal (`terminal_read` over jarvis-mcp)

**Status:** draft, 2026-10-10, written by a read-only planner.

**Builds on:**
- PR #33 (`feat/terminal-read`, head bd95320).
- Decision W-2 in `docs/plans/2026-10-09-hud-workspace-decisions.md`, which wins over this plan.
- The decisions file beside this plan (`2026-10-10-terminal-read-mcp-decisions.md`), which wins over this one.

It builds the peers plan's Phase 0 (`docs/plans/2026-10-07-peers-plan.md` §3 and §9) once, to serve this work, the peers plan and Discord B2b.

## For the owner

You asked for Claude and Codex chats to see terminal output too. After this work:

- **What a chat can read.** A Claude or Codex chat can call `terminal_read` on a turn you typed in the HUD window. It gets exactly what the fast path gets: the same plain text, the same fence, the same refusals, and the same "Jarvis read 200 lines · 15:42" note on the terminal.
- **Other turns of that chat are refused.** A message from Discord or the DM, a schedule, or the escape hatch gets the fast path's own refusal sentence. A Discord message steered into a HUD turn takes that turn off the desk, exactly as on the fast path.
- **Who never gets it.** Task workers, reviewers, orchestrators and sub-agents.
- **No approval involved.** Nothing asks you to approve a read, and no read counts as your approval of anything.
- **Where the text goes.** It is sent to Anthropic (Claude) or OpenAI (Codex), and stays in that CLI's own transcript on disk.

The plumbing is the peers plan's Phase 0. Each Claude or Codex chat session gets its own secret token, so when it calls Jarvis through jarvis-mcp, the daemon knows which chat and which turn is asking. Phase 0 lands first as its own PR, and the tool follows.

## 0. What the code says today

These facts come from bd95320, main at 34b6ab2, and the installed CLIs (Claude Code 2.1.295, codex-cli 0.161.0, claude-agent-sdk 0.2.153).

1. **No CLI session has jarvis-mcp.**
   - `Daemon._within_project` refuses `mcp_servers` from API callers (daemon.py:371), and the runner sets none.
   - Claude runs with `strict_mcp_config: True` (claude.py:674).
   - Codex runs `--strict-config` in a private CODEX_HOME, and writes only `brief.mcp_servers` into its `config.toml` (codex_config.py).
   - `grep JARVIS_PEER_TOKEN` finds nothing.
2. **The run copy exists already.** `_run_brief` (daemon.py:465) makes it. `_open` hands it to the provider and builds the permit over it, but writes the *saved* brief to `brief.json` (daemon.py:433). A token placed only in the run copy never reaches `brief.json`.
3. **The SDK puts `mcp_servers` on the command line.**
   - With a dict, `claude_agent_sdk/_internal/transport/subprocess_cli.py:660-694` adds `--mcp-config '<json>'` as one command-line argument, `env` included.
   - Command lines are world-readable (`/proc/<pid>/cmdline`, `ps aux`).
   - So the peers plan's `mcp_servers["jarvis"].env.JARVIS_PEER_TOKEN` would show every chat's token to anyone running `ps`. That includes a task worker that runs `ps aux` while debugging and then carries the token into its transcript.
   - The SDK also accepts a file path (`--mcp-config <path>`; `claude --help` says "JSON files or strings").
4. **jarvis-mcp runs the registry in its own process.**
   - `MCP_TOOLS` (mcp.py:26) lists 23 tools.
   - `_approver` refuses only tools marked `dangerous`, and only `gmail_send` is marked so.
   - These seven are not marked dangerous and would run with no approval: `memory_write`, `memory_delete`, `discord_dm_owner`, `spotify_play`, `spotify_pause`, `schedule_create`, `schedule_delete`.
5. **Claude's gate.**
   - The `PreToolUse` hook (claude.py:728) fires for every tool. `_decide` logs `gate_requested`/`gate_resolved`, which are never published (`daemon.GATE_KINDS`), then calls `permit`.
   - `build_permit` (permissions.py:559) has no idea of a Jarvis tool, so an `mcp__jarvis__…` call falls through to layer 5.
   - Under `auto`, layer 5 returns ALLOW and the hook returns `{}`, so Claude's auto-mode classifier still decides.
   - Under `ask`, layer 5 raises **an owner card**.
   - Claude chats may be on either profile (`thread_model.PROFILES`).
6. **Claude sub-agents can be recognised.** The hook input carries `agent_id` when a Claude sub-agent makes the call (SDK `types.py:308-330`). Sub-agents inherit MCP tools by default.
7. **Codex 0.161 gates MCP calls itself.**
   - The binary carries all of these:
     - `mcp_servers.<name>.default_tools_approval_mode`;
     - a per-tool `tools.<tool>.approval_mode`, whose values include `prompt` and `approve`;
     - `enabled_tools` and `disabled_tools`;
     - `readOnlyHint` in tool annotations;
     - the on-by-default feature `tool_call_mcp_elicitation`;
     - the string "MCP tool approval via request_user_input".
   - Our provider refuses every `mcpServer/elicitation/request`: it falls through to "Unsupported server request" (codex.py:965).
   - It turns every `item/tool/requestUserInput` into a QUESTION for you (codex.py:942).
   - So if Codex asked to approve an MCP call, the call would either fail or put a question in front of you.
8. **Codex MCP calls produce no tool events today.** `_notification` ignores `mcpToolCall` items (codex.py:801-827).
9. **Tool summaries carry terminal output into the log and onto the bus.**
   - `ClaudeProvider._tool_result` (claude.py:1248) and the fast path's `_on_tool_end` (fastpath.py:783) put the first 200 characters of every result into `TOOL_FINISHED.summary`.
   - `Daemon._record` writes that to `threads/<id>/log.jsonl` and publishes it on the bus.
   - A terminal read's fence header is about 163 characters (measured with `untrusted.fence`), so about 36 characters of terminal output reach the log and the bus.
   - This already affects #33's fast path.
10. **The desk rule lives only in the fast path:** `fastpath._at_desk(brief, message)` plus `FastPathProvider.steer()`. The daemon keeps no desk state per turn.
11. **Listeners.**
    - The API listener is on `config.DAEMON_PORT` (8405 live), the HUD on 8402, and preview on 8403.
    - The handler's `log_message` does nothing, and errors log the path only (daemon.py:1439, 1490).
    - `GET /events` (the bus) is open to any local client on the API listener.

## 1. Phase 0: per-session caller tokens (designed once)

### 1.1 What a token is

- **Format.** `jmcp_` followed by `secrets.token_urlsafe(32)` (256 bits). The prefix lets `credential_patterns` recognise one, which is the safe direction: a terminal that shows a token refuses the read. It also lets the Claude provider's `_safe` redact a token that turns up in stderr.
- **One per daemon session** (`_Session`). That means an owner's chat thread on Claude or Codex, from `_open` until the session ends.
  - The fast path never gets one; it runs in-process and uses `runtime.caller()`.
  - A task's thread never gets one.
- **Held only in daemon memory**, keyed by `sha256(token)`, in a new module `jarvis/v2/callers.py`:
  - `CallerTokens.mint(session) -> str`, `.resolve(token) -> Caller | None`, `.revoke(session)`, `.revoke_all()`.
  - `Caller = {thread_id, session, provider, minted_at}`.
  - `authenticate(handler, daemon) -> (Caller, _Session)` is the single check every token route uses.

### 1.2 Minting

`Daemon._run_brief` mints a token only when all three hold:
- the thread is an owner's chat (`thread_model.is_chat(thread)`);
- `thread.provider` is Claude or Codex;
- `session_tools.for_provider(provider)` is not empty.

When they hold, `_run_brief`:
- mints the token;
- stores its digest on `session.caller_key`;
- returns the run copy with `mcp_servers={"jarvis": bridge.server_entry(provider, daemon.port, token)}`.

`_open` refuses (`BriefRefused`) any non-chat brief whose `mcp_servers` names `jarvis`, so a future runner change that copied a chat brief fails closed.

`server_entry` builds:
- **`command`:** the venv's absolute python, as `print_config` builds it.
- **`args`:** `["-m", "jarvis.v2.mcp_session"]`. This is a stdlib-only module, so it skips `jarvis/__main__.py`'s heavy imports and the whole registry.
- **`env`:** `{JARVIS_MCP_TOKEN, JARVIS_DAEMON_PORT=<daemon.port>, PYTHONPATH=<repo root>}`.
- **Codex only:** a per-tool `approval_mode = "approve"` for read-only session tools (§4.2).

`JARVIS_MCP_TOKEN` replaces the peers plan's `JARVIS_PEER_TOKEN`, because the token is not peer-specific.

### 1.3 Carrying it: never on the command line

- **Claude.**
  - `ClaudeProvider._options` writes `{"mcpServers": …}` to `V2_DATA_DIR/claude/<thread_id>/mcp.json` and passes `mcp_servers=str(path)`.
  - The directories are 0700 and never symlinks. The file is 0600, written with mkstemp and replace (the `codex_config.atomically_write` pattern).
  - It is written once per `_open`, reused by `set_model`'s reconnect, and deleted by `close()`.
- **Codex.**
  - The token goes in the `env` table of `[mcp_servers."jarvis"]` in the private `config.toml`. `_prepare_home` already writes that file 0600 inside a 0700 directory.
  - `close()` rewrites the file without `mcp_servers`.
- **Neither CLI's own environment holds the token**, so the model's shell commands never inherit it. Codex's `shell_environment_policy inherit = "all"` passes down the app-server's environment, which has no token.
- **The relay** removes `JARVIS_MCP_TOKEN` from `os.environ` at start. It never writes the token, or any tool result, to stderr; Claude Code and Codex keep MCP servers' stderr in their own logs.
- **Start-up sweep.** `Daemon.start` deletes leftover `claude/*/mcp.json` files and strips `mcp_servers` from leftover Codex homes. These are left over from a crash, and their tokens are already dead.

### 1.4 Revoking

| Event | What happens |
|---|---|
| The session closes (`close_thread`, archive, delete) | `_cleanup` calls `callers.revoke(session)`; the provider deletes its file |
| A fatal turn, or a provider that closed itself, drops the session (`session.lost`) | Same, through `_cleanup` |
| `_open` fails | Revoked in `_open`'s `except` branch |
| The daemon stops | `stop()` calls `revoke_all()` before anything else closes |
| The daemon restarts | Tokens lived only in memory, so all are gone; each chat gets a new one when it resumes |
| A model switch | **No rotation** (D-7). The token names the daemon session, which a switch keeps. On Claude the old CLI and its jarvis-mcp exit, and the new CLI reads the same file. On Codex nothing restarts. A switch that ends in `SessionLost` drops the session, which revokes the token |
| A provider switch | Not possible on a session ("a session cannot change provider", hud_api.py:1129). Another provider means another thread, with its own token |

The route also requires `daemon._sessions.get(caller.thread_id) is caller.session`, so even a missed revoke can never reach a newer session.

### 1.5 How jarvis-mcp presents the token

- **Listener.** The API listener (`daemon.port`). `/mcp/*` answers 404 on the HUD listener (whose Origin means "the owner") and on the preview listener.
- **Routes**, in a new `jarvis/v2/bridge.py`, mounted in `daemon._handler._route` before `hud_api.route`:
  - `GET /mcp/tools` returns `{"tools": [{name, description, inputSchema, annotations}]}`, built in the daemon from `tools.REGISTRY[name].spec()`, so the schemas match the fast path's by construction.
  - `POST /mcp/call` takes `{"name", "arguments"}` and returns `{"text", "isError"}`.
- **Header.** `Authorization: Bearer jmcp_…`. The token never goes in the URL, because paths are logged.
- **Refusals:**
  - **403**, with one fixed sentence ("not a Jarvis chat session"), for:
    - no header, or a malformed one;
    - an unknown or revoked token;
    - a session that is not an owner's Claude or Codex chat;
    - any request carrying an `Origin` header.
  - **404** for a tool this session is not offered.
  - **400** for arguments that are not an object.
  - **413** for a body over 16 KiB.
- **The relay** (`jarvis/v2/mcp_session.py`, stdlib only):
  - `initialize` answers without `listChanged`.
  - `tools/list` calls `GET /mcp/tools`, and returns an empty list on any failure.
  - `tools/call` calls `POST /mcp/call`. On failure it returns an `Error:` result: "this jarvis-mcp is not connected to a Jarvis chat (the session ended or Jarvis restarted)".
  - It never imports `jarvis.tools` and never runs a tool locally.
- **`jarvis mcp` is unchanged.** The owner's own external use, `MCP_TOOLS` and `print_config` stay as they are.

### 1.6 Mapping a token to (thread, session, current turn)

- `authenticate` resolves the `Caller`, then the `_Session` (with the identity check above), and from it the thread, brief and provider.
- **The current turn** is `session.turn_id` while `session.worker is not None`.
- **Its desk state** is a new per-turn slot, `session.desk = {"present": bool}` (§3.2).
- **Locking.** The bridge reads the turn and the slot under `daemon._lock`, then runs the tool outside the lock.
- **`runtime.caller()`** is a new context variable, from the peers plan's Phase 0.
  - The bridge binds `{thread_id, project_id, provider, turn_id}` for each call, and the fast path binds the same in `_run`.
  - Peers and B2b read the caller from there, never from a tool argument.

### 1.7 Where the token never goes

The token never appears in any of these, and tests cover each one (§7.1):
- `brief.json`, `log.jsonl` or `decisions.jsonl`;
- the bus or Discord;
- any route response, or the daemon log;
- any command line;
- the CLIs' own environments.

### 1.8 The /proc caveat, stated plainly

**Who can read a live token.** Any program running as you can read one, from any of:
- the Claude session's `mcp.json`;
- the Codex session's `config.toml`;
- `/proc/<jarvis-mcp pid>/environ`.

With it, that program can call the bridge as that chat. This is an accepted limit. It is design §1.2's non-goal (a confused agent, not an adversary), the peers plan's stated caveat, and the same limit as WP-C's Origin check and #33's desk flag. Claude task workers run unsandboxed on this WSL2 (WP3), so a worker that went looking could find a token.

**What the token still guarantees.**
- A worker, reviewer or sub-agent that merely calls a Jarvis tool has no jarvis server and no token.
- A stale token is refused.
- A stolen token reads a terminal only while that chat's current turn is a desk turn, and every read leaves the note on the terminal.

Keeping the token off the command line (fact 3) means it does not show up in `ps` and does not drift into a transcript by accident.

### 1.9 What the peers plan and B2b reuse

- **Their own routes** (B2b's `POST /project-proposals`, the peers plan's `/peer/*`) can use `callers.authenticate`.
- **Preferred, for parity with the fast path:** add the tool to `session_tools.SESSION_TOOLS`, and let `/mcp/call` run the registry tool in the daemon with `runtime.caller()` bound.
- **B2b's model rule** should read the model the session actually runs (`session.applied[0]`).
- **Update the peers plan's Phase 0 text** in three places: the variable name, the file carrier and the bridge routes.

## 2. Wiring jarvis-mcp into chat sessions only

- **Which briefs:** only the run copy of an owner's chat thread (chat role, no task) on Claude or Codex. Never:
  - runner briefs (orchestrator, implementer, reviewer, researcher);
  - the fast path, whose `_toolset` already refuses `mcp_servers`;
  - briefs made over the API, which `_within_project` refuses when they carry `mcp_servers`.
- **What becomes reachable:** only the daemon's per-session list, `session_tools.SESSION_TOOLS`, filtered by provider.
  - WP-1 ships the list empty in production, so behaviour does not change.
  - WP-2 adds `terminal_read`.
  - The relay never offers `MCP_TOOLS`.
- **Today's `MCP_TOOLS` for chats** is a separate decision (D-3). The recommendation is not now, because:
  - seven of them would run with no approval (fact 4);
  - `gmail_*` would send your mail to Anthropic or OpenAI;
  - `memory_write` is the peers plan's "obvious place for a persistent injection".

## 3. `terminal_read` over MCP

### 3.1 The path: one implementation

- **The call chain:** model → CLI → relay → `POST /mcp/call` (with the token) → bridge → `tools.dispatch("terminal_read", args, approve=deny-all)` → #33's tool → `terminals.read_for_tool` → `terminal_guard.judge`.
- **The context.** The bridge calls `tools.dispatch` in a fresh `contextvars.Context()` with `runtime.bind(desk=session.desk, depth=0, caller=…)`.
- **Everything else is #33's, unchanged:**
  - the guard, the renderer and the credential list;
  - the refusal sentences;
  - the fence and the scrub;
  - the `readable` switch;
  - the bus event `{terminal_id, lines, at, refused}`, and so the HUD note.
- **No second copy.** The bridge and the relay import none of `terminals`, `terminal_guard`, `terminal_text` or `credential_patterns`, and a grep test checks this.
- **Results.** The relay returns the tool's text unchanged. `isError` is true for `Error:` and `Refused:` results, as `mcp.call_tool` does now.

### 3.2 The desk rule: the same as the fast path

- **One definition.** Move `fastpath._at_desk` to a new `jarvis/v2/desk.py` as `desk_message(brief, message)`. It is true only for a chat role with no task, `message.desk is True`, origin owner, via HUD. The fast path and the daemon both call it.
- **Each turn starts fresh.** `Daemon._begin` sets a new dict per turn: `session.desk = {"present": desk_message(session.brief, message) and not session.desk_carry}`.
- **Steers.** In `Daemon._steer`, under the lock and before the provider has the message:
  - a message that is not a desk message sets `session.desk["present"] = False` and `session.desk_steered = True`;
  - a steer never puts a turn *on* the desk.
- **Queued messages.** A message that waits in the queue does not touch the running turn; it later runs as its own turn with its own desk state.
- **Turn end.** In `_turn`'s `finally`, in this order:
  1. `session.desk = {"present": False}`;
  2. `session.desk_carry = session.desk_steered`;
  3. reset `desk_steered`.
- **The carry (D-8).** On Claude and Codex a steer can land in the next turn: Claude folds a late steer's CLI turn into the next send, and Codex may deliver a steer that got no answer late. So the turn after a non-desk steer also starts off the desk.
- **No turn running** (a CLI turn nobody sent, or a call between turns) means `{"present": False}`.
- **Every desk refusal** comes from the tool itself, with #33's `NOT_AT_DESK` sentence.

### 3.3 Listed always, refused cleanly (D-4)

`terminal_read` is listed in every owner chat session on Claude, and on Codex from WP-3, and is refused off the desk. Listing it only when usable would be worse:
- MCP tool lists are read when the CLI connects.
- Changing the list per turn needs `notifications/tools/list_changed`. That breaks the prompt cache each time, can arrive after the model's next request, and is unverified on Codex.
- The fast path can drop the tool per turn only because it builds its own request (`_turn_tools`).

Listing grants nothing: the daemon decides on each call, and the tool's description already says it works only in a chat you are typing in at the HUD.

### 3.4 Sub-agents

- **Claude:** the hook refuses `mcp__jarvis__terminal_read` when the hook input has `agent_id`, without asking `permit`. The tool's `session_tools` flag is `foreground_only`.
- **Codex:** not distinguishable yet (D-6).

### 3.5 Nothing of the read in the log or on the bus

- **Private summary.** For `terminal_read` (fast path) and `mcp__jarvis__terminal_read` (Claude), `TOOL_FINISHED.summary` never carries output. It comes from `tools.private_summary(name, text)`: "read N line(s)", the refusal's first line, or "error".
- **The set.** A new `tools.PRIVATE_RESULT` set, shaped like `EXPLICIT_ONLY`, is filled by the tool module.
- **The fast-path half** of this belongs in #33.
- **Codex (WP-3):** render `mcpToolCall` items as tool started/finished events with the same private summary, so the HUD shows the call.

## 4. Passing each provider's gate without a card

### 4.1 Claude

- **Permit layer.** `permissions.build_permit` gains a layer after layer 2, so an always-ask rule that names the tool still asks.
  - A read-only Jarvis session tool (`{"mcp__jarvis__terminal_read"}`) gets `Decision.ALLOW` at layer `jarvis-tool`, with the reason "read-only Jarvis tool; the daemon checks the turn".
  - It is not logged as a human decision, and the approval broker is never asked.
- **Hook.** `ClaudeProvider._pre_tool_hook` returns an explicit allow for those names under both profiles, with the reason "a read-only Jarvis tool" (never "approved").
  - Under `ask`, this pre-empts the CLI's own permission prompt.
  - Under `auto`, it skips the classifier, whose job here the daemon does.
  - Every other tool keeps today's behaviour.
- **No card, no DM.** The gate pair is still logged as `gate_requested`/`gate_resolved` and never published.

### 4.2 Codex

- **Config.** The `[mcp_servers."jarvis"]` entry sets `tools = { "terminal_read" = { "approval_mode" = "approve" } }`.
- **Annotations.** The relay's tool list marks the tool `readOnlyHint: true, destructiveHint: false, openWorldHint: false`.
- **Declining.** If Codex still sends an MCP approval request — an elicitation, or a `requestUserInput` marked as an MCP tool approval (`mcp_tool_call_approval` / `codex_approval_kind`) — `CodexProvider._server_request` declines it. It logs a non-fatal ERROR, "Codex asked to approve a read-only Jarvis tool; declined". It is never a QUESTION and never an accept.
- **Live check L3** confirms that `--strict-config` accepts these keys and that no approval request arrives.

## 5. Where the text goes (wording for the docs)

Add this beside #33's text in CLAUDE.md, design §18 and hud-api.md, covering all three providers:

> What `terminal_read` returns becomes part of the conversation. On the fast path it is sent to OpenRouter and the model provider behind it, and kept in the fast path's session transcript. In a Claude chat it is sent to Anthropic through your Claude login and kept in Claude Code's own session transcript under `~/.claude/projects/`. In a Codex chat it is sent to OpenAI through your ChatGPT login and kept in Codex's session files in that thread's private home (`V2_DATA_DIR/codex/<thread>/`). Whatever the model repeats in its reply goes where replies go: the HUD, the chat's Discord thread (scrubbed) and speech. The guard decides what is read; nothing decides what happens to it after.

## 6. Built-in ways to read an external terminal

Sources checked: `claude --help`, `codex --help` and `codex features list` (each run directly, by path), plus string searches in both binaries.

**Claude Code 2.1.295: none.**
- Bash runs its own commands, and BashOutput reads background shells Claude itself started.
- Its built-in guidance for terminal programs drives tmux sessions it starts itself (`tmux capture-pane`).
- `claude logs <id>` is a command for you, not the model, and prints only a background Claude session's output (`claude --bg`).
- `--tools` covers only the built-in set.
- The binary contains no "getTerminalOutput", "readTerminal" or "terminal_output" strings.

**Codex 0.161: none.**
- `unified_exec` and `unified_exec_tty` give it terminals for its own commands, and `write_stdin` writes only to those.
- It ships `browser_use`, `in_app_browser` and `computer_use`, all stable and on by default. These are not terminal readers. A browser pointed at the HUD page could in principle attach to a terminal, as any program running as you can (WP-C's stated limit), and that would raise `terminal_attached`. Whether those features are live in our strict, private Codex setup is unverified.

**The HUD terminals are out of reach of shell commands.** They are not tmux (W-3), and their output buffer lives in the daemon's memory. Opening the terminal device from a shell would steal the shell's input, not read its output.

**Confidence:** high that neither CLI has a built-in way to read another terminal; medium on the browser and computer-use point.

## 7. Tests

**Ground rules:** all free; temp HOME and config; the daemon on port 0; never 8402/8403/8405.

**New suites:** `tests/v2/mcp_session_check.py` (Phase 0) and `tests/v2/terminal_read_mcp_check.py` (WP-2/3).

**Reused harnesses:**
- #33's `ReadBase` (the fake shell);
- `steer_check.Steerable`;
- `claude_provider_check.FakeClient`/`Hook`;
- `codex_provider_check.FakeRpc`/`Brain`;
- `mcp_check.PipeClient`.

**Fake CLI providers**, registered as `claude` and `codex`, capture the run copy of the brief. From inside `send()` they call the real relay: a `python -m jarvis.v2.mcp_session` subprocess with the captured environment.

7.1 **Token refusals (WP-1).**
- **`CallerTokens`:** mint then resolve; revoke gives None; `revoke_all` clears everything; the registry holds no raw token.
- **403 with the fixed sentence, and the tool never called,** for:
  - no header, `Basic`, an empty bearer, two tokens, a random `jmcp_` token, a revoked token;
  - any call after `close_thread`;
  - any call after a fatal turn drops the session;
  - any call after `stop()` plus a new daemon on the same data folder;
  - the old token after a resume (the new one works).
- **Isolation:** chat A's token never reaches chat B's turn.
- **Listeners:** `/mcp/*` gives 404 on `face_port` and `workshop_port`, and 403 when an `Origin` header is present.
- **Leak scan.** The token never appears in:
  - `brief.json`, `log.jsonl` or `decisions.jsonl`;
  - captured logs at DEBUG;
  - any drained bus event;
  - `GET /threads`, `GET /threads/{id}` or `GET /status`;
  - the Claude CLI's command line (built with the SDK's transport command builder; `--mcp-config` must be a path);
  - the Codex app-server's command line or `clean_env`;
  - the relay's stderr.
- **Carriers:**
  - the Claude file is 0600 in a 0700 directory, gone after `close()`, and removed by the start-up sweep;
  - Codex's `config.toml` is stripped on close.

7.2 **Task workers never get it.**
- `_run_brief` adds no `jarvis` server and mints nothing for:
  - runner-made orchestrator, implementer, reviewer and researcher briefs, on both Claude and Codex;
  - a chat-role brief that has a task id;
  - fast-path chats.
- `_open` refuses a non-chat brief that names a `jarvis` server.
- A token forced onto a task session is refused by `authenticate`.
- A task thread's Codex config text has no `mcp_servers.jarvis`.

7.3 **A desk Claude or Codex turn reads.**
- A HUD send through `face_port` with the HUD Origin returns fenced text holding a marker, plus exactly one `terminal_read` event with `refused: false`.
- Each refusal family once, through the relay:
  - `printenv` gives exactly "Refused: possible credential in this output";
  - the switch turned off gives the refusal by name.
- `tools/list` through the relay matches the registry's spec for `terminal_read`, plus the read-only annotations.

7.4 **A Discord-driven turn on the same thread is refused.** Each of these gets exactly `NOT_AT_DESK` and publishes no `terminal_read` event:
- `via="discord"` or `via="dm"`;
- a send on the API listener with no Origin;
- `origin="owner-ran"` (the escape hatch);
- a schedule.

7.5 **A steer takes the turn off the desk.**
- A desk turn is held open and a Discord message steers into it. The turn's next read gets `NOT_AT_DESK`.
- The next HUD turn is also refused (the carry), and the one after that reads normally.
- A Discord message that waits in the queue (the provider refused the steer) leaves the running desk turn on the desk.
- A HUD steer into a Discord turn never puts that turn on the desk.
- A call with no turn running is refused.

7.6 **Sub-agents (Claude).**
- A hook input with `agent_id` is denied, and `permit` is never asked.
- Without `agent_id`, the hook returns an explicit allow under both auto and ask.

7.7 **The gate.**
- `build_permit` returns ALLOW at layer `jarvis-tool` under auto and ask, with the broker never asked, and the `decisions.jsonl` row shows that layer.
- An always-ask addition that names the tool still asks.
- No `approval_requested` event reaches the bus during a read.
- **Codex:**
  - the config text holds the per-tool approve;
  - an elicitation is declined, with no QUESTION and a non-fatal ERROR;
  - an MCP-approval `requestUserInput` is declined the same way.

7.8 **One guard implementation (grep).**
- Each of these appears exactly once in `jarvis/`:
  - `def judge(`;
  - `REFUSAL =`;
  - the literal "possible credential in this output" (in `terminal_guard.py`);
  - `def read_for_tool(`;
  - `NOT_AT_DESK =`;
  - `def desk_message(`.
- The new modules (`bridge.py`, `callers.py`, `mcp_session.py`, `session_tools.py`) import none of the terminal or guard modules and hold none of their sentences.
- `bridge.py` calls `tools.dispatch(` exactly once.
- `mcp_session.py` contains no `dispatch(` and imports nothing from `jarvis.tools` or `jarvis.v2.tools` (an AST check).

7.9 **`terminal_check`'s no-tool test allows exactly the new path.**
- `test_no_tool_is_named_for_a_terminal`:
  - the reachable tools now include `SESSION_TOOLS`;
  - the names containing "terminal" are still exactly `["terminal_read"]`;
  - it is still not in `mcp.MCP_TOOLS`;
  - it is in `SESSION_TOOLS`, marked read-only and foreground-only.
- `test_nothing_but_the_daemon_and_its_routes_reaches_the_module`: `ALLOWED` is unchanged, and the new modules get no exemption.
- New, `test_the_session_path_reaches_terminals_only_through_the_tool`: the only path is relay → `/mcp/call` → bridge → `tools.dispatch` → the tool's single `read_for_tool(` call.
- The bus-records test: a read through the bridge publishes exactly `{terminal_id, lines, at, refused}`.

7.10 **Nothing of the read in the log or on the bus.** A marker read in a fast-path turn, and one read in a Claude turn, never appear in `log.jsonl` or on the bus. The summary says "read N line(s)".

**Mutations that must make a test fail**, each tried in a scratch copy:
- mint a token for a task thread;
- drop the session identity check;
- accept a token on the HUD listener;
- pass `mcp_servers` as a dict again (back on the command line);
- let the relay run the tool locally;
- have the daemon ignore a non-desk steer;
- remove the carry;
- let the hook allow sub-agents;
- remove the permit layer (an ask-profile chat then raises a card);
- have Codex accept the approval instead of declining it;
- keep `text[:200]` in the summary;
- skip the revoke on close.

## 8. Live checks (by hand, never in the suite)

- **L1, Claude.**
  - A HUD chat lists and runs the tool under auto and ask, with no prompt and no classifier refusal, and the note appears.
  - `ps -eo args | grep jmcp_` finds nothing.
  - After close, the file is gone and `grep -r jmcp_ ~/.claude` finds nothing.
- **L2, Claude.** A sub-agent's call reaches the hook with `agent_id` and is refused.
- **L3, Codex.**
  - `--strict-config` accepts the per-tool `approval_mode`.
  - A call runs with no elicitation, no `requestUserInput` and no reviewer refusal.
  - The relay starts within the startup timeout, under the private HOME and minimal PATH.
- **L4, Codex.** Does a sub-agent see Jarvis tools? Can its calls be told apart, by the thread id on `item/started` for `mcpToolCall` or by MCP `_meta`?
- **L5, both.** A Discord message to the same chat is refused, and so is a Discord steer into a HUD turn.

## 9. Work packages and order

1. **#33, plus the summary fix** (WP-0, a review item); then merge.
2. **WP-1: Phase 0, its own PR.** No behaviour change until a session tool exists.
   - **New modules:** `callers.py`, `bridge.py`, `session_tools.py` (empty in production) and `mcp_session.py`, plus `runtime.caller()`.
   - **Daemon:** minting, the refusal of non-chat briefs, revokes, the start-up sweep, and the `/mcp/*` routes.
   - **Claude:** the file carrier; the hook's allow and sub-agent refusal for flagged tools; `_safe` redacting `jmcp_`.
   - **Codex:** the per-tool approval config; stripping on close; declining MCP approvals without asking.
   - **Gate and patterns:**
     - the `jarvis-tool` permit layer;
     - the `jmcp_` shape in `credential_patterns`. That file is SELF_PROTECTED, so this edit goes through a reviewed PR.
   - **The fast path** binds `caller`.
   - **SELF_PROTECTED** gains the new modules.
   - **Tests**, and the peers plan's Phase 0 text updated.
3. **WP-2: Claude chats.**
   - `SESSION_TOOLS["terminal_read"]`, marked read-only and foreground-only, for Claude.
   - `desk.py`, with the fast path switched over to it.
   - The daemon's per-turn slot, steer rule and carry.
   - The private summary.
   - `terminal_check` updates and the new `terminal_read_mcp_check`.
   - Docs: CLAUDE.md WP-F, design §18, hud-api.md, and the §5 wording.
   - Live checks L1, L2 and L5.
4. **WP-3: Codex chats.**
   - Add Codex to the tool's providers.
   - Render `mcpToolCall` items.
   - Settle sub-agents per D-6.
   - Tests, then live checks L3, L4 and L5.
5. **Peers phase 1 and B2b** can follow WP-1, whatever happens to WP-2 and WP-3.

## 10. Open decisions (with recommendations)

The answers go in `2026-10-10-terminal-read-mcp-decisions.md`.

- **D-1 Transport.**
  - **Recommended:** tokens plus the jarvis-mcp relay. It is one mechanism for both CLIs, testable without either CLI, and what the peers plan and B2b were planned around. Its remaining weakness is the already-accepted same-user limit.
  - **Alternative:** in-process tools, through the Claude SDK's in-process MCP server and Codex's `thread/start.dynamicTools` with `item/tool/call` (present in 0.161, experimental, unverified). This needs no token and no extra process per chat, but the peers plan and B2b would need re-planning. It is the fallback for Codex if L3 fails.
  - **Also considered:** a Unix socket with peer credentials (not recommended).
- **D-2 Token carrier.**
  - **Recommended:** a private file for Claude and the private `config.toml` for Codex, never the command line, with the variable named `JARVIS_MCP_TOKEN`.
  - **Rejected:** the peers plan as written, which leaks the token through `ps`.
  - **Later hardening:** a single-use enrollment code exchanged when jarvis-mcp starts.
- **D-3 Chats and today's `MCP_TOOLS`.** Recommended: no, not now.
- **D-4 Listing.** Recommended: list the tool always, and refuse cleanly off the desk.
- **D-5 Claude gate.** Recommended: an explicit allow for read-only Jarvis tools under both profiles. Leaving `{}` under auto lets the classifier refuse a harmless read, and under ask the layer is needed either way.
- **D-6 Codex sub-agents.** Recommended: ship Claude first. If L4 shows Codex sub-agent calls cannot be told apart, set `features.multi_agent = false` for Codex chat sessions only. You may prefer to leave Codex chats without the tool; letting sub-agents read would break your confirmed rule.
- **D-7 Model switch.** Recommended: no rotation.
- **D-8 Steer carry.** Recommended: yes. It costs one extra off-desk turn.
