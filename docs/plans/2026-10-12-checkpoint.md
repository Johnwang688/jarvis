# Work checkpoint (saved 2026-10-12 ~22:30 CDT, for Wednesday 2026-10-14)

Work pauses for usage limits until **Wednesday, 2026-10-14**. This file is the plan for that day. It records what is
live, what is half-built and where it is saved, what waits on the owner, and the queue in order. A fresh lead session
starts at section 5 ("Wednesday: start here").

The running log for the day is `docs/plans/2026-10-12-questions-for-owner.md`. Both files are **untracked** in the
main checkout, so a `git add -A` or a clean must not sweep them away. The memory note `work-checkpoint-2026-10-12`
points here.

## 1. What is live

**Before tonight's deploy**, the daemon served 8f74e81, everything through #83:
- #57, Claude's first reply;
- #62, chat titles;
- #63, the cost fix;
- #64, terminal continue;
- #65, the git submodule fix;
- #67, the Diff showing the project's changes;
- #68, the Editor and Browser names;
- #78, the Browser focus guard;
- #79, the dev-server daemon half;
- #80, the narrow `.env` scrub;
- #36, the thinking line;
- #82, the boot animation;
- #83, Install as app.

**Tonight's deploy (~22:30):** main **a2e1103**, adding:
- #75, command-gate follow-ups;
- #84, TC-1, the daemon half of tool calls in the transcript;
- #85, VEX build progress, from the owner's other session.

Deployed; the log is `~/.cache/jarvis-lead/deploy-now.log` and the result is in section 1a.

### 1a. Deploy result

**Deployed at ~22:30 at the owner's request** ("can you deploy now?"), forced over one running turn.
- **What was cut off:** a Codex task worker on the schoolwork report, thread 2531b2e0. Check that task on Wednesday;
  it may need resuming.
- **What is live:**
  - HUD build `20261013T033040287457Z-d7f77ae7123e`, from **a2e1103**, `installed`;
  - Claude, Codex and the fast path all ok (the fast path on `anthropic/claude-haiku-5.5`).
- **For the owner:** press Ctrl+R.

The section 5 step "check that tonight's deploy took" is done.

## 2. Saved half-built work (nothing is lost)

| What | Where it is saved | State |
|---|---|---|
| **#86**: Haiku 5.5 default, compaction at 90k, Claude prompt caching | PR [#86](https://github.com/Johnwang688/jarvis/pull/86), branch `feat/haiku-default` at **759b8d4** (pushed) | Two fix rounds done; one more fix, then review, sweep and merge (section 4, item 2). |
| **Git for Codex chats** | Branch `feat/codex-git-write` at **3da847a** (pushed); lead worktree `~/.cache/jarvis-lead/wt/cgit` | Helpers only, not wired. Waits on the owner (section 3). |
| **One-line chat titles that scroll on hover** | `~/.cache/jarvis-lead/title-patch/title-hover.patch` (copied out of `/tmp`; applies cleanly to a2e1103) | Built by a Codex chat (thread 8ae271c8). Unit tests and build passed there. Its browser suite never ran, because the Codex sandbox has no network. |
| Fix briefs and reports | `~/.cache/jarvis-lead/haiku-fix/` (`brief.md`, `brief2.md`, `report*-full.md`) | For reference. |
| Live probe scripts | `~/.cache/jarvis-lead/probe/` (`haiku_probe*.py`, `git_permit_probe.py`) | Re-runnable. A probe costs cents. |

## 3. Git for real work in Claude and Codex chats (the owner's ask, 2026-10-12 ~22:20)

The owner: *"enable both claude and codex sessions to write to git with the same permissions that claude code
normally has … adding git capabilities for real work and worktrees to claude and codex sessions in jarvis."*

**First thing on Wednesday**, unless the owner says otherwise.

### What was measured

