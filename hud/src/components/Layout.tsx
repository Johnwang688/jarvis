// The window's geometry, applied: the zoom, the two pane widths, folding a
// pane to a rail, and the separators that resize them (lib/layout.ts decides
// every number; this file only draws and listens).
//
// A folded pane is **hidden, never unmounted**: the sidebar's expanded
// projects, a half-typed steer in the task pane and an inline rename all live
// in component state, and folding a pane must not be how the owner loses them.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DEFAULT_LAYOUT, PANE, ZOOM_DEFAULT, ZOOM_MAX, ZOOM_MIN, dragWidth, fitPanes, isTypingTarget, isZoom, keyWidth,
  loadLayout, loadZoom, saveLayout, saveZoom, shortcutFor, stepZoom, toCss,
  type PaneLayout, type Side,
} from "../lib/layout";

export interface LayoutControl {
  zoom: number;
  layout: PaneLayout;
  /** The widths actually drawn, fitted to the window. */
  drawn: { left: number; right: number };
  available: number;
  setZoom: (level: number) => void;
  zoomBy: (dir: 1 | -1) => void;
  setWidth: (side: Side, width: number) => void;
  resetWidth: (side: Side) => void;
  setCollapsed: (side: Side, collapsed: boolean) => void;
  toggle: (side: Side) => void;
}

/** The zoom and pane state, persisted, with the keyboard shortcuts bound. */
export function useLayout(): LayoutControl {
  // Applied in the initializer too, so a window reopened at 140% does not
  // draw its first frame at 100% and then jump.
  const [zoom, setZoomState] = useState(() => {
    const z = loadZoom();
    document.documentElement.style.setProperty("--ui-zoom", String(z / 100));
    return z;
  });
  const [layout, setLayout] = useState<PaneLayout>(() => loadLayout());
  const [viewport, setViewport] = useState(() => window.innerWidth);

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
  const drawn = fitPanes(layout, available);

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
  const setCollapsed = useCallback(
    (side: Side, collapsed: boolean) =>
      setLayout((l) => ({ ...l, [side === "left" ? "leftCollapsed" : "rightCollapsed"]: collapsed })),
    [],
  );
  const toggle = useCallback(
    (side: Side) =>
      setLayout((l) =>
        side === "left" ? { ...l, leftCollapsed: !l.leftCollapsed } : { ...l, rightCollapsed: !l.rightCollapsed },
      ),
    [],
  );

  // Capture phase on the window: the input bar stops its keys from bubbling
  // (so Space typed there is not push-to-talk), and Ctrl+B should still fold
  // a pane from the box the owner's cursor usually sits in. The zoom keys
  // stand aside while the owner is typing — the browser keeps its own there —
  // and everywhere else they are the HUD's, not the browser's page zoom.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const what = shortcutFor(e);
      if (!what) return;
      if (what === "toggleLeft" || what === "toggleRight") {
        e.preventDefault();
        toggle(what === "toggleLeft" ? "left" : "right");
        return;
      }
      if (isTypingTarget(e.target)) return;
      e.preventDefault();
      if (what === "zoomReset") setZoomState(ZOOM_DEFAULT);
      else setZoomState((z) => stepZoom(z, what === "zoomIn" ? 1 : -1));
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [toggle]);

  return { zoom, layout, drawn, available, setZoom, zoomBy, setWidth, resetWidth, setCollapsed, toggle };
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
      onKeyDown={(e) => {
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
}) {
  const left = props.side === "left";
  return (
    <div className={"rail " + props.side} id={`rail-${props.side}`} data-testid={`rail-${props.side}`}>
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

