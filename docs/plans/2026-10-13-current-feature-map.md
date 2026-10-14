# Jarvis current feature map and team ownership

Updated 2026-10-13 (America/Chicago). This is the shared coordination map for the
2026-10-12 checkpoint, the five plans merged in [PR #125](https://github.com/Johnwang688/jarvis/pull/125),
and John's subsequent VEX, LaTeX and Browser-controls requests. It is a working
registry: update the relevant row when claiming, handing off, opening a PR,
merging or deploying. It is not a new authorization to expand product scope.

At the initial checkpoint the current Codex team reserved four packages:
`B-UI`, `B-DR`, `B-SOUND` and `B-CLASSIFIER`. **All four are now INSTALLED.**
In the next Browser batch, `B-AUTH` and `B-FIND` are **MERGED, not yet installed**;
`B-NATIVE` remains **TAKEN by the Codex lead**. The urgent `VEX-DOWNLOAD` fix is
also TAKEN. Other unclaimed packages remain available. A saved draft alone is
not an active claim.

**Voice packages owned by the Claude team (2026-10-13):** all of V0–V15. `V0`, `V3`, `V7` merged; `V2`, `V6` installed; the rest TAKEN.
Slot1 is reserved for that team under the shared limit below.

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

The current Codex lead owns integration/deployment for its reserved packages
below. Other teams should name their integrator in their claim. Serialize merges and deployments
through the leads; integrate latest main and review conflicting hunks before merging.
A documentation-only merge does not need a daemon restart.

## Browser release snapshot — original batch installed (2026-10-13)

- [PR #137](https://github.com/Johnwang688/jarvis/pull/137) merged at
  `a857c1f19c8b0cdab393e90a7e039ea7ea81be4a`, including installed
  [#135](https://github.com/Johnwang688/jarvis/pull/135) fast-path driving,
  [#132](https://github.com/Johnwang688/jarvis/pull/132) controls and
  [#130](https://github.com/Johnwang688/jarvis/pull/130) classifier fixes.
- Installed/served HUD build: `20261014T004949720546Z-7ff98f434711`, from
  `a857c1f19c8b0cdab393e90a7e039ea7ea81be4a`. Normal capped guarded
  rebuild/restart exited with code 0; exact source/build and installed state
  were verified. Claude/Codex/fast were healthy, with zero turns or approvals
  pending. Main matched origin/main; tracked files were clean, owner untracked
  files preserved and protected-config modification times unchanged.
- B-SOUND runtime freeze `502f62b6` passed build, 1,901 HUD unit tests and
  **3,217/3,217 full mock HUD checks**. The immutable backend receipt was
  **124/125**; reviewed fixture correction `3655a15` passed the complete
  **36-test routes suite**, covering the sole failed suite. The original
  failure remains preserved. The lead and a separate UI reviewer completed
  independent source reviews; GitGuardian passed. Cursor automated reviewers did **not** run because of
  quota/on-demand limits; their neutral checks are not review passes.
- Guarded **fast-path** driving and owner sound controls are installed.
  Native Claude/Codex driving was not part of that installed batch; it is now
  **B-NATIVE TAKEN** in the next batch below. Physical speaker
  audibility and mic echo are **UNMEASURED**; V15 speech/Browser coordination
  remains pending. Installation does not establish real-account/device acceptance.
- At this Browser release, slots 2 and 3 were released. **Current reservations
  are in the next-batch section below.** The global maximum remains three
  heavy jobs; newer Claude voice deployments and claims appear in their rows. [PR #131](https://github.com/Johnwang688/jarvis/pull/131)
  merged the Claude team's ownership; those feature rows are unchanged.

## Historical verified baseline — before this Browser batch

- Main and origin matched `711577cc302d9bf9ca7eae5de340702d3cd4d2a1` at this snapshot.
- Installed HUD: `20261013T175916735476Z-4c3252d7de89`, built from that main commit;
  normal rebuild/restart succeeded and all three providers were healthy.
- **Production Browser driving is still OFF in that installed build.** Backend
  and owner presentation have landed; the enabling release below has not.
- Local evidence: `/home/johnw/.cache/jarvis-lead/codex-oct13/execution.md` and
  per-package receipts there. These local files are not part of the repository.
- The original checkpoint and questions log are owner-owned, untracked files in
  `/home/johnw/projects/Jarvis/docs/plans/`. This map does not move or stage them.
  Older plan headings saying “docs only,” “M11 never run” or “driving remains OFF”
  describe their publication snapshot; use the dated status and evidence here.

## Current Codex packages — active work and installed receipts

Worktrees below are under `/home/johnw/.cache/jarvis-lead/wt/`.

| ID | Feature and acceptance scope | Status / owner | Branch / worktree | Current progress and remaining gate |
|---|---|---|---|---|
| B-UI | Compact Browser navigation, three-dot options, automatic localhost Direct / external Remote selection; Private remains Remote | **INSTALLED — #132; Codex lead / `vex_fix`** | `codex/browser-controls` / `browser-controls` | [PR #132](https://github.com/Johnwang688/jarvis/pull/132) merged at `df5099ab` and is installed in the current release above; reviewed product `1c572e5c` passes 66 focused HUD checks, typecheck/build, 39 Remote, 17 narrow-controls, 16 Direct-origin and 79 frame-focus checks. Independent actual-Chrome dismissal/capture checks pass 6/6; the initially found dismissal click-through is fixed. Options retain owner permissions while blocking page/agent input; explicit capture fallback remains. Final integrated capture `22ab268` passes all 49 assertions after adapting fixtures to the final options-dismissal behavior; the earlier 49 receipt predates that fix. Original reservation completed. |
| B-DR | Release guarded fast-path Browser driving with global and per-pane switches, named-profile defaults and transient Private opt-in | **INSTALLED — [#135](https://github.com/Johnwang688/jarvis/pull/135); Codex lead / `plans_voice`** | `codex/driving-release` / `driving-release` | Tested integration `22ab268` (product unchanged from reviewed `76177f39`) includes installed B-UI/B-CLASSIFIER and preserves Claude V0/V2/V6 claims. Build, 1,870 HUD units, 43 owner Browser checks, 19 execute + six scan real-Chrome tests, 33 view tests, independent authority probes and five mutants passed. Full HUD had 8/3,134 failures; reviewed fixture corrections passed all 215 complete affected checks, including final 49 capture assertions. Backend: 115 invoked, 108 initial passes; full corrected 88 Codex / 39 installer / 30 owner-browser run with one skip / 90 terminal / 33 view passed. Fake Pocket and four non-synthesis local groups passed; real local synthesis UNRUN (cached model paths were not configured in the initial isolated B-DR run). Original failures and RSS comparison retained. Merged at `c8a4bed` and installed in the current build above; original reservation completed. Fast-path only; native driving is tracked separately under B-NATIVE below. [Release plan](2026-10-13-browser-driving-release.md). |
| B-SOUND | Owner-only Remote sound, master permission, memory-only profile state, structural mute, guarded profile restart and visible indicators | **INSTALLED — [#137](https://github.com/Johnwang688/jarvis/pull/137); Codex lead / `sound_reconcile`** | codex/browser-sound / browser-sound | Reviewed final head `0d1e06e1` merged at `a857c1f` and is installed in the current build above; original reservation completed. Runtime freeze `502f62b6` passed build, 1,901 HUD units and 3,217 full HUD checks; backend 124/125 plus the full corrected 36-test routes suite covers all 125, with the original failure retained. Independent source reviews by the lead and a separate UI reviewer are complete; GitGuardian passed, Cursor automated reviews did not run (quota/on-demand limits). Per-profile restart and all-tabs/new-tabs/hidden-playback scope remain explicit; no automatic cross-site re-mute. Physical audio/echo UNMEASURED; V15 coordination remains pending. [Sound plan](2026-10-13-browser-sound.md). |
| B-CLASSIFIER | Stop Haiku 5.5 truncating bounded Browser-review JSON; reject incomplete response metadata and retain ASK on failure | **INSTALLED — #130; Codex lead / `sound_reconcile`** | `codex/browser-classifier-budget` / `browser-classifier-budget` | Reviewed product `81f1fd0`; 37 canonical, 4 wire, 11 independent checks and 11 mutants passed. Approved 50-call confirmation: 49 valid replies, one timeout/ASK, zero truncation; p50 1.364650 s, p95 1.734687 s. [PR #130](https://github.com/Johnwang688/jarvis/pull/130) merged at `d2c2239`; current-main integration and four relevant regression suites passed. Installed with B-UI in the current release above; original reservation completed. All M11 spending charged/reserved is $0.01187951250, including unknown timeout reserve. Both paid 50-call approvals are consumed. |

## Next Browser batch — Codex ownership and integration

The Codex lead integrates this batch; GPT-6.1-sol implementers use separate
worktrees under `/home/johnw/.cache/jarvis-lead/wt/`. Claim acceptance precedes
product edits. Saved drafts remain preserved; each implementation starts from
current main and deliberately ports reviewed draft work.

- **B-NATIVE** — `browser_native`, branch `codex/browser-native-v2`, worktree
  `browser-native-v2`: Claude/Codex parent driving through the installed shared
  gate. No child/task/off-desk driving or change to existing authority/budgets.
- **B-AUTH** — `browser_auth`, branch `codex/browser-auth-v2`, worktree
  `browser-auth-v2`: owner Basic/Digest challenges, including first protected
  navigation, exact request identity, bounded waits and credential privacy.
- **B-FIND** — `browser_find`, branch `codex/browser-find-v2`, worktree
  `browser-find-v2`: top-document find, owner context menu and finite cursors.
  Native picker fallbacks and real Windows IME acceptance are not included.

Current validation snapshot: the isolated combined worktree passed its build,
**2,179 HUD unit tests in 139 files** and **3,222 full HUD checks**. The initial
137-suite backend sweep passed 134; two fixture failures and one Find navigation
interaction were corrected. All 14 affected suites then passed on combined head
`ac8bdedec0c824971d763f26a2f879f2055cbbfb`, with unchanged source manifests and an
inactive, empty memory-limited test scope. Original failures remain preserved.
Independent source reviews passed. Auth #150 merged at `3b1205ae`; Find #151
merged at `dd210803`. Neither is installed yet; Native #149 remains a draft with
actual native lifecycle/child and real-HUD capability gates outstanding.

GitGuardian's advisory failures on Auth/Find were independently adjudicated as
three historical synthetic authentication fixtures. Dummy values were clarified
without changing test inputs. The check remains recorded as failed; no scanner
suppression, history rewrite or admin bypass was used. See #150 for exact paths
and introducing commits.

B-NATIVE can progress alongside B-AUTH. B-FIND develops isolated helpers/UI in
parallel, then integrates B-AUTH's accepted navigation/attention/socket changes
before its final freeze. Shared `host.py`, `stream.py`, `RemoteView` and Browser
state changes must preserve the installed sound activity/restart and capture
identity contracts. Each feature publishes its proposed interface before shared
integration. The lead serializes merges/deployments; independent reviews,
meaningful boundary tests and current-main integration precede each merge.
Voice V3/V7/V8 and their App/capture/speech work remain with the Claude team;
this batch does not claim voice work or native subagent visibility.

**Test slots:** slot 1 remains reserved for the Claude voice team. Codex reserves
slot 2 for B-NATIVE and slot 3 for the urgent VEX-DOWNLOAD fix (the Auth/Find
correction run is complete). A reservation
is not a running job: the lead grants each heavy run after a memory check.
At most three heavy test/build jobs globally, each capped at 3 GiB and no swap;
start only with at least 4 GiB available. Native/provider probes need a concrete
reviewed plan; completed paid-probe exceptions are not reused. Physical/account
acceptance remains separately identified, never inferred from synthetic tests.

## Browser and native-agent packages — active and available

| ID | Feature | Status / owner | Dependencies / definition of done |
|---|---|---|---|
| B-NATIVE (BR-9d, BD-a/b/c) | Claude and Codex parent chats drive through the same Browser gate | **TAKEN — Codex lead / `browser_native`; draft [#149](https://github.com/Johnwang688/jarvis/pull/149)** | `codex/browser-native-v2` / `browser-native-v2`. Source review and 248 focused backend checks passed; exact installed Codex parent identity/admission and real MCP call passed against an isolated scripted provider. Both installed CLIs survived a 115.000-second private-card wait and returned the exact result in the next scripted provider request. Actual native lifecycle/child checks and combined release gates remain pending. No paid model, real-account or installed-release claim. [Native plan](2026-10-13-browser-driving-claude-codex.md). |
| B-AUTH (BR-7) | HTTP Basic/Digest owner challenge form over the existing authenticated tab socket | **MERGED — [#150](https://github.com/Johnwang688/jarvis/pull/150); Codex lead / `browser_auth`; deployment pending** | Exact reviewed head `70460c2` merged at `3b1205ae`. First protected navigation, delayed reply/original expiry, Digest, stale identity, restore, privacy and cleanup passed focused checks; 62 auth/Sound checks, nine native cases, 14 actual HUD assertions and five guard mutants passed. Combined release correction gates passed as described above. Same-origin iframe prompts measured; first protected popup required explicit Reload, and separate OOPIF prompt delivery was absent in the bounded probe. Protected Sound restoration remains fail-closed with Retry. No real-account acceptance claim. |
| B-FIND (BR-7) | Find-in-page, bounded owner context menu, finite cursor shapes | **MERGED — [#151](https://github.com/Johnwang688/jarvis/pull/151); Codex lead / `browser_find`; deployment pending** | Exact reviewed head `5e638ea` merged at `dd210803` after #150. Fixed top-document search, five owner menu actions, finite cursors and exact document/socket/hold guards passed independent review. Final focused evidence: 70 backend checks, 19 real driving cases, real Chrome owner tools, 22 actual owner HUD assertions and seven intended guard mutants; all 14 combined correction suites passed. Navigation discards stale document input without adding an approval hold. Native layout preemption remains unproven; native picker/IME expansion excluded. |
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
| V0 | Measure speaker AEC, Silero/RNNoise/KWS cost, local STT real-time factor, speaker EER and Smart Turn | **MERGED — [#140](https://github.com/Johnwang688/jarvis/pull/140), Claude team** | Synthetic/offline receipts in `docs/notes/voice-face.md` (2026-10-13 V0 section); probes in `tests/voice_v0/`, gated by `JARVIS_VOICE_V0=1`. Local Parakeet RTF 0.04–0.07; window VAD+NS+KWS ≈12–14% of a core (near the 15% bar, unconfirmed on Windows Chrome); KWS recall too weak to retire Chrome's recognizer. Physical/owner measurements and cloud-vs-local WER remain manual gates. |
| V1 | Mic constraints/readback, bounded drop/transcription diagnostics | **INSTALLED — #128** | Existing capture behavior retained. This is observability, not ML VAD, speaker verification or a proven acoustic fix. |
| V2 | STT deadlines/retries, retained clips, visible errors and sequence ordering | **INSTALLED — [#136](https://github.com/Johnwang688/jarvis/pull/136), Claude team** | Installed build `20261014T021138099135Z-87990c016981` from main 6bbef98, daemon restarted, providers healthy. No physical-mic acceptance. Follow-ups: stale "Not retried" error line; oversize-clip message wording; no test for `place()` throwing. `/stt` and `/say` have no `owner_only` check (pre-existing). |
| V3 | Dictation steers busy chats; push-to-talk no longer interrupts; continuation window | **TAKEN — Claude lead; implementer Sonnet 5.5 agent** | Branch `claude/voice-v3`, stacked on V2 until it merges. Owner answered plan Q3–Q5 (send at the pause, then steer; PTT steers; reply goes to the pane that spoke). Verify busy/idle transitions, owner destination identity and unchanged Ctrl+D gesture. |
| V4 | AudioWorklet/resampler, uncapped utterance recorder and chunked STT | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v4`. stacked from main. V2. Bounded buffering, ordered chunks and long dictation without silent truncation. |
| V5 | Silero VAD worker, energy fallback and end-of-utterance timers | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v5`. after V4. V4 + V0 evidence. Test pause/continuation boundaries and degraded-worker fallback. |
| V6 | Streaming speech core: prose filter, sentence splitting, /say, one-thread player/queue, controls and output separate from mic | **INSTALLED — [#134](https://github.com/Johnwang688/jarvis/pull/134), Claude team** | Same install as V2. No physical speaker/AEC acceptance. 1200-character spoken cap is a stopgap replaced by V8. Follow-ups from review: headerless diffs still read; barge-in with nothing playing pauses nothing; buffering counts as active during a synthesis hang. |
| V7 | One shared queue across every open chat pane, per-thread voices, floor/stale/backlog rules | **TAKEN — Claude lead; implementer Sonnet 5.5 agent** | Branch `claude/voice-v7`, stacked on V6 until it merges. Hidden open panes speak; threads in no pane are silent; playback never overlaps. Includes the per-pane voice chip (Q9). |
| V8 | Bounded spoken summaries using Haiku tier, fallback, cost/latency measurement | **TAKEN — Claude lead; implementer Sonnet 5.5 agent** | Branch `claude/voice-v8`, stacked on V6 until it merges. Owner answered Q2: replies over 600 characters become a summary only. One paid cost/latency probe approved, capped at $0.10; no other paid calls. |
| V9 | Barge-in, self-voice reference gating and transcript commands | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v9`. after V5; Q7 stricter (only "Jarvis"/Ctrl+D pause before enrollment), Q8, Q10 answered. V5 + V6. Voice must never authorize an approval card; real speaker/headphone checks remain separate from fake audio tests. |
| V10 | Trailing send/cancel words and Undo | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v10`. after V5; Q1 answered. V5 + V2. Whole-utterance/trailing-silence semantics; ordinary prose containing “over” must not send early. |
| V11 | Local wake/command/send keyword spotting; retire phrase-avatar Chrome recognition | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v11`. after V9; Q16 answered (local wake only, commands via STT). V0 + V9. Choose only after measured resource/accuracy evidence. |
| V12 | Local speaker enrollment/verification/store and persistent Guest voice switch | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v12`. after V4; Q12 answered (keep clips). V0 + V4. Store stays local, mode0700, never uploaded; owner decided no additional protected-path rule. Guest survives restart until explicitly off and changes no approval or self-voice rule. |
| V13 | Local STT with explicit cloud-fallback switch | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v13`. Q6 answered; one paid cloud-vs-local WER check <= $0.25. V0 + V2. Offline rejection/selection and cloud fallback must be visible and tested. |
| V14 | RNNoise worker stage | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v14`. after V5; owner: build it, default off. V0 + V5; implement only if V0 demonstrates a gain. |
| V15 | Coordinate speech with Browser audio | **TAKEN — Claude lead; implementers Sonnet 5.5 (Haiku 5.5 only for non-critical chores)** | Branch `claude/voice-v15`. owner: duck Remote tabs via WSLg PulseAudio sink-inputs under a renewable lease; never unmute. V7 + B-SOUND. Resolve the plans' audio-transport mismatch: Remote audio leaves through WSLg, not the frame stream; Direct cross-origin audio is not observable/controllable by default. No speculative ducking hook or unmute. |

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
| Dormant driving | #71 rules; [#122](https://github.com/Johnwang688/jarvis/pull/122) backend; [#124](https://github.com/Johnwang688/jarvis/pull/124) owner controls/cards | B-DR enabling release [#135](https://github.com/Johnwang688/jarvis/pull/135) is installed for the fast path; native driving is tracked separately under B-NATIVE below. |
| VEX output | [#126](https://github.com/Johnwang688/jarvis/pull/126) Windows PTY handshake, visible Clean/Build output and clean process exit | Synthetic PTY/Windows shell checks passed; no real VEX tool, robot download or field test performed. |
| Math | [#127](https://github.com/Johnwang688/jarvis/pull/127) bounded KaTeX rendering in shared Markdown | Installed; actual proof rendering, hostile input and narrow-layout checks passed. |
| Test/command follow-ups | #90 scheduler-independent complexity checks; #75 and #100 runner/git/REPL handling | VEX and terminal throughput failures reproduced on unchanged baseline remain a separate VERIFY item, not hidden passes. |

## Remaining checkpoint housekeeping and explicit limits

| ID | Scope | Status / next step |
|---|---|---|
| QA-LOAD | Remaining load-sensitive tests and reliable sweep reporting | **READY — unclaimed.** Reproduce unchanged VEX/terminal throughput bounds; retain performance assertions and distinguish regression from scheduler noise. Preserve per-suite output and real exit codes: an old lead wrapper could exit0 with failed suites. Audit old quota flake against current waits before editing. |
| GIT-LIVE | Positive end-to-end native Git flow under the final approval policy | **VERIFY — unclaimed.** Local worktree/edit/commit and denied/resumed pushes were checked. Use a disposable authorized repo/branch and actual owner approval for the final positive flow; do not reuse old fixture-helper evidence as native proof. |
| HUD-FOLLOWUPS | Reconcile the checkpoint's old `hud-open-followups` list | **VERIFY — unclaimed.** Most named input/draft/editor issues already merged above; recover the original list and close by evidence before making new edits. |
| VEX-DOWNLOAD | Match Download output/completion to Build/Clean; visible brain/controller connectivity and timestamped connection history | **TAKEN — Codex lead / `browser_auth`; urgent owner request.** Branch `codex/vex-download-device-status`, worktree `vex-download-device-status`. Reuse the guarded owner-click download and one-device reservation; distinguish disconnected from failed/unknown device reads. Read-only diagnosis precedes changes; independent review and fake-tool/device tests precede merge. No real robot operation is part of validation. |
| VEX-UI | Broad VEX view redesign | **DEFERRED.** The urgent Clean/Build fix is installed. The narrow bug request does not itself expand into a full redesign or authorize real robot operations. |
| PLATFORM | Physical Browser sound, real Windows mic/AEC, real account sign-in, VEX download | **VERIFY / owner-assisted.** Synthetic/unit/native-shell success must not be reported as these real-world acceptances. |

Good starting points for another team, after claiming: **SV-0** or **QA-LOAD**
(Claude voice ownership/status is recorded in the individual rows above).
B-NATIVE, B-AUTH and B-FIND are reserved by Codex; choose other unclaimed
packages or coordinate a handoff with the named lead.

## Shared test, review and release contract

- WSL has about **15GB RAM**. **At most three heavy HUD/browser test jobs in total
  across all teams**, never three per team. Slot 1 is reserved for the Claude
  voice team; slot 2 is Codex B-NATIVE and slot 3 is Codex B-AUTH/B-FIND
  sequentially, with per-job lead admission. Claim any future heavy job
  with the coordinating lead using the current registry before launch;
  availability changes. Builds and full sweeps count as heavy jobs.
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
