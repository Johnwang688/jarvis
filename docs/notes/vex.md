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
