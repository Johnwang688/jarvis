# Control Plane — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 235–337

<!-- claude-source:000235-000337:begin -->
**Only the broker asks the owner (2026-10-08).** A provider's
`APPROVAL_REQUESTED`/`APPROVAL_RESOLVED` mean "the gate was consulted" — the
Claude hook emits a pair for *every* tool use, before `permit` decides — and
the daemon used to forward them onto the bus under those names, so auto mode
flashed a card and sent a DM per tool call (and a real question raised two
cards, one unanswerable). `daemon.GATE_KINDS` now logs them as
`gate_requested`/`gate_resolved` and never publishes them; the bus's
`approval_requested` comes only from `PendingApprovals`, and the HUD and the
Discord watcher both ignore one without a broker `code`. A broker timeout or
shutdown now publishes `approval_resolved` too, so a Discord post loses its
buttons. Dictation falls back from parakeet (one OpenRouter endpoint,
Together; `HTTP 404: Provider returned 404` when it is down) to
`config.STT_FALLBACK_MODELS` (default `x-ai/grok-stt-1.0`; free suite
`tests/voice_stt_check.py`), and `/stt` answers 502 with a sentence. The HUD
reads the preview port from `/status` → `workshop_port`, and `hud_v2_check`
aborts and fails any request to 8402/8403/8405 — it used to load the live
daemon's preview.

