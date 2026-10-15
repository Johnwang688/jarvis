# VEX view redesign (plan, 2026-10-15)

Status: **plan only**. No product code. Mockup:
[`2026-10-15-vex-ui-mockup.html`](2026-10-15-vex-ui-mockup.html) (static, the
real Graphite tokens from `hud/src/theme.css`, nine states).

Reads: `CLAUDE.md`, `docs/notes/vex.md` (all sections),
`docs/plans/2026-10-13-vex-download-device-status.md`,
`docs/plans/2026-10-09-hud-editor-plan.md` §2.9 and decisions V-1..V-5,
`hud/src/components/VexView.tsx`, `hud/src/lib/vex.ts`, `hud/src/state/vex.ts`,
`jarvis/v2/vex.py`, `jarvis/v2/terminals.py`, the VEX routes in
`docs/hud-api.md`, `tests/face/hud_v2_vex_check.py`.

## 0. The owner's brief (2026-10-15)

- The view is **too crowded**. Minimalist, provided that what each control
  does and the current state are clear. Hover help explains each control the
  first few times. After that, the buttons are direct.
- The lab loop is **edit → Clean → Build → Download → Run**. Output and the log
  stay visible. They are the state display that makes debugging direct.
- VEX stays **its own pane**, in Graphite like the rest of the HUD.
- Bring the **brain's printed output** (user-program `printf`/`print`) into
  the HUD.
- Is **Run (start/stop the program on the brain)** possible and safe?
- **Simulator**: should it use the Browser pane or get its own tab? Recommend one.
  Reworking the simulator itself is out of scope.

## 0.1 Owner decisions on this plan (2026-10-15)

The owner approved the layout ("your redesign layout is good") and answered §13:

1. **No Run button.** The owner starts programs from the robot. VX-UI-5's Run
   is dropped and V-3 stands ("starting stays at the robot").
2. **The Download button's text shrinks** to "Download to slot N"; the
   "replaces whatever is in slot N" sentence moves to the hover help, the
   screen-reader description and the slot popover.
3. **Explicit Clean, Build, Rebuild and Download buttons all stay visible** in
   the action bar (Rebuild is not hidden in ⋯). **Download cleans and rebuilds
   first**, then downloads, as one chain shown step by step in the status line
   and the Build/Download tabs; a failed clean or build stops the chain before
   anything reaches the robot.
4. **Simulator: the existing Browser (Direct tab beside the VEX pane), no VEX
   tab.** The simulator inherits every Browser feature as the Browser grows; no
   VEX-specific copy of those features.
5. **The Brain tab connects only when the owner clicks Connect** — never
   automatically when the tab is shown (owner's answer, 2026-10-15; Q5's
   recommendation). VX-UI-1's placeholder Brain tab says so and keeps today's
   "Open the brain terminal" click (it opens in the bottom panel until
   VX-UI-4a).
