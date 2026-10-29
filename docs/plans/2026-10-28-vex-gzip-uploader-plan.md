# VEX gzip uploader (plan, 2026-10-28)

Status: **stopped (2026-10-28), not built.** The owner's `vexcom --help` lists
`--nocompress` ("disable program compression for V5/EXP"), so vexcom already
compresses by default and this plan would gain nothing. See the decisions file.
Kept as a record of the research.

Reads: `CLAUDE.md`, `docs/notes/vex.md` (the download, `--chan 1` and
skip-the-build sections), `jarvis/v2/vex.py` (module docstring,
`download_argv`, `verify_tools`, `Vex.download`), `jarvis/v2/vex_setup.py`,
the vexcom DENY in `jarvis/rules.py`, `hud/src/lib/vex.ts`
(`downloadProgress`).

## 0. The owner's brief (2026-10-28)

Downloads through the V5 controller are slow: the override project, 156 KB,
took about 80 s (~2 KB/s, 2026-10-20). The owner asked what else would make
them faster, chose gzip as the next step, and asked for this plan. A
differential ("only what changed") upload is **out of scope**. It needs a
patcher in the program's own startup code (vexide took "a month-long
struggle" with the instruction cache), about 1–2 weeks. Revisit it only if
gzip is not enough.

## 1. Evidence

Measured here (gzip -9; bsdiff for comparison):

| Binary | Raw | gzip | bsdiff vs previous |
|---|---|---|---|
| `override-brake.bin` | 89,888 | 45,167 (50%) | — |
| `override-telemetry.bin` | 93,312 | 47,100 (50%) | 7,039 (a 1,944-line change) |
| `14147S-override.bin` | 12,872 | 5,795 (45%) | — |

From readable public sources (no vendor binary was decompiled):

- **VEXos accepts a gzip-compressed program.** PROS's uploader
  (`pros-cli`, `pros/serial/devices/vex/v5_device.py`) has
  `write_program(..., compress_bin=True)` by default. It gzips with
  `mtime=0`, writes `slot_N.bin` plus a `slot_N.ini` (name, slot, icon,
  version), loads at `0x03800000` in chunks of `max_packet_size / 2`, and is
  gated on system version ≥ 1.0.5. Its `@with_download_channel` switches the
  controller first.
- vexide's `cargo-v5` (MIT) also gzips by default (`Compression::best()`,
  `FileTransferTarget::Qspi`). Its `switch_to_download_channel` sends
  `FileControlGroup::Radio(RadioChannel::Download)`, waits up to 8 s for the
  controller to drop and up to 8 s for it to come back on channel 5. It never
  switches back to the pit channel.
- vexide's own figure: a 60 kB wireless upload "takes up to 20 seconds".
- The vendor extension (0.8.2026020401, `dist/extension.js`) zips only a
  project archive (`slot_N.binz`). It never compresses the `.bin` it hands to
  vexcom.

**Unknown, and it decides everything:** whether `vexcom.exe` gzips the
binary itself. If it does, this whole plan gains nothing. §2 settles it
before any code is written.

The library is `vex-v5-serial` 0.5.2 (MIT, vexide, 2025-11-27). Its pieces
are `serial::find_devices`, `SerialConnection`, `commands::file::UploadProgram`
/ `ProgramData` / `USER_PROGRAM_LOAD_ADDR`, and the `vex_cdc` packets
re-exported as `protocol`.

## 2. VG-0: measure first (the owner, no code)

