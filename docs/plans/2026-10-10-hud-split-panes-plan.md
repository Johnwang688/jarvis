# Plan: split and close panes, up to eight, and a thinking orb per chat (Jarvis v2 HUD)

_Planner's proposal, written read-only against PR #27 (WP-B, head feb61ff) and
PR #28 (WP-D, `origin/feat/hud-terminal-panel` 32e9bc1), for the state after
both merge. Where `2026-10-10-hud-split-panes-decisions.md` disagrees, the
decisions file wins. Built only after #27 and #28 are on `main`._

## For the owner: what this gives you

- **A split button in every pane's top strip** (a 16px window outline with a
  divider, styled like ◧ ⬓ ◨). Each click adds a copy of that pane beside it:
  1 → 2 → 3 → 4 → … → 8 panes.
- **A × at the top right of every pane** while more than one is drawn. It
  closes exactly that pane, and the others reflow. A chat keeps running; a
  terminal goes back to the panel; an unsaved edit asks first.
- **Six and eight panes** (3×2, 4×2) in the ⊞ menu; 5 (3 over 2) and 7 (4 over
  3) come from split and close.
- **A thinking line at the foot of every chat** while its turn runs: a mini
  orb that spins and flexes, then "Thinking…", "Running ls…", "Waiting for
  approval" or "Waiting for your answer", then the elapsed time. It works for
  turns started from Discord, in a pane that is not the selected chat, and
  after you close and reopen a pane.

```
┌ pane 1 · chat ─────────────────────── ◫ ×┬ pane 2 · file ──────────── ◫ ×┐
│ chat task file diff preview terminal     │ … src/app.ts                  │
│ …replies…                                │                               │
│ ◌ Running pytest… · 42s                  │                               │
│ [ input bar ]                            │                               │
```

## 1. What the plan builds on (after #27 and #28)

- **Panes are positions.** `panesOf(preset)` is `PANE_NOS.slice(0, n)`, so pane
  N is always slot N. Four specs are stored (`Panes` is a 4-tuple). Each
  preset has one `cols` edge list and one `row` edge (`Split`). `SHAPES` holds
  static CSS grid areas. `dropColumn` and `dropRow` are hand-written per preset.
  The sticky `DrawnSet` compares against `panesOf(ws.preset)`.
- **WP-B** keeps `chats: Record<PaneNo, ChatState>` (`emptyChats()` is a
  `{1..4}` literal), `selectedChat` and `selectedOrder` (W-6), drafts parked by
  `draftKey`, `heldBack` give-backs in `App`, and **one thread in at most one
  pane** (`heldElsewhere` swaps conversations). A pane that is hidden keeps its
  conversation. `turn_started` marks the panes showing the thread busy.
  **Opening a thread whose turn is already running does not mark the pane
  busy**: there is no Stop after a reload or a reopen.
- **WP-D** places a terminal by `placeTerminals(panes, drawn)`: the first drawn
  pane holding it, else the panel. The xterm view is moved, never rebuilt.
  There are at most 6 terminals. `specOf(id)` remembers where each was opened,
  and `setReadable` exists. The panel labels say "in pane N".
