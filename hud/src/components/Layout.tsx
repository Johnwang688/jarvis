import { isSettingsKey } from "../lib/settings";
// The window's geometry, applied: the zoom, the two pane widths, folding a
// pane to a rail, the separators that resize them, the centre's panes and the
// bottom panel, and the title bar that toggles all of it (lib/layout.ts and
// lib/workspace.ts decide every number; this file only draws and listens).
//
// A folded pane is **hidden, never unmounted**: the sidebar's expanded
// projects, a half-typed steer in the task pane and an inline rename all live
// in component state, and folding a pane must not be how the owner loses them.
// The same holds for a centre pane a preset stops drawing (Workspace.tsx).
//
// **While an authorization card is up nothing here moves anything** (§18, the
// card rules): every layout key is swallowed, every title-bar button, fold
// button and rail button is disabled (the veil stops the pointer, but a button
// that kept focus when the card arrived would still answer Enter), an open
// layout menu closes, and a focused separator ignores its keys.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  DEFAULT_LAYOUT, PANE, ZOOM_DEFAULT, ZOOM_MAX, ZOOM_MIN, dragWidth, inMonaco, isZoom, keyWidth,
  loadLayout, loadZoom, saveLayout, saveZoom, shortcutFor, stepZoom, toCss, isEditorShortcut, type EditorShortcut,
  type Fitted, type PaneLayout, type Side,
} from "../lib/layout";
import {
  MENU_PRESETS, PANEL, SHAPES, TITLEBAR_H, cellsOf, closePane as closePaneOf, defaultWorkspace,
  equalSplit as equalSplitOf, fitWorkspace, focusPane,
  forgetTerminal as forgetTerminalOf, loadWorkspace, paneAt, pinPane, resetWorkspace, saveWorkspace, setPaneTerminal,
  renameTerminal as renameTerminalOf,
  setPanel, setPreset as setPresetOf, setKeepOrigin as setKeepOriginOf,
  setBrowserUrl as setBrowserUrlOf, submitBrowserUrl as submitBrowserUrlOf, setSplit as setSplitOf, setView as setViewOf, show as showOf,
  setFilePath as setFilePathOf, splitPane as splitPaneOf, splitVerdict as splitVerdictOf,
  unpinProject as unpinProjectOf, setEditors as setEditorsOf,
  type DrawnSet, type PaneNo, type PaneSpec, type Preset, type Split, type SplitWhy, type View, type Workspace,
  type WorkspaceFit, type BrowserTabOptions,
  setBrowserRemote as setBrowserRemoteOf, setBrowserProfile as setBrowserProfileOf, setBrowserSee as setBrowserSeeOf,
  addBrowserTab as addBrowserTabOf, closeBrowserTab as closeBrowserTabOf,
  activateBrowserTab as activateBrowserTabOf, setTabKeepOrigin as setTabKeepOriginOf,
} from "../lib/workspace";
import { browserTakesKey } from "../lib/remote";
import { inTerminal, terminalTakesKey } from "../lib/terminal";
import { chordSecond, isSaveKey, resetChord } from "../lib/editorkeys";
import { terminals, usePanelDot } from "./Terminal";
import type { Tabs } from "../lib/buffers";

export interface LayoutControl {
  zoom: number;
  /** What the owner chose (and what is stored). */
  layout: PaneLayout;
  /** What this render draws for the side panes: widths fitted to the window, panes it folded. */
  fitted: Fitted;
  /** The window's room in the HUD's own pixels: its width, and its height under the title bar. */
  available: number;
  availableH: number;
  /** The centre's panes and the panel, as chosen (stored) and as drawn. */
  ws: Workspace;
  fit: WorkspaceFit;
  setZoom: (level: number) => void;
  zoomBy: (dir: 1 | -1) => void;
  setWidth: (side: Side, width: number) => void;
  resetWidth: (side: Side) => void;
  open: (side: Side) => void;
  fold: (side: Side) => void;
  toggle: (side: Side) => void;
  setPreset: (preset: Preset) => void;
  setView: (pane: PaneNo, view: View) => void;
  focus: (pane: PaneNo) => void;
  /** A sidebar click: the pane already showing that kind of thing, else the focused pane. */
  show: (view: View) => void;
  pin: (pane: PaneNo, projectId: string | null) => void;
  /** A File pane's tabs (ED-2), pinning it to their project in the same step. */
  setEditors: (pane: PaneNo, projectId: string, tabs: Tabs | null) => void;
  /** A project went away; panes in `keep` (holding an unsaved edit) stay pinned. */
  unpinProject: (projectId: string, keep?: readonly PaneNo[]) => void;
  setBrowserUrl: (pane: PaneNo, url: string, id?: string) => void;
  submitBrowserUrl: (pane: PaneNo, url: string, id?: string) => void;
  setBrowserRemote: (pane: PaneNo, id: string, value: Parameters<typeof setBrowserRemoteOf>[3]) => void;
  setBrowserProfile: (pane: PaneNo, profile: string, initialOnly?: boolean) => void;
  setBrowserSee: (pane: PaneNo, see: boolean) => void;
  addBrowserTab: (pane: PaneNo, url: string, opts?: BrowserTabOptions) => void;
  closeBrowserTab: (pane: PaneNo, id: string) => void;
  activateBrowserTab: (pane: PaneNo, id: string) => void;
  setTabKeepOrigin: (pane: PaneNo, id: string, origin: string | null) => void;
  /** The file a File or Diff pane has open (null: none), read again when it mounts (WP-G2). */
  setFilePath: (pane: PaneNo, path: string | null) => void;
  /** A Browser pane's "keep its own origin" grant (an origin), or none (WP-E, W-4). */
  setKeepOrigin: (pane: PaneNo, origin: string | null) => void;
  /** The terminal a terminal pane shows (null: choose again); no other pane keeps it (W-1). */
  setTerminal: (pane: PaneNo, id: string | null) => void;
  /** A terminal was closed or ended: no pane holds it any more. */
  forgetTerminal: (id: string) => void;
  /** A terminal continued under a new id after a restart: every pane holding it holds the new one. */
  renameTerminal: (from: string, to: string) => void;
  setSplit: (preset: Preset, split: Partial<Split>) => void;
  equalSplit: (preset: Preset) => void;
  togglePanel: () => void;
  /**
   * The owner's own ⬓, the layout menu's Panel row and Ctrl+`: togglePanel,
   * and opening the panel this way opens a terminal when there is none (the
   * owner's call, 2026-10-11). A stored layout, a reload and a terminal shown
   * in the panel open it with togglePanel, which opens nothing. Given a
   * click, only its first counts: a double-click is one gesture, as Ctrl+`
   * ignores auto-repeat.
   */
  ownerTogglePanel: (click?: { detail?: number }) => void;
  setPanelHeight: (height: number) => void;
  resetPanelHeight: () => void;
  /** Reset layout: one pane, default widths, the panel closed. */
  resetAll: () => void;
  /**
   * Split (WP-G2, lib/workspace `splitPane`): pane `id` shows `spec` right
   * after `source`, on the ladder's shape for one pane more. `focus: false`
   * leaves focus where it is. An id ⊞ had hidden mounts fresh.
   */
  split: (source: PaneNo, id: PaneNo, spec: PaneSpec, opts?: { focus?: boolean }) => void;
  /** × (lib/workspace `closePane`): that pane goes, and mounts fresh if a split reuses its id; focus to `then` if drawn. */
  close: (pane: PaneNo, then?: PaneNo | null) => void;
  /** Whether one more pane can be drawn in this window now (`dirty`: ids holding an unsaved edit). */
  splitVerdict: (dirty?: readonly PaneNo[]) => { ok: boolean; why: SplitWhy };
  /**
   * Render-only, by pane id: how many times that id has been closed (or
   * taken back from ⊞'s hidden panes by a split). The workspace keys a pane
   * by id and incarnation, so an id a close frees and a split reuses mounts
   * fresh — never the old FileTab, frame or terminal slot.
   */
  incarnation: Readonly<Partial<Record<PaneNo, number>>>;
}

