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

## Answers (2026-10-11 morning)

1. **Browser:** answered item by item. See `2026-10-10-hud-browser-decisions.md`. B-4 (accounts) and B-9 (agents may drive behind a classifier) change the plan, and an amendment is being written.
2. **BR-0:** "implement it as a small pr". It is being built.
3. **SELF_PROTECTED for runtime.py and toolgroups.py:** "you don't have to do". Not done.
4. **Fail closed when no tool list is bound:** "tool fails, go with your recommendation". Being built, with an opt-in for scripts.
5. **Prompts that name tools a surface lacks:** "fix it". Being built in the same follow-up PR as 4, along with cad-bench's `skill_read`.

Also answered: the daemon now **auto-starts with WSL**, as an enabled systemd user unit.

---

## 6. Signing in to Chrome itself, in the HUD Browser (BD-5, open)

You asked: "can i login to my chrome profiles as well in my google accounts". There are two kinds of sign-in:

- **Signing in to Google websites** (Gmail, YouTube, Drive, and so on) inside a Browser profile. **This works.** Each profile keeps its own logins, so "Main" can hold one Google account and "Work" another, both signed in at once. The one risk is that Google sometimes refuses sign-in from a Chrome a program is driving. Measurement M4 will show whether it does here.
- **Signing in to Chrome itself and turning on sync.** This is the "Turn on sync" button in your normal Chrome. It would copy your saved passwords, payment cards, history and bookmarks from your Google account into the Browser that Jarvis, Claude and Codex can now click and type in.

**Recommendation:** websites yes, Chrome sync no.
- The Browser is blocked from saving passwords and filling in cards. That way an agent can never pull one out of autofill, even by accident.
- This is enforced with a Chrome policy file, installed with one more `sudo` command when that part is built.

Reply "ok 6" to keep sync off. If you do want Chrome-level sign-in in the Browser, say so, and passwords will need to come from somewhere else.

**Answer to 6 (2026-10-11):** "chrome sync should be on. keep the sign ins. it's ok for the agents to click and type in stuff with autofill since I'm watching them do it in the HUD. if they try to do something dangerous #1 the classifier will most likely catch it, and #2 if the classifier fails to catch something dangerous for any reason I'm literally right there to stop it". Recorded as BD-5 = on in amendment 1 §C and in the decisions file. The password-store cost (no keyring in this WSL) is stated there.
