# HUD: one Settings page (2026-10-11)

## The owner's request

The owner sent a screenshot of the title bar's Model · Voice · Avatar · Settings buttons and the sidebar foot's Schedules / Usage / Route / Archive, and wrote:

> "can you make a proper settings page instead of these pills at the top? have it open with the settings icon. move the usage route and archive into settings as well. schedules can stay on the right sidebar"

## Today (main at 57f33d6)

- **The title bar** (`components/Layout.tsx`, `TitleBar`, ~l.737) holds four buttons, Model, Voice, Avatar and Settings, which fold into a `⋯` menu when narrow.
  - Each opens its own dialog from `components/Pickers.tsx`: `ModelPicker`, `VoicePicker`, `AvatarPicker`, `SettingsDialog`.
  - `SettingsDialog` today shows only the read-only routing table.
- **The sidebar foot** (`components/Sidebar.tsx` ~l.575) has four rows:
  - `open-schedules`, `open-usage` and `open-route`, which open the status column's sections;
  - `open-archive`, which opens `ArchiveView`.
- **The status column** (right pane, `App.tsx` ~l.2231) draws `UsagePanel` (the subscription meters), `SchedulesButton`, `DecisionsLog` (the last routing decisions) and `DiscordPanel`.
- **Settings other plans already expect:**
  - ED-8, editor settings;
  - the Browser plan's Settings → Browser: auto-open dev servers, sound, "Agents may drive";
  - the peers plan's Settings page: peer traffic, the kill switch.

  So the page needs sections that later packages can add to.

## Decisions (lead's, 2026-10-11; the owner can redirect)

| # | Decision |
|---|---|
| S-1 | **Settings is a page, not a dialog.** It covers the window below the title bar, with a section list on the left and the section on the right, like the Claude and VS Code settings pages. The workspace underneath is hidden, **never unmounted**, so chats, terminals, unsaved edits and turns carry on untouched. × or Escape closes it, back to exactly where the owner was. |
| S-2 | **One ⚙ icon button in the title bar** (`aria-label="Settings"`, `data-testid="titlebar-settings"`) replaces the four buttons, where they were. The `⋯` overflow menu goes, since one icon always fits. **Ctrl+,** also opens and closes it. It is the HUD's everywhere, a terminal included; Ctrl+, sends nothing a shell uses. It is inert under a card and on auto-repeat. |
| S-3 | **Sections**, in order: **General** · **Model** · **Voice** · **Avatar** · **Usage** · **Routing** · **Archive**. Later packages add Editor, Terminal, Browser and Peers as sections, never as new title-bar buttons. The section is a pure value (`lib/settings.ts`) remembered per window in localStorage behind try/catch. Opening with nothing remembered shows General. |
| S-4 | **What moves where:** <ul><li>The Model, Voice and Avatar pickers become their sections, reusing the existing components' bodies. Behaviour and routes are unchanged, including every rule in CLAUDE.md's *Model selector* notes: unpin refusals shown in their own words, `RosterRefused` and so on.</li><li>Usage takes `UsagePanel`'s meters.</li><li>Routing takes the routing table, its notes and `DecisionsLog`.</li><li>Archive takes `ArchiveView`, with restore and HUD-only delete unchanged.</li><li>General takes the dictation default, wake word and other window-wide switches that already exist, if any; otherwise it is the home for ED-8 and Browser.</li></ul> |
| S-5 | **The sidebar foot keeps only Schedules.** The Usage, Route and Archive rows go. The status column keeps `SchedulesButton` and `DiscordPanel`; the usage meters and decisions log move to Settings. If the owner wants a glanceable meter kept in the column, that is a one-line follow-up. |
| S-6 | **Safety is unchanged.** <ul><li>Under an approval card the page is inert like everything else: the card takes focus, Escape denies and never closes Settings, and ⚙ and Ctrl+, are disabled.</li><li>A render error inside a section is caught by `DialogBoundary` and closes only Settings.</li><li>Nothing a model writes is drawn with `innerHTML`.</li><li>The page sits under `#authveil`, so the card stays topmost at 70% and 160%.</li></ul> |
| S-7 | **Zoom:** every size divides by `--ui-zoom` (the `vh`/`vw` rule). The page works at 70–160% and in a 420px-wide window: the section list becomes a row of tabs above the section when narrow. |
| S-8 | **Test ids:** the old opener ids (`open-usage`, `open-route`, `open-archive`, the title-bar picker buttons) are removed. Every suite helper that opened a picker opens Settings and its section instead, through one helper in `hud_v2_check.py` (`open_settings(page, section)`). Ids inside each picker stay, so the checks' bodies are untouched. |

## Order

This starts **after PR #37 (WP-G1, eight panes) merges**, because both rewrite `TitleBar` and the HUD suites' helpers. The Preview → Browser rename (BR-3a) lands right after G1 as well. Whichever of the two starts second merges the other first. The conflicts are test-id renames found by grep.