- **Jarvis's own permission check already allows the git flow** for both providers in an AUTO chat:
  - allowed: `git worktree add`, `git add`, `git commit`, `git push -u origin <branch>`, `gh pr create`;
  - asked of the owner: a push to main and a force push.

  `git reset --hard`, `merge` and `rebase` are also allowed. The probe ran in-process with a temp HOME and nothing ran
  (`probe/git_permit_probe.py`).
- **Claude chats:** they run outside any sandbox, behind Jarvis's hook, so they can already do it. The recent thread
  logs show every git and `gh` request allowed.
- **Codex chats can't.** Codex's own `workspace-write` sandbox mounts every `.git` read-only.
  - Measured with `codex sandbox`: `git worktree add`, `git commit` and `git branch` fail with "Read-only file system".
    Ordinary project files stay writable.
  - Thread 8ae271c8 hit exactly this, and Codex never asked for more access.
  - The same sandbox has networking off. That's why it couldn't run the HUD's browser suites (no loopback).
- **The fix, measured to work in a throwaway repo:**
  - Each Codex chat's private `config.toml` gets
    `[sandbox_workspace_write] writable_roots = [<the repo's .git and its common dir>, <a chat worktrees folder>]`.
  - With that: commit, `worktree add` into the folder, and commit inside the new worktree all work.
  - `network_access = true` would also let git reach GitHub, **but** Codex could then push (to main, or with force)
    inside its sandbox, and Jarvis would never see it.
  - **So the network stays off.** `git push` and `gh` have to ask to leave the sandbox, and Jarvis's gate judges them.

### Built so far (branch `feat/codex-git-write`, 3da847a)

- `config.CHAT_WORKTREES_DIR`: `~/.local/share/jarvis/chat-worktrees` (env `JARVIS_CHAT_WORKTREES`).
- `codex_config.git_dirs(cwd)`: walks up to the repository's git dir, plus the common dir for a linked worktree.
  - It reads the `.git` and `commondir` files capped, as regular files only.
  - It refuses a symlinked `.git`, or a git dir inside a credential folder or Jarvis's data.
- `codex_config.git_roots(brief)`: for chat briefs only (task workers get none).
- `codex_config.git_note(brief)`: the sentence telling Codex where worktrees go and that push/`gh` must escalate.

### What blocked it tonight

Claude Code's auto-mode safety check refused the wiring edit as "Self-Modification", because it changes what an agent's
sandbox may write. The lead did not work around it. **The owner decides** by either:
- adding a Claude Code permission rule that allows the edit (or approving it in-session); or
- making the edit by hand. In `jarvis/v2/providers/codex_config.py`, `config_text`, right after the `trust_level` line:

  ```python
      roots = git_roots(brief)
      if roots:
          lines += ["[sandbox_workspace_write]", f"writable_roots = {_value(roots)}"]
  ```

### To finish on Wednesday

1. **The wiring** (above).
2. **Tell Codex.** In `codex.py`'s thread/start, `developerInstructions` becomes `brief.system_append` plus
   `git_note(brief)` for chats. Update the test that pins `developerInstructions`.
3. **Tests in `tests/v2/codex_provider_check.py`:**
   - a chat in a repo gets `[sandbox_workspace_write]` with its `.git` and the worktrees folder;
   - a linked worktree adds the common dir;
   - a chat not in a repo gets the worktrees folder only;
   - a git dir in a credential folder is left out;
   - a symlinked `.git` adds nothing;
   - a task brief gets no table;
   - the note appears in `developerInstructions` for chats only.

   Point `config.CHAT_WORKTREES_DIR` at a temp folder in `setUp`.
4. **Create the worktrees folder** (0700) before a session opens, and check whether Codex needs a writable root to
   exist.
5. **Review it with Opus**, as a sandbox boundary.
   - The owner accepts the risk: with `.git` writable, an agent could plant a hook or config that runs on the owner's
     next git command in that repo. Claude chats can do the same today.
   - What else must hold: no network inside the sandbox, chats only, and no credential folders.
