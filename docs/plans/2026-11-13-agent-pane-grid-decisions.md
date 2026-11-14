# Agent pane grid and report-back: owner decisions (2026-11-13)

## What the owner saw

The owner asked a Claude HUD chat to "open 11 sub-agents in the other 11
available panels". The chat started 11 sub-agents and called
`hud_open_subagents`. Only 3 sub-agent panes appeared, and the layout was
lopsided: the left column held chat, sub 4 and sub 3 stacked, then one tall
pane for sub 2 and one wide pane for sub 1. The other 8 were dropped without
a word. The chat said "the HUD doesn't report back, so I can't tell whether
all 11 fit."

**Root cause.** Each `byAgent` placement split from the chat's pane, one per
layout pass: right if `splitVerdict` allowed it, else down. The chat's pane
was halved until it hit PANE_MIN_W (360) or MAX_DOWN (3). From then on every
placement returned "full", because `byAgent` never reuses a pane. Halving one
pane can never reach a 12-pane window (4 across by 3 down).

The owner also asked Jarvis to "open 3 other panels in the HUD and pull up
instagram in the browser in them". Jarvis refused, because no tool opens a
Browser pane: the drive tools need a pane that is already showing.

## Decisions

- **D1. Panes a chat opens "properly wrap in the proper layout".** They form
  a balanced grid, not repeated halves of the chat's pane.
- **D2. `hud_open_browser` opens each new pane straight at its URL in Remote
  mode.** Only public URLs are allowed, judged by the same daemon-side URL
  check the agent drive and goto path uses. No loopback or LAN. It **never**
  admits a loopback port in the gate. This is the same reasoning as decision
  L2 in `2026-10-12-hud-browser-plan-amendment-2.md`: only the owner's own
  typed URL, click or "Open in Remote" admits a port.
- **D3. Placement is the same as for sub-agents.**
  - Reuse drawn panes that hold no unsaved work. Never take the chat's own
    pane, the focused or keyboard pane, a pane an agent is driving, or a
    KEEPS_OWN_WORK view.
  - When no pane is free, split or re-lay the grid.
  - Never move focus.
  - At most 11 panes, because one pane shows the chat.
- **D4. Same binding as `hud_open_subagents`.**
  - Only Claude and Codex owner HUD chats, and only the foreground
    conversation (depth 0).
  - Only during a turn the owner started at the desk (`runtime.at_desk()`).
  - Checked in the tool, then again in the service under the daemon lock.
  - Not available to Discord, the phone, schedules, tasks, sub-agents or
    relayed turns.
  - `EXPLICIT_ONLY`, not in `mcp_catalog.MCP_TOOLS`, and offered through
    `session_tools.py` the same way as `hud_open_subagents`.
- **D5. A chat can see and use the pages it opened** (owner, 2026-11-13,
  "Yes, usual switches (Recommended)"). After `hud_open_browser` opens a
  public page, the chat can snapshot it. It can also click and type there,
  exactly as if it had used `browser_goto`, but only when that pane's
  "Jarvis can drive" switch and the global `agents_may_drive` switch are
  both on. It still never opens a loopback port or a LAN address. This
  replaces PR B's first draft, which marked these tabs `unknown` so that no
  agent could look at or drive them.

## How PR A implements D1 and D3 (`claude/agent-pane-grid`)

- **One record is one batch** (`hud/src/lib/agentpanes.ts`
  `placeAgentPanes`, `App.tsx` `placeAgentBatch`).
  - A child already shown stays where it is and counts as shown.
  - The rest go first to the free drawn panes, in reading order
    (`agentFreePanes`, the D3 rules).
  - Whatever is left re-lays the window **once** as a balanced grid
    (`splittree.ts` `gridTree`, `balancedRows`). The size is
    T = min(12, drawn + rest, drawn + free ids).
  - Grid limits: at most 3 rows of at most 4 panes, and every cell at least
    360 by 200 at the px the window draws the centre with now. The grid never
    borrows from the side panes or the panel. The fewest rows that fit wins,
    so a wide window gets more columns. Longer rows come first: 7 is 4 over
    3, 11 is 4/4/3 and 5 is 3 over 2.
  - Existing panes keep their reading order and new panes follow, so the
    chat stays in slot 1.
  - When even the largest grid that fits is too small, the window places
    what fits and counts the rest as not placed.
  - A window that already drops panes, or a stale fit, takes free panes only.
- **Report back.**
  - The bus record carries a random `request_id` from the daemon.
  - After placing a batch, the window posts `{window, request_id, placed,
    not_placed}` (counts only) to `POST /agent-panes/report`
    (`jarvis/v2/agent_panes.py`). That route is owner-only, needs a strict
    loopback Host, and needs a live owner window (one holding the event
    stream it named).
  - The first window's answer wins: a second answer gets 409, and an id
    nothing is waiting for gets 404.
  - `SubagentPanes.open` waits up to 3 s on the monotonic clock for that
    answer, never under the daemon lock. It then returns one of:
    - "Shown 11 of 11 sub-agent(s) in HUD panes: …"
    - "Shown 7 of 11 …; no room for 4 (…)"
    - on timeout, "Asked … No HUD window replied within 3 seconds …"
    - with no window open, "No HUD window is open, so none is shown."

## Judgment calls PR A made (decisions D1–D4 do not cover these)

1. **Only truly free ids are used for new grid panes**, never a hidden (⊞)
   pane, not even a blank one. The brief says hidden panes and their specs
   stay untouched. As a result, a window with many ⊞-hidden panes gets fewer
   new panes.
