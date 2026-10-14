# Browser driving release: the fast path (BR-9b/9c to production)

**Status (2026-10-13):** B-DR is implemented, independently reviewed and ready for coordinated merge/install in [PR #135](https://github.com/Johnwang688/jarvis/pull/135). Tested integration `22ab268` includes installed Browser controls/classifier and the Claude team's V0/V2/V6 claim; production content is identical to reviewed `76177f39`. This records candidate verification, not installation or owner-account validation. Native Claude/Codex driving remains the separate B-NATIVE handoff in `2026-10-13-browser-driving-claude-codex.md`.

Named Remote panes default to driving on, subject to the global switch, see permission and existing action guards. Private requires a trusted owner grant bound to the exact pane/context and never persists it; Direct cannot drive. Existing malformed authority fails closed. Trusted activation and pending-operation identity checks are implemented.

Verification: build, 1,870 HUD unit tests, 43 owner Browser checks, 19 real-Chrome execute tests, six scan tests, 33 full view tests, independent authority probes and five mutants passed. The immutable integrated HUD run had **8 failures of 3,134 checks**: moved-options fixture calls and one passive empty-front audit. Independently reviewed fixture corrections then passed **215/215 complete affected checks**, including all 49 capture assertions; unchanged unaffected sections come from the full run. These are two receipts, not one all-green full invocation. Earlier AO3 failures (19/3,195) and their 95/95 corrected rerun are retained.

The backend sweep invoked all 115 suites with individual real exit codes: 108 initially passed and seven failed. Corrected full reruns passed Codex 88, installer 39, owner-browser **30 run / one private-netns skip**, terminal 90 and view 33. Fake Pocket checks and four non-synthesis local voice groups passed with synthetic metadata; real local synthesis is **UNRUN** in B-DR because cached model paths were not configured in its initial isolated run. The original view RSS failure at 1,558.4 MiB is retained; all six bounded baseline/candidate samples and the final full 33-test rerun passed the unchanged 1.5 GiB guard. The composing audit passed 29 negative controls, including actual permission/model/save routes. No paid model calls were made for B-DR; M11 was completed separately by B-CLASSIFIER (#130), as recorded in the [current feature map](2026-10-13-current-feature-map.md).

**Historical planning material:** the observations and questions below record the original docs-only baseline at `d4b65dd` and the approved design. They are not current code or installation status.

## For the owner

- **What it does.** "Agents may drive" becomes a real switch, and it is on. Jarvis's fast path can then click, type and scroll in a visible Remote Browser pane, on a turn you typed at the HUD.
- **What stays the same.** Every check the backend already runs stays as it is:
  - the hard refusals and the classifier;
  - your private card for anything sensitive;
  - the 5-second hands-off rule;
  - takeover, Stop and Resume;
  - the budgets.
- **What still doesn't work.** Cards still say **Preview unavailable**. Chrome 155 can't prove which frame a screenshot came from, so the card shows no crop and no target outline.
- **How to undo it.** Turn the switch off in Settings → Browser and driving stops at the next input event. In code, the revert is one function.
- **What I need from you.** Four answers, at the end. The main one is whether a new pane on a named profile starts with "Jarvis can drive" on.

## Decisions recorded

- **DR-0. Owner, 2026-10-13: turn "Agents may drive" on.** Production driving is enabled.
  - This reverses the global OFF decision in the 2026-10-12 checkpoint (`docs/plans/2026-10-12-checkpoint.md`, which is not in this repository).
  - That checkpoint is why `_shipping_enabled()` returns False unconditionally (`jarvis/v2/browser/drive.py:33-36`: "Checkpoint overrides BR9c's older proposed default"). It is also why CLAUDE.md says "Driving remains OFF."
- **DR-1. This satisfies "do not loosen without asking".** CLAUDE.md's Safety design section allows its rules to be loosened only when the owner is asked. This explicit owner decision is that asking.
  - Its scope is the enable seam and the two switches only.
  - No guard, budget, floor or card rule is weakened.
  - Any further loosening, such as a smaller quiet window, Always on Browser cards, or Direct-mode driving, needs its own owner decision.
- **DR-2. Unchanged amendment-1 decisions.**
  - BD-1: fast path, desk turns only.
  - BD-2: Browser cards are HUD-only.
  - BD-3: Private is off unless the owner switches it on for that pane.
  - BD-4: the `browser_review` tier. It now defaults through the review tier to `anthropic/claude-haiku-5.5` (`jarvis/config.py:928`), not Luna.
  - BD-6: uploads always ask.
  - BD-9: no driving under the strict profile.
  - BD-11: refs only, no coordinate clicks.

## Historical code baseline (verified at d4b65dd)

- **The backend.**
  - `BrowserDrive(daemon, enabled=_shipping_enabled)` (`drive.py:93`) calls `enabled()` in `_turn` (line 102), in `_current` before every guarded step and event (line 161), and in `publish_look` (line 178).
  - Refs are therefore never published while the backend is off.
  - `act_for_tool` refuses with `OFF` if nothing is installed (line 337).
- **The pane registry refuses `drive: true`.**
  - The front report refuses it with 409 "Agents may drive is off" (`panes.py:296-297`).
  - `PATCH /browser/panes/<id>` refuses it the same way (`panes.py:405-406`).
  - Receipts hard-code `"drive": False` (`panes.py:394`).
  - So `PaneSnapshot.drive` is always False, and `_binding` (`drive.py:131`) refuses.
- **The rules expect both switches.**
  - `act_rules.pre_judge` needs `agents_may_drive` and `pane_can_drive` (`act_rules.py:836-842`).
  - `drive._facts` passes both as True after `_binding` has checked them (`drive.py:183-194`).
- **The HUD controls are inert.**
  - `BrowserSettings.tsx:32` and `BrowserProfile.tsx:29` render disabled, unchecked checkboxes.
  - `browserowner.ts` types `drive: false`.
  - `workspace.ts` has no `drive` field. It has only `see?: false` (line 188).
- **The `browser.json` defaults** are `{lan: false, local_servers: false, auto_open: true}` (`settings.py:62`). There is no drive key.
- **The prompts say "no browser".**
  - The fast path's docstring says the action exception is "unconditionally OFF in production" (`fastpath.py:12-13`).
  - `FAST_PROMPT` says "You have no tool that … drives a browser" and tells the model to propose a task for "a browser" (`fastpath.py:85-90`).
  - The tool's own docstring, which is model-facing, says "Production Agents may drive is off" (`jarvis/v2/tools/hud_browser_act.py:18`).

## Design

**1. The enable seam stays one function.** `_shipping_enabled(daemon)` returns `daemon.browser_settings.value("agents_may_drive") is True`, and `BrowserDrive` binds it.

- No environment variable, CLI flag or profile setting reaches it.
- A test greps for that, as BR-9b's notes demand.
- Because `enabled()` is already read before every event, turning the switch off stops an in-flight `type` at the next chunk.
- That stop has BR-9b's truthful partial-input report.

**2. The setting is `browser.json` → `agents_may_drive`.**

- It is protected state. Only `PATCH /browser/settings` changes it, which is owner-only with a strict loopback Host. `settings.py` stays the one reader and writer.
- The default is True, per the owner's decision. Unlike the other keys, it **fails closed**: a file that exists but is unreadable, a symlink, oversized or not JSON, or a non-boolean value, reads as **off**.
- A missing file or missing key reads as the default.
- The HUD Settings checkbox becomes live, using the same pattern as `auto_open`.

**3. The pane switch.** The registry stores the owner's per-pane preference. The effective `PaneSnapshot.drive` is computed as:

> preference ∧ `see` ∧ the global switch ∧ (not Private, or Private with this pane's opt-in)

- A report claiming `drive: true` while the global switch is off is not a 409. It is stored as the preference and reported back as effective False, so flipping the global switch never breaks the HUD's report loop.
- Non-boolean values stay 400.
- Turning see off still forces drive off (amendment 1 §E).
- Navigation never turns drive on.
- `_receipt` returns the effective value.

**4. Defaults** (the docs are silent on most of this; see Q1 to Q3):

| Where | Default | Source |
|---|---|---|
| Global "Agents may drive" | **on** | owner DR-0 |
| A pane on a named profile | **on**, stored only when off (`drive?: false`, like `see`) | recommendation Q1. Amendment 1's owner summary names only Private as needing a per-pane switch, but §E's "a tab created without these values is … drive-off" is the fail-closed wire default |
| A Private pane | **off**. The opt-in lasts while that pane's Private session lives, and is never stored | BD-3, H5 |
| A missing or malformed value on the wire | off | amendment 1 §E |
| Direct mode | not drivable (H6) | unchanged |

**5. The HUD.**

- `workspace.ts` gains `drive?: false` plus `setBrowserDrive`. A migration keeps old specs valid.
- `browserowner.ts` reports a boolean.
- `BrowserProfile.tsx`'s "Jarvis can drive" becomes live. It is disabled, with a reason in its title, when the global switch is off, see is off, or a card or annotation blocks input.
- `state/browser.ts` already sends `drive:false` with see-off; that is kept.

**6. The fast-path wording.** `FAST_PROMPT` gains BD-1's desk-only driving paragraph, and it needs a `config.PROMPT_TOOL_PARAGRAPHS` rule (CLAUDE.md invariant 7). `tests/prompt_tools_check.py` must name it.

- **The catch.** `_turn_tools` (`fastpath.py:437-445`) swaps only `tool_specs` per turn. The prompt is narrowed once, at `Agent` construction, from the brief's full names.
- **So today a static paragraph would also reach off-desk turns,** which do not hold the tool.
- **Recommended fix.** Re-narrow the prompt in `_turn_tools` whenever the desk toolset changes. The tool specs already change at that moment, so the prefix-cache miss is already being paid.
- **Fallback.** Keep the guidance in the tool's own description only, and make the "no tool that … drives a browser" sentence true in both states.
- **Test.** An off-desk turn's request names no tool it does not hold.

The fast path's docstring and the tool docstring drop "unconditionally OFF". They state the switch, desk and pane rules instead.

**7. Self-protected files.**

- `drive.py`, `hud_browser_act.py`, `act_rules.py` and `act_review.py` are SELF_PROTECTED (browser notes, BR9b).
- The implementing agent's writes to them are refused or asked. The owner applies or approves those hunks, as with PR #122.
- `settings.py`, `panes.py` and the HUD files are ordinary.

## Safety checks that stay byte-for-byte

These are from the BR9b and BR9c notes; none of them is touched.

- **Who may call, and when.**
  - Explicit-only and serial: not in `PARALLEL_SAFE`; in `EXPLICIT_ONLY`, `PRIVATE_RESULT`, `DESK_TOOLS` and `DRIVE_TOOLS`.
  - `FORBIDDEN_PREFIXES` is unchanged.
  - Live desk and depth 0; non-strict; uncancelled; the same turn. A private turn scope binds the exact fast caller.
- **Which pane.** A drawn, uncovered Remote binding with see+drive, readable terminal provenance, and current refs from a presented Remote look only.
- **The owner's hold.**
  - An attached visible owner socket.
  - Not held, paused or depressed.
  - **5 s with no owner input** (`OWNER_QUIET_S`).
  - No pending card, including between every CDP event.
- **Budgets.** 40 judged actions per turn and 200 actions per tab per rolling hour. One driver per tab.
- **Order of judgement.** H1 to H12 refusals come before the model. ASK floors cannot be lowered. `JARVIS_BROWSER_REVIEW_AUTOALLOW=0` still makes the reviewer refuse-only.
- **Cards.** HUD-only private cards: 120 s, no Always, ALLOW only from the authenticated owner route, a fixed public projection, Discord skips them.
- **Network.** No local-port lease from agent navigation (amendment 2, L2). The shared gate and the landing guard are unchanged.
- **After an action.** Post-input reconciliation pins page, pane, profile and permission identity.
- **Files.**
  - Exact-byte upload binding with a 50 MiB cap.
  - Agent downloads held behind a hold marker, with the executable release card.
  - Taint until the tab closes.
- **Records.** Fixed bus, transcript and audit summaries (`browser_acted {tab_id, action_id, at, outcome}`). The eight-step ending says "go on" and proposes no task.
- **Presentation (BR9c).** The banner and list come from the ticketed socket only. Takeover, Stop and Resume (paused until confirmed). **Preview unavailable / no outline** stays.

## CLAUDE.md and notes changes (in the implementation PR)

- **CLAUDE.md, "Working on v2".** "Driving remains OFF." becomes:

  > Driving is on behind the owner's switch (2026-10-13): fast path on desk turns; per-pane and Private rules in the Browser notes.

  Keep the guide near its size.
- **`docs/notes/browser.md`, the "BR9b dormant Browser action backend" paragraph 1.** Replace "Production `_shipping_enabled()` unconditionally returns false … The checkpoint's global OFF decision wins" with the switch semantics and the owner decision. Retitle the section "BR9b Browser action backend".
- **The BR9c paragraph 1** ("Production agent driving remains OFF, with both Settings and pane switches disabled") and **the BR3b UI paragraph** ("Agents may drive stays disabled") are updated.
- **`docs/hud-api.md`.** The `/browser/settings` keys and the pane `drive` semantics.
- **The docstrings** of `drive.py`, `drive_state.py`, `panes.after_navigation` and `fastpath.py`.

## Packages and order

1. **DR-1, backend.** The seam, the setting, the registry, the prompt paragraph and its rule, plus the tests. The SELF_PROTECTED hunks are owner-applied.
2. **DR-2, HUD.** The two checkboxes, PaneSpec `drive`, the report and the tests.
3. **One PR.** Independent review with mutation evidence, then the full sweep under `systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0`, then the live validation below, then merge.
4. The report states separately whether the result is committed, merged, built, installed and live-validated.

## Free tests

**Update:**

- `tests/v2/browser_drive_integration_check.py:35`. Off only while the switch is off; with it on, the default path reaches the gate.
- `tests/v2/browser_panes_check.py:250-261`.
  - Drive is accepted with the global switch on.
  - Effective drive is False with the global switch off, see off, or Private without opt-in.
  - Non-booleans are 400.
- `tests/face/hud_v2_drive_check.py:78,98`, `tests/face/hud_v2_remote_check.py:56` and `hud/src/lib/browserowner.test.ts:82`.
  - The live switches change state only through owner routes.
  - See-off still disables drive.

**Add (each with the mutation it must catch):**

| Check | Mutation |
|---|---|
| `enabled` reads only `BrowserSettings`; grep finds no env or other route into `_shipping_enabled` | env flag added |
| A malformed, symlinked or oversized `browser.json` or a non-bool value → driving off; missing → on | malformed reads as default True |
| A switch flipped off mid-`type` stops before the next chunk and reports the partial count | enabled read once per act |
| `publish_look` publishes no refs while off | publish ignores enabled |
| A Private pane is refused by default and allowed after its opt-in; a new Private session is off again | Private opt-in persisted |
| Navigation never sets drive; see-off clears it | see-off leaves drive |
| `browser.json` writes are refused to `write_file` and to v2 layer 1 | not protected |
| The fast-path prompt paragraph is absent on an off-desk turn (prompt re-narrowed with the per-turn toolset); `prompt_tools_check` names it | paragraph without rule; prompt narrowed only at construction |
| An off-desk, strict or depth>0 call is refused with the switch on | desk check removed |

The existing BR-9b and BR-9c suites run unchanged; they already inject `enabled=lambda: True`.

## Original live-validation plan (historical; current evidence above)

Use the owner's real HUD and real Chrome 155 on a fixture or harmless public page. No account side effects; the owner supplies the sites.

1. **Switches.** Settings on/off; pane on/off; Private off then opted in; see-off forces drive-off.
2. **An ordinary action.** "Jarvis, scroll pane 2 down" and "click the Docs link". These need no card, and they show the banner and the action list.
3. **A card.** A login or form submit raises the private card. Check:
   - Deny, timeout (120 s) and Authorize (pointer only);
   - no Always;
   - **Preview unavailable** and no outline;
   - nothing on Discord or `/events` beyond the redacted projection.
4. **Takeover.** Owner input mid-action pauses before delivery. The next act is refused until **Resume Jarvis**. Resume is inert under a card.
5. **Stop** from the banner. **5 s quiet**: an act within 5 s of owner input is refused.
6. **An executable download** is held; the release card works; deny deletes.
7. **Settings off mid-turn** stops the next event.
8. **M11.** A paid classifier latency and cost probe, about 50 judged actions. It was never recorded; amendment 1 §I asks for it. Report p50, p95 and dollars.

## Rollback

1. **Owner, immediate.** Settings → Browser → Agents may drive off. The next event refuses. Per-pane switches give finer control.
2. **Code.** Restore `_shipping_enabled` to `return False`, a one-line owner-applied revert. Optionally restore the panes 409. All BR9b/9c guards remain, so the revert returns exactly to today's dormant state.
3. **Ending a run.** Daemon restart forgets all drive state, grants and leases (memory only).

## Historical planning questions (implementation defaults recorded above)

1. **A new pane on a named profile: "Jarvis can drive" on or off by default?** Recommend **on**. You decided driving should be on. Every action still needs a desk turn, a visible uncovered pane, 5 s hands-off and a card for sensitive categories, and Private stays off.
2. **Private opt-in lifetime.** Recommend keeping BD-3: per pane, until that pane's Private session ends, never stored.
3. **A broken `browser.json`.** Recommend that a missing file means on (your default) but an unreadable or malformed one means off, so a damaged safety file never enables driving.
4. **Spend about one dollar or less on the M11 classifier probe before calling it shipped?** Recommend **yes**.