6. **Sweep, merge, deploy with `--restart`, then a live test in a Codex chat:**
   - worktree → edit → commit → `git push` → `gh pr create`;
   - see how the escalated push arrives. Is it a plain command approval that Jarvis's rules judge (allowed for a branch,
     asked for main), or a sandbox widening that asks the owner every time? Record which.
7. **For both providers:**
   - Tell chats that the main checkout is shared, so real work goes in a worktree (in the chat brief's
     `system_append`, or the project's instructions).
   - Do a live end-to-end run in a Claude chat too.
8. **Recorded alternative**, if the owner prefers it: offer Jarvis's hardened `git`, `git_worktree` and
   `git_pull_request` tools to chats through jarvis-mcp (`session_tools.py` is empty today). The daemon does the git
   work, and `.git` stays read-only to Codex.
9. **Then the title patch:**
   - apply `title-hover.patch` in a lead worktree;
   - run the full HUD suites (`hud_v2_titles_check` included);
   - review, merge, then deploy with a reload.

## 4. The queue for Wednesday, in order

1. **Git for real work in Claude and Codex chats** (section 3).
2. **Finish #86** (`feat/haiku-default`, 759b8d4).
   - **Done in two Codex rounds, each probed live:**
     - explicit cache breakpoints on text only;
     - a middle breakpoint before context management's windows;
     - one-shot calls uncached;
     - compaction at 90k (the owner's decision is pending, below), with the env value validated and empty meaning unset;
     - docs.
   - **Live results:**
     - A short turn reads about 19.2k of 20k from cache from step 2, about 7x cheaper per step.
     - Images pass with no error (an attached image, and image results).
   - **Still open:**
     - In a long turn with images, the middle point is held back by the image window (`keep_images` = 2). From step 6,
       only 2–6k of 15–19k tokens are read and about 12k are rewritten each step, roughly break-even with no caching
       (`probe/haiku_probe_long.py`).
     - **Fix:** a fourth breakpoint at the tool-result boundary alone, next to the image-boundary one. Anthropic allows
       4: system, image boundary, tool boundary, tail.
     - Then re-probe the long turn.
   - **Then:**
     - fast-forward `wt/haiku` to the branch;
     - a Sonnet delta review of `2bbb782..HEAD`;
     - the full sweep in `wt/haiku`. Never sweep in the `haiku-fix` clone: its path contains an "x", which flips the
       kill-rule tests.
     - merge, then deploy with `--restart`.
   - Until #86 deploys, the owner's Haiku selection runs **uncached** and compacts at 60k.
3. **CLAUDE.md slimming** (the owner said yes), after #86 merges.
   - CLAUDE.md keeps the invariants, safety rules, decisions and an index, about 30–40 KB.
   - The history moves to `docs/notes/<area>.md`, by script. Nothing is deleted, and the move is checked line by line.
4. **TC-2, the HUD half of tool calls** (plan `docs/plans/2026-10-12-hud-tool-calls-in-transcript.md`). TC-1 is live
   after tonight's deploy. Unless the owner answers, use the planner's defaults:
   - no one-line result in an opened call;
   - collapse the running group too;
   - no tool kinds on the collapsed row.
5. **The rest of auto-open** (amendment 2 §H):
   - AO-1b-ii: terminal announcements into the registry;
   - AO-2: Browser tabs;
   - AO-3: auto-open in the window.
6. **The Settings page** (plan `docs/plans/2026-10-11-hud-settings-page.md`). Not started.
7. **Checks that fail only under load.** Each passes alone:
   - `rules_check`'s 100 ms cost bound (failed twice tonight in full sweeps, at 0.114 s and 0.145 s);
   - a `vex_check` 1 s bound;
   - a `terminal_check` throughput bound;
   - `hud_backend_check`'s Claude-quota test.
8. **The rest of the Browser plan:**
   - BR-1c, BR-3b, BR-4, BR-5;
   - BR-9b and BR-9c ("Agents may drive" off);
   - then BR-6, BR-9d, BR-10, BR-7.
9. **Small follow-ups:**
   - memory `hud-open-followups`;
   - #65's two small older git-tool gaps;
   - runner unwrapping (`uv run rm -rf ~`).
10. **Only when the owner asks:** the VEX view's UI rework. The project is at `C:\dev\override-jarvis`. Build works;
    download is untested on a real brain.

## 5. Wednesday: start here (for the lead)

1. **Read this file, then the end of the questions log.**
2. **Check the machine.**
   - Run `git -C ~/projects/Jarvis worktree prune`.
   - Check `ps -eo pid,rss,args --sort=-rss | head`. The owner's APUSH scripts can use about 11 GB.
   - Check `git -C ~/projects/Jarvis status --short`.
3. **Check the gate files.**
   - `stat -c %Y ~/.config/jarvis/allowlist.json` should be **1791521872**.
   - `models.json` should be **1791858606**. That changed at 21:30, the owner's Haiku pick (unconfirmed).
   - A later change is the owner's HUD use or a leak; check which.
4. **Check that tonight's deploy took.**
   - `curl -s 127.0.0.1:8405/status` should show `hud_git_head` equal to main's HEAD and `hud_state: installed`.
   - If the deploy log shows a refusal (a turn was running), run `jarvis hud rebuild --pull --restart` from the clean
     main checkout.
5. **Ask the owner about section 3's blocked edit**, then work the queue in section 4.
6. **The lead's tools:**
   - `~/.cache/jarvis-lead/lead_runall.py <wt> 3` (the full Python sweep; write it to a log file);
   - `lead_hud.sh <wt> <port>` and `hud_slot_wait.sh` (HUD suites, at most 3 at once);
   - Codex runs as `codex exec -m gpt-6.1-sol -c model_reasoning_effort=high -s workspace-write -c
     sandbox_workspace_write.network_access=true -o report.md - < brief.md` in a `--shared` clone, under a 4G
     `systemd-run` scope. Codex can't write `.git`, so the lead commits.

## 6. Waiting on the owner

- **Ctrl+R** in the HUD after tonight's deploy.
- **Section 3's blocked edit:** allow it, or make it by hand.
- **Confirm the Haiku 5.5 / `xhigh` selection** at 21:30 was yours.
- **Compaction at 90k**, the lead's decision: Haiku costs 5x for the whole request from 100k prompt tokens. Say if you
  want exactly 100k.
- **Settings → Install as app**, once, if not done.
- **Optional:** TC-2's three questions (section 4, item 4).

## 7. Standing rules (short form; the memory files hold the detail)

- **Every package:** plan → independent review → fixes → lead sweep in a clean worktree → merge → deploy.
  - Merge-on-review is authorized for HUD, editor, VEX, Browser and auto-open work, small fixes and queue items.
  - Sonnet does light reviews, Opus does boundary reviews, and Codex builds and fixes.
- **Lead work lives in `~/.cache/jarvis-lead/`, never the session scratchpad.**
- **Every test runs in `systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0`.** About 5–6 agents at once,
  and at most 3 HUD suites.
- **Never:**
  - read, print or write a real `.env*`;
  - print a secret;
  - write `~/.config/jarvis` from a test;
  - touch ports 8402/8403/8405 or the daemon unit from a test;
  - run a real VEX tool;
  - install into `~/.local/share/jarvis/hud` from a test;
  - `pkill -f`;
  - force-push;
  - edit or stash the owner's uncommitted work;
  - work around a denied permission.
- **Always** use `git -C`. Deploy only from a clean, synced main checkout.
- Push-to-talk stays Ctrl+D. Jarvis can't change its own model: the owner clicks Set as default.
