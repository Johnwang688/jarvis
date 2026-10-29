# VEX gzip uploader — owner decisions (2026-10-28)

Answers to §9 of [the plan](2026-10-28-vex-gzip-uploader-plan.md).
This file wins over the plan.

1. **Uploader: B.** Our own `vexcom-gz` in `tools/vexcom-gz/` on
   `vex-v5-serial` (MIT). It does upload only, with vexcom's argv, output
   and exit-code shape.
2. **Default: gzip, with vexcom via setup.** Once VG-3 passes, Download
   uses the uploader. `jarvis vex setup` can switch it back with
   `download_with: "vexcom"` in `vex.json`. There is no HUD toggle, and
   nothing falls back on its own.
3. **Build: setup builds it.** On the owner's y/N, `jarvis vex setup` runs
   Windows `cargo build --release --locked`, only from a clean, committed
   `tools/vexcom-gz/`, and records the SHA-256.
4. **Test first.** No VG-1/VG-2 code until VG-0 shows that vexcom does not
   compress. If the zeros file downloads much faster than the random one,
   the plan stops.

VG-0 files, made by the lead on 2026-10-28:

- `C:\Users\johnw\Documents\Robotics\vexcom-gz-test\zeros.bin`: 90,112
  bytes of zeros, which gzip to 132 bytes.
- `random.bin` beside it: 90,112 random bytes, which gzip to 90,156 bytes.

Neither is a program, and neither is ever run.

## Stopped (2026-10-28, same evening)

VG-0 was settled without a brain. The owner ran `vexcom.exe --help`
(V1.0.2b2) at home. It lists:

```
--nocompress    disable program compression for V5/EXP
--timer         show transfer timer
--chan n        set radio channel 0=pit, 1=download
```

**vexcom already compresses programs by default**, and Jarvis's argv never
passes `--nocompress`. A gzip uploader would send the same bytes, so
VG-1..VG-3 are not built. The 156 KB in 80 s was already about 79 KB of
gzip on the radio, roughly 1 KB/s.

What still helps, from this help text and the earlier analysis:

- A **smaller compressed binary.** Time scales with gzip size, so dead code
  and library weight are the lever.
- **USB straight to the brain** in the pit.
- `--timer`, to measure a transfer exactly.
- `--chan 0`, to return the controller to pit after a download (the open
  LOW-4).
- A **differential upload** (1–2 weeks).

The lead deleted the test files in `Robotics\vexcom-gz-test` the same evening.