/**
 * Focus pane `pane` (an id) for the keyboard: its message box when it has
 * one, else the pane itself. A pane a split just made may not be drawn yet
 * when the change came from outside a React event (a socket, a timer): it
 * is looked for again for a few frames (review of PR #47).
 */
export function focusPaneElement(pane: PaneNo, frames = 10) {
  requestAnimationFrame(() => {
    const el = document.querySelector<HTMLElement>(`[data-testid="pane-${pane}"]`);
    if (!el || el.offsetParent === null) {
      if (frames > 0) focusPaneElement(pane, frames - 1);
      return;
    }
    const box = el.querySelector<HTMLElement>('[data-testid="input"]');
    (box && !(box as HTMLTextAreaElement).disabled ? box : el).focus();
  });
}

/**
 * The zoom, pane, workspace and panel state, persisted, with the keyboard
 * shortcuts bound. `blocked` is true while an authorization card is up: every
 * layout key is then swallowed and does nothing, as push-to-talk and the wake
 * word already do — a zoom or a fold would re-lay the card out under a pointer
 * that has not moved, which is how a click meant for DENY lands on something
 * else.
 */
export function useLayout(blocked = false, onEditorKey?: (what: EditorShortcut) => void,
                          settings?: { open: boolean; toggle: () => void }): LayoutControl {
  // Applied in the initializer too, so a window reopened at 140% does not
  // draw its first frame at 100% and then jump.
  const [zoom, setZoomState] = useState(() => {
    const z = loadZoom();
    document.documentElement.style.setProperty("--ui-zoom", String(z / 100));
    return z;
  });
  const [layout, setLayout] = useState<PaneLayout>(() => loadLayout());
  const [ws, setWs] = useState<Workspace>(() => loadWorkspace());
  const [viewport, setViewport] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }));
  // A pane the owner opened while the window had folded it: the window folds
  // the other one first from then on (lib/layout.ts, fitLayout). Not stored.
  const [prefer, setPrefer] = useState<Side | null>(null);
  // The vertical version: a panel the owner opened while the window would
  // fold it drops rows instead (lib/workspace.ts, fitWorkspace). Not stored.
  const [preferPanel, setPreferPanel] = useState(false);

  useEffect(() => {
    document.documentElement.style.setProperty("--ui-zoom", String(zoom / 100));
    saveZoom(zoom);
  }, [zoom]);
  useEffect(() => saveLayout(layout), [layout]);
  useEffect(() => saveWorkspace(ws), [ws]);
  useEffect(() => {
    const onResize = () => setViewport({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const available = viewport.w / (zoom / 100);
  const availableH = viewport.h / (zoom / 100) - TITLEBAR_H;
  // What the last render drew, so a dropped set stays put while focus moves
  // inside it (fitWorkspace, "sticky"). Render-only: never stored, and
  // writing it here is idempotent, so a repeated render draws the same.
  const drawnBefore = useRef<DrawnSet | null>(null);
  const fit = fitWorkspace(layout, ws, available, availableH, prefer, preferPanel, drawnBefore.current);
  drawnBefore.current = { preset: ws.preset, drawn: fit.drawn, panes: fit.panes };
  const fitted = fit.sides;
  const now = useRef({ fit, ws, layout, available, availableH, prefer, preferPanel });
  now.current = { fit, ws, layout, available, availableH, prefer, preferPanel };
  const blockedRef = useRef(blocked);
  blockedRef.current = blocked;
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const editorKeyRef = useRef(onEditorKey);
  editorKeyRef.current = onEditorKey;

  const setZoom = useCallback((level: number) => setZoomState(isZoom(level) ? level : ZOOM_DEFAULT), []);
  const zoomBy = useCallback((dir: 1 | -1) => setZoomState((z) => stepZoom(z, dir)), []);
  const setWidth = useCallback(
    (side: Side, width: number) => setLayout((l) => ({ ...l, [side]: width })),
    [],
  );
  const resetWidth = useCallback(
    (side: Side) => setLayout((l) => ({ ...l, [side]: DEFAULT_LAYOUT[side] })),
    [],
  );
  const open = useCallback((side: Side) => {
    const f = now.current.fit.sides;
    if (side === "left" ? f.autoLeft : f.autoRight) setPrefer(side);
    setLayout((l) => ({ ...l, [side === "left" ? "leftCollapsed" : "rightCollapsed"]: false }));
  }, []);
  const fold = useCallback((side: Side) => {
    setPrefer((p) => (p === side ? null : p));
    setLayout((l) => ({ ...l, [side === "left" ? "leftCollapsed" : "rightCollapsed"]: true }));
  }, []);
  const toggle = useCallback(
    (side: Side) => {
      const f = now.current.fit.sides;
      if (side === "left" ? f.leftFolded : f.rightFolded) open(side);
      else fold(side);
    },
    [open, fold],
  );

  const setPreset = useCallback((preset: Preset) => setWs((w) => setPresetOf(w, preset)), []);
  const setView = useCallback((pane: PaneNo, view: View) => setWs((w) => setViewOf(w, pane, view)), []);
  const focus = useCallback((pane: PaneNo) => setWs((w) => focusPane(w, pane)), []);
  const show = useCallback((view: View) => setWs((w) => showOf(w, view, now.current.fit.panes)), []);
  const pin = useCallback((pane: PaneNo, projectId: string | null) => setWs((w) => pinPane(w, pane, projectId)), []);
  const setEditors = useCallback(
    (pane: PaneNo, projectId: string, tabs: Tabs | null) => setWs((w) => setEditorsOf(w, pane, projectId, tabs)), [],
  );
  const unpinProject = useCallback(
    (projectId: string, keep: readonly PaneNo[] = []) => setWs((w) => unpinProjectOf(w, projectId, keep)), [],
  );
  const setBrowserUrl = useCallback((pane: PaneNo, url: string, id?: string) => setWs((w) => setBrowserUrlOf(w, pane, url, id)), []);
  const submitBrowserUrl = useCallback((pane: PaneNo, url: string, id?: string) => setWs((w) => submitBrowserUrlOf(w, pane, url, id)), []);
  const setBrowserRemote = useCallback((pane: PaneNo, id: string, value: Parameters<typeof setBrowserRemoteOf>[3]) => setWs((w) => setBrowserRemoteOf(w, pane, id, value)), []);
  const setBrowserProfile = useCallback((pane: PaneNo, profile: string, initialOnly = false) => setWs((w) => setBrowserProfileOf(w, pane, profile, initialOnly)), []);
  const setBrowserSee = useCallback((pane: PaneNo, see: boolean) => setWs((w) => setBrowserSeeOf(w, pane, see)), []);
  const addBrowserTab = useCallback((pane: PaneNo, url: string, opts?: BrowserTabOptions) => setWs((w) => addBrowserTabOf(w, pane, url, opts)), []);
  const closeBrowserTab = useCallback((pane: PaneNo, id: string) => setWs((w) => closeBrowserTabOf(w, pane, id)), []);
  const activateBrowserTab = useCallback((pane: PaneNo, id: string) => setWs((w) => activateBrowserTabOf(w, pane, id)), []);
  const setTabKeepOrigin = useCallback((pane: PaneNo, id: string, origin: string | null) => setWs((w) => setTabKeepOriginOf(w, pane, id, origin)), []);
  const setFilePath = useCallback(
    (pane: PaneNo, path: string | null) => setWs((w) => setFilePathOf(w, pane, path)), [],
  );
  const setKeepOrigin = useCallback(
    (pane: PaneNo, origin: string | null) => setWs((w) => setKeepOriginOf(w, pane, origin)), [],
  );
  const setTerminal = useCallback((pane: PaneNo, id: string | null) => setWs((w) => setPaneTerminal(w, pane, id)), []);
  const forgetTerminal = useCallback((id: string) => setWs((w) => forgetTerminalOf(w, id)), []);
  const renameTerminal = useCallback((from: string, to: string) => setWs((w) => renameTerminalOf(w, from, to)), []);
  const setSplit = useCallback(
    (preset: Preset, split: Partial<Split>) => setWs((w) => setSplitOf(w, preset, split)), [],
  );
  const equalSplit = useCallback((preset: Preset) => setWs((w) => equalSplitOf(w, preset)), []);
  const setPanelHeight = useCallback((height: number) => setWs((w) => setPanel(w, { height })), []);
  const resetPanelHeight = useCallback(() => setWs((w) => setPanel(w, { height: PANEL.def })), []);
  const togglePanel = useCallback(() => {
    const at = now.current;
    if (at.fit.panel.open) {
      setPreferPanel(false);
      setWs((w) => setPanel(w, { open: false }));
      return;
    }
    // Opening: a panel the window folded (or would fold the moment it opens)
    // drops rows instead, so the click is never a button that does nothing.
    const opened = setPanel(at.ws, { open: true });
    const trial = fitWorkspace(at.layout, opened, at.available, at.availableH, at.prefer, false);
    setPreferPanel(at.fit.panel.auto || trial.panel.auto);
    setWs((w) => setPanel(w, { open: true }));
  }, []);
  // A pane closed, or a hidden one a split takes back, mounts fresh next time.
  const [incarnation, setIncarnation] = useState<Partial<Record<PaneNo, number>>>({});
  const bump = useCallback(
    (pane: PaneNo) => setIncarnation((m) => ({ ...m, [pane]: (m[pane] ?? 0) + 1 })), [],
  );
  const split = useCallback((source: PaneNo, id: PaneNo, spec: PaneSpec, opts: { focus?: boolean } = {}) => {
    if (blockedRef.current) return;
    if (now.current.ws.kept.includes(id)) bump(id);
    setWs((w) => splitPaneOf(w, source, id, spec, opts));
  }, [bump]);
  const close = useCallback((pane: PaneNo, then: PaneNo | null = null) => {
    if (blockedRef.current) return;
    const at = now.current.ws;
    if (closePaneOf(at, pane) === at) return;
    bump(pane);
    setWs((w) => closePaneOf(w, pane, then));
  }, [bump]);
  const splitVerdict = useCallback((dirty: readonly PaneNo[] = []) => {
    const at = now.current;
    return splitVerdictOf(at.layout, at.ws, at.available, at.availableH, at.prefer, at.preferPanel, dirty);
  }, []);
  const ownerTogglePanel = useCallback((click?: { detail?: number }) => {
    if (blockedRef.current || (click?.detail ?? 0) > 1) return;
    const opening = !now.current.fit.panel.open;
    togglePanel();
    // Closed again before the terminal is made: it is not opened, nor the panel reopened.
    if (opening) void terminals.ownerOpened("panel", () => now.current.fit.panel.open);
  }, [togglePanel]);
  const resetAll = useCallback(() => {
    setWs((w) => resetWorkspace(w));
    setLayout({ ...DEFAULT_LAYOUT });
    setPrefer(null);
    setPreferPanel(false);
  }, []);

  // **The one place the HUD's layout keys are taken** (push-to-talk's Ctrl+D
  // is App's, the same way, and shares the chord tracker below). Capture
  // phase on the window: the input bar stops its keys from bubbling, and
  // these keys must work from the box the owner's cursor usually sits in.
  // The zoom keys are the HUD's everywhere, Monaco and the input bar included — letting one through is Chrome's page zoom, which
  // this control cannot see and the browser remembers per site. Ctrl+B folds
  // the sidebar from Monaco too (decisions E-10), ignores auto-repeat
  // (holding it would flap the pane), and nothing fires mid-composition.
  // Ctrl+` and Ctrl+Alt+1 … 8 ignore auto-repeat too.
  //
  // Monaco's Ctrl+K chords (ED-1): the second key of one typed in an editor
  // is Monaco's, whatever HUD key it is — Ctrl+K Ctrl+B sets the selection
  // anchor, Ctrl+K Ctrl+0 folds all — and is still kept from Chrome. Every
  // keydown feeds the tracker (lib/editorkeys.ts), so a lone Ctrl+B is told
  // apart from a chord's. The tracker is the window's, shared with
  // push-to-talk, which leaves a chord's Ctrl+D to Monaco the same way.
  //
  // Ctrl+S: Chrome's "Save page as" never opens. In an editor that editor's
  // own Ctrl+S action saves its buffer (components/Editor.tsx); in a terminal
  // it is the shell's (XOFF, readline's forward search).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const step = { second: chordSecond(e, inMonaco(e.target)) };
      if (e.isComposing) return;
      if (isSettingsKey(e)) {
        e.preventDefault();
        if (step.second && !blockedRef.current) return;
        e.stopPropagation();
        if (!e.repeat && !blockedRef.current) settingsRef.current?.toggle();
        return;
      }
      const what = shortcutFor(e);
      const save = isSaveKey(e);
      if (!what && !save) return;
      if (blockedRef.current) {
        e.preventDefault();
        return;
      }
      if (settingsRef.current?.open && !what?.startsWith("zoom")) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      // In a terminal the shell gets Ctrl+B and Ctrl+Alt+B (readline back-
      // char; page up in less and vim), Ctrl+_ (readline undo) and Ctrl+S.
      // The zoom keys, Ctrl+` and Ctrl+Alt+N stay the HUD's there too.
      if (inTerminal(e.target) && (save || terminalTakesKey(what, e.key))) return;
      if (e.target instanceof Element && e.target.closest("[data-remote-input]") && browserTakesKey(e)) return;
      if (step.second || save || !what) {
        e.preventDefault();
        return;
      }
      if (isEditorShortcut(what)) {
        // Quick open, the palette, find in files (ED-7): never Chrome's print
        // dialog, and stopped here so Monaco never sees them (its F1 would
        // open its own quick command over ours). A pending chord's second
        // key was left to Monaco above; under a card nothing opens (above).
        e.preventDefault();
        e.stopPropagation();
        if (e.repeat) return;
        editorKeyRef.current?.(what);
        return;
      }
      if (what === "toggleLeft" || what === "toggleRight") {
        e.preventDefault();
        if (e.repeat) return;
        toggle(what === "toggleLeft" ? "left" : "right");
        return;
      }
      if (what === "togglePanel") {
        e.preventDefault();
        if (e.repeat) return;
        // VS Code's key: opening the panel opens a terminal when there is none.
        ownerTogglePanel();
        return;
      }
      if (what.startsWith("focus")) {
        // Slot N, never id N (plan G-1): the pane the owner sees N-th in
        // reading order. Only a slot the preset has: Ctrl+Alt+3 in two
        // columns is left alone. A pane the window dropped for room is
        // brought back into the drawn set (fitWorkspace keeps a dropped set
        // only while it holds the focus).
        const pane = paneAt(now.current.ws, Number(what.slice(5)));
        if (pane === null) return;
        e.preventDefault();
        if (e.repeat) return;
        setWs((w) => focusPane(w, pane));
        focusPaneElement(pane);
        return;
      }
      e.preventDefault();
      if (what === "zoomReset") setZoomState(ZOOM_DEFAULT);
      else setZoomState((z) => stepZoom(z, what === "zoomIn" ? 1 : -1));
    };
    // Monaco leaves a chord when the document loses focus (it polls
    // `document.hasFocus()`, which stays true while a frame inside has it);
    // so does the tracker.
    const onBlur = () => {
      setTimeout(() => {
        if (!document.hasFocus()) resetChord();
      }, 0);
    };
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("blur", onBlur);
    };
  }, [toggle, ownerTogglePanel]);

  return {
    ownerTogglePanel,
    zoom, layout, fitted, available, availableH, ws, fit,
    setZoom, zoomBy, setWidth, resetWidth, open, fold, toggle,
    setPreset, setView, focus, show, pin, setEditors, unpinProject, setBrowserUrl, submitBrowserUrl, setFilePath, setKeepOrigin,
    addBrowserTab, closeBrowserTab, activateBrowserTab, setTabKeepOrigin, setBrowserRemote, setBrowserProfile, setBrowserSee,
    setTerminal, forgetTerminal, renameTerminal, setSplit,
    equalSplit, togglePanel, setPanelHeight, resetPanelHeight, resetAll, split, close, splitVerdict, incarnation,
  };
}

