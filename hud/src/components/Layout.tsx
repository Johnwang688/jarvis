// The window's geometry, applied: the zoom, the two pane widths, folding a
// pane to a rail, and the separators that resize them (lib/layout.ts decides
// every number; this file only draws and listens).
//
// A folded pane is **hidden, never unmounted**: the sidebar's expanded
// projects, a half-typed steer in the task pane and an inline rename all live
// in component state, and folding a pane must not be how the owner loses them.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DEFAULT_LAYOUT, PANE, ZOOM_DEFAULT, ZOOM_MAX, ZOOM_MIN, dragWidth, fitLayout, inMonaco, isZoom, keyWidth,
  loadLayout, loadZoom, saveLayout, saveZoom, shortcutFor, stepZoom, toCss,
  type Fitted, type PaneLayout, type Side,
} from "../lib/layout";

export interface LayoutControl {
  zoom: number;
  /** What the owner chose (and what is stored). */
  layout: PaneLayout;
  /** What this render draws: widths fitted to the window, panes it folded. */
  fitted: Fitted;
  available: number;
  setZoom: (level: number) => void;
  zoomBy: (dir: 1 | -1) => void;
  setWidth: (side: Side, width: number) => void;
  resetWidth: (side: Side) => void;
  open: (side: Side) => void;
  fold: (side: Side) => void;
  toggle: (side: Side) => void;
}

/**
 * The zoom and pane state, persisted, with the keyboard shortcuts bound.
 * `blocked` is true while an authorization card is up: every layout key is
 * then swallowed and does nothing, as push-to-talk and the wake word already
 * do — a zoom or a fold would re-lay the card out under a pointer that has
 * not moved, which is how a click meant for DENY lands on something else.
 */
export function useLayout(blocked = false): LayoutControl {
  // Applied in the initializer too, so a window reopened at 140% does not
  // draw its first frame at 100% and then jump.
  const [zoom, setZoomState] = useState(() => {
    const z = loadZoom();
    document.documentElement.style.setProperty("--ui-zoom", String(z / 100));
    return z;
  });
  const [layout, setLayout] = useState<PaneLayout>(() => loadLayout());
  const [viewport, setViewport] = useState(() => window.innerWidth);
  // A pane the owner opened while the window had folded it: the window folds
  // the other one first from then on (lib/layout.ts, fitLayout). Not stored.
  const [prefer, setPrefer] = useState<Side | null>(null);

  useEffect(() => {
    document.documentElement.style.setProperty("--ui-zoom", String(zoom / 100));
    saveZoom(zoom);
  }, [zoom]);
  useEffect(() => saveLayout(layout), [layout]);
  useEffect(() => {
    const onResize = () => setViewport(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const available = viewport / (zoom / 100);
  const fitted = fitLayout(layout, available, prefer);
  const fittedRef = useRef(fitted);
  fittedRef.current = fitted;
  const blockedRef = useRef(blocked);
  blockedRef.current = blocked;

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
    const f = fittedRef.current;
    if (side === "left" ? f.autoLeft : f.autoRight) setPrefer(side);
    setLayout((l) => ({ ...l, [side === "left" ? "leftCollapsed" : "rightCollapsed"]: false }));
  }, []);
  const fold = useCallback((side: Side) => {
    setPrefer((p) => (p === side ? null : p));
    setLayout((l) => ({ ...l, [side === "left" ? "leftCollapsed" : "rightCollapsed"]: true }));
  }, []);
  const toggle = useCallback(
    (side: Side) => {
      const f = fittedRef.current;
      if (side === "left" ? f.leftFolded : f.rightFolded) open(side);
      else fold(side);
    },
    [open, fold],
  );

  // Capture phase on the window: the input bar stops its keys from bubbling
  // (so Space typed there is not push-to-talk), and these keys must work from
  // the box the owner's cursor usually sits in. The zoom keys are the HUD's
  // everywhere, Monaco and the input bar included — neither binds them, and
  // letting one through is Chrome's page zoom, which this control cannot see
  // and the browser remembers per site. Ctrl+B stands aside in Monaco (its
  // Ctrl+K Ctrl+B chord), ignores auto-repeat (holding it would flap the
  // pane), and nothing fires mid-composition.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      const what = shortcutFor(e);
      if (!what) return;
      if (blockedRef.current) {
        e.preventDefault();
        return;
      }
      if (what === "toggleLeft" || what === "toggleRight") {
        if (inMonaco(e.target)) return;
        e.preventDefault();
        if (e.repeat) return;
        toggle(what === "toggleLeft" ? "left" : "right");
        return;
      }
      e.preventDefault();
      if (what === "zoomReset") setZoomState(ZOOM_DEFAULT);
      else setZoomState((z) => stepZoom(z, what === "zoomIn" ? 1 : -1));
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [toggle]);

  return { zoom, layout, fitted, available, setZoom, zoomBy, setWidth, resetWidth, open, fold, toggle };
}