**Only the HUD window says yes, and none of Jarvis's ports is a web page to
Jarvis (BR-0, 2026-10-11; plan `docs/plans/2026-10-10-hud-browser-plan.md`
§1 item 4, decisions B-15).** Three approve paths closed together:
- **`config.control_ports()`** is the one Python list of Jarvis's own
  listeners — 8402 (face, v2 HUD), 8404 (desktop bridge), 8405 (daemon; v2's
  API serves the same HUD page), 8406 (Spotify callback) — at their defaults
  *and* wherever this process's config moved them. **8403, the workshop, is
  not in it on purpose** (v1's designer screenshots its own work there; v2's
  Browser gate admits it only as the daemon's reported workshop). v1's
  browser (typed, and wherever the page is — see below) and `fetch_page` refuse every one
  through `is_face_origin`, in every loopback spelling (`*.localhost`,
  `localhost.`, `127.1`, hex/octal/integer IPv4, IPv4-mapped and expanded
  IPv6, `0.0.0.0`, `::`) with no DNS. It used to refuse only 8402, so v1's
  browser could open the HUD on 8405. BR-1a's gate reuses the list.
- **`POST /approvals/{id}` is `projects.owner_only`**: the HUD's listener and
  the HUD's Origin, or 403. A page served by the API listener sends an Origin
  equal to its Host, which `check_origin` alone accepted. Discord and the
  escape hatch resolve in-process; `GET /approvals` stays open (reading is
  not a lever).
- **An out-of-band `answer()` can only deny, on both providers.**
  `POST /threads/{id}/answer` reaches `provider.answer` from any API client,
  and the provider's request id is in the thread log (`gate_requested`). The
  Codex provider always refused an ALLOW there; the Claude one accepted it —
  and coerced `{text: "allow"}` to one — and it beat the broker's card while
  the owner had not answered. The route tells the two kinds apart by body
  (`text` for a question, `decision` for an approval) and the provider by the
  pending slot (Codex marks question slots; Claude raises no questions, so
  every Claude slot is a tool permission — a source guard in
  `claude_provider_check` fails if that changes). Provider questions are
  Codex's, answered from Discord; task questions go through `TaskControl`
  and never reach `answer()`.
- **v1's browser judges a page where it is, not where it was sent** (the
  review's HIGH, older than BR-0). A page that passed goto's check and then
  moved itself — a timer, `location.replace`, a meta refresh — onto the
  HUD listener was snapshotted (the card's AUTHORIZE offered as a ref) and
  clicked, and the click's POST left the HUD listener with the HUD's own
  Origin, so `owner_only` passed; the guard backed out only after. Now a
  main-frame `framenavigated` handler backs out of the control plane at once
  (`Session._on_navigated`, on the browser thread, from whichever call pumps
  Playwright's events); `click`/`type_text` judge the page *before* acting;
  `snapshot`/`screenshot_b64` judge it before reading and after; every check
  asks the page itself (`location`, which page script cannot redefine) since
  `page.url` is stale while Python is idle; and the scan reads and stamps
  only the document whose origin was just judged (`_ORIGIN_GATE_JS`). A
  same-origin `pushState` trips nothing. Worth knowing: the daemon already
  answers a *cross-site* navigation onto the HUD with 403
  (`Sec-Fetch-Site`), so the shape needs a same-site page — any port on
  `localhost`.
- **A project's profile, always-ask list, extra folders and root change only
  from the HUD** (`PATCH /projects`, `daemon._OWNER_PROJECT_FIELDS`): any
  change to one is `owner_only`; one sent back unchanged, a rename, routing
  and every other field are as before. Any API client could loosen what asks
  it. `POST /projects` (create) is unchanged.
- **`fetch_page` follows redirects itself** (up to 20, cookies carried as
  httpx did) and judges each `Location` *before* requesting it; httpx's own
  following had sent the GET first. **`config.ambiguous_authority`** refuses a
  URL whose authority is empty or holds a backslash, or that holds a tab or
  line break — Python and WHATWG read those hosts differently (Chrome loads
  `http://<jarvis port>\@<public host>/` from Jarvis's port) — in
  `fetch_page` and `BrowserPolicy.allows` alike.
- **The built HUD is not writable by the write tools** (`config.HUD_DIST`,
  which `hud_api` serves): a file there runs with the HUD's Origin on the
  owner's next reload. `files._hud_build` refuses it to `write_file`/
  `edit_file` by resolved path and, through `_self_protected`, to v2's layer
  1 (Claude's Write/Edit, Codex patches); `denied_grant` refuses a Codex write
  grant on, in or above it. **Not a SELF_PROTECTED widening** (the owner
  declined that): the HUD's source stays writable. Since ED-4 (C-1)
  `config.HUD_DIST` is the **installed** HUD's root and `files._hud_roots`
  refuses the install root always (plus `hud/dist` under `JARVIS_HUD_DEV=1`),
  so this one check covers every installed build; only `jarvis hud install`
  writes it, and Jarvis is refused that command.
Free checks: `tests/face/approval_check.py` (`control_port_checks`),
`tests/browser/policy_check.py` (`control_port_*`, `fetch_redirect_checks`,
`late_landing_checks` — a port-0 daemon with one card, a stand-in and the
built HUD, four shapes of moving — and `read_gate_checks`),
`tests/v2/permissions_check.py` (`approve_route_owner_only_checks`,
`hud_build_deny_checks`), `tests/v2/claude_provider_check.py`
(`daemon_answer_route_checks`, end to end on a daemon),
`tests/v2/archive_check.py` (`ProjectPolicyIsTheOwners`) and
`tests/files_check.py` (`hud_build_checks`) — each fails on a49423c, and
the second round's on dfb1d7e (where v1's browser approved the card).
**Not a boundary against a program running as the owner** (it can send
the HUD's Origin to the HUD's port), the footing `/terminals` states; it
closes the routes a v1 agent's browser and any Origin-less API client had.

<!-- claude-source:000235-000337:end -->

**Current (2026-10-25, phone P-1):** `config.control_ports()` is now five
ports at their defaults: 8402, 8404, 8405, 8406 and **8408**, the phone's
remote listener (`config.REMOTE_PORT`), configured and default; still not
8403. The preserved text above predates it.

## Original CLAUDE.md lines 866–938

<!-- claude-source:000866-000938:begin -->
**jarvis-mcp for chat sessions, WP-1 (2026-10-10; plan
`docs/plans/2026-10-10-terminal-read-mcp-plan.md` §1–§2, §4, decisions file
beside it — the peers plan's Phase 0, built once).** Plumbing only, **no
behaviour change**: `jarvis/v2/session_tools.py` (the per-session tool list,
flags `read_only`/`foreground_only`/`desk_only`, `for_provider`) **ships
empty**, and with nothing in it no token is minted and no session gets a
jarvis server. When it holds something (WP-2: `terminal_read` plus the D-3
tools), `Daemon._run_brief` mints a caller token (`jarvis/v2/callers.py`:
`jmcp_` + `token_urlsafe(32)`, **held only as a sha256 digest**) for an
owner's chat thread on Claude or Codex, and puts `mcp_servers["jarvis"] =
bridge.server_entry(...)` (`python -m jarvis.v2.mcp_session`, env
`JARVIS_MCP_TOKEN`/`JARVIS_DAEMON_PORT`/`PYTHONPATH`) in **the run copy
only** — never brief.json; `_open` refuses a non-chat brief naming `jarvis`.
**Never on a command line**: the SDK puts a dict `mcp_servers` on `claude`'s
argv as JSON (world-readable through `ps`), so Claude gets a private 0600
`V2_DATA_DIR/claude/<thread>/mcp-<nonce>.json` in 0700 dirs and
`--mcp-config <path>` (checked with the SDK's own command builder), reused by
`set_model`, deleted by `close()` — **one file per session, never per
thread** (PR #35 review): a lost session is closed outside the daemon lock
while the next send opens its successor, and a shared `mcp.json` was deleted
from under the new session; Codex carries it in its private `config.toml`'s `env`
table, with `approval_mode = "approve"` per read-only tool, stripped on close
and on a failed open.
`Daemon.start` sweeps leftover carriers **after it holds its ports** (the bind
is the single-instance lock, so a second daemon on the live folder fails
before touching the live one's) and **only under the data folder it owns**
(`stores.root == V2_DATA_DIR`), so a test daemon never touches the live
one's; `/mcp` paths are logged without a query. The relay is **stdlib only and never runs a tool**: it pops the token
from its environment, writes nothing to stderr, lists `[]` and answers
`Error:` when it cannot reach the daemon. The bridge (`jarvis/v2/bridge.py`)
serves `GET /mcp/tools` and `POST /mcp/call` **on the API listener only**
(404 on the HUD and preview listeners), `Authorization: Bearer` only, 403
`not a Jarvis chat session` for any `Origin` or bad/revoked token or a
session that is not the thread's live one (`_sessions.get(id) is session` —
a missed revoke cannot reach a newer session) or not an owner's Claude/Codex
chat; 404 not offered, 400, 413 over 16 KiB. A call runs through the bridge's
**one** registry dispatch in a fresh `contextvars.Context()` with
`runtime.caller()` (`{thread_id, project_id, provider, turn_id}`, new) bound,
depth 0 and the offered names as the toolset; a **dangerous** tool goes to
the session's permit with `dangerous_tool=True` — the owner every time, no
Always, never the allowlist or the reviewer. Tokens are revoked by
`_cleanup` (close, archive, delete, a lost session), `_open`'s failure and
`stop()` (`revoke_all` first); a model switch keeps the token (D-7). Claude's
hook explicitly allows a read-only session tool under both profiles (after
`permit`'s new **`jarvis-tool`** layer, which follows layer 2 so an
always-ask rule still asks) and refuses a `foreground_only` one for a
sub-agent (`agent_id`) without asking `permit`; Codex declines an MCP
elicitation (before the turn-identity check, which used to kill the turn
over it) or an MCP-approval `requestUserInput` with a non-fatal ERROR, never
a QUESTION. The fast path binds `runtime.caller()` in its turn (the daemon
binds it on the turn thread, so the turn id is the daemon's). All four new
modules are SELF_PROTECTED. Free suite: `tests/v2/mcp_session_check.py`.

**Owner-only reads (2026-10-11, `projects.is_owner`).** Chromium sends no
`Origin` on a same-origin GET, and `is_owner` demanded one, so on the
owner's live daemon **every `GET /terminals` from the HUD was 403**: the
panel only worked through its "could not check" fallback, and opening it
could never open a terminal. The HUD suites missed it because `MockDaemon`
does not enforce `owner_only` — **a gate checked only against a mock is a
gate checked against the mock's idea of the browser.** Now a request is the
owner's when it arrives on the HUD listener and either (a) carries `Origin:
http://<Host>`, as before, or (b) is a plain GET/HEAD — not a WebSocket
upgrade — with **no** Origin header at all, a loopback Host and
`Sec-Fetch-Site: same-origin` (a forbidden header no page can set; any other
origin is `same-site`/`cross-site`). Never a missing Origin on a write or a
socket, never `Origin: null`. DNS rebinding is refused twice over (measured
in Chromium 149): a rebound name is not a potentially trustworthy URL, so it
gets no fetch metadata and (b) never matches, and its Host is not loopback,
which `hud_api.check_origin`'s strict Host test (and
`terminals._strict_host`) refuses first — keep it. `tests/v2/owner_read_check.py`
runs a real port-0 daemon and real headless Chromium and reads the headers
where the daemon receives them; its same-origin read fails on main.

<!-- claude-source:000866-000938:end -->


## Current implementation: Claude session MCP (WP-2)

Claude owner chats offer `terminal_read` plus the exact shared D-3 catalog
(`jarvis/v2/mcp_catalog.py`), filtered by registered tool availability. The
external MCP server still excludes terminal reads and retains its deny-all
dangerous-tool approver. Codex now shares this catalog under the WP-3
boundary below. Task workers receive neither a token nor a Jarvis server.

`desk.py` owns memory-only per-session authority. Every daemon turn gets a
fresh mutable slot using the original fast-path predicate. A non-HUD native
steer revokes that slot and reserves carry before provider handoff. Carry
costs the next turn; a separate pending set keeps every successor off-desk
while delivery remains unsettled. Completion removes only its pending token,
never grants authority to an existing successor. Only proven pre-write
refusal releases its own carry and may restore its own original active slot;
query errors/timeouts are ambiguous. Concurrent reservations are independent.
Queued messages do not revoke the current slot. End, Stop and close revoke
old slots, including those captured by in-flight calls. Fast-path capability
selection otherwise retains its existing behavior.

The authenticated bridge rechecks exact session/worker/turn under the daemon
lock, binds caller/depth/toolset/desk/stop in a fresh Context, and calls the
registry dispatch once. Dangerous approval waits recheck identity and Stop
after the human answer, before execution. Terminal results recheck desk/Stop
after the existing guarded read, so a concurrent steer cannot disclose its
text. The unchanged terminal note records the guarded read itself; a later
revoked result is refused without publishing raw text or a second note.

Claude's exact offered session-tool hook delegates to this bridge. Read-only
and nondangerous calls retain named always-ask rules (canonical or exact
server-prefixed spelling). Dangerous calls get exactly one owner decision at
the bridge, never Always; strict and layer-1 refusals still win. Foreground
terminal calls from Claude subagents are refused before permit. Other D-3
tools retain the owner's approved off-desk/subagent availability.

Claude terminal result summaries and denial reasons use fixed private words
or line counts, covering canonical and exact `mcp__jarvis__terminal_read`
names. Ordinary tool summaries retain 200 characters. Returned terminal text
still reaches the Claude/model transcript and anything the model repeats;
this is not a promise to filter model replies. Caller carriers remain subject
to the documented same-user process/file access limit. No desk state or token
is saved in the canonical brief or public event stream.

Free validation: `tests/v2/terminal_read_mcp_check.py` covers real ephemeral
listeners/relay/fake-shell PTY, native hooks over a fake SDK, lifecycle and
approval barriers, guard/read-switch behavior, summaries and catalog bounds.
Native authenticated Claude L1/L2/L5 validation is a separate release gate;
these fake/free checks do not claim that gate passed.


## Current implementation: Codex session MCP (WP-3)

Codex owner chats offer the same availability-filtered D-3 catalog (23
names declared, 15 read-only) plus `terminal_read`. Its supported profile
remains AUTO only; ASK/STRICT and always-ask brief additions still fail
provider validation. Defensive permit tests do not expand that matrix.
Per-tool native `approval_mode="approve"` delegates every exact offered name
to the daemon without falsifying `readOnlyHint`. The bridge checks the
session permit before nondangerous dispatch and after any wait. Dangerous
calls use dispatch's one owner card, never Always. External MCP stays deny-all.
Private config readback must match the actual relay, environment, enabled
names and per-tool policy. Start/resume override the exact new session server;
its 900-second timeout covers the broker's bounded owner wait. Existing
sandbox, network-off, reviewer and carrier cleanup rules remain in force.

Installed Codex 0.161 emits host outer `tools/call._meta`: `threadId`,
`callId`, and `x-codex-turn-metadata` with native thread/turn identity.
The relay forwards only bounded typed identities, never arguments._meta.
Both thread fields must agree. The provider admits a terminal call only
against its already-established active parent handle/turn, with no
parent/subagent marker and a never-used call ID. IDs remain spent for that
turn (cap 4096); cap exhaustion denies until a real new turn. The callback
checks the exact handle, turn, active send, cancellation and close again
outside daemon locks after waits and guarded reads. Missing or malformed
metadata refuses before terminal dispatch. This keeps native child D-3 tools
available without enabling child terminal reads; no global or chat-only
multi_agent setting is changed.

The same desk slots and steer carry described above apply. MCP item events
are matched to the current native thread/turn and stable server/tool/call
identity, deduplicated, and completed once on interruption. Terminal results
use only `read`, `read N lines`, `refused` or `error` in normalized summaries;
raw results and metadata are not retained in the provider's lifecycle map.
Raw tool text still goes to Codex/OpenAI and its private conversation files,
and anything the model repeats follows normal reply surfaces. Outer metadata
is an installed-native-host boundary, not protection from an OS user who
reads the carrier and forges a direct relay request. That accepted same-user
limit remains, including a malicious child using such out-of-band access.
Unmeasured internal agents are not claimed individually tested: a request
without the exact live parent identity fails closed.

`codex_session_mcp_check.py` covers identity, replay/races, policy/readback,
private lifecycle events, and the real relay plus guarded fake-shell PTY.
Synthetic installed-native gates passed parent/child identity and forged
argument provenance, parent success/child terminal refusal/child D-3 success,
read/write and exactly one dangerous deny/allow, a 65-second owner wait,
off-desk/accepted steer/postread withholding/next-turn carry, and resume
with a new token, old-token refusal and the same native thread. These probes
used no real terminal contents or account side effects. Independent review
and the full regression sweep remain separate release gates.


## Native Browser action delegation (BR9d)

`hud_browser_act` is an `owner_gated`, foreground-only, desk-only session tool.
Its native annotations remain truthful: read-only false, destructive/open-world
true. Claude explicitly delegates the exact owner tool under ASK/AUTO; Codex
uses its validated per-tool approve policy under AUTO. Both reach the same
bridge permit, Browser review/private card, budgets and exact foreground guards.
A named always-ask permission is a separate private delegation card and never
pre-approves an action. Strict refuses; native subscription validators are intact.

Native request identity and the current desk/DriveTurn are checked again after
waits. Relay notifications validate typed cancellation parameters, bind the exact
socket before request bytes, and cannot let captured cleanup cancel a reused-ID
successor. Private gate labels/records omit arguments; native tool results still
enter that provider's conversation. See [contract and measured scope](browser.md#native-parent-browser-driving-br9d).

## Fast-path approvals (2026-10-14)

The fast path's desk-turn edits and commands (decisions F-1..F-7) ask through
the route every provider uses: `dispatch` → the provider's approver → the
daemon's `PermitContext`/`build_permit` permit → `PendingApprovals.ask`,
which publishes `approval_requested` for the thread and blocks the turn's
worker. The owner's card answer (`POST /approvals/{id}`, owner-only) or a
Discord reply resolves the broker directly; `Daemon.answer` →
`provider.answer` is for provider questions and the fast path still refuses
it. A timeout, shutdown or unreadable answer denies. The fast path asks with
an ASK copy of an AUTO brief, so layer 5 never hands its call to a reviewer it
does not have.

## Chat-to-chat: the rules in one place (C0-C5 merged, 2026-10-19)

Owner request and answers: `docs/plans/2026-10-19-chat-to-chat-decisions.md`
(CC-1..CC-9, which win over the plan, `docs/plans/2026-10-19-chat-to-chat-plan.md`).
It widens the old rule "session tools read, never switch the owner's live
session": no tool switches that session, and reading and messaging are as
follows. Status: C0, C1, C3, C4 and C5 are merged and live (daemon restart
2026-10-19, `00c11df`); C2 was folded into C3 (CC-6 dropped the plan's `typed`
desk key); C6 (approval-card provenance) is merged (#275, below) and
installed and live (`725459c`, build `20261020T071216527282Z-f85e9cd91489`, daemon restarted 2026-10-20 ~07:12Z; L4 not run); the native release checks L1-L4 are **not run**
([runbook](../plans/2026-10-19-chat-to-chat-live-gates.md)).

| Decision | Rule now in force |
|---|---|
| CC-1 reads | Every HUD chat (Jarvis, Claude, Codex) reads every other open chat; archived and trashed chats are neither readable nor reachable. |
| CC-2 sends | Every chat can send to every other chat. |
| CC-3 / CC-6 when | Only during a turn the owner started at the HUD, typed or spoken: `desk.slot["present"]`, depth 0. A task, schedule, Discord message, DM, sub-agent or a turn a foreign steer took off the desk cannot send. |
| CC-4 reply | Asynchronous: the sender moves on; the target's streamed reply comes back once (a step of the sender's running turn, else a short reply turn, else only shown). `chat_wait` blocks only when asked. |
| CC-5 busy target | Queued behind the current turn (and the owner's own queue); never interrupts. |
| CC-7 tools | A relayed or reply turn keeps every tool a non-desk turn holds, under the target's own gates and approvals; it cannot send onward (no chains). No `RELAY_REFUSED`. |
| CC-8 | `jarvis/v2/chat_relay.py` and `jarvis/v2/tools/chats.py` are SELF_PROTECTED. |
| CC-9 | Each C-PR merges once the lead's review and the full sweep pass; a daemon restart is still asked separately. |

Lead defaults (reversible, not asked): the HUD shows every relayed message in
both chats and it is never the owner's (no "YOU"); incoming text is fenced
untrusted data with its provenance; a message from a chat never approves,
denies or answers a card (the "no operation of one's own gate" rule: the
service has no path to the broker, `Daemon.answer`, `deliver` or `_steer`);
reads pass the same redaction as the HUD display plus the credential scrub;
O-2 a mid-turn reply keeps the sender's desk, O-4 card provenance is C6 (below) and
Always stays, O-7 `chat_summary` is deterministic (no model call), O-8
Discord and read-aloud skip relayed and reply turns, O-9 Stop withdraws queued
sends.

**C6, card provenance (O-4: add the line, keep Always; #275).** A card
raised by a call from a relayed turn (in the target) or a reply turn (in the
sender) says "turn started by a message from chat ‹title› (Provider · id)"
under REQUESTED BY: the provider and the chat's thread id keep two chats with
one title apart. The route is the one permit every provider is handed:
`Daemon._permit` gives `PermitContext` a `relayed_from` reader over the live
session, and `permissions._ask` copies `ctx.relayed(child)` onto
`ApprovalRequest.relayed_from` (`{"title", "provider", "thread_id", "kind"}`
or None) for the card. **Whose turn** (`daemon._relayed_from`; the lead's
decision on the reviews of #275):

- The orchestrator's own call (`child` None): the running turn's
  (`_turn_relay`: the relay item's sender for a relayed turn, its target
  for a reply turn, None for any other turn, between turns, or a mark that
  does not match), kind `started`.
- A native sub-agent's call (`child` its sid) whose spawning turn was a
  relayed one: that turn's, kind `started`, even after it ended.
  `_record_subagent` stamps each child once, at its first record
  (`_stamp_child`): a grandchild whose parent the daemon has seen takes a
  copy of the parent's stamp (none if it has none); a top-level child — its
  lifecycle `started`, with no parent — takes the turn its record was read
  in, never a record read between turns (`idle`) nor one during whose
  preparation outside the lock another turn began; anything else (a parent
  the daemon never saw, its `started` dropped past a provider's bound, or a
  first record that is not the lifecycle `started`) gets no stamp. A child
  seen once is never stamped again: the session keeps up to 256 relay
  stamps (oldest forgotten first) and every sid it has seen, for its life.
  **Native ordering (follow-up to #275):** a Claude child is also stamped
  from the orchestrator's own spawning `Agent` (or `Task`) call, a
  TOOL_STARTED in the turn's stream whose call id makes the child's sid
  "cl:<id>" (`daemon._spawned`), so a background child whose `task_started`
  the CLI emits only after the turn's result keeps its line. Codex children
  are stamped from their first record only; whether Codex can announce a
  relayed turn's child only after that turn ends is not verified: L4 step 5
  of the [runbook](../plans/2026-10-19-chat-to-chat-live-gates.md) observes it.
- Any other child, or a call nobody can attribute (`child` ""): if a relayed
  turn is running when it asks, that turn with kind `running`, drawn as
  "while a turn started by a message from chat ‹title› (Provider · id) runs"
  (it may be carrying that chat's words to the child: Claude continuing an
  agent, Codex sending a child input), else nothing. So an owner turn's
  child re-tasked by a later relayed turn says "running" while it runs, and
  nothing after; a child the daemon never saw (Claude's unnamed child before
  its `task_started`, a forgotten one) is treated the same way.
- **The jarvis-mcp bridge carries no child attribution**: its dangerous-tool
  ask (`bridge._approver`, `dangerous_tool=True`) and a Codex session tool's
  ask (`bridge._delegated`) cannot tell a child from the orchestrator, so
  `build_permit` asks the reader with "" and their cards are at most
  `running`, never `started`.

Claude passes the child's sid with every child call it knows; Codex passes
it on every child approval (each is a widening). So it covers the Claude
hook's question, Codex's approvals and widenings, the bridge and the fast
path's approver. The fast path's relayed and reply turns hold no tool that
reaches the permit today (`run_command` and the edits are desk-only, and no
`dangerous` tool is in the set), so no card arises there yet; Browser drive
cards cannot arise in a relayed turn (the drive refuses any turn not at the
desk); the Browser delegation consent card is private and its projection
keeps its fixed fields, so it does not show the line; Discord's card does not
show it. **Display only:** the reader is read once per card, a reader that
raises or returns a non-dict shows nothing, and no layer reads it; the
decision, layer, reason, asked-or-not, the whole request but the provenance
(Always, timeouts and waits included) and human-backed status are the same
with and without it, whatever its kind (`tests/v2/permissions_check.py
relay_provenance_checks`, every profile, provider and asker). The title is
another chat's model-written text: `chat_reads.card_title` (and the HUD's
`cardTitle` again, by the same rule) keeps one line of at most 60 characters;
every opening, closing and quotation bracket but ASCII ( ) [ ] { }, < > and
their look-alikes, and dot look-alikes become `_`; symbols, emoji, combining
marks and Hangul fillers go; nothing left is "(untitled)". `_turn_relay`
leaves out a provider outside fast/claude/codex and an id that is not 8 hex
characters, and the HUD draws neither. The HUD isolates the title in a
`<bdi>`, clips the line in `--text-2`, never amber or red, and puts the whole
line in its hover title. A mid-turn reply step in an owner's turn adds no
line (the turn is the owner's). Tests: `tests/v2/relay_provenance_check.py`,
the `provenance_checks` section of `tests/face/hud_v2_relay_check.py`, and
vitest in `Approvals.test.ts` and `relays.test.ts`.

Where each part lives:

| Part | Code | Notes |
|---|---|---|
| C0 read projection | `jarvis/v2/chat_reads.py`, `untrusted.fence_chat` | below, and Runtime |
| C1 read tools | `tools/chats.py` (`chat_list`, `chat_summary`, `chat_search`, `chat_read`) | "Chat reads in the session catalog" |
| C3 relay core | `jarvis/v2/chat_relay.py`, `Daemon._begin/_drain/_turn` | "The chat-to-chat relay core" |
| C4 send tools | `tools/chats.py` (`chat_send`, `chat_wait`) | "Sending in the session catalog" |
| Providers | fast path, Claude, Codex | [Runtime](providers-runtime.md#chat-reads-on-all-three-providers-chat-to-chat-c1-2026-10-19) |
| C5 HUD rows | `hud/src/lib/relays.ts`, `components/RelayRow.tsx` | [Workspace](hud-workspace.md#chat-to-chat-in-the-hud-c5-2026-10-19) |
| Wire contract | `hud_api._relay_message`, the withdraw route | [`docs/hud-api.md`](../hud-api.md) |

**The C0 projection (`chat_reads.py`).** Pure over `Stores`, no daemon, no
model call, no v1 session and never a native CLI transcript (P-1): the one
source is each thread's own `log.jsonl`, and only these rows come back: the
owner's typed words (`data.typed`, never the provider-received `text`,
attachments named and never opened), each chat's `text`, the three relay
records, and `model_set`, `notice`, `cli_turn` and `compacted` as system
lines. Tool events, arguments, results, thinking, gates, questions, plans,
usage, errors and sub-agents are never included. Every row is scrubbed where
it is read, outside any daemon lock: `strip_invisible`, `secrets.redact_inline`
(#205), `redact.scrub_text`, `strip_invisible` again; a scrub that raises
withholds the row, never passes it. Every result carrying a chat's words is one
`untrusted.fence_chat` with a fresh nonce. Limits (pinned by
`tests/v2/chat_reads_check.py`): list 20/50, search 20/50 over the 200 latest
chats in a 2 s budget (query 2-200 characters), read 20/100 messages, 2,000
characters a row and 16,000 a read, summary 3,000 characters over 6 rows, a
4 MiB log tail. A chat is named by its 8-hex id, its exact title or
`Project / Title`; several candidates are refused with their ids, never
guessed.

**Messages and bounds at a glance.** 3 sends per owner turn, 3 relays waiting
per target, 4,000 characters (refused, never cut), 30 relayed turns an hour
daemon-wide, a waiting relay expires after 60 minutes, a reply passes to the
model at 6,000 characters (tail kept), `chat_wait` 120 s by default and 300 s
at most (under Codex's 900 s tool timeout and the relay's 900 s; whether the
Claude CLI honours a 300 s MCP call is L1), a fast-path reply turn gets 3
steps. All are constants in `chat_relay.py`.

## Chat reads in the session catalog (chat-to-chat C1, 2026-10-19)

`chat_list`, `chat_summary`, `chat_search` and `chat_read`
(`jarvis/v2/tools/chats.py`) are `SESSION_TOOLS` entries for Claude and Codex
owner chats: `read_only=True`, neither foreground- nor desk-only, so the
listing says `readOnlyHint: true, destructiveHint: false, openWorldHint:
false`, Claude's hook returns the explicit "a read-only Jarvis tool" allow
(sub-agents included, as for `session_*`), and Codex gets the per-tool
`approval_mode="approve"` and `enabled_tools` entry its readback must match.
They are not in `mcp_catalog.MCP_TOOLS`: the external `jarvis mcp` neither
lists nor runs them. They are `EXPLICIT_ONLY`, `PRIVATE_RESULT` and
`PARALLEL_SAFE`.

Each tool refuses (`Error: chats can be read only inside a Jarvis HUD chat.`)
unless a v2 caller is bound and the daemon's `ChatRelay`
(`jarvis/v2/chat_relay.py`) is installed. The daemon installs it in `start()`
and removes it at the start of `stop()`; there is no HTTP route behind it.
The service hands the tools only the store and a snapshot of running turns
and owner-queue lengths, taken under the daemon lock; the log reads and the
scrub (`chat_reads.py`, C0) run outside it. Both files are SELF_PROTECTED
(decision CC-8). Sending (`chat_send`, `chat_wait`) is below.

## The chat-to-chat relay core (chat-to-chat C3, 2026-10-19)

`ChatRelay` (`jarvis/v2/chat_relay.py`) sends. `send(caller, chat, text)`
and `wait(caller, message_id, seconds)` are the service calls the
`chat_send`/`chat_wait` tools make; no HTTP route sends (P-3).

- **Who may send** is checked by the service under the daemon lock, never
  only by a tool: the caller's live session, a running worker, the exact
  `turn_id`, an owner's chat (role CHAT, no task), not cancelled or closing,
  `relay_turn is None` (no chains) and `desk.slot["present"] is True`. Under
  CC-6 that is the whole desk rule: typed or spoken at the HUD, never Discord,
  the DM, a schedule, the escape hatch, the API listener, a task or a turn a
  foreign steer took off the desk. A relayed or reply turn is refused even
  with a forged `present`.
- **Bounds** (§5.4): 3 relays waiting per target (`RELAY_QUEUE_MAX`, separate
  from the owner's `QUEUE_MAX`), 3 sends per turn, 4,000 characters (refused,
  never cut, checked before the scrub), 30 sends per rolling hour
  daemon-wide, a one-hour TTL on a waiting relay, waits of at most 300 s.
  The TTL, hourly cap and wait deadlines read `ChatRelay.clock`.
- **Two queues, never one (P-4).** A relay waits in `inbound[target]`, never
  `_queues`. `_drain` takes the owner's queue first, then `inbound`, then
  `held` replies. A relay never goes through `deliver()` or `_steer`: it
  never steers, interrupts, reserves the desk, or answers a card or a
  question; a target blocked on the owner keeps it queued. The owner's own
  message still steers into a running relayed turn.
- **A relayed turn** is `_begin(..., relay=RelayStart)` with
  `UserMessage(origin="chat", via="peer", desk=False)`: off the desk, so
  desk-only tools stay away, and every other tool a non-desk turn holds is
  kept under the target's own gates (CC-7; there is no `RELAY_REFUSED`). Its
  log opens with `relay_in`, never `user`, and every record of it carries
  `relayed: {"message_id", "from"}`. The Discord mirror and the task runner
  skip `relayed` records (O-8), as HUD speech does (Workspace note).
- **The reply goes back once** (`turn_ended`, from `_turn`'s `finally`
  before the worker is cleared): to a blocked `wait`; else steered into the
  sender's running owner turn with `origin="chat-reply"` and no
  `desk.reserve()` (P-7), unless that turn waits on the owner; else, for an
  answer, held to run as a reply turn (3 fast-path steps, `relayed` with
  `"reply": true`); else only shown (P-10: stopped or failed never starts a
  turn). A refused or failing steer, whatever its `fallback`, goes to
  `held`; a reply never interrupts. A steered reply the turn never took
  (`undelivered`) is handed back to `held`, never to `_queues`.
- **Stop, Withdraw, close, shutdown.** The owner's Stop on a thread drops
  relays waiting on it, withdraws that turn's own still-queued sends and
  shows (never runs) its held replies; a running relay elsewhere goes on and
  its reply is only shown. Closing or archiving drops what waits on the
  thread; a sender archived or deleted mid-flight gets its reply dropped
  (`relay_state` reason in the target's log). `POST
  /threads/{sender}/relays/{mid}/withdraw` is owner-only. Shutdown drops
  everything in memory and wakes waits without logging, so after a restart
  an unfated `relay_out` reads "not delivered".
- Bodies and replies go through `chat_reads.scrub` before they are logged or
  delivered, and through `untrusted.fence_chat` with a fresh nonce; titles
  are cleaned and their fence phrases annotated. The source guard in
  `tests/v2/chat_relay_check.py` keeps `chat_relay.py` and `tools/chats.py`
  free of the broker, the daemon's answer, `deliver` and `_steer`.

## Sending in the session catalog (chat-to-chat C4, 2026-10-19)

`chat_send` and `chat_wait` (`jarvis/v2/tools/chats.py`) call the service's
`send` and `wait`. Both are `SESSION_TOOLS` entries for Claude and Codex
owner chats, **not** read-only, so the listing says only `readOnlyHint:
false`, and neither the hook's read-only allow nor the `jarvis-tool` layer
covers them: `permit` decides at `jarvis-session-delegate` (allowed unasked
on AUTO and ASK, refused unasked for STRICT; an always-ask rule naming one
still asks). Both are `foreground_only`: Claude's hook denies a sub-agent's
call (`agent_id`) before `permit` runs, and Codex admits only the exact
native parent turn. `chat_send` is also `desk_only`; `chat_wait` is not, as
a turn may wait on its own message from any surface. Codex gets the per-tool
`approval_mode="approve"` and `enabled_tools` entries. Neither is in
`mcp_catalog.MCP_TOOLS` or `PARALLEL_SAFE`; both are `EXPLICIT_ONLY`; only
`chat_wait` is `PRIVATE_RESULT` (it returns another chat's words, while a
send's result is its own sentence).

Authority is checked twice (P-6). The tool refuses first, as
`hud_browser_view` does: `chat_send` unless `runtime.at_desk()` and
`runtime.depth() == 0` (CC-6: typed or spoken at the HUD; no `typed` key),
`chat_wait` unless `runtime.depth() == 0`; both without a v2 caller or the
installed service. The service then re-checks the live session and exact
turn under the daemon lock (C3). A relayed or reply turn is never at the
desk, so it cannot send onward (CC-7, no `RELAY_REFUSED`). The bridge's
`request_alive` peek now covers `chat_wait` as well as the Browser look and
act: a relay socket that closes makes `current()` false, so the wait's
`should_stop` ends it and the reply goes on through the next delivery route.
The owner's Stop ends a wait through the same `current()` and the service's
own live-turn check.

## The Compact button's route (2026-10-19, WP-A)

`POST /threads/{id}/compact` (`hud_api.route`) is the owner's alone:
`owner_only` runs first, before the thread is looked up or a body read, so
the API listener gets 403 whatever it sends. Then an empty query and an
empty body (a field is 400; a focus text is a later PR), and 202 with
`Daemon.compact(id)`; any other method is 405. It is not a tool: no MCP
tool, `bridge.py` route, Discord command or CLI verb reaches
`Daemon.compact`, and a source guard in `tests/v2/compact_check.py` pins
the HUD route as its only caller.

`Daemon.compact` resumes a closed session (`_live_session`, no model call)
and refuses with 409, never calling the provider, when `_sendable` refuses,
the thread is not an owner's chat ("Tasks are not chats; only a chat
compacts."), a turn runs ("A turn is running; compact when it ends."),
owner messages or chat relays wait ("Messages are waiting to run first."),
the provider has no `compact` ("This chat's provider cannot compact its
conversation."), a CLI turn of its own waits (it starts instead), the turn
waits on the owner ("Answer the card first.") or `compact_refusal` says why.
`_begin_compact` (modelled on `_begin_cli`) sets `_Session.compacting`,
logs a `compact_started` record, and runs `_turn` over `provider.compact`
with no desk, model switch, drive turn or `user` record. While it runs,
every record is stamped `compaction: true`, `deliver` queues (never steers)
and `chat_relay._hand_over` never steers a reply into it (held, run as a
reply turn after the owner's queue). Stop is `interrupt`, unchanged: the
owner's queue is dropped (CB-1). A compaction is not counted in
`thread.turns`; its usage and cost count. The provider's reason is
scrubbed as chat reads are (`chat_reads.scrub`, then one line of at most
200) before the turn's `turn_finished` is recorded, so neither the log nor
the bus carries it raw, and the turn ends with a `compacted` record:
`{"ok", "text"}`, the text "Conversation compacted", "Compaction stopped",
"Not compacted: <reason>" (ended, nothing compacted) or "Compaction
failed: <reason>". No summary text is logged. The task runner, the Discord
mirror (CB-5) and activity skip `compaction` records (a compaction never
makes a chat unread); `"compacted"` is a system row in chat reads and the
HUD transcript; `thread_json` carries a live `compacting`.

## The Bypass switch's route (2026-10-21)

`POST /threads/{id}/bypass` (`hud_api.route`) is the owner's alone:
`owner_only` runs first, before the thread is looked up or a body read, so
the API listener gets 403 whatever it sends. The body is exactly
`{"on": bool}` (missing, non-bool or extra fields are 400; a query is 400);
any other method is 405. 200 with `Daemon.set_bypass(id, on)` →
`{"bypass": bool}`. It is not a tool: no MCP tool, `bridge.py` route,
Discord command or CLI verb reaches it, and `tests/v2/bypass_check.py`
pins the HUD route as the only caller of `Daemon.set_bypass` and of any
`.set_bypass(`. On resumes a closed session (`_live_session`) and is 409
for a task thread, an archived chat, a provider without the switch, or the
provider's `bypass_refusal` sentence; off always succeeds, a no-op with no
open session; a provider missing a Bypass method, or a session that closes
mid-switch, is 409, never 500. A change appends one `notice` record and publishes
`bypass_changed {bypass}`; a repeat records nothing. What the switch does
to the gate: safety notes, *The owner's Bypass switch*.

## The owner inbox routes (inbox A1, 2026-10-25)

`jarvis/v2/inbox.py` (SELF_PROTECTED) is a projection, never a store
(design `docs/plans/2026-10-24-owner-inbox-design.md`, IN-1): live approvals
(rank 1), open provider questions on a running turn and blocking questions of
`clarifying` tasks (2), proposals inside their grace window (3), `blocked`
tasks (4) and `failed` tasks (8), ties to the older item. `title` and
`blocks` are built from ids, states and tool names through `clean_line`,
never from a thread title. Agent text is in `detail` and in `origin`, which
is the approval's `approvals.label` attribution with its model-chosen task
name (one `clean_line`d line). **The HUD view (A2) must render `origin` and
every `detail` field as untrusted `textContent`**, as the approval card
does. An approval's `detail` is exactly `ApprovalRequest.to_json()` (a
private card's row carries nothing more).
`GET /inbox` and `POST /inbox/<id>/answer` run `owner_only` before anything
else on every `/inbox` path. The id is percent-decoded once (the HUD's
`encodeURIComponent`). An answer finds its row in a fresh projection
(absent: 409) and calls the source's own function with its own surface's
arguments: `Daemon.resolve_approval(req_id, decision, always, hud=<the Host
test of POST /approvals>)`, `Daemon.answer(thread, req, text)`,
`TaskControl.answer_question(task, index, text)`, and `TaskControl.cancel` for
a proposal's withdraw (`{"withdraw": true}`). A `task` row, an unknown kind or
a malformed body is 400. The bus observer publishes `inbox_changed` `{ids,
reason}` (never text) after each source event, in the same publish; the
project archive/restore/delete events count as sources too. `Daemon._finish`
clears a turn's open questions before `turn_finished` is published, so the
row is already gone when that event names it.
`tests/v2/inbox_check.py`; `tests/v2/archive_check.py` keeps every tool, MCP
catalog entry and session tool away from the routes and the module.

**"Denied while you were away" (inbox A3, IN-3, 2026-10-25).** The inbox's
one store. When a broker waiter ends with resolution `timeout` or
`shutdown`, `Daemon._approval_resolved` (the broker's `on_resolve`, both
broker constructions) calls `Inbox.approval_ended` **before** it publishes
`approval_resolved`; `approvals.py` is unchanged. `MissedLog` appends one
line to `<data root>/inbox/missed.jsonl`: `{req_id, tool, origin,
thread_id, task_id, at, resolution}`, read from `ApprovalRequest.to_json()`
only, never `owner_json`. A private (HUD-only) card records its private
projection's fields only: `origin` "" and `task_id` null (also emptied by
name). One `os.write` of a whole line to an `O_APPEND`/`O_NOFOLLOW`
descriptor under a lock, so a shutdown's N waiters write N lines; the file
is 0600, `inbox/` 0700, both tightened if found wider, and a symlink or a
non-regular file there is refused. Both opens carry `O_NONBLOCK` (review of
#307): a FIFO at the path used to block the open before `fstat` could refuse
it, stalling every later waiter (under the lock) and every `GET /inbox`; now
a FIFO read is refused at once and a FIFO write is ENXIO. **Bound:** at most `MISSED_KEEP` (500)
records; an append that would make 501 rewrites the file with the newest 500
by `stores._write_bytes` (temp file, `os.replace`). A last line without its
newline (a torn write) is never read and is dropped at the next write.
**A write failure never touches the resolve:** `record` and
`approval_ended` each catch everything and log only the error's class; the
broker had already settled the deny. The publish's `inbox_changed` then
names `missed:<req_id>` beside `appr:<req_id>` (reason
`approval_resolved`, ids only), and only when the line was written.
`rows()` adds a `missed` row per record at rank 8: title `Denied while you
were away: <tool>`, blocks `task <id>` or `chat <id>` `was denied it:
nobody answered in time` / `Jarvis stopped before you answered`, the
record as `detail`, `origin` as untrusted text, no `project_id` (so a
project's archive does not hide it, as it never hides an approval).
**Retention until B3's dismiss:** rows show only records whose `at` is
within `MISSED_WINDOW` (7 days) of now (a future `at` is taken as now);
older lines stay in the file until the bound trims them.
`POST /inbox/missed:<id>/answer` is 400, whatever the
body: there is nothing to answer and no "ask again" (design §10, question
3); the row links to its chat.
**`Daemon.stop()` releases every pending ask** (review of #307): right after
`callers.revoke_all()`, outside `self._lock`, it calls
`PendingApprovals.shutdown()`, so each waiter returns DENY at once with
resolution `shutdown` (one record each) instead of waiting out its timer or
dying with the process. Before this, `stop()` released no waiter itself:
only an ask with a `cancelled=` callback could end early, as `cancelled`
(the permit's `_ask_owner_delegation`, through `_delegation_cancelled`,
which reads `_stopping`; the Browser drive asks in `browser/drive.py` and
`drive_downloads.py` pass their own). Every other ask (ordinary permit
cards, the escape hatch, Discord `/project new`) waited out its timer or
died with the process, unrecorded. Each released
waiter announces on its own thread and the bus closes later in `stop()`, so
`stop()` waits, at most `RELEASE_WAIT_S` (2 s), until the broker's
`decisions` log holds each released id (appended after `approval_resolved`
was published): every window then clears the card. `main()`'s `finally`
ordering for Discord is unchanged (an owner decision, open).
`missed.jsonl` **and the `inbox/` folder path itself** are protected state
(`files._protected_state_paths`, keyed on `config.V2_DATA_DIR`): v1 write
tools refuse them, v2 layer 1 refuses them (`denied_file`'s generic
"permission state" sentence), the shell gate asks for any write to the file
and for moving, replacing, re-moding or deleting the folder. Since B1 the
whole `inbox/` tree is protected (below).

**Decisions and `owner_post` (inbox B1, IN-5, 2026-10-25).** An agent's
"answer whenever" question to the owner. Three modules: `jarvis/v2/decisions.py`
(the store and the post service, `OwnerPosts`), `jarvis/v2/tools/owner_post.py`
(the tool) and `inbox.py` (rows, the answer route, delivery); all three are
SELF_PROTECTED.

- **The tool.** `owner_post(kind, question, options, why, default=None)`,
  kind `"decision"` (since B2 also `"check"`, below), 2 to 6
  options (text, never actions), `default` an option's index or absent. Its
  result is its own sentence ("Posted decision d… to the owner…"), so it is
  not `PRIVATE_RESULT`; `EXPLICIT_ONLY`, not `PARALLEL_SAFE`, not dangerous,
  not in `mcp_catalog.MCP_TOOLS`.
- **Bound as `chat_send` is (§10 question 1, chat-to-chat CC-3/CC-6/CC-7,
  P-6).** A `SESSION_TOOLS` entry for Claude and Codex owner chats,
  `foreground_only` (a Claude sub-agent is denied by the hook before
  `permit`; a Codex child by `admit_foreground_mcp`) and `desk_only`, not
  read-only (`permit` decides at `jarvis-session-delegate`: unasked on AUTO
  and ASK, refused for STRICT, asked under an always-ask rule); Codex gets
  the per-tool `approval_mode="approve"` and `enabled_tools` entries. Checked
  twice: the tool refuses unless `runtime.at_desk()` and `runtime.depth() ==
  0`, then `OwnerPosts.post` re-checks under the daemon lock exactly
  `ChatRelay._refuse_sender`'s rows (live session, worker and handle, exact
  `turn_id`, not cancelled/closing/retired/lost, `relay_turn is None`, role
  CHAT with no task, `desk.slot["present"] is True`) **plus** a Claude or
  Codex thread. So the fast path (it holds no such tool: not in
  `FAST_TOOLS`, `DESK_TOOLS`, `EDIT_TOOLS` or `CHAT_TOOLS`, and the service
  refuses a fast thread anyway), sub-agents, relayed and reply turns,
  schedules, Discord (voice or text), the DM, the escape hatch, the API
  listener, a turn a foreign steer took off the desk, between turns, and a
  decision's own delivered answer (`via="inbox"`, off the desk) never post.
  **Task orchestrators are not bound (a deviation from the design's
  default, §10 question 1):** task threads get no session server at all
  (`Daemon._refuse_jarvis_server`, `callers.authenticate`, the bridge's
  `live_session`, `claude_session_tool`/`codex_session_tool` all require an
  owner's chat), and giving them one is its own boundary change, left for
  the owner.
- **Caps (§10 question 2), refused never cut.** 5 open decisions per chat
  (`OPEN_PER_THREAD`; "5 per thread" read as 5 waiting at once), 10 posted
  by one chat (`PER_THREAD_DAY`) and 20 in all (`PER_DAY`) in any rolling 24
  hours, all counted from the files under the store's lock (a restart
  forgets nothing; concurrent posts keep the caps); question 500, each
  option 120, why 1000 characters; 2 to 6 distinct non-empty options.
  Unknown kinds, wrong types, a `bool` or out-of-range default and extra
  arguments are refused, nothing written.
- **Scrub, display rule and provenance.** Every field through
  `chat_reads.scrub` (invisible characters, typed credentials, Jarvis's own
  credential values, private-key blocks, the known key and bearer shapes),
  then the display rule below, then the scrub again, before it is stored:
  a token split by a character the display rule drops is whole again after
  it, so the second scrub is the one that counts (round 3 of the review). A
  scrub that raises refuses the post. Each is cleaned as the owner will see it (`decisions.display_text`, the sibling
  of `chat_reads.card_title`'s character rule: NFC; enclosing marks,
  symbols, format and private-use characters and the Hangul fillers go; at
  most two combining marks per character, so no Zalgo tower) and judged
  again (review of PR #309): a question, why or option left empty is
  refused, two options alike under NFKC, case-folding and a small
  confusables skeleton (the common Cyrillic and Greek look-alikes of Latin
  letters; not Unicode's whole table, which would be a dependency) are
  refused (so
  two credentials that scrub to one string, or `yes` beside a `yes` with a zero-width space in it), and no
  option may say "(suggested)" (the HUD's own marker). `origin` records the
  asker: "Provider · chat ‹title› (id)", the title through
  `chat_reads.clean_title` and `card_title` (one line, brackets and bidi
  gone), as the relay line on a card; a task's would be `approvals.label`'s.
- **The store.** `<data root>/inbox/decisions/<id>.json` (`d` + 12 hex), one
  per decision: §3.2's fields (`id, thread_id, task_id, project_id,
  question, options, default, why, asked_at, answer: {choice, text, at} |
  null`) plus `kind`, `origin` and `delivery` (`started | queued | closed |
  failed`, set once answered). `inbox/` and `decisions/` are 0700 and each
  file 0600, tightened when found wider. Every open is relative to folder
  descriptors opened `O_DIRECTORY | O_NOFOLLOW` (a symlinked `inbox/` or
  `decisions/` is refused, nothing written through it); files are read
  `O_NOFOLLOW | O_NONBLOCK` and refused unless `fstat` says regular and at
  most 64 KiB (a FIFO never blocks a read, a symlinked file is never read or
  written); a write is a fresh `O_EXCL` temp file, fsynced, then
  `os.replace`d. A file that does not read back as exactly a decision
  (every field present with its type and no other, `kind` "decision", its
  name and id agreeing, the option and answer shapes holding) is never a row,
  never counted and never answered; and `inbox.rows` reads each source and
  each decision on its own, so one that raises shows nothing of its own and
  never takes `GET /inbox` (or the phone's `/m/api/inbox`) down. At most `KEEP` (500) files: the oldest answered go
  first; a post with none to drop is refused, nothing open is ever dropped.
- **Protected: the whole tree.** `files._inbox_tree` makes everything at or
  below `<V2_DATA_DIR>/inbox` `_self_protected` (v1 write tools refuse it;
  v2 `denied_file` and `denied_grant` refuse it); the `decisions` folder is
  also named in `_protected_state_paths`. The shell gate's
  `protected_state.owner_trees` judges any write that lands at or below the
  tree: a provable one (a redirect onto, `cp`/`rm`/`tee`/`mv`/`touch` on a
  path there) is a certain touch, refused by v2's layer 1 and asked by v1;
  an unprovable one asks. Every spelling counts, on #310's machinery: each
  word of a brace expansion is an operand of its own, and a glob is matched
  component by component (`_glob_relate`: it may be the tree or lie below
  it, so `<D>/*/decisions/x`, `<D>/i?box/decisions/`, `<D>/**/x` and
  `decision[s]` are all inside) and by what it reaches on disk (`_glob_fs`,
  through a symlink); a walk cut short asks. A folder *above* the tree is not the tree rule's:
  it goes on to `_ancestor_of` exactly as before, so `cp -r /tmp/x/inbox
  <data>/` stays a certain DENY and `rm -rf <data>` still names
  `missed.jsonl` and the phone's files on its card (review of PR #309, which
  found an early `continue` there; a 48-command differential against main
  `291d1f2b`, the brace and glob spellings included, shows the only verdict
  changes are writes inside the tree, ASK or ALLOW before, DENY at v2's
  layer 1 now).
- **Rows.** An open decision is a `decision` row at rank 6 ("When you have
  time"), oldest first: title `chat <id> asks for your decision`, blocks
  `nothing waits; chat <id> hears your answer as a message`, or `chat <id> is
  closed; an answer is recorded only` when the asking chat is gone or it or
  its project is archived (an archived project's decision is still shown,
  without its `project_id`). The question, options, default, why, answer
  and delivery are in `detail` only, untrusted text; `origin` is the
  provenance. An answer delivered (`started`/`queued`) removes the row; one
  recorded for a closed chat (`closed`) or that could not be delivered
  (`failed`) is a rank 8 row for `RECORDED_WINDOW` (7 days, until B3's
  dismiss): title `Your decision for chat <id>`, blocks `recorded; the asking
  chat is closed` / `recorded; it could not reach the asking chat`. Rows
  keep A1's shape, so the phone (C3) can read them generically.
- **The answer.** `POST /inbox/decisions/<id>` (owner-only, the HUD
  listener, as every `/inbox` path): exactly one of `{choice: index}` and
  `{text}` (non-empty, at most 2000 characters, stripped). 400 for a
  malformed id or body or a query, 404 for an unknown decision, 409 for one
  already answered. `POST /inbox/dec:<id>/answer` is the same function (an
  unknown one is that route's 409). It records first (`DecisionStore.settle`,
  under the lock: a second answer is 409, never overwritten), then delivers
  (§5), then notes the delivery and publishes `decision_answered` (ids
  only). The reply is `{ok, decision_id, delivery, note}`.
- **Delivery (§5).** The asking chat, unless it is gone or archived, it or
  its project (`closed`: nothing is sent; the answer stays recorded), gets
  an owner message (`origin="owner", via="inbox", desk=False`) through
  `Daemon.deliver_queued`: a turn of its own when the chat is idle and
  nothing of the owner's waits (`started`), else it waits in a **queue of
  its own** (`_inbox_queues`, `queued`), which `_drain` runs after the
  owner's queue and before other chats' messages. So an answer waiting
  never takes one of the owner's `QUEUE_MAX` slots and never turns the
  owner's next message from a steer into a wait (review of PR #309). It
  **never steers**: never `_steer`, never into a running turn, never read
  as the answer to a card or question it waits on. A closed session is
  resumed to take it, as a HUD message would be (a deviation from §5's "if
  the asking thread has a live session", noted in the PR).
  - **The words** (`inbox.decision_message`, reframed on the review): code's
    first line, "The owner answered your decision <id>: they picked option
    <n> of the options you gave, "<option, JSON-quoted>"." or "… in their own
    words: <the owner's text>", then a last line "(Your question, quoted;
    your words, not the owner's: "<the question as one JSON-quoted line,
    fence phrases annotated>")". Look-alike quotes (Unicode Pi/Pf, fullwidth
    `＂`, `〃`, primes, guillemets and the like) become `'` first, so nothing
    the agent wrote can start a line or close its quote, to a parser or to
    the eye, and none of it reads as the owner's.
  - **No desk, no Bypass.** `via="inbox"` (a new `VIAS` value) is never at
    the desk (`desk.desk_message` requires "hud"): the turn holds no desk
    tool (fast path F-1..F-7) and cannot itself post a decision or send to a
    chat. Claude's Bypass switch does not count in it (`owner_turn` excludes
    `via="inbox"`); Codex has no Bypass.
  - **Lost answers say so.** The queued item carries the decision's id. When
    it is dropped (the owner's Stop, a close, a thread that cannot resume,
    shutdown, and a thread whose record is gone when `_drain` tries to
    resume it) `_drop_waiting` hands it to `Inbox.answer_dropped`, which
    marks the decision `failed` and publishes `decision_answered`, so the
    rank-8 "recorded; it could not reach the asking chat" row shows; at start
    `Inbox.fail_lost_answers` does the same for any left `queued`, or
    answered with no delivery noted (a crash between the two), by a previous
    run. The route never overwrites such a `failed`.
  - **Counted where the queue is shown.** The transcript route reads the
    answers' queue too (a waiting answer reads "queued" after a reload, not
    "not sent"), and the relay counts it ahead of other chats' messages
    (`ChatRelay._ahead`: `snapshot`'s `queued`, a send's position, a wait's
    "position").
  - **Where it shows.** The transcript shows it as the owner's message "from
    the inbox"; chat reads (other chats, the phone) mark it "via the inbox
    (not typed in this chat)"; the Discord mirror writes "You (inbox): <the
    answer line>" and, on a line of its own, "↳ the chat's own question (its
    words, not yours): <quoted>".
- **Events.** The post publishes `decision_posted` and the answer
  `decision_answered`, both `{decision_id}` (plus `delivery`) and never text;
  `Inbox.observe` turns each into `inbox_changed` `{ids: ["dec:<id>"],
  reason}`.
- **The phone (C3).** `remote_api.DETAIL_FIELDS["decision"]` is
  `(decision_id, question, options, default, why, answer, delivery)`, the
  projection's own keys, every string through the phone's scrub; the phone
  shows the question, options (the suggestion marked) and why, and the
  answer once recorded. **Since P-4 (C4) the phone answers an open
  decision** (`POST /m/api/inbox/decisions/<id>`, the same function, with
  `via="phone"`: see "Answering from the phone").
- **No tool reaches the answer side.** `tests/v2/archive_check.py` now scans
  `decisions.py` and the tool's module for every inbox route and module
  needle; `tests/v2/owner_post_check.py` keeps `settle`, `delivered`,
  `answer_decision`, `deliver_queued`, the broker and the steer out of the
  tool and `OwnerPosts`.

Tests: `tests/v2/owner_post_check.py` (binding per caller kind through the
real bridge, hook and Codex admission; the service's rows; caps; field caps;
scrub with planted tokens; provenance; the store's modes, atomicity, FIFO
and symlink refusals and the protected tree, its DENYs and its card names;
the projection; the route; the delivery's `via`, desk, queueing, its own
queue, drops and closed-chat cases; the framing on every surface; partial
and foreign files; the display rule; the reviewers' races; `inbox_changed`
without text), plus `inbox_check`, `archive_check`, the catalog pin in
`mcp_session_check`, the Bypass case in `claude_provider_check` and the
decision row in `remote_api_check`.

**Checks (inbox B2, IN-6, 2026-10-27).** A live check is a list of steps
only the owner can run, each a `do` and an `expect`. Its result is the
owner's alone. Three modules: `jarvis/v2/checks.py` (new, SELF_PROTECTED:
the store, the field caps, the deployed test and the runbook import),
`decisions.OwnerPosts` (the post) and `inbox.py` (rows and routes).

- **The post.** `owner_post(kind="check", title, steps, prs=None)`. The tool
  keeps one name. Every argument but `kind` is now optional in its schema,
  because each kind has its own fields. The service refuses a kind that is
  missing its own fields or is given the other kind's. Decision posting is
  unchanged: the same checks, caps, words and results.
- **Same binding as a decision.** The tool checks `at_desk()` and depth 0.
  Then `OwnerPosts._refuse_caller` re-checks under the daemon lock. Only a
  Claude or Codex owner chat, in a turn the owner started at the HUD, posts.
  Each field is scrubbed, cleaned with `display_text`, then scrubbed again.
  A scrub that fails refuses the post. `created_by` is the decision's
  provenance line.
- **Caps, refused never cut.** Title 200 characters, 1 to 30 steps, `do` and
  `expect` 1000 characters each (both required for an agent's check), at
  most 10 PR numbers. 5 open checks per chat, 10 per chat and 20 in all in
  any rolling 24 hours, counted from the files. These are separate from the
  decisions' counts, so an agent can post up to 20 of each a day. The caps
  are checked again on the cleaned text (review of PR #321, MED 1): NFC can
  lengthen text, and U+0958 becomes two characters.
- **A step is text only.** Nothing in a check runs anything. The HUD's
  "Copy" puts a step's `do` on the clipboard and nothing more.
- **The store.** `<data root>/inbox/checks/<id>.json` (`c` + 12 hex). The
  fields are §3.2's (`id, title, prs, deployed_sha, steps, results,
  created_by, imported_from`) plus `kind`, `thread_id`, `task_id`,
  `project_id` and `created_at`. `CheckStore` subclasses `DecisionStore` and
  uses the same I/O: folder fds opened `O_DIRECTORY | O_NOFOLLOW`, files read
  `O_NOFOLLOW | O_NONBLOCK` and refused unless regular, an `O_EXCL` temp file
  fsynced then `os.replace`d, 0700 folders and 0600 files.
  **Every file the store writes reads back** (review of PR #321, MED 1).
  Before, a check that grew under NFC wrote a file too large to read. The
  agent was told "Posted", but the check was never counted, capped or
  pruned. Now:
  - `checks.valid` holds the field caps.
  - A new check may encode to at most 128 KiB (`CREATE_BYTES_MAX`).
  - No write may exceed `_WRITE_MAX`, which is half of what a read accepts
    (`_FILE_MAX`, 512 KiB). So the widest notes on every step still fit.
  - The decision store got the same `_WRITE_MAX` guard (32 KiB of 64 KiB). A
    worst-case decision is about 13 KB.

  `checks.valid` accepts exactly the record's shape; any other file is
  never a row and never marked. At most 500 files; the oldest
  done checks go first. The `checks` folder is named in
  `_protected_state_paths`, and the whole `inbox/` tree was already
  protected (`files._inbox_tree`, `protected_state.owner_trees`).
- **Results are the owner's alone.** `POST /inbox/checks/<id>/steps/<n>`
  (owner-only, the HUD listener) takes `{verdict: pass|fail|skip, note}`.
  The note is optional, at most 1000 characters, scrubbed and cleaned; a
  scrub that fails records nothing. A step marked again is replaced (the
  owner's own correction). Nothing ever clears a result. When every step has
  a verdict, the check is done. The reply is `{ok, check_id, step, done}`.
  400 for a malformed id, step, body or query, 404 for an unknown check or
  step. `POST /inbox/chk:<id>/answer` is a 400 that names the step route.
- **No other way to write a result.** `inbox.mark_check_step` is the only
  caller of `CheckStore.mark`. `tests/v2/live_checks_check.py` greps all of
  `jarvis/` for the route, the function and the event. Only `inbox.py` and
  `checks.py` may name them. It also sweeps every session tool with a check
  id in every argument and sees no mark. `tests/v2/archive_check.py` keeps
  every tool, MCP catalog entry, session tool, Discord, the `jarvis` command,
  `owner_ops` and `mcp_session` from naming them, and refuses both new
  routes to every client but the HUD.
- **Deployed gating.** A check that names PRs can be marked only when each
  one is in the build that runs (`checks.Deployment`). The test is local git
  only, never `gh` and never the network:
  1. A PR's merge commit is a **first-parent** merge commit on the running
     commit's line whose **subject** is exactly "Merge pull request #<n>
     from <branch>" (`git log --first-parent --merges --format=%H%x09%s
     <commit>`). This was a review finding on PR #321 (MED 2): a message
     grep had counted a PR title or body that named another PR, a merge
     carried inside a branch, and a revert.
  2. The running commit is the one the daemon started from. `main()`
     captures it once with `Deployment.attach_git()`.
  3. If the PR changed anything under `hud/` (`git diff --quiet <m>^1 <m> --
     hud`), the same merge commit must also be on the installed HUD's line
     (its manifest's `git_head`). The manifest must also say the build was
     not made from uncommitted `hud/` changes (`dirty` false).
  4. If more than one commit matches, the answer is "not confirmed".
  5. A PR that is not on the running line but is on the checkout's HEAD
     line is "not deployed yet" (merged, not running).

  Anything git cannot say is "not confirmed deployed": a squash merge, a PR
  not fetched, the dev HUD, a dirty HUD build, a git failure, or a daemon
  that was never attached (every test and embedded daemon). Then the row
  says so and the route answers 409 with the same line. A mark records the
  confirmed daemon commit as `deployed_sha`. A check with no PRs is not
  gated.

  **Bounded git (review LOW 2).** The map of PRs to merge commits is read
  once per line and kept by the line's SHA. The checkout's HEAD map and the
  HUD manifest are kept for 60 s, and whether a merge touched `hud/` is
  kept per commit. One inbox read may run git at most `GIT_BUDGET` (8)
  times; past that a check reads "not confirmed" until the next read.
  `answer()` finds its row with `rows(daemon, with_checks=False)`, so
  answering an approval, a question or a proposal never reads deploy state.
  The delta review of PR #321 tightened this further:
  - A git failure (a line read, or a hud/ diff) is kept for the same 60 s
    per SHA, so a call that keeps failing stops spending each read's budget.
  - Every git process the test starts has a 5 s timeout (`GIT_TIMEOUT_S`),
    including the two config checks `hud_install._git` runs first.
    `hud_install.config_risk` and `gitops._risky_config` take an optional
    `timeout`; their default (60 s) is unchanged. So one read is bounded by
    about 8 runs x 3 processes x 5 s.
  - The budget is spent round-robin: each read starts at the check where
    the last one ran out, so older checks never starve newer ones.
  - The mark route asks with `fresh=True`. It reads the HUD manifest (head
    and `dirty`) and the checkout's HEAD again, rather than trusting the
    rows' 60-second copies. So a HUD rolled back, or rebuilt dirty, inside
    that window refuses the mark. The running commit is the one captured at
    start; what is kept by SHA cannot change.
  - A subject is matched as git printed it, with only the line ending taken
    off. A subject that ends in `\r` or a space never counts.
- **Rows.** An open check is a `check` row at rank 7. Within rank 7, checks
  whose PRs all have a merge commit come first, then oldest first. The title
  is `Live check <id> from chat <id>: <k> of <n> steps marked` ("from you
  (imported)" for an import). The blocks line is the deployed test's line,
  or "nothing waits; your marks are the receipt". A done check is a rank 8
  row for 7 days (`DONE_WINDOW`): `Live check <id> done: <p> passed, <f>
  failed, <s> skipped`. Agent text (the title and steps) is only in
  `detail`, as untrusted text. `detail` also holds the results, `done`,
  `deploy: {state, text, markable}`, `merged`, `deployed_sha`, `created_at`
  and `imported_from`. An archived project's check is still shown, without
  its `project_id`. One bad file never hides the others.
- **The receipt** is exported in the HUD: "Copy receipt (Markdown)" builds it
  from the row (`lib/inbox` `checkReceipt`). Every agent or runbook string is
  escaped, so it stays one Markdown line.
- **Import (§6, owner question 5).** `POST /inbox/checks/import` (owner-only)
  takes `{path}`. The path must be absolute and NFC, with no control, format
  or filler characters, no `.` or `..`, and a `.md` file directly inside
  `<root>/docs/plans/` of a project that is not archived. On disk, `docs/`,
  `plans/` and the file must not be symlinks. The file's realpath must be
  exactly that path under the root's realpath. A credential file
  (`secrets.is_credential`, so `.env.md` too) is refused. The read then
  walks folder descriptors from the project root: `docs`, then `plans`, each
  `O_DIRECTORY | O_NOFOLLOW`. It opens the file relative to `plans` with
  `O_NOFOLLOW | O_NONBLOCK`. So a folder swapped for a symlink after the
  path was judged is refused, on the preview and on the save alike (review
  of PR #321, LOW 1). The file must be regular and at most 512 KiB.
  `checks.parse_runbook` makes one draft per `##` heading. No model is
  involved. A step is a numbered line with the lines that continue it. Its
  `expect` is the text after "Expect:" (bold or not); later "Expect:"s join
  with "Then:". A `**Part X**` line prefixes the steps after it. PR numbers
  come from `#<n>` in the heading. Each draft is scrubbed and judged
  (`judge_draft`). The reply is `{path, project_id, digest, origin, drafts:
  [{index, title, prs, steps, savable, reason}]}`, and nothing is saved.
  `{path, digest, save: <index>}` is the owner's confirm of one draft. The
  file is read again and must have the same SHA-256 (409 if it changed). A
  draft with no steps, over a cap, or not checkable for credentials is 400.
  One already imported from that file under that title is 409. A saved
  draft is a check with `thread_id` null, `imported_from` the path, and
  `created_by` "Imported by you from docs/plans/<name>". Imports count only
  towards the 500-file bound, not the agents' caps.
  `docs/plans/2026-10-16-owner-live-checks.md` gives 10 drafts. VEX view has
  no numbered steps, so it cannot be saved. The questions-for-owner import
  of decisions is not built.
- **Events.** `check_posted`, `check_imported` and `check_marked` carry
  `{check_id}` (and `step`, `done`), never text. `Inbox.observe` turns each
  into `inbox_changed {ids: ["chk:<id>"], reason}`.
- **The phone.** `remote_api.DETAIL_FIELDS["check"]` is `(check_id, title,
  prs, steps, results, done, deploy)`, every string through the phone's
  scrub. The phone shows the row read-only and cannot mark a step.

Tests: `tests/v2/live_checks_check.py` (binding, caps, scrub, the store's
modes, symlinks, FIFO and shapes, the deployed test on a fake git, the
route, the grep and the tool sweep, rows and order, the phone's projection,
the import's paths and drafts), `owner_post_check` (the schema), and
`archive_check` (the routes and the source rule).

**PRs (inbox B3, IN-4, 2026-10-27).** Open pull requests, as rank-5 rows.
The module is `jarvis/v2/inbox_prs.py` (SELF_PROTECTED). The owner's answer to
design §10 question 4, on 2026-10-27: **every open, non-draft PR in each
project's GitHub repository**, not only PRs that Jarvis pushed.

- **The poll.** `PrSource` runs on a thread of its own in the daemon. It
  polls every 5 minutes (`POLL_INTERVAL_S`). A task that moves into done,
  failed or blocked also polls. `Inbox.observe` hands each
  `task_status_changed` to `PrSource.task_changed`. That event carries only
  the new state and fires on plain saves too, so `task_changed` remembers
  each task's last state and calls `poke` (an event) only on a change into
  one of those three. The poll waits `DEBOUNCE_S` (10 s) after a poke, and
  never runs sooner than `MIN_GAP_S` (30 s) after the last one. Nothing runs until
  `attach_system()`, which only daemon2's `main()` calls, as with
  `remote_secure`. So an embedded or test daemon never runs `gh` and has no
  PR rows. `Daemon.start()` only starts the thread; `Daemon.stop()` stops
  it and kills a running `gh`. A stop during a `gh` run publishes nothing
  (no false "timeout" row). `start()` never starts a second loop while an
  old one is still alive.
- **Which repositories.** Each project that is not archived. Its root's git
  metadata is validated by reading files only (`codex_config.git_dirs`).
  Then `remote.origin.url` is read by one hardened `git config --file
  <private copy> --no-includes`, run in an empty home and never in the
  repository (`chat_git._origin_url`). `chat_git.github_repo` keeps
  github.com `OWNER/REPO` only. Anything else is skipped. No hook, include or
  executable config runs. Two projects on one repository read it once; such
  a row has no `project_id`. At most 50 repositories are read
  (`REPOS_MAX`). The rest are named in the `prx:list` row, never dropped
  silently.
- **The call.** `gh pr list --repo OWNER/REPO --state open --search
  draft:false --limit 100 --json
  number,title,headRefName,headRefOid,isDraft,url,statusCheckRollup,mergeStateStatus,createdAt,author,isCrossRepository`.
  The query leaves drafts out, so they never use up the limit.
  An exact argv, no shell, in `/`. The environment is
  `chat_git._owner_env()`: the owner's HOME and PATH, no GIT_*, no tokens
  from the daemon's environment, prompts and pagers off. Stderr is never
  read, since it could carry a token. The run has a 30 s deadline
  (`GH_TIMEOUT_S`) and a 2 MiB output bound (`OUTPUT_CAP`), in its own
  process group, killed whole on a timeout, an overflow or a stop.
- **Validation.** The answer must be a JSON list of at most 100 PRs, each
  with the expected types, and no number twice. Anything else fails the
  repository ("malformed"). Drafts are dropped again. A branch is shown only
  when `chat_git.valid_branch` accepts it. The author's login is shown only
  when it has GitHub's login shape (or gh's `app/<name>`). The checks summary is `passing`,
  `failing`, `pending` or `none`; a value it does not know counts as
  pending, never passing.
- **Rows.** Id `pr:OWNER/REPO#N`, kind `pr`, rank 5. The title is built in
  code from the number and state: "PR #N is ready to merge", "has merge
  conflicts", "has failing checks", "is behind its base branch", "waits on
  its checks", "is held by its branch rules" or "waits on you". `blocks` is
  `OWNER/REPO · by <login> · branch <name> · checks <summary>`. A PR from a
  fork (`isCrossRepository`) says `from a fork (fork:<branch>)` instead, so
  a stranger's `main` never reads as this repository's branch. `asked_at` is the PR's
  creation time, so older PRs sort first. The PR's own title is untrusted.
  It gets B1's treatment: `chat_reads.scrub`, `decisions.display_text`, the
  scrub again, one line, at most 300 characters. It is only in
  `detail.title`. `detail.url` is the URL only when it is exactly
  `https://github.com/<OWNER>/<REPO>/pull/<N>` for the repository asked and
  the PR's own number (owner and repo compared without case); else None.
- **Failure is visible.** A repository whose list fails has no PR rows. The
  last good list is dropped, not kept stale, so a PR merged meanwhile never
  shows as waiting. One row says so: id `prx:list`, kind `pr_unavailable`,
  rank 5, "PR list unavailable for OWNER/REPO" (or "for N repositories").
  Its `detail.repos` lists each repository, a reason in the module's own
  words (`REASONS`: not installed, not logged in (exit 4), no answer in
  time, too large, failed, could not be read) and when it last read well.
  So an empty list never reads as "nothing waiting". The same row names a
  repository whose answer held exactly 100 PRs (it may have more) and one
  past the 50-repository cap that was not read. With no failure among them
  it reads "PR list incomplete for …"; it is counted either way.
- **Freshness.** Each poll replaces the snapshot. A merged or closed PR is
  gone after the next poll. The poll publishes `inbox_changed` `{ids,
  reason: "pr_poll"}` only when a row came, went or changed, with no text.
- **Never in the way.** `inbox.rows` reads the last snapshot under the
  source's own lock, which no `gh` call holds. A slow `gh` never stalls
  `GET /inbox`, the daemon's lock or `Daemon.start()`.
- **No new authority.** `POST /inbox/pr:…/answer` and `prx:…` are 400, as a
  missed row is. Merging stays where it was: the owner's own `gh`, or an
  agent's card under the merge gate. The list is reached only through the
  owner-only `GET /inbox`. Nothing new is in tools, MCP or the API
  listener; `tests/v2/archive_check.py` also refuses `inbox_prs` imports,
  `PrSource` and `.prs.rows`/`.prs.poll` in every tool module. The phone
  shows a PR row's title and line only (no detail field of it is in
  `remote_api.DETAIL_FIELDS`).

Tests: `tests/v2/inbox_prs_check.py` (a fake `gh` script on disk, no
network), `hud/src/components/InboxView.prs.test.ts` and the `prs` section
of `tests/face/hud_v2_inbox_check.py`.

**Marks (inbox B4, IN-7, 2026-10-28).** The owner's snoozes and
dismissals. `jarvis/v2/inbox_marks.py` (new, SELF_PROTECTED: the store and
the rule) and `inbox.py` (the route and where marks apply).

- **Rank 5 to 8 only (IN-8), twice.** `inbox_marks.markable(row)`: rank 5
  to 8, and never "PR list unavailable" (#320's `prx:list`, kind
  `pr_unavailable`), refused by kind (`UNMARKABLE_KINDS`) and by id prefix
  (`UNMARKABLE_PREFIXES`). Hiding that row would turn "unavailable" into
  "nothing waiting". `POST /inbox/<id>/mark` (owner-only, the HUD listener;
  the API listener and a non-owner page get 403) finds the row in a fresh
  projection, marks ignored, and refuses (409) a row that is not markable
  **now**. The client never sends a rank (an extra field is a 400). The store
  refuses one too. Then `rows()` honours a mark only while the row's
  current rank is 5 or more, whatever the file says. So a forged mark on a
  live approval hides nothing, and a `failed` task (rank 8) dismissed that
  later reads rank 4 shows again.
- **The body.** Exactly one of `{snooze_until: <ISO time with its zone, in
  the future by the daemon's clock, at most 30 days ahead>}`, `{dismiss:
  true}` and `{clear: true}` (the Undo; it needs no row, since it only ever
  shows one). 400 for a malformed id, body or query, 409 for a row that is
  gone or not markable. The reply is `{ok, id, hidden}` (`cleared` on an
  Undo). It publishes `inbox_changed {ids: [<id>], reason: "inbox_marked"}`,
  the id only.
- **A mark is for the row as it was (`material_key`).** Each mark keeps a
  16-hex digest of the row's kind and rank plus, per kind: a PR's `head`
  (new commits bring it back), a task's state and last update, a decision's
  delivery, a check's done flag (a new step result keeps it hidden), nothing
  more for a "denied while away" record, and the code-built title and
  blocks for any other kind. A digest that no longer matches shows the row
  again. Cheap and honest: it reads only what the projection already holds.
- **Applying.** `rows(daemon, marks=...)`: "hide" (the default: `GET
  /inbox`, the phone's `/m/api/inbox`) leaves hidden rows out; "flag"
  (`GET /inbox?all=1`) keeps them with `hidden: {snoozed_until,
  dismissed_at}` and gives every row `markable`; "ignore" is the projection
  alone (`answer`, `mark_row`). Hidden rows are never in the HUD's count or
  badge (`lib/inbox` `waitingCount`). A store that raises hides nothing.
- **A snooze that ends comes back on its own** (review of #322, MED).
  Nothing else would re-read: the HUD and the phone read on `inbox_changed`,
  reconnect and mount only. `Inbox.arm_snoozes` keeps one `threading.Timer`
  (a daemon thread, no polling) for the earliest snooze still running, by the
  store's clock, plus 0.5 s (`SNOOZE_SLACK_S`), and never waits more than an
  hour at once (`SNOOZE_RECHECK_S`, so a clock that jumped is caught). When
  it fires it publishes `inbox_changed {ids: <the rows whose snooze ended>,
  reason: "snooze_ended"}` and re-arms; fired early, it only re-arms. It is
  armed in `Daemon.start()` (`start_snoozes`, so a snooze from a previous
  run counts), re-armed after every mark write (a snooze, a dismissal, an
  Undo), and cancelled in `Daemon.stop()` (`stop_snoozes`; a late fire then
  neither publishes nor re-arms). As a backup the HUD's store schedules one
  read just after the earliest `snoozed_until` it holds (`state/inbox.ts`,
  at most an hour at a time, replaced by every read). The phone holds no
  hidden rows, so it relies on the event, which its stream forwards.
- **The 30-day bound holds in the store too.** `MarksStore.mark` refuses a
  snooze outside (now, now + 30 days], and on read an entry whose
  `snoozed_until` is more than 30 days after its `at` makes the file not
  ours (no mark applies; every row shows).
- **The file.** `<data root>/inbox/marks.json`: `{row_id: {snoozed_until,
  dismissed_at, key, at}}` (design §3.2's two fields, plus the digest and
  when it was marked), exactly one of the first two set. The decisions
  store's I/O: `inbox/` opened `O_DIRECTORY | O_NOFOLLOW` (0700,
  tightened), the file read `O_NOFOLLOW | O_NONBLOCK`, refused unless
  regular and at most 512 KiB, written as a fresh `O_EXCL` 0600 temp file,
  fsynced, then `os.replace`d (a symlink there is replaced, never written
  through; a symlinked `inbox/` refuses the write, a 500). **Fails closed:**
  a file that is missing, a symlink, a FIFO, too large, not JSON, or with
  any entry not exactly that shape applies no mark at all, so every row
  shows; a mark written over it starts from empty. **Bounded:** at most
  `MARKS_MAX` (500) entries and 256 KiB per write. Each write first drops
  expired snoozes and marks whose row is gone, changed or no longer
  markable, then the oldest. A dropped mark only shows a row. The file is
  named in `_protected_state_paths` and lies in the protected `inbox/` tree,
  so v1 write tools refuse it, v2 layer 1 refuses a file tool or shell write
  to it, and v1's shell gate asks or denies.
- **The phone (default kept).** It shows what `GET /inbox` shows: hidden
  rows are left out, through the same `rows()` default. It cannot mark:
  there is no phone route (`remote_api._match` and `remote_answer._match`
  have none). A phone mark would be its own owner-verified route, left for
  the owner.
- **No tool path.** `archive_check.test_no_tool_or_cli_marks_an_inbox_row`
  keeps every tool, MCP catalog entry, session tool, Discord, the `jarvis`
  command, `owner_ops`, `mcp_session` and the post service from naming the
  route, `mark_row`, `MarksStore`, the file or `inbox_marked`
  (`files._protected_state_paths` set aside, as for the phone's files). The
  marks test greps all of `jarvis/` and sweeps every session tool with a
  row id in every argument.
- **The HUD judges again.** `lib/inbox` keeps `hidden` only on a row of rank
  5 to 8 that is not `pr_unavailable` or `prx:` (`parseRow`), and
  `isHidden`, which `shownRows`, `hiddenRows` and `waitingCount` use, checks
  the same again, so a served `hidden` on a live row never hides or uncounts
  it.
- **Known, inherited, not this package:** `tar -xf x.tar -C <data root>` is
  not refused at v2's layer 1 (an extraction into an ancestor of the
  protected tree is not judged as a write to it); v1 asks. It predates B4.
- **With the PR rows (inbox B3, #320).** A `pr` row is markable at rank 5
  like any other; its material key is B3's `detail.head`, so new commits
  bring a dismissed PR back. A PR that leaves the list (merged, closed, or a
  failed poll) and returns shows again too: the next mark write prunes a
  mark whose row was gone. `prx:list` (kind `pr_unavailable`) is refused by
  kind and by prefix, against the real row.

Tests: `tests/v2/inbox_marks_check.py` (every rank 1 to 4 kind refused live;
5 to 8 accepted and undone; the rank judged at mark time; a forged mark on
a live row; a row rising to rank 4; `prx` by kind and prefix on synthetic
rows; material change; `?all=1`; snooze expiry on a clock set relative to
now; malformed, oversized, symlinked and FIFO files; modes; the bound and
pruning; 403 for the API listener; `inbox_changed`; the phone; the grep and
the session-tool sweep; v2 layer 1 and v1 for shell writes), `inbox_check`
(`?all=1` is a query now), `archive_check`, and the HUD's.

## The phone's remote listener (phone C1 / P-1, 2026-10-25)

`jarvis/v2/remote_listener.py` (SELF_PROTECTED) is v2's fourth listener
(design `docs/plans/2026-10-24-phone-surface-design.md`, PH-1):
`config.REMOTE_PORT` (8408, `JARVIS_REMOTE_PORT`) on 127.0.0.1 only, reached
from the tailnet through `tailscale serve` (PH-2). **It binds only when
`config.remote_host()` names a host** (`JARVIS_REMOTE_HOST`, a well-formed
lowercase `<machine>.<tailnet>.ts.net`; anything else is None, never
repaired). No name, no fourth port: the default, so a deploy changes nothing
until the owner sets it. It binds after the three listeners that are the
single-instance lock, and a failed bind is one warning line and no listener;
the daemon starts either way (`Daemon.remote_port` is the bound port or
None).

Its handler is a separate class with its own route table; it never reaches
`hud_api.route`, `inbox.route`, `bridge.route` or the preview routes. It
serves `GET /m/` (the build's `mobile.html`, the second Vite entry, PH-9),
`/m/manifest.webmanifest` (built in the module: scope and start `/m/`,
standalone, the orb icons), `/m/icons/<orb icon>`, and `/m/assets/<file>`
only for the files the built page names and Vite's build manifest
(`build-manifest.json`, `build.manifest` in `hud/vite.config.ts`) lists for
the `mobile.html` entry and its imports, never the rest of the HUD bundle.
`/m/pair` is the same page (where a pairing QR code lands). `/m/api/*` is
`remote_auth.api_route` since P-2 (next section): 401 `{"error":
"unauthorized"}` without a session, except the four pairing and login
routes; everything else 404. Every request body is read before any answer,
a refusal's included (64 KiB cap, 413 above it after draining up to 1 MiB of
wire bytes; chunked bodies decoded the same way, their size lines, data and
trailers all counted against that 1 MiB and trailers capped at 8 KiB; the
whole body within 10 s on the monotonic clock, else 408), so `tailscale
serve`'s Go proxy never turns an answer over an unread body into a 502 (PR
#304's follow-up). Files come from `hud/dist` under
`JARVIS_HUD_DEV=1`, otherwise from the installed builds, verified on each
read (`hud_install.read`).

Every request: `Host` exactly the configured name, once, else 400; any method
but GET/HEAD needs exactly one `Origin: https://<name>`, else 403. Every
response, a malformed request's included: `remote_listener.REMOTE_POLICY`
(`default-src 'none'`, `script-src`/`style-src`/`img-src`/`connect-src`/
`manifest-src 'self'`, no `'unsafe-inline'`, `frame-ancestors 'none'`),
`X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, COOP and
CORP `same-origin`, `Cache-Control: no-store` (hashed `/m/assets/` files are
`immutable`). `projects.is_owner` refuses any server marked `remote_only`
before it compares ports. The daemon's own handler answers any `/m/...` path
with 404 on the API, HUD and workshop listeners. 8408 joined
`config.DEFAULT_CONTROL_PORTS`, `control_ports()` (with `REMOTE_PORT`) and
`CONTROL_PORTS` in `hud/src/lib/devopen.ts`, so the Browser gate and
auto-open refuse it. The built phone page names its files under `/m/`
(`experimental.renderBuiltUrl` for `mobile.html` only); phone CSS must not
use `url()`, which Vite would point at the HUD's `/assets/`
(`hud/src/mobile/mobile.test.ts`, and the real-build case in the suite).
`tests/v2/remote_listener_check.py`.

## The phone's passkeys, pairing and sessions (phone C2 / P-2, 2026-10-25)

`jarvis/v2/remote_auth.py` (SELF_PROTECTED) decides who the owner is off
the PC (design PH-3..PH-6, §4). **The passkey proves the owner; the tailnet
and Tailscale's identity headers do not** (they are recorded per session as
advisory text and never read for a decision: a local process can send them).

- **Pairing starts at the desk.** Owner-only HUD routes on the HUD
  listener (`remote_auth.hud_route`, `projects.owner_only` first on every
  `/phone` path): `GET /phone` (window state, the active passkeys and their
  sessions, the remote listener's `{port, bound, host_configured}`), `POST
  /phone/pair` (opens or replaces the window: `{url, expires_in}`), `POST
  /phone/pair/cancel`, `POST /phone/passkeys/<k-…>/revoke`, `POST
  /phone/sessions/<s-…>/end`. The window is 5 minutes and one successful
  registration ("single-use" means that: a failed attempt may be retried
  with the same code while the window is open; the first registration that
  lands closes it); its code is 256 random bits, only in the daemon's memory,
  compared in constant time, and travels in the URL fragment
  (`https://<host>/m/pair#<code>`), which no browser sends. A replaced window's
  code and registration challenges die with it. No window without a
  configured host and a bound listener (409), nor without `webauthn`
  installed (503, "uv pip install -e ."). At most 3 active passkeys: refused
  at the window, the options and the verify (409).
- **Registration** (`POST /m/api/pair/options`, `/pair/verify`, each with the
  code): `rp_id` = the remote host, origin `https://<host>`,
  `residentKey: required`, `userVerification: required`, verified with
  `require_user_verification=True` and the result's UV flag checked again.
  **A registration never makes a session** (with `none` attestation it proves
  nothing about the private key): the phone signs in next.
- **Login** (`POST /m/api/login/options`, `/login/verify`): a 32-byte
  server-side challenge, single-use (taken out before verifying, so a failed
  attempt spends it), 60 s on the monotonic clock, pruned, and capped per
  kind (32 login, 8 registration and those only while a window is open); a
  full pool answers 429 rather than evicting a pending ceremony, so a flood
  of unauthenticated `login/options` cannot evict a phone's sign-in or
  pairing. Options name no credential (discoverable passkeys). Unknown,
  spent, expired or registration challenge: 409. Unknown or revoked
  credential, another user handle, wrong origin or rpId, `crossOrigin: true`
  or a `topOrigin` in the client data (at registration too), UV clear, a bad
  signature, or PH-4's counter rule (0 while the stored counter is 0;
  otherwise strictly greater): 401 with no detail.
- **Sessions.** `__Host-jarvis_m=<256-bit token>; Path=/; Max-Age=604800;
  Secure; HttpOnly; SameSite=Strict`. Only the token's SHA-256 is stored.
  12 h idle, 7 days absolute, on the wall clock. Each records its passkey,
  the cleaned and capped user agent, created, last-seen (written at most once
  a minute) and the advisory headers; one dated more than 5 minutes in the
  future is refused, and past 32 sessions the least recently seen goes.
  `POST /m/api/logout` ends it and clears the cookie; End ends one. **Revoke**
  ends every session of the passkey and refuses its credential in memory at
  once, and is durable once the passkeys file is written. If that write
  fails the revoke answers 503, the HUD shows the passkey as "revoked, not
  saved" (`GET /phone` `state: "unsaved"` and `unsaved`), every later call
  (`status`, a session check, login, pairing) retries the write, and a repeat
  Revoke retries it and answers 200 once it lands (a repeat of a saved one
  is 200 too). `Daemon.stop()` makes one last try and logs an ERROR naming
  the passkey if it still cannot be written. A restart before any write
  lands loses the revocation (the file still has the passkey active): the
  owner was told (503, "revoked, not saved"); accepted and documented.
- **`is_remote_owner(handler)` / `owner_status`** is the only owner test on
  the remote listener (`projects.is_owner` stays false there): this daemon's
  bound remote server (`RemoteAuth.server`, set by `remote_listener.bind`),
  exactly the configured Host (400), exactly `Origin: https://<host>` on any
  method but GET/HEAD (403), and a live session of an active passkey (401).
  `GET /m/api/me` and `POST /m/api/logout` are this module's own session
  routes; since P-3 every other `/m/api` path is `remote_api.route`'s (next
  section), reached only past this check, and 401 without a session.
  `RemoteAuth.session_live(token hash)` is the event stream's check: the
  same tests as `session_for` without touching last-seen.
- **Storage.** `<data root>/remote/` 0700, `passkeys.json` and
  `sessions.json` 0600, atomic (`stores._write_bytes`), read with
  `O_NOFOLLOW`. Both are protected state (`files._protected_state_paths`): no
  write tool, shell write or grant reaches them, and a writer on the
  `remote/` folder itself (`rm -rf`, `mv` it away, `mv`/`cp -r` a folder into
  its place), or inline source that spells it in full beside a call that
  removes or replaces a folder (`shutil.rmtree`, `os.rename`, `fs.rmSync`,
  …), is a certain touch of its files (`protected_state.phone_dirs`:
  refused at v2's layer 1, asked by v1, as the files are). Still asked, not
  refused: the data root above it, a path assembled at run time, and `git
  clean -fdx` run inside the data folder (it depends on the working folder
  and on that folder being a repository, which a line does not show). An unreadable passkeys file is never
  overwritten (pairing and login 503, the HUD says so) and is read again on
  the next call; an unreadable sessions file signs every phone out. `webauthn` is imported only
  when used, so a daemon without it starts and answers 503 there.
- **The agent path** (design §4). `rules.remote_reading`, called from
  `rules._decide` on the raw line like the Secret Service reading, DENYs a
  line that holds, in any spelling and whatever program runs it (an option
  value such as `pip --index-url` or `git -c http.proxy=`, a variable's
  value, a here-doc, `-c` source, a substitution, a function, an alias,
  `eval`): a loopback host joined to the remote port (`localhost:8408`,
  `http://127.0.0.1:8408/`, `[::1]:8408`, `9000:localhost:8408`, or the
  loopback host joined to an expansion when the line names the port); a
  socket path (`/dev/tcp/…/8408`, gawk's `/inet/tcp/…/8408`); the port, a
  loopback spelling and a client or connecting library's word apart (`nc
  127.1 8408`, `TCPSocket.new("127.0.0.1", 8408)`); or `/m/api` with a
  loopback spelling or a `ts.net` name. Bare mentions are not refused: the
  number alone, `:8408` with no host, `REMOTE_PORT` unexpanded. A loopback
  URL with the port inside a commit message or an `echo` is refused too
  (deliberately: exempting text would have to know which programs only
  print). Not caught: port arithmetic, a hosts-file alias, a script file.
  v1's gate and v2's layer 1 both start from `decide`, so neither asks and
  no allowlist entry covers it.
- **HUD:** Settings > Phone (`hud/src/components/PhoneSettings.tsx`): the
  listener's state from `/status`, Pair a phone (QR code from the vendored
  Nayuki encoder `hud/src/lib/vendor/qrcodegen.ts`, the URL as text, a
  countdown, Cancel), the passkeys with their sessions, Revoke (confirmed)
  and End. Phone-supplied text is React text only. **Phone shell:**
  `hud/src/mobile/main.ts` (`/m/pair#code`: the code leaves the address bar
  at once, Create passkey, then Sign in; `/m/`: Sign in with passkey or
  Signed in · paired with Sign out), the spike's base64url serialization in
  `hud/src/mobile/webauthn.ts`.

`/status` gains `remote: {port, bound, host_configured}` (no name, no
secret). Tests: `tests/v2/remote_auth_check.py` (a software P-256
authenticator against the real `webauthn`), `tests/face/hud_v2_phone_check.py`
(the Settings section, and the built shell end to end with Chromium's virtual
authenticator), `rules_check`/`permissions_check` `remote_listener_checks`,
`archive_check` (`test_no_tool_reaches_the_phone_pairing`).

## The phone's read API and tabs (phone C3 / P-3, 2026-10-25)

`jarvis/v2/remote_api.py` (SELF_PROTECTED) is what a signed-in phone can read
(design §5, §6). `remote_auth.api_route` hands it every `/m/api` path but the
P-2 routes, and only after `owner_status` (§4 items 1 to 4) passes: without a
session every path is 401 before the module is imported for it. With one, an
unknown path is 404 and a read asked with anything but GET (HEAD included) is
405 with `Allow: GET`; a write without the phone's Origin is still the
listener's 403 first. The session is checked again after a read is built and
before it is sent (`session_live`): a logout or revoke during a slow read is
a 401. `after` takes ASCII digits only. The listener's handler now carries the daemon
(`remote_listener.bind(host, port, auth, daemon)`); with none, the reads are
404. `projects.is_owner` is never asked and stays false; the module calls no
answering, sending or publishing function (pinned by source in
`remote_api_check`).

| Route (`GET /m/api/...`) | Calls | Caps |
|---|---|---|
| `inbox` | `inbox.rows` | 200 rows (`more`) |
| `projects` | `projects.project_view`, unarchived | 200 |
| `threads[?project=]` | `Daemon.thread_json`, as `GET /threads` | 300, newest first |
| `threads/<id>/log[?after=]` | `chat_reads.projected` over `read_log` | tail 60; pages of 200 rows and 512 KiB; a row cut at 20,000 characters (`cut`) |
| `tasks` | the task records, as `GET /tasks` | 200, a brief at 400 characters |
| `tasks/<id>` | one task record | strings 20,000, lists 200 |
| `tasks/<id>/diff` | `hud_api._diff_context/_diff_paths/_diff_parts` | 200 files, 256 KiB a file, 1 MiB in all; past any, `patch: null`, `too_large`, `truncated` and `note: "Too large to show here: open it at the desk."` |
| `events` | the bus, filtered | 8 streams (a ninth ends the oldest) |

**Each answer is the HUD projection minus documented fields**
(`remote_api.DROPPED`, held field for field against the HUD's own routes by
`tests/v2/remote_api_check.py`, so a field the HUD gains later fails the
check until it is decided): no filesystem path (project `root`, `root_real`,
`extra_dirs`; thread `cwd`; task `worktree`, `root`; status `last_file`;
a task's `status` keeps `STATUS_FIELDS` only), no permission
configuration (`profile`, `routing`, `always_ask`, thread `bypass`), no other
surface's addressing (Discord ids and names, an approval's `code` and
`discord_channel_id`), no provider session handle or accounting internals;
the task list leaves `spec`, `plan`, `report`, `thread_ids`, `branch`,
`provider_override` and `ceilings` to `tasks/<id>`. Never `owner_json`: a
private approval row is `inbox.rows`' private projection exactly (minus
`code`). **The inbox is an allowlist** (review of #308): every row keeps
`ROW_FIELDS` only; its `detail` keeps `APPROVAL_DETAIL_FIELDS` (and
`RELAYED_FIELDS` of `relayed_from`) for an approval, `DETAIL_FIELDS[kind]`
for a question, task question, proposal or task, and for a kind P-3 does not
know (A3's "missed" rows) only `GENERIC_DETAIL_FIELDS` with scalar values.
**Every string passes `chat_reads.scrub`**: thread titles, a task's brief,
plan, report, spec and status, every inbox row's `title`, `blocks` and
`origin`, and every `detail` string, an approval card's included. The card's
own redaction is not enough off the box: `card_args` misses a private key's
body in an argument, and `origin` and `relayed_from.title` are only
`clean_line`d. So the phone's card can withhold more than the desk's; P-4
must show the full shared card before any answer. Diff patches pass the Diff
tab's scrub and then `chat_reads.scrub` too (the v1 scrub the Diff tab uses
lets a GitHub token in a changed file through; the desk still shows it, the
phone does not). **On the phone only**, `remote_api.labelled_values` then
withholds a value written after a credential's label (`aws_secret_access_key
= …`, `password: …`, `"password": "…"`, a Google `client_secret: …`):
every string `_scrub` sends, the transcript rows and the diff. It keeps what
reads as code or a reference (a bare or dotted identifier without a digit,
anything with `(` or `[`, anything starting `~ / ! * % & @ $ {`, a mask) and,
under 16 characters, anything without a digit. It is not in the shared
`redact.scrub_text`/`chat_reads.scrub` (delta review of #308): those feed
agents (chat relays, git tool output to Codex), where it rewrote ordinary
code; they are main's, pinned on the review's 50 lines in `remote_api_check`.

**The transcript is `chat_reads`' projection**, row for row
(`chat_reads.projected`, new: `transcript` with each row's log-record index):
the owner's `typed` words, replies, relays, system lines; tool events and
output, thinking, gates, usage and inlined attachments never leave. Each row
carries `seq`; `after=<seq>` resumes and `next` is the next cursor; without
`after` the newest 60 rows, `older` saying more are at the desk.

**The event stream is SSE** (`/m/api/events`, as the HUD's `/events`; the
design's `/m/ws` named a WebSocket the HUD does not have, and under `/m/api`
the stream inherits the 401 and the agent-path DENY). It sends `retry: 3000`
and four kinds, ids and a source kind only, never text:
`inbox_changed {ids, reason}`, `thread_changed {thread_id, project_id,
reason}` (a transcript record, a turn's start or end, a thread change: not
deltas, tools, thinking, usage or sub-agents), `task_changed {task_id,
project_id, reason}` (created, status, question) and `resync` (the
subscription dropped events). Nothing is sent for a thread or task of an
archived project (`_Hidden`, read again after a project event or every 5 s).
It needs a session to open, and ends when the
session does (logout, a revoke, idle or absolute expiry), checked before
every write and at least once a second through `session_live`, which does
not count as activity. The daemon's shutdown ends it.

**The app** (`hud/src/mobile/app.ts`, `views.ts`, `model.ts`, `net.ts`,
`markdown.ts`; plain DOM, no React): Inbox (the HUD's groups,
`lib/inbox.ts`; every kind in full; approvals and questions say "Answer at the
desk", proposals "Withdraw at the desk", task rows "Resume or cancel at the
desk"; no answer control), Chats (projects, then the five most recent chats
by `lib/recentChats.ts`, then the transcript; no input box), Tasks (running,
then blocked, then the rest; state, plan, last report, open questions, the
diff folded file by file, folds kept across a refresh). Every daemon string
is `textContent`; only an assistant's reply goes through `lib/markdown.ts`,
then `mobile/markdown.ts` drops KaTeX's HTML layer and every `style`
attribute in an inert template (MathML stays, which iOS draws), so the CSP is
C1's unchanged; the shared renderer's own `style` attributes on KaTeX output
are refused by it (`style-src-attr`, reported, dropped). Each stream event
re-reads its route; a reconnect re-reads everything shown. A read that cannot
reach Jarvis (a network error, or 502/503/504 from the proxy), or a dropped
stream whose `/m/api/me` check fails, shows the offline banner ("Offline.
Nothing here is current: it was last read at HH:MM:SS.") and marks the view
stale (`data-stale`, dimmed) until a read lands. A 401 anywhere stops the app
and returns to sign-in. Tap targets are at least 44 px.

Tests: `tests/v2/remote_api_check.py`, `hud/src/mobile/app.test.ts`, the app
half of `tests/face/hud_v2_phone_check.py` (`app_checks`: the built bundle
through a real remote listener, signed in with the virtual authenticator).

## Answering from the phone (phone C4 / P-4, 2026-10-25)

`jarvis/v2/remote_answer.py` and `jarvis/v2/remote_secure.py` (both
SELF_PROTECTED) let a signed-in phone answer (design PH-4, PH-7, PH-10,
§4.1, §5). `remote_auth.api_route` hands them their four paths after
`owner_status` (§4 items 1 to 4), before P-3's reads:

| Route (`/m/api/...`) | Needs | Calls |
|---|---|---|
| `GET inbox/<id>/detail` | session; a HUD-only card: every §4.1 check, fresh | `to_json` (`owner_details` for a HUD-only card), minus `code`/`discord_channel_id` |
| `POST inbox/<id>/answer/options` `{decision: allow\|always}` | session, §4.1 for Always and HUD-only | `RemoteAuth.answer_options`; for Always `PendingApprovals.always_rule` |
| `POST inbox/<id>/answer` | Deny: session. Allow/Always: the assertion, then §4.1 | `inbox.answer(…, owner_verified, via="phone")` |
| `POST inbox/decisions/<id>` | session | `inbox.answer_decision(…, via="phone")` |

- **`owner_verified` (approvals.py).** `PendingApprovals.resolve(…, hud=)`
  is now `owner_verified=`; it alone lets a HUD-only card be allowed, and
  its refusal is unchanged. Exactly two callers pass a value that can be
  True: the HUD's routes (`POST /approvals/<id>` and `POST
  /inbox/<id>/answer`, after `owner_only`, with the HUD listener's own
  Host, as `hud=` was) and the phone's answer after a verified assertion
  while every §4.1 check holds. Discord, the escape hatch, tools and the
  inbox dispatch from anywhere else pass False (`tests/v2/remote_answer_check.py`
  greps every assignment in `jarvis/`). Always on a HUD-only card still
  writes no rule, as at the desk.
- **Per-answer assertions (PH-4).** The challenge is `sha256("jarvis-phone-
  answer-v1\0" ‖ req_id ‖ "\0" ‖ decision ‖ "\0" ‖ 32-byte nonce)`, kept
  server side with the req_id, the decision, the session's token hash and its
  passkey; single use (spent before anything is verified), 60 s on the
  monotonic clock, at most 4 per session and never a shared cap (review of
  PR #313: one session's abandoned prompts can never refuse another's; the
  same session asking again for the same card and answer replaces its
  earlier challenge; 429 past 4; sign-ins are a pool of their own).
  `allowCredentials` names the session's passkey only.
  `answer_verify` refuses (401, no detail) an assertion issued for another
  card, another answer (an Allow's is never an Always), another session or
  passkey, a credential that is not the session's passkey, `crossOrigin` or
  `topOrigin`, a wrong origin or rpId, UV clear, a bad signature or a
  counter that did not rise (PH-4's rule); an unknown, used or expired
  challenge is 409. The new counter is saved before the answer counts.
- **§4.1's monitor (`remote_secure.SecureMonitor`, `Daemon.secure`).** Five
  checks by name: `loopback` (the listener's socket: AF_INET, 127.0.0.1,
  the daemon's port), `serve` (`tailscale serve status --json`: exactly one
  handler, `<host>:443` `/` proxying to `http://127.0.0.1:<port>`, TCP
  `{"443": {"HTTPS": true}}` only, no Funnel, no other kind of entry),
  `certificate` (`tailscale status --json`: running, `Self.DNSName` is the
  host, the host under `MagicDNSSuffix` ending `.ts.net` and in
  `CertDomains`, serve's 443 HTTPS; for an answer, its Host and Origin are
  the host's), `passkey` (the passkeys file reads, a paired passkey exists,
  `REQUIRE_UV`; for an answer, the session's own passkey) and `listeners`
  (`/usr/bin/ss -H -ltne sport = :<port>`, by full path as the Windows
  programs are, never through PATH: exactly one, 127.0.0.1, its inode the
  listener's own socket; `Get-NetTCPConnection` through `powershell.exe`:
  only `wslrelay` on loopback, or nothing, then an `END-OF-LISTENERS` line:
  "no such connection" (CIM ObjectNotFound, an unused port) is the empty
  answer, any other error has no end line and fails). The reads are four
  fixed argvs, no shell, read-only, in parallel, each cut at 8 s and 1 MiB
  of output;
  any timeout, error, non-zero exit or unknown shape fails that check. They
  run at start-up and every 5 minutes (a thread started with the remote
  listener and stopped with the daemon) and **again in full before every
  detail, options and answer**; the last result is kept only to show it
  (`/status`'s `remote.secure` and `/m/api/me`'s `secure`: `{ok, failed,
  reason, label, at}`, fixed words), never to authorize, so a failure is
  never masked by an earlier pass. **Only daemon2's `main()` attaches the
  real reads** (`attach_system`); an embedded or test daemon reads nothing
  and is limited. Measured on this PC: about 0.6 s for the four (PowerShell
  is the slowest); WSL interop works from a systemd user service.
- **Limited** is Discord's authority (O-3): Deny everywhere; Allow on an
  ordinary card (`owner_verified=False`, answered `authority: "limited"`);
  no Always (409 "limited: <label>; Always needs every check"); a HUD-only
  card's detail, options and answer are 409 "answer at the desk".
- **The card the owner answers from.** The detail is the HUD's own card,
  not P-3's capped read view, through the phone's scrub, nothing cut:
  `remote_api.scrub_structure` (review of PR #313) scrubs every key and
  string, reads each string with its key as `<key>: <value>` and
  `<key>=<value>` (a nested `{"Authorization": "Bearer …"}`), and reads the
  whole card as one JSON text (a label in a key, its value beside it). If
  any pass changes anything, or a value was nested past the card
  redaction's depth (`"[nested too deep to judge]"`, never judged), the card
  is withheld (`phone_card`): the phone is sent the scrubbed card and Allow
  and Always are refused at the detail, the options and the answer: "this
  card has withheld text: answer it at the desk". Deny still works. P-3's
  inbox list sends approval rows through the same `scrub_structure`.
  **At the desk** (a desk behaviour change, failing closed): a card whose
  arguments hold the depth placeholder shows "Some arguments are nested too
  deep to be shown or checked…" and offers DENY only (`nestedTooDeep`,
  `hud/src/lib/approvalCard.ts`). A HUD-only card's private detail goes only to
  that route, only while pending, only under §4.1; it is never in a log
  line, the bus, `/m/api/events` or the inbox list (asserted).
- **Always shows its rule first.** `PendingApprovals.always_rule(req_id)`
  is a dry run of `resolve(always=True)`'s branch (the same tests in the
  same order, `permissions.entry_for` on `v1_request`), writing nothing; the
  options answer carries `rule`, `rule_words` and `resolution`, the phone
  shows them and asks for Confirm before the passkey; the verified answer
  writes it (the check holds the two equal case by case).
- **Questions, task questions, decisions** need the session only, through
  `inbox.answer` with `via="phone"`: a decision's answer is
  `UserMessage(origin="owner", via="phone", desk=False)` with B1's frame and
  queue (`daemon.VIAS` gains "phone"); `desk.desk_message` never counts it
  (it requires "hud"); Claude's `owner_turn` excludes "phone" as it does
  "inbox", so Bypass never applies to it; chat reads mark it "via the
  phone's inbox (not typed in this chat)", the HUD "from the phone", the
  Discord mirror "You (phone)". A chat question's `question_answered` log
  record gains `via: "phone"`; a task question goes to
  `TaskControl.answer_question`, which takes no `via`. A proposal's
  withdraw stays at the desk (400).
- **PH-10.** `PendingApprovals.wait_s()`: a broker on the local default
  (120 s) waits 600 s while `RemoteAuth.recently_active()` (an active,
  unrevoked passkey is paired, and a session was seen or signed in within
  the last 24 h; kept past a logout, rebuilt from the sessions file at
  start; a revoke of the last passkey ends it at once) says so; a broker given its own wait keeps it,
  Discord's is 600 s anyway, a HUD-only card keeps its 120 s cap.
- **The app** (`hud/src/mobile/card.ts`, `app.ts`, `views.ts`): an approval
  row's "Open the card to answer" opens the card, drawn with the HUD card's
  words (`hud/src/lib/approvalCard.ts`, which `Approvals.tsx` and
  `BrowserApproval.tsx` now use too): headline, REQUESTED BY, the relayed
  line, tool, the whole command, every argument, `[layer] reason`; a
  HUD-only card the desk's Browser body (`browserBody`, page text in
  `<bdi>`), parsed by the HUD's own `parseBrowserPrivateApproval`. DENY
  first and alone; 120 px below it ALWAYS and AUTHORIZE, out of the Tab
  order, never focused by a tap; Enter and Space on the card do nothing
  unless DENY has focus; no form; buttons at least 48 px. A withheld or
  limited card disables what it may not use and says why; a red "limited:
  …" line shows on the card and atop the inbox. Questions take a text box
  and Send, decisions their options (the suggestion pre-picked) or words
  and Answer; Enter never sends, and a redraw waits while an answer is
  typed. Each answer is sent once; a refusal shows the daemon's words.
  **HUD:** Settings > Phone shows the same red "limited: …" line from
  `/status`.

Tests: `tests/v2/remote_answer_check.py` (45: the acceptance list,
one test per §4.1 condition, the monitor's shapes, a timeout and the
display cache), `hud/src/mobile/answer.test.ts`,
`tests/face/hud_v2_phone_answer_check.py` (the built bundle, a real
daemon and broker, Chromium's virtual authenticator: Deny, Allow, Always, a
HUD-only card under §4.1 then limited, a withheld card, hostile text, no
keyboard default, a task question), the P-4 additions to
`tests/face/hud_v2_phone_check.py`, and the Bypass case for "phone" in
`claude_provider_check`.