/**
 * A draggable edge, on either axis: a 7px hit area over a 1px rule (it takes
 * no room), a resize cursor, and the accent line on hover, focus and while
 * dragging. Keyboard: the caller's `onKey` (arrows, Shift for a bigger step,
 * Home and End); a double-click resets. Space on a focused separator does
 * nothing. `orientation` is ARIA's: a "vertical" separator stands
 * between left and right and moves along x.
 */
export function Separator(props: {
  testid: string;
  className: string;
  orientation: "vertical" | "horizontal";
  label: string;
  title?: string;
  value: number;
  min: number;
  max: number;
  zoom: number;
  blocked?: boolean;
  style?: React.CSSProperties;
  /** The value when the drag began, and the pointer's travel since, in zoomed px along the axis. */
  onDrag: (start: number, delta: number) => void;
  /** A key on the focused separator; true when it was used. */
  onKey: (key: string, shift: boolean) => boolean;
  onReset: () => void;
}) {
  const start = useRef<{ pos: number; value: number; id: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const x = props.orientation === "vertical";

  useEffect(() => {
    document.body.classList.toggle("resizing", dragging);
    document.body.classList.toggle("resizing-rows", dragging && !x);
    return () => {
      document.body.classList.remove("resizing");
      document.body.classList.remove("resizing-rows");
    };
  }, [dragging, x]);

  const end = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!start.current) return;
    try {
      e.currentTarget.releasePointerCapture(start.current.id);
    } catch {
      /* already released */
    }
    start.current = null;
    setDragging(false);
  };

  return (
    <div
      className={props.className + (dragging ? " dragging" : "")}
      data-testid={props.testid}
      role="separator"
      aria-orientation={props.orientation}
      aria-label={props.label}
      aria-valuenow={Math.round(props.value)}
      aria-valuemin={Math.round(props.min)}
      aria-valuemax={Math.round(props.max)}
      aria-disabled={props.blocked ? true : undefined}
      tabIndex={0}
      title={props.title ?? `${props.label} · double-click to reset`}
      style={props.style}
      onPointerDown={(e) => {
        if (e.button !== 0 || props.blocked) return;
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        start.current = { pos: x ? e.clientX : e.clientY, value: props.value, id: e.pointerId };
        setDragging(true);
      }}
      onPointerMove={(e) => {
        const s = start.current;
        if (!s) return;
        // clientX/Y are in screen pixels; every value is in the zoomed space.
        props.onDrag(s.value, toCss((x ? e.clientX : e.clientY) - s.pos, props.zoom));
      }}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={() => {
        start.current = null;
        setDragging(false);
      }}
      onDoubleClick={() => {
        if (!props.blocked) props.onReset();
      }}
      // Space on a focused separator does nothing (no scroll behind it).
      onKeyDown={(e) => {
        if (e.code === "Space" || e.key === " ") {
          e.preventDefault();
          return;
        }
        if (props.blocked) return;
        if (!props.onKey(e.key, e.shiftKey)) return;
        e.preventDefault();
        e.stopPropagation();
      }}
    />
  );
}

