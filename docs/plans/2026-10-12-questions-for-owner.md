# For the morning (2026-10-12)

Nothing in this file blocks the lab. The editor, VEX build/download and the
fixes are live as of last night. Press Ctrl+R if the HUD still shows the old build.

## To do before or at the lab

1. **Make the robot folder a project** if it isn't one yet: New project →
   `/mnt/c/Users/johnw/OneDrive/Desktop/override`.
2. Open two panes (⊞ → two side by side). Put a chat on the left, pick the
   project on the right and click **vex** in the pane's view strip.
3. **Build**, then **Download to slot N**.
   - If the files changed since the last build, it builds first.
   - It never sends a program that's out of date.
   - Start the program from the brain or the controller.
4. If anything misbehaves on the real brain, copy the exact message from the
   VEX view or the panel. It's the first time it runs against real hardware.

`jarvis vex setup` is done. Device info (`vexcom --json`) is being added; it
will show up after a Ctrl+R once it merges.

## Decisions I took for you overnight (say if you want otherwise)

- **Vim in the editor (ED-8):** push-to-talk keeps Ctrl+D, as you said. Vim
  mode is an off-by-default setting, and only if `monaco-vim` works with our
  Monaco.
- **An error click with no room for a pane** says "no room" instead of turning
  the VEX view into an editor (ED-7 × VX-2).
- **The built-in Browser can't reach your local servers unless you open them (BR-1a).**
  - All of the Browser's traffic goes through Jarvis's network gate, and the review found that this switches off Chrome's own protection for local servers. Without the gate, any website you visit could quietly send requests to your dev servers or the trading firm on 8407.
  - So a `localhost` port works in the Browser only after **you** open it in a Browser pane, for example by typing `localhost:5173` or by auto-open from a terminal. Any other local port stays closed, and an agent or a web page can't open one.
  - It's the same rule as the LAN, which stays closed. Say if you want a switch to open all local ports.

## Questions (answer whenever)

