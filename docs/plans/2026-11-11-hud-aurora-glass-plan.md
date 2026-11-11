# HUD aurora glass — plan (2026-11-11)

The owner asked for the HUD to move to the Aurora glass style (the `claude-ui:deep-blue`
family): frosted glass panes over a moving aurora, keeping today's glacier cyan, with the
accent and aurora colours customisable. This plan records the owner's answers, then splits
the work into four PRs.

## 1. Owner decisions (2026-11-11)

| # | Question | Answer |
|---|---|---|
| D-1 | Corner rounding | **Crisp.** `--radius-btn` stays 4px (the 2026-10-15 rule stands). Panes and cards may go to 8–10px; no pills except Send, Stop and Steer (already circles). |
| D-2 | Font | **Keep Segoe UI.** No bundled font, no CSP change. |
| D-3 | How far the glass goes | **Every pane**, not only the chrome. |
| D-4 | Customisation | **Accent + aurora colours.** No preset gallery. |
| D-5 | Presets / default | **One look:** aurora glass with today's cyan (`#6fc3df`) as the default accent. No Deep blue / Northern lights presets, no light themes. |
| D-6 | Phone | **HUD first.** The phone keeps its look; porting the tokens is a later follow-up (AG-5, not in this plan). |
| D-7 | Motion | **Full fluid.** The WebGL2 fluid simulation from the owner's Bible app (`C:\dev\bible-app\client\src\aurora\`), not a still wash or a CSS drift. |

Lead calls inside those answers (open to the owner in review):

- **L-1 Stirring.** The pointer stirs the fluid, as a finger does in the Bible app. On by default, with a setting to turn it off. A passive `pointermove` listener on `window` only: it never calls `preventDefault`, never takes focus, and gets nothing while the pointer is over a Browser frame (the frame owns those events).
- **L-2 Fallback style.** The Bible app's lighter flow shader is kept as the fallback for no-GPU machines (and as a setting). Reduced motion shows one still frame.
- **L-3 Cool text.** The warm cream text tokens move to cool neutrals so they sit right over a cool aurora. Every text/background pair stays at WCAG AA (the Graphite rule).
- **L-4 Inbox row.** The owner Inbox row keeps its own aurora band and sparkle (2026-10-25 exception). AG-1's browser review checks that it still stands out over the moving background.

## 2. What stays fixed

- **Meanings.** Amber = a tool running / an authorisation pending; red = an error; green = done. Their values do not change, and **no custom colour may enter the amber/red band** (§4, AG-3). This is a tighten-only boundary: approval cards must never be repainted (Safety design: "avatars cannot repaint pending/error meaning").
- **Layout.** No pane, title bar or input changes size. 1px borders become 1px rims, so dimensions hold. Class names the suites assert on (`droptarget`, `cur`, `done`, `ok`, `warn`, `high`), the avatar's `pointer-events:none`, and no `text-transform` on `.phase` / `.tool-group` all survive.
- **Focus and frames.** The canvas is `pointer-events:none`, `aria-hidden`, never focusable, and sits under every pane. Nothing about the "No Browser frame takes the keyboard" rule changes.
- **Browser behaviour.** Only the chrome around Browser panes is restyled. No Browser feature work (the 2026-10-14 freeze), and framed page content keeps its own background.
- **Fonts and CSP.** `font-src 'self' data:` is unchanged. WebGL2 needs no CSP change.

## 3. Rendering design (the hard part)

"Every pane" plus a 60 fps aurora is the performance risk. Real `backdrop-filter: blur()` on up to 12 panes over a canvas that changes every frame makes the compositor re-blur each pane every frame. The owner's PC has Intel integrated graphics (the Bible app's perf notes), and the HUD window is far bigger than a phone viewport.

**Default: pre-softened field, no per-pane blur.**

1. The fluid renders at half resolution, as in the Bible app. A final cheap blur pass in the shader makes the field already "frosted".
2. Panes get the glass paint without `backdrop-filter`:
   - a translucent tint (`--glass-pane`) over the base colour,
   - the white sheen gradient,
   - a 1px rim (`--rim`),
   - the inset highlight shadow (`--glass-shadow`).
   Because the field is already soft, this reads as frosted glass at near-zero compositor cost.
3. Real `backdrop-filter` is used only on floating layers that sit over **sharp** content: menus, pickers, popovers, modals, approval cards, toasts. They are few, and rarely more than one is open.
4. **Dense text surfaces** (Monaco, xterm, chat transcript, diff) use the more opaque `--glass-solid` tint. Monaco gets a translucent `editor.background` built from that tint (`lib/editorTheme.ts` already reads `:root`). xterm 6 (DOM renderer) uses `allowTransparency: true` with an rgba background.
5. **Legibility rule.** The pane tint alpha is chosen so that `--text` and `--text-muted` pass 4.5:1 composited over the **brightest** aurora colour at full strength. Because the aurora colours are customisable, AG-3 enforces this at pick time (§4).

AG-2 measures real per-pane `backdrop-filter` too. If it holds the budget at 12 panes on the owner's PC, the lead may switch panes to real blur. That would be recorded here, not assumed.