2. **Child panes already showing count as placed** in the report.
3. **A batch that waited behind a card for more than 3 s is still placed**
   when the card goes. Its report then gets 404, because the daemon stopped
   waiting. The chat was already told "no reply".
4. **With no live HUD window, the call does not wait.** It publishes with no
   request id and says that no window is open.
5. **Re-laying the grid resets pane sizes to equal**, as ⊞ does (FS-8). The
   owner's dragged edges are not kept.
6. **The grid keeps the owner's keyboard and counts the drawn rules** (review
   of #358). If the re-layout unmounts the focused control in the owner's pane,
   focus goes back to that pane's message box or to the pane itself, never to a
   frame. The fit checks the cells as drawn, so the 1px rules between panes
   count. `splitVerdict` and `autoSplit` still check the nominal halves;
   that is out of this PR's scope.

## How PR B implements D2–D5 (`claude/hud-open-browser`)

- **The tool and the service.** The tool is `hud_open_browser(urls)`: 1 to
  11 URLs (`jarvis/v2/tools/hud_open_browser.py`). The service is
  `jarvis/v2/browser_open.py`, `BrowserOpens`. It is bound exactly as
  `hud_open_subagents` is (D4):
  - The tool checks the desk and the depth.
  - The service checks the shared binding `agent_panes.refuse_desk_caller`
    under the daemon lock, and checks it again after the URL lookups.
  - The tool is `EXPLICIT_ONLY` and listed in `session_tools.py`.
- **The daemon judges each URL** (`judge_public_url`, D2). One refusal
  refuses the whole call, as text, and nothing is published. The steps:
  1. `policy.judge_url`, the judge agent navigation and the owner's routes
     use. It allows only http, https and `about:blank`, and refuses user
     names, ambiguous authorities and every spelling of Jarvis's ports.
  2. http(s) only, and no Jarvis port on any host (`policy.judge_port`).
  3. An IP literal must be in the public class (`policy.address_class`).
  4. A name must not look local: one label, or a suffix such as `.local`,
     `.lan`, `.internal`, `.home.arpa`, `.test` or `.example`.
  5. A name must resolve, right now, only to public addresses. Lookups run
     in parallel, off the daemon lock, within 3 s.

  The SOCKS gate still judges every connection the page makes afterwards.
- **The bus record** is `browser_open {thread_id, data: {urls (as judged),
  request_id?}}`. It is placed as one batch with PR A's placement, and the
  report and its wording work as in PR A ("Opened 3 of 3 Browser pane(s) at
  instagram.com …").
- **Each new pane** (`lib/workspace.ts` `agentBrowserSpec`):
  - It is a Browser view on the default named profile, never Private.
  - It has one Remote tab at the URL, with `source {kind: "agent"}` and no
    Remote handle yet.
  - See and drive start at their defaults; the tool never touches them.
  - A reused pane (`showAgentBrowser`) gains an active tab and keeps its
    spec. A Private pane, or one with 12 tabs, is never reused.
- **The window judges each URL again** (`lib/agentpanes.ts`
  `agentBrowserUrl`). It refuses loopback (`isLoopbackHost`, and anything
  `judgeBrowserUrl` would frame as a local server), private, link-local,
  CGNAT and IPv6 literals, one-label hosts and local-network names. A
  refused URL is counted as not placed.
- **It never admits a port.** The window's existing `ensure` creates the tab
  with `POST /browser/tabs` (no URL) and then `/restore`, never `/navigate`.
  `restore` passes `owner=False`, so the gate opens nothing. With real
  Chrome, that is `owner_browser_check.py` `test_11c`.
- **It never takes focus** (R-1). The placement keeps `focused`, the tab is a
  Remote canvas rather than a frame, and the address field focuses only on
  the owner's own action.
- **D5: an explicit `agent` source kind**, because none existed. An agent's
  own `browser_goto` (drive `navigate`) records no source of its own: the tab
  keeps its pane's source, which is the owner's in practice, and navigates
  with `owner=False`. Agent-opened tabs get exactly that treatment:
  - The daemon (`browser/panes.py` `ResolvedSource`, `_source`) accepts
    exactly `{"kind": "agent"}`. `look.py` and `drive.py` accept `agent`
    wherever they accept `owner`.
  - Drive still needs the pane's see and drive switches, Remote mode and
    `agents_may_drive` (`_effective_drive`, unchanged).
  - `agent` grants nothing beyond `owner`.
  - As a non-owner kind, it is sticky across reports: only the owner's own
    address route makes a tab `owner`. In the HUD, `submitBrowserUrl` drops
    the mark when the owner presses Enter in the address bar.
  - A pop-up keeps its parent's `agent` mark.
  - A forged variant parses as `unknown` in the HUD and is refused (400) by
    the daemon.

## Judgment calls PR B made

1. **The daemon resolves names when the tool is called** (D2 says "public
   URLs only"). It also refuses local-looking names by their spelling. A
   lookup failure refuses the call.
2. **A public page can still redirect elsewhere.** The gate judges each
   redirect, so loopback is refused unless the owner opened that port. A LAN
   address is allowed only when the owner's LAN switch is on, as for any page
   in the Browser.
3. **The HUD is trusted to use `restore`.** The daemon cannot tell an
   agent-opened `POST /browser/tabs` from the owner's. The defenses are the
   daemon's URL judge (no loopback reaches the record), the window's second
   judge, and tests that pin restore and never navigate.
4. **Private panes and panes with 12 tabs are never reused**, and nothing is
   ever opened in a Direct frame.