/**
 * The draggable edge between a side pane and the centre. Keyboard: the arrows
 * move the edge (Shift for a bigger step), Home and End go to the ends, and a
 * double-click puts the default width back.
 */
export function Splitter(props: {
  side: Side;
  width: number;
  max: number;
  zoom: number;
  blocked?: boolean;
  onResize: (width: number) => void;
  onReset: () => void;
}) {
  const label = props.side === "left" ? "Resize the sidebar" : "Resize the status pane";
  return (
    <Separator
      testid={`split-${props.side}`}
      className={"splitter " + props.side}
      orientation="vertical"
      label={label}
      title={`${label} · double-click for the default width`}
      value={props.width}
      min={PANE[props.side].min}
      max={props.max}
      zoom={props.zoom}
      blocked={props.blocked}
      onDrag={(start, delta) => props.onResize(dragWidth(props.side, start, delta, props.max))}
      onKey={(key, shift) => {
        const w = keyWidth(props.side, props.width, key, shift, props.max);
        if (w === null) return false;
        props.onResize(w);
        return true;
      }}
      onReset={props.onReset}
    />
  );
}

/** What a folded pane leaves behind: a thin rail that brings it back. */
export function Rail(props: {
  side: Side;
  onExpand: () => void;
  onNewThread?: () => void;
  approvals?: number;
  error?: boolean;
  /** Folded by the window (too narrow for it), not by the owner. */
  auto?: boolean;
  /** An authorization card is up: nothing here may re-lay the window out. */
  disabled?: boolean;
}) {
  const left = props.side === "left";
  return (
    <div
      className={"rail " + props.side}
      id={`rail-${props.side}`}
      data-testid={`rail-${props.side}`}
      data-auto={props.auto ? "true" : undefined}
      title={props.auto ? "Folded to give the centre room; opening it folds the other pane first" : undefined}
    >
      <button
        type="button"
        className="railbtn"
        data-testid={`expand-${props.side}`}
        title={left ? "Show the sidebar (Ctrl+B)" : "Show the status pane (Ctrl+Alt+B)"}
        aria-label={left ? "Show the sidebar" : "Show the status pane"}
        aria-expanded={false}
        disabled={props.disabled}
        onClick={props.onExpand}
      >
        {left ? "»" : "«"}
      </button>
      {left && props.onNewThread ? (
        <button
          type="button"
          className="railbtn plus"
          data-testid="rail-new-thread"
          title="New thread"
          aria-label="New thread"
          disabled={props.disabled}
          onClick={props.onNewThread}
        >
          +
        </button>
      ) : null}
      {/* A folded status pane still says when something there wants the
          owner: an approval queue (amber, pending) or an error (red). */}
      {!left && props.approvals ? (
        <button
          type="button"
          className="railmark amber"
          data-testid="rail-approvals"
          title={`${props.approvals} authorization${props.approvals === 1 ? "" : "s"} pending`}
          disabled={props.disabled}
          onClick={props.onExpand}
        >
          {props.approvals}
        </button>
      ) : null}
      {!left && props.error ? (
        <button
          type="button"
          className="railmark red"
          data-testid="rail-error"
          title="An error is showing in the status pane"
          disabled={props.disabled}
          onClick={props.onExpand}
        >
          !
        </button>
      ) : null}
    </div>
  );
}

