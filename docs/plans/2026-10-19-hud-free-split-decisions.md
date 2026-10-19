# Decisions: directional split (2026-10-19)

The owner's request and answers. **Where this file and the plan
(`2026-10-19-hud-free-split-plan.md`) disagree, this file wins.** It replaces
decision S-1 of `2026-10-10-hud-split-panes-decisions.md` ("one pane at a time,
1–8", the split ladder). Everything else there (S-2 copy-of-source, S-6
terminal split, S-7 task → diff, S-8 save-first, no Ctrl+W, approval card
loudest) still holds.

## What the owner asked for

> Keep the current fixed layouts, but I want to change the split behavior. When I
> press the split button it just transitions to the next number right now. Can we
> change it so that I can split up/down or left/right with a max width of 4 panels
> across and 3 panels up/down. Also when I split it shouldn't resize the window it
> should just split the panel in half.

## Answered by the owner (2026-10-19)

| # | Decision | Answer |
|---|---|---|
| FS-1 | Total pane limit | **Raise to 12.** A full 4×3 grid is reachable. Pane ids, persistence and every 1–8 assumption grow to 12. |
| FS-2 | How the direction is picked | **Two buttons** in the pane's top strip: "split right" and "split down". Always visible, one click each. |
| FS-3 | How 4 across / 3 down is counted | **Across the whole window.** No horizontal line through the centre crosses more than 4 panes; no vertical line more than 3. A split that would break that is disabled, and its tooltip says why. |
| FS-4 | A split whose halves would be under the minimum pane size (360 × 200 px) at the current window size | **Disabled**, with a tooltip ("Not enough room to split right/down"). Nothing else moves. |
| FS-5 | What a split does to the layout | **Halves the pane that was split; no other pane moves or resizes.** (Owner's words: "it shouldn't resize the window, it should just split the panel in half.") |
| FS-6 | The fixed layouts | **Kept.** ⊞ still offers its presets; choosing one lays the panes out in that shape, as today. |
| FS-7 | (plan O-1) Split right with no room, e.g. 1920 px with both side panes open, where a third column would be 342 px | **Disabled**, with the tooltip naming widen / fold a side pane / zoom out. A split never borrows from the side panes. |
| FS-8 | (plan O-3) ⊞ after a custom layout | **No memory.** A preset replaces the custom shape. The panes come back in order, at the preset's default sizes. |
| FS-9 | (plan O-2) Focus keys for panes 9–12 | **Only Ctrl+Alt+1–8 have keys.** Panes 9–12 are click-only. This overrides the plan's §6 default: no focus9/focus10 and no Ctrl+Alt+0. |
| FS-10 | (plan O-5) Merge rule | **Merge on review.** Each FS PR merges once its lead review and full sweep pass. |

## Lead's defaults (not asked; reversible, say if wrong)

- **Close (×)** gives the closed pane's space to its neighbour in the same split, VS Code style. The other panes keep their sizes.
- Every split edge stays draggable, and a double-click equalises that split, as edges do today.
- A stored workspace from before this change opens as the same layout it drew (a migration, no reset).
- The terminal cap (6) is unchanged by FS-1. At the cap, a split of a terminal shows the picker (S-6).
- FS-3 is counted geometrically, with drags stopping at the cap (plan O-4, the owner's words).
