# Plan: directional split (VS Code style), up to twelve panes (Jarvis v2 HUD)

_A read-only planner's proposal against `main` at d9209b7. Where
`docs/plans/2026-10-19-hud-free-split-decisions.md` and this plan disagree, the
decisions file wins. This plan replaces S-1's ladder (`LADDER`,
`splitPane`/`closePane` jumping to the next shape). S-2, S-6, S-7, S-8, "no
Ctrl+W" and "the approval card is loudest" carry over unchanged._

## 0. What the owner gets

- **Two buttons in every pane's strip**, in the single layout too, where today's split button is:
  - **Split right**: an outline with a vertical divider (today's icon).
  - **Split down**: an outline with a horizontal divider.
  - Each halves only that pane. Nothing else moves or resizes.
- **The 4-across / 3-down cap**: no horizontal line across the centre crosses more than 4 panes, and no vertical line more than 3. Up to 12 panes, so a full 4×3 grid is possible.
  - A split that would break the cap is disabled, and its tooltip says why.
  - So is a split whose halves would be under 360×200 ("Not enough room to split right/down").
- **× gives the pane's space to its neighbour** in the same split.
- **⊞ keeps its eight tiles.** A tile is checked when the layout has that tile's shape.
- **An old stored layout opens exactly as it drew.**

One consequence the owner should see (open question O-1):
- At 1920×1080 with both side panes open, the centre is 1368 px. One split right gives two 684 px panes. A third column would be 342 px, so it is disabled.
- Four across needs a 1440 px centre. Fold a side pane or zoom out to get it.
- ⊞'s 4×2 still works there, because ⊞ re-lays the window out and may squeeze the side panes.

## 1. What this builds on (current code)

- `hud/src/lib/workspace.ts`:
  - `Preset` / `SHAPES` with `rows`: :72-126
  - `LADDER`: :79
  - `PaneNo` / `PANE_NOS` (1–8): :84-85
  - `Split` / `Workspace` (`preset`, `order`, `kept`, `panes`, `splits`, `focused`, `panel`): :227-247
  - `parseWorkspace`: :458; `saveWorkspace`: :524 (key `jarvis.hud.workspace`, :47)
  - `setPreset`: :543
  - `allocate` / `splitPane` / `closePane` / `copySpec`: :951-1066
  - `splitVerdict` / `splitTitle`: :1112-1141
  - `clampCols` / `moveColEdge`: :1150-1179
  - `cellsOf` / `gridTemplate`: :1206-1270
  - `dropColumn` / `dropRow`: :1303-1342
  - `fitWorkspace` with the sticky `DrawnSet`: :1397-1485
- `hud/src/components/Workspace.tsx`:
  - Draws one CSS grid (`gridTemplate`, :217-226).
  - Sections are keyed `${id}.${incarnation}` in **`PANE_NOS` order** (:272, :283), hidden with `display:none`, never unmounted.
  - One split button (:328-346), × (:359-374), separators `split-col-N`, `split-colb-N`, `split-row` (:243-257, :421-487).
- `hud/src/components/Layout.tsx`:
  - `useLayout` (:160-446): `split`/`close`/`splitVerdict` (:293-312), the incarnation bump (:289), the focus keys through `paneAt` (:403-416).
  - `PresetIcon`: :765; ⊞ menu: :1018-1101.
- `hud/src/App.tsx`:
  - `splitFrom`: :2330-2363; `closePane`: :2299-2319
  - `openSubagent` split: :2377-2410
  - dev-server auto-split: :2498-2552
  - `besideTarget` use: :2187-2198
  - the verdict shared by every pane: :4247-4249
  - `mainMin = SHAPES[fit.drawn].minW`: :3503
  - `__hud.split`: :3457; `__hud.workspace`: :3443
- Browser frame focus:
  - `lib/frameguard.ts` holds the rules; `state/frameguard.ts` holds the DOM half. Its pointer shield is set by `pointermove`/`pointerdown`, never by `pointerover` (:452-480).
  - **Moving an iframe's ancestor in the DOM reloads it.** That is why sections must never be re-parented or reordered (§10).
- **The daemon stores no workspace** (`grep workspace jarvis/v2` finds only comments). The layout lives in localStorage only.
- **The daemon pins Browser pane ids and slots to 1–8** (§6). The HUD posts `/browser/front` with every Browser pane, and a single id 9 or above gets the whole body refused.

## 2. Planner's decisions (each reversible)

| # | Decision | Alternative rejected |
|---|---|---|
| P-1 | A split tree is the single source of truth. `order` and `preset` are derived, never stored as truth. | Keep `order`/`preset` fields in sync by hand: two truths |
| P-2 | Reading order (slots) is the tree's depth-first order: x children left→right, y children top→bottom. This is VS Code's "grid appearance" order. A split puts the new pane right after its source, as G-4 did. | Geometric row-major sort: a split down would renumber panes far away |
| P-3 | FS-3 counts **lines across the drawn layout geometrically** (the owner's words). Drags stop at the cap. Minimum sizes stay structural. | Purely structural counts: these refuse e.g. "split 1 down, then 6 down" in 4×2 although no column holds more than 3 |
| P-4 | **Flat rendering**: every section stays a direct child of `#workspace` in id order, absolutely positioned from its own fractions. | Nested flex: re-parents panes on split, which remounts them and reloads frames |
| P-5 | **Collinear sibling edges are one separator** (they move together). This keeps 2×2/3×2/4×2's full-height column edges, as today. | Per-node edges only: 2×2 grows two half-height edges, an input change |
| P-6 | **A split is disabled while the window is dropping panes** ("dropped") | Allow it: the new pane only adds drops, so the split result can't be seen |
| P-7 | **Close gives the space to the previous sibling** (left/above), else the next one. Focus goes to W-6's `then`, else the heir's nearest leaf. This makes split-then-close an exact inverse. | The old slot rule: focus could jump to another column |
| P-8 | A split first commits the fitted sizes along the source's root path as stored (§5, condition C3) | — |

## 3. Data model

### 3.1 The tree (new `hud/src/lib/splittree.ts`, pure)

```ts
export type Axis = "x" | "y";                 // x: children side by side (split right); y: stacked (split down)
export interface Leaf { pane: PaneNo }
export interface SplitNode {
  axis: Axis;
  children: LayoutNode[];                      // x: 2–4, y: 2–3; no child has its parent's axis (flattened)
  sizes: number[];                             // one per child, finite, > 0, sum 1 (fractions of the node)
}
export type LayoutNode = Leaf | SplitNode;
export type Path = readonly number[];          // child indices from the root
export interface Rect { left: number; top: number; width: number; height: number }  // unit fractions of the pane area

export const MAX_ACROSS = 4, MAX_DOWN = 3, MAX_LEAVES = 12, MAX_DEPTH = 6;
export const SAME_EDGE = 1e-4;                 // edges closer than this are one edge

export function leaves(n: LayoutNode): PaneNo[];                 // depth-first = reading order
export function normalize(n: LayoutNode): LayoutNode;            // flatten same-axis, collapse 1-child, renormalise
export function pathOf(n: LayoutNode, pane: PaneNo): Path | null;
export function rectsOf(n: LayoutNode): Partial<Record<PaneNo, Rect>>;
export function acrossS(n: LayoutNode): number;                  // structural (minimum sizes), §4.4
export function downS(n: LayoutNode): number;
export function crossing(rects: Partial<Record<PaneNo, Rect>>, axis: Axis, from: number, to: number): number; // §4.1
export function insertBeside(n: LayoutNode, at: PaneNo, axis: Axis, id: PaneNo): LayoutNode;  // §5.1
export function removeLeaf(n: LayoutNode, pane: PaneNo): { tree: LayoutNode; heir: PaneNo } | null; // null: root leaf
export function withSizes(n: LayoutNode, path: Path, sizes: readonly number[]): LayoutNode;
export function presetTree(p: Preset, ids: readonly PaneNo[], edges?: LegacyEdges): LayoutNode; // §3.3
export function presetOf(n: LayoutNode): Preset | null;          // structure only, sizes ignored
export function shapeKey(n: LayoutNode): string;                 // structure and ids, no sizes (sticky check)
export type StoredNode = number | { axis: Axis; children: StoredNode[]; sizes: number[] };
export function parseTree(raw: unknown): LayoutNode | null;      // §8.3
export function storeTree(n: LayoutNode): StoredNode;
```

### 3.2 The workspace

```ts
export type PaneNo = 1|2|3|4|5|6|7|8|9|10|11|12;
export interface Workspace {
  tree: LayoutNode;        // which panes are drawn, how they are arranged, their sizes
  kept: PaneNo[];          // hidden by ⊞, most recently hidden first, specs kept; never in the tree (as today)
  panes: PaneSpec[];       // 12, by id (PaneSpec unchanged)
  focused: PaneNo;         // a leaf of the tree
  panel: { open: boolean; height: number };
}
export function orderOf(ws: Workspace): PaneNo[];             // leaves(ws.tree)
export function presetOfWs(ws: Workspace): Preset | null;      // presetOf(ws.tree)
export function inWorkspace(ws: Workspace, n: PaneNo): boolean;
export function slotOf(ws: Workspace, id: PaneNo): number;     // index in orderOf + 1; a kept id: its place in paneSequence (as now)
```

- `Workspace.order`, `.preset` and `.splits` are removed from the type, so the compiler finds every reader. The readers are `browserowner.ts:31,44`, `state/browser.ts:135,143`, `Workspace.tsx:174,267`, `Layout.tsx:198,970,1033,1052-1054`, `chats.ts:314-323`, `App.tsx:2198,4498` and `store.tsx:122`.
- `__hud.workspace()` returns a JSON-safe projection for the suites:
  - `{ ws: { ...ws, order: orderOf(ws), preset: presetOfWs(ws) ?? "free" }, fit: { ..., drawn: fit.drawnPreset ?? "free" } }`
  - The fit uses Records, never Maps (Playwright serialises a Map as `{}`).

### 3.3 Presets and trees

`presetTree` assigns `ids` in reading order. `SHAPES` keeps only `{label, panes}`; `minW`/`minH` come from the tree (§4.4). Defaults are equal sizes, except main2's x sizes `[0.6, 0.4]`.

| Preset | Tree | Edge test ids |
|---|---|---|
| single | `1` | — |
| cols2 | `x[1,2]` | `split-x-1` |
| rows2 | `y[1,2]` | `split-y-1` |
| cols3 | `x[1,2,3]` | `split-x-1`, `split-x-2` |
| main2 | `x[1, y[2,3]]` | `split-x-1`, `split-y1-1` |
| grid4 | `y[x[1,2], x[3,4]]` | `split-y-1`, `split-x0-1` (linked with `x1-1`) |
| grid5 | `y[x[1,2,3], x[4,5]]` | `split-y-1`, `split-x0-1`, `split-x0-2`, `split-x1-1` |
| grid6 | `y[x[1,2,3], x[4,5,6]]` | `split-y-1`, `split-x0-1`, `split-x0-2` (both linked) |
| grid7 | `y[x[1..4], x[5,6,7]]` | `split-y-1`, `split-x0-1..3`, `split-x1-1..2` (none coincide) |
| grid8 | `y[x[1..4], x[5..8]]` | `split-y-1`, `split-x0-1..3` (linked) |

- **The grids are rows-first**, so depth-first order is today's reading order. Each row has its own x node. Linked collinear edges (P-5) keep the shared full-height column edge.
- **`preset` survives only as derived.**
  - `presetOf(tree)` is the preset whose structure matches, ignoring sizes, else null.
  - ⊞ checks tile `p` iff `presetOf(tree) === p`. A dragged cols2 is still cols2, as today.
  - grid5 and grid7 have no tile, as today.
  - Any 9–12 pane or free tree is `null`: no tile, `data-preset="free"`.
  - "The preset it came from" is not stored. After a split, claiming a tile the shape no longer has would mislead.
- **`order`** is `leaves(tree)`. `slotOf`, `paneAt` (Ctrl+Alt+N), `data-slot`, "New thread · N", "in pane N", aria labels, the refusal chip, dev-server targets, the drive banner and the Browser front's `slot` all read it.
- **`kept` works as now.**
  - `setPreset(ws, p)`: `seq = [...orderOf, ...kept, ...free]`; `tree = presetTree(p, seq.slice(0, n))`; the rest that was in the workspace goes to `kept`. Focus stays if still drawn, else goes to the first pane. So 8 → 1 → 8 restores every pane's spec and place.
  - × still discards a pane (its spec resets, its id is freed). `allocate`/`freeId` are unchanged but range over 12 ids.
  - **⊞ replaces a free tree; its shape is not remembered** (O-3). Per-preset remembered edges (`splits`) also go: choosing a preset lays it out at default sizes.

## 4. The 4-across / 3-down rule (FS-3), and FS-4

### 4.1 Definition: geometric, on what is drawn

Take the pane area as the unit square and every drawn pane P as its fitted `Rect` `[l, l+w] × [t, t+h]`.

- **A horizontal line at height y crosses P iff `t < y < t+h`** (strictly). A line lying on an edge crosses nothing there. Edges within `SAME_EDGE` are one edge.
- `crossH(y)` is the number of panes crossed. It is enough to test the midpoints between consecutive distinct horizontal edges (at most 25 for 12 panes). `crossV(x)` is the same along x.
- `across = max_y crossH(y)` and `down = max_x crossV(x)`.
- Nesting does not matter: the count is on the final rectangles, "across the whole window" (FS-3).

**The verdict.** A split right of L adds exactly one pane to every horizontal line through L's interior and changes no other line. So:
- split right is allowed iff `crossing(rects, "x", L.top, L.top+L.height) ≤ 3`, where `crossing` is the maximum of `crossH` over that open band;
- split down is allowed iff the maximum of `crossV` over L's x-band is ≤ 2.

### 4.2 Worked examples (`x[...]` = side by side, `y[...]` = stacked)

1. **`1`**: every line crosses 1. Right gives `x[1,n]`; down gives `y[1,n]`.
2. **`x[1,2,3,4]`**: lines through 2 cross 4, so right is ✗ ("across"). Down on 2: vertical lines through 2 cross 1, so it is ok and gives `x[1,y[2,n],3,4]` (across 4, down 2).
3. **4×2** `y[x[1,2,3,4], x[5,6,7,8]]`, columns aligned:
   - Right is ✗ everywhere.
   - Down on 1: lines through column 1 cross {1,5} = 2, so it is ok. Column 1 now holds 1, 9, 5.
   - Then down on 5: lines through 5's band cross {1,9,5} = 3, so it is ✗ ("down").
   - Then down on 6: lines through ¼..½ cross {2,6} = 2, so it is ok. Column 2 now holds 2, 6, 10, and no vertical line crosses more than 3. A structural count would refuse this; that is why P-3 counts geometrically.
4. **3 over 2** `y[x[1,2,3], x[4,5]]`:
   - Down on 1: x ∈ (0,⅓) crosses {1,4}, so it is ok.
   - Then down on 4: its band (0,½) includes (0,⅓), which crosses {1,9,4} = 3, so it is ✗. Every line through L must have room, because the new pane spans all of L's width.
   - Down on 5: its band (½,1) crosses {2,5} or {3,5} = 2, so it is ok.
5. **Staggered** `x[y[x[a,b], c], d]`, row edge at ½:
   - Top lines cross {a,b,d} = 3; bottom lines cross {c,d} = 2.
   - Right on d: the max over its band is 3, giving 4 — ok. Right on c: the max is 2 — ok.
   - After both, top = 4 and bottom = 3.
6. **The full 4×3**: from `1`: down, down (`y[1,2,3]`: the second down is a sibling insert), then right ×3 in each row. That is 12 panes, after which every split is ✗ by FS-3. Columns-first works too.
7. **An edge line**: in 2×2 a vertical line exactly on the shared column edge crosses nothing; lines just either side cross 2.

### 4.3 Drags

- `moveEdge` stops an edge where moving further would make some line cross a fifth pane across or a fourth down.
- It walks the other panes' boundaries that lie within the travel and stops at the first one past which the count would exceed. The separator's `aria-valuemin`/`aria-valuemax` reflect the stop.
- With P-5's linked edges, the common grids never reach the stop.
- A stored tree already over the cap (hand-edited) is drawn as is. Drags never make it worse, and splits on the offending lines stay disabled.

### 4.4 Structural minimums: fitting and FS-4, separate from FS-3

These are the least size at which any sizes give every pane 360 × 200:

```
acrossS(leaf) = 1;  acrossS(x[c..]) = Σ acrossS(c);  acrossS(y[c..]) = max acrossS(c)
downS:          the same with x and y swapped
minW(tree) = acrossS === 1 ? MAIN_MIN (480) : 360 · acrossS      // = rowShape's rule for every row preset
minH(tree) = 200 · downS
```

- `geometric ≤ structural`, with equality whenever the edges line up (every preset).
- **main2's centre minimum drops from 840 to 720.** Keeping 840 would need a preset-specific bump, which breaks the FS-5 witness argument (§5.3).
- Depth bound: along any root-to-leaf path, each x ancestor adds at least 1 to the lines through that leaf, so there are ≤ 3 x ancestors and ≤ 2 y ancestors. Depth is therefore ≤ 6 (`MAX_DEPTH`), x nodes have ≤ 4 children and y nodes ≤ 3.
- FS-1's 12 is enforced by the id pool, not implied by FS-3 in general. With all 12 ids in the tree there is no id to take, so the verdict is "max".

### 4.5 FS-4, measured at the fitted size

- Split right needs `box.px.width / 2 ≥ 360`, i.e. a pane ≥ 720 wide.
- Split down needs `box.px.height / 2 ≥ 200`, i.e. ≥ 400 tall (the strip is inside the pane, as today).
- With no measured window (NaN), FS-4 is skipped.

## 5. Split and close

### 5.1 Split: `splitPane(ws, fit, source, axis, id, spec, {focus})`

1. **Disabled first** (`splitVerdict(ws, fit, source, axis, dirty)`), first reason wins:
   1. `dropped` (`fit.dropped > 0`)
   2. `across` / `down` (§4.1)
   3. `room` (§4.5)
   4. `max` (no id)
   5. `hidden-edit` (`allocate` finds only dirty hidden panes)

   Under a card the buttons are disabled in the UI, as every layout button is.
2. **Commit the fitted sizes along the path.** Every node on root → L takes the sizes it was drawn with as stored (C3 below). On screen this changes nothing.
3. **Insert.** Let L be `leaf(source)` and P its parent, with L its i-th child.
   - **Case B: P exists and `P.axis === axis`.** Insert `leaf(id)` at i+1, and `sizes[i]` becomes `sizes[i]/2, sizes[i]/2`. Halving is exact in binary floating point, and the sum is unchanged.
   - **Case A: otherwise** (including the root leaf). Replace L by `{axis, children: [L, leaf(id)], sizes: [½, ½]}`.
   - So the new pane goes right of or below its source: a sibling in a same-direction parent when there is one, else a new node.
4. `panes[id] = spec`, `id` leaves `kept`, and focus goes to `id` unless `focus: false` (dev servers). The incarnation bump stays as in `useLayout`.
5. **Copy of source is unchanged.** `copySpec` handles chat → new compose row (App), terminal → new shell in its folder with `readable` copied (picker at the cap), task → diff, file → same buffers (ED-2), Browser → URLs without keep-origin.

### 5.2 Close: `closePane(ws, id, then)`

- **Remove the leaf.**
  - If P has more than 2 children, the closed child's size goes to the **previous sibling**, or the next one if it was first.
  - If P has 2 children, P collapses into the survivor S, which takes P's size. If S has the grandparent's axis, its children are flattened in with their sizes scaled by P's size.
  - The receiving sibling is the heir. The last pane is never closed.
- **Panes outside P keep their rects**, because P's rect is unchanged.
  - One exception, as today: if the closed pane was what kept the side panes squeezed (minW drops), the side panes go back to the owner's widths and the centre narrows.
- **Focus** goes to `then` (W-6: the selected chat, App.tsx:2306-2310) when drawn. Otherwise it goes to the heir's leaf nearest the closed pane: the last leaf in reading order of a previous heir, or the first leaf of a next one.
- Release (`releasePane`), the inline ask strip, `CLOSE_GUARD_MS` and "no × with one pane" are unchanged.
- **Split followed by close of the new pane restores the tree exactly** (a property test).

### 5.3 FS-5: proof and its exact condition

**Lemma.** Let F be the current fit and L the source with fitted box w × h. Assume:

- **C1** nothing is dropped;
- **C2** FS-4 holds;
- **C3** the root → L path has been committed to its fitted sizes;
- **C4** the per-node clamp is *minimal*: the identity on any node whose children all meet their subtree minimums;
- **C5** each pane is positioned from its own fractions only (§10).

Then the fit after the split draws every other pane, both side panes and the panel at exactly the same px, and L's halves at w/2 (or h/2).

*Proof.*

- **(a) Fractions.** In case A only L's subtree changes. In case B, P's other sizes are untouched and their sum is preserved. Every other leaf's rect is computed top-down from unchanged numbers.
- **(b) Minimums.** At this geometry every leaf meets its minimum: the others did in F (C1), and the halves do by C2. So the new tree has a layout of width `F.width` meeting all minimums. minW is the least width at which any sizes meet them (a guillotine fact), so `minW' ≤ F.width`; adding a leaf never lowers a minimum, so `minW' ≥ minW`. The same holds for height.
- **(c) Side panes and panel.** `fitLayout` reads mainMin only through `over = left + right + mainMin − available` and the fold test.
  - If `over ≤ 0`, then `minW' ≤ F.width` keeps it ≤ 0.
  - If the centre was squeezed, it was squeezed to exactly `minW = F.width`, so `minW' = minW`.
  - The panel's `room = availableH − minH` is the same argument. The folds use the same comparisons.
- **(d) Clamping.** By C3 and C4 the clamp is the identity along the path. Off the path, every subtree's rect is unchanged, and its clamp reads only its rect and its own subtree.
- **(e) Pixels.** By C5, equal fractions give equal px. ∎

What breaks without each condition:

- **Without C3**, case B can move a sibling while the window is clamping P. (The cost: a clamped ancestor forgets the proportion it would have regained when the window grows.)
- **Without C5**, a shared CSS grid moves unrelated panes by sub-pixels: a new track adds a 1 px gap that the `fr` space loses.
- **C1** is why "dropped" disables a split.
- **Too small without drops** (centre < minW with nothing dropped) only happens with a single pane, where C2 already needs 720.

## 6. Growing to 12 ids: every place 1–8 is assumed

| Where | What | WP |
|---|---|---|
| `jarvis/v2/browser/panes.py:278` (focused_slot 1..8), `:280` (`len(panes) > 8`), `:285`, `:290`, `:413`, `:460` (`[1-8]`), `:482`, `:559` | Browser pane id and slot bounds. **A HUD pane 9+ would get the whole `/browser/front` refused.** Add one `PANE_MAX = 12` | FS0 |
| `jarvis/v2/browser/routes.py:164` | `range(1, 9)` on `/browser/panes/<id>` | FS0 |
| `jarvis/v2/browser/captures.py:187`, `drive.py:49-50,234`, `drive_state.py:91`, `tools/hud_browser_view.py:23-24`, `tools/hud_browser_act.py:12` (description "slot 1–8") | agent-facing slot range | FS0 |
| `docs/hud-api.md:2848-2854`, `docs/notes/browser.md:590,1093` | contract text | FS0 |
| `tests/v2/browser_panes_check.py:299` (`focused_slot` 9 refused → 13), `tests/v2/hud_browser_view_check.py:76` ("9" refused → "13") | negative tests move to 13 | FS0 |
| `hud/src/lib/browserapproval.ts:53` (`b.pane > 8`), `hud/src/lib/drive.ts:280` (`slot <= 8`) | the HUD's side of that contract | FS0 |
| `hud/src/lib/workspace.ts:84-85` | `PaneNo`, `PANE_NOS` → 12 | FS2 |
| `workspace.ts:249-251` | `DEFAULT_VIEWS`: 9–12 are `chat` | FS2 |
| `workspace.ts:940` | `MAX_PANES = PANE_NOS.length`. In FS2 it becomes `MAX_DRAWN = 8` (the ladder stops at grid8); FS4 removes it | FS2 / FS4 |
| `workspace.ts:1133` | "Eight panes is the most" | FS4 |
| `workspace.ts:356` | `LEGACY_PANES = 4`: **unchanged** (WP-A/WP-B values) | — |
| `hud/src/lib/layout.ts:301,348` | `Shortcut` `focus1…8`; `k <= "8"` | FS2 |
| `hud/src/lib/remote.ts:203,214-217` | `HUD_KEEPS` focus keys | FS2 |
| `hud/src/App.tsx:272`, `:2523` | `/^pane-([1-8])$/` → `/^pane-(\d{1,2})$/` and `PANE_NOS.includes` | FS2 |
| `hud/src/components/DevServerPrompt.tsx:79` | `/^[1-8]$/` slot keys → `/^[1-9]$/`; 10–12 by click | FS2 |
| `hud/src/lib/browserowner.ts:31,44` | `ws.order` and the slot from `indexOf` | FS3 (`orderOf` / `slotOf`) |
| `hud/src/state/browser.ts:135,143` | `ws.order` / `ws.kept` | FS3 (`inWorkspace`) |
| `hud/src/state/store.tsx:122` | `defaultWorkspace().order[0]` | FS3 |
| Generic `PANE_NOS` loops: `App.tsx` (:372, 577, 859, 964, 1108, 1215, 1458-1492, 2034, 2086, 2212, 2260, 2498, 2527, 2664-2724, 2877, 2912-2962, 3140, 3398, 3555, 4247), `store.tsx` (:412-608), `chats.ts:100,105`, `Workspace.tsx:159,272` | grow automatically (12 iterations) | — |
| Tests: `layout.test.ts:335-345` (9 and 0 now focus), `chats.test.ts:259-298` (`allocate` now returns 9, not 8), `workspace.test.ts` (8-long arrays), `hud_v2_layout_check.py:2419,2475,2484,2594,2929,2932` (`range(1,9)`), `:2612,:2971` (regex `pane-\\d-` → `pane-\\d+-`) | update | FS2 / FS4 |
| **Not panes, unchanged:** `lib/browserframes.ts:5` (`LIVE_FRAMES_WINDOW = 8`, a frame budget: a ninth live frame goes dormant as today), `BrowserProfile.tsx:100` (8 profiles), `jarvis/v2/vex.py` and `hud_v2_mock_vex.py:462` (VEX brain slot 1–8), `drive_state.py:59` ("eight steps"), `BROWSER_TABS_MAX = 12` | — | — |

- **Keys** (O-2): Ctrl+Alt+1…9 focus slots 1–9 and Ctrl+Alt+0 focuses slot 10. Slots 11–12 have no key. AltGr+9/0 report `]`/`}`, so they never match, and the AltGr test list gains them.
- **The Python mock** (`tests/face/hud_v2_mock*.py`) has no workspace logic. `hud_v2_mock_browser.py:10,53` runs the real `BrowserPanes`, so FS0 reaches it automatically.

## 7. Fitting

The order of operations is kept. Only the shape-specific parts become generic:

1. **Across.** `sides = fitLayout(layout, available, prefer, minW(render))`. If the centre is still under `minW`, `render = pruneAcross(render, focus)` and repeat.
2. **Down.** The panel shrinks, then folds (or with `preferPanel` panes are dropped first), then `pruneDown`. Then the sides are refitted (as today, `workspace.ts:1457`).
3. **Sticky** (§7.2).
4. **Clamp** each node top-down (§7.3), then compute boxes and edges.

### 7.1 Generic drop (render-only, never stored, never the focused pane)

`prune(node, axis, t)` returns a render tree with `acrossS ≤ t` for x (or `downS ≤ t` for y). The outer loop sets `t = current − 1` each time, as `dropColumn` reduced one column at a time.

- **A leaf** is kept.
- **A node across `axis`'s cross direction** (a y node when pruning x): every child is pruned with the same t.
- **A node along `axis`** with children `c1..ck`:
  - If `Σ acrossS(ci) ≤ t`, it is unchanged.
  - Otherwise pick the anchor child *j*:
    - on the focus path, the child holding the focus;
    - elsewhere, the first child whose interval overlaps the focused pane's interval on that axis, else the nearest child.
  - Keep the leftmost (topmost) maximal contiguous window containing *j* with `Σ ≤ t`.
  - If `cj` alone exceeds t, keep only `cj`, pruned with t.
  - Kept sizes are renormalised in proportion, so the drawn panes keep their own proportions. A one-child result collapses.

This reproduces every `dropColumn`/`dropRow` case for the six WP-B presets × every focused pane. Checked by hand:
- grid8 focus 3/4 → `[1,2,3]/[5,6,7]` and `[2,3,4]/[6,7,8]`;
- grid5/grid7 as in `workspace.ts:1295-1300`;
- main2: focus b or c → rows2, else single; a row drop keeps the large pane plus the stack's focused pane, or b, because a's interval overlaps both and the topmost wins.

**Intended differences from today, enumerated in FS3's golden update:**
- (i) main2's 720 minimum;
- (ii) a dropped render keeps the drawn panes' own proportions, not the smaller preset's stored edges;
- (iii) a 4-wide row whose other row is dropped stays 4 wide (no "no cols4 shape" rule).

### 7.2 Sticky set

`DrawnSet = { shape: shapeKey(ws.tree); drawn: shapeKey(render); panes: PaneNo[] }`.

- The previous set is kept while `shape` and `drawn` match, it holds the focus, and some focus f in `orderOf(ws)` makes the same prune sequence produce exactly it.
- The render is then rebuilt from that f.
- This is `workspace.ts:1459-1472`, keyed by `shapeKey` instead of preset.

### 7.3 Clamp

- It is `clampCols`'s edge-push (forward pass, then backward) generalised to per-child minimums (`acrossS(child) × 360` or `downS(child) × 200`).
- It is minimal (the identity when the minimums are met), which C4 needs, and matches the golden for single-level nodes.
- Below `Σ minimums` it falls back to sizes in proportion to the minimums (equal for equal children, as today).
- A window resize scales everything proportionally, because stored sizes are fractions.

### 7.4 `WorkspaceFit` (replaces `drawn`/`cols`/`row`/`bottom`)

```ts
export interface RenderNode { axis?: Axis; pane?: PaneNo; children?: RenderNode[]; sizes?: number[] /* fitted */;
                              src: Path; srcIndex?: number[] /* each render child's index in the stored node */ }
export interface PaneBox { rect: Rect; px: { width: number; height: number } }
export interface EdgeHandle {
  testid: string;                         // §9
  axis: Axis;                             // the way it moves ("x": a vertical separator)
  members: { path: Path; i: number }[];   // stored node boundaries it moves (several when linked)
  at: Rect;                               // where it is drawn
  value: number; min: number; max: number;// px, after the minimums and the FS-3 stop
}
export interface WorkspaceFit {
  sides: Fitted;
  render: RenderNode;
  panes: PaneNo[];
  boxes: Partial<Record<PaneNo, PaneBox>>;
  edges: EdgeHandle[];
  dropped: number; why: "" | "narrow" | "short";
  width: number; height: number;
  minW: number; minH: number;   // replaces SHAPES[drawn].minW/minH (App.tsx:3503, maxPanelHeight :1495)
  drawnPreset: Preset | null;
  panel: { open: boolean; height: number; auto: boolean };
}
```

Side-pane rules (`fitLayout`, `prefer`), the panel (`preferPanel`, `PANEL`) and every dimension (`PANE_MIN_W` 360, `PANE_MIN_H` 200, `MAIN_MIN` 480, rails 36, side ranges) are unchanged.

## 8. Persistence and migration (same key, `jarvis.hud.workspace`)

### 8.1 The v2 value

```ts
interface StoredWorkspaceV2 {
  version: 2;
  tree: StoredNode;                   // leaves as bare numbers
  kept: number[]; panes: StoredPane[] /* 12 */; focused: number;
  panel: { open: boolean; height: number };
  // Read only by a rolled-back HUD (its parser ignores the rest):
  preset: Preset;                     // presetOf(tree) ?? the ladder preset for min(count, 8)
  order?: number[];                   // only when every id is ≤ 8 and the count matches SHAPES[preset].panes
}
```

- `kept` keeps its name. The old `parseKept` filters out ids it doesn't know.
- `workspaceStorage` (the Private-pane projection, `workspace.ts:508-522`) is unchanged.
- **Reading:**
  - `version === 2` with a valid tree → the v2 parse.
  - Anything else (no version, a bad v2 tree, or a version > 2) → today's v1 parse (`workspace.ts:458-491`, kept verbatim as `parseV1`) followed by the table below. A bad v2 tree therefore falls back to its own mirror.

### 8.2 v1 → tree

The table uses `o = order` (WP-A/WP-B values: ids 1…n, others kept, as today) and `s = parseSplit(splits[preset], preset)`. Edge lists become sizes as `[e1, e2−e1, …, 1−ek]`.

| v1 preset | Tree | Sizes |
|---|---|---|
| single | `o1` | — |
| cols2 | `x[o1,o2]` | from `s.cols` |
| rows2 | `y[o1,o2]` | `[s.row, 1−s.row]` |
| cols3 | `x[o1,o2,o3]` | from `s.cols` |
| main2 | `x[o1, y[o2,o3]]` | x from `s.cols` (default `[0.6,0.4]`), y `[s.row, 1−s.row]` |
| grid4 | `y[x[o1,o2], x[o3,o4]]` | y from `s.row`; **both** rows from `s.cols` (linked) |
| grid5 | `y[x[o1..o3], x[o4,o5]]` | top from `s.cols`, bottom from `s.bottom` |
| grid6 | `y[x[o1..o3], x[o4..o6]]` | both from `s.cols` |
| grid7 | `y[x[o1..o4], x[o5..o7]]` | top `s.cols`, bottom `s.bottom` |
| grid8 | `y[x[o1..o4], x[o5..o8]]` | both from `s.cols` |

Only the current preset's edges carry over. `focused`, `panel` and `panes` are parsed as today, and `panes` is padded to 12.

### 8.3 Fail-safe `parseTree`

- **Caps first, rejecting the whole tree:**
  - depth > 6 (a counter, never unbounded recursion);
  - more than 23 nodes;
  - a children array longer than 4 (x) or 3 (y), checked **before** iterating;
  - a leaf not an integer in 1–12 (0, 13, "3", 3.5, NaN);
  - a duplicate leaf;
  - an `axis` that is not `"x"`/`"y"`;
  - more than 12 leaves.
- **Field by field, as v1 does:**
  - Bad `sizes` (wrong length; any entry non-number, NaN, ±Infinity, ≤ 0; or a non-finite sum) → equal sizes for that node only.
  - Unknown keys are ignored. Raw objects are never spread, so `__proto__` is inert.
- `normalize` then flattens and collapses.
- FS-3 is **not** checked on read: a layout is never lost on reload because a clamp or a hand edit put it over the cap.
- `kept` keeps only distinct ids 1–12 not in the tree; `focused` falls back to the first leaf.

## 9. Edges

- **One separator per boundary between adjacent children of a node**, at that node's rect, spanning its cross extent.
- **Test ids** are `split-<axis><path digits>-<i>`, e.g. `split-y-1`, `split-x0-2`, `split-y01-1`. Path digits are 0–3, so ids are unambiguous.
  - Old → new: `split-col-N` → `split-x-N` (one-row shapes) or `split-x0-N`; `split-colb-N` → `split-x1-N`; `split-row` → `split-y-1` (main2: `split-y1-1`).
- **Linked (P-5).** Within one node, consecutive children of the cross axis whose fitted boundaries coincide (< `SAME_EDGE`) share one separator. It carries the first member's test id and `data-linked=<count>`. Dragging moves every member.
  - The allowed range is the intersection of the members' ranges plus the FS-3 stop.
  - Coincidence is only among one node's direct children.
- **Drag** (`moveEdge(ws, fit, handle, px) → Workspace`): moves only the two children beside each member boundary (as `moveColEdge`). Each keeps `≥` its subtree minimum in px, and FS-3 holds (§4.3).
  - The edit starts from the fitted sizes, as today's drag uses `fit.cols`.
  - In a dropped render, only the drawn window's share of the stored node is rescaled; dropped children keep their stored sizes.
- **Keys:** `keyStep` as now (arrows ±16, Shift ±64, Home/End to min/max). Space does nothing. `blocked` ignores the keys.
- **Double-click** sets every member node's sizes to `1/k` (`equalize(ws, paths)`). The clamp fixes the render when a child needs more.
- Separator props, labels and titles stay: "Resize the panes · double-click to make them equal".

## 10. Rendering (`Workspace.tsx`, `theme.css`)

- **Flat sections, absolute.**
  - `#workspace` loses `display:grid; gap:1px` (`theme.css:1418-1421`) and keeps `position:relative; background: var(--border)`.
  - Each drawn section is `position:absolute` from `fit.boxes[n].rect`: `left: calc(L·100% + (L>0 ? 1px : 0))`, `width: calc(W·100% − (L>0 ? 1px : 0))`, and the same for top and height. That keeps the 1 px rule and depends only on the pane's own fractions (C5).
  - Undrawn sections keep `display:none`. `gridTemplate`, `cellsOf`-for-render and `slotWidths` go.
- **No remount, no reload.** Sections stay one flat list keyed `${id}.${incarnation}` (`Workspace.tsx:283`) in `PANE_NOS` order, never slot order.
  - React then never moves a section in the DOM (moving an iframe's ancestor reloads it) and never re-parents one. A split, a close, ⊞, a drag or a resize changes only `style`.
  - `ever` (:171-175) uses `inWorkspace`.
  - Terminals keep `placeTerminals(ws.panes, fit.panes)`. Monaco and xterm see a size change only on the split pane.
- **The two buttons**, replacing `pane-<id>-split` at :328-346:
  - `pane-<id>-split-right`: `AreaIcon area="split-right"` (today's vertical divider), `aria-label="Split right"`.
  - `pane-<id>-split-down`: a new `"split-down"` variant with a horizontal divider at y=8, `aria-label="Split down"`.
  - Both use class `tb-icon panesplit` (the title-bar toggles' computed size and colour), sit right after the view strip or select, and are always shown, in the single layout too.
  - Space and Enter press them once (`buttonKeys`); Space is never push-to-talk.
  - `disabled={props.blocked || !ok}` — **under an approval card both are disabled** and inert.
- **Props.** `splitFor(pane) → { right: {ok, title}, down: {ok, title} }` is now per pane (App.tsx:4247-4249). `onSplit(pane, axis)`.
- **Titles:**
  - ok: "Split right: a copy of this pane beside it" / "Split down: a copy of this pane below it"
  - across: "Four panes across the window is the most"
  - down: "Three panes down the window is the most"
  - room: "Not enough room to split right: widen the window, fold a side pane or zoom out (Ctrl+-)" (and "…down")
  - dropped: "Showing X of Y panes: widen the window or close a pane to split"
  - max: "Twelve panes is the most"
  - hidden-edit: as today
- `PaneInfo` gains `height`. `narrow`/`compact` read the box width. `data-preset` is `presetOfWs ?? "free"`; `data-drawn-preset` is `fit.drawnPreset ?? "free"`.
- **⊞** (`Layout.tsx:765-781, 970, 1033-1059`): `PresetIcon` draws `rectsOf(presetTree(p, …))`, a tile is checked iff `presetOfWs === p`, the head reads "Layout · N panes" with `N = orderOf(ws).length`, and the badge is `fit.dropped`.

## 11. Work packages (each its own PR with same-PR docs: `docs/notes/hud-workspace.md`, design §18, `docs/hud-api.md` where touched)

Order: **FS0 ∥ FS1 → FS2 → FS3 → FS4.**
- FS0 and FS1 touch disjoint files.
- FS2 must follow FS0 (the daemon must accept ids 9–12).
- FS2, FS3 and FS4 all touch `workspace.ts` and `App.tsx`, so they are sequential.

Every check named as catching a mutation must be shown failing with that mutation in a scratch copy, as WP-G1/G2 did.

### FS0: the Browser pane contract to 12. Size S, risk low. **Boundary-sensitive (Browser gate input validation).**

- **Files:**
  - `jarvis/v2/browser/panes.py` (one `PANE_MAX = 12`), `routes.py:164`, `captures.py:187`, `drive.py:49-50,234`, `drive_state.py:91`
  - `jarvis/v2/tools/hud_browser_view.py:23-24`, `hud_browser_act.py:12`
  - `hud/src/lib/browserapproval.ts:53`, `hud/src/lib/drive.ts:280`
  - `docs/hud-api.md:2848-2854`, `docs/notes/browser.md:590,1093`
- **Tests:**
  - `tests/v2/browser_panes_check.py`: 12 rows and `pane_id`/`slot`/`focused_slot` 12 are accepted; 13 rows and 13 are refused (:299 moves 9 → 13).
  - `/browser/panes/12` is accepted and `/13` gets 400 (route test).
  - `tests/v2/hud_browser_view_check.py:76`: "13" is refused, and slot "12" resolves for a drawn slot-12 pane.
  - `drive_state`/`drive` slot 12 is kept; 13 is cleared or refused.
  - Vitest: `browserapproval` pane 12 ok / 13 null; `drive.bannerOf` slot 12.
- **Risk:** it only widens an upper bound, and every negative case keeps its shape at 13. Review it as a gate change. A HUD that still sends ≤ 8 is unaffected, so it can merge first.

### FS1: the pure tree. Size M, risk low.

- **Files:** new `hud/src/lib/splittree.ts` and `splittree.test.ts` only.
- **Tests** (mutation each must catch in brackets):
  - `normalize` flattens and collapses.
  - `presetTree`/`presetOf` round-trip all 10 presets, and sizes are ignored.
  - §4.2's seven examples, verdicts included [counting lines on an edge].
  - `crossing` at coincident edges.
  - `acrossS`/`downS` and minW/minH for every preset (equal to `SHAPES` today except main2).
  - `insertBeside` cases A and B. Property: over 2000 seeded random reachable trees, every other leaf's rect is bit-identical [redistributing `1/(k+1)` to all siblings].
  - `removeLeaf` gives to the previous sibling, collapses and flattens. Property: split then close is the identity [giving to the next sibling].
  - `prune` reproduces the old `dropColumn`/`dropRow` expectation tables (copied from `workspace.test.ts`), plus "never drops the focused pane" over every shape × focus × t [anchoring on the first child].
  - The clamp equals `clampCols` on one-level nodes and is the identity when minimums are met [a water-filling clamp moving untouched siblings].
  - Linked-edge detection: grid4 → one column separator plus one row separator; grid5 → three column separators.
  - `parseTree`: every rejection and field-fallback case of §8.3, including a 100 000-entry children array returning quickly and depth 7.
- **Risk:** none at runtime; nothing uses it yet.

### FS2: twelve ids, still on the ladder. Size M, risk medium.

- **Files:**
  - `lib/workspace.ts` (:84-85, :249-251; `MAX_DRAWN = 8` for the ladder; `allocate`/`freeId` over 12)
  - `lib/layout.ts:301,348` (focus9, focus10 → slot 10 on Ctrl+Alt+0)
  - `lib/remote.ts:214-217`
  - `App.tsx:272,2523`
  - `components/DevServerPrompt.tsx:79`
  - tests: `layout.test.ts`, `remote` test, `chats.test.ts:259-298`, `workspace.test.ts` (12-long arrays, `allocate` → 9)
- **Face checks:**
  - New `hud_v2_layout_check._twelve_ids_checks`: ⊞ grid8, then single (7 kept), then a split takes id 9; `pane-9` mounts with `data-slot="2"`. Ctrl+Alt+9 / Ctrl+Alt+0 focus slots 9/10 when present and are ignored otherwise. AltGr `]`/`}` match nothing.
  - `hud_v2_browser_controls_check`: a Browser pane with id 10 publishes `/browser/front`, and its See/Drive PATCH succeeds against the mock's real `BrowserPanes` [FS0 not merged → refused body].
  - The regex fixes in `hud_v2_layout_check.py:2612,2971`.
- **Risk:** many call sites, all mechanical. `PANE_NOS` loops run 12 times (12 `emptyChat`s, 12 per-pane refs).

### FS3: the tree becomes the model; behaviour preserved. Size L, risk high. **Boundary-sensitive: no Browser frame may reload, move parents or take the keyboard when panes move.**

- **Files:**
  - `lib/workspace.ts`: Workspace on `tree`; `parseV1` + migration + v2 storage with the mirror; `setPreset` via `presetTree`; the fit of §7; `EdgeHandle`s; `moveEdge` (FS-3 stop deferred to FS4); `equalize`; and the ladder's `splitPane`/`closePane` re-expressed as `presetTree(LADDER[n±1], newOrder)` so the owner-visible behaviour is unchanged.
  - `components/Workspace.tsx` (§10 rendering and edges; one split button still).
  - `components/Layout.tsx`: `useLayout` replaces `setSplit`/`equalSplit` with `moveEdge`/`equalize`; `drawnBefore` becomes the new `DrawnSet`; ⊞ and `PresetIcon`.
  - `theme.css` (`#workspace`, `.wpane` absolute, the 1 px rule).
  - `App.tsx` (:3503 `fit.minW`, :4498 `layoutKey` from `orderOf`, the `__hud.workspace` projection).
  - `lib/browserowner.ts:31,44`, `state/browser.ts:135,143`, `lib/chats.ts:302-324` (`presetOfWs`), `state/store.tsx:122`.
  - `workspace.legacy-fits.json`.
- **Vitest tests:**
  - All 300 golden fits through an adapter (`drawn` = `drawnPreset`; `cols`/`row` read from the boxes). Every field is equal except the enumerated sets (i)–(iii) of §7.1, which are regenerated with their keys listed in the PR [migrating grid4 rows with independent sizes].
  - The §8.2 table for all 10 presets × default and custom edges, grid5/grid7 bottoms included [the bottom row reading `s.cols`].
  - v2 round trip.
  - Mirror fields readable by a frozen copy of today's parser (`parseV1`), so a rolled-back HUD draws the mirror preset.
  - §8.3 garbage per field.
  - ⊞ 8 → 1 → 8.
  - Today's ladder tests pass unchanged.
- **Face checks** (in `hud_v2_check`'s layout and multichat sections, each with its own `MockDaemon(0)`):
  - `hud_v2_layout_check._workspace_checks`, `_eight_checks`, `_grid_card_checks`, `_sticky_checks`, `_split_checks` (ladder) with the new edge ids.
  - "2×2: an edge down and one across" still holds via linking.
  - In grid5, dragging `split-x1-1` moves only the bottom row.
  - New `_no_remount_checks`: tag every section, iframe, Monaco host and xterm element with a JS expando. Then do ⊞ grid4 → single → grid4, a ladder split, a close of another pane, an edge drag and a resize. The same elements survive, the mock Browser page's hit count is unchanged, there is no new terminal ticket, and the Monaco model count is unchanged [rendering in slot order, or nested wrappers].
  - `hud_v2_multichat_check._eight_chats_checks` unchanged (the v1 seed migrates).
  - `hud_v2_browser_check` new `_guard_layout_checks`:
    - after each move above, a page calling `focus()` with no gesture is removed ("This page took the keyboard"), including with the pointer resting where the frame now is;
    - a frame in a ⊞-hidden or dropped pane is refused as hidden;
    - under a card, none of the moves happen.
- **Risk:** the render rewrite, edge identity and the golden changes. Mitigations:
  - behaviour is held to today's suites;
  - FS-5 is not claimed yet;
  - the frame identity checks are mandatory for merge.

### FS4: directional split and close. Size L, risk high. **Boundary-sensitive: panes move under the pointer.**

- **Files:**
  - `lib/workspace.ts`: `splitPane(ws, fit, source, axis, …)` with C3, `closePane` to the sibling, `splitVerdict`/`splitTitle` per pane and axis, `autoSplit(ws, fit, source, held) → {axis, id} | null` (right, else down, never the destructive `allocate` tiers), the FS-3 drag stop, and `LADDER`/`MAX_DRAWN` removed.
  - `components/Workspace.tsx`: two buttons, per-pane `splitFor`.
  - `components/Layout.tsx`: `AreaIcon` `split-right`/`split-down`; `LayoutControl.split(source, axis, id, spec, opts)`; `splitVerdict(source, axis, dirty)`.
  - `App.tsx`:
    - `splitFrom(source, axis)` (:2330);
    - `openSubagent` prefers right, then down (:2392);
    - dev-server auto-split (:2535-2550, `canSplit` per source);
    - `pickThread`'s beside (:2187-2198);
    - `__hud.split(source, {axis, focus})`;
    - per-pane `splitFor`.
  - `lib/chats.ts` `besideTarget`: its two grow cases become split right; the `preset` field goes.
  - `lib/devopen.ts:52-62,110-116`: `canSplit` → `canSplitFrom: readonly PaneNo[]`.
  - `state/frameguard.ts`: a `layoutMoved()` that, after any split, close or ⊞, raises every shield and refuses a shield step-aside on a `pointerdown` for `CLOSE_GUARD_MS`. It only narrows: the second click of a double-click on × must not hand the keyboard to a page that grew under the pointer.
  - `theme.css`.
- **Vitest tests:**
  - The verdict table: §4.2's examples; FS-4 at 719/720 px and 399/400 px; "dropped"; "max" with 12 leaves; hidden-edit.
  - The FS-5 property over seeded random reachable trees × windows, including windows that clamp: every other box's px, `sides` and `panel` are identical (`===`) [skipping C3; splitting from stored rather than fitted sizes].
  - Close: heir, focus to `then`, else the heir leaf [the old slot rule].
  - `autoSplit` prefers right and never folds or drops.
  - `devopen.test`, `chats.test`.
- **Face checks:**
  - `hud_v2_layout_check._split_checks` rewritten:
    - both buttons in the single layout, with the toggles' computed size and colour and their titles;
    - at 1920×1080 with both side panes open: right, then right is disabled with the room title (342 px), and down works twice, then is disabled;
    - with both side panes folded: build 4×3 = 12; each split leaves every other pane's, the side panes' and the panel's bounding box unchanged within 0.5 px, at 100% and 150% zoom [CSS grid rendering];
    - the across title at 4, the down title at 3, "Twelve panes" at 12;
    - under a card both are disabled, and Enter and Space do nothing;
    - Space is not push-to-talk;
    - × gives the space to the left or upper neighbour, and other boxes are unchanged;
    - a double-click on × closes one pane.
  - `_split_close_one_checks` and `_split_file_checks` use the new ids.
  - Multichat `_split_close_chat_checks`, `_split_close_selected_checks` and `_rv47_*`, terminal `_split_close_terminal_checks`, and the browser suite's `_split_checks` use `-split-right`.
  - `hud_v2_browser_check._guard_layout_checks` gains: a double-click on × over a Browser pane that grows under the pointer leaves the page without the keyboard [no `layoutMoved`].
- **Risk:** WP-B's release paths are reused unchanged. The new parts are the pure verdict and the fitted-path commit, both property-tested. Frame focus stays tightened, never loosened.

## 12. Risks

1. **Room, not the cap, is the usual limit** (O-1). At 1366×768 splitting reaches 2 across × 2 down; 4×3 needs a 1440 × 600 centre.
2. **The golden-file changes** (§7.1 i–iii) must be enumerated, not regenerated wholesale.
3. **Mounted-pane memory** grows with 12 panes: up to 12 Monaco editors and Browser panes (frames capped at 8 live, unchanged) and 12 chats. Hidden `kept` panes stay mounted by design.
4. **Edge ids change.** Every suite that names `split-col-*`/`split-row` is listed in FS3.
5. **C3 rewrites a clamped ancestor's proportions** on split. This is visible only after a later window grow.

## 13. Open questions for the owner (defaults in bold)

- **O-1.** At 1920×1080 with both side panes open, split right stops at two columns (a third would be 342 px). **Default: as decided (FS-4/FS-5): disabled, and the tooltip names widen / fold a side pane / zoom out.** The alternative is to let a split borrow from the side panes' slack, giving it only to the split pane's column. It is more code, and it changes the owner's stored widths.
- **O-2.** Keys for slots 9–12. **Default: Ctrl+Alt+9 → 9, Ctrl+Alt+0 → 10; 11 and 12 by click.**
- **O-3.** ⊞ keeps no memory of shapes. Choosing a preset replaces a free layout (its panes come back in order, its shape does not), and presets open at default proportions. **Default: no memory.** The alternative is a "Back to my layout" tile holding the last free tree.
- **O-4.** FS-3 counting. **Default: geometric (your words), with drags stopping at the cap.** The alternative is structural: simpler, but it refuses e.g. "split 1 down, then 6 down" in 4×2 although no column exceeds 3.
- **O-5.** Merge rule (the decisions file says ask). **Default: merge on review, as S-5.**

### Critical files for implementation
- hud/src/lib/workspace.ts
- hud/src/lib/splittree.ts (new)
- hud/src/components/Workspace.tsx
- hud/src/components/Layout.tsx
- hud/src/App.tsx
- jarvis/v2/browser/panes.py

## Errata (found by the FS0/FS1 test writers, 2026-10-19)

- **§4.2 example 5.** After both splits the counts are top = 4 and bottom = **4**. The plan's 4/3 holds only after the split on d alone. The FS1 tests check 4/4.
- **§4.4 / §8.3 caps.** The 12-leaf cap and the 23-node cap can't be tested on their own. With ids 1–12 and no duplicates, the leaf cap holds automatically, and reaching 23 nodes needs one-child wrappers.
- **§6.** `docs/hud-api.md:3016` and `:3047` also say 1–8, so FS0 changes them too. `docs/notes/browser.md:590` describes the Ctrl+Alt+1–8 keys, which stay 1–8 under FS-9, so it is left alone.
- **§7.1 (iii).** Three old `dropRow` rows for grid7 and grid8 change: the four-wide row stays four wide. FS1's table marks them as the new expectations.
- **`rectsOf`** must use a correctly rounded running sum when it computes offsets, or the FS-5 bit-identity property fails when a sibling is halved.
- **§10 (the 1 px rule; found in the FS3 lead sweep).** `L·100% + 1px` with `W·100% − 1px` takes the whole gap from the right or lower pane, so three equal columns drew 616/615/615 px instead of the grid's 615.33 each, and `hud_v2_scm_check`'s click on the Diff pane's modified editor landed on its scrollbar. FS3 instead places each section at `L·(100% + 1px)` with width `W·(100% + 1px) − 1px`, and the same for top and height. That is where the grid drew equal tracks. It still depends only on the pane's own fractions (C5), and every pair of touching edges keeps exactly a 1 px gap. Separators are centred on that gap.
