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