/** The button inside a pane that folds it. */
export function CollapseButton(props: { side: Side; onCollapse: () => void; disabled?: boolean }) {
  const left = props.side === "left";
  return (
    <button
      type="button"
      className="collapse"
      data-testid={`collapse-${props.side}`}
      title={left ? "Hide the sidebar (Ctrl+B)" : "Hide the status pane (Ctrl+Alt+B)"}
      aria-label={left ? "Hide the sidebar" : "Hide the status pane"}
      aria-expanded={true}
      disabled={props.disabled}
      onClick={props.onCollapse}
    >
      {left ? "«" : "»"}
    </button>
  );
}

/** − 100% + — the percentage resets to 100%. */
export function ZoomControl(props: {
  zoom: number; onStep: (dir: 1 | -1) => void; onReset: () => void; disabled?: boolean;
}) {
  return (
    <div className="zoomctl" data-testid="zoom-control" role="group" aria-label="Zoom">
      <button
        type="button"
        data-testid="zoom-out"
        title="Zoom out (Ctrl+-)"
        aria-label="Zoom out"
        disabled={props.disabled || props.zoom <= ZOOM_MIN}
        onClick={() => props.onStep(-1)}
      >
        −
      </button>
      <button
        type="button"
        className="pct"
        data-testid="zoom-reset"
        title="Reset zoom to 100% (Ctrl+0)"
        aria-label={`Zoom ${props.zoom}%, reset to 100%`}
        disabled={props.disabled}
        onClick={props.onReset}
      >
        {props.zoom}%
      </button>
      <button
        type="button"
        data-testid="zoom-in"
        title="Zoom in (Ctrl+=)"
        aria-label="Zoom in"
        disabled={props.disabled || props.zoom >= ZOOM_MAX}
        onClick={() => props.onStep(1)}
      >
        +
      </button>
    </div>
  );
}

