# VEX Download feedback and USB connection diagnostics

Scope: make an explicit owner Download show the same visible progress/output and
reliable completion as Build; distinguish configured tooling from physical V5 USB
Brain/Controller presence and retain a small timestamped connection log. This is
separate from the Browser batch. No real tool/device operation is used in tests.

## Existing boundaries

Preserve explicit trusted primary-pointer Download only, one chain per window,
card/crash/cancel invalidation, bounded Build-first/rebuild, no run-after-download,
program PTYs, fixed argv, executable fingerprints, project/build locks and one
device reservation. A probe never closes/reopens the brain terminal. The HUD uses
existing Graphite tokens. Output is untrusted text, bounded and memory-only.

## Download contract

`POST /projects/{id}/vex/download` remains additive: return its existing
`terminal_id` plus a `download_id`. `GET /vex/downloads/{id}` is owner-only and
returns `{id, project_id, slot, state, terminal_id, exit_code, output, started,
ended}`, where state is running/succeeded/failed/timed_out. Keep at most 32 records
and the existing bounded plain-text output tail. Register the record before
spawning; the actual PTY EOF/on_exit hook owns final output/state and releases
exact device/build reservations. Query code never invents completion from a
missing/stale terminal listing. Fast exit, failed start, timeout and closed
terminal cases must settle exactly once. Existing full terminal stays accessible.

HUD starts elapsed/progress immediately, shows the terminal and inline bounded
output, retains final status and Show terminal, retries failed status reads with
a visible warning and bounded deadline. Unknown/lost status says so and offers a
recheck; it never reports success or sends another Download. Existing chain/card
and brain-resume rules remain authoritative.

## USB schema evidence (source inspection only)

The installed vendor-distributed readable JavaScript was inspected, never run:
`vexrobotics.vexcode-0.8.2026020401/dist/extension.js`, SHA-256
`df1207f9f9889bb24ad3448f0979b2813fe1b51c1725f736d63d8a014fd32ab5`.
Its fixed listUSB operation uses `--usb` (character offset 69284). Its parser
reads `usb.vex`/`usb.dfu` arrays and vid/pid strings (offset 166710). Its device
mapping uses VEX VID 2888, PID 0501 V5 Brain / 0503 V5 Controller (offsets 154957,
155217 and 178133). No vendor executable was decompiled or copied. The old fake
`devices[]` system-info shape is explicitly not evidence. The new fixed read-only
flag needs root/independent review before implementation.

## Proposed shared connection contract

Owner-only `POST /vex/device/connection {}` expresses visible-window demand and
returns cached typed state immediately. A shared daemon worker coalesces windows,
starts at most one verified `vexcom --usb` read, retains at least a five-second
minimum interval and uses increasing backoff on errors. The read uses the same
program PTY, fingerprint check and device reservation, a short two-second bound,
and exact holder cleanup. Polling never waits for or interrupts brain/download/
manual-info ownership; busy state retains the last confirmed values and timestamp.
Owner actions must get priority over future probes; an owner request may wait
boundedly for an already-running read-only probe, never queue behind new probes.

Only strict bounded recognized USB records may update confirmed booleans. Empty
recognized lists confirm absent USB devices. Unknown/malformed/truncated output,
unsupported schema/device identity, nonzero exit, setup/tool error, timeout and
busy states never become Disconnected. Keep `checked_at`, last confirmed values,
current check/error/stale state and at most 32 timestamped confirmed transition
entries in memory; never return serial numbers, names or raw probe output.
Announce each actual probe with a fixed id-only connection action/time record;
background probes must not masquerade as unclicked downloads/manual device reads.

The UI has separate Tooling and USB Brain/USB Controller indicators, last-check
and stale/busy/error wording, an explicit Refresh, and a bounded event log. It
polls only while a VEX view is visible, with one in-flight request in the window,
and stops under cards, crash, hidden window and active download chain. USB
presence does not assert radio link, program running, download readiness, or
extension-host health. A missing VS Code process is not a hardware disconnect.

## Focused verification before independent review

Use only temporary HOME/config and fake tools/devices: live output then actual
exit, fast exit, failed/timeout/forgotten status, stale terminal list, card and
chain guards; strict USB schema/type/size checks, connected/disconnected/reconnect
log, unknown/error/busy/stale preservation, cross-window coalescing/min interval/
backoff, owner action priority and exact cleanup. Actual mock-HUD Download output,
completion, keyboard refusal, plain text and narrow layout; TypeScript/build and
appropriate existing VEX/terminal regressions. No heavy jobs until root grants a
slot. Real-device verification remains a separately identified owner step.