Download is the owner's click only, and agents are refused anything that
names vexcom. So the owner runs these by hand in a Windows terminal, from
`build\`, to an unused slot (8):

1. **Does vexcom compress?** Make two files of the same size as the real
   binary: one all zeros, one random bytes. The lead prepares them in the
   project's `build\`; neither is ever run.
   ```
   vexcom.exe --name zeros --slot 8 --write zeros.bin --chan 1 --progress
   vexcom.exe --name random --slot 8 --write random.bin --chan 1 --progress
   ```
   If the zeros file goes much faster, vexcom already compresses: **stop
   here**, and the plan is moot. If the times match, it does not.
2. **The `--chan 1` baseline** (open since #285): time a normal HUD
   Download of the real project through the controller.
3. **The USB baseline:** the same Download with the brain on USB (no
   controller).
4. Download a real program to slot 8 afterwards, so no junk is left in a
   slot.

Expected, if vexcom does not compress and the radio runs at ~2 KB/s:

- the 90 KB project: ~45 s → ~25 s;
- the 13 KB project: ~7 s → ~4 s.

The fixed costs stay the same: the channel switch (a few seconds), the file
open and the metadata.

## 3. Which uploader (owner question 1)

The uploader must run on **Windows**. WSL sees no COM ports (usbipd would
take the device away from VS Code). Windows has `cargo.exe` and Python
3.12/3.14.

- **A. `cargo-v5.exe` as is** (`upload --file x.bin --slot N`). No code to
  write, but it is a broad tool: terminal, run, field control, and more. Its
  output is an ANSI progress bar that `downloadProgress` cannot read, and its
  exit codes are its own. Trusting it means trusting all of it.
- **B. Our own small Rust tool on `vex-v5-serial` (recommended).** About
  200–300 lines in `tools/vexcom-gz/`. It does one thing, upload, with
  vexcom's argv shape, output shape and exit-code table, so nearly nothing
  downstream changes. Built from pinned source with Windows cargo.
- **C. Windows Python plus a pyserial port of PROS's `write_file`.** That
  means writing the protocol by hand (PROS is MPL-2.0, read as reference
  only). It is a general interpreter in the fingerprint, and it needs pyserial
  installed on the Windows side. The weakest of the three.

The rest of this plan assumes **B**.

## 4. VG-1: the tool, `vexcom-gz.exe`

**Name.** `vexcom-gz` keeps the word `vexcom` in it. The existing
word-anywhere DENY in `rules.py` then already refuses an agent line that
names it. The builder checks that this holds, and adds the name to
`_VEX_TARGETS` for the program-position glob rule (§6).

**Argv, fixed, mirroring vexcom's download:**

```
vexcom-gz --name <N> --slot <1-8> --write <file.bin> [--chan 1] --progress
```

- The same `DOWNLOAD_NAME` rule.
- No other flags: no run, erase, terminal, firmware or file read. Anything
  else is a usage error, exit 2.
- `--chan 1` switches a wireless controller to the download channel, the
  way cargo-v5 does (8 s caps each way). It does not switch back, the same as
  cargo-v5. Whether vexcom switches back stays the open LOW-4 robot check.

**What it sends:**

- The binary gzipped (best, `mtime=0`), as a program upload: vid user,
  target flash/QSPI, `USER_PROGRAM_LOAD_ADDR`, `slot_N.bin` plus `slot_N.ini`.
- Name, slot and the default icon, the same metadata vexcom writes; the
  builder confirms it with `vexcom --programs` after a download.
- After the upload: **don't run** (V-3).
- It refuses a file over a cap (4 MB) and a VEXos older than 1.0.5. In that
  case the owner downloads with vexcom.

**Output, matching vexcom's so the daemon and HUD parse it unchanged:**

```
Device: COM3
vexcom-gz 0.1.0 (gzip 93312 -> 47100 bytes)
download :
0....10....20....30....40....50....60....70....80....90....100
```

`download_port` and `downloadProgress` read these lines as they read
vexcom's. Errors are one plain line on stderr.

**Exit codes, vexcom's table** (from the extension, offset 77619):

- 0: success.
- 4: a command error, a NACK or a timeout mid-transfer (this is what a radio
  dropout looks like), so the HUD's exit-4 retry applies unchanged.
- 5: a device error: a refusal, the VEXos version, or a wrong device.
- 6: a port error: no device found, or the port won't open.
- 2: a usage error.

**Tests (cargo test, no device):**

- argv parsing, including every refused flag;
- gzip round trip, and that `mtime=0` makes it deterministic;
- the progress-line format at 0, 34, 70 and 100%;
- the exit-code mapping;
- an in-memory fake `Connection` that records the packet sequence: init,
  chunks, exit, ini, and the channel switch only with `--chan 1`.

**Build.** `Cargo.lock` is committed, with `vex-v5-serial = "=0.5.2"`.
`cargo build --release --locked` for `x86_64-pc-windows-msvc`.

## 5. VG-2: setup and daemon

**Setup** (`jarvis vex setup`, human-run, TTY, y/N as today):

- It offers "Build the gzip uploader?". On a yes it copies `tools/vexcom-gz/`
  to `%LOCALAPPDATA%\jarvis\vexcom-gz\src`. Windows cargo builds slowly
  from a `\\wsl$` path.
- It runs `cargo.exe build --release --locked` there.
- It records the exe path in `vex.json` as a new optional key, `uploader`,
  with its SHA-256 beside the others.
- It **refuses to build** when `tools/vexcom-gz/` has uncommitted changes,
  and prints the commit it built from. An agent can edit that source, so the
  owner's rebuild must take reviewed, committed code only (owner question 3).
- `check_path` gets the one new root, `%LOCALAPPDATA%\jarvis\vexcom-gz`.
  Agents can write it, like the vendor's AppData folders, and the
  fingerprint is the guard, as it is for those.

**Daemon** (`vex.py`):

- `EXE_KEYS` stays as it is. `uploader` is optional (`OPTIONAL_KEYS`), and
  it is hashed only when used.
- `download_argv(..., uploader=True)` builds the argv in §4 from the
  `uploader` path. `verify_tools(cfg, "uploader")` runs immediately before
  the spawn.
- `Vex.download` uses the uploader when `vex.json` has `uploader` and
  `download_with` is `"gzip"` (owner question 2). Otherwise it uses vexcom,
  as today.
- **Nothing falls back on its own:** a failed gzip download is never resent
  through vexcom. The exit-4 retry stays the only retry.
- The log line says "(gzip)" beside "(controller download channel)".
- **Unchanged:** the program name (`vex-download`, so it is already in
  `terminals.STARTUP_REPLY_PROGRAMS`), the device reservation, the build
  lock, the `_fresh` rule, `DOWNLOAD_TIMEOUT_S` (180 s), the `--chan 1`
  rule (`_controller_only` with a fresh reading), Stop (SIGTERM over
  interop, with the same "slot N may hold an incomplete program" wording),
  the `vex_device` bus record, and the Python refusal.
- `/vex/status` gains `uploader: "gzip" | "vexcom"`. The HUD's Download
  hover help says which one is used. No new button.

## 6. Safety

- **Download stays the owner's click only.** There is no new tool, MCP tool,
  CLI verb or Discord verb. `vexbuild` still builds only.
- **`rules.py`:**
  - `vexcom-gz`/`vexcom-gz.exe` join `_VEX_TARGETS`.
  - The builder adds DENY cases next to the vexcom ones: a literal path, a
    `cmd.exe /c` string, a `uv run` runner, and globs such as `vexcom-g*`
    and `*com-gz.exe`.
  - The builder also adds "too complex" cases, and a differential over the
    existing vexcom corpus that should move nothing but the new name.
- **`makefile_refusal`** refuses a make file that names the new name too.
- **`vex.json`** is already gate state. The new keys change nothing there.
- **The residual gap, unchanged and stated:** an agent can still write its
  own serial script and reach a plugged-in brain, as `vex.py`'s docstring
  says today. This tool adds no new reach. It is one more program behind the
  same fingerprint and DENY.
- **Supply chain:**
  - The pinned `Cargo.lock` and `--locked`.
  - The owner's setup builds dependency build scripts (`serialport` and its
    dependencies) on their own machine.
  - The vendored dependency list goes in the PR for review.

## 7. Tests (fakes only; no device, no live daemon)

- `tests/v2/vex_fake/` gets a fake `vexcom-gz`: §4's output shape, and an
  exit code taken from its argv.
- `vex_check` `Tables`:
  - the exact uploader argv, with and without `--chan 1`;
  - vexcom when `uploader` is missing or `download_with` is `"vexcom"`;
  - refused when the hash changed.
- `vex_output_check`: the gzip output parses to the port and to 100%.
  Exit 4 retries, and 5 and 6 do not.
- `vex_setup` checks:
  - refuses a dirty `tools/vexcom-gz/`;
  - records the hash;
  - no cargo run without a yes. The fake `cargo.exe` lives in a temp tree.
- The `rules` DENY cases in §6.
- `hud_v2_vex_check`: the hover text names the uploader. Only that section
  runs while iterating, and the full suite runs once.
- A few mutations, scaled to the risk: argv order, the uploader without its
  hash check, the DENY name dropped, the automatic fallback.

## 8. Packages and order

| Id | What | Who | Size |
|---|---|---|---|
| VG-0 | §2 timing test; go / no-go | owner, by hand | 15 min |
| VG-1 | `tools/vexcom-gz` with cargo tests | builder (Opus: device protocol) | ~1 day |
| VG-2 | setup, daemon, rules, fakes, HUD hover, docs | builder (Opus: gate change) | ~1 day plus review rounds |
| VG-3 | owner live check: controller download, timing, `--programs` metadata, exit 4 on a pulled radio, Stop mid-download | owner | 20 min |

VG-1 and VG-2 can run in parallel once §9 is answered. VG-2 uses the fake
until VG-1 lands. The notes and `docs/hud-api.md` change in VG-2. CLAUDE.md
gets no new lines, as it is at its size cap.

## 9. Open questions for the owner

1. **Which uploader:**
   - **(Recommended) B:** our own `vexcom-gz` on `vex-v5-serial`.
   - A: cargo-v5 as is.
   - C: a Windows Python port.
2. **After VG-3 passes, the default:**
   - **(Recommended)** gzip by default, with vexcom kept as a `vex.json`
     setting (`download_with`) that setup can switch back.
   - A HUD toggle instead.
   - Keep vexcom by default.
3. **Building the exe:**
   - **(Recommended)** setup builds it after a y/N, and only from a clean,
     committed `tools/vexcom-gz/`.
   - The owner builds by hand, and setup only fingerprints.
4. **Go / no-go:** proceed only if VG-0 shows vexcom does not compress
   (recommended), or build VG-1 now in parallel with the test.
