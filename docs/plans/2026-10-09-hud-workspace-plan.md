# Plan: a split workspace, layout toggles and an integrated terminal (Jarvis v2 HUD)

_Planner's proposal, written read-only against `origin/main` at 2ede93d. Where
a later decisions file disagrees, the decisions file wins._

## For the owner: what this gives you

- **A title bar with VS Code's layout toggles.** At the top right are four
  buttons: Customize Layout, Sidebar, Terminal panel and Status pane. Each is
  filled when its area is open. Ctrl+B and Ctrl+Alt+B still work, and Ctrl+`
  toggles the panel.
- **A split centre.** Choose one pane, two side by side, two stacked, three
  side by side, one large plus two stacked, or a 2×2 grid. Any pane can show
  chat, task, file, diff, preview or terminal. Two chats side by side work as
  two separate conversations.
- **A real Linux terminal.** It lives in a bottom panel, and any pane can show
  one too. It runs your own shell, in the folder of whatever you are looking
  at. It survives a HUD reload. Jarvis has no way to type into it or open
  one.
- **Dev servers in Preview.** A `localhost` link printed in the terminal opens
  in a Preview pane, beside the chat.

```
┌ title bar ──────────────────────────────────────────────────────────────────────────┐
│                               Model  Voice  Avatar  Settings │ − 100% + │ ⊞  ◧  ⬓  ◨ │
├ sidebar ─────┬ pane 1 · chat ──────────────┬ pane 2 · preview ─────────┬ status ─────┤
│ + New thread │ chat task file diff preview │ http://localhost:5173/    │ approvals   │
│ projects     │ …replies…                   │ (the dev server's page)   │ usage       │
│  ▸ threads   │ [ input bar ]               │                           │ schedules   │
│              ├ panel ──────────────────────┴───────────────────────────┤             │
│ (orb)        │ [bash · Jarvis] [npm run dev] +▾                         │             │
└──────────────┴──────────────────────────────────────────────────────────┴─────────────┘
```

## 1. What exists now

**Folding and resizing already exist (PR #17), as do zoom and the rules for
fitting a small window.**
- `lib/layout.ts` decides every number: pane ranges (`PANE`, :44), the centre
  minimum (`MAIN_MIN` = 480, :53), the 36px rail, and `fitLayout` (:213). When
  the window is too narrow, `fitLayout` shrinks the side panes, then folds the
  right pane and then the left, for that render only. It never rewrites the
  stored widths or folded flags.
- `shortcutFor` (:284) maps Ctrl+B, Ctrl+Alt+B and Ctrl+= / Ctrl+- / Ctrl+0.
  `inMonaco` (:307) is the stand-aside rule.
- `components/Layout.tsx` applies them:
  - `useLayout(blocked)` (:39), whose keys are inert while a card is up
    (:112);
  - `Splitter` (:141), a `role="separator"` with drag, arrow keys and
    double-click to reset;
  - `Rail` (:224) and `CollapseButton` (:294).
- **What is missing is a visible, always-present control.** The toggles are
  the `«`/`»` inside each pane and the rails. The zoom control lives in the
  status pane's header (`App.tsx:1343`), so folding the status pane hides it
  (`theme.css:133`).

**The window holds exactly one conversation.**
- `state.threadId | compose`, `messages`, `draft`, `ops`, `busy`,
  `turnThreadId`, `status`, `restore` and `tab` are single fields
  (`state/store.tsx:28–57`).
- The rule "the chat pane decides where a message goes" is enforced by
  `lib/compose.ts` (header, and `activeProjectId` :76).
- SSE events are filtered by one `shown` thread (`App.tsx:358–361`).
- Capture is suppressed while that one turn is busy (:125–130).
- `/seen` fires only for the thread in the chat tab (:909–916).

**The centre is one pane with five tabs** (`App.tsx:1220–1328`). The same
`#tabs` row holds the profile select and the global Model, Voice, Avatar and
Settings buttons (:1233–1265).

**A latent bug that splitting would expose.** `FileTab` saves to whatever
project is current at save time, with `pid + file.path` (`FileTab.tsx:117–126`).
Today, switching threads also switches to the chat tab, which unmounts the
editor, so the bug never fires. A file pane that stays open while the chat
moves to another project would save into the wrong project.

**Preview already accepts any `http://localhost:<port>/`** except 8402
(`lib/preview.ts:37–63`).
- The frame is sandboxed `allow-scripts allow-forms` without
  `allow-same-origin` (`PreviewTab.tsx:97`).
- The daemon's API port 8405 is not refused.
- The HUD's own pages carry no `frame-ancestors`: `binary()` sends a CSP only
  when one is passed (`hud_api.py:691`), and `/` serves the HUD on every
  non-preview listener (:977).

**How the daemon serves and checks requests.**
- It runs three stdlib `ThreadingHTTPServer` listeners on loopback: API 8405,
  HUD 8402 and preview-only 8403 (`daemon.py:257–271`). They share one
  `BaseHTTPRequestHandler`, which sets a 2-second socket timeout (:1424) and
  speaks HTTP/1.0.
- SSE is a handler that blocks and writes (:1756).
- `check_origin` (`hud_api.py:705–721`) checks three things:
  - Host must be exactly `127.0.0.1:<port>` or `localhost:<port>`;
  - an Origin, *if present*, must match the Host;
  - `Sec-Fetch-Site: cross-site` is refused, and a POST must be JSON.
- `projects.owner_only` (:117–133) is stricter: the HUD's listener **and** an
  Origin that must be present.
- `POST /approvals/{id}` uses only `check_origin` (`daemon.py:1675`). A local
  client that sends no Origin can therefore answer an approval today.

**The headless suites guard live ports for HTTP only.** `hud_v2_check.py`
aborts requests to 8402/8403/8405 with `ctx.route` (:2256–2268). That
function never sees WebSockets. Playwright 1.61 in the venv has
`route_web_socket`.

## 2. Design

### 2.1 The title bar and the layout toggles (feature 2, plus the owner's addition)

**Where they go: a window-wide title bar, 28px tall, above `#shell`.**
- Contents, right-aligned: Model · Voice · Avatar · Settings │ `− 100% +` │
  ⊞ ◧ ⬓ ◨. The zoom control moves here from the status pane.
- The status pane's header would not work. When that pane is folded the
  header disappears, so the button that reopens the pane would vanish with
  it. The zoom control has exactly that problem today.
- The title bar's right end is the window's top-right in every fold state. It
  costs 28px of height. In return, the centre's per-pane header no longer
  carries the global buttons.

**The four buttons, in VS Code's order.** Each is an inline 16×16 SVG of a
window outline. The area the button controls is filled with `currentColor`
when open and drawn as an outline when closed. Colours are `--text-dim`,
`--text` on hover, and the accent focus ring only on focus. Every button is
an `aria-pressed` toggle with a tooltip that names its shortcut.

| Button | Does | Shows "open" when |
|---|---|---|
| ⊞ Customize Layout | Opens the layout menu, the **one entry point for presets** | menu open (`aria-expanded`) |
| ◧ Sidebar | `toggle("left")`, the existing function | the sidebar is *drawn* open |
| ⬓ Panel | Shows or hides the bottom terminal panel (§2.3). Ships with the terminal UI (WP-D); until then the group has three buttons | the panel is drawn open |
| ◨ Status pane | `toggle("right")` | the status pane is drawn open |

**When the window, not the owner, folded a pane.** The icon follows what is
*drawn* (`fitted`), so it shows closed. It also gets `data-auto="true"` and
a dashed outline. Clicking it does exactly what the rail's `»` does: the pane
opens and the window folds the other pane instead (the `prefer` rule in
`fitLayout`). The tooltip says so: "Show the sidebar. The window is too
narrow for both, so the status pane folds instead (Ctrl+B)." The panel button
behaves the same way vertically (§2.2, Fitting).

**The Customize Layout menu** contains:
- six preset tiles, each a small diagram, with the current one checked;
- the same three toggles written out with their shortcuts, for
  discoverability;
- **Reset layout**: single pane, default widths, panel closed;
- when the window has dropped panes (§2.2), a line "Showing 2 of 3 panes:
  the window is too narrow".

It is placed from the button's rect through `toCss()`, kept on screen
right-aligned, and closed by Escape. Keyboard: arrow keys move between tiles,
and Space never reaches push-to-talk.

**Card invariants.**
- While an authorization card is up, every title-bar button is `disabled`,
  and an open menu closes.
- The veil already blocks clicks. Keyboard activation is the hole: a toggle
  that still has focus when a card appears re-lays the window out on Enter,
  and so do `CollapseButton` and the rail buttons today. WP-A disables all of
  them while blocked.

**At the narrowest widths.** First, the four text buttons fold into a `⋯`
menu. The zoom control and the toggles never hide, because they are how you
get room back. They need about 260 zoomed px, and the narrowest supported
window (both rails, centre 480) is about 552. In a window smaller than that,
the bar wraps to a second line rather than clipping, following the existing
§18 "wrap, never clip" rule.

### 2.2 The split workspace (feature 1)

**Presets, not free splitting.** You asked for 2, 3 and 4 side by side. Free
splitting (VS Code's tree of groups with drag-to-split drop zones) costs a
tree data model, drop targets and size limits propagated through the tree.
That is several times the code for a choice six presets already cover:

| Preset | Shape | Centre min width | Pane-area min height |
|---|---|---|---|
| `single` | 1 | 480 (today's `MAIN_MIN`) | 200 |
| `cols2` | 2 side by side | 720 | 200 |
| `rows2` | 2 stacked | 480 | 400 |
| `cols3` | 3 side by side | 1080 | 200 |
| `main2` | 1 large + 2 stacked | 840 | 400 |
| `grid4` | 2×2 | 720 | 400 |

`PANE_MIN` is 360 wide and 200 tall, in unzoomed px like every width here.

**The pane model** lives in a new `lib/workspace.ts`, which is pure and
tested the way `layout.ts` is:

```ts
type View = "chat" | "task" | "file" | "diff" | "preview" | "terminal";
interface PaneSpec { view: View; projectId?: string | null; terminalId?: string | null; previewUrl?: string }
interface Workspace {
  preset: Preset; panes: [PaneSpec, PaneSpec, PaneSpec, PaneSpec];
  splits: Record<Preset, { cols: number[]; row: number }>; // fractions, per preset
  focused: 1 | 2 | 3 | 4; panel: { open: boolean; height: number };
}
```

- **All four pane specs are always stored.** Going from 4 panes to 1 and back
  brings back what each pane showed.
- **A pane is mounted the first time a preset shows it, and then kept.** It
  is hidden, never unmounted, as with the side panes. An unsaved edit or a
  half-typed message in pane 3 survives switching to one pane.
- A fresh window has only pane 1, so the default layout is today's centre
  with the title bar above it. Every existing selector still matches exactly
  one element.
- **Resizing.** The column and row edges are the existing `Splitter`,
  generalised to an axis. They support drag, the arrow keys (Shift for a
  bigger step), Home/End, and double-click to make the panes equal. Positions
  are stored as **fractions per preset**, because the centre's width changes
  with the side panes. They are clamped to `PANE_MIN` when drawn.
- **Persistence.** The workspace is stored under `jarvis.hud.workspace`,
  separate from `jarvis.hud.layout`, so damage to one never resets the other.
  It is read and written behind try/catch. Each field falls back to its own
  default, an unknown preset reads as `single`, and the panel is open only on
  a literal `true`.

**Pane headers.**
- Each pane has a view strip: the six tab words, or a `view ▾` menu when the
  pane is under 520px. Beside it is the pane's context: the thread title, the
  file path or the URL.
- A chat pane's header also carries the **profile select**, which moves off
  the global row because it belongs to that conversation's project.
- The focused pane has a 2px accent line under its header. The Graphite
  accent is reserved for focus and selection.
- File, Preview and Diff hide their tree behind a toggle below 560px.

**File and Preview panes pin their project.** They follow the focused chat's
project until they open something, then stay pinned to that project, with
`in: <project>` and a "follow chat" reset in the header. `FileTab` is keyed by
its project, so a save can never land in another one. This fixes the latent
bug above.

**Where a sidebar click goes.** Rule: a click goes to the pane that already
shows that kind of thing, and only otherwise to the focused pane.

| Click | Goes to |
|---|---|
| A thread | The pane already showing it. Otherwise, the focused pane if it shows chat. Otherwise, the voice-target chat pane. Otherwise, the focused pane switches to chat. |
| A task | It becomes selected, and the status pane follows as today. A visible task or diff pane follows it too. If none is visible, the focused pane switches to task, which in the single layout is exactly today's behaviour. |
| Alt+click, or "Open beside" in the row's `⋯` menu | The next pane to the right. From the single layout it switches to two columns (VS Code's "open to the side"). |

**Several chats at once (WP-B).** This generalises "the chat pane decides
where a message goes; the sidebar only shows it".
- **Each chat pane holds its own conversation and its own input box,** with
  its own project chip, model chip, Send/Steer and Stop. Where you type is
  where it goes, with no ambiguity to resolve. A shared box at the bottom
  would need a "which pane?" indicator and would be one keystroke away from
  the wrong thread.
- **A thread is open in at most one pane.** Clicking a thread already on
  screen focuses that pane rather than giving one thread two boxes.
- **Voice goes to the voice target: the chat pane you used last.** Its header
  carries a small mic mark. The dictation strip (AUTO / REVIEW / OFF, the
  level meter and the hint) is drawn only in that pane's input bar, so the
  strip itself shows where speech will land.
  - REVIEW puts the transcript in that box.
  - The orb's state follows that pane's turn.
  - Pressing the orb interrupts that pane's turn.
  - The follow-up listening window opens only when that pane's own turn
    finishes.
  - Capture is suppressed only while *that* turn runs. A long turn in
    another pane no longer silences the mic.
- **Turn tracking moves into the pane.** `busy`, `turnThreadId`, `status`,
  `messages`, `draft`, `ops`, `restore` and `pendingTranscript` become
  `chats[pane]`.
  - An SSE event with a `thread_id` is routed to the pane that shows that
    thread or tracks its turn.
  - Approvals, activity and lifecycle events stay window-wide, as now.
  - Stop, steer, give-back, held-back words and the 15-second busy reconcile
    all become per pane. The logic is the same, keyed by pane.
- **The sidebar shows every visible pane's conversation.** The focused one
  gets the accent and the others a dimmer mark. Each composing chat pane
  shows its own compose row, numbered by pane when there is more than one.
- **What counts as read.** A thread or task counts as read when it is visible
  in **any** pane of the drawn layout while the window is visible, not only
  when it is in the focused pane. A chat you are watching finish in a side
  pane should not turn blue.

**Fitting.** This generalises `fitLayout` into `fitWorkspace`. Only the
render changes, never what is stored.
- **Across:**
  1. side panes give back their slack (as today);
  2. the right pane folds, then the left (as today, with `prefer` honoured);
  3. only then does the window drop columns for the render: `cols3` to
     `cols2` to `single`, keeping the focused pane's column. The focused pane
     is never dropped. ⊞ shows a small badge, and its menu says "Showing 2 of
     3 panes".
- **Down:**
  1. the panel shrinks to its minimum (120);
  2. it folds for the render;
  3. rows drop (`grid4` to the focused row, `rows2` to `single`).
- Opening a panel the window folded drops rows instead. This is the vertical
  version of `prefer`.

| Window (zoomed px) | `cols2` | `cols3` | `grid4` + panel |
|---|---|---|---|
| 1920×1080 @100% | both side panes open | both open | fits |
| 1366×768 @100% | fits; side panes shrink a little for `main2` | status pane folds for the render | fits |
| 1280×800 @150% (853×533) | both side panes fold | drops to 2 columns, with the badge | the panel folds for the render (400 + 120 > 505) |
| 1024×700 @160% (640×437) | drops to 1 pane | drops to 1 pane | 1 pane, panel at its minimum |

All sizes are unzoomed px. `available` is the inner size divided by the zoom,
as now. Any new menu goes through `toCss()`, and any new `vh`/`vw` divides by
`--ui-zoom`.

**Keyboard.**

| Key | Does | Collision notes |
|---|---|---|
| Ctrl+B / Ctrl+Alt+B | Fold the sidebar or status pane (exists) | Stands aside in Monaco (exists) **and in the terminal** (readline back-char; page up in less and vim) |
| Ctrl+` | Show or hide the panel; opens a terminal if there is none (VS Code's key) | Requires no Alt, because AltGr+7 is a backtick on French layouts. Matched on `key`; where backtick is a dead key, `code` Backquote |
| Ctrl+Alt+1 … 4 | Focus pane N | AltGr+digit types `{[]}` on European layouts. That reports a symbol, not a digit, so it never fires mid-word. **There is no cycle key**, because every bracket is a character on some AltGr layout |
| Ctrl+= / Ctrl+- / Ctrl+0 | Zoom (exists) | In the terminal, Ctrl+_ (readline undo) passes through |

Every one of these is inert while a card is up, as layout keys already are.

**The approval card stays global and on top.** It is `#authveil`, fixed at
z-index 100, above every pane, the panel and any iframe.
- Its button row stays unsticky.
- The `vh` caps stay divided by the zoom.
- Pickers keep their places below it (z 90/96).
- A new check opens a 2×2 grid showing Monaco, a preview iframe, a terminal
  and chat, at 160% and at 70%. It asserts that the card is entirely on
  screen, that AUTHORIZE is below the fold for a long command, and that each
  button is the topmost element at its own point.

### 2.3 The bottom panel

The panel is the terminal's home, as VS Code's panel is.
- **Where it sits.** It spans the workspace columns, between the side panes,
  below the panes (VS Code's default "center" alignment). It holds only
  terminals: a tab per terminal, then `+` and `▾` (choose the folder), and ×
  per tab.
- **Toggling.** ⬓ and Ctrl+` toggle it. A hidden panel takes no room and has
  no rail. A dot on ⬓ shows that a terminal in the hidden panel has exited.
- **Sizing and storage.** Its height is stored with the workspace: unzoomed
  px, default 260, minimum 120. The maximum leaves the panes their minimum
  height. Its edge is a horizontal `role="separator"` (Up/Down, double-click
  to reset).
- **Terminals in panes.** Any pane can also show a terminal. **A terminal
  shows in one place at a time**: the server allows one attachment (§2.4). If
  pane 2 shows terminal B, B's tab in the panel reads "in pane 2" and jumps
  there.

### 2.4 The integrated terminal (feature 3)

**What it is.** It is your login shell, running as you, unsandboxed and not
gated by permissions: your terminal, exactly as Windows Terminal is.
- CLAUDE.md keeps terminals out of `DESKTOP_APPS` because keystrokes from the
  *agent* into a shell route around the approval gate.
- An owner-driven terminal is the opposite direction, so the same reasoning
  becomes this plan's main requirement: **the agent never gets a lever on
  this terminal.**
  - No tool creates, lists, reads or types into one.
  - No desktop app or browser route reaches it.
  - Chat code blocks get no "Run in terminal" button. That button would let
    the agent drive your shell with one click on text you may not have read.
    If one is ever wanted, it pastes without Enter.

**Transport: WebSocket on the HUD listener.**
- **SSE plus POST loses on three counts:**
  - Chrome allows 6 HTTP/1.x connections per host. Four terminal streams plus
    `/events` would leave the HUD one connection for every other fetch.
  - The daemon speaks HTTP/1.0, so each keystroke POST is a new connection,
    and two in flight can arrive out of order.
  - PTY bytes would have to be base64 inside text events.
- **WebSocket gives one ordered, binary, two-way channel per terminal.**
  WebSockets do not count against the HTTP pool.
- `BaseHTTPRequestHandler` can serve it. A GET with `Upgrade: websocket` goes
  through `_dispatch`, so `check_origin` runs first. It then gets an `owner_only`
  check and a ticket, and takes the socket over, as SSE already holds its
  thread.
- The server framing is hand-rolled in a new `jarvis/v2/ws.py`, about 150
  lines, server side only. `websocket-client`, already a dependency, is the
  test client. Rules:
  - client frames must be masked;
  - no extensions are negotiated (no compression);
  - messages are capped at 64 KiB, and the HUD sends pastes in 16 KiB chunks;
  - the server pings every 20 s, and a socket that misses pongs for 60 s is
    detached.
- Two traps:
  - the `101` status line must read `HTTP/1.1`, because the handler writes
    HTTP/1.0 by default and browsers reject it;
  - the 2-second socket timeout (`daemon.py:1424`) must be lifted after the
    upgrade.

**Routes.** All of them are `owner_only`, so they answer only on the HUD
listener and only to the HUD's Origin. None is mounted on 8403 or 8405.

| Route | Body → answer |
|---|---|
| `GET /terminals` | `[{id, title, folder, project_id, created, exited, shown}]` |
| `POST /terminals` | `{in: {"thread": id} \| {"project": id} \| {"task": id} \| "home", cols, rows}` → `{id, …}`. The folder is resolved **server side from ids, never from a path string**. 409 at the cap. |
| `POST /terminals/{id}/ticket` | `{}` → `{ticket}`: single use, 30 s, valid for this terminal only |
| `GET /terminals/{id}/attach?ticket=…` | WebSocket. Binary frames carry bytes in both directions. Text frames carry JSON control: `resize`, `exit`, `takeover_request`, `taken`, `refused` |
| `DELETE /terminals/{id}` | Closes it (see Lifecycle) |

The HUD builds the socket URL from `location.host`, never from a fixed port.
A hard-coded 8403 once sent the test suite to the live daemon.

**Who can connect, and what stops each of them.** Browsers do not apply the
same-origin policy to WebSockets, so the Origin check carries the weight.

| Who tries | What stops it |
|---|---|
| Any website open in your Chrome | **Origin is required and must equal the HUD's** (`http://127.0.0.1:8402` or `http://localhost:8402`, matching Host). A browser always sends Origin on a WebSocket, and a page cannot forge it. The page also has no ticket: tickets come from a JSON POST, and a cross-origin page cannot send one without a CORS preflight the daemon never answers. |
| DNS rebinding (`evil.example` resolving to 127.0.0.1) | Host must be literally `127.0.0.1:8402` or `localhost:8402` (existing). |
| The workshop on 8403, or the Preview frame beside the terminal | The Origin is `http://127.0.0.1:8403`, or `null` from the sandboxed frame. Both are refused. The routes do not exist on 8403. |
| A dev-server page (`localhost:5173`, possibly agent-written) | Origin mismatch. **No cookies are used**: cookies ignore ports, so any page on 127.0.0.1 could plant one. |
| Jarvis's own browser (`browser.py`, `fetch_page`) | `is_face_origin` (`config.py:70`) refuses navigating to or fetching 8402, which covers the route URLs. It does *not* see a WebSocket opened by script on some page Jarvis browses. The Origin check covers that case. |
| Any tool: the fast path, `jarvis-mcp`, the schedule tools, Claude's WebFetch | No tool names these routes. A test asserts this, in the `archive_check` pattern. WebFetch sends a GET with no Origin, so `owner_only` answers 403, and it cannot upgrade. |
| A program running as you, including a script an agent wrote and ran | **Not a boundary, and the plan does not pretend it is.** Such a program can forge Origin and fetch a ticket. Three things limit it. (1) An agent has to get it run first: `curl`/`wget` never auto-run (`permissions.py:163`), Claude workers pass the PreToolUse permit, and Codex workers have no network in their sandbox. (2) Taking over a terminal your HUD is showing asks you in that window first ("Another window wants this terminal: Let it / Keep it"; refused after 20 s). (3) `sudo` in a HUD terminal never caches (decision 5), so an injected line cannot ride a `sudo` you just typed past the rule that denies `sudo` to agents. |

**Compared with `/approve`.** `POST /approvals/{id}` refuses only a *wrong*
Origin, so a local client that sends none is accepted. The terminal is
stricter: the HUD listener, an Origin that must be present, a single-use
ticket, and the takeover question. For browser-based attacks, the ticket means
two independent checks would have to fail at once.

**Lifecycle.**

| | |
|---|---|
| Shell | Your login shell from `/etc/passwd` (bash), started with `-l`, so PATH, nvm, uv and aliases match a fresh WSL tab |
| Folder | The folder of what the focused pane shows: a chat's working folder (`Thread.cwd`, which follows a moved thread's real folder), a file or preview pane's project root, or a task's worktree if it has one. Otherwise `~`. `+ ▾` offers Home and each project |
| Environment | **A clean login environment, not the daemon's.** It gets `HOME`, `USER`, `SHELL`, `LANG`/`LC_*`, `TERM=xterm-256color`, `COLORTERM`, and the WSL interop and display variables (so `explorer.exe .` works). It gets nothing loaded from Jarvis's `.env` (no `OPENROUTER_API_KEY`), and no `HF_HUB_OFFLINE`, `VIRTUAL_ENV` or venv `PATH` (so `python` is not Jarvis's venv), and no `ANTHROPIC_*`, `CLAUDE_*` or `CODEX_*` |
| Spawn | `os.openpty()` plus `subprocess.Popen` through util-linux `setsid --ctty` (verified present at `/usr/bin/setsid`), or an exec'd helper that does the same. **Never `pty.fork()`**: the daemon is heavily threaded, and Python 3.14 warns on fork with threads. `TIOCSWINSZ` handles resize. `JARVIS_TERMINAL_SHELL` must be an absolute path (the CLI-override rule) |
| Output | One reader thread per terminal, which never waits on a browser: a slow socket is detached and catches up later. The output keeps draining, so a dev server never blocks on a full pipe |
| Cap | 6 open terminals; the seventh is refused with a sentence |
| HUD reload | **The terminal survives.** The view reattaches and replays the last 1 MiB of output (about 10k lines), trimmed to a line boundary. A resize then makes full-screen programs redraw |
| Close (×) | Sends SIGHUP to everything in the terminal's session (found by session id in `/proc`, so background jobs are included), then SIGKILL after 3 s. It asks first only when the PTY's foreground process is something other than the shell (`tcgetpgrp`) |
| Shell exits | The view reads "exited (code N)" and offers Restart (same folder) and Close |
| Daemon stop or restart | `Daemon.stop` closes every terminal the same way. **Terminals do not survive a restart** (decision 3). The view reads "Jarvis restarted; this terminal ended" and offers "New terminal here" |

**Keys inside a terminal.**
- Every key, including Space and Escape, goes to the shell, and push-to-talk
  never hears it.
- Ctrl+B stands aside, and Ctrl+_ passes through (see the keyboard table).
- Ctrl+C copies when text is selected and otherwise interrupts. Ctrl+V
  pastes (bracketed).
- **Known limit, to verify live:** Chrome may keep Ctrl+W, Ctrl+T and Ctrl+N
  for itself even in app mode. bash's Ctrl+W (delete word) might then close
  the window; Alt+Backspace is the fallback.

**While a card is up,** the terminal holds input (xterm's `disableStdin`) and
gives up focus.
- Escape then reaches the card.
- A `y⏎` typed at the wrong moment goes nowhere.
- Output keeps drawing, and focus returns when the card closes.
- This matches the input bar, which is disabled under a card (`App.tsx:1283`).

**Output is hostile bytes, so:**
- **An OSC title sets the pane header only** (as text, capped), and never
  `document.title`. The desktop bridge recognises the HUD by `J.A.R.V.I.S.`
  (`FORBIDDEN_TITLES`), so a program that could retitle the window would
  disarm that check.
- Links open only for `http`/`https`, which is the markdown rule. Loopback
  links offer "Open in Preview".
- **No clipboard addon**, so output cannot write your clipboard (OSC 52) and
  set up a malicious paste.
- xterm's `windowOptions` (window reports) stay off. Title reporting is a
  classic terminal-injection route.

**The front end.**
- Pin `@xterm/xterm` **6.0.0** (published 2025-12-22) and
  `@xterm/addon-fit` **0.11.0** exactly, and optionally
  `@xterm/addon-web-links` 0.12.0. Pin exactly because this is the one
  front-end package that parses hostile bytes.
- Lazy-load them, as Monaco is, and theme them from the Graphite tokens
  (cursor `#6fc3df`).
- **Main risk: xterm under CSS `zoom`.** xterm measures character cells and
  mouse positions itself, and `zoom` on `#root` confuses both. The plan
  counter-zooms the terminal host (`zoom: calc(1 / var(--ui-zoom))`) and
  scales xterm's own `fontSize` by the zoom. Text then matches the HUD's
  size, and xterm works in unzoomed space. The 160% and 70% checks guard
  clicking to place the cursor, selection, and `fit`.

**Logging and secrets.**
- Terminal output exists in only two places: the in-memory ring and your
  browser. It never goes to the bus, a thread log, the decision log,
  Discord, disk, or the daemon log.
- The daemon log records only lifecycle lines: opened or closed, an exit
  code, and the folder as a project name or `~`.
- **Terminals are not on the bus at all.** The HUD lists them with
  `GET /terminals`, and exit arrives over the socket. Nothing an observer or
  Discord watcher reads can ever see one.

**Can Jarvis read a terminal?** This is open decision 2. The options:
- **A (recommended): no tool.** A "Send selection to chat" button puts the
  selected text into the voice-target chat's box, **unsent**. You see
  exactly what goes before you press Send.
- **B: a read-only `terminal_read` tool.** It would return at most the last
  200 lines of one terminal, ANSI-stripped, passed through `secrets.scrub`,
  fenced as untrusted data, and optionally behind an approval card that shows
  the text first.
  - The cost: output is untrusted. A dev server's log can carry a
    prompt-injection string from any request it served.
  - `scrub` removes values from protected files and attributed lines. It does
    not catch an arbitrary token a CLI printed (`gh auth token`, a cloud
    login).
  - That is the 2026-07-30 lesson: the leak was a read that never named the
    file.
- **Writing stays never, under both options.**

### 2.5 Preview on a dev server

1. **Discoverability.** A `localhost` link in the terminal offers "Open in
   Preview". It goes to a visible Preview pane, or else to the focused pane,
   by the click rule. The URL is stored per pane and judged again on reload.
2. **Refuse the API port too.** `/status` gains `api_port`, and
   `judgePreviewUrl` refuses it alongside 8402. The workshop on 8403 stays
   allowed. Today, 8405 frames a copy of the HUD in a null-origin frame. That
   is harmless but pointless.
3. **Frame hardening, which is free and independent.** Every response on the
   HUD and API listeners gets `Content-Security-Policy: frame-ancestors
   'none'` and `X-Frame-Options: DENY`. A framed page that redirects or
   navigates itself to 8402 then gets a blocked frame instead of a working
   copy of the approval surface.
4. **Why dev apps break in Preview today.** Without `allow-same-origin`, the
   frame's origin is opaque, so `localStorage`, cookies and IndexedDB throw.
   Decision 4 proposes a per-pane "Let this page keep its own origin" toggle.
   - It is off by default and offered only for loopback ports other than the
     daemon's three. It turns itself off when the port changes, and it ships
     only after item 3.
   - Why it is safe then: scripts plus same-origin together amount to no
     sandbox only when the framed page is *same-origin with the HUD*. The URL
     check refuses 8402 up front, and `frame-ancestors` stops a frame from
     becoming 8402 later. A page on another origin cannot touch the parent.
   - The frame still never gets `allow-top-navigation`, popups that escape
     the sandbox, or downloads.
5. **Later, if wanted:** a "Dev servers in your terminals: :5173" list, built
   from the listening sockets owned by terminal sessions.

## 3. Work packages

Each work package is a separately mergeable PR with its own free tests. The
order is A, then B and C in parallel, then D, then E. E's item 3 can land at
any time.

| WP | What | Size | Risk |
|---|---|---|---|
| **A** | Title bar, layout toggles, presets, pane model (still one chat) | L | Medium |
| **B** | Several chat panes and the focus rules | L | **High** |
| **C** | Terminal backend | M | Medium–high |
| **D** | Terminal panel and terminal view | M | Medium |
| **E** | Preview and dev servers | S | Low |

**WP-A: title bar, toggles, presets, pane model** (frontend only)
- **Files:**
  - new `lib/workspace.ts` and `lib/workspace.test.ts`; `lib/layout.ts`
    (`fitWorkspace`, vertical `Splitter` keys, Ctrl+Alt+1–4);
  - `components/Layout.tsx` (Splitter axis, `TitleBar`, `LayoutMenu`, toggles
    and rail buttons disabled while blocked);
  - new `components/Workspace.tsx` (panes and headers);
  - `App.tsx` (the render moves into panes; Model/Voice/Avatar/Settings and
    zoom move into the title bar; the profile select moves into the chat
    pane header);
  - `FileTab.tsx` (keyed by its pinned project, tree toggle), `DiffTab.tsx`,
    `PreviewTab.tsx` (URL kept per pane), `theme.css`.
- **Scope:**
  - The chat view stays a singleton in this package. Choosing chat in another
    pane swaps the two panes' views.
  - Seen generalises to "visible in a drawn pane".
- **Tests:**
  - `workspace.test.ts`: parsing garbage per field, the preset table, the
    four worked windows, the drop order, that the focused pane is never
    dropped, vertical precedence.
  - `layout.test.ts`: the new keys, and that AltGr symbols never match.
  - `hud_v2_layout_check.py` gains `_titlebar_checks`, `_workspace_checks`
    and the 2×2 card-on-top check.
  - **Every existing suite passes unchanged in the single layout.** That is
    the acceptance bar.
- **Risk:** a large re-render of `App.tsx`. Mitigation: single is the
  default and must render identically.

**WP-B: several chat panes, voice target, click rules** (frontend only)
- **Files:**
  - `state/store.tsx` (`chats[pane]`, `voiceTarget`, pane-scoped actions);
  - `App.tsx` (`send`, `stopTurn`, `reconcileBusy`, SSE routing per pane,
    capture suppression and the follow-up window from the voice target,
    give-back and held-back per pane);
  - `lib/compose.ts` (per-pane project, `activeProjectId` becomes the focused
    pane's);
  - `components/Sidebar.tsx` (one highlight per visible pane, numbered
    compose rows, Alt+click, "Open beside");
  - `InputBar.tsx` (dictation strip only on the target).
- **Tests:**
  - `store.test.ts` and `compose.test.ts` per pane;
  - a new `tests/face/hud_v2_multichat_check.py`:
    - two threads side by side;
    - a turn running in A while typing in B;
    - every event kind landing in the right pane;
    - A's Stop not touching B;
    - voice going to the target;
    - a thread never open twice;
    - seen for both visible panes;
    - the steer and give-back cases re-run in pane 2.
- **Risk: high.** This touches the turn state machine, which took several
  bug-fix rounds (steer, give-back, reconcile). It should land on its own,
  after A.

**WP-C: terminal backend**
- **Files:**
  - new `jarvis/v2/ws.py` and `jarvis/v2/terminals.py`;
  - `hud_api.py` (routes, `owner_only` plus ticket);
  - `daemon.py` (`Daemon.stop` closes terminals; the upgrade path lifts the
    2 s timeout);
  - `config.py` (`JARVIS_TERMINAL_SHELL`);
  - `docs/hud-api.md`.
- **Tests:** a new free suite, `tests/v2/terminal_check.py`. It uses a real
  PTY with `JARVIS_TERMINAL_SHELL` pointed at a scripted fake shell, a temp
  HOME, and ephemeral ports.
  - **Handshake refusals:** no Origin, wrong Origin, `null`, the 8403
    Origin, wrong Host, the API listener, and a ticket that is missing,
    expired, reused, or for another terminal.
  - **Frames:** unmasked and oversize frames refused, fragments reassembled,
    ping/pong, the HTTP/1.1 status line.
  - **Lifecycle:**
    - the cap of 6;
    - takeover asks, and refuses after 20 s;
    - the ring replays in order and stays capped;
    - resize reaches the child (`stty size`);
    - close kills a background job in the session;
    - `Daemon.stop` kills every terminal.
  - **Environment:** checked by name only, never printing values. No
    `OPENROUTER_API_KEY`, `HF_HUB_OFFLINE` or `VIRTUAL_ENV`.
  - **Leaks and reachability:**
    - a bus subscriber sees **no record** during a whole session;
    - the daemon log holds no output bytes;
    - no tool, MCP tool or fast-path tool reaches `terminals`, in the
      `archive_check` pattern.
- **Risk:** new protocol code and process management, in an isolated module.

**WP-D: terminal panel and terminal view**
- **Files:**
  - `package.json` (pinned xterm);
  - new `components/Terminal.tsx`, `components/Panel.tsx`,
    `lib/terminal.ts` and its test;
  - `Workspace.tsx` (the terminal view);
  - `Layout.tsx` (the ⬓ button, Ctrl+`, the terminal stand-aside rules);
  - `App.tsx` (hold and blur terminals while blocked);
  - `api.ts`, `theme.css`;
  - `tests/face/hud_v2_mock.py` (the `/terminals` HTTP routes in its world).
- **Tests:**
  - `terminal.test.ts`: the URL comes from `location`; pastes are chunked;
    control messages are parsed; links are judged.
  - A new `tests/face/hud_v2_terminal_check.py`. The PTY is **faked by
    Playwright's `ctx.route_web_socket`**, so no shell ever runs and HOME is
    never touched. It covers:
    - typing round trips;
    - Space never firing push-to-talk;
    - Ctrl+B reaching the shell;
    - keys held under a card, with Escape denying;
    - takeover UI;
    - exit UI;
    - reattach after reload replaying the ring;
    - an OSC title never changing `document.title`;
    - a `javascript:` link inert;
    - fit and selection at 160% and 70%.
  - **`hud_v2_check.main()` also extends the live-port guard to WebSockets**
    (`route_web_socket` on `wss?://…:(8402|8403|8405)/`), because `ctx.route`
    never sees them.
- **Risk:** xterm under CSS zoom (see §2.4).

**WP-E: Preview and dev servers**
- **Files:**
  - `hud_api.py` and `daemon.py` (`frame-ancestors`, `X-Frame-Options`,
    `/status.api_port`);
  - `lib/preview.ts` and its test (refuse 8405; the keep-origin judge);
  - `PreviewTab.tsx` (the toggle, if decision 4 is yes; "Open in Preview"
    from terminal links).
- **Tests:**
  - `hud_backend_check.py`: both headers on every HUD and API response,
    `api_port` reported.
  - `preview.test.ts`.
  - `hud_v2_check` preview section: the `sandbox` attribute exactly as
    expected with the toggle off and on, and the toggle absent for the
    daemon's ports.
- **Risk:** low.

**Optional WP-F (only if decision 2 is B):** `terminal_read` behind the gate,
with the scrub, the cap and the untrusted fence, and an `archive_check`-style
test proving it cannot write.

Every package updates `docs/hud-api.md`, design §18 and CLAUDE.md in the same
PR.

## 4. Open decisions for the owner

1. **Where the terminal lives.** I recommend both: the bottom panel is its
   home (⬓, Ctrl+`), and any pane can show a terminal too, though one
   terminal shows in only one place at a time. The alternatives are the panel
   only (simplest, pure VS Code) or panes only (no panel, no ⬓, and a third
   pane spent on the terminal).
2. **Can Jarvis read a terminal?** I recommend no tool, and a "Send selection
   to chat" button that fills the box unsent. The alternative is a read-only
   `terminal_read` tool (last 200 lines, scrubbed, untrusted, optionally
   asking each time). It answers "what's that error?" without copy-paste, but
   terminal output is untrusted and can hold tokens the scrub doesn't know
   about.
3. **Terminals that survive a Jarvis restart (tmux).** I recommend no. With
   no, dev servers stop when the daemon restarts. tmux would keep them, but
   any program running as you, an agent's shell command included, could
   `tmux send-keys` into them with no Origin check at all, and tmux's Ctrl+B
   prefix fights the HUD's.
4. **Let dev apps in Preview keep their own origin.** I recommend yes, opt-in
   per pane, and only after `frame-ancestors` lands. Apps that use storage,
   cookies or HMR start working. The safety then rests on the port check and
   `frame-ancestors` holding.
5. **`sudo` never caches in HUD terminals** (`alias sudo='sudo -k'` in the
   terminal's startup). I recommend yes. Every `sudo` there asks for your
   password. Without it, a line injected by a local program could reuse a
   `sudo` you just typed.

Defaulted; say if you want otherwise:

6. Six presets (single, two columns, two rows, three columns, one plus two,
   2×2), not free splitting.
7. A window-wide 28px title bar holds the toggles, zoom and Model, Voice,
   Avatar and Settings, rather than the status pane's header, which
   disappears when that pane is folded.
8. One input box per chat pane. Voice goes to the chat pane you used last,
   marked in its header.
9. A thread is open in at most one pane.
10. "Read" means visible in any pane of the layout, not only the focused one.
11. A narrow window folds the side panes first (as today), then drops panes
    for the render, never the focused one. ⊞ says when it has done so.
12. The terminal runs your login shell in a clean environment, in the folder
    of what the focused pane shows. At most 6 are open, each keeps 1 MiB of
    scrollback in memory only, and output is not logged anywhere.
13. Taking over a terminal another HUD window is showing asks in that window
    first.
14. The WebSocket is hand-rolled, about 150 lines, rather than a new
    dependency (`wsproto`).

## 5. Verification

**Free suites, extended per package:**
- `cd hud && npx vitest run && npm run build`, covering `workspace.test.ts`,
  `layout.test.ts`, `store.test.ts`, `compose.test.ts`, `terminal.test.ts`
  and `preview.test.ts`.
- `uv run python tests/face/hud_v2_check.py`. It runs `hud_v2_layout_check`
  first, then the new multichat and terminal sections, with the live-port
  guard now covering WebSockets.
- `uv run python tests/v2/terminal_check.py`, `hud_backend_check.py`, and
  `archive_check.py` (the no-tool pattern).

**Test IDs the headless suites drive:**
- **Title bar:** `titlebar`, `layout-customize` (`aria-expanded`),
  `layout-menu`, and `layout-preset-{single,cols2,rows2,cols3,main2,grid4}`
  (`aria-checked`). Also `layout-reset` and `layout-dropped`.
- **Toggles:** `toggle-left`, `toggle-panel` and `toggle-right`
  (`aria-pressed`, plus `data-auto="true"` when the window folded the area).
  `zoom-control` keeps its id in its new place.
- **Workspace:** `workspace` (`data-preset`, `data-drawn-preset`).
- **Panes:**
  - `pane-1` … `pane-4` (`data-view`, `data-focused`, `data-voice`);
  - `pane-N-view`, with the existing `tab-<view>` ids scoped inside each
    pane (pane 1 alone in the default layout, so old selectors still match
    once);
  - `pane-N-project`;
  - the separators `split-col-1`, `split-col-2` and `split-row`.
- **Panel:** `panel` (`data-open`), `panel-split`, `panel-new`,
  `panel-new-menu`, `panel-tab-<id>`, `panel-close-<id>`.
- **Terminal:**
  - `terminal-<id>` (`data-state`: `attached`, `taken`, `exited` or
    `ended`);
  - `term-takeover`, `term-let`, `term-keep`;
  - `term-exited`, `term-restart`;
  - `term-send-selection`, if decision 2 is A.
- **Preview:** `preview-open-port`, and `preview-keep-origin` if decision 4
  is yes.

**By hand, live (`jarvis hud`):**
1. Choose two columns with chat on the left and Preview on the right. Ask
   something and scroll the preview while the answer streams.
2. At 160% on the laptop, open ⊞. The toggles show the right states. Fold
   the status pane with ◨ and check that zoom is still reachable.
3. Press Ctrl+` to open a terminal in the project root. Run `npm run dev` in a
   scratch project. Click its `Local:` link, choose "Open in Preview", then
   edit a file and watch it reload (with keep-origin, if chosen).
4. Reload the HUD. The dev server's output is still there, and the server is
   still running.
5. Raise an approval (a Claude thread in an `ask` project) while typing in
   the terminal. Keys stop, Escape denies, and focus comes back.
6. Open a second HUD window. The first window asks "Let it / Keep it".
7. In the terminal, run `env | cut -d= -f1 | grep -iE 'openrouter|hf_hub|virtual_env'`.
   It prints nothing. Check names only; never print values.
8. Run `sudo true` twice. It asks both times, if decision 5 is yes.
9. Restart the daemon. The terminal reads "ended", and `pgrep -f vite` finds
   nothing.
10. In an ordinary Chrome tab on any site, run `new WebSocket("ws://127.0.0.1:8402/terminals/x/attach")`
    in DevTools. The handshake fails.
11. (After WP-B) Open two chats side by side. Start a long turn in A, then
    talk to B by voice. B gets the words, and A keeps running and keeps its
    Stop.
12. In a 1366-wide window, choose three columns. The status pane folds for
    the render, and its rail still shows approvals.
