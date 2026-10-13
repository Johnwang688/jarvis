# HUD Settings page implementation

Implements S1–S8 in [the owner plan](2026-10-11-hud-settings-page.md).

The titlebar gear and Ctrl+, open a page below the titlebar, with General, Model, Voice, Avatar, Usage, Routing and Archive in that order. The validated last section is a localStorage window preference, with General as the fallback when storage is absent or unavailable. General holds the existing install-as-app control. Model roster APIs/refusal text, voice/avatar selection, provider-reported usage, routing notes/decisions and HUD-only archive/trash actions retain their existing contracts. The sidebar footer keeps Schedules; the status column keeps task/approval status, Discord and Schedules.

Settings open state is separate from nested dialogs. Escape closes the catalogue or archive confirmation first; under an approval it denies only the card. Enter never confirms archive deletion or empty-trash. The page has a separate DialogBoundary; a section crash closes only Settings and keeps an existing approval answerable.

The workspace, orb, Monaco models, xterm sessions and quiet browser frames stay mounted under an inert hidden wrapper. Component drawn flags and terminal registry visibility account for coverage. Hidden work accepts no keyboard input, microphone capture, paste or editor commands; ongoing output and turns continue. Closing restores the prior connected focus target. Zoom remains available; workspace layout actions wait until Settings closes. Measured titlebar geometry keeps the page below it at both zoom extremes, and narrow windows use wrapping horizontal navigation.

Validation before independent review: TypeScript/Vite build; all 1,517 Vitest tests; focused browser coverage for sections, focus/selection, dirty buffers, ongoing turns and terminal output, nested dialogs, approval priority, hidden iframe focus, storage errors, 420px and 70/160% geometry, crash containment; migrated picker/layout checks. Browser tests use synthetic HTTP/SSE/PTYs and temporary config, not live providers or services. Lead owns final independent review, integrated full HUD sweep and merge/deployment.
