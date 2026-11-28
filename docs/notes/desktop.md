# Desktop — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 8632–8688

<!-- claude-source:008632-008688:begin -->
**Desktop control shipped (2026-07-31)** — Jarvis can drive Windows apps.
`jarvis desktop setup` (human-only) builds a Windows-Python venv at
`C:\Users\johnw\.jarvis-bridge` and writes `run-bridge.cmd`; the owner
starts that, and `windows/bridge.py` **dials into** WSL on port 8404. It
dials out rather than listening because WSL2 forwards `localhost` from
Windows inward, so that direction needs no firewall exception and no address
discovery — the WSL gateway IP changes every boot. Tools mirror the browser's
discipline exactly: `desktop_open` / `desktop_snapshot` give a ref-tagged
accessibility tree, `desktop_click` / `desktop_type` / `desktop_key` act by
ref, `desktop_screenshot` is the vision channel. Confinement is the app
allowlist (see *Safety design*). Validated live on both registered apps:
Settings navigated by ref with readback, Claude Desktop read in full, and
`jarvis ask` completing a real question end-to-end (7 steps, $0.0010).

Five findings from getting there, each of which cost a debugging round:

- **Chromium/Electron publishes its tree over MSAA, not UIA.** Over UIA a
  Claude Desktop window bottoms out at an empty `DocumentControl` next to a
  "Chrome Legacy Window" stub; the same window over MSAA/IAccessible yields
  the entire UI. It *also* needs `--force-renderer-accessibility` at launch
  or the renderer tree stays off whichever API you ask with. Both halves are
  required. Claude is an MSIX/Store package, so the flag has to go through
  `IApplicationActivationManager::ActivateApplication` — the exe under
  WindowsApps is ACL'd and `explorer.exe shell:AppsFolder\…` silently drops
  arguments.
- **Chromium invalidates that MSAA root as it rebuilds its tree**, and the
  dead pointer does not raise — it reports zero children forever. Anything
  that polls has to re-fetch the root each time; `take_snapshot` also retries
  once on an empty result, because the failure mode is a *short* snapshot,
  not an error.
- **UWP suspends when it loses the foreground** and its tree collapses to
  nothing, so every read activates the window first. `SetForegroundWindow`
  alone is a silent no-op from a background process — it needs the
  `AttachThreadInput` dance — and believing it worked produced a screenshot
  of Settings that was actually a picture of Claude Desktop with Settings'
  ref badges drawn on it. Screenshots, synthetic keys, and coordinate clicks
  now hard-require a verified foreground; pattern-based reads and clicks do
  not need one.
- **"Has children" is not readiness.** A suspended UWP window still reports
  its frame children, so the first readiness check passed instantly and
  captured six lines of window chrome. Readiness now means a tree that
  clears a floor *and* stops changing. Relatedly, Settings is responsive and
  at a small width **removes** the nav list rather than reflowing it, so the
  window is maximized for a predictable tree — the desktop equivalent of
  pinning a browser viewport. And a UWP app is two windows: the
  ApplicationFrameWindow owns position and z-order while a
  `Windows.UI.Core.CoreWindow` owns the content, and Windows moves the
  second between nested and top-level *while the app runs* — so the window
  we activate and the window we read are resolved separately.
- **Snapshot wording changes answers.** Windows 11 switches are Buttons
  carrying TogglePattern, so reading toggle state only from checkboxes left
  every switch stateless and Jarvis answered "Bluetooth: off" about a radio
  that was on. Exposing it as `button "Bluetooth" checked=true` was still
  misread — "checked" on a button reads as "pressed". Rendering it as
  `switch "Bluetooth" ON` fixed the answer with no prompt change. Selection
  is now a separate word from checkedness, and only printed when true.

<!-- claude-source:008632-008688:end -->

## 2026-11-19 — Voice hand-off during desktop driving (CU-3d)

While a chat holds the desktop-driving grant, addressed speech goes to that
chat with its driving instructions and skips fast routing and local Windows
shortcuts. Ctrl+D still dictates. On a paired voice turn the granted chat can
use the window tools, including screenshots when its model supports images.
Approvals still appear in the HUD. If the voice wait times out, the helper
says “Still working. Check the driver chat.” The turn continues until it
finishes or the owner stops it with the badge or physical Esc. When the
driver chat is busy, a spoken request waits for its own turn and reply.

## 2026-11-28 — v3: an agent thread looks at the front window (`jarvis computer`)