/**
 * The draggable edge between a pane and the centre. A 6px hit area laid over
 * the 1px rule (negative margins, so it takes no room), a resize cursor, and
 * the accent line on hover, focus and while dragging. Keyboard: the arrows
 * move the edge (Shift for a bigger step), Home and End go to the ends, and a
 * double-click puts the default width back.
 */
export function Splitter(props: {
  side: Side;
  width: number;
  max: number;
  zoom: number;
  onResize: (width: number) => void;
  onReset: () => void;
}) {
  const start = useRef<{ x: number; w: number; id: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const label = props.side === "left" ? "Resize the sidebar" : "Resize the status pane";

  useEffect(() => {
    document.body.classList.toggle("resizing", dragging);
    return () => document.body.classList.remove("resizing");
  }, [dragging]);

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
      className={"splitter " + props.side + (dragging ? " dragging" : "")}
      data-testid={`split-${props.side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={props.width}
      aria-valuemin={PANE[props.side].min}
      aria-valuemax={props.max}
      tabIndex={0}
      title={`${label} · double-click for the default width`}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        start.current = { x: e.clientX, w: props.width, id: e.pointerId };
        setDragging(true);
      }}
      onPointerMove={(e) => {
        const s = start.current;
        if (!s) return;
        // clientX is in screen pixels; the width is in the zoomed space.
        const dx = toCss(e.clientX - s.x, props.zoom);
        props.onResize(dragWidth(props.side, s.w, dx, props.max));
      }}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={() => {
        start.current = null;
        setDragging(false);
      }}
      onDoubleClick={props.onReset}
      // Space on a focused separator is not push-to-talk (the document-level
      // handler would otherwise hear it), and does nothing here either.
      onKeyUp={(e) => {
        if (e.code === "Space" || e.key === " ") e.stopPropagation();
      }}
      onKeyDown={(e) => {
        if (e.code === "Space" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        const w = keyWidth(props.side, props.width, e.key, e.shiftKey, props.max);
        if (w === null) return;
        e.preventDefault();
        e.stopPropagation();
        props.onResize(w);
      }}
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
          onClick={props.onExpand}
        >
          !
        </button>
      ) : null}
    </div>
  );
}

/** The button inside a pane that folds it. */
export function CollapseButton(props: { side: Side; onCollapse: () => void }) {
  const left = props.side === "left";
  return (
    <button
      type="button"
      className="collapse"
      data-testid={`collapse-${props.side}`}
      title={left ? "Hide the sidebar (Ctrl+B)" : "Hide the status pane (Ctrl+Alt+B)"}
      aria-label={left ? "Hide the sidebar" : "Hide the status pane"}
      aria-expanded={true}
      onClick={props.onCollapse}
    >
      {left ? "«" : "»"}
    </button>
  );
}

/** − 100% + — the percentage resets to 100%. */
export function ZoomControl(props: { zoom: number; onStep: (dir: 1 | -1) => void; onReset: () => void }) {
  return (
    <div className="zoomctl" data-testid="zoom-control" role="group" aria-label="Zoom">
      <button
        type="button"
        data-testid="zoom-out"
        title="Zoom out (Ctrl+-)"
        aria-label="Zoom out"
        disabled={props.zoom <= ZOOM_MIN}
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
        onClick={props.onReset}
      >
        {props.zoom}%
      </button>
      <button
        type="button"
        data-testid="zoom-in"
        title="Zoom in (Ctrl+=)"
        aria-label="Zoom in"
        disabled={props.zoom >= ZOOM_MAX}
        onClick={() => props.onStep(1)}
      >
        +
      </button>
    </div>
  );
}

