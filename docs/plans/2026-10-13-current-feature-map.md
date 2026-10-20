# Jarvis current feature map and team ownership

Updated 2026-10-19 (America/Chicago, main `1c0c006`). This is the shared coordination map for the
2026-10-12 checkpoint, the five plans merged in [PR #125](https://github.com/Johnwang688/jarvis/pull/125),
and John's subsequent VEX, LaTeX and Browser-controls requests. It is a working
registry: update the relevant row when claiming, handing off, opening a PR,
merging or deploying. It is not a new authorization to expand product scope.

At the initial checkpoint the current Codex team reserved four packages:
`B-UI`, `B-DR`, `B-SOUND` and `B-CLASSIFIER`. **All four are now INSTALLED.**
In the next Browser batch, `B-AUTH` and `B-FIND` are **INSTALLED**, as is the
urgent `VEX-DOWNLOAD` fix. `B-NATIVE` is now **INSTALLED** after its reviewed
native acceptance and guarded deployment. Other unclaimed packages remain available.
A saved draft alone is not an active claim.

**Voice packages owned by the Claude team (2026-10-13):** all of V0–V15. As of 2026-10-15 all sixteen are merged and installed (V0 as measurements only); follow-ups in [2026-10-15-voice-followups.md](2026-10-15-voice-followups.md). No package has physical-mic, speaker or Windows Chrome acceptance; every receipt is synthetic or fake-device audio.
Slot1 is reserved for that team under the shared limit below.

**2026-10-14/15 landings (all merged to main and installed unless noted):** the
three owner-reported Browser bugs ([#186](https://github.com/Johnwang688/jarvis/pull/186)),
the small HUD follow-ups ([#187](https://github.com/Johnwang688/jarvis/pull/187)),
load-proof test bounds QA-LOAD ([#188](https://github.com/Johnwang688/jarvis/pull/188)),
the HTTP-auth navigate-away fix ([#190](https://github.com/Johnwang688/jarvis/pull/190)),
the HUD hardening decisions ([#191](https://github.com/Johnwang688/jarvis/pull/191)) and two
archived-notes payload restores ([#185](https://github.com/Johnwang688/jarvis/pull/185),
[#189](https://github.com/Johnwang688/jarvis/pull/189)). The Claude lead team started SV-0 and
two editor items on 2026-10-15; all three have since landed (rows below). The owner lifted
the Browser feature freeze on 2026-10-16, when agent slots freed up.

**2026-10-15/16 landings (merged to main; #193 to #225 are INSTALLED per the lead deploy log 2026-10-15/16: every deploy installs the whole of main, with `jarvis hud install` where the HUD changed, then a daemon2 restart, and the live build and daemon run e181beb):** native sub-agent view SV-0 to SV-4 ([#193](https://github.com/Johnwang688/jarvis/pull/193), [#198](https://github.com/Johnwang688/jarvis/pull/198), [#199](https://github.com/Johnwang688/jarvis/pull/199)
with [#195](https://github.com/Johnwang688/jarvis/pull/195), [#212](https://github.com/Johnwang688/jarvis/pull/212), [#216](https://github.com/Johnwang688/jarvis/pull/216)); editor ruler marks and tab icons ([#194](https://github.com/Johnwang688/jarvis/pull/194));
secrets redaction in cards and logs ([#205](https://github.com/Johnwang688/jarvis/pull/205)); the owner-only merge/close gate ([#208](https://github.com/Johnwang688/jarvis/pull/208)) and its
follow-ups ([#222](https://github.com/Johnwang688/jarvis/pull/222), [#225](https://github.com/Johnwang688/jarvis/pull/225)); Monitor judged as a command tool ([#211](https://github.com/Johnwang688/jarvis/pull/211)); the keyring rule and
Stop-label leftovers ([#220](https://github.com/Johnwang688/jarvis/pull/220)); rounded-rectangle buttons with the binary click sparkle ([#210](https://github.com/Johnwang688/jarvis/pull/210));
Browser input-to-picture ack ([#214](https://github.com/Johnwang688/jarvis/pull/214)), keyring password store ([#215](https://github.com/Johnwang688/jarvis/pull/215), [#218](https://github.com/Johnwang688/jarvis/pull/218)) and sign-in
pop-up adoption ([#217](https://github.com/Johnwang688/jarvis/pull/217)); the Diff tab's Quick review ([#219](https://github.com/Johnwang688/jarvis/pull/219)); Codex chat git tools
([#223](https://github.com/Johnwang688/jarvis/pull/223)) and the closed Codex sandbox read gap ([#224](https://github.com/Johnwang688/jarvis/pull/224)); the VEX view redesign
([#209](https://github.com/Johnwang688/jarvis/pull/209), [#213](https://github.com/Johnwang688/jarvis/pull/213)). Smaller merges: Codex Stop keeps the chat ([#200](https://github.com/Johnwang688/jarvis/pull/200)), Codex partial
turns disclosed in usage ([#207](https://github.com/Johnwang688/jarvis/pull/207)), five recent chats per project in the sidebar ([#206](https://github.com/Johnwang688/jarvis/pull/206)), and
the fast path's desk-turn edits and commands (F-1..F-7, [#182](https://github.com/Johnwang688/jarvis/pull/182)). Later merges [#227](https://github.com/Johnwang688/jarvis/pull/227) to [#235](https://github.com/Johnwang688/jarvis/pull/235) are INSTALLED per the lead's 2026-10-16 deploys: owner-only voice routes ([#227](https://github.com/Johnwang688/jarvis/pull/227)), SV-5 ([#228](https://github.com/Johnwang688/jarvis/pull/228)), VEX view finish ([#229](https://github.com/Johnwang688/jarvis/pull/229)), Codex profile leftovers ([#231](https://github.com/Johnwang688/jarvis/pull/231)), gate and Codex git leftovers ([#230](https://github.com/Johnwang688/jarvis/pull/230)), B-PICKERS ([#232](https://github.com/Johnwang688/jarvis/pull/232)), B-IME ([#233](https://github.com/Johnwang688/jarvis/pull/233), follow-up [#235](https://github.com/Johnwang688/jarvis/pull/235)) and SV-6 ([#234](https://github.com/Johnwang688/jarvis/pull/234)).

**2026-10-17 to 2026-10-19 landings ([#238](https://github.com/Johnwang688/jarvis/pull/238) to [#272](https://github.com/Johnwang688/jarvis/pull/272); all merged and INSTALLED. The lead's last deploy on the evening of 2026-10-19 installed main `1c0c006` with `jarvis hud install` and restarted daemon2; no PR is open):** the directional split, up to twelve panes (FS0 to FS4); chat-to-chat (C0 to C5, C7); the Compact button ([#270](https://github.com/Johnwang688/jarvis/pull/270)); Claude skills in Jarvis and Codex chats ([#272](https://github.com/Johnwang688/jarvis/pull/272)); VX-UI-4b and its VEX follow-ups ([#240](https://github.com/Johnwang688/jarvis/pull/240), [#241](https://github.com/Johnwang688/jarvis/pull/241), [#242](https://github.com/Johnwang688/jarvis/pull/242)); the duplicate-helper probe's security fixes SEC-1 to SEC-6 and the cleanup cards CL-1 to CL-4. Rows below, under "2026-10-17 to 2026-10-19 additions" and the sections after it.

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
  Native Claude/Codex driving was not part of that installed batch; its later
  **B-NATIVE installation** is recorded in the next batch below. Physical speaker
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
  gate. **Installed; implementation reservation complete.** No child/task/off-desk
  driving or change to existing authority/budgets.
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
merged at `dd210803`. Both are installed; Native [#149](https://github.com/Johnwang688/jarvis/pull/149)
subsequently passed its supported native CLI/HUD lifecycle gates and merged from
reviewed head `20eb04bf45e121ae88ff796d2a3c905e4c6c46cb` at
`11b7db8cd7475c8e0d8b51646100e068bd698b75`.

Verified Native installation: source `11b7db8cd7475c8e0d8b51646100e068bd698b75`,
served HUD build `20261014T052420758225Z-2350d3b4a8b1`. Normal guarded
rebuild/restart exited 0 in 14.611 seconds under a 3 GiB / zero-swap scope;
Claude/Codex/fast health was OK, with zero running turns and pending approvals.
Primary HEAD, tracked cleanliness and owner-untracked metadata were unchanged;
the exact build scope ended inactive and empty. Local receipt:
`/home/johnw/.cache/jarvis-lead/codex-oct13/batch2-native-install-8a9f96bf1fa14199b88f47614ed2db27.json`.
Eleven current-main regression suites passed; the original stale callback-name
dispatch-audit failure and its separate corrected pass remain retained. See the
[measured Native acceptance](2026-10-13-browser-native-offline-cli.md) for supported
CLI/HUD profiles and composed evidence. Codex Chat Stop requires a fresh chat;
Claude's literal live child Act-hook observation remains UNRUN. Subscription
account authentication, paid-model judgment and physical/device acceptance are
not established by this deployment.

Verified installed release: Auth [#150](https://github.com/Johnwang688/jarvis/pull/150),
Find [#151](https://github.com/Johnwang688/jarvis/pull/151) and VEX
[#158](https://github.com/Johnwang688/jarvis/pull/158) are included in source
`f26848d1cf0186423754e7b2d139fd030bff7f42`, served HUD build
`20261014T043013447556Z-2350d3b4a8b1`. The normal guarded rebuild/restart exited 0;
Claude/Codex/fast health was OK, with zero turns and zero approvals, and primary
owner untracked files unchanged. The pinned-main smoke passed the build, 50 VEX
units, nine feedback/narrow HUD checks, dispatch-toolset checks and nine auth-state
tests; source stayed unchanged and every test scope was inactive and empty.
Real VEX tools/devices and robot Download remain **UNRUN**; USB-only indicators
do not establish radio pairing or extension-host health. Browser real-account
and physical sound acceptance are not implied by this installation.

GitGuardian's advisory failures on Auth/Find were independently adjudicated as
three historical synthetic authentication fixtures. Dummy values were clarified
without changing test inputs. The check remains recorded as failed; no scanner
suppression, history rewrite or admin bypass was used. See #150 for exact paths
and introducing commits.

B-NATIVE implementation and its reservation are complete. B-FIND developed
isolated helpers/UI in parallel, then integrated B-AUTH's accepted
navigation/attention/socket changes before its final freeze. Shared `host.py`,
`stream.py`, `RemoteView` and Browser
state changes must preserve the installed sound activity/restart and capture
identity contracts. Each feature publishes its proposed interface before shared
integration. The lead serializes merges/deployments; independent reviews,
meaningful boundary tests and current-main integration precede each merge.
Voice V3/V7/V8 and their App/capture/speech work remain with the Claude team;
this batch does not claim voice work or native subagent visibility.

**Test slots:** slot 1 remains reserved for the Claude voice team. Codex slots
2 and 3 are released: all Codex heavy jobs and installation are complete. Future
heavy work requires a new coordinating-lead grant after a memory check.
At most five heavy test/build jobs globally (raised from three on 2026-10-14, after WSL went from about 15 GB to 22 GB at the owner's request), each capped at 3 GiB and no swap;
start only with at least 4 GiB available. Native/provider probes need a concrete
reviewed plan; completed paid-probe exceptions are not reused. Physical/account
acceptance remains separately identified, never inferred from synthetic tests.

## Browser and native-agent packages — active and available

**Owner decision, 2026-10-14, lifted 2026-10-16:** Browser feature work was frozen
until the Browser bugs the owner reported were fixed. Those fixes are installed (first
table), and the owner lifted the freeze on 2026-10-16 now that agent slots are free:
the remaining unbuilt rows below are READY to claim, except B-PREVIEW, which is blocked on a
Chrome capability. Bug fixes:

| ID | Bug | Status |
|---|---|---|
| B-BUGS-1014 | Soft Remote frames (1× stretched on a 150% display); pointer/click lag (moves queued behind a 61/s Chrome ceiling at 160 Hz); "only loads local servers" flashing while an external address opens | **INSTALLED — [#186](https://github.com/Johnwang688/jarvis/pull/186), merged 2026-10-15; Claude lead, branch `claude/browser-sharp-input`. Owner confirmed 2026-10-15: sharp, no refusal flash; lag "noticeably better, usable" (some delay remains; frames at 1.5× cost ~38 ms to encode against ~17 ms).** Syntax colours (#191), editor tab icons and ruler git marks (#194) also owner-confirmed 2026-10-15. | Remote frames are sharp at `RASTER_SCALE` 1.5 (Chrome launches with `--force-device-scale-factor=1.5`; scale 2 was tried and dropped for memory); a still-queued pointer move or wheel merges into the next one, so a page that keeps up sees every event; a Direct tab shows "Opening…" instead of the refusal flash. Cost on a text-heavy page: press to changed frame 17 to 38 ms p50, a frame 338 to 638 KB. Synthetic and real-Chrome suites pass; nobody has looked at it on the owner's 4K display yet. Measurements and limits in `docs/notes/browser.md` ("Sharp frames and a pointer that keeps up"). |
| B-AUTH follow-up | Superseding an HTTP auth prompt (owner navigates away) | **INSTALLED — [#190](https://github.com/Johnwang688/jarvis/pull/190), merged 2026-10-15; Claude lead, `claude/browser-auth-navigate-away`.** A Browser bug fix, allowed under the freeze. | Found by QA-LOAD. The old load is now stopped (`Page.stopLoading`) while it is still paused on its challenge, and the prompt is finished (CancelAuth, reason `changed`) only if the stop has not settled it. Before, cancelling first let Chrome commit the held 401 page late and interrupt the new navigation ("navigation failed"). Review MED-1: while the stop is awaited the prompt is marked `answering`, so a refresh cannot republish it and an answer cannot be sent. New fake test plus a real-Chrome test (old order failed 3 of 3, new order passed 3 of 3). Split from #188 so it reverts alone. No real-account acceptance claim. |

| ID | Feature | Status / owner | Dependencies / definition of done |
|---|---|---|---|
| B-NATIVE (BR-9d, BD-a/b/c) | Claude and Codex parent chats drive through the same Browser gate | **INSTALLED — [#149](https://github.com/Johnwang688/jarvis/pull/149); Codex lead / `browser_native`; verified release above** | Reviewed head `20eb04bf` merged at `11b7db8c`; original reservation complete. Actual CLI + real HUD guarded action/resume passed for Claude ASK/AUTO and Codex AUTO; Codex ASK is unsupported by production policy. Both CLIs passed 115-second scripted private-card transport, actual sensitive HUD cards, pane Stop/Resume and production Chat Stop via `Daemon.interrupt` while real CDP typing settled. Codex Chat Stop requires a fresh chat; same-chat continuation remains refused. Actual Codex child host denial passed; Claude 2.1.295 child catalog omission plus separate Driving-ON hook guard/mutant are distinct evidence, with literal live child Act-hook observation UNRUN. Eleven current-main regression suites and the separate corrected dispatch audit passed; original failures retained. Subscription/account authentication, paid-model judgment and physical-device acceptance remain UNRUN. [Native plan](2026-10-13-browser-driving-claude-codex.md). |
| B-AUTH (BR-7) | HTTP Basic/Digest owner challenge form over the existing authenticated tab socket | **INSTALLED — [#150](https://github.com/Johnwang688/jarvis/pull/150); Codex lead / `browser_auth`; verified build above** | Exact reviewed head `70460c2` merged at `3b1205ae`. First protected navigation, delayed reply/original expiry, Digest, stale identity, restore, privacy and cleanup passed focused checks; 62 auth/Sound checks, nine native cases, 14 actual HUD assertions and five guard mutants passed. Combined release correction gates passed as described above. Same-origin iframe prompts measured; first protected popup required explicit Reload, and separate OOPIF prompt delivery was absent in the bounded probe. Protected Sound restoration remains fail-closed with Retry. No real-account acceptance claim. |
| B-FIND (BR-7) | Find-in-page, bounded owner context menu, finite cursor shapes | **INSTALLED — [#151](https://github.com/Johnwang688/jarvis/pull/151); Codex lead / `browser_find`; verified build above** | Exact reviewed head `5e638ea` merged at `dd210803` after #150. Fixed top-document search, five owner menu actions, finite cursors and exact document/socket/hold guards passed independent review. Final focused evidence: 70 backend checks, 19 real driving cases, real Chrome owner tools, 22 actual owner HUD assertions and seven intended guard mutants; all 14 combined correction suites passed. Navigation discards stale document input without adding an approval hold. Native layout preemption remains unproven; native picker/IME expansion excluded. |
| B-PICKERS (BR-7/M9) | Owner select/date/color fallback; assess datalist separately | **INSTALLED — [#232](https://github.com/Johnwang688/jarvis/pull/232), merged 2026-10-16; Claude lead, branch `claude/b-pickers`; owner live check pending.** | Design and measurements: [the B-PICKERS plan](2026-10-16-browser-pickers.md). The owner's own click is pinned to the element under it; after the release, that exact element must be `:open`. The native popup is closed with a trusted Escape and the HUD overlay is shown; the choice is applied only to the same node and frame, with the option identity checked, and input/change re-checked step by step (untrusted events). Never focuses itself, never under a card. Real Chrome 155 suite, vitest and face check, with 16 of 16 mutants killed. Datalist assessed, not built: headless shows no suggestions. Not covered: datalist (assessed, not built), cross-site iframes and keyboard-opened pickers. |
| B-IME (BR-7) | Finish real owner IME/composition acceptance | **INSTALLED — [#233](https://github.com/Johnwang688/jarvis/pull/233), merged 2026-10-16, with follow-up [#235](https://github.com/Johnwang688/jarvis/pull/235); Claude lead; owner live Windows IME check pending.** | Composition transport exists in Remote UI; do not rebuild it blindly. Audit done in #233: demonstrated gaps are fixed; limits are in `docs/notes/browser.md`. Real Windows composition is still unchecked. |
| B-DIRECT-CAPTURE (BR-4/M6) | Direct iframe capture in installed app mode | **READY — unclaimed; freeze lifted by the owner 2026-10-16, unbuilt.** Was QUEUED (freeze, 2026-10-14) and before that VERIFY — unclaimed | Remote capture/annotation is installed. Direct currently offers explicit “Open in Remote” fallback. Requires actual Windows app-mode getDisplayMedia/CropTarget prompt and pixel evidence before enabling a Direct capture path. |
| B-PREVIEW (BR-9b/9c) | Proven approval crop / target outline | **DEFERRED — blocked on a Chrome capability** (freeze lifted 2026-10-16, but nothing is buildable until the proof exists), unbuilt. Was QUEUED (freeze, 2026-10-14) and before that DEFERRED | Current Chrome 155 screencast metadata cannot prove matching-frame identity. Keep “Preview unavailable” and no outline until a reviewed capability proof exists; synthetic crop tests are insufficient. **Re-checked 2026-10-20, still blocked:** [capability probe](2026-10-19-browser-preview-capability.md), logs in `~/.cache/jarvis-lead/b-preview-probe/logs/`. Chrome 155 has no `HeadlessExperimental`. Screencast metadata has no frame id, and its timestamp matched the DOM in 2 of 182 frames under 11 ms changes. A freeze-plus-bracketed `captureScreenshot` was defeated by a CSSOM toggle (7 of 28 accepted captures false; 24 of 29 in the first run). Only chrome-headless-shell with begin-frame control and paused virtual time held (120/120). That would mean a different browser, daemon-driven frames and a frozen page per card: an owner decision, not recommended. Not probed: `setScriptExecutionDisabled`, `Debugger.pause`, other virtual-time policies, `DOMSnapshot`. |
| B-LOGIN (M4/M4b/M12) | Real site sign-in, Chrome sync entry and password-store behavior | **INSTALLED — [#215](https://github.com/Johnwang688/jarvis/pull/215) (keyring password store), [#218](https://github.com/Johnwang688/jarvis/pull/218) (observed-store follow-up), [#217](https://github.com/Johnwang688/jarvis/pull/217) (sign-in pop-up adoption); Claude lead; lead deploy log 2026-10-15/16.** Built. Owner-assisted check pending: a real Google sign-in on a keyring profile. Was QUEUED (freeze) | Named profiles keep Chrome's password and cookie key in the owner's keyring (`gnome-libsecret`) instead of the public `basic` store; a locked or missing keyring refuses the launch with an unlock hint, and a profile that has used the keyring is never quietly downgraded (sticky marker). [#218](https://github.com/Johnwang688/jarvis/pull/218) observes which store Chrome actually used (a probe cookie's `v11`/`v10` prefix) and adds the Secret Service gate rule in `rules.py`. [#217](https://github.com/Johnwang688/jarvis/pull/217) adopts Chrome's own `accounts.google.com` sign-in tab beside the sign-in tab. All fakes and isolated real-Chrome checks; **no real Google, Cloudflare or GitHub sign-in has been done.** Do not read real credentials or weaken automation/sandbox policy to make a test pass. The keyring protects passwords at rest, not against a program running as the owner while the keyring is unlocked. Details in `docs/notes/browser.md`. |
| B-PERF (M1/M2/M5/M7/M8) | Browser performance and platform acceptance record | **INSTALLED (input-to-picture part) — [#214](https://github.com/Johnwang688/jarvis/pull/214), merged 2026-10-16; Claude lead.** The rest is measurement and owner decisions; a Chrome tile-size sweep remains. Was QUEUED (freeze) | Per-stage pipeline measured with `scripts/browser_measure.py`; the HUD acks a frame when its decode starts. A 20-agent latency search found no further change worth more than about 1 ms; adaptive quality and the other candidates were measured and rejected (browser.md, "Remote input to picture, stage by stage"). **Remaining:** a Chrome tile-size measurement sweep. **Bigger levers are owner decisions, not built:** 1x frames while the pointer moves, a video stream, a GPU for Chrome. Still missing: the owner's real Windows input-to-picture p50/p95 (recipe in that section: `flip.html` + `owner_snippet.js`). Reconcile frame/CPU/profile-memory, Local Network Access and sandbox receipts against current source; do not label all five measurements unimplemented or all passed. |

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
| V2 | STT deadlines/retries, retained clips, visible errors and sequence ordering | **INSTALLED — [#136](https://github.com/Johnwang688/jarvis/pull/136), Claude team** | Installed build `20261014T021138099135Z-87990c016981` from main 6bbef98, daemon restarted, providers healthy. No physical-mic acceptance. Follow-ups: stale "Not retried" error line; oversize-clip message wording; no test for `place()` throwing. `/stt` and `/say` had no `owner_only` check (pre-existing; closed by [#227](https://github.com/Johnwang688/jarvis/pull/227)). |
| V3 | Dictation steers busy chats; push-to-talk no longer interrupts; continuation window | **INSTALLED — [#145](https://github.com/Johnwang688/jarvis/pull/145), Claude team** | Branch `claude/voice-v3`, stacked on V2 until it merges. Owner answered plan Q3–Q5 (send at the pause, then steer; PTT steers; reply goes to the pane that spoke). Verify busy/idle transitions, owner destination identity and unchanged Ctrl+D gesture. |
| V4 | AudioWorklet/resampler, uncapped utterance recorder and chunked STT | **INSTALLED — [#160](https://github.com/Johnwang688/jarvis/pull/160), Claude team; in build `20261015T001651343547Z-106a9cf598f7` from main 5674901** | Branch `claude/voice-v4`. stacked from main. V2. Bounded buffering, ordered chunks and long dictation without silent truncation. |
| V5 | Silero VAD worker, energy fallback and end-of-utterance timers | **INSTALLED — [#168](https://github.com/Johnwang688/jarvis/pull/168), Claude team; build `20261015T005432633348Z-e50371719a7f` from main 8a5c910** | Branch `claude/voice-v5`. after V4. V4 + V0 evidence. Test pause/continuation boundaries and degraded-worker fallback. |
| V6 | Streaming speech core: prose filter, sentence splitting, /say, one-thread player/queue, controls and output separate from mic | **INSTALLED — [#134](https://github.com/Johnwang688/jarvis/pull/134), Claude team** | Same install as V2. No physical speaker/AEC acceptance. 1200-character spoken cap is a stopgap replaced by V8. Follow-ups from review: headerless diffs still read; barge-in with nothing playing pauses nothing; buffering counts as active during a synthesis hang. |
| V7 | One shared queue across every open chat pane, per-thread voices, floor/stale/backlog rules | **INSTALLED — [#144](https://github.com/Johnwang688/jarvis/pull/144), Claude team** | Branch `claude/voice-v7`, stacked on V6 until it merges. Hidden open panes speak; threads in no pane are silent; playback never overlaps. Includes the per-pane voice chip (Q9). |
| V8 | Bounded spoken summaries using Haiku tier, fallback, cost/latency measurement | **INSTALLED — Codex lead's build from main 11b7db8; fix in [#164](https://github.com/Johnwang688/jarvis/pull/164)** | Branch `claude/voice-v8`, stacked on V6. Owner answered Q2: replies over 600 characters become a summary only. One paid cost/latency probe approved, capped at $0.10; no other paid calls. Fix installed in build `20261014T211807284758Z-2350d3b4a8b1` from main 3165b31. Probe receipt (second run, 2026-10-14, voice-face notes): 24/24 summaries, $0.0042. |
| V9 | Barge-in, self-voice reference gating and transcript commands | **INSTALLED — [#177](https://github.com/Johnwang688/jarvis/pull/177), Claude team; build `20261015T010706274548Z-c0f9136d0f93` from main 01c2422** | Branch `claude/voice-v9`. after V5; Q7 stricter (only "Jarvis"/Ctrl+D pause before enrollment), Q8, Q10 answered. V5 + V6. Voice must never authorize an approval card; real speaker/headphone checks remain separate from fake audio tests. |
| V10 | Trailing send/cancel words and Undo | **INSTALLED — [#173](https://github.com/Johnwang688/jarvis/pull/173), Claude team; build `20261015T023806896445Z-ebe8a80def39` from main f99ae87** | Branch `claude/voice-v10`. after V5; Q1 answered. V5 + V2. Whole-utterance/trailing-silence semantics; ordinary prose containing “over” must not send early. |
| V11 | Local wake/command/send keyword spotting; retire phrase-avatar Chrome recognition | **INSTALLED — [#183](https://github.com/Johnwang688/jarvis/pull/183), Claude team; build `20261015T032937780637Z-9838f37042df` from main 21ab31e; Chrome's recognizer removed** | Branch `claude/voice-v11`. after V9; Q16 answered (local wake only, commands via STT). V0 + V9. Choose only after measured resource/accuracy evidence. |
| V12 | Local speaker enrollment/verification/store and persistent Guest voice switch | **INSTALLED — [#170](https://github.com/Johnwang688/jarvis/pull/170), Claude team; build `20261015T001651343547Z-106a9cf598f7` from main 5674901, daemon restarted, speaker model fetched (SHA-pinned)** | Branch `claude/voice-v12`. after V4; Q12 answered (keep clips). V0 + V4. Store stays local, mode0700, never uploaded; owner decided no additional protected-path rule. Guest survives restart until explicitly off and changes no approval or self-voice rule. |
| V13 | Local STT with explicit cloud-fallback switch | **INSTALLED — Claude lead; merged as [#159](https://github.com/Johnwang688/jarvis/pull/159) and installed in build `20261014T224813840513Z-de8b643e2d81` from main f33c810** | Branch `claude/voice-v13`. Q6 answered; one paid cloud-vs-local WER check <= $0.25. V0 + V2. Offline rejection/selection and cloud fallback must be visible and tested. Default stays cloud per the comparison receipt (voice-face notes). No physical-mic acceptance yet. |
| V14 | RNNoise worker stage | **INSTALLED — [#175](https://github.com/Johnwang688/jarvis/pull/175), Claude team; build `20261015T020107337609Z-1f9832b45a61` from main 52bd073; switch off by default** | Branch `claude/voice-v14`. after V5; owner: build it, default off. V0 + V5; implement only if V0 demonstrates a gain. |
| V15 | Coordinate speech with Browser audio | **INSTALLED — Claude lead; implementer Sonnet 5.5; merged as [#156](https://github.com/Johnwang688/jarvis/pull/156) and installed in build `20261014T215321486547Z-6198f6bb9128` from main 729c86f** | Branch `claude/voice-v15`. The duck has not been tested by ear on real speakers. owner: duck Remote tabs via WSLg PulseAudio sink-inputs under a renewable lease; never unmute. V7 + B-SOUND. Resolve the plans' audio-transport mismatch: Remote audio leaves through WSLg, not the frame stream; Direct cross-origin audio is not observable/controllable by default. No speculative ducking hook or unmute. Probe + design + crash behaviour: voice-face notes, "V15". Physical audibility UNMEASURED. |

## Native subagent view

[Full subagent plan](2026-10-13-subagent-view.md). This is visibility and stop-one
for native Claude/Codex children, not a second task scheduler. Children stay silent
apart from the coalesced finish/failure notices (SV-5, installed in [#228](https://github.com/Johnwang688/jarvis/pull/228)). Existing child-tool restrictions remain.

| ID | Feature | Status / owner | Dependencies / acceptance |
|---|---|---|---|
| SV-0 | Capture actual native Claude/Codex child protocol and lifecycle fixtures | **INSTALLED — [#193](https://github.com/Johnwang688/jarvis/pull/193), merged 2026-10-15; Claude lead team; lead deploy log 2026-10-15/16** | Recorded sanitized fixtures for one/parallel/background/nested/interrupted children; Claude stop_task, Codex experimental schema and child notifications/interrupt/token totals. Findings in [2026-10-15-sv0-findings.md](2026-10-15-sv0-findings.md). |
| SV-1 | Claude child tagging/events, daemon record/routes/runner and API docs | **INSTALLED — [#198](https://github.com/Johnwang688/jarvis/pull/198), merged 2026-10-15; Claude lead team; lead deploy log 2026-10-15/16** | Provider tagging, daemon record and owner routes. Child prose stays out of the parent/Discord answer; approval identity preserved. |
| SV-2 | Codex child set/routing, spawn events and non-fatal child approval handling | **INSTALLED — [#199](https://github.com/Johnwang688/jarvis/pull/199), merged 2026-10-15; Claude lead team; lead deploy log 2026-10-15/16** (with [#195](https://github.com/Johnwang688/jarvis/pull/195), a known sub-agent's server request no longer kills the orchestrator's turn, and [#200](https://github.com/Johnwang688/jarvis/pull/200), a Codex Stop no longer ends the chat) | Codex children are their own records; child approvals are judged, never fatal to the parent. |
| SV-3 | HUD child store, inline spawn rows and read-only transcript pane | **INSTALLED — [#212](https://github.com/Johnwang688/jarvis/pull/212), merged 2026-10-15; Claude lead team; lead deploy log 2026-10-15/16** | Child store outside React, inline spawn rows, read-only sub-agent pane and card-to-row link. Nothing spoken or posted to Discord. |
| SV-4 | Stop one child via owner route and button | **INSTALLED — [#216](https://github.com/Johnwang688/jarvis/pull/216), merged 2026-10-16; Claude lead team; lead deploy log 2026-10-15/16** (leftovers in [#220](https://github.com/Johnwang688/jarvis/pull/220)) | Owner-only `POST /threads/{id}/subagents/{sid}/stop` and a Stop button per live row and pane header. Claude per-child Stop rests on SV-0's offline evidence for CLI 2.1.295; Codex per-child Stop is offered with its caveat shown. Silent: nothing about a Stop is spoken or posted. |
| SV-5 | Coalesced child finished/failed speech notices | **INSTALLED — [#228](https://github.com/Johnwang688/jarvis/pull/228), merged 2026-10-16.** | SV-1 and V7 are merged. HUD-only: one short spoken notice per chat window (3 s) for finished or failed children. Never reads child prose, names or briefs; silent for owner Stops; not posted to Discord. Physical speaker and live child checks not run. |
| SV-6 | Read child activity between orchestrator turns | **INSTALLED — [#234](https://github.com/Johnwang688/jarvis/pull/234), merged 2026-10-16; owner live check pending.** | SV-1/2 are merged. One watcher per session reads child activity between turns. A CLI-started turn is recorded as `self_started`, unposted to Discord and unspoken, per the owner's 2026-10-15 decision. High-risk reader lifecycle change. |

## HUD editor polish — installed 2026-10-15

| ID | Feature | Status / owner | Definition of done |
|---|---|---|---|
| ED-MARKS | Git marks (added, modified, deleted) in the editor's overview ruler | **INSTALLED — [#194](https://github.com/Johnwang688/jarvis/pull/194), merged 2026-10-15; Claude lead team; owner-confirmed 2026-10-15; lead deploy log 2026-10-15/16** | The accepted limit on owner global git filters in editor status (HUD-FOLLOWUPS below) is unchanged. |
| ED-ICONS | File-type icons on editor tabs | **INSTALLED — [#194](https://github.com/Johnwang688/jarvis/pull/194), merged 2026-10-15; Claude lead team; owner-confirmed 2026-10-15; lead deploy log 2026-10-15/16** | Same PR as ED-MARKS. Owner decisions on editor and tab dimensions and input behavior still apply. |

## VEX view redesign — 2026-10-15/16

[Plan](2026-10-15-vex-ui-redesign.md), [mockup](2026-10-15-vex-ui-mockup.html), owner decisions of
2026-10-15 in the plan's section 0.1. Fakes only; real VEX tools, robot Download and the hardware checks H1 to H10 are UNRUN.

| ID | Feature | Status / owner | Dependencies / definition of done |
|---|---|---|---|
| VX-UI-1 | Layout shell: header with slot chip, Clean/Build/Rebuild/Download bar, status line with Stop, tabbed output | **INSTALLED — [#209](https://github.com/Johnwang688/jarvis/pull/209), merged 2026-10-15; Claude lead team; lead deploy log 2026-10-15/16** | One Download button ("Download to slot N") that chains clean, build and download; Stop closes the running action's own terminal (a trusted press only). No Run button: the owner starts programs from the robot. |
| VX-UI-2 | State marks on steps and tab badges | **INSTALLED — finished in [#229](https://github.com/Johnwang688/jarvis/pull/229), merged 2026-10-16** (marks first landed in [#209](https://github.com/Johnwang688/jarvis/pull/209)) | Idle/running/done/failed/stale/unknown marks with glyph and label; stale is advisory only. A mark and its tab badge read one `actionState` function. |
| VX-UI-3 | First-use help | **INSTALLED — finished in [#229](https://github.com/Johnwang688/jarvis/pull/229), merged 2026-10-16**, in the owner's simplified form | Permanent hover help plus a `?` list with numbered controls; the plan's first-use counts were dropped by the owner. |
| VX-UI-4a | Brain tab: the brain terminal in the VEX pane, Connect only on a trusted press | **INSTALLED — [#213](https://github.com/Johnwang688/jarvis/pull/213), merged 2026-10-16; Claude lead team; lead deploy log 2026-10-15/16** | Details in `docs/notes/vex.md`, "Brain tab, VX-UI-4a". |
| VX-UI-4b | Brain startup reply and `--quiet --user <port>` | **INSTALLED — [#240](https://github.com/Johnwang688/jarvis/pull/240), merged 2026-10-17; follow-ups [#241](https://github.com/Johnwang688/jarvis/pull/241) (Connect offered once a download ends) and [#242](https://github.com/Johnwang688/jarvis/pull/242) (review LOWs)** | Built after hardware check H1 showed a blank Brain tab. Hardware-unverified until the owner's next session (H1, H3, H4). Details in `docs/notes/vex.md`, "Brain terminal startup, VX-UI-4b". |
| VX-UI-5 | Run / Stop | **DROPPED** (owner, 2026-10-15: no Run button) | Stop is VX-UI-1's cancel of a running Clean/Build/Download. |
| VX-UI-6 | Docs | **INSTALLED — [#229](https://github.com/Johnwang688/jarvis/pull/229), merged 2026-10-16** | `docs/notes/vex.md` ("The VEX view as built"), `docs/hud-api.md` (no route changed) and the decisions file (V-3 amendment). |
| H1 to H10 | Hardware checks | **VERIFY / owner-assisted — UNRUN** | Include H9/H10 (what a Stop mid-download leaves on the brain and COM3). Open firmware decision: braking during a blocking whenWithin after a chained move. |

## 2026-10-16 additions outside the checkpoint rows

| Area | Completed scope / evidence | Remaining qualification |
|---|---|---|
| Buttons | [#210](https://github.com/Johnwang688/jarvis/pull/210) 4px rectangle buttons and a binary click sparkle | **INSTALLED** (lead deploy log 2026-10-15/16); no outstanding row. |
| Secrets display | [#205](https://github.com/Johnwang688/jarvis/pull/205) typed credentials are redacted in labels, approval cards and logs; the gate judges the raw text and what runs is never hidden | **INSTALLED** (lead deploy log 2026-10-15/16); no outstanding row. |
| Gate: merge/close | [#208](https://github.com/Johnwang688/jarvis/pull/208) merging or closing a pull request always asks the owner; [#222](https://github.com/Johnwang688/jarvis/pull/222) and [#225](https://github.com/Johnwang688/jarvis/pull/225) close its delta-review findings (trailing `--`, open-ended git exec settings, GraphQL `$b`, blanked refs) | **INSTALLED** (lead deploy log 2026-10-15/16). `merge_gate.py` and `rules.py` are SELF_PROTECTED; the owner reviews those hunks. |
| Gate: commands | [#211](https://github.com/Johnwang688/jarvis/pull/211) Monitor and any command-bearing tool are judged as a command; [#220](https://github.com/Johnwang688/jarvis/pull/220) the Secret Service rule and SV-4 Stop-label leftovers | **INSTALLED** (lead deploy log 2026-10-15/16). |
| Diff tab | [#219](https://github.com/Johnwang688/jarvis/pull/219) one-click Quick review (Claude Haiku, then Codex Luna, then v1 Haiku; read-only; a harness at 75% or more of any quota window is skipped) | **INSTALLED** (lead deploy log 2026-10-15/16). Owner-assisted: the first real press has not been made. Every test used fakes. |
| Codex chat git | [#223](https://github.com/Johnwang688/jarvis/pull/223) Codex chats push, open and merge or close pull requests through Jarvis, which keeps the GitHub login (CG-0, CG-1); [#224](https://github.com/Johnwang688/jarvis/pull/224) the Codex sandbox read gap is closed with default-closed HOME dot entries, the Windows profiles and key-like files (CG phase 2). [Plan](2026-10-15-codex-git-credentials.md) | **INSTALLED** (lead deploy log 2026-10-15/16), daemon restarted. Live check is GIT-LIVE. `permissions.py` and `session_tools.py` hunks await the owner's SELF_PROTECTED review. |
| Codex turns | [#200](https://github.com/Johnwang688/jarvis/pull/200) a Stop no longer ends the chat; [#207](https://github.com/Johnwang688/jarvis/pull/207) partial turns disclosed in usage, ledger and HUD | **INSTALLED** (lead deploy log 2026-10-15/16). |
| Sidebar | [#206](https://github.com/Johnwang688/jarvis/pull/206) each project shows its five most recent chats | **INSTALLED** (lead deploy log 2026-10-15/16). |
| Voice routes | [#227](https://github.com/Johnwang688/jarvis/pull/227) `/stt`, `/say`, `/voices`, `/voice` and `/mute` are owner-only; the check runs before the query, body or audio work. | **INSTALLED** (lead's 2026-10-16 deploy). The model and avatar pickers were not changed. |
| Codex profiles | [#231](https://github.com/Johnwang688/jarvis/pull/231) follow-ups to [#224](https://github.com/Johnwang688/jarvis/pull/224): `/mnt/c` walk deadline with a chat notice, symlinked profile folders, glob-syntax names, repo-top credential files. | **INSTALLED** (lead's 2026-10-16 deploy). Stores created mid-session stay readable until the next session start. |
| Gate and Codex leftovers | [#230](https://github.com/Johnwang688/jarvis/pull/230) review leftovers of [#223](https://github.com/Johnwang688/jarvis/pull/223) and [#225](https://github.com/Johnwang688/jarvis/pull/225) (chat git and merge gate) and a `rules_check` path flake. | **INSTALLED — [#230](https://github.com/Johnwang688/jarvis/pull/230), merged 2026-10-16.** It edits SELF_PROTECTED files (`merge_gate.py`, `permissions.py`, `bridge.py`, `tools/__init__.py`); independent review passed. |

## 2026-10-17 to 2026-10-19 additions

| Area | Completed scope / evidence | Remaining qualification |
|---|---|---|
| Claude model menu | [#238](https://github.com/Johnwang688/jarvis/pull/238) offers Claude Haiku 5.5 | **INSTALLED.** |
| Codex projects | [#239](https://github.com/Johnwang688/jarvis/pull/239) a project rooted at the owner's home is refused with a clear message | **INSTALLED.** |
| Security (dup probe) | SEC-1 [#246](https://github.com/Johnwang688/jarvis/pull/246) `127.<name>` hosts no longer skip the LAN refusal (tighten-only); SEC-2 [#243](https://github.com/Johnwang688/jarvis/pull/243) unseen characters stripped from the approval card's origin; SEC-3 [#245](https://github.com/Johnwang688/jarvis/pull/245) full credential scrub on Discord and diff-review output, with [#248](https://github.com/Johnwang688/jarvis/pull/248) stripping invisible characters before it; SEC-4 [#244](https://github.com/Johnwang688/jarvis/pull/244) credential files private from creation; SEC-5 [#261](https://github.com/Johnwang688/jarvis/pull/261) `scrub_text` redacts a private key's whole block; SEC-6 [#263](https://github.com/Johnwang688/jarvis/pull/263) cards, logs and v1 tool lines hide it too. [Specs](2026-10-18-security-fix-specs.md) | **INSTALLED.** Open LOWs: `scrub_text` misses a short `Bearer` token that `chat_reads.scrub` catches; a non-compaction turn's `turn_finished` reason is published unscrubbed. |
| Duplicate cleanup | CL-1a [#251](https://github.com/Johnwang688/jarvis/pull/251), CL-1b [#249](https://github.com/Johnwang688/jarvis/pull/249) shared test helpers; CL-2 [#252](https://github.com/Johnwang688/jarvis/pull/252) HUD path and storage helpers; CL-3 [#253](https://github.com/Johnwang688/jarvis/pull/253) one Host-header check; CL-4 [#250](https://github.com/Johnwang688/jarvis/pull/250); one HUD modifier-key list [#255](https://github.com/Johnwang688/jarvis/pull/255). [Plan](2026-10-18-dedupe-cleanup.md) | **INSTALLED.** Behaviour unchanged by rule. Copies in SELF_PROTECTED files wait on the owner. |
| Tests | [#254](https://github.com/Johnwang688/jarvis/pull/254) the live Discord handshake is opt-in (`JARVIS_LIVE_DISCORD=1`); [#256](https://github.com/Johnwang688/jarvis/pull/256) HUD voice tests speak only once the capture listens | **MERGED.** |
| HUD usage | [#247](https://github.com/Johnwang688/jarvis/pull/247) real Claude and OpenAI marks | **INSTALLED.** |
| v2 stores | [#267](https://github.com/Johnwang688/jarvis/pull/267) a log read skips a torn last line still being written, and splits on `\n` only (a U+2028 in a record no longer splits it) | **INSTALLED.** Found as a `chat_relay_check` flake. |

## HUD directional split (2026-10-19)

Split right or down, VS Code style, up to twelve panes. Owner answers FS-1..FS-10: [decisions](2026-10-19-hud-free-split-decisions.md); [plan](2026-10-19-hud-free-split-plan.md); as built in `docs/notes/hud-workspace.md`. Claude lead team.

| ID | Scope | Status |
|---|---|---|
| FS0 | The Browser pane contract grows to 12 | **INSTALLED — [#258](https://github.com/Johnwang688/jarvis/pull/258)** |
| FS1 | The pure split tree (`splittree.ts`) | **INSTALLED — [#257](https://github.com/Johnwang688/jarvis/pull/257)** |
| FS2 | Twelve pane ids, still on the ladder | **INSTALLED — [#259](https://github.com/Johnwang688/jarvis/pull/259)** |
| FS3 | The tree becomes the workspace's model, behaviour preserved (with the plan's §10 1px-rule erratum) | **INSTALLED — [#264](https://github.com/Johnwang688/jarvis/pull/264)** |
| FS4 | Directional split and close | **INSTALLED — [#268](https://github.com/Johnwang688/jarvis/pull/268)** |

## Compact button (2026-10-19)

A per-chat button that runs each provider's own compaction: Claude's CLI `/compact`, Codex's `thread/compact/start`, the fast path's `Agent.compact_now`. Owner answers CB-1..CB-5 and the plan: [compact-button plan](2026-10-19-compact-button-plan.md). **INSTALLED — [#270](https://github.com/Johnwang688/jarvis/pull/270)** (WP0 recording, WP-A route and providers, WP-B Codex, WP-C HUD chip). Not built: typed `/compact <focus>` (CB-4, later; Claude first). No live press is recorded here.

## Claude skills in Jarvis and Codex chats (2026-10-19)

`~/.claude/skills` (the owner's and the claude.ai-synced skills) is a second, read-only skill root for Jarvis chats, and `jarvis skills link` links those skills into `~/.codex/skills`. Owner answers SK-1, SK-2: [plan](2026-10-19-claude-skills-link.md). **INSTALLED — [#272](https://github.com/Johnwang688/jarvis/pull/272).** The first link run made 25 Codex links; the `/` menu lists 32 skills. Rerun `jarvis skills link` after adding a skill.

## Chat-to-chat (2026-10-19)

Chats read and message each other. Owner answers CC-1..CC-9: [decisions](2026-10-19-chat-to-chat-decisions.md); [plan](2026-10-19-chat-to-chat-plan.md); rules in [control-plane.md](../notes/control-plane.md#chat-to-chat-the-rules-in-one-place-c0-c5-merged-2026-10-19). Claude lead team.

| ID | Scope | Status | Remaining qualification |
|---|---|---|---|
| C0 | The read projection (`chat_reads.py`) and `untrusted.fence_chat` | **MERGED — [#260](https://github.com/Johnwang688/jarvis/pull/260); INSTALLED** (daemon restart 2026-10-19) | None. |
| C1 | `chat_list`, `chat_summary`, `chat_search`, `chat_read` on the fast path, Claude and Codex | **MERGED — [#262](https://github.com/Johnwang688/jarvis/pull/262); INSTALLED** | Free suites only; no live receipt is recorded here. |
| C2 | Owner-typed authority | **FOLDED into C3** | CC-6 dropped the `typed` desk key: typed or spoken HUD turns send, decided by the desk. |
| C3 | The relay core (`chat_relay.py`), queues, replies, Stop, Withdraw route | **MERGED — [#265](https://github.com/Johnwang688/jarvis/pull/265); INSTALLED** | L1, L2 not run. |
| C4 | `chat_send` and `chat_wait` on all three providers | **MERGED — [#266](https://github.com/Johnwang688/jarvis/pull/266); INSTALLED** | L1, L3 not run. |
| C5 | The HUD: relay rows, waiting line, sidebar badge, Withdraw | **MERGED — [#269](https://github.com/Johnwang688/jarvis/pull/269); INSTALLED** | Free and mock-daemon suites only; no live receipt is recorded here. |
| C6 | Approval-card provenance ("turn started by a message from chat ‹title›") | **DEFERRED — optional, not done** | Add if L4 shows the owner cannot tell where a relayed turn's card came from. |
| C7 | These notes, the `hud-api` contract and the live-gate runbook | **MERGED — [#271](https://github.com/Johnwang688/jarvis/pull/271)** | The runbook is a plan; nothing in it has been run. |
| L1-L4 | Native release checks (a 300 s MCP call on Claude, mid-turn reply labelling, sub-agent and child refusal, a relayed Claude turn's approval on the owner's card) | **NOT RUN** | [Runbook](2026-10-19-chat-to-chat-live-gates.md); run only on the owner's go, by the lead. |

## Original checkpoint and requested fixes already landed

These are not available implementation tasks. All listed code is included in the
verified installed baseline, unless a remaining acceptance qualification is noted.
PR #125 itself adds plans, not the features those plans describe.

| Area | Completed scope / evidence | Remaining qualification |
|---|---|---|
| Chat Git/worktrees | [#88](https://github.com/Johnwang688/jarvis/pull/88) sandbox roots; [#92](https://github.com/Johnwang688/jarvis/pull/92) owner approval routing | Superseded for Codex chats by [#223](https://github.com/Johnwang688/jarvis/pull/223) and [#224](https://github.com/Johnwang688/jarvis/pull/224) (see Codex chat git below). Final-policy positive real push→PR acceptance is GIT-LIVE; denial/resume and local commit were verified. Old pre-routing probes are not that receipt. |
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
| QA-LOAD | Load-sensitive tests and reliable sweep reporting | **INSTALLED — [#188](https://github.com/Johnwang688/jarvis/pull/188), merged 2026-10-15; Claude lead, `claude/qa-load`.** | `tests/loadclock.py` judges load-sensitive bounds at idle speed; the `rules`, VEX, terminal and devurl bounds use it, and the runner collision test is fixed. Honest limit: under load a bound can be about 30% looser than at idle, so a machine under load can pass a regression that an idle machine would fail. An idle judge is exact. A failing bound is still a failure; none was removed. Per-suite output and real exit codes stay required. The navigate-away auth race it found shipped separately as #190. |
| GIT-LIVE | Positive end-to-end native Git flow under the final approval policy | **VERIFY / owner-assisted — the owner's live check of [#223](https://github.com/Johnwang688/jarvis/pull/223) and [#224](https://github.com/Johnwang688/jarvis/pull/224).** Both are merged; neither has a live Codex receipt. Check, with a disposable authorized repo and branch and a real owner approval: a Codex chat's `chat_git_push` / `chat_pr_create` card and result, and that a plain sandboxed command still cannot open the gh token, `.env`, `~/.codex/auth.json`, `~/.claude/.credentials.json` or the credential folders. Local worktree/edit/commit and denied/resumed pushes were checked earlier; do not reuse old fixture-helper evidence as native proof. |
| HUD-FOLLOWUPS | Reconcile the checkpoint's old `hud-open-followups` list | **DONE — audited 2026-10-14; [#187](https://github.com/Johnwang688/jarvis/pull/187) merged 2026-10-15.** The "Jarvis can read" switch now follows other windows and an answered task question clears at once. | The old accepted-at-merge list is closed by evidence. Git marks in the editor overview ruler shipped as ED-MARKS above. **Accepted limits, not tasks:** `terminal_guard` over-refuses a command with `> `; the AST walker has an `importlib` blind spot; the fence tier is keyed on `fetch_page`/`web_search`; a leftover `.tmp` can remain after a hard crash; `/mnt/c` delete by path; owner global git filters apply to editor status; the Browser gate is shared across profiles (Browser frozen). |
| NOTES-CHECK | `slim_claude --check` failing on the archived notes | **INSTALLED — [#185](https://github.com/Johnwang688/jarvis/pull/185) (`editor.md`) and [#189](https://github.com/Johnwang688/jarvis/pull/189) (`terminals.md`), merged 2026-10-15; docs and tooling only.** | Each restored the archived `claude-source` payload byte for byte and moved the current text outside it (the terminals readable-switch note moved out of the archive). `python scripts/slim_claude.py --check` passes again. |
| HUD-HARDENING | Owner hardening decisions of 2026-10-15 | **INSTALLED — [#191](https://github.com/Johnwang688/jarvis/pull/191), merged 2026-10-15.** | The approval page's CSP has no `'wasm-unsafe-eval'`. Only a `Sec-Fetch-Dest: worker` fetch of a `.js` directly under `/assets/` gets it, and those responses send `Vary: Sec-Fetch-Dest`; a path-only grant was rejected in review because a same-origin popup gained a realm that could compile WebAssembly. The three WebAssembly users (syntax, VAD, wake word) run in module workers. `jarvis hud rebuild` runs `npm ci --ignore-scripts`; this closes only the install-time route. Decisions in [the editor decisions](2026-10-09-hud-editor-decisions.md) under "Added 2026-10-15"; worker invariant in `docs/notes/editor.md`. |
| VEX-DOWNLOAD | Match Download output/completion to Build/Clean; visible brain/controller connectivity and timestamped connection history | **INSTALLED — [#158](https://github.com/Johnwang688/jarvis/pull/158); Codex lead / `browser_auth`; verified build above.** Branch `codex/vex-download-device-status`, runtime `70dfd6c`, [contract/verified gates](2026-10-13-vex-download-device-status.md). Stable actual-PTY Download output/final records; shared source-verified USB-only Brain/Controller checks with unknown/busy/stale preservation and bounded timestamped history. PASS: 38 focused Python, 50 targeted HUD units, production build, 49 selected actual-HUD checks, 9 feedback/narrow checks and 7 Build/Clean output regressions. Merged at `f26848d1` and installed by the normal guarded rebuild/restart above. Real VEX tools/devices and robot Download acceptance **UNRUN**; no radio-link or extension-host health claim. |
| VEX-UI | VEX view redesign ([plan](2026-10-15-vex-ui-redesign.md), [mockup](2026-10-15-vex-ui-mockup.html)) | **INSTALLED — VX-UI-1, 2, 3 and 6 ([#209](https://github.com/Johnwang688/jarvis/pull/209), [#229](https://github.com/Johnwang688/jarvis/pull/229)); VX-UI-4a ([#213](https://github.com/Johnwang688/jarvis/pull/213)); VX-UI-4b ([#240](https://github.com/Johnwang688/jarvis/pull/240)). Hardware checks still unrun.** See the VEX view redesign table below. The urgent Clean/Build fix ([#158](https://github.com/Johnwang688/jarvis/pull/158)) is installed. Does not authorize real robot operations. |
| PLATFORM | Physical Browser sound, real Windows mic/AEC, real account sign-in, VEX download | **VERIFY / owner-assisted.** Synthetic/unit/native-shell success must not be reported as these real-world acceptances. |

Starting points for another team: **GIT-LIVE** (needs a disposable repo and the
owner), the unbuilt Browser rows (the freeze is lifted), the B-PERF tile-size sweep (idle machine only), or the unclaimed
items in the voice follow-ups. SV-0 to SV-6 and QA-LOAD are merged. B-NATIVE, B-AUTH
and B-FIND are installed; their implementation reservations are complete. Choose other
unclaimed packages or coordinate a handoff with the named lead.

**Remaining:** voice follow-ups are listed in
[2026-10-15-voice-followups.md](2026-10-15-voice-followups.md). The boundary gap it named, `/stt` and `/say` without `owner_only`, is closed by [#227](https://github.com/Johnwang688/jarvis/pull/227) (installed).

## Next up (2026-10-19)

Added 2026-10-19, in this order:

1. **Chat-to-chat live gates L1-L4:** [runbook](2026-10-19-chat-to-chat-live-gates.md); only on the owner's go, on the live daemon. C6 only if L4 asks for it.
2. **Typed `/compact <focus>`** (CB-4), Claude first.
3. **Scrub alignment:** give a non-compaction turn's `turn_finished` reason `chat_reads.scrub`-strength scrubbing, and close `scrub_text`'s short-`Bearer` gap.
4. **Known failures on main:** `browser_drive_integration_check` has a stale fake (its session lacks `self_started`, its daemon lacks `chat_relay`); `ledger` and `devservers` also fail on main. `hud_install_check` fails only with `TMPDIR=/var/tmp` (environment, not a regression).

Still open from 2026-10-16, in this order:

1. **#230 test gaps (LOW/NIT, from its review):** pin that `tools.Refusal` is falsy; that `permissions` `_LAST_VERDICT` is per-context; the Browser-delegation ask marking `asked`; and a desk_only stale-turn test for `bridge._delegated`. Nothing is unsafe today.
2. **B-DIRECT-CAPTURE:** READY and unclaimed. Before a Direct capture path is enabled, it needs an actual Windows app-mode `getDisplayMedia`/CropTarget prompt and pixel evidence.
3. **B-PREVIEW:** blocked on a Chrome capability. Chrome 155 screencast metadata cannot prove matching-frame identity, so "Preview unavailable" stays. Re-checked 2026-10-20 with probes: still blocked ([findings](2026-10-19-browser-preview-capability.md)).
4. **B-PERF Chrome tile-size sweep:** run only when the machine is idle.
5. **VX-UI-4b:** built ([#240](https://github.com/Johnwang688/jarvis/pull/240)); hardware checks H1, H3 and H4 wait on the owner's next robot session.
6. **Voice follow-ups:** [2026-10-15-voice-followups.md](2026-10-15-voice-followups.md).
7. **Owner live checks:** [2026-10-16-owner-live-checks.md](2026-10-16-owner-live-checks.md). The pending checks for [#232](https://github.com/Johnwang688/jarvis/pull/232) (B-PICKERS), [#233](https://github.com/Johnwang688/jarvis/pull/233) and [#235](https://github.com/Johnwang688/jarvis/pull/235) (B-IME) and [#234](https://github.com/Johnwang688/jarvis/pull/234) (SV-6) are also in their rows above. The older owner-assisted items still stand: GIT-LIVE, B-LOGIN, the Quick review first press, and H1 to H10. The braking firmware decision is made (brake, keep the target heading); it is built as [Johnwang688/87867D-override#10](https://github.com/Johnwang688/87867D-override/pull/10), reviewed, and waits on the owner's robot test before it merges.

## Shared test, review and release contract

- WSL has about **22GB RAM** (since 2026-10-14; it was about 15 GB). **At most five heavy HUD/browser test
  jobs in total across all teams**, never five per team. The 4 GiB available floor still applies;
  drop back to three if WSL comes under memory pressure. Slot 1 is reserved for the Claude
  voice team; Codex slots 2 and 3 are released after completed testing and
  installation. Claim any future heavy job with the coordinating lead using
  the current registry and obtain a per-job grant before launch;
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
  [sound](2026-10-13-browser-sound.md), [voice](2026-10-13-voice-rework.md), [subagent view](2026-10-13-subagent-view.md),
  [VEX redesign](2026-10-15-vex-ui-redesign.md), [Codex git credentials](2026-10-15-codex-git-credentials.md),
  [Diff quick review](2026-10-15-diff-quick-review.md), [PR merge/close decisions](2026-10-15-pr-merge-close-decisions.md).
- [Current Browser notes](../notes/browser.md), [control-plane notes](../notes/control-plane.md),
  [voice notes](../notes/voice-face.md), [HUD API](../hud-api.md), [working guide](../../CLAUDE.md).
- Historical owner-local checkpoint: `/home/johnw/projects/Jarvis/docs/plans/2026-10-12-checkpoint.md`.
  Preserve it; this tracked map provides the portable team view.