// ---- the title bar (2026-10-09, plan §2.1) -------------------------------------

type Area = "left" | "panel" | "right" | "grid" | "split";

/**
 * A window outline, 16×16, with the area the button controls filled with
 * `currentColor` when open and drawn as an outline when closed.
 */
export function AreaIcon(props: { area: Area; open: boolean }) {
  const { area, open } = props;
  const band =
    area === "left" ? { x: 1.5, y: 1.5, w: 4.5, h: 13 }
    : area === "right" ? { x: 10, y: 1.5, w: 4.5, h: 13 }
    : area === "panel" ? { x: 1.5, y: 9.5, w: 13, h: 5 }
    : null;
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <rect x="1.5" y="1.5" width="13" height="13" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
      {area === "split" ? (
        // A pane's split button (WP-G2): the outline and one divider, VS Code's split.
        <line x1="8" y1="1.5" x2="8" y2="14.5" stroke="currentColor" strokeWidth="1.2" />
      ) : band ? (
        <rect
          x={band.x} y={band.y} width={band.w} height={band.h}
          fill={open ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.2"
        />
      ) : (
        <>
          <line x1="8" y1="1.5" x2="8" y2="14.5" stroke="currentColor" strokeWidth="1.2" />
          <line x1="1.5" y1="8" x2="14.5" y2="8" stroke="currentColor" strokeWidth="1.2" />
        </>
      )}
    </svg>
  );
}

/**
 * A preset tile's diagram: the shape's panes as small rectangles, drawn from
 * the shape itself (lib/workspace `cellsOf`, at its default edges), so a
 * tile can never disagree with what choosing it draws.
 */
function PresetIcon(props: { preset: Preset }) {
  const cells = cellsOf(props.preset, defaultWorkspace().splits[props.preset]);
  const gap = 1;
  return (
    <svg width="24" height="16" viewBox="0 0 24 16" aria-hidden="true" focusable="false">
      {cells.map((c, i) => {
        const x = 1 + c.left * 22 + (c.left > 0 ? gap / 2 : 0);
        const y = 1 + c.top * 14 + (c.top > 0 ? gap / 2 : 0);
        const w = (c.right - c.left) * 22 - (c.left > 0 ? gap / 2 : 0) - (c.right < 1 ? gap / 2 : 0);
        const h = (c.bottom - c.top) * 14 - (c.top > 0 ? gap / 2 : 0) - (c.bottom < 1 ? gap / 2 : 0);
        return (
          <rect key={i} x={x} y={y} width={w} height={h} rx="1" fill="none" stroke="currentColor" strokeWidth="1.1" />
        );
      })}
    </svg>
  );
}

interface MenuAt {
  right: number;
  top: number;
}

