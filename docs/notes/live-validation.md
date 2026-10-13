# Live Validation — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 9624–9706

<!-- claude-source:009624-009706:begin -->
### PENDING LIVE VALIDATION — needs API keys (delete this section once done)

Everything above was built and tested in a sandbox with **no `OPENROUTER_API_KEY`**,
so every check is synthetic. The free suites all pass (`context_check`,
`longhorizon_check`, plus `secrets`, `permissions`, `skills`, `workflows`,
`gmail`, `onshape`, `face/approval_check`, `face/controls_check`). What a
human with keys still has to confirm — **and this list should be deleted from
CLAUDE.md once it has been, with the results folded into the notes above:**

1. **Playwright suites never ran here** — not installed in the sandbox. Run
   `tests/browser/policy_check.py`, `tests/browser/thread_check.py`, and the
   `tests/face/` HUD suites (`hud_state_check`, `hud_approval_check`,
   `whiteboard_check`, `attach_check`, `hud_input_check`). They are free; they
   just need the browser. Nothing in this change touches them, so a failure
   means a genuine regression.
2. **Does the model actually use `plan_write`?** The mechanism is tested; the
   *prompting* is not. Give Luna a genuinely long task (a multi-file refactor,
   a 10-part research job) via `jarvis chat` and watch whether it writes a plan
   unprompted and keeps it current, or ignores the tool. If it ignores it, the
   system-prompt paragraph in `config.py` is what needs work, not the tool.
3. **Does `run_subagent` earn its cost?** Same task twice, once with the tool
   available and once without, comparing total `$` and whether the answer holds
   up. The bet is that isolation pays for the extra model call; that bet is
   unverified. Watch for the failure mode where the model delegates something
   it should have done itself and pays twice for a worse answer.
4. **A real run past the compaction threshold.** Nothing has yet exercised
   compaction against a live model — the orphan fix is proven synthetically
   only. A long browser or CAD session (parallel `cad_render` calls are the
   exact shape that triggered the bug) crossing 90k tokens would confirm no
   400s and that the pinned goal reads sensibly after a summary lands.
4b. **Does the model actually use `edit_file` and `grep_files`?** (Added
   2026-08-09.) Both are mechanically tested; the *prompting* is not. Watch a
   real self-improve or CAD session for three things: whether it reaches for
   `edit_file` instead of rewriting whole files, whether it pages past the
   first `read_file` footer on CLAUDE.md rather than stopping there, and
   whether it starts a search with `mode='files'`. If it ignores them, the
   system-prompt paragraph in `config.py` needs work, not the tools. Also
   worth measuring on agent-bench: `project` is the task these should move,
   and the baseline to beat is Luna's 100% at $0.0065 / 115s — the interesting
   number is **cost**, not score.
5. **Stale plans across turns.** The plan persists for the life of the agent,
   which is the point for long work but means a finished checklist can linger
   in the face's persistent agent into an unrelated conversation. The model can
   clear it with `plan_write("")`. Check in live use whether it does, or
   whether the plan needs to expire.
6. **Does the model actually delegate to `task_start`?** (Added 2026-08-20.)
   The mechanism is tested; the *prompting* is not — the same bet as
   `plan_write`'s. Run `jarvis face`, ask for a real job ("refactor X", "write
   me a report on Y") and keep talking: does he start a task and stay
   conversational, or grind through it inline? Does an approval card raised by
   the task carry its label, and does he read `task_log` before summarizing
   results instead of inventing them? And the inverse failure: does he
   delegate two-step jobs that were cheaper inline? If the judgment is off,
   the delegation paragraph in `config.py` is what needs work, not the tools.

7. **Spotify, end to end.** (Added 2026-08-21.) Everything is synthetic —
   there is no account connected here. Run `jarvis auth spotify`, then check
   the three things a fake transport cannot: that the **consent redirect
   actually lands** on 127.0.0.1:8406 (a redirect URI that differs by one
   character fails at the *token exchange*, not at consent, so the browser says
   success and the CLI says no refresh token); that a **refresh an hour later
   still works**, which is the rotation path; and that `spotify_play("my <x>
   playlist")` picks the owner's playlist rather than a stranger's. Then the
   judgement question, the same one `plan_write` and `task_start` have open:
   with only `spotify_play`/`spotify_status` visible, does the model call
   `load_tools('spotify')` when it needs to pause or queue, or does it flounder
   with the two it has? If it flounders, the pointer wording in
   `Agent._groups_block` is what needs work, not the mechanism.

8. **A real turn on a switched model.** (Added 2026-08-22.) Every model check
   is synthetic — the roster, the catalog and the routes are all exercised
   against a fixture. What a fixture cannot answer: pick a non-default model in
   the HUD, run a real turn, and confirm the tool calls actually land (the
   eligibility filter says the model *advertises* `tools`, which is not the
   same as being good at them — the 2026-07-30 bench found tool-calling quality
   varies by provider, not just by model), that the SYSTEMS CORE row and the
   cost readout agree with what was billed, and that a sub-agent spawned during
   that turn ran on the same model rather than the configured default.

Later: real integrations (calendar/email), scheduled proactive runs, and
more registered desktop apps as they earn their place (each is one entry in
`config.DESKTOP_APPS`, plus a backend choice — `uia` for native/UWP, `msaa`
for anything Chromium-based).
<!-- claude-source:009624-009706:end -->
