# Owner live checks for the 2026-10-15/16 merges

Every check that only you can do on your Windows PC or the installed HUD, in one place. **Everything below is already deployed** (the lead installed the HUD and restarted the daemon after each merge on 2026-10-16), so just press Ctrl+R in the HUD before you start. Each step says what should happen; if it does not, note what you saw and tell the lead.

## Codex chat git tools and permissions ([#223](https://github.com/Johnwang688/jarvis/pull/223), [#224](https://github.com/Johnwang688/jarvis/pull/224))

Already deployed; press Ctrl+R in the HUD before you start.

**Part A: Codex permissions (#224)**

1. Open a new Codex chat in `~/projects/Jarvis`. Expect: it starts. If it is refused with "Codex permission profile not applied: ...", the text after the colon names the check that failed.
2. In the chat, run `ls /mnt/c/Users/johnw/AppData`, then `cat /mnt/c/Users/johnw/.git-credentials`. Expect: both are denied.
3. Ask Codex to run `cat ~/.config/gh/hosts.yml` as a plain command. Expect: `Permission denied` and no approval card. Repeat for `~/projects/Jarvis/.env`, `~/.codex/auth.json`, `~/.docker/config.json`, `~/.claude.json`, `ls ~/.local/share/keyrings`, `~/.local/share/com.vercel.cli/auth.json` and `ls ~/.local/share/jarvis/v2`. Expect: all denied.
4. In a scratch folder (for example a chat worktree with a `package.json`), ask Codex to run `npm install left-pad` with network permission. Expect: one card saying "SANDBOX WIDENING ... network on", with no Always button. Approve it. Expect: the install reaches the registry and succeeds.
5. Ask for `cat ~/.config/gh/hosts.yml` again, with network approved. Expect: still denied.
6. Ask Codex to run `curl -sI https://example.com` as a plain command. Expect: no network and no card.
7. Ask Codex to read one of its skills, for example `cad`. Expect: it succeeds.
8. Open a Codex chat in `/mnt/c/dev/override-jarvis`. Expect: it starts and `cat README*` works. Starting can take about 2.6 s.

**Part B: Codex git tools (#223)**

Use a scratch repo you make yourself.

1. Run `gh repo create <you>/jarvis-cg-scratch --private --add-readme`. Clone it under `~/projects/` and add it as a Jarvis project. Expect: it appears as a project.
2. Open a Codex chat in that project. Ask it to make a worktree under the chat worktrees folder, commit a file there, and call `chat_git_push`. Expect: one card, "Codex wants to push <branch> to <you>/jarvis-cg-scratch", with "first push: pins origin to ..." and a `git push https://github.com/...` line, and no Always. Approve it. Expect: the branch is on GitHub, and `git log -1 --format='%an <%ae>'` shows your name and email.
3. Ask for `chat_pr_create` with a title and body. Expect: one card showing the push line and a `gh pr create --repo ... --body-file -` line. Approve it. Expect: the PR exists on GitHub with the body you asked for.
4. Ask for `chat_pr_merge` on that PR. Expect: the card starts with "merges a pull request". Approve it. Expect: the PR is merged on GitHub.
5. Ask Codex to run an escalated `git push` and an escalated `gh pr list`. Expect: both are declined with no card. The HUD shows a pointer, and `~/.local/share/jarvis/v2/decisions.jsonl` has entries with layer `chat-git`.
6. Ask for `chat_git_fetch`, then have Codex run the `git fetch <mirror> ...` line it returns. Expect: it runs with no escalation card.
7. Repeat step 2 in `/mnt/c/dev/override-jarvis`, with push and a draft PR only (`draft=true`). Then close that PR with `chat_pr_close`. Expect: the card starts with "closes a pull request".
8. Check the stored state. Expect: `stat -c %a ~/.local/share/jarvis/v2/chat-git` prints `700`; `pins.json` is `600`; `git --git-dir ~/.local/share/jarvis/v2/chat-git/<key> config --list` shows no credential settings.
9. Write the results into the work checkpoint. The PR says these are not proven by unit tests, so only what you saw here counts.

## Keyring passwords and Chrome sign-in ([#215](https://github.com/Johnwang688/jarvis/pull/215), [#217](https://github.com/Johnwang688/jarvis/pull/217))

Already deployed. Check `/status`, `browser.sync`.

**Part A: keyring (#215)**

1. If a named Browser profile is refused, the message says "Unlock the keyring in Windows Terminal (not a Jarvis terminal)". Run the unlock command in `docs/notes/browser.md`, "Password store: the owner's keyring", step 2. Expect: the next launch of that profile opens.
2. Check `/status`, `browser.sync`. Expect: `keyring: unlocked`, and with a named profile open, `password_store: gnome-libsecret`.
3. Lock-refusal check (M12): lock the keyring with the gdbus command in the same section (step 5). Open a named profile. Expect: it is refused with a 409 and the unlock hint, not opened with a weaker store. Then unlock it again (step 1).
4. Check that an existing older password (stored as `v10`) still works after the switch. Expect: it stays readable.
5. Profiles that synced passwords under the old `basic` store keep them as `v10` on disk. The notes advise deleting and recreating those profiles so everything is stored as `v11`. Decide first which profiles those are.
6. Sign in to Chrome on a keyring profile, restart the daemon, then open that profile again. Expect: it is still signed in. (The PR bodies do not spell this out; this check comes from the owner's brief.)

**Part B: Chrome sign-in tab (#217)**

1. In a Browser pane, open `chrome://settings/people` and click Chrome's own "Sign in to Chrome". Expect: Google's sign-in page opens as a tab and stays with the pane. It should not end on "does not open this kind of address".
2. Expect: the Google tab is the pane's active tab.
3. Note what the PR says it could not test: Google may refuse sign-in because the browser is automated; Chrome's sync/history confirmation is a browser dialog that may not show in the HUD; "Turn on sync" appears only after Chrome has an account. Report what happens at each of these.

## Diff tab Quick review ([#219](https://github.com/Johnwang688/jarvis/pull/219))

Needs the installed HUD built with #219. Nothing about this feature has run on a real harness yet.

1. Open a task with a diff, go to the Diff tab, and press **Quick review** in the header. Expect: the button shows a spinner and the harness being tried (Claude first).
2. Wait for the result. Expect: a panel above the diff listing each finding with its severity, `file:line` and a one-line reason.
3. Click a finding. Expect: that file opens in the diff with the cursor on that line.
4. Watch the header. Claude may not accept `claude-haiku-5-5` on your plan (the PR says this is unverified). Expect: if Claude fails, the header says why and the review moves to Codex, then to the v1 loop.
5. Press Quick review again while one is running. Expect: the review cancels.
6. Change the diff, then look at the panel. Expect: it says the diff has changed instead of showing the old result.
7. Expect: no approval card appears at any point.

## VEX view ([#229](https://github.com/Johnwang688/jarvis/pull/229))

The hardware checks H1–H10 are in [`docs/plans/2026-10-15-vex-ui-redesign.md`](2026-10-15-vex-ui-redesign.md) (the table near its end). Work through them there. The PR says the real VEX behaviour has not been run, and nothing was installed or touched on the live daemon. H4 decides whether VX-UI-4b (the brain startup reply) gets built.

## Browser pickers ([#232](https://github.com/Johnwang688/jarvis/pull/232))

Needs the installed HUD built with #232.

1. On a real page with a `<select>` dropdown, click it. Expect: a HUD overlay opens under the control, in the right place at your 150% scale and HUD zoom.
2. Click the HUD's own date or colour input. Expect: Windows Chrome's native picker opens, and the overlay does not close when it opens.
3. Use the keyboard (arrows, then Enter) in the overlay. Expect: navigation feels right and Enter picks the value.
4. On a site that checks `isTrusted` on `change`, pick a value. Expect: the site still reacts.

## Text input and IME ([#233](https://github.com/Johnwang688/jarvis/pull/233), [#235](https://github.com/Johnwang688/jarvis/pull/235))

Use a page with a text box, such as a search field, in a Remote pane on the installed HUD. The list below is #235's current version (it adds steps 6 and 7 to #233's list). The same list is in `docs/notes/browser.md` under the IME section.

1. **Microsoft Pinyin:** click the field and type `nihao`. Expect: the composition box appears where you clicked, with candidates beside it. Press Space or 1. Expect: 你好 appears once. Type a full-width comma (，). Expect: it appears once.
2. **Microsoft IME (Japanese):** type `nihon`, then Space, then Enter. Expect: 日本 appears once. Commit kana without converting. Expect: they appear once.
3. **Emoji panel (Win+.):** pick an emoji. Expect: it appears once. Do it again right after typing a letter. Expect: it appears once.
4. **Esc:** type `nihao`, then press Esc until the box is gone. Expect: nothing appears in the page. Type a letter. Expect: it appears once.
5. **Mid-composition:** type `nihao` without committing. Then each of these should put nothing in the page: clicking a chat pane; Ctrl+Alt+1; clicking back into the page. Then commit a new word. Expect: it appears once, with no leftover pinyin.
6. **Pinyin in English mode on a key-driven page** (j/k shortcuts, a web terminal): some IMEs send keyCode 229 for every key, and those keys are now dropped. Known limit from the PR: if the keys arrive only as text, the page's shortcuts will not fire. Report it if that happens.
7. **Mouse-picked candidate:** click a candidate in the IME's candidate window. Expect: it appears once. If that click blurs the HUD, the pick is cancelled instead of committed (nothing appears). Tell the lead which happened.
8. **Optional:** a dead key (´ then e) should give é once. An approval card that arrives mid-composition should put nothing in the page.

## Sub-agent activity between turns ([#234](https://github.com/Johnwang688/jarvis/pull/234), SV-6)

Already deployed (daemon restarted and HUD installed 2026-10-16); press Ctrl+R.

1. **Claude Code 2.1.295, background agent:** ask the orchestrator for a background Agent. Expect: its row turns done while the chat is idle. A line "A background task finished, and Claude Code carries on by itself." appears, followed by the CLI's own reply as its own turn. Nothing from that turn is posted to Discord or spoken.
2. **Long background flood** (L-C7, `background_flood`): run it while a second agent is working. Expect: the second agent's hook is not stalled.
3. **Codex 0.161:** a child that outlives the parent's turn shows its end without a new message. A child's approval between turns raises a card, and a message you send meanwhile waits for that card.
4. **Per-child Stop while idle:** Stop one child. Expect: confirmed at once, for both Claude and Codex.
5. **Lifecycle:** run `jarvis restart`, switch the model, and close the chat. Expect: no stray `jarvis-watch-*` threads (check with `py-spy` or the logs). During a CLI turn, the HUD queue and Stop look right.

## Sub-agent spoken notices ([#228](https://github.com/Johnwang688/jarvis/pull/228), SV-5)

The PR body has no live-check section. Its own owner steps and expected results are:

1. Already deployed; press Ctrl+R in the HUD.
2. Settings, Voice, "Sub-agent notices": check it exists for each window. Expect: on by default.
3. When a native sub-agent finishes, expect one short spoken sentence, "A sub-agent finished." or "A sub-agent failed.", with no name and no reading of the child's text. Two endings close together become one sentence, such as "Two sub-agents finished, one failed."
4. Stop one child yourself. Expect: no sentence.
5. Not yet checked, per the PR's "Not run" list: a physical speaker or Windows audio, and a live Claude or Codex child in your HUD. Report what you hear.

## Also waiting on the owner

- **Firmware brake, robot test** ([Johnwang688/87867D-override#10](https://github.com/Johnwang688/87867D-override/pull/10), open; merges only after this test). You decided 2026-10-16 that a chained move brakes before a drive or turn with a blocking `whenWithin` armed, and that the next drive keeps the chained motion's target heading. None of the current autons reach this case, so use the PR's test routine:
  1. Put the branch in its own folder (your `C:\dev\override-jarvis` has uncommitted edits): from that checkout run `git fetch origin && git worktree add ../override-brake origin/claude/brake-armed-whenwithin`, open `C:\dev\override-brake` in VS Code, and make sure `main.cpp` selects the test routine before downloading.
  2. A chained turn or drive, then `whenWithin(0, true, …)`, then a `drive`. Expect: the robot stops, waits for the mechanism, then drives.
  3. `turnTo(-45, 800, 2.0, 100, 30, 15)`, then a blocking `whenWithin`, then a `drive`. Expect: after stopping, the drive steers back toward -45° rather than driving off at the angle the turn stopped short.
  4. The same with the blocking flag off. Expect: no stop.
- **Credential file:** move `gha-deployer-key.json` out of the top level of `C:\Users\johnw` (suggested, not done).