/** Placed under its opener, right edges aligned: screen px in, zoomed px out (`toCss`). */
function menuAt(el: HTMLElement, zoom: number): MenuAt {
  const r = el.getBoundingClientRect();
  return {
    right: Math.max(4, Math.round(toCss(window.innerWidth - r.right, zoom))),
    top: Math.round(toCss(r.bottom, zoom)) + 2,
  };
}

/**
 * A small menu: closed by Escape (focus back to its opener) and by a press
 * anywhere else; the arrow keys move between its items (`data-mi`), and Space
 * on one presses it.
 */
function MenuShell(props: {
  at: MenuAt;
  testid: string;
  label: string;
  opener: React.RefObject<HTMLButtonElement>;
  onClose: () => void;
  /** Items per row in the first group (the preset tiles), for Up and Down. */
  gridCols?: number;
  gridCount?: number;
  children: React.ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  const { onClose, opener } = props;
  // It opens on the chosen tile (a `menuitemradio`), else on the first item —
  // never on a checked toggle row (`menuitemcheckbox`): with 3 over 2 or 4
  // over 3 current no tile is checked, and focus used to land on the sidebar
  // toggle, where Enter folded the sidebar (review of PR #37).
  useLayoutEffect(() => {
    const first =
      box.current?.querySelector<HTMLElement>('[data-mi][role="menuitemradio"][aria-checked="true"]')
      || box.current?.querySelector<HTMLElement>("[data-mi]");
    first?.focus();
  }, []);
  useEffect(() => {
    const close = () => onClose();
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      onClose();
      opener.current?.focus();
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc, true);
    };
  }, [onClose, opener]);

  const move = (e: React.KeyboardEvent) => {
    const items = Array.from(box.current?.querySelectorAll<HTMLElement>("[data-mi]") || []).filter(
      (b) => !(b as HTMLButtonElement).disabled,
    );
    const from = items.indexOf(document.activeElement as HTMLElement);
    if (from < 0 || !items.length) return;
    const n = items.length;
    const cols = props.gridCols || 0;
    const tiles = props.gridCount || 0;
    let to: number;
    if (cols && from < tiles && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      const step = e.key === "ArrowDown" ? cols : -cols;
      to = from + step;
      if (to < 0) to = n - 1;
      else if (to >= tiles) to = e.key === "ArrowDown" ? Math.min(tiles, n - 1) : from;
    } else if (e.key === "Home") {
      to = 0;
    } else if (e.key === "End") {
      to = n - 1;
    } else {
      const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1;
      to = (from + d + n) % n;
    }
    items[to].focus();
  };

  return (
    <div
      ref={box}
      className="laymenu"
      role="menu"
      aria-label={props.label}
      data-testid={props.testid}
      style={{ right: props.at.right, top: props.at.top }}
      onMouseDown={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
          e.preventDefault();
          e.stopPropagation();
          move(e);
        }
      }}
    >
      {props.children}
    </div>
  );
}

/** The tooltip of a side or panel toggle: what it does, and the shortcut. */
function toggleTitle(area: "left" | "right" | "panel", open: boolean, auto: boolean): string {
  const name = area === "left" ? "sidebar" : area === "right" ? "status pane" : "panel";
  const key = area === "left" ? "Ctrl+B" : area === "right" ? "Ctrl+Alt+B" : "Ctrl+`";
  if (open) return `Hide the ${name} (${key})`;
  if (auto && area === "panel") {
    return `Show the panel. The window is too short for it, so panes are dropped instead (${key})`;
  }
  if (auto) {
    const other = area === "left" ? "status pane" : "sidebar";
    return `Show the ${name}. The window is too narrow for both, so the ${other} folds instead (${key})`;
  }
  return `Show the ${name} (${key})`;
}

/** Window controls stay reachable in every fold state. Settings covers the
 * workspace while zoom remains available; layout controls wait until close. */
