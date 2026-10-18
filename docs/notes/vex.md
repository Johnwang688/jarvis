# Vex — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Download feedback and USB diagnostics (2026-10-13)

The additive Download record (`download_id`, owner `GET /vex/downloads/{id}`)
keeps live/final plain-text output and actual PTY completion independently of
terminal-list timing. The HUD shows elapsed progress, terminal access and inline
output, retains the final result, and labels lost status unknown. Status reads
never resend a Download; the trusted pointer/card/chain and build/device locks
remain authoritative.

The fixed read-only `--usb` exception is source-verified from the vendor's
distributed readable extension JavaScript, not executable decompilation or a
guessed fake schema: [contract and SHA-256 evidence](../plans/2026-10-13-vex-download-device-status.md).
Shared visible-window demand uses the same fingerprint/PTY/device reservation,
5-second minimum spacing, bounded error backoff and 2-second read budget. An
existing owner device action is never interrupted; actual EOF owns cleanup even
after timeout. Strict complete `usb.vex`/`usb.dfu` snapshots identify V5 Brain
(2888:0501) and Controller (2888:0503). Unknown identities, truncated output,
errors and busy checks retain last confirmed values rather than inventing a
disconnect. The UI separates configured tooling from USB presence, marks stale
readings and retains a bounded timestamped connection log.

USB presence does not establish radio pairing, download readiness or extension
health. Real hardware/tool validation is **UNRUN**; all automated checks use
fake programs/devices and temporary configuration. No robot download or installed
HUD/daemon restart is part of these receipts. The preserved flag inventory below
is historical; the current fixed `--usb` exception above supersedes it.

## Device programs and interop's cursor query (2026-10-14)