1. **Terminals after a restart (your item 1).** There are two ways to do it:
   - **(a) Quick, planned first:** when a terminal's shell has ended (daemon
     restart, reboot), typing in it, or a single click, starts a new shell in
     the same folder, under the old history, with a "restarted" line between
     them. The same "Jarvis can read" setting carries over. Running programs
     don't survive. This is what VS Code does after a full restart: it keeps
     the text and starts a new shell. Nothing is written to disk; terminal
     output stays in memory, as decided in WP-C.
   - **(b) Real survival:** shells keep running across a daemon restart,
     inside a small separate "terminal host" service, like VS Code's pty host
     surviving a window reload. This reverses decision W-3 ("no tmux, the
     daemon ends them all") and is a bigger change.
   - I'm doing **(a)**. Tell me if you also want **(b)**.
   - **Answered (10:58): (a).** (b) is not being built.

2. **The Diff tab (your item 4).** Right now Diff only shows a *task's*
   changes, so with no task selected it says "select a task". I'd make it
   show the **project's uncommitted changes** (what `git diff` shows, like VS
   Code's Source Control) when no task is selected, with the task diff when
   one is. I'll queue that unless you say no.
   - **Answered (10:58): yes.** It's queued after ED-9 merges.

3. **Should the editor's git and navigation code be self-protected?** ED-9 (git gutter) adds `jarvis/v2/editor_git.py`. Like ED-7's `editor_nav.py`, it is **not** on the list of files Jarvis's own write tools refuse to change (SELF_PROTECTED). Both run git and judge which files the editor may show you. Adding them means Jarvis can't edit them in a chat. You said no for `runtime.py` before. I'd add these two, since they decide what is safe to show. Your call; nothing waits on it.

4. **How hard to scrub `.env` values out of what Jarvis reads (PR #60, ENV-1).** As a last line of defence, Jarvis blanks any value from your env files that turns up in what a tool returns. The review found the cost: with `NODE_ENV=production` in `.env.production`, every "production" in your code came back to Jarvis as `[redacted]`, and a file he rewrote whole could save that marker into your code.
   - **I decided** (the fix round is building it): the newly covered `.env.<anything>` files are scrubbed only for secret-named keys (`…KEY`, `…TOKEN`, `…SECRET`, `…PASSWORD`, …) and for values that look like secrets (long random strings, URLs carrying a password or token). `.env` and `.env.local` keep today's behaviour, which blanks every value.
   - **Your call:** should `.env` and `.env.local` get the same narrower rule? Today, a `.env` with `NODE_ENV=development` blanks every "development" Jarvis reads in that project. Values under 8 characters, like `PORT=3000`, are already left alone. Narrowing it is a (small) loosening of a safety layer, so I won't do it without a yes.

## Answers

**Your answers on the lab items (2026-10-12, evening):**
1. The VEX code is a project now. Its folder is `C:\dev\override-jarvis`, not the OneDrive path above.
2. The VEX view works. You want to rework its UI later; that's queued and not started.
3. Build works. Download isn't tested yet. The first real download is still the one to watch: if it misbehaves, copy the exact message from the VEX view or the panel.


**What do Tasks do?** A task is a bigger job Jarvis runs in the background,
apart from the chat:
- It gets its own git worktree (a separate copy of the repo).
- Claude plans and orchestrates, Codex writes the code, and a separate Claude
  reviewer checks it before it's marked done.
- It posts its progress in a Discord thread.
- A task starts when Jarvis proposes one in a chat and you accept, or from
  "+ new task" in the sidebar.

The **Task** tab shows the selected task: what it's doing, any question it's
waiting on (answer it there), and Steer, Cancel and Resume. **Diff** shows
the files that task changed.

**Auto-resume after a usage limit:** a scheduled check-in in this session
picks the work back up after a limit resets, as long as this Claude Code
session stays open. Don't close it overnight.

## Queued from your last messages

These come before the Browser work, in this order. Each starts as soon as an agent slot frees.
1. ✅ Claude's first reply showing: **live** (#57, 11:12).
2. ✅ Chat titles: **live** (#62, 12:35).
3. ✅ File → Editor, Preview → Browser: **live** (#68, 18:40). Browser is the full rename from the Browser plan, and your stored layouts carry over: a Preview pane you have open comes back as a Browser pane, with its URL. "Editor" is a label change only, so nothing about your open editor panes changes.
4. ✅ Terminal auto-continue, option (a): **live** (#64, 18:40).
5. ✅ Claude's cost overcount fix: **live** (#63, 12:35), with the old threads recounted.
6. ✅ Diff shows the project's changes when no task is selected: **live** (#67, 19:46).
7. **A security fix, found by the ED-9 builder.** The `git` tool that background agents use (workflows, sub-agents, goals) runs a *submodule's* own filter program when it runs `status` or `diff`, because its safety check only reads the main repository's settings. It's hard to exploit: an agent can't write those settings itself, so a submodule would have to come with them. Still, it goes in before the Browser work. ✅ **Live (#65, 19:46).**
  - The fix also covers the git that `gh` runs when it opens a pull request.
  - Review rounds closed a gap the previous round had opened, plus several older ones in the same code: a `--` that hid a signing option, `-S` signing allowed outside `commit`, and `rebase -m -x <command>`.
  - A fuzz test of about 71,000 git command lines against real traps found none that fire. The old code had 551.
  - Two small older gaps are queued as a follow-up.

8. **Auto-open local servers (asked ~20:20).** This is how it will work; say if any of it should change.
   - **Which servers:** ones started in your HUD terminals, or by a chat or task on Jarvis, Claude or Codex. Agents' servers are found by watching for new local listening ports from their processes. Servers you start outside Jarvis aren't included.
   - **Where it opens:**
     - **Room left:** a new Browser pane beside the chat or terminal that started it, without taking your focus or the mic.
     - **8 panes, or no room:** a new tab in an existing Browser pane. Browser panes get a tab strip.
     - **No room and no Browser pane:** a small prompt asks which pane to open it in, or "Don't open". It never blocks you like an approval card.
   - It opens as a local page in the pane's sandbox. Auto-opening never unlocks that port for the Remote browser; only you can do that.
   - **Plan merged (#74, ~21:20); building.** First the keyboard-focus guard for every Browser pane, then detection of agents' servers, Browser tabs, and auto-open in the window.
   - Your two open points are settled as recommended: a server whose `/` isn't a web page (a JSON API) doesn't auto-open, and a new tab comes to the front.
   - "Open in Browser" from a terminal link will open a new tab instead of replacing the page.

## Overnight log

(I'll add what merged and anything that needs you here.)

- **~01:45: #55, VEX device info, merged and live.** The VEX view has **Read device info** (`vexcom --json`) and **List programs** (`vexcom --programs`). Both only read from the brain, and both are shown as plain text. Press Ctrl+R to get them.
- **Thread 447ff25e (Jarvis-improvements) is full.** Its Claude conversation hit "Prompt is too long". Start a new thread instead of using that one. A fix that handles this properly is part of the first-reply bug PR.
- **Your boot animation and app icon:** no branches or PRs exist yet. The builders that thread started seem to have died with it; their worktrees are untouched. Your approved prototype is safe at `prototypes/boot/index.html`. **Do you want me to take both over?** I'm not touching them until you say so.
- **Why Claude's first reply went missing (found; PR #57, in review).** After a daemon restart, the Claude CLI first runs a queued background-task notice of its own. It does that in the moment before your message arrives. That notice gives an empty answer, and Jarvis took it as the answer to your message, so your real reply waited until your next message pulled it out. The fix tells the CLI's own turns apart from yours. "Prompt is too long" now compacts the conversation once and retries. If that fails, it tells you to start a new thread instead of showing the error as a reply.
- **~02:00: #58 (ED-6, folding from the syntax tree) is merged and live.** Code now folds by structure (classes, functions, Markdown sections) instead of by indentation, a block's closing `};` stays visible when it's folded, and sticky scroll shows the function you're in.
- **~02:45: WSL crashed** (the log doesn't say why), and everything in flight stopped until you restarted at 10:47. No work was lost: every commit was in the repo. The agents' scratch folders were wiped. At 10:55 I resumed all five where they stopped: ED-8 settings, ED-9 git gutter, the `.env` refusals (ENV-1), and the reviews of #56 (new VEX project) and #57 (Claude's first reply). The daemon came back on its own, on f1de871, healthy. Your allowlist and models.json are untouched.
- **11:12: #57 is merged and live: Claude's first reply shows again.**
  - A message sent right after a daemon restart now gets its real answer.
  - "Prompt is too long" now compacts the conversation once. The message is re-sent only if its turn hadn't done anything yet. A turn that already ran a tool (a push, an edit) is never run twice; instead it tells you to send the message again.
  - Stop works during compaction.
  - The daemon restarted for this, so press Ctrl+R; any open HUD terminals ended.
- **~11:45: #56 (new VEX project) is merged.**
  - It went live with ED-9 at 11:59, in one restart. ED-8 (editor settings) needs one more review round and comes later.
  - The palette has "New VEX project", built from the VEX extension's own templates, and you approve the folder on a card.
  - A folder whose name VEX's tools can't handle (`My (1)`, `a&b`, non-English letters) is refused before you're asked.
  - A write that fails partway takes back only the files it made itself.
- **~11:59: #61 (ED-9, git gutter) is merged, and with #56 it is live** on 8248177. Press Ctrl+R; your HUD terminals ended with the restart.
  - The editor's margin marks lines added, changed or removed since your last commit, and it follows your unsaved typing.
  - The file tree colours files by git status: modified, untracked, ignored.
  - Env files never get a gutter.
  - In a folder that holds several repositories, a commit inside one clears its marks within about 10 s.
- **12:35: chat titles (#62) and the Claude cost fix (#63) are merged and live** on 6887a37. Press Ctrl+R; your HUD terminals ended with the restart.
  - **Titles:** each chat names itself from your first message, through Codex `gpt-6-luna`, falling back to the default OpenRouter model. Your existing chats are titled shortly after the start. The sidebar shows the title (wrapping up to 3 lines), and the id is under ⋯ → Copy id. A title you set yourself is never replaced. An archived chat's Discord thread is renamed only when you restore it.
  - **Cost:** Claude threads now count each turn's own cost. At this start, your five overcounted Claude threads were recounted from their logs (447ff25e: $109.97 → $23.22). A backup of every thread record from just before is in `~/.local/share/jarvis/backups/threads-before-cost-recount-20261012/`.
- **~12:40: #60 (ENV-1, Jarvis's half of your `.env*` rule) is merged.** It deploys together with ED-8, in one restart.
  - Jarvis reads none of your `.env*` files and writes only `*.example`, through every tool: v1's file and shell tools, Claude's Read/Grep/Glob, Codex's grants and `terminal_read`.
  - Writes to one are refused outright. Ordinary work isn't affected: a differential over ~3,600 commands moved only lines that name an env file.
  - Not covered: a program running as you (a dev server, a script) can still read `.env`. The real boundary would be file permissions.
- **~12:55: ED-8 (#59, editor settings) is merged, and with #60 it is live** on ecbfbc1. Press Ctrl+R; your HUD terminals ended with the restart.
  - The editor's ⚙ (or the palette's "Editor Settings…") sets the font, size, tabs, word wrap, minimap, whitespace and so on, applied live to every editor.
  - A project's `.editorconfig` sets indentation and line endings per file.
  - **Vim mode** is an off-by-default setting. Ctrl+D stays push-to-talk, as you decided.
  - A crafted `.editorconfig` can't freeze the window: a hard 5 ms clock guards it.
  - **The editor is now complete** (ED-1 to ED-9, VX-1 to VX-3).
- **Terminal auto-continue (#64): one decision I made for you.** A line you type into an ended terminal goes into the new shell **typed but not run**: it stops before your Enter. Until your first key, the screen still shows the old prompt, which could be an ssh session on the robot, and a line meant for there would otherwise run on this machine. Check the new prompt and press Enter. A paste with line breaks isn't sent; paste it again. Say if you'd rather it ran.
- **18:40: terminal auto-continue (#64) and the renames (#68) are live** on 6815ab1. Press Ctrl+R; your HUD terminals ended with the restart. From now on, a terminal a restart ended picks up again when you use it.
  - **Terminals:** a terminal whose shell ended (a daemon restart or a reboot) keeps its text. Type in it, or click it, and a new shell starts in the same folder under the old history, with a "restarted" line between them. A line you type there is put in **but not run**: check the new prompt and press Enter. The "Jarvis can read" switch carries over.
  - **Renames:** the File view is called **Editor** and Preview is now **Browser** everywhere. A Preview pane you had open comes back as a Browser pane with its URL. A "keep its origin" setting carries over only if it was given for that same URL's origin.
- **~18:55: Browser work under way.** #66 merged: the Browser's key, mouse and paste handling, plus its "Jarvis is driving" banner logic. These are new modules, not wired into the window yet, so nothing changes on screen. The Browser engine (#69, the Chrome host and the network gate every page goes through) is in review. Dev-server detection in terminals and the rules for what an agent may do in a page are being built. A fix for tests that fail under load is also being built.
- **19:46: the Diff tab (#67) and the git safety fix (#65) are live** on 481eb35. Press Ctrl+R. Your terminals pick up again when you type in them (#64).
  - **Diff:** with no task selected, the Diff tab shows the project's uncommitted changes, like VS Code's Source Control.
    - A list of changed files with their status letters; click one to see its diff. An unsaved edit shows too, because the right side is your live buffer.
    - Env files are never read.
    - When a Task pane is open, the Diff beside it shows that same task's changes, and its caption says why.
  - **Git safety:** background agents' `git` can no longer run a submodule's own programs, sign with your key outside `commit`, or slip options past the check behind `--`.
- **~20:05: #71 merged: the rules for what an agent may do in a Browser page.** They aren't wired in yet, so nothing changes until the driving packages land.
  - Never type into a password, one-time-code or card field.
  - Never act while an approval card is up, or within 5 s of your own input.
  - Logins, payments, OAuth and executable downloads always ask you.
  - Uploads never come from credential or hidden folders.
  - Anything else goes to a reviewing model, which never sees form values, never runs on a Chinese-hosted model, and asks you whenever it's unsure.
- **~20:45: the Browser engine (#69) is merged.** It goes live with the next restart, and it isn't wired into the window yet. Every page's traffic goes through Jarvis's network gate:
  - Jarvis's own ports are never reachable.
  - The LAN and local servers stay closed unless you open them.
  - WebRTC can't send traffic around the gate.
  - Pop-ups open as tabs.

  Next is BR-1b, which lets a HUD pane show and drive a tab; it is being built now.
- **Found by review, being fixed (PR #75): an old gap in the command gate.**
  - Background agents' ungated `run_readonly` tool would run `env -u ls rm -rf build`, because it reads `env`'s options naively.
  - The same misreading lets your `python3` allowlist entry cover `env -u python3 rm -rf …`.
  - It's in the same fix as the runner rule (`uv run`/`npx` judged by what they start). That PR's first round also had two regressions of its own, which are being fixed too.
- **Found by the auto-open plan's review, and true of today's Browser pane too: a page in a Browser pane can take your keyboard.**
  - Any page can call `focus()` without a click and take the keyboard from the chat box, the editor or a terminal. Everything you type after that goes to the page, and the page can send it to its own server.
  - Ordinary apps that focus an input when they load do this too.
  - Until the fix lands (its own PR, being built now, ahead of auto-open), be careful typing anything sensitive while a Browser pane shows a page you didn't write.
  - **The fix:** a page that takes focus while your pointer isn't on it, and without a click or Tab into it, is unloaded, and the pane shows "This page took the keyboard · Reload". A page that focuses while you're pointing at it keeps working.
- **~21:20: #72 merged: tests that failed under load now pass reliably.** Test-only, except one change in the editor: a slow `.editorconfig` that is later read in time is no longer counted against it. Next: #73 (Diff follow-ups) is in its final check, then one restart deploys #69 to #73 together.
- **~21:30: two Browser packages are built and in review.**
  - **#76:** detection of local servers agents start. It is the auto-open half that decides a server is an agent's, and it refuses any process Jarvis didn't start.
  - **#77:** BR-1b, which lets a Browser pane show a live view of a real Chrome tab and send it your keys and mouse.
    - Nothing reaches the page while an approval card is up, and nothing is replayed after it.
    - Downloads are saved privately and executables are held.
    - Pop-ups past 12 tabs are refused.
  - Neither changes the window yet.
- **Usage:** at your request, new lighter-work agents now run on Sonnet 5.5: re-reviews, docs and mechanical builds. Plans, and the first review of anything that guards your input, approvals or secrets, stay on Opus. The #76 review runs on Sonnet; #77's is on Opus.
- **~21:35: #69–#73 are live on 2ed51b6. Press Ctrl+R.** Your HUD terminals ended with the restart; type in one to pick it up again.
  - **Diff tab (#73):**
    - The changed-files list stays fast at thousands of files, and ↑/↓, Page Up/Down, Home and End move through it.
    - A Task pane's header has a `task <id> · clear` chip, so a Diff beside it can go back to showing uncommitted changes.
    - The uncommitted diff follows your editor settings: font and size.
  - **Browser engine (#69), the dev-server URL detection in terminals (#70) and the in-page rules (#71):** live, but nothing on screen changes until the auto-open and live-view packages land.
- **~22:35: the plan for the usage limit (your answers recorded).**
  - **Codex (gpt-6.1-sol) now does part of the work**, on the ChatGPT quota: #77's fixes and #75's re-review. The lead commits for it, because its sandbox can't.
  - **New Claude agents default to Sonnet.** Opus is kept for plans and for the first review of anything guarding your input, approvals or secrets.
  - **CLAUDE.md gets slimmed (your yes).** It is about 620 KB, and every Claude agent turn re-reads it. It shrinks to the core rules and an index, and the per-feature history moves to `docs/notes/`. Nothing is deleted. This happens right after #75–#78 merge.
  - **Scope (your answer):** the Browser plan continues past auto-open.
- **~23:10: the VEX simulator for tomorrow's lab (`C:\dev\override-jarvis`).**
  - **The HUD can show the sim now.** Its server sent `frame-ancestors 'none'`, which refused every frame. It now allows only the HUD (`127.0.0.1:8402` and `localhost:8402`).
  - **New `GET /api/runs`:** lists the last 8 runs, newest first, so Jarvis can find the run you just made and read it from `/api/result/<id>`.
  - Both changes are in `sim/tuning/server.mjs`, with tests in `sim/test/tuning.test.mjs`. They are uncommitted on `main`, beside your other edits.
  - **Found: the sim refuses to run even on committed `main`.** Commit 7401863 (clawToPoint/clawToPose) changed `src/auton-actions.cpp` without the sim's motion-parity review. With your OK, a Codex agent (gpt-6.1-sol) is doing that review now. It refreshes `sim/parity/motion-source.sha256` only if the parity tests pass, otherwise it reports why.
  - **To use it at the lab:**
    1. In a HUD terminal, run `node sim/tuning/server.mjs`.
    2. Open `http://127.0.0.1:8787` in a Browser pane and turn on **Keep its origin**. Without it, the page's own requests get refused.
    3. In a Claude thread on the override-jarvis project, ask Jarvis to read your last run.
  - **The pane blocks downloads**, so the page's "export JSON" button won't save a file there. Ask Jarvis to save the run instead; it holds the same data.
- **~00:10: the VEX sim works again, and your uncommitted VEX changes are committed** (`C:\dev\override-jarvis`, `main`, 9 commits; not pushed).
  - **Parity.** The C++ parity cases pass on your Windows Node 24 with `C:\Program Files\LLVM\bin\clang++.exe` (WSL has no clang++). Codex's review found the claw moves leave existing moves unchanged, so the pin was refreshed. The sim's parity and tuning tests: 20/20.
  - **Line endings.** About 29 files had changed only in line endings (a Windows tool rewrote LF to CRLF). They went back to LF with their contents identical, so they're not in any commit.
  - **Commits, oldest first:**
    1. HUD framing plus `/api/runs`.
    2. Parity pin refresh.
    3. Build script COM-port pick plus `build.sh`.
    4. Front distance sensor calibration plus reset switch.
    5. Loader reach and standoff.
    6. Claw score button.
    7. auton-helpers, Skills and SideTwoPin.
    8. Select Skills.
    9. The `.vscode` project settings.
  - **Check commit 9 before the lab.** Your settings had the project name `matchload_Test` and slot 3, where the committed version had "override 87867D" and slot 1. The VS Code extension and the HUD's VEX Download both use these, so a HUD download now writes `matchload_Test` to slot 3. `build.sh download` still writes `override-jarvis` to slot 1. If that was a test setting, revert commit 9 (`git revert HEAD`).
  - `./build.sh build` succeeds on the final tree.
- **~00:40: #77 and #76 merged** (main 8f1d588; deploy with the next restart).
  - **#77:** the live-Chrome-tab backend. Codex wrote its fixes. The re-review then caught one real bug, which the lead fixed: a new safety check dropped ordinary fast typing.
  - **#76:** the dev-server detection rules.
  - **Next:** Codex is building AO-1b-i, the daemon half of auto-open. #78 (the keyboard guard) is in its final check. #75 (the command gate) is in a second fix round: the re-review found two ways a write to the allowlist could run without asking.
- **18:52 CDT: #76–#78 are live on d2c16bb. Press Ctrl+R.** Your HUD terminals ended with the restart; type in one to pick it up again.
  - **#78: a page in a Browser pane can no longer take your keyboard.** A page that grabs focus without your click is unloaded, and the pane says "This page took the keyboard · Reload".
  - **One change in how it feels:** after you type in the HUD (or in another page), your first click on a page only gives it the keyboard; the second click acts. The click only focuses the page so a page you merely crossed with the mouse can't take your typing.
  - **#76 and #77:** backend only, nothing visible yet.
- **~19:15 CDT: your answers.**
  - Take over the splash animation and the app icon, and push them tonight: **yes**.
  - `editor_git.py` and `editor_nav.py` become SELF_PROTECTED: **yes**.
  - The narrower `.env` scrubbing applies to `.env` and `.env.local` too: **yes**.
  - New ask: tool calls go **in the chat transcript**, collapsed per run ("Used 15 tools", click to open).

## ~19:45 CDT: splash, icon, #79, #80, tool-calls plan

- **Splash animation: [PR #82](https://github.com/Johnwang688/jarvis/pull/82).** Codex ported your prototype; it passes its 33 checks.
  - The lead changed one thing: the overlay now mounts on the first frame. Codex had waited for the first load, which would have shown the HUD for a moment before the animation covered it.
  - A Sonnet review is running. After it, a full HUD sweep, then merge and deploy.
- **App icon:** Codex is building it now (branch `feat/hud-app-icon`). It covers:
  - orb icons;
  - `manifest.webmanifest`;
  - CSP `manifest-src 'self'`;
  - "Install as app" in Settings;
  - `jarvis hud` opening `--profile-directory=Default`, and the installed app via `--app-id` once installed.

  **This one is not merged without asking you**, since it needs your one Install click.
- **#80 (your Q3/Q4):** the review passed with one gap. Joined names like `PGPASSWORD`, `MYSQLPASSWORD` and `USERCREDS` were no longer caught, because they weren't treated as secret-named. The lead fixed it and added a check (it fails on the old rule). Full sweep running, then merge.
- **#79 (auto-open, the daemon half):** the delta review passed. The lead fixed its two `/status` nits: a faulted watcher now reads inactive, and every hook fault is counted. Sweep next, then merge.
- **Tool calls in the transcript: the plan is [PR #81](https://github.com/Johnwang688/jarvis/pull/81).** It needs your answers, with the planner's recommendations:
  1. Show a one-line result in an opened call? *Recommended: no, not now.* It shows the name, a label, the status and the duration, never the output.
  2. Collapse the running group too ("Using tools… 3")? *Recommended: yes.*
  3. Name the tool kinds on the collapsed row (e.g. "Used 15 tools · 9 reads, 4 edits")? *Recommended: not now.*

## ~20:20 CDT: restarts, #80 and #79 live

- **Two restarts tonight:** a WSL reboot (~19:35) and Claude Code restarting twice.
  - Each wiped the lead's scratch folder. In-progress work was lost: the app-icon build twice, test runs, and the #75 builder's round.
  - Nothing pushed was lost.
  - The lead now works from `~/.cache/jarvis-lead`.
  - **Likely cause of the memory trouble:** three APUSH `check_audio` scripts (`~/fwenv`, `~/c15`) were using about 11 GB uncapped. They OOM-killed a HUD test run at 19:53.
- **#80 is merged and live:**
  - `editor_git.py` and `editor_nav.py` are self-protected.
  - `.env` and `.env.local` use the narrow scrub.
  - The review gap is fixed: joined `*PASSWORD`/`*CREDS` names and `MNEMONIC` are now caught.
- **#79 is merged and live:** the daemon half of auto-open, which finds dev servers that Jarvis, Claude or Codex start. The window half (opening the pane) is next in the plan. Deployed 991d1b3 at 20:16; **press Ctrl+R**.
- **Follow-up logged:** `hud_backend_check`'s Claude-quota test is flaky under load. It fails 1 in N when run beside other suites and passes alone. It needs a wait-for-state fix.
- **~20:25: the thinking line (#36) is live**, deployed from 79c66b5. **Press Ctrl+R.**
  - While a chat's turn runs, its foot shows a mini orb, a word ("Thinking…", "Running pytest…", "Waiting for approval") and the elapsed time.
  - Checks: 1,458 unit tests and 2,702 HUD checks passed. The merge review passed, and its three small nits are fixed.
- **Splash (#82):** the review's four fixes are in. The important one: a canvas failure no longer blanks the HUD and the approval card. Its own full run passed with 2,686 checks. It's now in a final run merged with the thinking line, then merges and deploys.
- **Tool calls in the transcript:** the daemon half (TC-1) is being built now. The HUD half (TC-2) follows it and waits on your three answers above.
- **App icon: [PR #83](https://github.com/Johnwang688/jarvis/pull/83).**
  - The review passed. The lead fixed its two small items: the Chrome profile name is now an allowlist, and an empty setting means Default.
  - The installed-app id was checked against a real Chromium install in a throwaway profile, which caught a wrong formula in the lead's brief.
  - **Waiting on your OK to merge.** Once it's deployed, click Settings → **Install as app** once in your Default profile, then pin the orb.
  - It gets its full HUD run after the splash merges, since both touch `App.tsx`.
- **~20:40: the splash animation (#82) is live**, deployed from e0a5f2d with no daemon restart. **Press Ctrl+R:** it plays on that reload, then once per new window.
  - A click or a key skips it.
  - It never shows under automated tests, and it gives way to an approval card at once.
  - Checks: 1,495 unit tests and 2,741 HUD checks on the merged tree.
- **~20:50: the owner OK'd #83** ("merge and deploy 83 once it passes review"). The review passed; it merges after its full HUD run.
- **#75 (command-gate follow-ups), third round: done, head 8ab4aa3.**
  - **The HIGH is fixed:** an `X=1`-style prefix hid a write to the allowlist; with a stem allowlisted, it ran unasked.
  - **Three older holes the audit found are fixed:**
    - a reserved word (`if true; then …`) in front of a writer;
    - `D+=` path building;
    - `LD_PRELOAD=` in `run_readonly`.
  - A fresh independent delta review is running.
  - **Recorded as a limit:** `LD_PRELOAD=… ls` stays ALLOW in the gate's rules, as on main. It is the same class as `python3 script.py`, which is already ALLOW.
  - **Flaky under load, for the "a check must hold under load" follow-up:**
    - `rules_check`'s 100 ms cost bound;
    - a `vex_check` 1 s bound;
    - a `terminal_check` throughput bound;
    - `hud_backend_check`'s Claude-quota test.

    Each passes alone.
- **~21:00, the owner: compaction at 100k, Haiku 5.5 as the default.**
  - Confirmed against the catalog: `anthropic/claude-haiku-5.5` lists at Luna's price, with a 1M context, tools, vision and effort up to max.
  - Codex is building it (`feat/haiku-default`):
    - `config.COMPACT_AT_TOKENS` = 100k, which was 60k in `ContextPolicy`;
    - the configured default orchestrator becomes Haiku 5.5;
    - **OpenRouter's top-level `cache_control` for Claude models.**
  - The cache switch matters: unlike OpenAI's, Claude's prompt caching is opt-in, so without it every step would pay full input price.
  - **The owner's saved HUD choice (gpt-6-luna) still wins over the config default.** After it deploys, the owner picks Haiku 5.5 in the Model picker → **Set as default** (Jarvis can't change its own model).
- **~20:57: Install as app (#83) is live**, deployed from 8f74e81 with a daemon restart.
  - Checks: 1,500 unit tests and 2,744 HUD checks.
  - The live daemon serves `/manifest.webmanifest` and the icons with `manifest-src 'self'`.
  - **For you:** press Ctrl+R → Settings → **Install as app**, once. Then close the window, run `jarvis hud`, and pin the orb.
- **#75 (command-gate follow-ups): the third delta review passed** with no findings, nothing looser than before and no ordinary work refused. The lead's merge with main is in its full Python sweep; it merges and deploys after.
- **CLAUDE.md slimming** (your yes, earlier): starts right after #75, the Haiku change and TC-1 merge, since all three edit CLAUDE.md. CLAUDE.md keeps the invariants, safety rules, decisions and an index; the per-feature history moves to `docs/notes/<area>.md`, with nothing deleted, checked line by line.
- **TC-1 (#84): the fix round is pushed (54d3392).**
  - Labels now mask credential shapes, passwords and tokens in URLs, and the project's own `.env` values.
  - "denied" comes only from Jarvis's own denials, never from what a tool printed.
  - A delta review is running.
- **~21:15, the owner: "Leave them, wait"** — about uncommitted VEX edits found in the main checkout (build output in the VEX view, 12 files, ~21:00). #75 is merged on GitHub; its deploy waits until the checkout is clean.
- **~21:30:**
  - **Haiku 5.5 default + 100k compaction + Claude caching** is [PR #86](https://github.com/Johnwang688/jarvis/pull/86), in review and in its full Python sweep.
  - **TC-1 (#84):** the delta review found the new URL regex could freeze the daemon on a long token (2 s at 100K). The lead fixed it: 1 ms now, a bounded scheme on a 4,096-character head. A parent folder's `.env` values are now withheld too. Now in its full sweep.
  - **PR #85** (`fix/vex-build-feedback`) is from the owner's other session: the same VEX work that has uncommitted edits in the main checkout. The lead isn't touching it. **#75 and the next deploys wait for that checkout to be clean.**
- **~21:35: checkpoint.** Usage limits may pause work until Wednesday.
  - The queue, open PRs, deploy blocker and how to resume are in `docs/plans/2026-10-12-checkpoint.md`.
  - `models.json` changed at 21:30: Haiku 5.5 is selected at `xhigh`. That looks like your Model picker; please confirm.
  - Until #86 deploys, Haiku runs without prompt caching.
- **~21:55:**
  - **TC-1 (#84) is merged** (ae463e4).
    - Its full sweep: 90 of 90 suites pass, counting `rules_check`'s known under-load timing flake, which passes alone.
    - Not deployed yet; it waits for the clean checkout with #75.
  - **#86's review found the caching never hits, and a live probe confirmed it.**
    - The breakpoint landed on the working-context block, which changes every step.
    - Result: no cache reads and a 1.25x write every step ($0.0026 a step on a 20k prompt).
    - Two explicit breakpoints (the system message, and the newest message before the block) cache about 19k of the 20k from step 2: $0.00035 a step, about 7x cheaper.
    - The probe cost $0.02 in all.
  - **Haiku 5.5's catalog price:**
    - Under 100k prompt tokens it matches gpt-6-luna: $0.10/$0.50 per million.
    - **From 100k, the whole request costs 5x.**
  - **Decision for you: compaction defaults to 90k, not 100k.** Inside one long turn the context can grow past the threshold before it can be compacted, and 100k sits right on the price step. Say if you want exactly 100k; `JARVIS_COMPACT_AT_TOKENS` sets it either way.
  - **Reasoning:** a live 3-step tool turn on Haiku at your `xhigh` setting ran cleanly.
  - **Next:** Codex is building the fix round. It goes through a quick review and a sweep, then merges. Deploys still wait for the clean checkout.
