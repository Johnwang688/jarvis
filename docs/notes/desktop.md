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
