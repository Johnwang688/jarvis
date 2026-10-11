# Questions for the owner (written overnight, 2026-10-10 → 11)

Work carried on overnight under the merge rules you've already given. Everything below needs your answer. Each item has a recommendation; if one looks right, reply with "ok N" (or "ok all") and I'll go with it.

Nothing here is blocking the work already under way. Items 1 and 2 decide what gets built next.

---

## 1. The built-in Browser (plan: `docs/plans/2026-10-10-hud-browser-plan.md`)

The planner recommends one engine: a Chrome that Jarvis's daemon runs, streamed into the pane, with your clicks and keys passed through. It is the only option where an approval card always stays on top and no key can reach the page underneath it. Local dev servers keep today's direct, no-lag frame.

The costs:
- an estimated 30–100 ms of lag;
- choppy video;
- no sound in the pane;
- DRM video (Netflix) probably won't play.

| # | Decision | Recommendation |
|---|---|---|
| B-1 | Engine | Daemon-owned Chrome streamed into the pane. The iframe stays as "Direct" mode for local dev servers |
| B-2 | Local dev URLs | Open Direct as today, with a per-pane switch to the streamed mode |
| B-3 | Which Chrome | Google Chrome stable, installed with apt in WSL (security updates, a working sandbox). **This needs you to run one `sudo apt install` command**, which I'd give you |
| B-4 | Stay logged in across restarts? | Yes, in a private profile that is never your real Chrome's, with a "Sign out of everything" button and no saved passwords. Any program running as you could read those cookies, so keep banking and your main email in your own Chrome |
| B-5 | LAN addresses (router, NAS) | Off by default, with a Settings switch |
| B-6 | Downloads | A private folder, plus "Save a copy" that hands the file to your normal Chrome downloads |
| B-7 | "Jarvis can see" default | On for local dev pages. For every other site, a tick per pane that you set, cleared when the page goes to another site |
| B-8 | Tool name | `hud_browser_view` |
| B-9 | May Jarvis, Claude or Codex **click or type** in your browser? | **No.** It holds your logins. They can look (with the rules above), and you can annotate and "Send to chat" |
| B-10 | What Send to chat attaches | The marked-up screenshot, the URL, the title and the text visible on screen. Whole-page text is an unticked option |
| B-11 | Terminal links | Ctrl+click on any http(s) link offers "Open in Browser" first |
| B-12 | Sound | Off at first. Turned on if a test shows headless Chrome can play through WSLg |
| B-13 | Tabs | Tabs inside each pane, pop-ups open as tabs, 12 tabs total |
| B-14 | When "Preview" is renamed "Browser" | After the split/close-pane work (G2) lands, just before the streamed mode |
| B-16 | Fonts | Use your Windows fonts, if they don't slow start-up much |

**Merging:** may Browser PRs merge once their independent review passes, like the HUD and editor work? Recommended: yes.

---

## 2. A security fix found while planning the Browser (BR-0)

**What's wrong.**
- v1's agent browser tool runs without asking you. It refuses Jarvis's own HUD on port 8402, but not on 8405, where the v2 daemon serves the same HUD page.
- The "approve" route on 8405 only checks that the page asking came from the same address. A page loaded from 8405 itself passes that check.
- Together: a v1 surface (`jarvis chat`, the old face, the v1 Discord bot) running alongside the v2 daemon could open the HUD on 8405 and approve a pending card.

**The fix (small):**
- v1's browser and `fetch_page` refuse all of Jarvis's control ports (8402, 8404, 8405, 8406);
- the approve route accepts only requests from the HUD window itself.

I'd like to build it overnight. **May I merge it once its review passes?** Recommended: yes.

---

## 3. Protecting two more files from Jarvis's own edits (from the #34 review)

PR #34 makes each agent able to run only the tools it was given. The reviewer's recommendation:
- Add `jarvis/runtime.py` (and `jarvis/tools/toolgroups.py`) to the files Jarvis can't edit himself.
  - runtime.py has had about 5 commits ever, so self-improve loses almost nothing.
  - It already holds the approval hook that sub-agents and tasks inherit.
- Leave `jarvis/agent.py` editable. It's the loop you're building to learn from.

Recommended: yes to runtime.py and toolgroups.py, no to agent.py.

---

## 4. Should a missing tool list fail closed? (follow-up to #34)

Today, if code calls a tool with no agent's tool list in place, the call is allowed. That only happens in scripts and tests, never in a real Jarvis surface.

The reviewer recommends refusing instead, with an explicit opt-in for scripts. A future bug would then fail loudly instead of silently opening up. About 17 test suites would need one line each. Recommended: yes, as a small follow-up PR.

---

## 5. Prompts that name tools some surfaces don't have (from the #34 review)

The system prompt mentions tools that some surfaces don't hold:
- the fast path: `task_start`, `workflow_start`, `plan_write`, `run_subagent`, `write_file`;
- tasks, goals and workflows: `task_start`.

Before #34, the non-dangerous ones would quietly run anyway. Now the model gets a refusal and spends a step.

Also, cad-bench doesn't hold `skill_read`, so the CAD skill it was meant to measure can't load.

Recommended: a follow-up that gives each surface a prompt naming only the tools it holds, and adds `skill_read` to cad-bench. Bench numbers taken before #34 may differ from numbers taken after, because some "no-shell" or "no-web" bench modes were only advisory until now.

---

_Answers so far: none yet._