- **FileTab** keeps the open file in component state. Each `CodeEditor` builds
  its own Monaco model. Saves are guarded by mtime (a 409 says "changed on
  disk"). A dirty pane refuses to switch away, with a chip.
- **Orb.tsx** is a 60 fps canvas, the push-to-talk button, with ids `orb`,
  `orbdock` and `orbstatus`. `palette()` and `ACCENTABLE` are private to it.
- **Hard-coded four**, all of which this plan changes:
  - `workspace.ts`: `PaneNo`, `PANE_NOS`, `DEFAULT_VIEWS`, `Panes`, `panesOf`,
    the drop tables;
  - `chats.ts`: `emptyChats`, `besideTarget`, and the `New thread · ${n}`
    label;
  - `App.tsx`: `perPane()`;
  - `Workspace.tsx`: `[1, 2, 3, 4]` and `slotColumn`;
  - `layout.ts` and `Layout.tsx`: `focus1…4`;
  - `Terminal.tsx`: "in pane N";
  - tests: `chats.test.ts` `none`, and PR #28's `panes.map(...)` 4-arrays in
    `workspace.test.ts`.

## 2. Decisions

D1–D4 are the owner's (2026-10-10). The G and H rows are the planner's; each
gives its alternative.

| # | Decision | Answer | Alternative |
|---|---|---|---|
| D1 | Split and × past 4 | **One pane at a time, 1–8.** 5 = 3 over 2, 6 = 3×2, 7 = 4 over 3, 8 = 4×2. ⊞ offers single, cols2, rows2, cols3, main2, grid4, grid6 and grid8. | (rejected) an even ladder with empty slots |
| D2 | What a split shows | **A copy of the pane split from**, VS Code style (table in §3.5) | a new chat; the slot's stored spec |
| D3 | The ladder's 3-pane shape | **cols3.** The new pane lands beside its source, which is what VS Code's split does. main2 stays one ⊞ click away. Cost: cols3 needs a 1080px centre, so at 1366 wide the status pane folds for the render. | main2: needs only 840 |
| D4 | Where the thinking orb sits | **At the foot of the chat**: mini orb · word · elapsed. Amber for a tool or an approval, red on error. | the pane's top strip |
| G-1 | Pane identity | **Ids are stable; slots are positions.** A pane keeps its id (`pane-<id>`, React key, `chats[id]`, dirty flag) for life. `ws.order` lists the ids in reading order. Every number the owner reads is the **slot** (`slotOf`): "New thread · 2", "in pane 2", aria labels and Ctrl+Alt+N. | Renumber on close. That moves `chats`, dirty flags and refs, and changes React keys, which remounts Monaco, iframes and FileTab, so an unsaved edit would be lost. Rejected. |
| G-2 | ⊞ to fewer panes vs × | **⊞ hides; × discards.** ⊞ keeps WP-A's promise that 4 → 1 → 4 brings back what each pane showed (`kept`). × resets the pane: it is unmounted and its spec goes back to the default. | ⊞ closes too, which loses WP-A's restore |
| G-3 | Shape after split or close | **Always the ladder shape for the new count** (cols2, cols3, grid4, grid5, grid6, grid7, grid8). rows2 and main2 are ⊞-only. Closing from main2 gives cols2, and splitting from rows2 gives cols3. | Keep the "family" (rows2 + split → main2). More cases, more surprises. |
| G-4 | Where the new pane goes | **Right after its source, in reading order**, and it takes focus. Some panes move (grid4 → grid5 reflows the rows). The focus accent marks the new pane. | Always last: the source's neighbour moves less, but the copy can land far away |
| G-5 | When split is disabled | At 8; under a card; when **the next shape would not be drawn whole** in this window even after the side panes and panel fold (`fitWorkspace(...).dropped > 0`); and when every free id is a hidden pane holding an unsaved edit | Allow it and let the window drop panes. Rejected: a button whose result you cannot see. |
| G-6 | A copy of a dirty File pane | **Interim (WP-G2): split is disabled** ("Save or reload first to split this file"). Clean copies open from disk, and a save in one pane reloads the other clean panes on that file. **Target (WP-G3, owner's call O-3): one shared buffer**, so both panes show the same unsaved edit. | The copy opens from disk beside a dirty source. The mtime guard prevents a clobber, but two panes would disagree on screen. |
| G-7 | Terminal copy | **A new terminal in the folder the source terminal was opened in** (`specOf`), VS Code's split terminal. If the source cannot be read by Jarvis (WP-F switch off), the copy is off too. At the 6-terminal cap the copy shows the picker with the daemon's sentence and never retries. Owner's call O-1. | The terminal picker, which spawns nothing |
| G-8 | Task pane copy | **A Diff pane of the same task.** Task views follow the window's selected task and have no per-pane state, so an exact copy is a mirror. Owner's call O-2. | A second, identical task view |
| G-9 | × on a composing chat with unsent words | **Asks** ("Discard the unsent message?"): those words belong to no thread, so they cannot be parked. Words in an existing thread's box are parked under the thread and come back when it reopens, so that × does not ask. | Discard silently |
| G-10 | Keys | **Ctrl+Alt+1…8 focus slot N** (WP-A's rule extended; AltGr+digit reports a symbol, so it never fires mid-word). **No key for split or close**: Ctrl+W and Ctrl+F4 are Chrome's, and Ctrl+\\ is SIGQUIT in a terminal. | — |
| H-1 | Turn state for the line | **Keyed by thread, window-wide** (`State.turns`), fed by the turn events *and* the daemon's activity records. It follows the thread across swaps, close and reopen, Discord-started turns and reloads. | Per pane only. Misses Discord turns started before the pane opened the thread, and reopened threads. |
| H-2 | Drawing | **An inline SVG with CSS animations** (transform only, on the compositor). No canvas and no requestAnimationFrame. It shares `palette()` with the big orb. | Orb.tsx at 18px: 8 canvases at 60 fps, and duplicate PTT ids |
| H-3 | A running turn the pane does not track | **The pane adopts it** (busy, `turnThreadId`), so Stop returns after a close, reopen or reload. The 15 s reconcile undoes a wrong adoption. | Leave WP-B's gap |

## 3. Panes (WP-G)

### 3.1 Data

```ts
export type PaneNo = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;
export const PANE_NOS: readonly PaneNo[] = [1, 2, 3, 4, 5, 6, 7, 8];
export type Preset = "single" | "cols2" | "rows2" | "cols3" | "main2" | "grid4"
                   | "grid5" | "grid6" | "grid7" | "grid8";
export const MENU_PRESETS: readonly Preset[] =               // ⊞ tiles, 4 per row
  ["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid6", "grid8"];
export const LADDER: Record<number, Preset> =
  { 1: "single", 2: "cols2", 3: "cols3", 4: "grid4", 5: "grid5", 6: "grid6", 7: "grid7", 8: "grid8" };

interface PaneSpec { view: View; projectId: string | null; terminalId: string | null;
                     previewUrl?: string; filePath?: string /* file & diff: the open file */ }
interface Split { cols: number[]; row: number; bottom?: number[] /* grid5, grid7 only */ }
interface Workspace {
  preset: Preset;               // SHAPES[preset].panes === order.length, always
  order: PaneNo[];              // the panes the preset draws, in reading order (identity ≠ slot)
  kept: PaneNo[];               // hidden by ⊞, spec kept, restorable; disjoint from order
  panes: PaneSpec[];            // length 8, by id
  splits: Record<Preset, Split>; focused: PaneNo /* ∈ order */; panel: { open: boolean; height: number };
}
```

| Preset | Panes | Rows (panes per row) | Centre minW | minH | In ⊞ | Ladder |
|---|---|---|---|---|---|---|
| single | 1 | [1] | 480 | 200 | yes | 1 |
| cols2 | 2 | [2] | 720 | 200 | yes | 2 |
| rows2 | 2 | [1,1] | 480 | 400 | yes | — |
| cols3 | 3 | [3] | 1080 | 200 | yes | 3 |
| main2 | 3 | (column-shaped, as now) | 840 | 400 | yes | — |
| grid4 | 4 | [2,2] | 720 | 400 | yes | 4 |
| grid5 | 5 | [3,2] | 1080 | 400 | no | 5 |
| grid6 | 6 | [3,3] | 1080 | 400 | yes | 6 |
| grid7 | 7 | [4,3] | 1440 | 400 | no | 7 |
| grid8 | 8 | [4,4] | 1440 | 400 | yes | 8 |

`ShapeSpec.areas` is replaced by `rows: number[] | null` (null for main2).
`DEFAULT_VIEWS` for 5–8 is `chat`.

**Edges.**
- The uniform grids (grid4, grid6, grid8) keep WP-A's model: one set of column
  edges spanning both rows (`split-col-1…3`) and one row edge (`split-row`).
- grid5 and grid7 cannot align their columns, so their bottom row has its own
  edges (`Split.bottom`, separators `split-colb-1…2`, spanning the bottom row
  only). The top row's `split-col-*` span the top row only.
- Defaults: grid5 is `{cols: [⅓, ⅔], bottom: [½], row: ½}` and grid7 is
  `{cols: [¼, ½, ¾], bottom: [⅓, ⅔], row: ½}`.
- A new pure `gridTemplate(shape, fittedSplit)` returns `{columns: fr[], rows:
  fr[], areas: string[]}`. For an uneven shape it takes the union of the
  top-row and bottom-row edges as unit columns, and each cell spans the units
  between its own edges. For example, edges ⅓, ⅔ over ½ give units ⅓ ⅙ ⅙ ⅓
  and areas `"s1 s2 s2 s3" / "s4 s4 s5 s5"`. Coincident edges merge.
- `Workspace.tsx` draws from it in place of the static areas. `clampCols`
  applies per row. `info().width` (which feeds `narrow` and `compact`) reads
  the slot's own row.

### 3.2 Storage (`jarvis.hud.workspace`, same key, additive)

`parseWorkspace` reads an old value exactly as before. Every check below
falls back on its own:
- No valid `order` → `PANE_NOS.slice(0, SHAPES[preset].panes)`. So every
  stored WP-A/WP-B value draws the same panes.
- `order` must hold distinct valid ids, as many as the preset draws.
- `kept` keeps only distinct ids that are not in `order`.
- `panes` is padded to 8 with the defaults.
- `focused` must be in `order`, else `order[0]`.
- `filePath` is a string of at most 1024 characters.
- `bottom` must have the right length and be strictly increasing, else the
  default.
- An unknown preset still reads as `single`.
- A rolled-back HUD ignores `order` and `kept`, and an unknown preset reads
  as single, so that direction is tolerant too.

### 3.3 Fitting

`fitWorkspace` keeps WP-A's order of operations:
- **Across:** side panes give back slack, fold right, fold left, and only
  then drop columns.
- **Down:** the panel shrinks, then folds, and only then are rows dropped.

It keeps the sticky dropped set (compared against `ws.order` now), and **the
focused pane is never dropped**. The per-preset drop tables become generic
over `rows`:
- **Drop a column** (to t = widest row − 1): every row keeps a contiguous
  window of `min(len, t)` panes. The focused pane's row keeps the leftmost
  window that contains it, and the other rows keep the window at the same
  start, clamped. So [4,4] and [4,3] → [3,3]; [3,3] and [3,2] → [2,2];
  [2,2] → [1,1] (rows2); [3] → [2] → [1].
- **Drop a row:** keep the focused pane's row. A 4-wide row also loses a
  column (→ cols3), so there is no drawn-only cols4 shape.
- main2 keeps its hand-written cases.
- WP-A's `dropColumn` and `dropRow` test expectations for cols3, cols2, main2
  and grid4 must hold unchanged.

**Real windows** (100% zoom unless noted; app-window inner height ≈ screen −
80; PANE_MIN 360 × 200):

| Window | grid4 | grid6 / grid5 | grid8 / grid7 |
|---|---|---|---|
| 1920×1080 | default side panes | default side panes, columns ≈456 | side panes squeezed to ≈480 together, columns **360** (the minimum) |
| 1366×768 | fits | the status pane folds for the render | 6 of 8, status pane folded, badge |
| 1280×800 | fits (728) | both side panes fold (1208) | **6 of 8**, both rails, badge |
| 1280×800 @150% (853×≈450) | both rails; an open panel folds for the render | 4 of 6 | 4 of 8 |
| 1280×800 @80% (1600 wide) | fits | fits | **fits**, both rails (1528 ≥ 1440) |

So eight panes need a window about 1512 px wide at 100% (4 × 360 + two
rails). Zooming out is the lever on a laptop, and the split tooltip says so.
`PANE_MIN_W` stays 360. Under it, the chat chips, the view strip and the file
tree stop fitting (COMPACT_W 520 and NARROW_W 560 already fold the strip and
the tree).

### 3.4 Split

**The buttons.**
- `pane-<id>-split` sits right after the view words (or the `view ▾`
  select) in every pane's strip, **in the single layout too**, because that
  is how you get to two.
- It is an `AreaIcon` variant `"split"`: outline plus one vertical divider,
  16×16. The `.tb-icon` rules are lifted off `#titlebar` so pane buttons share
  them (`--text-dim`, `--text` on hover, the accent ring only on focus).
- It has `aria-label="Split pane"`, and its title is the verdict sentence
  below.
- Space on it activates it and is never push-to-talk.

**The pure functions** (`workspace.ts`):
- `splitVerdict(layout, ws, available, availableH, prefer, preferPanel) →
  {ok, why: "" | "max" | "room" | "hidden-edit"}`. "room" means a trial
  `fitWorkspace` of the split result returns `dropped > 0`. The verdict
  depends only on the count, so every pane's button shares it.
- `allocate(ws, dirtyIds)`:
  - the lowest id in neither `order` nor `kept`;
  - else the oldest `kept` id that holds no unsaved edit, which is closed
    first (§3.6);
  - else none, which gives `hidden-edit`.
- `splitPane(ws, source, id, spec)` inserts `id` after `source` in `order`,
  sets `preset = LADDER[n + 1]`, `focused = id` and `panes[id] = spec`, and
  removes `id` from `kept`.

**The tooltips:**
- ok: "Split: a copy of this pane beside it"
- max: "Eight panes is the most"
- room: "No room for N panes in this window: widen it, zoom out (Ctrl+-) or
  close a pane"
- hidden-edit: "A hidden pane has an unsaved edit: show it from ⊞ first"
- a dirty File source (G-6 interim): "Save or reload first to split this
  file"

The button is disabled under a card (`blocked`), as every layout button is.
The veil's `inert` covers the keyboard as well.

**A dropped set.** The verdict is about the next shape, so a window already
dropping panes can still split when the next shape fits. For example, cols3
drawn as 2 in a 900px window can split to grid4 (720 wide), which draws all
four.

### 3.5 What the copy shows (D2)

| Source | The new pane |
|---|---|
| chat (thread or compose) | **a new compose row in the source's project** (`chatProjectId`, else the last project). WP-B allows one thread in one pane only. Provider, model and effort start as "+ New thread" starts them. The compose row is written to `chats[id]` *before* the layout changes, so the `emptyChatPanes` effect does not put it in the last project. |
| file | the same pinned project and the same file (`filePath`, read from disk on mount). The source must be clean (G-6). |
| preview | the same URL (judged again before it loads, as now) and the same pinned project. WP-E's keep-origin toggle is **never** copied. |
| diff | diff, for the same task (window-wide), with the same open file (`filePath`) |
| task | a **diff** of the same task (G-8) |
| terminal | `terminals.create(specOf(src) ?? defaultIn(), id)`, then `setReadable(new, false)` if the source's switch is off. Refused at the cap: the pane shows the picker with `mgr.error`. "The folder it was opened in" is the start folder: a later `cd` is not tracked. |
| empty (no project, no URL, terminal picker, diff with no task) | the same empty view |

`FileTab` gains `filePath`:
- `onOpened(pid, path)` writes it.
- It reads the file on mount when set.
- The same-file broadcast (`fileSaved(pid, path, mtime)` within the window)
  makes a clean pane on that file re-read it. A dirty one gets the existing
  "changed on disk" note.

`DiffTab` seeds `sel` from `filePath` when the path is in the diff. A side
effect: a File pane reopens its file after a reload.

"Open beside" (`besideTarget`) keeps WP-B's results. Its two "grow" cases
(single → cols2, cols2-last → cols3) become `splitPane` with the thread's
chat as the spec, not a copy.

### 3.6 Close

**When it shows.** `pane-<id>-close` sits at the right end of the strip, only
when `fit.panes.length > 1`, and is disabled under a card.
- A chat's title is "Close this pane. The thread keeps running and stays in
  the sidebar". Otherwise it is "Close this pane".
- There is no key (G-10).

**It asks first** in an inline strip in the pane (`pane-<id>-close-ask`, plain
`Discard and close` / `Keep` buttons, never a modal, never a card look-alike)
when:
- a File pane holds an unsaved edit (with G3: only if it is the buffer's
  last view);
- a composing chat has unsent words, files or handed-back words (G-9).

**Then, in this order** (`App.closePane(id)`):
1. **Chat.**
   - Park `input`/`files` under `draftKey` (thread) in `State.drafts`.
   - Move every `restore` item to `heldBack.hold(thread)`.
   - Clear `pendingThread[id]`.
   - Dispatch `release_chat`: `chats[id] = emptyChat()`, and the id leaves
     `selectedOrder`.
   - The turn keeps running on the daemon (sidebar dot "working"). Its Stop
     returns wherever the thread is next shown (H-3).
   - An in-flight first send finds no pane (WP-B's `conversation()` → null)
     and touches nothing.
2. **Terminal.** `terminals.release(id)`: the terminal becomes the panel's
   selected tab (`prefs.active`). The panel's open state is unchanged, the
   socket stays attached, and there is **no DELETE**: the shell never dies.
   `placeTerminals` already hands it to the panel once no drawn pane holds it.
3. **Preview.** The URL goes with the spec, and the iframe unloads.
   (⊞-hiding is how to keep it.)
4. **Workspace** (`closePane(ws, id)`):
   - `order` loses the id and `preset = LADDER[n − 1]`;
   - `panes[id]` = the default spec;
   - `kept` is unchanged;
   - focus goes to the pane now in the closed slot, else the slot before.
5. **Remount.** `useLayout` bumps a render-only `incarnation[id]`.
   Workspace keys sections `${id}.${incarnation}` and drops the id from its
   mounted set. A split that later reuses the id mounts fresh: never the old
   FileTab, iframe or terminal slot.
6. **W-6.** `selectedChatOf` recomputes:
   - the newly focused pane, if it is a drawn chat;
   - else the most recently selected drawn chat;
   - else any drawn chat;
   - else none.
   This is WP-B's own fallback, plus pruning the closed id.

`resetWorkspace` becomes `setPreset(single)` plus default edges and a closed
panel. `setPreset`:
- shrinking keeps `order.slice(0, n)`, moves the rest to `kept`, and moves
  focus to `order[0]` if needed (WP-A's rule);
- growing appends `kept` first, then free ids with the defaults;
- a same count changes the shape only.

### 3.7 ⊞, keys and labels

- **⊞.** Eight tiles, 4 per row (`MenuShell gridCols={4}`). `PresetIcon` gains
  grid5–grid8. The head reads "Layout · N panes". With grid5 or grid7 current,
  no tile is checked. "Showing X of Y panes" stays.
- **Keys.** `shortcutFor` maps Ctrl+Alt+1…8 to `focus1…8`. Each focuses
  `order[N − 1]` (a slot), and is ignored past the count. A dropped pane comes
  back as WP-A's sticky rule allows. In a terminal they stay the HUD's
  (`terminalTakesKey` unchanged).
- **Labels.** `slotOf(ws, id)` feeds:
  - `New thread · N` (`composeRows`);
  - the panel's "in pane N" and "Go there";
  - the dirty-refusal chip;
  - aria labels.
  Each section also gets `data-slot`.

### 3.8 Eight panes: chats, terminals, memory

- **One SSE stream**, routed per pane (`routeEvent` over 8 ids): no new
  connections.
- **Transcripts** load once per thread a pane opens. Chrome's 6 HTTP/1.x
  connections per host, one held by `/events`, mean 8 reopened threads queue
  briefly. Boot opens compose rows, so it fetches none.
- **Memory.** Every mounted pane keeps its DOM:
  - up to 8 Monaco editors (one shared worker pool);
  - each Preview iframe is a live page with its own HMR socket to the dev
    server's origin;
  - hidden (`kept`) panes stay mounted by design, so with 7 hidden that is 7
    live pages.
  Nothing is capped. The risk is noted in §7.
- **Terminals.** WP-D's cap of 6 stays. A 7th or 8th terminal pane shows the
  picker ("choose one, or close one to open another"). One terminal is still
  drawn in one place (W-1).

## 4. The thinking orb (WP-H)

**Source** (H-1). The store gains `turns: Record<threadId, { since: number |
null; error: "" | "during" | "ended" }>`, written in the reducer:

| Event | `turns[tid]` |
|---|---|
| send with a thread id | `{since: now, error: ""}` |
| `turn_started` | new `{since: now}` unless an entry from this turn exists (a send set it). An `"ended"` entry is replaced. |
| `activity` working or needs_input, no entry | `{since: running_since ?? now}`: a turn noticed late (Discord, reopen, reload) |
| `error` | `error: "during"` |
| `turn_finished` | `stop === "error"` or `error === "during"` → `"ended"` (kept); else delete |
| `activity` idle/unread/failed with a non-ended entry | delete (a missed finish) |

**The line** is pure, in `lib/thinking.ts`:
`thinkingLine({chat, threadId, turn, activity, approvalHere}) →
{word, text, orb: OrbState, since, moving} | null`. Precedence, first match
wins:

| Condition | Text | Orb (palette key) | Colour |
|---|---|---|---|
| `error === "during"` | "Error" | error | red |
| `error === "ended"`, turn over | "Ended on an error" (static; cleared by the next turn or a send) | error, not moving | red |
| an approval in `state.approvals` with this `thread_id` | "Waiting for approval" | approval | amber |
| activity `needs_input`, no approval | "Waiting for your answer" | approval | amber |
| an unfinished op in this pane | "Running \<name\>…" (name cleaned to one line, capped at 40) | tool | amber |
| the pane tracks this turn, or activity `working`, or a compose send in flight | "Thinking…" | thinking | the accent (the avatar's, if set) |
| otherwise | no line | | |

The elapsed time follows the word: `12s`, `3m 07s`, `1h 02m`. One shared 1 s
ticker runs only while some drawn line shows. With no `since` (a compose send
before its thread exists), no time is shown.

**Placement.** `ChatTab` draws `[data-testid=thinking]` as a sibling right
after the `.chatlog` scroller and before `.chatops`. It is **outside
`[data-testid=log]`**, so the suites that read the log's text are unchanged.
- It is in the pane's flow: no `position: fixed`, no z-index, no buttons and
  no links. Its words never say Authorize, Approve or Deny.
- Under a card it is inert like everything else, and the card stays on top.
- The input bar's own status words are left as they are, because the suites
  assert them. Removing the duplicate is a follow-up.

**Drawing** (H-2). `components/MiniOrb.tsx` is an 18px inline SVG:
- a 24-tick outer ring and a two-arc inner ring, rotating in opposite
  directions at the palette's `spin`;
- a core circle that "flexes" (scale 0.78 ↔ 1.08 over 1.2 s);
- CSS `@keyframes` on `transform` only (`transform-box: fill-box`), so 8 at
  once cost the compositor, not the main thread;
- colour from `--mo-hue`.

`palette()` and `ACCENTABLE` move to `lib/orbpalette.ts`, imported by both
Orb.tsx and MiniOrb, so amber and red can never take the avatar accent in one
and not the other. The mini orb is `aria-hidden`. The word sits in a
`role="status"` span; the time does not, so a screen reader does not hear
every second.
- **`prefers-reduced-motion: reduce`**: no rotation and no flex. Colour and
  word carry the state, and the time still counts.
- Hidden panes are `display: none`, so their animations do not run.

**Adoption** (H-3). In App, a drawn chat pane adopts a running turn when all
of these hold:
- it shows thread T;
- `activity.threads[T] === "working"`;
- no pane has `turnThreadId === T`;
- the pane is not busy with another turn.

Adopting sets `busy: true, turnThreadId: T, status: "THINKING"`. Stop and
Enter-steers then work again. The existing 15 s reconcile clears it if
`running` is false.

**Optional backend (H-b).** `thread_json` adds `running_since` (ISO, set where
`session.worker` is assigned, `daemon.py:907`). The HUD seeds `since` from it,
so the time survives a reload. Without it, a reload restarts the count.
`docs/hud-api.md` and `hud_v2_mock` gain the field.

## 5. Work packages

Order:
- **H** can start as soon as #27 and #28 merge, in parallel with **G1**.
- **G2** follows G1.
- **G3** is built only if the owner says so (O-3).

Each is a PR with its own free tests and the same-PR docs (CLAUDE.md, design
§18, `docs/hud-api.md`). Each merges after an independent review passes
(owner's OK, 2026-10-10). Every check below must fail against the mutation
named beside it before it is kept.

### WP-G1: eight panes and the new shapes (no new buttons yet). Size M, risk medium.

**Files:**
- `lib/workspace.ts` (+test): types; `order`/`kept`; SHAPES with `rows`;
  `LADDER` and `MENU_PRESETS`; `gridTemplate`; generic drops; parse;
  `setPreset` grow/shrink; `slotOf`.
- `lib/layout.ts` (+test): focus1…8.
- `components/Workspace.tsx`: `PANE_NOS` loop, `gridTemplate`, bottom-row
  separators, `data-slot`, incarnation keys.
- `components/Layout.tsx`: focus by slot, `PresetIcon`, 8 tiles.
- `lib/chats.ts` (+test): `emptyChats`, `besideTarget` and `composeRows` by
  slot.
- `App.tsx`: `perPane` and labels.
- `components/Terminal.tsx`: slot labels.
- `theme.css`: tiles and `.wsplit` for the bottom row.

**Tests and the mutations they must catch:**

| Check | Mutation it must catch |
|---|---|
| `workspace.test`: every WP-A/WP-B stored value (the six presets, with and without panel and splits) draws the same `fit` as before | `order` derived from anything but `slice(0, n)` |
| `workspace.test`: garbage `order`/`kept`/`bottom`/`filePath` each fall back alone | parse resets the whole workspace on one bad field |
| `workspace.test`: `gridTemplate` for all 10 shapes, uneven union and coincident edges | the bottom row reusing the top edges |
| `workspace.test`: the generic drop reproduces the old drop expectations, plus a property (every shape × every focused pane × widths 400–2000 never drops the focused pane) | keeping the leftmost window regardless of focus |
| `workspace.test`: §3.3's window table | — |
| `workspace.test`: ⊞ 8 → 1 → 8 restores order and specs | `kept` not appended on grow |
| `layout.test`: focus5…8; AltGr symbols (`[`, `{`, `` ` ``) with Ctrl+Alt never match | — |
| `hud_v2_layout_check._eight_checks` (its own `MockDaemon(0)`): 1920×1080 grid6 and grid8 draw all panes ≥ 360 wide with no horizontal scroll; 1280×800 grid8 draws 6 with "Showing 6 of 8"; 1280@150% draws 4 | — |
| same: Ctrl+Alt+7 brings a dropped pane into the drawn set | — |
| same: a stored `order` `[1,5,2]` focuses id 5 on Ctrl+Alt+2 | focusing by id instead of slot |
| same: in a stored grid5, dragging `split-colb-1` moves only the bottom row | — |
| same: an 8-pane card check (Monaco, preview, terminal, chat panes at 160% and 70%): the card is fully on screen and each button is topmost at its point | — |
| `hud_v2_multichat_check._eight_chats_checks`: 8 chat panes on 8 threads; a turn in pane 7 draws only there; Stop in 8 leaves 7; a click selects 6 and voice lands in 6 | `PANE_NOS` left at 4: pane 5 never mounts or routes |

### WP-G2: split and close. Size L, risk high (it touches WP-B's release paths).

**Files:**
- `lib/workspace.ts` (+test): `splitVerdict`, `allocate`, `splitPane`,
  `closePane`, `copySpec`.
- `components/Workspace.tsx`: split button, ×, ask strip.
- `components/Layout.tsx`: the `"split"` icon, shared `.tb-icon`, and
  `LayoutControl.split/close/splitVerdict`.
- `App.tsx`: `splitPane` and `closePane` orchestration.
- `state/store.tsx` (+test): `release_chat`.
- `lib/chats.ts`: prune `selectedOrder`; `besideTarget` grow cases via split.
- `components/FileTab.tsx`: `filePath`, the same-file save broadcast.
- `components/DiffTab.tsx`: the `filePath` seed.
- `components/Terminal.tsx`: `release(id)`.
- `theme.css`.

**Tests and the mutations they must catch:**

| Check | Mutation it must catch |
|---|---|
| `workspace.test`: the ladder 1 → 8 and back, with exact shapes | — |
| `workspace.test`: insertion after the source (split the middle of cols3) | appending at the end |
| `workspace.test`: focus goes to the new pane, then to the slot on close | — |
| `workspace.test`: `allocate` (free first; the oldest clean kept; all kept dirty gives hidden-edit) | — |
| `workspace.test`: the verdict at 1280 (2 → 3 ok, 6 → 7 room) and at 1920 (7 → 8 ok) | ignoring room |
| `workspace.test`: `copySpec` per row of §3.5 | — |
| `workspace.test`: close resets only that spec and leaves `kept` and other presets' edges alone | — |
| `store.test`: `release_chat` parks input under the thread; drafts return when that thread opens in another pane; `selectedOrder` pruned | not parking the draft; not pruning, so a reused id inherits the selection |
| `hud_v2_layout_check._split_checks`: the button is in the single layout with the title-bar toggles' computed size and colour | — |
| same: clicks give cols2, cols3, grid4, focusing the new pane each time | — |
| same: up to 8 at 1920; disabled at 8 with the "max" title; at 1280, 6 → 7 disabled with the "room" title | — |
| same: under a card, disabled, and Enter on a focused button does nothing | — |
| same: Space is not push-to-talk | — |
| same: no × with one pane drawn; × on each drawn pane otherwise; grid5 → × → grid4 | × rendered with one pane |
| `_split_close_chat_checks` (multichat): split a thread's chat → the new pane composes in the same project, and the thread is open once | — |
| same: close mid-turn → the mock keeps streaming, the sidebar dot stays working, and reopening shows Stop (after WP-H) | — |
| same: words in a closed thread pane's box return on reopen | — |
| same: a composing pane with unsent words asks; Keep keeps, Discard closes | — |
| same: closing the selected chat moves selection to the next most recent drawn chat, and voice lands there | — |
| `_split_close_terminal_checks` (terminal check, `route_web_socket` fake): split a terminal → one `POST /terminals` with the source's `in` | — |
| same: at the cap, the picker and its sentence, with no retry | — |
| same: readable off is copied | — |
| same: closing the pane → no `DELETE /terminals`, no new ticket, and the terminal is the panel's selected tab | close sending a DELETE |
| `_split_file_checks`: a clean split opens the same path in the copy | — |
| same: split disabled on a dirty File pane | — |
| same: a save in A reloads clean B | — |
| same: × on dirty asks, and Discard closes | — |
| same: close a File pane on a.txt, then split a File pane on b.txt so the id is reused → the copy shows b.txt | no incarnation bump |
| `_split_preview_checks`: the copy loads the same URL, judged again; keep-origin is off in the copy (once WP-E lands) | — |

### WP-G3 (optional, owner's O-3): one buffer per file. Size M, risk medium.

**Design.** A new `lib/buffers.ts` is a window-wide, refcounted registry
keyed by `(projectId, path)`. Each entry holds `{base, mtime, text,
protected}` and one Monaco `ITextModel` (URI
`inmemory://jarvis/<pid>/<path>`), disposed at refcount 0.
- **Editing.** `FileTab` subscribes to the buffer. `CodeEditor` takes the
  model (`setModel`); the textarea fallback reads and writes `buffer.text`.
- **Dirty** belongs to the buffer. Each showing pane still reports
  `onDirty`. The ×, the view switch and "follow chat" ask or refuse only when
  the pane is the buffer's **last** view.
- **Save.** Either pane saves, once, with `expected_mtime`. A 409 note shows
  in every view.
- **Release.** G-6's "save first" restriction goes.

**Tests and the mutations they must catch:**

| Check | Mutation it must catch |
|---|---|
| typing in A appears in B | — |
| one save is one `writeFile` | — |
| a 409 shows in both views | — |
| closing one of two dirty views does not ask | — |
| closing the last asks | — |
| the model is disposed at zero | the model never disposed |
| — | the guard asking on every view |

### WP-H: the thinking orb. Size M, risk medium (adoption touches turn tracking).

**Files:**
- new: `lib/orbpalette.ts` (+test), `lib/thinking.ts` (+test),
  `components/MiniOrb.tsx`;
- `components/Orb.tsx` (imports the palette);
- `components/ChatTab.tsx` (the line);
- `state/store.tsx` (+test): `turns` and its reducer cases;
- `App.tsx`: dispatches, adoption, the shared ticker;
- `theme.css`: keyframes and reduced motion;
- optional H-b: `jarvis/v2/daemon.py` `running_since`, `docs/hud-api.md`,
  `tests/face/hud_v2_mock.py`, and a `hud_backend_check` case that it is set
  while running and absent after.

**Tests and the mutations they must catch:**

| Check | Mutation it must catch |
|---|---|
| `thinking.test`: each row of the precedence table | approval and tool swapped |
| `thinking.test`: activity-only lines; static ended error | — |
| `thinking.test`: a tool name with control characters and 200 characters is cleaned and capped | — |
| `thinking.test`: no `since`, no time; time format | — |
| `orbpalette.test`: tool, approval and error never take the accent | the accent applied to amber |
| `orbpalette.test`: Orb.tsx and MiniOrb import the same function (a grep, like `terminal_check`'s no-tool test) | — |
| `store.test`: every row of the `turns` table | a finish deleting an ended error |
| `hud_v2_multichat_check._thinking_checks` (own `MockDaemon(0)`): a turn in pane 2 shows mini orb, "Thinking…" and a ticking time there and nothing in pane 1 | — |
| same: `tool_started` gives "Running ls…" with a computed stroke of rgb(251,191,36) | — |
| same: an approval for that thread gives "Waiting for approval", and the card's buttons are topmost | — |
| same: needs_input with no approval gives "Waiting for your answer" | — |
| same: `error` turns it red; `turn_finished{stop: "error"}` leaves the static red line; the next send clears it | — |
| same: a Discord-started turn (`user_message` via discord, then `turn_started`) shows | a line driven only by the pane's busy flag |
| same: reopening a working thread shows the line and Stop (adoption) | — |
| same: under `emulate_media(reduced_motion="reduce")`, the computed `animation-name` is none and the word still shows | reduced motion ignored |
| same: no `canvas` in any line, and 8 lines at once | — |
| same: the single layout's `[data-testid=log]` text is identical with the line up | the line inside the log |
| same: the avatar accent recolours "Thinking…" only | — |

## 6. Overlap with WP-E and WP-F

- **WP-E (Preview).**
  - "Open in Preview" from a loopback terminal link should go to a drawn
    Preview pane. With none, it should call G2's `splitPane(source,
    {view: "preview", previewUrl})` when the verdict allows, so it never
    replaces the terminal's own pane. Otherwise it falls back to the click
    rule.
  - The keep-origin toggle is per pane and never copied by a split.
  - A copied URL is judged again on load, exactly as a stored one is.
- **WP-F (`terminal_read`).**
  - The "Jarvis read N lines · 15:42" note and the readable switch move with
    the terminal's view (pane or panel).
  - A split copy of a terminal is a new terminal and inherits only an *off*
    switch. A copy must never widen what Jarvis can read.
  - Closing a pane changes nothing that `terminal_read` sees: the terminal
    lives on.

## 7. Risks

1. **Eight panes is a wide-screen feature** (§3.3). At 1280×800 and 100% the
   window draws 6 of 8. The badge and the split tooltip say so, and zooming
   out is the lever.
2. **Ids are not slots after a split or close.** Anything that reads `pane-N`
   as a position breaks. Mitigation: `slotOf` for every visible number,
   `data-slot` for tests, and G1's `[1,5,2]` check.
3. **Release on close runs through WP-B's draft, give-back and turn
   machinery.** That area took several fix rounds. G2 uses only the existing
   primitives (`park`, `heldBack.hold`, `emptyChat`), and the multichat suite
   reruns every steer and give-back case after a close.
4. **Hidden `kept` panes stay mounted and live**: iframes, Monaco, attached
   terminals, loaded transcripts. That is the price of WP-A's restore (G-2).
5. **The single layout gains one button** (the split icon in pane 1's strip).
   Every existing suite must still pass; a selector that counts `#tabs button`
   would not.
6. **Merge order.** #27 and #28 both rewrite `workspace.ts`, `Workspace.tsx`
   and `Layout.tsx`, and #28's `workspace.test` still pins the WP-A chat
   singleton, which #27 removes. G1 starts from whatever the merge resolves.

## 8. For the owner, beyond D1–D4

- **O-1.** Splitting a terminal pane starts **a new shell in the same starting
  folder** (VS Code's split terminal). The alternative is the terminal picker,
  which starts nothing until you choose. Default: a new shell.
- **O-2.** Splitting a task pane shows **that task's diff**. The alternative
  is a second, identical task view. Default: the diff.
- **O-3.** A file with unsaved changes: is "save first to split it" (G2, cheap)
  enough, or should WP-G3 build a shared buffer, so two panes show and edit
  the same unsaved text the way VS Code does? Default: G2 only; G3 if asked.