6. **Stop cancels whatever VEX action is running** — pressing it during a
   download stops the download (likewise a running clean or build). It is not
   a "stop the program on the brain" control (no `vexcom --stop`).
   Implementation (lead's default, VX-UI-1, #209): Stop takes a trusted mouse
   click **or a trusted Enter/Space** on the focused button (it is in the Tab
   order, since stopping is the safe direction); a script's `.click()` or a
   dispatched event does nothing. A view's Stop acts on its own project only,
   never another project's chain. What the slot holds after a mid-download
   Stop is unknown (H9, H10 below), and the view says so.
7. **Hover help never retires.** Every control keeps its hover explanation
   permanently (for refreshers months later, and for a teammate using the PC);
   the `?` mode stays too. The first-use counts in §2.6 / VX-UI-3 are dropped.

## 1. The problem: what the view shows today

From top to bottom, `VexView.tsx` draws 22 visible things before any output.
Three of them, the slot, the Download action and the brain/controller reading,
are shown twice.

| # | Element (testid) | What it is for | Verdict |
|---|---|---|---|
| 1 | Chain bar (`vex-chain`, `vex-chain-cancel`) | The window's one Download chain, shown in every VEX view, with Cancel until the download is sent (M1) | **Merge** into the status line |
| 2 | Card: name, language, slot, SDK (`vex-card`, `vex-name`, `vex-language`, `vex-slot`) | Which robot project this pane is pinned to | **Keep**, as a one-line header |
| 3 | Card USB badges (`vex-usb-badges`) | Brain/Controller presence without scrolling | **Keep** in the header. It becomes the only always-visible reading |
| 4 | "Not on a Windows drive" note (`vex-not-windows`) | Explains why a C++ build cannot run | **Keep**, shown only when it applies |
| 5 | Setup box (`vex-setup`, command, Check now) | Says that `jarvis vex setup` has not been run | **Keep**. It replaces the action bar while it applies |
| 6 | Status error (`vex-status-error`) | `/vex/status` failed | **Move** into the status line |
| 7 | BUILD heading | Section label | **Remove**: the action bar names the actions |
| 8 | Build / Rebuild / Clean buttons | The three make actions | **Keep** Clean and Build. **Move** Rebuild to the ⋯ menu (Clean → Build is the owner's loop) |
| 9 | Quick Download in the build row (`vex-download-quick`) | Download without scrolling to the pickers | **Merge**: one Download button |
| 10 | Build state + spinner + elapsed (`vex-build-state`, `vex-build-elapsed`) | The last build's result | **Move**: a state mark on the Build button, with detail in the status line |
| 11 | Show terminal / Check status (`vex-build-output-btn`, `vex-build-recheck`) | Full output; recovers lost monitoring | **Move** into the Build output tab's toolbar |
| 12 | Waiting text, build output preview (`vex-build-output`) | Live make output | **Move** into the Build output tab |
| 13 | Errors list (`vex-errors`) | Click an error to open its file and line | **Keep**, directly under the status line, and only when there are errors |
| 14 | DOWNLOAD heading | Section label | **Remove** |
| 15 | Slot select + Name input (`vex-slot-pick`, `vex-name-input`) | Where the program goes and what the brain calls it | **Move** into a slot chip `Slot 3 · override ▾` that opens a small popover |
| 16 | The full Download button (`vex-download-btn`) | The owner's click that sends code to the robot | **Keep**: it becomes the one Download in the action bar |
| 17 | Download state, elapsed, Show terminal, Check status, progress bar, output, "✓ In slot N" | Download feedback | **Move** to the status line (result and progress) and the Download tab (output and controls) |
| 18 | DEVICE CONNECTION: Tooling, Brain:, Controller:, Refresh, note, "USB presence only" disclaimer, `<details>` log | USB diagnostics | **Move** into the Device tab. The log is no longer hidden behind `<details>` |
| 19 | BRAIN: Open/Show brain terminal (`vex-brain-btn`) | The user port, both ways | **Move** into the Brain tab, which shows the output in the pane (§5) |
| 20 | Read device info / List programs (`vex-info-btn`, `vex-programs-btn`) and their `<pre>` | `vexcom --json` and `--programs` | **Move** into the Device tab |
| 21 | Run note (`vex-note`) | Chain outcome sentences | **Move** into the status line |
| 22 | Unclaimed device-action notices (`vex-device-notice`) | A device action this window did not start (forged-Origin canary) | **Keep where it is.** It stays above everything, in red, and is never folded away |

What makes the current view crowded:

- **Six section headings and three duplicated readings.**
- **Results scattered by section.** The Build result is in the Build row, the
  Download result is near the bottom, and the USB reading is in both the card
  and the device section. To learn what happened, the owner has to scroll.
- **Output has no fixed home.** Two `<pre>` blocks are capped at 220 px,
  device info is a third, and all of the brain's output sits in the bottom
  panel.

## 2. Proposed layout

Below is one pane, top to bottom. Every element below the header uses the
existing `.vexview` padding and Graphite tokens. No new colour is introduced.

```
┌ pane header (unchanged): Chat · Files · Browser · Terminal · [VEX] ·  override ▾ ┐
│ ⚠ device-action notice (only when one is unclaimed, red, as today)               │
│ override-jarvis  C++  (Slot 3 · override ▾)              ● Brain  ● Controller  ? │  header
│ [Clean ✓] › [Build ✓] › [Download to slot 3 ●] › [Run slot 3]  [■ Stop]        ⋯ │  action bar
│ ● Downloading override to slot 3 ▓▓▓▓▓▓▓░░░ 70%  · 9s   (Cancel only before sent) │  status line
│ ✗ error  src/main.cpp:42:7  'chassis' was not declared …   (only after a failed build) │
│ ┌ Build ✓ │ Download ● │ Brain ◌ paused │ Device ● ───────────────── ⤢  ▾ ┐     │
│ │ Device: COM3                                                            │     │  output
│ │ vexcom utility V1.0.2b2                                                 │     │  (flex: 1)
│ │ download :                                                              │     │
│ │ 0....10....20....30....40....50....60....70                             │     │
│ └─────────────────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Header (one line)

- Shows the project name, then `C++` muted (the SDK goes in its title), then the
  **slot chip** `Slot 3 · override ▾`, and the `● Brain ● Controller` badges on
  the right. The slot chip sits in the header, not the action bar: the slot is
  where the program goes, a fact about the target. This also keeps the action
  bar on one row at a typical 600 px pane.
- The badges are today's `UsbBadges`, with the same labels, colours, stale
  dimming, aria-labels and titles.
- A `?` toggle turns on help mode (§2.6).
- The badges are also a button. Clicking one opens the Device tab. It never
  refreshes or probes anything by itself.

### 2.2 Action bar: the loop as four connected steps, plus Stop

| Control | Label | What a click does | State mark |
|---|---|---|---|
| Clean | `Clean` | `vex.build(pid, "clean")` (unchanged) | ✓ / ✗ / spinner |
| Build | `Build` | `vex.build(pid, "build")` (unchanged) | ✓ / ✗ / spinner / stale |
| Download | `Download to slot 3` | The unchanged chain: pause brain → build-first/rebuild → download → resume brain. **Explicit click only** | ✓ / ✗ / spinner / ? unknown |
| Run | `Run slot 3` | **VX-UI-5 only; pending V-3 and the hardware checks (§6)** | ✓ / ✗ / spinner |
| Stop | `■ Stop` | **VX-UI-5 only** | spinner while sending |
| Slot chip (header) | `Slot 3 · override ▾` | Opens a popover with today's slot `<select>` and name `<input>` and the warning sentence "Download to slot 3 replaces whatever is in slot 3 on the brain." | turns red when the name would be refused |
| ⋯ | | Opens a menu with: Rebuild (`make -B`), Read device info, List programs and Show terminal for the latest build/download. These are the rare actions | |

- **The steps are buttons joined by `›`**, so the bar reads as the loop.
- Each button carries a small state mark (§3) at its top-right corner. The mark
  is a glyph plus colour, never colour alone. It is drawn as a **sibling** of
  the button, not inside it, so `button:disabled`'s 0.4 opacity never dims the
  result while a chain holds the controls.
- At a 600 px pane the bar is one row (measured in the mockup). At narrow
  widths it wraps after Run, and Stop and ⋯ move to the second row. The 375 px
  fit check stays (§10).
- **There is one Download button.** The full button and the quick button merge
  into one, which keeps every guard of both (§4).
- Its visible label is `Download to slot N`, the label the quick button
  already used. The longer "replaces whatever is in slot N on the brain" is
  kept in three places:
  - the button's `aria-description` and `title`;
  - the first-use help;
  - the slot popover.

  Today the full button's visible text is that sentence (plan §2.9). That
  wording was set in an owner-approved plan, so shortening it is
  **open question Q2**.

### 2.3 Status line (one line, `role="status"`, `aria-live="polite"`)

- Shows the **most recent thing that happened, in words**, with its elapsed
  time.
- It absorbs the chain bar, the build line, the download line, `run.note`,
  `buildNote`/`downloadNote`, `statusError`, `brainNote`/`infoNote`, and the
  stale-status "Check status" control.
- What it shows, in priority order:
  1. A running chain: `chainLine(c)` plus the progress bar plus Cancel while
     `chainCancellable`. It keeps `data-testid="vex-chain"` and
     `vex-chain-cancel`, and appears in **every** VEX view, as M1 requires.
  2. A running build/clean: `Building override… 12s`.
  3. The latest finished action's result: `✓ Built build/override.bin
     (184 KB) · 14:02`, `✗ Build failed (exit 2): 2 errors`, `✓ In slot 3 ·
     14:03`, or `Download outcome unknown. Check status`. The last offers its
     Check status button.
  4. Idle: `Ready. Edit, then Clean → Build → Download.` in muted text.
- Refusals in the daemon's own words (`buildNote`, `downloadNote`,
  `stillStale`) appear on a second red line with `role="alert"`, as today.
- The status line is **never collapsible**. It is the state display that
  stays when the output area is folded.

### 2.4 Errors list

This is today's `vexerrors` list with `ErrorRow`. It is unchanged, including
`errorTarget`, file-at-line opening, outside-project rows shown as text, and
disabled under a card.

It now sits directly under the status line and appears only when the last
build has errors. More than 5 rows collapse behind a "Show all N" control. This
is display-only: every row stays in the DOM, and the list's `max-height` is
kept.

### 2.5 Output area: four tabs, one body, collapsible

| Tab | Body | Badge |
|---|---|---|
| **Build** | The latest build/clean output (`vex-build-output`, plain text, follow-tail), with "Show terminal" / "Check status" in a small toolbar | idle · spinner · ✓ · ✗ |
| **Download** | The download output (`vex-download-output`), the progress bar, "Show terminal" / "Check status", and "✓ In slot N" (`vex-downloaded`) | idle · spinner · ✓ · ✗ · ? |
| **Brain** | The brain's user port in the pane: an embedded terminal of the brain program, plus Connect/Disconnect (§5) | off · ● live · paused (download/run holds the port) · ✗ ended |
| **Device** | Tooling, `Brain:`/`Controller:` lines, last check, Refresh, the "USB presence only" disclaimer, the **visible** connection log, and Read device info / List programs with their text | worst confirmed USB state; ? when stale |

- **Which tab is shown.** An action the owner starts selects its tab: Build or
  Clean selects Build, Download selects Download, and Connect selects Brain.
  Background events never switch tabs. The last tab chosen is kept per pane in
  the workspace record. A missing record means Build.
- **Collapse.** `▾` folds the body away and leaves the tab strip with its
  badges. `⤢` gives the body all of the pane's remaining height. The folded
  state is per pane.
  - The body is `flex: 1`. It no longer has the 220 px cap. When the pane is
    short, the minimum is 120 px.
- **Every tab is a real `role="tablist"`.** Arrow keys move between tabs, and
  each tab panel is labelled by its tab (§9).

### 2.6 First-use help

- Every control in the header, action bar, slot chip and output tabs has a help
  sentence. It is shown as a Graphite tooltip (`--bg-3`, `--border-strong`,
  12 px) on hover, and on keyboard focus for focusable controls.
- **Each control's tooltip shows on its first 3 hovers** (count stored in
  `localStorage` `jarvis.hud.vex.help`, `{controlId: n}`, try/catch, a
  per-viewer convenience). After that only the native `title` remains.
  The `?` toggle in the header turns on **help mode**. Every control gets a
  small numbered marker, and the output area lists what each number does, so
  callouts never overlap (mockup state 1). Pressing `?` again, or Escape,
  leaves help mode.
- Download is out of the Tab order and never focusable, so its help shows on
  hover only. It is always in `aria-description`.
- Draft sentences (the mockup's idle state shows them):
  - Clean: "Deletes this project's build folder (make clean). The next Build
    compiles everything."
  - Build: "Compiles the project (make). Errors appear below; click one to open
    the file at that line."
  - Download: "Sends the built program to the brain, by USB or through the
    controller. Replaces whatever is in slot 3. Builds first if the program is
    older than its sources. Mouse click only."
  - Run: "Starts the program in slot 3 on the brain. The robot may move."
  - Stop: "Stops the program running on the brain."
  - Slot chip: "Which slot (1–8) and name the program gets on the brain."
  - Brain/Controller badges: "USB presence only. Not radio link, not whether a
    program is running." This is the existing disclaimer.
  - Brain tab: "What the program prints (printf / print), live. What you type is
    sent to the program."

### 2.7 What is not in the view any more

- Six section headings.
- The second Download button.
- The second slot reading. The card's `slot N` fact is now the slot chip.
- The second Brain/Controller reading. The Device tab's lines are the long
  form, the badges the short form; it is one reading (`useUsbReading`).
- The `<details>` that hid the connection log.

## 3. State model

### 3.1 Per action: one pure function

`actionState(kind, run, connection, now)` in `lib/vex.ts` returns
`{state, text, detail?}`. Here `state` is one of:

| State | Meaning | Mark | Colour |
|---|---|---|---|
| `idle` | Not done in this window, or no record kept | none | `--text-2` label |
| `busy` | Starting or running (incl. chain phases) | spinner (`.vexspinner`; static under reduced motion) | `--accent` (Graphite: "running") |
| `ok` | Last run succeeded | ✓ | `--green` |
| `failed` | Failed, timed out, or refused | ✗ | `--red` |
| `stale` | Succeeded, but no longer current: Build after an editor save in this project; Download after a later successful build | ✓ dimmed (`opacity .6`, as `.vexusb.stale`) | the `ok` colour, dimmed, with the words "edited since" or "older build" |
| `unknown` | The outcome was lost: a status read gave up, or a record was forgotten | `?` hollow ring | `--text-muted` |

- **Amber is never used for a VEX result.** In Graphite amber means an approval
  is pending. Under an approval card the whole view is inert and disabled, as
  today, and the status line says `Held: an approval card is up` in
  `--amber`. That is the token's existing meaning.
- **Text always accompanies colour.** Each mark's `aria-label` reads
  "Build: failed (2 errors)", and the status line says it in words.
- **Stale is advisory.** It never disables anything. The daemon's freshness
  rule (`Vex._fresh`, `build_first`) stays the only authority on whether a
  download may go. An editor save is known through the HUD's own write path:
  the `PUT /projects/{id}/file` success that the editor store already sees.
- **USB readings keep their own rule.** Confirmed connected is green, confirmed
  disconnected is red, unknown is muted, and stale is dimmed with "(last
  confirmed)". No disconnect is ever invented.
- **USB presence never disables Download or Run.** It cannot establish radio
  link or readiness. Two confirmed disconnects add a muted hint line, "No brain
  or controller on USB (checked 14:05)". vexcom's own refusal is the real
  answer.

### 3.2 Who owns state

Nothing moves. The view keeps reading the same store (`state/vex.ts`); the
redesign is presentation. The only new state is per-pane UI:

- the selected output tab;
- whether the output area is folded or maximised;
- the help counts.

None of it is safety-relevant. All of it fails safe to defaults.

## 4. Safety guards: each one kept, and where it now lives

| Guard (source) | Today | After |
|---|---|---|
| **Download is only a trusted primary first click** (`isExplicitClick`: `isTrusted`, `detail === 1`, `button === 0`; V-2/V-3, L3) | Two buttons, both guarded | **One button, same handler.** `tabIndex=-1` and `onMouseDown=preventDefault` (no pointer focus) remain. No key, no palette entry, nothing else calls `vex.download` |
| Download off when blocked / unconfigured / `!d.builds` / busy / name refused / any chain | Both buttons | Same predicate. The slot chip shows the name refusal in red even while the popover is closed |
| **One chain per window, Cancel until the download is sent** (M1) | `ChainBar` | The status line's chain row, in every VEX view, with the same testids |
| **Under a card, every control is disabled and inert; the store refuses** (`blocked`, `vex.setBlocked`) | Every control | Every control, **including the new ones**: tabs, the fold toggle, the slot chip and popover, ⋯ and its items, `?`, Connect, Run and Stop. The popover and ⋯ menu close when a card comes up. The pane stays under the veil's `inert` |
| **A card that comes up between click and any step drops the chain; a dropped chain never reopens the brain** (card count) | `state/vex.ts` | Unchanged (store) |
| Crash stops the chain (`vex.crash()`) | store | Unchanged |
| **Build-first only on `code: "build_first"`, one rebuild, then `stillStale`** | store | Unchanged; `stillStale` text goes to the status line's alert row |
| **Device lock**: one device action at a time; reservation by token; device reserved before build lock; per-project build flock | daemon | Unchanged. Run/Stop (VX-UI-5) join the same reservation |
| **Output and device info are text** (`<pre>{text}</pre>`, never HTML) | `BuildOutput`, `vexinfo` | Same components inside the tabs. The brain output is xterm (§5), which draws characters and never HTML |
| **Build errors open only in-project relative paths** (`errorTarget`) | `ErrorRow` | Unchanged |
| **Unclaimed `vex_device` records are said** (`DeviceLedger`) | notice rows | Unchanged and never folded. Run/Stop add `run`/`stop` to `DeviceAction`, `parseDeviceRecord` and the ledger |
| **Status reads never resend a Download; unknown stays unknown** | store | Unchanged. `unknown` gets its own mark (§3) |
| **USB polling only while drawn, visible, not under card/crash/chain; strict parse; last-confirmed** | `useUsbReading` | Unchanged hook. The badges and Device tab are both fed by it |
| **Strip offers VEX only for VEX projects; switching pins; never follows the chat; lone VEX pane survives reload** | App | Unchanged |
| **The brain terminal**: owner input only, Ctrl-C allowed, agents never write, "Jarvis can read" switch, `terminal_read` refusals | panel `TerminalView` | Same `TerminalView` (same session, same switch bar) placed in the Brain tab (§5) |
| **No run after download** (V-3) | n/a | Run is a separate control with its own click. Download's chain never runs. `--run` is never added to `download_argv` |
| **Agents have no lever** (no tool, MCP, CLI or Discord verb; `vexcom` DENY in `rules.decide`) | | Unchanged. New routes are owner-only (`projects.is_owner`, loopback Host) |
| Setup missing → the one command | `vexsetup` | Replaces the action bar, otherwise unchanged |
| Python projects: no build/download | sections | The action bar shows Build/Download disabled with the existing sentences as their help |

## 5. Brain output in the HUD

### 5.1 What exists already

- `POST /vex/brain` runs exactly `vexcom --user` (`brain_argv`). This is
  DishPy's user-port terminal.
- It runs as a program terminal: `program="vex-brain"`, `input_ok=True`, one at
  a time, reattaching after a reload with its ring, Ctrl-C allowed.
- It currently opens **in the bottom panel only** (`terminals.showProgram`).
- A Download chain closes the brain terminal and reopens it afterwards. A
  chain dropped by a card, a crash or Cancel does not reopen it.
- `terminal_read` can read it under the "Jarvis can read" switch.

### 5.2 Source evidence for the transport

This was inspection only, under the precedent set for `--usb` in the
2026-10-13 plan. The file is the vendor-distributed readable JavaScript
`vexrobotics.vexcode-0.8.2026020401/dist/extension.js`, SHA-256
`df1207f9f9889bb24ad3448f0979b2813fe1b51c1725f736d63d8a014fd32ab5`, the same
file. Nothing was run, decompiled or copied. Character offsets:

- **68430** `stop(){… "--stop <dev>"}`
- **68529** `play(e=slot1){… "--slot ${e} --run <dev>"}`
- **69190** `openUserPort(e){… "--quiet --user ${e}"}`, run as a vexcom child
  process whose stdin is written to
- **83443**: when the selected V5 device is a **Controller** and has no
  separate user serial path, the extension's user port is
  `new zo(this._communicationPath)`. That is the vexcom `--user` child,
  aimed at the controller's communication port. A brain on USB with its own
  user CDC port is instead opened directly as a serial port at 115200 (`new
  st(this._userPath)`).
- **94996**: on a V5 device, `play`/`stop` **close the user port first**, run
  `systemInfo`, run the command, then reopen the user port. This is the
  same pause/resume Jarvis's Download chain already does.
- **67359**: the extension's download can append `--run`. Jarvis must not;
  that is V-3.

### 5.3 What the evidence says about the owner's setup

The owner downloads through the **controller on COM3**, with the brain linked
by radio. The vendor extension, in that same configuration, reads the program's
printed output with **`vexcom --quiet --user <controller port>`**, so vexcom
relays the user port through the controller.

Jarvis's existing `vexcom --user` (vexcom auto-selects the port, as the
download did, "Device: COM3") is therefore the right transport. The two
differences are `--quiet`, which presumably suppresses vexcom's banner, and an
explicit device path, which Jarvis has never needed.

**HARDWARE-UNVERIFIED.** None of the following has been seen on the owner's
machine:

- (H1) that `vexcom --user` streams `printf` output through the controller over
  radio;
- (H2) its throughput and whether it drops lines under heavy telemetry. The
  owner's firmware prints `$BEGIN,<stream>,<label>` CSV blocks
  (`include/telemetry.hpp`), which can be bursty;
- (H3) that typed input reaches the program;
- (H4) whether the brain terminal starts at all under WSL interop. See 5.4.

### 5.4 A startup risk found while planning

- **What happens on startup.** On 2026-10-14 every input-disabled vexcom hung
  until it timed out. WSL interop's console wrote `ESC[6n` and waited for a
  cursor report. The fix, `terminals.STARTUP_REPLY_PROGRAMS`, deliberately
  leaves out `vex-brain`, because it takes input.
- **How the brain terminal gets its reply.** It gets one only from an
  **attached, already-replayed xterm**. `TermSession.input` drops anything
  xterm "says on its own" while the session is not attached or is still
  replaying.
- **The race.** If the query reaches the ring before an xterm is attached, it
  is answered during replay, which means never. The brain terminal would hang
  like the others did.
- **Status.** The notes do not record a live brain-terminal run. **Hardware
  check H4** decides whether a daemon-side reply is needed for `vex-brain` too.
  If it is, that is a terminals change for VX-UI-4b, and it must keep the
  following:
  - only the first exact query is answered;
  - the reply is hidden from the ring;
  - the owner's input path stays unchanged.

### 5.5 Design

- **The Brain tab hosts the brain terminal's own `TerminalView`.** It is the
  same session, keeps the same "Jarvis can read" bar and the same Ctrl-C
  rule, and xterm draws untrusted bytes as characters.
- It is not a new text tail, for three reasons:
  - a plain-text tail cannot answer the cursor query;
  - it would add a second rendering path for untrusted output;
  - it would add a second input path to a device.
- **Placement.** A terminal session mounts in one place at a time (the
  `place` map). The Brain tab is a new place, `vex-<pane>`. While it is shown
  there, the panel's tab for it says "Shown in the VEX pane" with Go there.
  That is the same pattern as `pane-N-terminal-elsewhere`.
  - Switching to another output tab keeps the session attached and hidden; the
    ring keeps everything. Only Disconnect closes it.
- **Connect / Disconnect** sit in the Brain tab toolbar.
  - Connect is `vex.openBrain()`, unchanged: off under a card or a chain, and
    one device action at a time.
  - Disconnect closes the program terminal (`DELETE /terminals/{id}`, as the
    chain's pause does).
  - Recommendation (**Q5**): the tab does **not** auto-connect when selected.
    Connecting holds the device port and is a device action.
- **Paused state.** While a Download chain or Run/Stop holds the port, the
  Brain tab badge says `paused` and the body says "Paused for the download;
  reconnects after". When a dropped chain leaves it closed, it says so, as
  today.
  - **As built (VX-UI-4a, 2026-10-15, the lead's direction):** no chain
    reconnects it. After any download the tab says it was closed for the
    download and offers Connect, so the link opens only on the owner's
    Connect (decision 5). See `docs/notes/vex.md`, "Brain tab, VX-UI-4a".
- **Later, out of scope here.** A "Save CSV block" helper for the owner's
  `$BEGIN…$END` telemetry blocks, mirroring `serial-monitor.ps1 -Csv`.

## 6. Run and Stop on the brain

**Supported by vexcom.** The vendor source (§5.2) uses:

- `vexcom --slot N --run` to start slot N;
- `vexcom --stop` to stop the running program.

In both cases, on a V5 device it closes the user port before the command and
reopens it after.

Before any code, three gates:

1. **V-3 is a settled owner decision** (2026-10-09). It says "no run after
   download. Starting a program stays something done at the brain or controller
   by someone who can see the robot." Its recorded alternative was "a separate
   Run button behind a second confirmation, never combined with download". The
   owner's 2026-10-15 loop includes Run, which reads as choosing that
   alternative. CLAUDE.md says not to relitigate settled decisions, so the owner
   must **explicitly revise V-3** (Q1), and the decisions file must record it.
2. **Licence rule.** VEX flags come only from DishPy or from the owner's own
   `vexcom --help`. `--usb` was accepted on vendor-source evidence, but only
   after an independent source review. For Run/Stop either:
   - the owner runs `vexcom --help` once and confirms `--run`, `--slot` and
     `--stop`. The notes already say its help lists options that "write,
     **run**, erase or update firmware". This is preferred. Or:
   - the same independent source-review acceptance as `--usb`.
3. **Hardware checks.** H5: Run starts the slot through the controller over
   radio. H6: Stop stops it, both by radio and on USB. H7: the exit codes and
   output on success, an empty slot and no device.

Design, once the gates pass (VX-UI-5):

- **Daemon.**
  - `run_argv(cfg, slot) = [vexcom, "--slot", str(check_slot(slot)), "--run"]`
    and `stop_argv(cfg) = [vexcom, "--stop"]`, in the argv tables. Requests
    name nothing but `{slot}`.
  - Before each spawn: `verify_tools`, the one-device reservation (actions
    `run`/`stop`), the 10 s timeout, `STARTUP_REPLY_PROGRAMS` entries
    `vex-run`/`vex-stop` (input-disabled), and the id-only `vex_device
    {action, at}` announcement.
  - Owner-only `POST /vex/device/run {slot}` and `POST /vex/device/stop {}`.
    There is no tool, MCP tool, CLI or Discord verb. The `vexcom` DENY in
    `rules.decide` is unchanged.
  - **Not a boundary.** The residual gap stated for Download applies
    word for word: an agent runs as the owner.
- **HUD Run.** The same guard as Download: `isExplicitClick`, `tabIndex=-1`,
  no pointer focus, no key, no palette entry.
  - **Never chained to Download.** A finished download enables Run; it never
    triggers it.
  - **Second confirmation** (V-3's alternative, recommended): the first click
    arms an inline confirm in the status line, "Run slot 3 on the brain? The
    robot may move." with **Run now**. Run now is also explicit-click-only,
    disarms after 5 s, and is cancelled by a card.
  - Q1 asks whether the owner wants the confirmation.
  - The brain terminal pauses and resumes as in the Download chain, with the
    same card/crash rule.
- **HUD Stop.** It is in the Tab order. Enter, Space and a pointer all work,
  but only from **trusted** events (`isTrusted`), so a script click does
  nothing. Stopping is the safe direction, and a keyboard Stop is an
  accessibility gain.
  - It is still owner-only, still a device action, off under a card, and
    refused by the daemon while another device action holds the port. A Stop
    during a Download is refused. Q6 asks whether Stop should wait for the
    port instead.
- **State.**
  - Run's mark is ✓ "Started slot 3 · 14:05". The HUD **cannot know** that the
    program is still running; `--programs`/`--json` might say, which is H8. So
    the text says "Started", never "Running".
  - Stop's result is shown in the status line.

## 7. Simulator: the Browser pane, not a VEX tab

**How it runs today.** The override project's simulator is a local dev
server, `node sim/tuning/server.mjs`, on `http://127.0.0.1:8787`, plus a
self-contained `sim/field.html` viewer. Both are described in
`C:\dev\override-jarvis\sim\README.md` and its `CLAUDE.md`, read only.

**Recommendation: open it in a Browser pane's Direct tab, beside the VEX
pane. Do not add a simulator view or a VEX tab.** The reasons:

1. **Every guard already exists in the Browser pane.** A framed page needs
   all of these:
   - Direct frames for loopback;
   - the owner-gesture-only port admission (BR-1a, "closed like the LAN");
   - "no frame takes the keyboard without the owner's gesture" (R-1);
   - inert/hidden-pane rules;
   - card disabling;
   - frame eviction.

   A VEX-hosted frame would have to duplicate all of them, or it would
   become a second, weaker frame host. That is the most review-heavy
   surface in the HUD.
2. **It already opens itself.** Starting the server in a HUD terminal is a
   dev server that auto-open places **Direct** (AO-3). It never admits a port
   in Remote's gate (L2), and it never moves focus. The owner can also type
   the URL, or Ctrl+click it in the terminal.
3. **Side by side is the lab layout.** A split pane gives VEX on one side and
   the simulator on the other, both visible. A tab inside the VEX pane would
   hide the output and log, which are the state display the owner asked to
   keep visible.
4. **The simulator rework is coming.** Coupling it to the VEX view now would
   tie that rework to this one.

**Optional later (Q4).** A `Simulator ↗` link in the VEX header that opens a
Direct tab in a Browser pane at a per-project loopback URL. It is an owner
click, so it counts as owner navigation. It runs nothing and starts no
server.

## 8. Look

- Graphite throughout, with no new hues:
  - surfaces: `--bg-1` pane, `--bg-2` controls and chips, `--bg-inset` output
    and terminal, `--bg-3` hover and tooltip;
  - `--border` / `--border-strong` lines;
  - `--accent` for running, the selected tab underline, focus and links;
  - `--green` done, `--red` error, `--amber` only for "held by an approval
    card";
  - `--mono` for output and file positions.
- Button metrics are the HUD's existing ones: 12 px, `5px 10px`, `--radius`
  6 px. The output tabs reuse the pane strip's underline style at a smaller
  size (`11px`, `6px 10px`). The pane header is untouched.
- **Dimensions.** The pane uses its full height. The output body is `flex: 1`
  and scrolls internally. There is no horizontal overflow at 375 px (existing
  check).

## 9. Accessibility

- **Tab order.** Slot chip, badges, `?`, then Clean, Build, Stop, ⋯, then the
  output tabs and the output.
  - **Download, Run and Run now are out of the Tab order**, deliberately. A keystroke
    never sends code or starts motors. Their `aria-description` says "mouse
    click only".
  - The order is still navigable, and Download is reachable as an accessible
    object for screen readers to read.
- **Output tabs.** `role="tablist"`, `aria-selected`, roving `tabIndex`,
  ←/→/Home/End, and `aria-controls` → `role="tabpanel"`. Each tab's accessible
  name includes its state ("Build, failed"). The fold toggle has
  `aria-expanded`.
- **Status.** The status line is `role="status"` `aria-live="polite"`. Refusals
  are `role="alert"`. Progress keeps `role="progressbar"` with
  `aria-valuenow`.
- **Popovers and menus.** The slot chip popover is a `dialog`-less disclosure:
  `aria-expanded`, focus moves to the slot select, Escape closes and returns
  focus to the chip, and a card closes it. The ⋯ menu is `role="menu"` with
  arrow keys.
- Tooltips use `aria-describedby`; they are never the only place information
  lives. Reduced motion means a static spinner and no progress transition.
  Contrast: every pair is an existing Graphite pair (AA).

## 10. Checks that change

**`tests/face/hud_v2_vex_check.py`.** Every guard check is kept. Selectors move
as follows.

- `_explicit_click_checks`: the button's text check becomes `Download to slot
  N` plus `aria-description` containing "replaces whatever is in slot N"
  (Q2). The slot and name are set through the chip popover (`vex-slot-chip` →
  `vex-slot-pick` / `vex-name-input`, same testids). Every key, script and
  untrusted-event refusal is unchanged.
- `_quick_download_checks`: **merged** into `_explicit_click_checks`, since
  there is now one button. Its double/triple-click "later click of a run"
  refusal moves there verbatim. `vex-download-quick` is removed. A check
  asserts there is exactly one Download control in the view.
- `_card_checks`: the controls list adds the new controls: `vex-slot-chip`,
  `vex-more`, `vex-out-tab-*`, `vex-out-fold`, `vex-help-toggle` and
  `vex-brain-connect`, plus `vex-run`/`vex-stop` after VX-UI-5.
  - `vex-rebuild-btn`, `vex-info-btn` and `vex-programs-btn` are asserted
    disabled **inside their opened menu or tab**. The check opens them before
    the card comes up, and asserts that the menu or popover closes when it
    does.
- `_build_checks` / `_clean_output_checks`: output is read from the Build tab
  (`vex-build-output` unchanged). The state is read from the status line
  (`vex-status`) and the Build mark (`vex-mark-build[data-state]`).
  `vex-build-state` is kept on the status line for one release, or the checks
  are updated.
- `_feedback_checks`: the connection log is visible in the Device tab (no
  `<summary>` click). Colour checks are against `--green`/`--red`, and the
  progress is 34/72/100%, unchanged. The 375 px fit now covers
  `vex-actionbar`, `vex-status` and the output body.
- `_brain_checks`: Connect lives in the Brain tab (`vex-brain-connect`; or keep
  `vex-brain-btn` as its testid). New: the session is drawn in the pane
  (`data-where="vex-2"`), the panel tab says it is elsewhere, it reattaches
  after a reload, and Download's pause/resume is unchanged.
- `_info_checks`: reached through the Device tab (or ⋯). The text-only and
  markup checks are unchanged.
- `_chain_checks`, `_one_chain_checks`: `vex-chain` / `vex-chain-cancel` now
  live in the status line of **every** VEX view. Assertions are unchanged.
- **New sections:**
  - `_tabs_checks`: an action selects its tab; background events do not;
    badges follow state; fold and maximise; keyboard tablist.
  - `_help_checks`: a tooltip on the first 3 hovers, then title only; the `?`
    toggle; none under a card.
  - `_run_checks` (VX-UI-5): explicit click and confirm, never after Download,
    keyboard and script refusals for Run, a trusted keyboard Stop works and a
    script Stop does nothing, card drops an armed Run, `vex_device` run/stop
    notices.

**Vitest.**

- `lib/vex.test.ts` gains `actionState` (every state, stale and unknown, and
  no amber) and `outputTabFor(action)`.
- `state/vex.test.ts` gains Run/Stop store guards (blocked, chain, crash, brain
  pause/resume) in VX-UI-5.
- `Terminal.test.ts` gains the `vex-<pane>` place in VX-UI-4a.

**Python.** `tests/v2/vex_check.py` gains, in VX-UI-5, `run_argv`/`stop_argv`
tables, the reservation, `verify_tools`, timeouts, owner-only routes, the
`vex_device` announcement and `STARTUP_REPLY_PROGRAMS` entries, against the
fake `vexcom`, which gains `--run`/`--stop`. In VX-UI-4b,
`tests/v2/vex_output_check.py` gains the `vex-brain` startup reply, if H4 needs
it. `rules` tests are unchanged.

## 11. Packages (each small, each its own PR)

| Package | Scope | Daemon? | Gate |
|---|---|---|---|
| **VX-UI-1** Layout shell | Header; action bar (Clean/Build/Download plus slot chip popover and ⋯ with Rebuild/info/programs); status line absorbing chain/notes/results; errors under it; output area with **Build / Download / Device** tabs (Brain later); one Download button; checks moved per §10 | no | review |
| **VX-UI-2** State marks | `actionState` (pure, vitest); marks on steps and tab badges; stale on editor save and newer build; unknown; amber only when held by a card | no | review |
| **VX-UI-3** First-use help | Tooltip component; counts in `localStorage`; `?` toggle; sentences of §2.6; `_help_checks` | no | review |
| **VX-UI-4a** Brain tab | `TerminalView` placed in the VEX pane (`vex-<pane>` place); Connect/Disconnect; paused state; panel "shown elsewhere"; `_brain_checks` updated | no | review |
| **VX-UI-4b** Brain startup + `--quiet` | Only if H4 shows the hang: a one-shot `ESC[1;1R` reply for `vex-brain`'s first exact query, input path untouched; optionally `--quiet` on `brain_argv` (vendor-source evidence, so the same acceptance as Run's flags) | **yes**, terminals + vex | H1/H4 hardware check; independent review (terminal boundary) |
| ~~**VX-UI-5** Run / Stop~~ — dropped (owner, 2026-10-15: no Run button). Stop is VX-UI-1's cancel of a running Clean/Build/Download instead. |
| **VX-UI-6** Docs | `docs/notes/vex.md` section, `docs/hud-api.md` routes, decisions file (V-3 amendment) | no | with 5 |

VX-UI-1 to VX-UI-3 and 4a can land with fakes alone. Real-device behaviour
stays **UNRUN** until the owner's hardware session.

## 12. Hardware check list (the owner's session, nothing run by agents)

| ID | Check | Needed by |
|---|---|---|
| H0 | `vexcom --help` output pasted to the plan: confirm `--run`, `--slot`, `--stop`, `--quiet` | 4b, 5 |
| H1 | The brain terminal shows `printf` output **through the controller over radio** | 4a value |
| H2 | Telemetry burst rate: are lines dropped or delayed over radio? | 4a |
| H3 | Typed input reaches the program | 4a |
| H4 | The brain terminal starts under WSL interop (no `ESC[6n` hang), opened from the HUD | 4b |
| H5 | `--slot N --run` starts the program over radio | 5 |
| H6 | `--stop` stops it, over radio and on USB | 5 |
| H7 | Exit codes/output: success, empty slot, no device | 5 |
| H8 | Whether `--json`/`--programs` say which program is running | 5 (state text) |
| H9 | Stop mid-download: does closing the download's terminal (SIGHUP, then SIGKILL of the WSL interop process) also end `vexcom.exe` on Windows, and is COM3 free for the next device action? | **Before relying on a mid-download Stop** (VX-UI-1) |
| H10 | Stop mid-download: what the slot then holds (incomplete program, the previous one, nothing), whether the brain shows or runs it, and whether the brain or controller needs a power cycle before the next download | **Before relying on a mid-download Stop** (VX-UI-1) |

## 13. Open questions for the owner

1. **Run/Stop and V-3.** V-3 said Run stays at the robot. Do you revise V-3 to
   allow a separate Run button, never chained to Download? If so, keep its
   second confirmation ("Run slot 3? The robot may move. Run now"; recommended),
   or make it a single click? Stop: allow it from the keyboard (recommended),
   given it only accepts trusted events?
2. **Download label.** OK to shorten the visible text to `Download to slot 3`,
   with "replaces whatever is in slot 3 on the brain" in the hover help, the
   accessible description and the slot popover? Or keep the full sentence on
   the button?
3. **Rebuild.** Is it fine in the ⋯ menu (Clean → Build covers your loop), or
   should it be a visible step?
4. **Simulator.** Agree it uses a Browser pane's Direct tab beside VEX (no own
   tab)? Want a `Simulator ↗` header link to a per-project localhost URL later?
5. **Brain tab connect.** Connect only on an explicit click (recommended,
   because it holds the device port), or whenever the Brain tab is shown?
6. **Stop while downloading.** Refuse (current one-device rule), or queue the
   Stop to run as soon as the port frees?
7. **Help.** 3 hovers per control before the tooltip retires: right number?
