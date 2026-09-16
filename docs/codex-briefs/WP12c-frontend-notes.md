# WP12c — HUD frontend, the owner's first-use feedback

## Built
`lib/cron.ts` (preset ↔ 5-field cron), `lib/quota.ts` (bar math),
`lib/threads.ts` (the move reducer), `components/DirPicker.tsx`,
`components/ScheduleDialog.tsx`; `NewProject` rewritten, `Sidebar` and
`UsagePanel`/`SchedulesPanel` reworked; `PATCH /threads/{id}`, `GET /fs/dirs`,
`POST /schedules/preview` added to `api.ts` and to the mock.

1. **New project** is a `<button>` at the top of the sidebar — the old row sat
   under the projects and read as one of them. Root and access folders come
   from the dir picker (breadcrumb, `..`, double-click, typed box); a `/mnt/`
   root previews the WIN badge **before** the project exists.
2. **Drag** moves a chat thread; the drop is optimistic and reverted from the
   list captured before it, with the backend's words. A **"Move to…" menu** is
   the keyboard path — a gesture-only affordance is the same problem as the
   button nobody found. Task threads are not draggable and their menu says why.
3. **Quota** is a bar per reported window (name, percent, "resets in 3h 12m",
   steps at 70/90) plus a second, thinner **local allowance** bar. No report
   still reads "not reported", never a computed bar.
4. **Schedules** are a dialog: presets (daily / weekdays / weekly / every N) or
   an Advanced cron, re-previewed through `POST /schedules/preview` on every
   change. The same dialog edits, opening on the preset the cron came from.
   Both the empty state and the panel note that he can be asked in chat.

## Two rules worth keeping
- **The window never computes what the backend owns.** No local cron reading
  (DST), no local copy of the `/fs/dirs` root rule (it shows the 403), no
  invented quota — a reading computed here would look exactly as authoritative
  as the measured one.
- **A no-op move is identity, not a new array.** `moveThreadTo` returning the
  same list is what stops the drop target lighting up for a drop that would
  then be refused, and what stops a pointless PATCH.

## Tests
```
cd hud && npm ci && npm run build && npm test
   Test Files 10 passed (10) · Tests 99 passed (99) · ✓ built in 3.60s
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/face/hud_v2_check.py
   all 210 HUD v2 checks passed   (was 134; every earlier check still runs)
```
**Verified to bite**, three mutations built and re-run: dropping the revert on
a refused move (2 Playwright failures), shifting the colour steps to 85/95 (2
vitest), Weekdays emitting `*` instead of `1-5` (2 vitest).

Harness: **HTML5 DnD is dispatched a step at a time** (`window.__dnd`) —
`dragstart` sets the state `dragover` reads, so firing all four in one
`evaluate` tests a component that never re-rendered. A project row *toggles*
and several have been toggled by then, so there is an `expand()` helper rather
than a click and a hope. And the fixture's reset times are fixed, so one window
reads "resets now"; the countdown is checked against a pushed-forward time.

## Contract notes (nothing blocking)
- `POST /schedules` is assumed to reject a body carrying both `cron` and
  `every_s`; the dialog sends exactly one.
- `GET /fs/dirs` with no `path` is read as "start at `$HOME`", and the mock
  answers `/mnt` itself with the drive letters so the picker can walk up out of
  `/mnt/c`. Worth stating in `docs/hud-api.md` if the backend disagrees.