**Performance budget** (AG-2 measures it in Windows Chrome, profile Default, at the owner's real HUD window size, 12 panes open, chat streaming, terminal output running):

- 60 fps aurora with a GPU; main-thread time ≤ 4 ms per frame for the aurora.
- No typing latency regression in Monaco or the chat input (measured before and after).
- No GPU: flow shader at 30 fps (the Bible app's `SOFTWARE_MAX_FPS`).
- Paused while `document.hidden`. GPU context loss shows the still CSS gradient until the context returns.
- Reduced motion: one still frame.

## 4. Customisation

Settings → **Appearance** (a new section, next to Click sparkle):

- **Accent**: one colour. Every other accent token (`--accent-hi`, `-soft`, `-line`, `--focus-ring`, `::selection`, `--syn-fn`, the orb palette, the xterm cursor and cyan) is **derived** from it in OKLCH, so one pick stays coherent.
- **Aurora**: three colours (the fluid's dye colours) plus the base colour under them.
- **Style**: Fluid (default) / Flow / Still.
- **Stir with the pointer**: on/off (L-1).
- **Reset to default** (cyan accent, default aurora).

Rules (unit-tested in `lib/appearance.test.ts`, mutation-checked):

- **Input is `#rrggbb` only.** Anything else is refused before it reaches `style.setProperty`, so no `url()`, `var()` or other CSS ever enters a custom property.
- **Accent hue guard.** In OKLCH, the accent may not enter the amber/orange/red band, or come within a tested distance of `--amber` and `--red`. A refused pick explains why and keeps the last good value. Picks are refused, never silently shifted, so the owner always sees what was applied.
- **Contrast guard.**
  - The accent must reach 3:1 against the glass pane composite (it is used for focus, links and the selection bar).
  - Each aurora colour is capped in lightness so the §3 legibility rule holds. Too-bright picks are clamped, and the swatch shows the clamped value.
- **Live apply.** A change re-themes Monaco, xterm and the orb without a reload.
- **Storage.** Saved in `localStorage` `jarvis.hud.appearance` (behind try/catch, like `jarvis.hud.editor`); a corrupt record falls back to the default. It is a per-window convenience: nothing is sent to the daemon, and nothing in it is trusted.

## 5. Work packages

| PR | Scope | Builder → verifier | Size |
|---|---|---|---|
| **AG-1 Glass tokens + static aurora** | New tokens in `theme.css`: `--base`, `--wash-1..3` (new names; `--aurora-*` stays the inbox row's), `--glass-pane`, `--glass-solid`, `--glass-sheen`, `--rim`, `--glass-shadow`, `--blur`, cool text. Every pane and the chrome get the glass paint; floating layers get real blur with the `@supports` fallback. A still CSS aurora wash behind `#root`. Monaco/xterm transparency. Terminal's hard-coded hexes ([Terminal.tsx:92](../../hud/src/components/Terminal.tsx)), `BootOverlay` and `annotate.ts` read tokens instead. `aurora.test.ts`, `editorTheme.test.ts`, `orbpalette.test.ts` and `vexTheme.test.ts` recompute contrast against the glass composites. Browser review with screenshots, including the inbox row (L-4). | Sonnet → Sol | ~500–700 changed CSS lines, ~150 TS |
| **AG-2 Fluid aurora** | Port `fluid.web.ts`, `renderer.web.ts`, `gl.web.ts`, `controller.web.ts` and `loop.ts` (~1,270 lines, the owner's own code) into `hud/src/aurora/`, dropping the React Native parts. A full-window half-res canvas under `#root` content. Pointer stirring (L-1), flow fallback (L-2), hidden-pause, context-loss still, reduced-motion still frame, 30 fps software cap. Lifecycle tests ported from the Bible app's suite. The §3 perf budget measured and recorded here. | Sonnet (port) → Sol; the lead runs the perf measurement | ~1,300 TS + ~600 tests |
| **AG-3 Appearance settings** | `lib/appearance.ts` (validation, OKLCH derivation, hue and contrast guards, storage) and `components/AppearanceSettings.tsx`. Live re-theme of Monaco, xterm and the orb. Tests for every §4 rule, with negative cases and a mutation check on the hue guard (the boundary piece). | Sonnet → Sol | ~400 TS + ~300 tests |
| **AG-4 Docs** | CLAUDE.md's "The HUD uses **Graphite**…" sentence becomes the aurora glass rule (cyan default, customisable accent, fixed amber/red). Update `docs/notes/hud-workspace.md` and the `theme.css` header comment. Before merging, grep `tests/face/*` and `hud/src` for pinned "Graphite" text (seven `tests/face` checks and `views.test.ts`, `monaco.ts`, `VexView.tsx` mention it). | Haiku → Sol | small |

AG-1 → AG-2 → AG-3 run in order. AG-4 can land with AG-3. Each PR follows the usual workflow:
worktree build, `systemd-run` memory caps, isolated mock-server browser tests (never the live
daemon), then lead review before merge. After each merge, the standard post-merge steps run:
`jarvis hud install` and restart, then the owner presses Ctrl+R.

## 6. Risks

- **Integrated-GPU cost at HUD size.** Half-res rendering helps, but the HUD window can be 4K-class. Mitigations: AG-2 caps the simulation grid by area, not just by scale, and Flow and Still are one setting away.
- **Readability over a bright custom aurora.** Handled by the lightness clamp (§4). The owner's review is the real test.
- **Monaco transparency edge cases** (minimap, sticky scroll, overview ruler, suggest widget). AG-1 sets each explicitly and checks it in the browser review.
- **Tests pinning Graphite text** (memory: docs text is tested). Listed in AG-4.

## 7. Out of scope

Phone theming (AG-5 later), light themes, the other Bible presets, bundled fonts, per-project themes.
