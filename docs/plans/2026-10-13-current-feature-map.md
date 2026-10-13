# Jarvis current feature map and team ownership

Updated 2026-10-13 (America/Chicago). This is the shared coordination map for the
2026-10-12 checkpoint, the five plans merged in [PR #125](https://github.com/Johnwang688/jarvis/pull/125),
and John's subsequent VEX, LaTeX and Browser-controls requests. It is a working
registry: update the relevant row when claiming, handing off, opening a PR,
merging or deploying. It is not a new authorization to expand product scope.

**Four packages are TAKEN by the current Codex team. Do not duplicate them:**
`B-UI`, `B-DR`, `B-SOUND`, `B-CLASSIFIER` below. No other remaining implementation
package is claimed by this team in this map. A saved draft is not an active claim.

## How teams coordinate

- **TAKEN**: exclusive implementation ownership. Coordinate shared-file changes
  with the named lead; independent review is welcome.
- **READY**: unclaimed; prerequisites are present. Claim before implementation.
- **WAIT**: unclaimed, but named dependencies must land first. Planning, isolated
  helpers and fixtures may proceed after a claim and an agreed interface.
- **HANDOFF**: unclaimed saved work exists. Inspect the draft and its failures
  before deciding what to reuse; do not start a duplicate from scratch.
- **VERIFY**: evidence or a capability/owner decision is missing. Investigate
  before promising an implementation or calling it shipped.
- **MERGED / INSTALLED**: code landed / the named build was deployed. Neither word
  alone means physical-device or real-account acceptance passed.
- **DEFERRED**: outside the currently authorized implementation, or conditional
  on a measurement/decision. Do not silently turn it into active work.

Claim protocol: read this file from current `origin/main`; make a small claim-only
PR updating the row with team/lead, branch/worktree, scope and dependencies. The
claim becomes effective when that update merges. If two claims race, reconcile
ownership before product edits. Use the feature ID in PR titles/descriptions;
link dependencies and record exact review/test heads. A team may hand off a row
explicitly; an old date, quiet session or preserved worktree does not transfer it.

Each implementation gets its own worktree. Do not edit another team's worktree,
reset its branch, force-push, or sweep in untracked owner files. Share proposed
interfaces in the feature's PR/plan before changing a common host, provider,
`App.tsx`, workspace state or API contract. GitHub state and this registry are the
handoff record; private agent messages and local cache logs alone are insufficient.
Update this map in the feature PR, including a new owner if work is handed off.

The current Codex lead owns integration/deployment for its four packages. Other
teams should name their integrator in their claim. Serialize merges and deployments
through the leads; integrate latest main and review conflicting hunks before merging.
A documentation-only merge does not need a daemon restart.

## Verified baseline

- Main and origin matched `711577cc302d9bf9ca7eae5de340702d3cd4d2a1` at this snapshot.
- Installed HUD: `20261013T175916735476Z-4c3252d7de89`, built from that main commit;
  normal rebuild/restart succeeded and all three providers were healthy.
- **Production Browser driving is still OFF in that installed build.** Backend
  and owner presentation have landed; the enabling release below has not.
- GitHub had no open implementation PRs at the initial snapshot; newer PR links
  and progress are recorded in the TAKEN rows below.
- Local evidence: `/home/johnw/.cache/jarvis-lead/codex-oct13/execution.md` and
  per-package receipts there. These local files are not part of the repository.
- The original checkpoint and questions log are owner-owned, untracked files in
  `/home/johnw/projects/Jarvis/docs/plans/`. This map does not move or stage them.
  Older plan headings saying “docs only,” “M11 never run” or “driving remains OFF”
  describe their publication snapshot; use the dated status and evidence here.

## Taken now — current Codex team

Worktrees below are under `/home/johnw/.cache/jarvis-lead/wt/`.

| ID | Feature and acceptance scope | Status / owner | Branch / worktree | Current progress and remaining gate |
|---|---|---|---|---|
| B-UI | Compact Browser navigation, three-dot options, automatic localhost Direct / external Remote selection; Private remains Remote | **TAKEN — Codex lead; implementer `vex_fix`** | `codex/browser-controls` / `browser-controls` | Focused tests and build pass; actual browser suites and independent review in progress. Options must block page/agent input while open, retain visible errors/driver status, and preserve capture, annotation, focus and approval behavior. Remove manual mode selection; explicit capture fallback remains. |
| B-DR | Release guarded fast-path Browser driving with global and per-pane switches, named-profile defaults and transient Private opt-in | **TAKEN — Codex lead; implementer `plans_voice`** | `codex/driving-release` / `driving-release` | Product head `546bfccb`; focused backend/prompt checks, 1,830 HUD unit tests, 43 browser checks and mutation checks passed. Full integration and independent release checks remain. Needs B-CLASSIFIER result; integrate B-UI and sound settings without weakening their guards. [Release plan](2026-10-13-browser-driving-release.md). |
| B-SOUND | Owner-only Remote sound, master permission, memory-only profile state, structural mute, guarded profile restart and visible indicators | **TAKEN — Codex lead; implementer `sound_reconcile`** | `codex/browser-sound` / `browser-sound` | Integrated main at `26817560`; fixing independently found failed-restart recovery before final sweep/review. Chosen route is per-profile restart, with clear all-tabs label; no automatic cross-site re-mute claim. No physical-audibility acceptance yet. Voice ducking belongs to V15, not this PR. [Sound plan](2026-10-13-browser-sound.md). |
| B-CLASSIFIER | Stop Haiku 5.5 truncating bounded Browser-review JSON; reject incomplete response metadata and retain ASK on failure | **TAKEN — Codex lead; reviewer `sound_reconcile`** | `codex/browser-classifier-budget` / `browser-classifier-budget` | Reviewed product `81f1fd0`; 37 canonical, 4 wire, 11 independent checks and 11 mutants passed. Approved 50-call confirmation: 49 valid replies, one timeout/ASK, zero truncation; p50 1.364650 s, p95 1.734687 s. [PR #130](https://github.com/Johnwang688/jarvis/pull/130) is open; current-main integration and four relevant regression suites passed. Merge/deployment remain. All M11 spending charged/reserved is $0.01187951250, including unknown timeout reserve. Both paid 50-call approvals are consumed. |

## Browser and native-agent work still to assign

| ID | Feature | Status / owner | Dependencies / definition of done |
|---|---|---|---|
| B-NATIVE (BR-9d, BD-a/b/c) | Claude and Codex parent chats drive through the same Browser gate | **HANDOFF — unclaimed** | Depends on B-DR release and current Browser lifecycle. Saved branch `codex/browser-drive-mcp`, worktree `browser-drive-mcp`, head `10d601b`. Earlier source review and focused tests exist, but the interrupted full sweep is not a pass. Update latest desk steer, relay-abandonment behavior and approximately 115-second card wait; complete native LB-1…LB-6 for both providers. Parent-only, desk-only, one shared budget/driver; child calls remain refused. [Plan](2026-10-13-browser-driving-claude-codex.md). |
| B-AUTH (BR-7) | HTTP Basic/Digest owner challenge form over the existing authenticated tab socket | **HANDOFF — unclaimed** | Saved `codex/browser-http-auth` / `browser-auth`, committed head `158c63a` plus uncommitted draft. Synthetic production-host Basic-auth capability passed; final protected navigation, timeout, stale socket, privacy and cleanup gates remain. Integrate accepted B-SOUND activity/restart seam before shared-host changes. Never use real account credentials in fixtures. |
| B-FIND (BR-7) | Find-in-page, bounded owner context menu, finite cursor shapes | **HANDOFF — unclaimed; plan/probe only** | `codex/browser-owner-tools` / `browser-owner-tools` at `fb34634`, no product implementation. Approved local plan and seven-case Window.find probe exist. Integrate B-UI/B-SOUND/B-AUTH shared surfaces deliberately. Top-document find, bounded work, exact page/socket/hold identity, no arbitrary evaluator or DOM highlight rewriting. Native layout preemption remains unproven. |
| B-PICKERS (BR-7/M9) | Owner select/date/color fallback; assess datalist separately | **VERIFY — unclaimed** | Chrome 155 select/date/color popups were absent from screencast; datalist opening was not proven. Refine and review exact-element fallback design before implementation. Preserve input/change revalidation, bounds, focus, native-control privacy and stale-element refusal. Shared host/RemoteView integration waits for active Browser packages. |
| B-IME (BR-7) | Finish real owner IME/composition acceptance | **VERIFY — unclaimed** | Composition transport exists in Remote UI; do not rebuild it blindly. Audit existing coverage, then validate real Windows composition and cancellation across holds/reconnects. Implement only demonstrated gaps. |
| B-DIRECT-CAPTURE (BR-4/M6) | Direct iframe capture in installed app mode | **VERIFY — unclaimed** | Remote capture/annotation is installed. Direct currently offers explicit “Open in Remote” fallback. Requires actual Windows app-mode getDisplayMedia/CropTarget prompt and pixel evidence before enabling a Direct capture path. |
| B-PREVIEW (BR-9b/9c) | Proven approval crop / target outline | **DEFERRED — capability blocked** | Current Chrome 155 screencast metadata cannot prove matching-frame identity. Keep “Preview unavailable” and no outline until a reviewed capability proof exists; synthetic crop tests are insufficient. |
| B-LOGIN (M4/M4b/M12) | Real site sign-in, Chrome sync entry and password-store behavior | **VERIFY — owner-assisted, unclaimed** | Profile/sign-in UI is installed. Real Google/Cloudflare/GitHub acceptance, sync and usable keyring are not established. Do not read real credentials or weaken automation/sandbox policy to make a test pass. |
| B-PERF (M1/M2/M5/M7/M8) | Browser performance and platform acceptance record | **VERIFY — unclaimed** | Recover existing measurements before repeating. Explicitly missing here: real Windows input-to-picture p50/p95. Reconcile frame/CPU/profile-memory, Local Network Access and sandbox receipts against current source; do not label all five measurements unimplemented or all passed. |

Saved local handoff material (ask the current lead for a repository-visible PR or
plan copy before relying on it on another machine): `br7-followups-plan.md`,
`br7-auth-amendment-review.md`, `br7-find-menu-cursor-plan.md`,
`br9d-root-independent.py`, and native-gate drafts under
`/home/johnw/.cache/jarvis-lead/codex-oct13/`. Old OFF-only wording and one-heavy-job
wording in these drafts are superseded by current owner instructions; their
identity/privacy guards are not. Never discard uncommitted draft work during handoff.

## Voice rework — package IDs from section 4 of the plan

These are **package IDs V0…V15**, not the similarly numbered owner decisions
V-1…V-14. [Full voice plan](2026-10-13-voice-rework.md).

| ID | Feature | Status / owner | Dependencies / acceptance |
|---|---|---|---|
| V0 | Measure speaker AEC, Silero/RNNoise/KWS cost, local STT real-time factor, speaker EER and Smart Turn | **READY — unclaimed** | Synthetic/offline probes can start; actual owner mic/speaker acceptance is a distinct manual gate. Publish CPU/RSS/latency/WER/EER receipts before choosing models or claiming acoustic quality. |
| V1 | Mic constraints/readback, bounded drop/transcription diagnostics | **INSTALLED — #128** | Existing capture behavior retained. This is observability, not ML VAD, speaker verification or a proven acoustic fix. |
| V2 | STT deadlines/retries, retained clips, visible errors and sequence ordering | **READY — unclaimed** | Start from installed V1; keep clips for explicit retry and preserve transcript order on delayed replies. Capture code is shared with V3–V5: land sequentially. |
| V3 | Dictation steers busy chats; push-to-talk no longer interrupts; continuation window | **WAIT — unclaimed** | V2. Verify busy/idle transitions, owner destination identity and unchanged Ctrl+D gesture. |
| V4 | AudioWorklet/resampler, uncapped utterance recorder and chunked STT | **WAIT — unclaimed** | V2. Bounded buffering, ordered chunks and long dictation without silent truncation. |
| V5 | Silero VAD worker, energy fallback and end-of-utterance timers | **WAIT — unclaimed** | V4 + V0 evidence. Test pause/continuation boundaries and degraded-worker fallback. |
| V6 | Streaming speech core: prose filter, sentence splitting, /say, one-thread player/queue, controls and output separate from mic | **READY — unclaimed** | Separate track from capture; agree App/voice state interfaces with V2 author. Skip code/diffs/tool calls; Web Lock prevents multiple speaking windows. |
| V7 | One shared queue across every open chat pane, per-thread voices, floor/stale/backlog rules | **WAIT — unclaimed** | V6. Hidden open panes speak; threads in no pane are silent; playback never overlaps. |
| V8 | Bounded spoken summaries using Haiku tier, fallback, cost/latency measurement | **WAIT — unclaimed** | V6. Resolve the plan's summary-length inconsistencies before freezing acceptance; no new paid probe inherits the completed Browser budget. |
| V9 | Barge-in, self-voice reference gating and transcript commands | **WAIT — unclaimed** | V5 + V6. Voice must never authorize an approval card; real speaker/headphone checks remain separate from fake audio tests. |
| V10 | Trailing send/cancel words and Undo | **WAIT — unclaimed** | V5 + V2. Whole-utterance/trailing-silence semantics; ordinary prose containing “over” must not send early. |
| V11 | Local wake/command/send keyword spotting; retire phrase-avatar Chrome recognition | **WAIT — unclaimed** | V0 + V9. Choose only after measured resource/accuracy evidence. |
| V12 | Local speaker enrollment/verification/store and persistent Guest voice switch | **WAIT — unclaimed** | V0 + V4. Store stays local, mode0700, never uploaded; owner decided no additional protected-path rule. Guest survives restart until explicitly off and changes no approval or self-voice rule. |
| V13 | Local STT with explicit cloud-fallback switch | **WAIT — unclaimed** | V0 + V2. Offline rejection/selection and cloud fallback must be visible and tested. |
| V14 | RNNoise worker stage | **WAIT / conditional — unclaimed** | V0 + V5; implement only if V0 demonstrates a gain. |
| V15 | Coordinate speech with Browser audio | **WAIT — unclaimed** | V7 + B-SOUND. Resolve the plans' audio-transport mismatch: Remote audio leaves through WSLg, not the frame stream; Direct cross-origin audio is not observable/controllable by default. No speculative ducking hook or unmute. |

## Native subagent view

[Full subagent plan](2026-10-13-subagent-view.md). This is visibility and stop-one
for native Claude/Codex children, not a second task scheduler. Children stay silent
apart from queued finish/failure notices. Existing child-tool restrictions remain.

| ID | Feature | Status / owner | Dependencies / acceptance |
|---|---|---|---|
| SV-0 | Capture actual native Claude/Codex child protocol and lifecycle fixtures | **READY — unclaimed** | Synthetic one/parallel/background/nested/interrupted children; Claude stop_task, Codex experimental schema and child notifications/interrupt/token totals. Sanitized fixture recordings, no invented event shapes. Coordinate native processes and test slots. |
| SV-1 | Claude child tagging/events, daemon record/routes/runner and API docs | **WAIT — unclaimed** | SV-0 Claude evidence. Keep child prose out of parent/Discord answer and preserve approval identity. |
| SV-2 | Codex child set/routing, spawn events and non-fatal child approval handling | **WAIT — unclaimed** | SV-0 Codex + SV-1. High-overlap with B-NATIVE provider identity filters; agree shared hunks. If CLI lacks child notifications, explicitly narrow support. |
| SV-3 | HUD child store, inline spawn rows and read-only transcript pane | **WAIT — unclaimed** | SV-1 merged event/route shapes; includes workspace migration, reducer, mock and browser checks. |
| SV-4 | Stop one child via owner route and button | **WAIT — unclaimed** | SV-1 + SV-3 + actual L-C3/L-X3 stop-one proof; parent and siblings must survive. |
| SV-5 | Coalesced child finished/failed speech notices | **WAIT — unclaimed** | SV-1 + V7; no reading child prose aloud or speaking owner-initiated Stop. |
| SV-6 | Read child activity between orchestrator turns | **DEFERRED — decision after SV-0** | SV-1/2 and evidence background work needs it (L-C7/Q6). High-risk reader lifecycle change; do not silently absorb into SV-1. |

## Original checkpoint and requested fixes already landed

These are not available implementation tasks. All listed code is included in the
verified installed baseline, unless a remaining acceptance qualification is noted.
PR #125 itself adds plans, not the features those plans describe.

| Area | Completed scope / evidence | Remaining qualification |
|---|---|---|
| Chat Git/worktrees | [#88](https://github.com/Johnwang688/jarvis/pull/88) sandbox roots; [#92](https://github.com/Johnwang688/jarvis/pull/92) owner approval routing | Final-policy positive real push→PR acceptance still needs a dedicated receipt; denial/resume and local commit were verified. Old pre-routing probes are not that receipt. |
| Models/caching | [#86](https://github.com/Johnwang688/jarvis/pull/86) Haiku default,90k compaction, stable text/image cache boundaries | Browser classifier fix is separate B-CLASSIFIER; do not change the owner's selected model. |
| Guide/history | [#96](https://github.com/Johnwang688/jarvis/pull/96), [#99](https://github.com/Johnwang688/jarvis/pull/99) lossless notes extraction and guide limits | Preserve archived source blocks; current notes belong outside them. |
| Chat tool calls/titles | TC-1 #84, [#93](https://github.com/Johnwang688/jarvis/pull/93) TC-2; [#87](https://github.com/Johnwang688/jarvis/pull/87) one-line title/hover | No outstanding implementation from the checkpoint. |
| Dev-server auto-open | #76/#79 foundation; [#94](https://github.com/Johnwang688/jarvis/pull/94) announcements; [#103](https://github.com/Johnwang688/jarvis/pull/103) tabs; [#108](https://github.com/Johnwang688/jarvis/pull/108) automatic placement | Direct-only auto-open and no automatic Remote port grant remain. |
| Settings | [#98](https://github.com/Johnwang688/jarvis/pull/98) consolidated Settings page | Active Browser/voice packages extend their own sections. |
| Prompt/runtime/input/drafts | #95 prompt preservation; #97 turn context; #102 talk-key chord; #104 compose; #105 Send/Stop; #106 task refusal; #107 input sizing; #112 notice timestamps | Merged fixes, not separate open tasks. |
| Editor | [#110](https://github.com/Johnwang688/jarvis/pull/110) recovery, [#111](https://github.com/Johnwang688/jarvis/pull/111) navigation | Earlier editor/settings/git gutter foundations already present. |
| Browser foundation | #69 engine/gate; #77 stream/input; #66 pure HUD; #68 rename; [#101](https://github.com/Johnwang688/jarvis/pull/101) profiles/Private; [#113](https://github.com/Johnwang688/jarvis/pull/113) Remote HUD | Remaining real sign-in/IME/performance gates appear above. |
| Browser look/capture/fonts | [#109](https://github.com/Johnwang688/jarvis/pull/109) read-only look; [#115](https://github.com/Johnwang688/jarvis/pull/115) capture/annotation; [#116](https://github.com/Johnwang688/jarvis/pull/116) private Windows fonts; [#123](https://github.com/Johnwang688/jarvis/pull/123) native MCP looks | Direct app-mode capture and proven card preview are still gated above. |
| Session MCP | [#117](https://github.com/Johnwang688/jarvis/pull/117) Claude; [#118](https://github.com/Johnwang688/jarvis/pull/118) Codex | Installed native terminal/read-only Browser tools do not imply BR-9d driving is installed. |
| Dormant driving | #71 rules; [#122](https://github.com/Johnwang688/jarvis/pull/122) backend; [#124](https://github.com/Johnwang688/jarvis/pull/124) owner controls/cards | Enabling release is TAKEN as B-DR. |
| VEX output | [#126](https://github.com/Johnwang688/jarvis/pull/126) Windows PTY handshake, visible Clean/Build output and clean process exit | Synthetic PTY/Windows shell checks passed; no real VEX tool, robot download or field test performed. |
| Math | [#127](https://github.com/Johnwang688/jarvis/pull/127) bounded KaTeX rendering in shared Markdown | Installed; actual proof rendering, hostile input and narrow-layout checks passed. |
| Test/command follow-ups | #90 scheduler-independent complexity checks; #75 and #100 runner/git/REPL handling | VEX and terminal throughput failures reproduced on unchanged baseline remain a separate VERIFY item, not hidden passes. |

## Remaining checkpoint housekeeping and explicit limits

| ID | Scope | Status / next step |
|---|---|---|
| QA-LOAD | Remaining load-sensitive tests and reliable sweep reporting | **READY — unclaimed.** Reproduce unchanged VEX/terminal throughput bounds; retain performance assertions and distinguish regression from scheduler noise. Preserve per-suite output and real exit codes: an old lead wrapper could exit0 with failed suites. Audit old quota flake against current waits before editing. |
| GIT-LIVE | Positive end-to-end native Git flow under the final approval policy | **VERIFY — unclaimed.** Local worktree/edit/commit and denied/resumed pushes were checked. Use a disposable authorized repo/branch and actual owner approval for the final positive flow; do not reuse old fixture-helper evidence as native proof. |
| HUD-FOLLOWUPS | Reconcile the checkpoint's old `hud-open-followups` list | **VERIFY — unclaimed.** Most named input/draft/editor issues already merged above; recover the original list and close by evidence before making new edits. |
| VEX-UI | Broad VEX view redesign | **DEFERRED.** The urgent Clean/Build fix is installed. The narrow bug request does not itself expand into a full redesign or authorize real robot operations. |
| PLATFORM | Physical Browser sound, real Windows mic/AEC, real account sign-in, VEX download | **VERIFY / owner-assisted.** Synthetic/unit/native-shell success must not be reported as these real-world acceptances. |

Good starting points for another team, after claiming: **V2**, **V6**, **SV-0**,
**V0** synthetic measurement, or **QA-LOAD**. V2 and V6 are separate logical
tracks but still need an agreed App/voice-state integration boundary. B-AUTH and
B-NATIVE have reusable drafts but more shared-file/dependency risk.

## Shared test, review and release contract

- WSL has about **15GB RAM**. **At most three heavy HUD/browser test jobs in total
  across all teams**, never three per team. Current assignments are slot1 B-UI,
  slot2 B-SOUND, slot3 B-DR; availability changes and must be confirmed with the
  coordinating lead before launch. Builds and full sweeps count as heavy jobs.
- Use `systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0` and monitor
  memory; start only with at least4GiB available. Reduce concurrency if pressure
  rises. Do not kill unrelated user processes or use `pkill -f`.
- Tests use temporary HOME/XDG/config, dummy provider values, ephemeral ports and
  isolated worktree assets. No real `.env*` reads/prints/writes; no owner config,
  live8402/8403/8405 or installed-HUD mutation; no real VEX tool/device.
- Completed paid probe exceptions are **not reusable permission** for new calls.
  Native login/account, paid-model and physical-device checks retain their own
  authorization boundaries; this registry grants none.
- Independent review before merge; boundary changes need meaningful negative or
  mutation evidence. Report test counts and failures honestly, including a failed
  initial attempt and what changed before rerun. Do not require broad unrelated
  tests for a documentation-only claim update.
- Integrate current main, run checks appropriate to the integrated change, inspect
  required CI, and merge against the reviewed head. Deploy only from clean synced
  main, preserving owner untracked files. Report **committed, PR, merged, built,
  installed, browser-tested and live-validated** as separate facts.

## Source map

- [Browser plan](2026-10-10-hud-browser-plan.md), [decisions](2026-10-10-hud-browser-decisions.md),
  [amendment 1](2026-10-11-hud-browser-plan-amendment-1.md), [amendment 2](2026-10-12-hud-browser-plan-amendment-2.md).
- [Driving release](2026-10-13-browser-driving-release.md), [native driving](2026-10-13-browser-driving-claude-codex.md),
  [sound](2026-10-13-browser-sound.md), [voice](2026-10-13-voice-rework.md), [subagent view](2026-10-13-subagent-view.md).
- [Current Browser notes](../notes/browser.md), [control-plane notes](../notes/control-plane.md),
  [voice notes](../notes/voice-face.md), [HUD API](../hud-api.md), [working guide](../../CLAUDE.md).
- Historical owner-local checkpoint: `/home/johnw/projects/Jarvis/docs/plans/2026-10-12-checkpoint.md`.
  Preserve it; this tracked map provides the portable team view.