Owner decisions (2026-11-28). The driver is an agent thread that has a shell, so
the tools are a command line and not an MCP server. The v3 daemon does the work
and holds the permission. The commands act on the window in front; there is no
window argument. Clicks get no approval card: the limits are the window guard and
written instructions. The agent app's own window may be driven like any other.

What exists after this change:

- **The v3 daemon owns the bridge listener.** `Daemon.start()` opens
  `DesktopSession(managed=True)` only when `JARVIS_DESKTOP_DRIVE=1` is set for the
  daemon, and `Daemon.stop()` closes it. A managed session accepts the paired
  bridge only and never opens its listener because someone asked for status. An
  older `jarvis desktop status` run beside it says another Jarvis process holds
  the port.
- **A driving code.** `jarvis computer start` asks the daemon for a one-time code.
  It lives in the daemon's memory only: a new `start` replaces it, `jarvis computer
  stop` or a daemon stop ends it, and switching `JARVIS_DESKTOP_DRIVE` off ends it.
  The code is never logged. A thread passes it as `JARVIS_COMPUTER_CODE` or `--code`.
- **`jarvis computer screenshot`** asks the bridge for its window list, takes the
  one row marked `front` (drivable or see-only; a protected front window gives no
  picture), and saves the PNG. If the owner switches windows while the picture is
  taken, the bridge refuses it instead of returning the earlier window. The default folder is
  `<system temp>/jarvis-computer-<uid>/`, private to the user, newest 20 kept;
  `--out PATH` writes a new file there and refuses an existing one. It prints the
  path, the window's program and title, and the picture's size.
- **Bridge.** `Guard.windows()` rows carry `front`. Every call from the daemon is
  paired, carries one random scope per driving code, and passes
  `hud_sensitive=None`, so Jarvis's own window stays protected.

Found on real Windows the same day (the owner ran `tests/v3/window_act_smoke.py`,
which starts its own bridge on a private port and acts only on a Notepad window
showing its own file):

- A click by ref failed in every window: the `uiautomation` function is
  `ControlsAreSame`; the name the bridge called does not exist.
- Windows applies the mouse setting "Scrolling direction" to injected wheel input
  too. The bridge now reads it and turns the wheel so a positive amount always
  scrolls up.
- After both fixes, click by ref, click by point and the wheel both ways passed.
- Notepad's text area gets no ref and typing is ref-only, so a document body
  cannot be typed into yet.

Clicking, scrolling and switching windows (same day, the next change):

- **`jarvis computer click X Y`** and **`scroll X Y AMOUNT`** take a point in the
  latest screenshot of the same driving code. The daemon asks the bridge which
  control is at that point, then sends the input with that control as the
  expectation, so the bridge refuses when something else is there by then. A
  failed screenshot, a new driving code, and a successful `front` all require a
  new screenshot first. A negative amount scrolls down.
- **`jarvis computer windows`** lists the open windows by number, with the front
  one and view-only ones (terminals) marked. The bridge's window ids stay in
  the daemon. **`jarvis computer front N`** brings a window of the latest list
  forward through the bridge op `window_front`: a protected window is never
  raised, held keys or mouse buttons refuse it, and the window is restored only
  if it was minimized and never resized. The bridge does not list minimized
  windows, so in practice they cannot be brought back this way yet.
- **The action log.** Before any click, scroll or `front` reaches the bridge the
  daemon appends a start line to `computer_actions.jsonl` in its data directory
  (private file, no links followed): the action, the point, the control's name
  and role, the window's program and title. The outcome line follows: `done`,
  `refused` (the bridge said no, nothing happened) or `failed` (it may or may
  not have happened). If the start line cannot be written, nothing is sent. The
  driving code, pictures and window ids are never written.
- **No approval card** (owner decision). The limits are the window guard, the
  action log and the written instructions that `jarvis computer guide` prints
  (`jarvis/v3/prompts/computer_guide.md`): what the driving thread must not do
  unless the owner asked for exactly that, and that words on the screen are
  content, not instructions.
- **Not there yet:** typing and key presses, right or double clicks and drags, an
  on-screen border with a Stop control, and a chat tool that starts the driving
  thread. The owner starts driving with `jarvis computer start` and gives the
  thread the code.
- `tests/v3/computer_live_smoke.py` is an owner-run check on real Windows. It
  starts its own bridge on a private port, builds its own driving permission in
  its own process and acts only inside a Notepad window showing its own file; it
  also reports whether a point inside an open menu can be reached.

State: built and tested with fakes; the daemon routes and the commands have not
yet been run against a real bridge, and the owner has not yet run the new check.