export function TitleBar(props: {
  view: LayoutControl;
  blocked: boolean;
  settingsOpen?: boolean;
  settingsDisabled?: boolean;
  onSettings: () => void;
}) {
  const v = props.view;
  const { blocked } = props;
  const [layoutMenu, setLayoutMenu] = useState<MenuAt | null>(null);
  const customize = useRef<HTMLButtonElement>(null);
  const closeLayout = useCallback(() => setLayoutMenu(null), []);
  // Where a menu opens is read **in the click**, never inside the updater:
  // React runs an updater during the click only when nothing is pending, and
  // otherwise at render time, when the event's `currentTarget` is already
  // null — `getBoundingClientRect` then threw inside render and, with nothing
  // to catch it, unmounted the whole window, an approval card included (three
  // quick clicks on ⊞ did it). An updater reads only its own argument.
  const toggleMenu = (set: typeof setLayoutMenu, e: React.MouseEvent<HTMLButtonElement>) => {
    const at = menuAt(e.currentTarget, v.zoom);
    set((m) => (m ? null : at));
  };

  // An authorization card closes any open menu: nothing behind it may move.
  useEffect(() => {
    if (blocked || props.settingsOpen) setLayoutMenu(null);
  }, [blocked, props.settingsOpen]);

  const sides = v.fit.sides;
  const dropped = v.fit.dropped;
  // A terminal in the hidden panel has exited (plan §2.3): a dot on ⬓.
  const panelDot = usePanelDot() && !v.fit.panel.open;
  // `act` gets the click: ⬓ counts only a double-click's first (ownerTogglePanel).
  const toggles: {
    area: "left" | "panel" | "right"; open: boolean; auto: boolean; act: (click: React.MouseEvent) => void;
  }[] = [
    { area: "left", open: !sides.leftFolded, auto: sides.autoLeft, act: () => v.toggle("left") },
    { area: "panel", open: v.fit.panel.open, auto: v.fit.panel.auto, act: v.ownerTogglePanel },
    { area: "right", open: !sides.rightFolded, auto: sides.autoRight, act: () => v.toggle("right") },
  ];

  return (
    <header id="titlebar" data-testid="titlebar">
      <div className="tb-group tb-tools">
        <button type="button" className="tb-icon" data-testid="titlebar-settings"
          aria-label="Settings" title="Settings (Ctrl+,)" aria-expanded={!!props.settingsOpen}
          disabled={blocked || props.settingsDisabled}
          onMouseDown={(e) => e.preventDefault()} onClick={props.onSettings}>⚙</button>
      </div>
      <span className="tb-sep" aria-hidden="true" />
      <ZoomControl
        zoom={v.zoom}
        disabled={blocked}
        onStep={v.zoomBy}
        onReset={() => v.setZoom(ZOOM_DEFAULT)}
      />
      <span className="tb-sep" aria-hidden="true" />
      <div className="tb-group tb-toggles" role="group" aria-label="Layout">
        <button
          type="button"
          ref={customize}
          className="tb-icon"
          data-testid="layout-customize"
          title={dropped ? `Customize layout · showing ${v.fit.panes.length} of ${SHAPES[v.ws.preset].panes} panes` : "Customize layout"}
          aria-label="Customize layout"
          aria-haspopup="menu"
          aria-expanded={!!layoutMenu}
          aria-pressed={!!layoutMenu}
          data-dropped={dropped || undefined}
          disabled={blocked || props.settingsOpen}
          onMouseDown={(e) => e.stopPropagation()}
          onClick={(e) => toggleMenu(setLayoutMenu, e)}
        >
          <AreaIcon area="grid" open={false} />
          {dropped ? <span className="tb-badge" data-testid="layout-badge">{dropped}</span> : null}
        </button>
        {toggles.map((t) => (
          <button
            type="button"
            key={t.area}
            className="tb-icon"
            data-testid={`toggle-${t.area}`}
            title={toggleTitle(t.area, t.open, t.auto)}
            aria-label={`${t.open ? "Hide" : "Show"} the ${t.area === "left" ? "sidebar" : t.area === "right" ? "status pane" : "panel"}`}
            aria-pressed={t.open}
            data-auto={t.auto ? "true" : undefined}
            disabled={blocked || props.settingsOpen}
            onClick={t.act}
          >
            <AreaIcon area={t.area} open={t.open} />
            {t.area === "panel" && panelDot ? (
              <span className="tb-dot" data-testid="toggle-panel-dot" title="A terminal in the panel has exited" />
            ) : null}
          </button>
        ))}
      </div>

      {layoutMenu ? (
        <LayoutMenu view={v} at={layoutMenu} opener={customize} onClose={closeLayout} />
      ) : null}
    </header>
  );
}

/**
 * ⊞'s menu, the one entry point for presets: eight tiles, four to a row (the
 * stored preset checked; 3 over 2 and 4 over 3 have no tile, so with one of
 * them chosen no tile is), the three toggles written out with their
 * shortcuts, Reset layout, and — when the window is drawing fewer panes than
 * the preset has — a line saying so.
 */
export function LayoutMenu(props: {
  view: LayoutControl;
  at: MenuAt;
  opener: React.RefObject<HTMLButtonElement>;
  onClose: () => void;
}) {
  const v = props.view;
  const sides = v.fit.sides;
  const rows: {
    area: "left" | "panel" | "right"; label: string; key: string; on: boolean; act: (click: React.MouseEvent) => void;
  }[] = [
    { area: "left", label: "Sidebar", key: "Ctrl+B", on: !sides.leftFolded, act: () => v.toggle("left") },
    { area: "panel", label: "Panel", key: "Ctrl+`", on: v.fit.panel.open, act: v.ownerTogglePanel },
    { area: "right", label: "Status pane", key: "Ctrl+Alt+B", on: !sides.rightFolded, act: () => v.toggle("right") },
  ];
  const total = SHAPES[v.ws.preset].panes;
  return (
    <MenuShell
      at={props.at}
      testid="layout-menu"
      label="Customize layout"
      opener={props.opener}
      onClose={props.onClose}
      gridCols={4}
      gridCount={MENU_PRESETS.length}
    >
      <div className="lm-head" data-testid="layout-head">Layout · {total} pane{total === 1 ? "" : "s"}</div>
      <div className="lm-tiles" role="group" aria-label="Panes">
        {MENU_PRESETS.map((p) => (
          <button
            type="button"
            key={p}
            role="menuitemradio"
            data-mi=""
            className={"lm-tile" + (v.ws.preset === p ? " on" : "")}
            data-testid={`layout-preset-${p}`}
            aria-checked={v.ws.preset === p}
            title={SHAPES[p].label}
            aria-label={SHAPES[p].label}
            onClick={() => v.setPreset(p)}
          >
            <PresetIcon preset={p} />
          </button>
        ))}
      </div>
      {v.fit.dropped ? (
        <div className="lm-note" data-testid="layout-dropped">
          Showing {v.fit.panes.length} of {total} panes: the window is too {v.fit.why === "short" ? "short" : "narrow"}
        </div>
      ) : null}
      <div className="lm-sep" />
      {rows.map((r) => (
        <button
          type="button"
          key={r.area}
          role="menuitemcheckbox"
          data-mi=""
          className="lm-row"
          data-testid={`layout-toggle-${r.area}`}
          aria-checked={r.on}
          onClick={r.act}
        >
          <AreaIcon area={r.area} open={r.on} />
          <span className="lm-label">{r.label}</span>
          <span className="lm-key">{r.key}</span>
        </button>
      ))}
      <div className="lm-sep" />
      <button
        type="button"
        role="menuitem"
        data-mi=""
        className="lm-row"
        data-testid="layout-reset"
        onClick={() => {
          v.resetAll();
          props.onClose();
          props.opener.current?.focus();
        }}
      >
        <span className="lm-label">Reset layout</span>
      </button>
    </MenuShell>
  );
}
