# Decisions: split panes, pane close, 6/8 layouts, thinking orb (2026-10-10)

The owner's answers for `2026-10-10-hud-split-panes-plan.md`. **Where this file and the plan disagree, this file
wins.**

## What the owner asked for

1. A small **×** at the top right of each pane when more than one pane is open, to close just that one.
2. A **one-click split** button, VS Code style, in the pane's top strip next to Chat / Diff / File / Preview. It is an
   icon styled like the title bar's panel and sidebar toggles. From one pane: click → 2, click → 3, click → 4.
3. **6-pane and 8-pane** layouts.
4. A **thinking indicator** in chats: a mini Jarvis orb spinning and flexing in place while that chat thinks, like
   Claude Code's animated logo.

Multiple chats at once is WP-B (PR #27). It is not part of this plan.

## Answered by the owner

| # | Decision | Answer |
|---|---|---|
| S-1 | How split and × step past 4 | **One pane at a time, 1–8.** Split adds a pane and × removes exactly that pane; the others reflow. 5 = 3 over 2, 6 = 3×2, 7 = 4 over 3, 8 = 4×2. The ⊞ menu also offers 6 and 8 directly. 5 and 7 are reached by split or close. |
| S-2 | What a new pane shows after a split | **A copy of the pane split from**, VS Code style. A chat gives a **new** chat in the same project. A file gives the same file, a preview the same URL, a diff the same diff. The plan settles a terminal view, a task view and an empty pane. |
| S-3 | The 3-pane shape on the split ladder | Not asked. The planner's call: **three columns** (plan D3). |
| S-4 | Where the thinking orb goes | **At the foot of the chat**, under the last message while that chat's turn runs, Claude Code style. It shows the mini orb, a status word ("Thinking…", "Running <tool>…", "Waiting for approval") and the elapsed time. It is amber for a tool or an approval and red on error, as the big orb is. |
| S-6 | Splitting a terminal pane (plan O-1, G-7) | **A new shell in the folder the source terminal was opened in**, VS Code's split terminal. At the 6-terminal cap the copy shows the picker. |
| S-7 | Splitting a task pane (plan O-2, G-8) | **That task's diff.** |
| S-8 | Splitting a file with unsaved changes (plan O-3, G-6) | **Save first.** Split is disabled with "Save or reload first to split this file". **WP-G3 (shared buffer) is not built.** |
| S-5 | Merging | **Merge on review**, the same rule as WP-A to WP-F. Merge once an independent review passes, its fixes are in and the lead's clean-worktree sweep is green. |

## Notes for builders

- Build only after PR #27 (WP-B) and PR #28 (WP-D) have merged. Both change `workspace.ts`, `Workspace.tsx` and
  `App.tsx`.
- No Ctrl+W for close: Chrome owns it.
- The approval card must stay the loudest thing on screen. Under a card, split and × are disabled like every
  title-bar and fold button, and the thinking orb never covers or imitates the card.