On the owner's machine every `vexcom` started by the daemon (download, USB
check, device reads) hung with no output until its timeout, while builds ran.
Cause, reproduced with a replica of `create_program`'s spawn: under a PTY that
has a size, WSL interop's Windows console first writes `ESC[6n` and waits for
a cursor report; an unsized PTY never asks, so `vexcom --usb` run by hand
answered in under a second. `ab84d58` answered the query for `vex-build`
only. `terminals.STARTUP_REPLY_PROGRAMS` now lists every input-disabled VEX
program (`vex-build`, `vex-download`, `vex-connection`, `vex-info`); the same
one fixed `ESC[1;1R`, for the first exact query only, hidden from the ring.
The brain terminal takes input and is not listed (superseded on 2026-10-17:
VX-UI-4b gives it the same reply; see "Brain terminal startup, VX-UI-4b"
below). The fake `vexcom` models
the handshake with `STARTUP_DSR` beside it and exits 91 without the reply;
`tests/v2/vex_output_check.py` (`DeviceStartup`, and the pinned set in
`StartupProtocol`) fails against the build-only rule. After the reply the console wraps `--usb`'s
JSON in its own sequences (`ESC[?9001h`, `ESC[?1004h`, an OSC 0 window title,
`ESC[?25h`, the toggles off again after the first line), which the strict
parse refused (review of #172, then seen live), so `usb_presence` removes
escape sequences (`vex._ANSI`) from a strictly decoded snapshot and nothing
else; the fake writes the same around its output and `UsbConsoleOutput`
holds the live shape. With a plugged-in controller that snapshot is one
`usb.vex` record (`0x2888`/`0x0503`, `"device":"ctrl"`) and parses to
controller present.

**First real download (2026-10-14, after #172 merged and daemon2 restarted).**
The owner downloaded `matchload_Test` to slot 3 through the V5 controller on
COM3 (the brain by radio): vexcom printed `Device: COM3`, `vexcom utility
V1.0.2b2`, `download :` and `0....10....20....` to `100`, the HUD settled
"✓ In slot 3", and the USB panel read Controller Connected, Brain
Disconnected (the brain was not on USB). Owner-reported from the HUD.

**Status colours and download progress (same day, owner's request).** The
indicators read `Brain:` / `Controller:` (no "USB" prefix; the note under them
still says USB presence only): green connected, red disconnected, plain
unknown, a stale reading dimmed beside "(last confirmed)". The Download
section shows a bar from `lib/vex.ts` `downloadProgress`, which reads only a
line of numbers and dots (a number per 10%, a dot per 2%; the last such line
wins) and is full once the record has succeeded. Checks: `lib/vex.test.ts`
and `_feedback_checks` in `tests/face/hud_v2_vex_check.py` (labels, computed
colours against `--green`/`--red`, the bar at 34%, 70%, 100%).

**Card badges and a build-row Download (same day, owner's request).** The
project card carries `● Brain ● Controller` (`UsbBadges`), the same reading
and staleness as the device section (`useUsbReading`, one helper for both),
with the full state in its label and title, shown only when the tooling is
configured. The build row has a second Download (`vex-download-quick`, "Download
to slot N") beside Clean. It is the same `DownloadButton` as the one under the
pickers: the slot and name live in `BuildAndDownload` and both send them; out
of the Tab order, no pointer focus, `isExplicitClick` only, and off under the
same conditions. `_quick_download_checks` repeats the key, script, untrusted
and later-click refusals for it.

## Original CLAUDE.md lines 2231–2491

<!-- claude-source:002231-002491:begin -->
**VEX backend, VX-1 (2026-10-11; editor plan
`docs/plans/2026-10-09-hud-editor-plan.md` §2.9/§3, decisions file beside it
wins: V-1..V-5; design §18; contract in `docs/hud-api.md`).**
`jarvis/v2/vex.py` builds, downloads, opens the brain's user port and makes
new projects with the Windows `vexcom.exe` and toolchain the owner's VS Code
VEX extension downloaded, **run in place through WSL interop — nothing
copied, bundled or committed**, and **never decompiled**: the only `vexcom`
flags are DishPy's (`--name --slot --write --progress`, `--user`) and the
two reads the owner's `vexcom --help` lists (`--json`, `--programs`). **Tests
never run a real VEX tool or touch a device** — `tests/v2/vex_fake/` holds
a fake `make.exe` and `vexcom.exe`. Which executables run is
**`config.VEX_PATH` (`~/.config/jarvis/vex.json`)**, written only by the
human-run `jarvis vex setup` (`vex_setup.py`: looks only in
`config.VEX_STORAGE_ROOTS`/`VEX_EXTENSION_ROOTS`, checks by the daemon's own
rule, y/N, mode 600, needs a TTY; it also offers `~/.local/bin/vexbuild`),
and it is **gate state** (`files._protected_state`, by path and by name beside
the allowlist). **Every path is re-checked at each use** (`load_config`:
absolute, `realpath == path` so no symlink anywhere, a regular executable or a
folder, inside the extension's folders; the file itself `O_NOFOLLOW |
O_NONBLOCK`, capped). **Those folders are under AppData, which agents can
write** (`cp payload.exe …/make.exe` is an ALLOW), so setup records **each
tool's SHA-256** in `vex.json` (required) and **`verify_tools` hashes them
again immediately before every spawn** — `make.exe` and the five tools it
runs before a build, `vexcom.exe` before a download, brain or info — through
one `O_NOFOLLOW | O_NONBLOCK` fd, then re-stats the spawned path against that
fd (device, inode, size, mtime, ctime): a mismatch refuses with "the VEX
tools changed since `jarvis vex setup`". **What remains, stated**: a swap in
the instant between that stat and `exec`, the five tools `make.exe` opens
later on the Windows side, during the build, and **a DLL planted beside a tool
in its AppData folder** — Windows loads a program's DLLs from its own folder
first, and the fingerprint covers the executable only. **No request names an
executable, a path or an argument**: argv tables in `vex.py`, the project from
its id, `{action}` / `{slot, name}` validated first (slot an int 1–8; name
`^[A-Za-z0-9_][A-Za-z0-9_-]{0,19}$`; the settings file's own name is held to
the `P=` rule before it names `build/<name>.bin`), Windows paths through one
`to_windows` that refuses `"<>|?*%^&!();,=\:`, invisible characters, a
trailing dot or space, reserved names (`NUL`, `con.txt`) and **anything outside
ASCII** (review round 2: Windows' "best fit" for an ANSI program maps U+FF02 to
`"`, U+FF3C to `\` and U+2215 to `/`). Programs run as
**program terminals** — `terminals.create_program`, called from `vex.py`
alone (grep-tested; `terminal_check`'s allowed set now includes `vex.py`):
same ring and routes as a shell, `WSLENV` emptied, typed input refused but
Ctrl-C except the brain's, an `on_exit` hook after EOF, six apart from the
shells' six. One build per project (a `flock` under `V2_DATA_DIR/vex/`,
shared with `vexbuild`), one device action at a time (download, info, brain;
the device is reserved before the build lock, and **a reservation is released
only by its holder, at most once** — a token, never a wildcard), 5-minute
build timeout (SIGTERM then SIGKILL, output kept), and **a build refuses a
project whose make files name `vexcom` and fails closed** where it cannot
read them all: more than 32, one past 256 KB, a symlink or FIFO among them, a
symlinked `vex/`, a glob too complex to check, an `include` named by a
variable, holding a backslash, outside the project, or **not in the project at
all** (make would search other folders for it; `-include` too) — and it reads
includes as make does, a backslash-newline joining the next line on (CRLF
too), so `include a.mk \` + `src/notes.txt` includes the second file.
**Download is meant to be only the owner's click** (V-2/V-3): no tool, MCP
tool, CLI or Discord verb, no "run after download", and **any agent command
naming `vexcom`, or running something that could be it, is DENY in
`rules.decide`** (see *Command rules*). **That is
not a boundary, and neither is the button — the residual gap, stated**: an
agent runs as the owner, so it can still reach a plugged-in brain through a
script it writes and runs, a shell fed its commands through a pipe, cmd.exe's
or PowerShell's own wildcard forms, a launcher `rules.py` does not model,
`make -f` on a makefile of its own, a DLL beside the tool, or a POST to
the download route with a forged Origin. What makes such a download visible
is **one id-only bus record per device action, `vex_device {action, at}`**
(as `terminal_attached`); closing the gap needs the device to belong to a
user the agent is not. Agents build through **`vexbuild`** (`python -I -m
jarvis.v2.vex build [--rebuild|--clean]`, nothing else — **isolated**, because
a plain `-m` puts the agent-writable project folder first on `sys.path`, so a
planted `jarvis/v2/vex.py` would have run), which asks like any command unless
allowlisted; running it or its module through a wrapper or a runner (`python
-Im`, `uv run --no-project`, `npx --yes`, `python -m runpy`, `uv run
--module`; review round 2 parses each runner's options) asks too, naming it as
an argument (`cp …/vexbuild`) does not; `vexbuild build` and plain `vexbuild`
both build. **`terminal_read` (#33) can read a program terminal** as it reads
a shell, under the same "Jarvis can read" switch and guard — for the brain
terminal that includes the owner's typed input, which the PTY echoes back.
**Every route is owner-only by the shared rule**,
`projects.is_owner` (PR #41: the HUD's Origin, or on a plain GET, which
Chromium sends with no Origin, `Sec-Fetch-Site: same-origin`), plus a strict
loopback Host. New projects reuse the `project_folder` card:
**`projects.confirm_folder` is now the one confirmation handler** (moved out
of `discord/project_commands.py`; still the single caller of
`make_project_folder`), templates are opened as regular files only, checked
whole before anyone is asked, and written through directory fds (each folder
opened `O_DIRECTORY | O_NOFOLLOW` relative to the last, the file `O_EXCL |
O_NOFOLLOW` relative to its folder; what remains is the new folder's own path,
resolved once after the yes). The log gets lifecycle lines only. **Device
reads** (2026-10-12, from the owner's own `vexcom --help`, vexcom 1.0.2b2):
`POST /vex/device/info {read?}` runs exactly `vexcom --json` ("system info in
json format", the default `system`) or `vexcom --programs` (`programs`) —
`vex.DEVICE_READS`, the only two; every other option it lists writes, runs,
erases or updates firmware — through the same `verify_tools`, the one-device
reservation (both are the `info` action) and the 10 s timeout; any other
`read` is a 400 before anything is looked up. JSON output is pretty-printed
as text (a JSON string's control characters stay escaped, invisibles are
removed). The output shapes in `tests/v2/vex_fake/` are guesses: only a
real brain shows vexcom's. Not yet, said by the routes: Python download
(spike). Agents' task worktrees are on the Linux filesystem, so
`vexbuild` builds only in a chat's own project folder. Tests point every
`config.VEX_*` path, `VEX_MOUNT_ROOT` and `VEXBUILD_PATH` at a temp tree, with a
non-empty `WSLENV`. `tests/v2/vex_check.py` (111 checks); 69 guards of round 1
and the guards of rounds 2–4 each shown to fail their check when removed in a
scratch copy (the PR lists them). Open question, not done: whether `vex.py`
and `vex_setup.py` join SELF_PROTECTED (the owner declined widening it for
`runtime.py`).

**The VEX view, VX-2 (2026-10-11; editor plan §2.9/§3; design §18; contract
in `docs/hud-api.md`).** `View` gains `"vex"` (`components/VexView.tsx`,
rules in `lib/vex.ts`, state outside React in `state/vex.ts`). **The strip
offers it only when the pane's project is a VEX project** (`offersVex`: the
settings parse, or are malformed/PROS so the view can say why; App's
`stripProject` names the project — a chat pane's conversation's, else the
pin, else the active chat's), and switching a pane to it **pins** it, so a
build or download never follows the chat to another robot. Build / Rebuild
/ Clean immediately show the action, a spinner and elapsed time. The view
previews the latest output and diagnostics while running, reports status-read
errors while retrying, and offers Show terminal for full output. Build records
keep a bounded output tail in memory after exit; no output is persisted.
`showProgram` requests a fresh listing so an in-flight list cannot hide a
new terminal. Actions show ✓ or the errors list; **an error click opens the file at its
line** (`openAt`, through ED-7's `placeFile`: a drawn File pane on that
project, else a split beside the view, else a sentence — since the ED-7
merge never the VEX pane itself; FileTab's `goto` sets the cursor after
PaneEditor's own model swap; an error outside the project is text). The
build, a download and the brain terminal are VX-1's **program terminals in
the WP-D panel** (`terminals.showProgram`; one that ended has no Restart or
"New terminal here" — those open a shell). **Download is only a click on
the labelled button**: `isExplicitClick` (trusted, `detail` exactly 1 — a
double or triple click's later clicks never count — primary button;
Enter/Space on a focused button and `el.click()` have `detail` 0),
`tabIndex=-1`, no focus from a press; **no key and no palette entry
downloads, and nothing else calls `vex.download`**. One click is one chain:
pause the brain terminal if it holds the port, build first on VX-1's 409
`code: "build_first"` (never its wording; still `build_first` after that
build, one rebuild; still after that, it stops and says so), download,
reopen the brain terminal (not after a chain a card, a crash or Cancel
dropped, nor after a card that came and went during the download). **One chain per
window** (review M1): no other download, for any project, while one runs;
every VEX view shows it ("Building override, then downloading to slot 1")
with a **Cancel** that closes the build's program terminal and never sends
the download (none once the download is sent); the VEX panes' project
pickers, the brain terminal and device info are off meanwhile. **Under a card every VEX
control is disabled (and inert), `vex.setBlocked` (App, above the boundary,
beside the terminals' hold) makes the store refuse, and a card that comes up
between the click and any step drops the rest of the chain** (a card count,
as `ownerOpened`: one that came and went counts, and so does one that came up
while the daemon was answering) — nothing builds or downloads behind it; a
crash (`vex.crash()`) stops it too; **a dropped chain never reopens the brain
terminal**, and says so. No `vex.json`: the view names `jarvis vex setup`
and checks again every 4 s and on focus. Detection is asked again on focus
and every 30 s, so a project that becomes a VEX project is offered the view
without a reload. **A VEX view never follows the chat**: a pane whose pinned
project is gone says "Pick a project". **A lone VEX pane survives a reload**
(the boot never puts the chat over a VEX view; a window with no chat drawn is
WP-B's refused-with-a-sentence case). `vex_device` records this window did
not start are said in the view. **Read device info** (`vexcom --json`) and
**List programs** (`vexcom --programs`) are one device read at a time, off
under a card and during a chain; the answer is drawn as text in a `<pre>`
(markup in it is characters), headed by which read it was and a non-zero
exit. **The daemon's freshness rule** (`vex.py`
`Vex._fresh`, review M2 and its delta review): **sources** are only what a
build compiles or obeys (`SOURCE_SUFFIXES`: C/C++ sources and headers,
assembly, make files — a notes file or an editor backup never counts, which
was M2's loop). **Never while a build is unfinished**: a build marks the
project before make runs (`mark_unfinished`, a file under `V2_DATA_DIR/vex/`,
`vexbuild` too) and only a success clears it, so a cancelled, failed or
timed-out build — or a daemon that died mid-build — refuses the download
(a truncated binary has a fresh mtime). **When this daemon's last build wrote
exactly this binary** (its `P=` name; mtime_ns and size changed, or a
rebuild), the binary may be sent only if the sources' digest — sorted
(path, mtime_ns, size) — is exactly what it was when that build started: an
edit, deletion, rename or older-mtime copy since asks for a build, and a
teammate's file dated in the future hides nothing. Otherwise (a restart, a
binary `vexbuild` wrote) make's own rule: newer than every source. **A make
that found nothing to do never vouches for a binary**: a header is compiled
wherever it is included, and VEX's makefile lists only `include/*.h`, so an
edited `include/pid.hpp` leaves make nothing to do — the daemon still says
Build first, and the click's chain then runs **one rebuild** (make -B) and
downloads; still stale after that, it stops and says so (never a loop). A
rename mid-build records nothing. Its 409 is `BuildFirst`, `{error, code:
"build_first"}`. Build errors' `./` is dropped so they stay clickable. Free
checks: `lib/vex.test.ts`, `tests/face/hud_v2_vex_check.py` (its own
`MockDaemon(0)`; VX-1's routes in `hud_v2_mock_vex.py`, builds and downloads
as terminal rows, their sockets played by the terminal suite's `FakePty`);
the four safety guards (explicit click, the chain's card check, `blocked` on
the controls, the strip's filter) and each review fix (M1, M2's HUD half,
L1–L7) shown to fail their checks when removed; M2's daemon half in
`tests/v2/vex_check.py` (`Device`, the fake make's `NOOP` and `HOLD`).

**New VEX project, VX-3 (2026-10-12; editor plan §2.9 "New VEX project",
decisions V-4; design §18; contract in `docs/hud-api.md`).** A dialog
(`components/NewVexProject.tsx`, rules in `lib/vexNew.ts`, picker
`"newVexProject"`) reached from New project's "New VEX project from a
template…" and the palette's **New VEX Project from a Template…**
(`project.newVex` — the palette's one VEX entry, which opens the dialog and
nothing else; `commands.test.ts` pins that). **The window never makes up a
path**: the templates are `GET /vex/templates` (split by language, labelled
from `index.json`), and **the exact folder it shows is the daemon's answer to
the very body Create sends** — the new `POST /vex/projects/check` (VX-1's
`_plan`, shared with `new_project`, so the same refusals word for word and the
path the `project_folder` card names; nothing asked or made; owner-only like
every VEX route). Create is enabled only while that answer is for exactly the
inputs on screen (`bodyKey`), the name passes the daemon's `PROJECT_NAME`
(mirrored, `projectNameRefusal`), and no card is up. Location defaults to
`vex.json`'s `project_folder` (the HUD now reads it from `/vex/status`, which
already reported it) and is sent only when changed. **A folder VEX's tools
could never work in is refused before anyone is asked** (review MED-1):
`_plan` runs the build's own `to_windows` on the folder, so `My (1)`, `a&b`
or `Документы` are refused by the check and by Create in the same words — the
owner used to approve a project whose every build answered 409. Create raises
the card; the dialog polls `GET /vex/projects/{id}`, **one read at a time,
riding out a failed read with a backoff** (`afterFailedRead`, review LOW-3:
only the daemon's answer, a 404/400, or three minutes of failures end the
tracking — one blip used to say `failed` while the card was still up), and
says how it ended — **Deny makes nothing and the dialog stays open saying
so**; on `created` it refreshes projects, closes and starts a new thread in it
(as New project does). An existing empty folder is used with no card (O5),
said beforehand. **Under any card every control is disabled (`blocked` from
App) and inert, and Escape is left to the card** (the dialog's own Escape
listener returns while a card is up, and its inputs never stop a key then).
`GET /vex/templates` answers a templates folder that cannot be listed with a
409 sentence, never a 500. **A template write that fails midway takes back
what this request wrote** (VX-1 code, review LOW-2): `_write_new` records each
folder it made and each file it created `O_EXCL` **with its `(st_dev,
st_ino)`, read by `fstat` from the fd it was made through**, and
`_remove_written` unlinks those files and `rmdir`s those folders, newest
first, through the same `O_DIRECTORY | O_NOFOLLOW` fds — **judged by identity,
never by name** (delta review LOW-B: `O_NOFOLLOW` stops a symlink swap, not a
rename, and a folder renamed away with an owner folder moved into its name
lost the owner's `mkenv.mk`): what is at the name now is `stat`ed without
following and must be the very entry it made, else it is kept and listed as
left. A project the store would not record takes its template back too
(`remove_written`), **only through the very folder the template was written
through** — reopened by path, its `(st_dev, st_ino)` must match `unpack`'s
(LOW-A: a parent swapped for a symlink to a decoy lost the decoy's files);
else nothing is removed, here or there. **The message is read from the
folder** (`_listing`, through its fd; LOW-C): empty again (a retry uses it
without asking, O5), or what it still holds and says to empty or remove it,
or that the path is no longer the folder written to. A colliding file is named
("src/main.cpp already exists …"), and an `OSError` is said by its
`strerror`, never the path it names. What remains: another program swapping an
entry in the instant between the take-back's `stat` and its `unlink`.
Free checks: `lib/vexNew.test.ts`,
`commands.test.ts`, `tests/v2/vex_check.py` (`NewProjectCheck`, `HalfWritten`; the check
route in `Routes` and `Coverage`) and `tests/face/hud_v2_vex_new_check.py` (66, a
`MockDaemon(0)` of its own; `hud_v2_mock_vex.setup_new` lays a temp "Windows
drive" with real template zips that the daemon's own `list_templates`,
`read_template`, `unpack` and `settings_document` read and write, and the
mock's `POST /approvals/{id}` answers the card through
`mock_vex.approval_decided`; run by `hud_v2_check` after the VEX view
section; the mock judges the folder with the daemon's own `to_windows` and its
`new["state_fail"]` fails the next reads). Guards shown to bite when removed:
the dialog's Escape hold, its `disabled` under a card, `createReady`'s key
match, the check never asking, the check behind `_owner`, and the review
round's four — the toolchain refusal, the normalised path, the take-back and
the poll's tolerance — and the delta review's identity compare (the swapped-in
folder's `mkenv.mk` is taken without it). `HalfWritten`'s three delta-review
tests fail on 9e1fcb6.

<!-- claude-source:002231-002491:end -->

## VEX view redesign, VX-UI-1 with parts of VX-UI-2/3 (2026-10-15)

Plan: [`docs/plans/2026-10-15-vex-ui-redesign.md`](../plans/2026-10-15-vex-ui-redesign.md);
its §0.1 (the owner's decisions) overrides the rest. `VexView.tsx` is now,
top to bottom: unclaimed device notices (red, never folded); a one-line
header (name, language with the SDK in its title, the slot chip `Slot N ·
name ▾` whose popover holds the slot select, the name box and "Download to
slot N: replaces whatever is in slot N on the brain", the Brain/Controller
dots as a button to the Device tab, and `?`); the action bar **Clean ›
Build, Rebuild › Download to slot N** plus ⋯ (Read device info, List
programs), all visible (decision 3), each with a state mark beside it; one
status line (`vex-status`: the running chain with its steps, progress and
elapsed time in **every** VEX view, else the running build, else the last
result in words, else Ready; **Held** in `--amber` under a card, amber's only
use); refusals in the daemon's words on alert rows under it; the errors list
(more than five behind "Show all"); and one output area with **Build /
Download / Brain / Device** tabs (a real tablist, roving tabindex, arrows,
Home/End), foldable (▾) and filling the pane (⤢), each tab with a badge. The
selected tab, fold and fill are per pane for the window's life, not across a
reload. An action the owner starts selects its tab; background events never
do. The setup box replaces the action bar while `vex.json` is missing.

**One Download button** (the build row's and the pickers' merged):
`isExplicitClick`, `tabIndex=-1`, no pointer focus, no key or palette entry.
Its text is "Download to slot N" (decision 2); the replace sentence is its
`aria-describedby` help and the popover's warning. **One click is clean →
build → download** (decision 3, `state/vex.ts` `download`): a failed clean or
build stops it before anything is sent; the brain terminal is paused only once
the build has succeeded (it used to pause first), and a chain dropped before
that never touches it; `build_first` after the chain's build gets one rebuild,
then `stillStale`. Every card/crash/chain guard is unchanged.

**Stop** (decision 6) appears at the end of the status line while something
runs, for **that view's own project only** (review MED-2: another project's
chain is shown in every view but stopped only from its own; Clean, Build and
Rebuild are off during any chain, as Download is): its chain at any step —
**the download included**, replacing VX-2's "never cut off mid-write" Cancel —
or its own clean, build or rebuild. A clean or build Stop cut off is marked
"stopped", not "failed (exit 129)", and a stopped download stays "stopped" when
a later Check status reads its end. It needs a trusted press (`isTrustedPress`: pointer or Enter/Space,
never `el.click()` or a dispatched event), is off under a card, and only ever
closes the action's program terminal (`DELETE /terminals/{id}`, as ×); one
pressed while the download request is in flight closes vexcom as soon as its
terminal id arrives. There is no `vexcom --stop` anywhere.

**What a mid-download Stop does — findings (source inspection and Jarvis's own
code; nothing run on hardware).** Jarvis side: `Terminals.close` SIGHUPs the
session, waits up to `CLOSE_GRACE_S` (3 s), SIGKILLs what is left, and
`finish` fires the download's `on_exit`, which settles the record `failed`
(exit 129 for SIGHUP, 137 after SIGKILL), keeps its output tail and releases
the device reservation and the build lock exactly once — the same path the
3-minute timeout takes with SIGTERM. Vendor side (the same readable
`vexrobotics.vexcode-0.8.2026020401/dist/extension.js`, SHA-256
`df1207f9…32ab5`, inspected only): the extension **never cancels a program
download** — its progress notification is `cancellable:!1` for V5 (offset
≈94740) and EXP/IQ2 (≈99280); `_runVexcomCMD` (≈72321) spawns vexcom with no
kill path; the only `.kill()` in the file (≈9289) is the UUU flasher's; and its
exit-code table has no "interrupted" value. So nothing on record says what the
brain keeps after an interrupted `--write`. The view therefore never claims
success for a stopped download unless its record says it finished first: it
says "Download stopped: slot N may hold an incomplete program, the previous
one, or nothing. Download again before running it.", marks Download ✗
"stopped", and does not reopen the brain terminal (L6). **HARDWARE-UNVERIFIED**:
(H9) whether killing the WSL interop process also ends `vexcom.exe` on the
Windows side — if it does not, a still-running vexcom could hold COM3 after
Jarvis released its reservation, and the next device action would fail on the
port; (H10) what the slot holds after an interrupted write, and whether the
brain or controller needs a power cycle. Both are in the plan's §12 as required
before relying on a mid-download Stop.

**Marks and help (VX-UI-2/3 parts).** `lib/vex.ts` `buildMark`/`downloadMark`
are pure: idle (no mark), busy (`--accent` spinner), ✓ `--green`, ✗ `--red`,
stale (✓ at 0.6 opacity: a source saved through the editor's write path —
`vex.edited`, the daemon's `SOURCE_SUFFIXES` — or a later clean; Download "from
an older build" after a later build), unknown (? dashed: monitoring gave up, or
a lost download outcome). Stale is advisory and disables nothing; the daemon's
`_fresh` still decides. Text always goes with colour (`aria-label`, title).
Every control has **permanent** hover help (decision 7; no counting), shown
after a short hover or on `:focus-visible`, hidden while pressed and under a
card, named by `aria-describedby`; `?` lists every sentence in the output
area, Escape closes. **The Brain tab is a placeholder** until VX-UI-4a: it says
the robot's output connects only when the owner clicks (decision 5, answered
2026-10-15) and keeps today's "Open the brain terminal" (bottom panel).

Checks (fakes only; nothing installed, no live daemon or device):
`lib/vex.test.ts` and `state/vex.test.ts` (marks, tabs, help, chain order,
stop at every phase, stopped-download honesty, in-flight stop, no reopen);
`tests/face/hud_v2_vex_check.py` (new `_stop_checks`, `_tabs_checks`,
`_help_checks`; `_quick_download_checks` merged into `_explicit_click_checks`;
the mock's `download_live` keeps a download's row running so the HUD's Stop
closes a live program, and `terminal_closing` settles its record as the
daemon's exit hook does). Mutations that removed the trusted-press test, the
stopped-download wording, the chain's clean, Stop during the download, the
late brain pause, the tablist keys, the short Download label and the card's
popover close each failed their checks (eight mutations; the store's five in
vitest too); so did the review round's seven (MED-2's two halves, LOW-1,
LOW-2, NIT-2, LOW-4, and Escape closing ⋯ when every item is off). Receipts
after the review round: `hud_v2_vex_check` 231/231, `hud_v2_vex_new_check`
66/66, vex vitests 47 + 33 (full vitest 3031 passed, 6 skipped),
`tests/v2/vex_check.py` 148. Real VEX behaviour stays **UNRUN**.

## Brain tab, VX-UI-4a (2026-10-15)

Plan §5.5 and §0.1 item 5. The Brain tab draws **the brain terminal itself**
in the VEX pane: the same `vexcom --user` program terminal (`POST /vex/brain`
unchanged, no daemon change), the same `TerminalView` with its "Jarvis can
read" switch, input path, Ctrl-C and `terminal_read` rules, at a new place,
`vex-<pane>` (`TermManager.vexPlace`, `holdInVex`, `shownIn`). One place at a
time (W-1): a terminal pane showing it wins, then the first VEX pane whose
Brain tab shows it; the panel's tab says "in the VEX view, pane N" and its body
"shown in the VEX view … Go there"; another VEX pane's Brain tab, or one whose
terminal a terminal pane holds, says where it is with Go there. Another window
showing it is the terminal's own "in another window · Show it here". Switching
to another output tab keeps the session attached (its ring keeps the output).

**It connects only on Connect** (owner decision 5): a trusted press —
pointer, or Enter/Space on the focused button (`isTrustedPress`, Stop's rule;
in the Tab order); a script's `.click()` or a dispatched event does nothing.
Showing the tab, a reload and the end of a download ask for nothing. A link
the daemon already holds (`/vex/status` `brain`, after a reload or another
window's Connect) is shown, never opened. Connect is off, with the reason in
its hover help, under a card, after a crash, during any download chain and
while a device read is on its way; the daemon's one-device reservation stays
the authority. Connect claims the terminal for this window
(`terminals.adoptProgram`) and never opens the panel; the Brain tab whose
Connect was pressed takes the `vex-<pane>` place first (a status read while
Connect is on its way never names the terminal, so another open Brain tab
cannot take it; review LOW-1). Connect from an ended link clears the old
exited terminal away, its panel tab too (LOW-2). **Disconnect** (also a
trusted press) is `DELETE /terminals/{id}`; it frees the port, and the program
on the brain keeps running.

**A download never reopens it** (the lead's direction for this package,
extending L6; it supersedes VX-2's "reopen the brain terminal" step and the
plan's "reconnects after"). The chain still closes it only once the build has
succeeded; while the download runs the tab says "Paused for the download" with
Connect off; afterwards it says it was closed for the download (also under the
status line, `vex-brain-closed`, as status, not alert) and offers Connect.
`resumeBrain` is gone. A brain terminal this window closed is never taken back
from a status read made before the daemon let go of it (`brainGone`).

States (`lib/vex.ts` `brainView`, pure): not set up; not connected; connecting
(the request, then the attach, or asking another window); connected (live;
"Nothing printed yet" until xterm's buffer holds visible text, so a ring of
control sequences alone, such as interop's `ESC[6n` with vexcom hung behind
it, still says it and names H4 without promising a fix; review MED-1);
paused; ended (vexcom exited, with its code and last output, or the terminal
is gone: Connect again, Close); taken or kept by another window (its own
words, with the terminal's Take it back or Ask again, never a spinner; MED-2);
connection lost (this window's socket retries ran out while the link may be
open: Reconnect re-attaches, and Disconnect is named Disconnect, never Close;
MED-2); refused (the daemon's sentence, Connect again); shown elsewhere. The
tab's badge: ● live, ◌ paused or lost, ✗ ended or refused, spinner
connecting; never amber.

**HARDWARE-UNVERIFIED**: H1–H4 (plan §12) are unrun. In particular H4,
whether the brain terminal starts at all under WSL interop's `ESC[6n` cursor
query: `vex-brain` is deliberately not in `STARTUP_REPLY_PROGRAMS` (it takes
input), so its query is answered only by an attached, replayed xterm. Drawing
it in the pane attaches the xterm as soon as Connect returns, which narrows but
does not close the race in §5.4. It stays open in particular when the owner
switches to another output tab (or the pane is hidden) before the terminal view
mounts: nothing is attached when the query arrives, so it lands in the ring and
is replayed later, where xterm's answer is dropped by design. If H4 shows the hang, VX-UI-4b (a daemon
reply for `vex-brain`'s first exact query) is the fix; this package does not
build it. (It did hang: VX-UI-4b, 2026-10-17, below.)

Checks (fakes only; nothing installed, no live daemon, port or device):
`lib/vex.test.ts` (`brainView`: every state, Connect's reasons, and no state
offering an enabled Connect with a terminal open, a chain or a card),
`state/vex.test.ts` (Connect/Disconnect guards, no reopen after a download
with or without a card, a late status read), `components/Terminal.test.ts`
(the `vex-<pane>` place, `adoptProgram`), and `_brain_checks` in
`tests/face/hud_v2_vex_check.py` (the mock's `brain_refuse` lever). Seven
mutations each failed their checks: Connect called when the tab is shown, the
old reopen after a download, Connect without the trusted-press test, Connect
enabled under a card, Connect enabled during a chain, the panel ignoring the
`vex-<pane>` place, and Disconnect without the trusted-press test. The review
round's five did too: any frame counted as printed (MED-1), `taken` falling
through to connecting (MED-2), no place callback and a status read naming the
terminal mid-Connect (LOW-1), and no clearing of the ended link (LOW-2). Before
this package the new vitests fail (22) and the face sections stop at the
missing Connect.

## The VEX view as built: VX-UI-1 to VX-UI-4a, with VX-UI-2/3 finished (2026-10-16)

Plan: [`docs/plans/2026-10-15-vex-ui-redesign.md`](../plans/2026-10-15-vex-ui-redesign.md),
whose §0.1 (the owner's decisions) overrides the rest of it. This section
describes the whole view as it stands. The two sections above have the
details and receipts for each package.

**Top to bottom**, in one pane (`VexView.tsx`):

1. **Unclaimed device notices.** Red, above everything, never folded.
2. **Header.** The project name, the language (the SDK version is in its
   title), the slot chip `Slot N · name ▾`, the Brain and Controller USB dots,
   and `?`. The slot chip's popover holds the slot select, the name box and
   the warning "Download to slot N: replaces whatever is in slot N on the
   brain". The USB dots are a button that opens the Device tab. They probe
   nothing.
3. **Action bar.** `Clean › Build, Rebuild › Download to slot N`, plus ⋯
   (Read device info, List programs). All of them are visible (decision 3).
   Each step has a state mark at its top-right corner. There is **no Run
   button** (decision 1; VX-UI-5 is dropped and V-3 stands). The setup box
   replaces the bar while `vex.json` is missing.
4. **Status line** (`vex-status`, never folded). It shows, in order of
   priority:
   - the running chain, step by step, with progress, in every VEX view;
   - the running build;
   - the last result in words;
   - "Ready".

   Any stale ✓ is also said here in words, for example "Build: ✓ edited
   since". **Stop** sits at the end of the line while this project has
   something running (decision 6). Under an approval card the line says
   **Held** in `--amber`, which is amber's only use in the view. Refusals
   appear on alert rows under the line.
5. **Errors list.** It appears only after a build with errors. More than five
   rows collapse behind "Show all".
6. **Output area** with **Build / Download / Brain / Device** tabs. It is a
   real tablist, foldable (▾) and able to fill the pane (⤢), and each tab has
   a badge. The output and the log stay visible: the area fills the pane and
   is never hidden behind a `<details>`. The Brain tab draws the brain
   terminal itself (VX-UI-4a) and connects only on Connect (decision 5).

**State marks (VX-UI-2, finished here).**

- `lib/vex.ts` `actionState(kind, run, following)` is the one pure function.
  It turns a project's run in this window into a state for Clean, Build,
  Rebuild or Download: idle, busy, ok, failed, stale or unknown, always with
  words (`text`).
  - A stale state also carries a `detail`: "edited since", "cleaned since"
    or "from an older build".
  - The action bar's marks and the Build and Download tab badges all read it
    (`tabBadge`), so a step and its tab never disagree. The Build tab follows
    whichever of clean, build or rebuild it shows. When the daemon forgot the
    current build (a 404 clears `build`), the tab follows the latest record
    kept for a step, so it says ? as that step's mark does (review LOW).
  - A running record that this window no longer follows is **unknown (?)**,
    never a spinner. Before this package the Build tab's badge showed a
    spinner there while the step's mark said unknown.
- **Stale is advisory.** Build or Rebuild goes stale after a source is saved
  through the editor's write path (`vex.edited`), or after a later clean.
  Download goes stale when a build or rebuild succeeds after it. A source edit
  alone does not make Download stale. Stale disables nothing: the daemon's
  `_fresh` and `build_first` alone decide whether a download may go.
- **The status line says stale ✓s in words** (`staleNotes`,
  `vex-stale-note`), as in mockup state 6. A dimmed ✓ alone would say it only
  in colour, and the words are no longer only in a hover title.
  - Only the **latest successful** of Build and Rebuild is said, then
    Download. An older one superseded by a later build keeps its dimmed mark
    but no words (review LOW).
  - Nothing is said while a chain or a build runs; the line says that
    instead, and the words come back when it ends.
- **The Device tab's badge** (`deviceBadge`) shows the worst confirmed USB
  state: a green dot when a device is present, a red dot when nothing is on
  USB. It shows **? when the reading is stale or unknown** (paused, older than
  12 s, or erroring), as plan §2.5 asks. Before this package a stale reading
  was a dimmed dot. USB presence still disables nothing.
- **Amber is never a result.** `lib/vexTheme.test.ts` reads `theme.css` and
  allows `--amber` in exactly two VEX rules: `.vexheld` and
  `.vexstatus.held`. The face check also asserts that no mark, badge or stale
  note is amber, under a card included. Under a card the marks keep their own
  state.

**Help (VX-UI-3, finished here).**

- Every control keeps a **permanent** hover help (decision 7). There is no
  first-use count and nothing in `localStorage`. The help shows after a short
  hover or on `:focus-visible`, is hidden while the control is pressed and
  under a card, and is named by `aria-describedby`.
- The output toolbars' controls now have help too:
  - Show terminal: "… It runs nothing."
  - Check status, which differs by button (review LOW):
    - the build's asks again and follows the build to its end;
    - the download's asks once how it ended;
    - neither starts or resends anything.
  - Refresh USB status: it returns Jarvis's latest check, which runs at most
    every 5 seconds, so the reading may be up to 5 s old (older after
    errors). "It writes nothing to the robot."
- **Refresh USB status moved to the Device tab's toolbar** (`vexcom --usb`,
  beside the other tabs' Show terminal). Inside the scrolling body, its help
  was cut off (review MED). The face check now asserts that each tip it shows
  fits inside every clipping ancestor and the window; `is_visible` cannot see
  clipping.
- **`?` help mode** lists every sentence in the output area. Each visible
  control now also shows **its number in that list** (mockup state 1; plan
  §2.6), so no callout covers another. `?` itself is numbered and listed
  too.
  - The numbers are `aria-hidden`, never focusable and take no pointer
    events.
  - A step's number sits at its bottom-left corner, because the mark holds the
    top-right and a top-left number would cover the previous step's mark. A
    tab's number, and ⤢'s and ▾'s, sit just above them.
  - Turning help mode on moves no focus. Escape or `?` closes it.
  - A card closes it and removes the numbers. It stays off after the card
    until `?` is pressed again.

**Routes.** None changed in VX-UI-2, VX-UI-3 or this section. Marks, badges
and help are computed in the window from the records the view already reads.

**Pending.**

- **VX-UI-4b**, the brain terminal's startup reply and `--quiet`: built on
  2026-10-17 after the owner's hardware check showed a blank Brain tab (see
  "Brain terminal startup, VX-UI-4b" below). H0–H4 and H9–H10 remain the
  owner's hardware session.
- Real VEX behaviour stays **UNRUN**: no tool, device or COM port was used.

Checks (fakes only; nothing installed, no live daemon, port or device):

- `lib/vex.test.ts`, under "actionState":
  - every state, stale after an edit or a clean;
  - Download stale only after a later *successful* build, unknown when its
    outcome was lost;
  - the tab badges, the Device badge's ? when stale, and the stale words.
- `lib/vexTheme.test.ts`: the amber rule and each state's colour.
- `tests/face/hud_v2_vex_check.py`:
  - the new `_marks_checks` (Download ✓ → stale after a later build, in words
    on the step, its tab and the status line; nothing disabled; nothing amber,
    a card included);
  - in `_build_checks`, the "edited since" words and the stale tab badge;
    the words giving way while a Clean runs and coming back after it; and a
    fresh ✓ after the edit with no words;
  - in `_feedback_checks`, the Device badge: a green dot, then ? when stale,
    then a red dot after two confirmed disconnects;
  - in `_help_checks`:
    - the help numbers (they match the list, sit on their own control, are
      decoration only and move no focus);
    - the toolbar help, and no tip cut off by a scroll box or the window;
    - a card closing help mode.
- The mock's `finish_build` and `build_auto` take an `ended`, so a build can
  end after a download.

Receipts for this section (fakes only):

- `hud_v2_vex_check` 299/299, and
  `hud_v2_vex_new_check` 66/66.
- The vex vitests: 73 across `lib/vex.test.ts` and `lib/vexTheme.test.ts`.
  The full vitest run passed 3186; it failed only
  `RemoteView.test.ts` "draws and ACKs only the current socket generation",
  which fails the same way on a clean origin/main (e181beb).
- `tsc --noEmit` is clean, and `tests/v2/vex_check.py` passes.
- Fourteen mutations each failed their checks. Nine came first:
  - in vitest:
    - the Build tab's badge ignoring whether the build is followed;
    - the Device badge without ? when stale;
    - a stale mark in amber;
    - no stale words;
    - Download's staleness ignoring a later rebuild;
    - the Download tab reading Build's state;
  - in the face check:
    - no help numbers;
    - the status line without its stale words;
    - the Device tab badge ignoring a stale reading.

  Then the review round's five:
  - the Refresh toolbar back inside the scrolling body (face: clipped tip);
  - the Build tab ignoring the kept records after a 404 (vitest);
  - every stale build and rebuild said, superseded ones included (vitest);
  - stale words while something runs, once in `staleNotes` (vitest) and once
    in the view's call (face, with a Clean running).

## Brain terminal startup, VX-UI-4b (2026-10-17)

Plan §5.4 and §11. **The owner's evidence (hardware check H1, live).** The
program on the brain prints unbuffered `$RUN` and `$POSE` lines every 50 ms.
The VS Code VEX extension's terminal shows them through the V5 controller
over radio. After Connect, the Jarvis Brain tab stayed **completely blank**:
not even vexcom's `vexcom utility V1.0.2b2` banner, which the download
terminal does print. That fits H4: under a sized PTY, WSL interop's console
writes `ESC[6n` and waits for a cursor report before vexcom runs at all. The
brain terminal was the one VEX program the daemon did not answer. Its answer
came only from an attached, already-replayed xterm, so a query that reached
the ring before the Brain tab attached was answered during the replay, where
the answer is dropped by design, and vexcom hung behind it.

**What changed.**

- **Startup reply.** `terminals.STARTUP_REPLY_INPUT_PROGRAMS = {"vex-brain"}`.
  The brain terminal gets the same one-shot answer as the input-disabled
  VEX programs: only when the program's very first bytes are exactly
  `ESC[6n` (split across reads or not), one fixed `ESC[1;1R`, and the query
  hidden from the ring and from any attached window. That window therefore
  never sees it and never answers a second time. A later query, a lookalike
  (`ESC[?6n`, `ESC[16n`) and a query after any other byte are never answered
  by the daemon. The reply goes to the writer's queue as protocol input. The
  owner's input path (`Terminals.serve`, the socket's guard, the latch) is
  unchanged, and typed input still reaches the program after it.
- **The vendor's invocation.** `brain_argv` is now `vexcom --quiet --user`,
  the VS Code extension's own (plan §5.2, offset 69190). The Brain tab's bar
  says `vexcom --quiet --user`.
- **The controller's port.** Jarvis had no record of the COM port: the
  download, the USB check and the device reads all let vexcom pick it, and
  `--usb`'s evidenced schema has no port field. The one place the port
  appears is the download's own output (`Device: COM3`, seen live on
  2026-10-14). The daemon now remembers, in memory, the port that the last
  **successful** download's output names. It reads only an exact
  `Device: COMn` line in the first 2 KB of output, with n from 1 to 999.
  The brain terminal is given that port (`--quiet --user COM3`) only while
  the last confirmed USB reading is a controller and no brain. That is the
  vendor's rule (offset 83443): a controller has no user port of its own,
  so vexcom relays the brain's through the controller's communication port,
  while a brain on USB has its own user port and vexcom's pick is kept. In
  every other case (no download since the daemon started, a failed download,
  a brain on USB, an unknown reading), the argv carries no port, as before.
  `check_port` refuses anything but `COM1`–`COM999`. Since the follow-up
  (review LOW of #240, below) the port is also forgotten after any
  confirmed USB change, and after a brain terminal that failed by itself.
- **Diagnostic.** Every program terminal keeps its first 4 KB of output
  (`Terminal.head`, `terminals.HEAD_CAP`), when its first byte came, its
  byte count, and whether the startup reply was sent. For the last brain
  terminal the daemon keeps a record in memory: its flags and port, the
  times, the bytes, the first 4 KB **escaped** (`vex.escaped`: every control,
  invisible character and backslash spelled out), and the exit code. The
  owner reads it at the owner-only `GET /vex/brain/record`, for example from
  the HUD's DevTools console: `await (await fetch("/vex/brain/record")).json()`.
  The daemon log's line when it ends gives the exit code, the byte count,
  the first-byte time, whether the reply was sent, and the **shape** of the
  first output (`vex.shape`): escape sequences spelled out, each run of text
  only counted (`<text 12>`), an OSC's contents counted too. So the log
  shows whether vexcom printed anything without holding a word of program
  output, which still reaches no bus, log or file (`Leaks`). The next blank
  pane can be read from the log: a shape of only `\x1b[6n` with the reply
  not sent means a query the rule did not see; the reply sent and no
  `<text …>` means vexcom started and printed nothing; text in the shape but
  a blank pane means the HUD.
- The Brain tab's "Nothing printed yet" sentence now says that Jarvis
  answers the cursor query, and that a program that is not printing, or a
  controller not linked to the brain, also leaves the pane empty.

**Not changed.** `vexcom` stays DENY for agents (`rules.py`). Connect and
Download stay the owner's trusted clicks, the brain terminal never opens by
itself, and a download never reopens it. No new vexcom flag beyond the
vendor's `--quiet` and its port argument. Nothing was run on hardware.

**HARDWARE-UNVERIFIED (only the owner's robot can settle these).** Whether
the real brain terminal's first bytes are exactly `ESC[6n` (as they were for
the download, the USB check and the device reads). Whether `--quiet --user`
without a port reaches the brain through the controller, or whether the
port is needed. Whether a stale remembered port (the controller moved to
another USB socket since the download) makes vexcom exit, and with what
message. The line endings vexcom's console writes for the program's `\n`.
Typed input reaching the program (H3) and the telemetry rate (H2).

**Fakes.** The fake `vexcom` now models the brain's start: with
`STARTUP_DSR`, `--user` writes the query and **hangs**, printing nothing,
until the reply comes. It prints a banner unless `--quiet`, says the port is
busy and exits 3 with `PORT_BUSY`, and streams `$RUN` then a `$POSE` line
every 20 ms with `STREAM`. Its download prints `Device: COM7` and the
banner first (`DOWNLOAD_PORT` replaces the port line; `FAIL_DOWNLOAD` exits
5).

Checks (fakes only; nothing installed, no live daemon, port or device):

- `tests/v2/vex_output_check.py`:
  - `StartupProtocol`: the brain's first exact query answered once and
    hidden in every split, with a window already attached; a later query,
    a mid-stream lookalike and a query after other output not answered; the
    reply written before the owner's typed input, which still flows; the
    head capped and kept only for program terminals.
  - `BrainHelpers`: `download_port`, `escaped`, `shape`.
  - `BrainStartup`, against the hanging fake: it starts with no window
    attached and its output waits in the ring; streamed `$POSE` lines reach
    an attached socket live and typed input reaches the program; a busy port
    ends it, frees the device and is recorded; with the brain left out of
    the rule it hangs blank (the owner's symptom); the record route is
    owner-only and says what ran.
  - `BrainPort`: the controller's port from a download, never with a brain
    on USB, an unknown reading, a failed download or an odd port line.
- `tests/v2/vex_check.py`: the exact `--quiet --user [COMn]` argv and
  `check_port`'s refusals; `Leaks` holds the brain's log line to shape
  only.
- `tests/face/hud_v2_vex_check.py` `_brain_checks`: 120 streamed `$POSE`
  lines are drawn live in the Brain tab. The newest is on screen, the first
  is in the scrollback, and the pane has a real size.

## Connect stayed off after a download ended (2026-10-17)

**What the owner saw, live.** A download to slot 1 finished. The view said
"Downloaded to slot 1. Start it on the brain or the controller." and the
Build ✓ and Download ✓ tabs were green. The Brain tab said the terminal "was
closed for the download. Click Connect…", but **Connect stayed greyed out**.
Its tip said "A download holds the robot's port; Connect once it ends." That
is the non-chain `device === "download"` branch of `brainView`
(`hud/src/lib/vex.ts`): the window's `/vex/status` still said `download`.

**Cause: the window's copy of the status, not the daemon.** The journal for
that evening shows the daemon freed the device after every download. The
info read at 20:21:16 came after the exit-4 download at 20:17:30. The brain
terminal at 20:26:25 opened without a refusal after the successful download
at 20:22:20. A reload, or focusing the window again, always cleared it,
because the view reads the status when it mounts and on focus. The stale read
came from `heardDevice` (`hud/src/state/vex.ts`): the bus's
`vex_device {action: "download"}` record, published at the download's start
(`vex.py` `_announce`), had the window read `/vex/status` while the daemon
held the device. Nothing read it again when the download ended. There is no
bus record for an end, the chain's `finally` read nothing, and the status is
otherwise read only on mount, focus or the owner's refresh. Every exit
behaved the same way: success, failure, an instant exit or Stop. A device read
(⋯) had the same flaw with `"info"`.

**The fix:**

- **The daemon.** A download's exit hook now frees the device in the same
  locked step that settles its record (`release(locked=True)` in
  `Vex.download`'s `ended`). Before, the record said it had ended one lock
  section before the device was freed. A `/vex/status` read in between, just
  after the HUD saw the record end, could still say `download`. The
  `release()` in the `finally` still runs if the settle raised, and it does
  nothing once the device is free.
- **The HUD store:**
  - The download chain's `finally` reads the status again, however the chain
    ended.
  - `refreshDownload` reads it again when it first sees a record end. This
    covers the chain's own follow and a later Check status.
  - `readInfo` reads it again once its read has answered.
  - `refreshStatus` keeps only the newest read's answer (`statusSeq`). Without
    that, the bus's earlier read could answer last and bring `download` back.
- **The view.** While the status says `download` or `info` and no chain of
  this window's runs, `VexView` reads it again every `DEVICE_POLL_MS`
  (2 s, only while the window is visible). This is for another window's
  download, whose end no chain here sees and no bus record announces.

The rules are unchanged: one device action at a time; a download only on the
owner's click; Connect off while a download really runs (the daemon still
says `download` then); the brain terminal never reopens by itself
(VX-UI-4a). Every addition only reads `/vex/status`.

Checks (fakes only; no vexcom, port or live daemon):

- `tests/v2/vex_check.py` `DownloadEnd`:
  - success, exit 4, an instant exit 6 and Stop (a `DELETE` of the
    download's terminal), plus nine ends in a row;
  - `Vex._lock` is wrapped so that every time it is let go, no record this
    test started says it ended while the device is still the download's;
  - the fake vexcom takes `DOWNLOAD_EXIT` beside it, for the exit code.
- `hud/src/state/vex.test.ts`, "Connect is offered once a download ends":
  - the four exits, and the brain closed for the download;
  - a forgotten record (404);
  - a later Check status;
  - out-of-order status answers;
  - a device read.
- `tests/face/hud_v2_vex_check.py` `_download_end_checks`:
  - the four exits, in the real Brain tab;
  - a reload, which always showed Connect free;
  - another window's download, with the owner's exact sentence until it ends.
- The mock now holds the device as `download` while any download record runs,
  as the daemon does, and `end_download` ends one. With that,
  `_brain_checks`' "offers Connect again" check also reproduced the bug.

Receipts:

- **Before the fix:**
  - all five `DownloadEnd` tests failed;
  - 9 of the new vitests failed;
  - six face checks failed against the origin/main bundle, the four exits,
    another window's download and `_brain_checks`' check, each with the
    owner's sentence.
  - The plain HTTP "status at once" assertion alone did not catch the daemon
    race; only the watched lock did.
- **After the fix:**
  - `hud_v2_vex_check` 326/326 and `hud_v2_vex_new_check` 66/66;
  - all 48 store vitests pass;
  - `vex_feedback_check` and `vex_output_check` pass;
  - `vex_check` 152 of 153 pass. It fails only
    `HereDocuments.test_the_body_backstop_is_judged_fast`, a wall-clock
    bound that fails the same way on a clean origin/main.
- **Mutants**, each failing its check:
  - no newest-read guard;
  - no refresh at the chain's end (the 404 case);
  - no refresh when a read sees the end (Check status);
  - neither refresh (seven vitests);
  - no refresh after a device read;
  - no poll in the view (face: another window's download);
  - the daemon's settle and release split again (`DownloadEnd`).

## Review follow-ups of #240 and #241 (2026-10-17)

Branch `claude/vex-brain-followup`.

- **Startup reply, whole reads** (#240 LOW 1). The negative table in
  `StartupProtocol` fed each case one byte per read, so a rule that
  answered the query anywhere in the first read (`query in initial`)
  passed it. Each case is now also fed as one read, and that mutant fails.
- **A stale brain port** (#240 LOW 2). `Vex._download_port` keeps the
  port together with the USB reading's last confirmed change at the time of
  the download (`_usb_mark`: the newest of `Connection`'s events). `_brain_port`
  forgets the port once that mark has changed: a controller unplugged and moved
  to another socket (COM7 → COM8) is two confirmed changes, and so is the first
  reading after the download. It is also forgotten when a brain terminal that
  was given it fails by itself (a non-zero exit while `Terminal.closing` is
  false). The owner's Disconnect closes the terminal (exit 129) and keeps
  the port. After either, the next Connect lets vexcom pick, until the next
  successful download. A move between two USB checks that the poll never
  saw is invisible: the connection record keeps no serial number or port by
  design. The first failed Connect is then what forgets it.
- **Held-port poll** (#241 LOW 1). The 2 s `/vex/status` poll while a
  download or device read holds the port now runs only while the view is
  drawn and no card is up, like the USB check.
- **Superseded status reads** (#241 LOW 2). `refreshStatus` keeps its newest
  read (`statusLatest`). A superseded read, whether it succeeded or failed,
  settles only after that newest one, so the download chain's `await
  refreshStatus()` then reads the newest answer, never an older one.
- **An older failing read** (#241 LOW 3). A test now holds that it never
  overwrites `statusError` after a newer success. The reviewer's mutant M5
  (no guard in the error handler) fails it.
- Not done: the NIT about owner keystrokes queued before the `ESC[1;1R`
  reply. The HUD sends nothing until a window has attached and its replay
  is parsed. The query comes at spawn, so the reply already precedes any
  typing. Putting it at the head of the queue would change the reply's call
  in every startup test for an ordering nothing produces.

Checks (fakes only):

- `vex_output_check`: 31 tests, including `BrainPort`'s USB-change,
  first-reading, Disconnect and failure cases.
- `state/vex.test.ts`: two new tests.
- `_download_end_checks`: no status poll for 2.8 s under a card while
  another window's download holds the port.

Eight mutations each failed their checks:

- the startup rule answering the query anywhere in the first read;
- the USB change ignored;
- the mark always None;
- the Disconnect counted as a failure;
- a brain failure keeping the port;
- no guard in the error handler (M5);
- a superseded read returning at once;
- the held-port poll not gated on a card (face).
