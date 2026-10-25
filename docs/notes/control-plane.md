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
a refusal's included (64 KiB cap, 413 above it after draining up to 1 MiB;
chunked bodies decoded the same way), so `tailscale serve`'s Go proxy never
turns an answer over an unread body into a 502 (PR #304's follow-up). Files come from `hud/dist` under
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
  registration; its code is 256 random bits, only in the daemon's memory,
  compared in constant time, and travels in the URL fragment
  (`https://<host>/m/pair#<code>`), which no browser sends. A replaced window's
  code and registration challenges die with it. No window without a
  configured host and a bound listener (409). At most 3 active passkeys:
  refused at the window, the options and the verify (409).
- **Registration** (`POST /m/api/pair/options`, `/pair/verify`, each with the
  code): `rp_id` = the remote host, origin `https://<host>`,
  `residentKey: required`, `userVerification: required`, verified with
  `require_user_verification=True` and the result's UV flag checked again.
  **A registration never makes a session** (with `none` attestation it proves
  nothing about the private key): the phone signs in next.
- **Login** (`POST /m/api/login/options`, `/login/verify`): a 32-byte
  server-side challenge, single-use (taken out before verifying, so a failed
  attempt spends it), 60 s on the monotonic clock, pruned and capped at 64
  outstanding. Options name no credential (discoverable passkeys). Unknown,
  spent, expired or registration challenge: 409. Unknown or revoked
  credential, another user handle, wrong origin or rpId, UV clear, a bad
  signature, or PH-4's counter rule (0 while the stored counter is 0;
  otherwise strictly greater): 401 with no detail.
- **Sessions.** `__Host-jarvis_m=<256-bit token>; Path=/; Max-Age=604800;
  Secure; HttpOnly; SameSite=Strict`. Only the token's SHA-256 is stored.
  12 h idle, 7 days absolute, on the wall clock. Each records its passkey,
  the cleaned and capped user agent, created, last-seen (written at most once
  a minute) and the advisory headers. `POST /m/api/logout` ends it and clears
  the cookie; Revoke ends every session of the passkey **in memory first**,
  so it holds even if the file write fails; End ends one.
- **`is_remote_owner(handler)` / `owner_status`** is the only owner test on
  the remote listener (`projects.is_owner` stays false there): this daemon's
  bound remote server (`RemoteAuth.server`, set by `remote_listener.bind`),
  exactly the configured Host (400), exactly `Origin: https://<host>` on any
  method but GET/HEAD (403), and a live session of an active passkey (401).
  `GET /m/api/me` and `POST /m/api/logout` are the only session routes until
  P-3; the rest of `/m/api` is 404 with a session, 401 without.
- **Storage.** `<data root>/remote/` 0700, `passkeys.json` and
  `sessions.json` 0600, atomic (`stores._write_bytes`), read with
  `O_NOFOLLOW`. Both are protected state (`files._protected_state_paths`): no
  write tool, shell write or grant reaches them. An unreadable passkeys file
  is never overwritten (pairing and login 503, the HUD says so); an
  unreadable sessions file signs every phone out. `webauthn` is imported only
  when used, so a daemon without it starts and answers 503 there.
- **The agent path** (design §4). `rules.remote_reading`, called from
  `rules._decide` on the raw line like the Secret Service reading, DENYs a
  line that names the remote port (8408 or `config.REMOTE_PORT`, or the name
  `REMOTE_PORT`) together with a loopback spelling, a `host:port` or
  `/dev/tcp/…/port` form, or a network client or library; or `/m/api`
  together with a loopback spelling or a `ts.net` name. v1's gate and v2's
  layer 1 both start from `decide`, so neither asks and no allowlist entry
  covers it. An accident guard, not a boundary (a computed port, a script
  file, a hosts alias without a port get past it); the boundary is the
  cookie, which only the phone's browser holds.
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
