// The centre's panes (plan §2.2): the drawn preset as a CSS grid, a header per
// pane (its view strip, its context in a split, and whatever the caller puts
// on the right — the profile select, a pinned project), the edges between
// panes, and the bottom panel with its own edge. lib/workspace.ts decides
// every number; App decides what each view shows.
//
// A pane is mounted the first time a preset draws it and is then **hidden,
// never unmounted**, as the side panes are: an unsaved edit or a loaded
// preview in pane 3 survives switching to one pane and back.
//
// In the single layout — the default — pane 1 is the only pane mounted and
// its header is the old tab row exactly: the five view buttons and, on the
// right, the profile select. The split-only chrome (the focus line, the
// context, the compact view menu) is drawn only when there is more than one
// pane, so the default window renders as it did.

import { useRef, type ReactNode } from "react";
import { keyStep } from "../lib/layout";
import {
  PANEL, PANE_MIN_H, PANE_MIN_W, PANE_VIEWS, SHAPES, columnWidths, maxPanelHeight, moveColEdge,
  type PaneNo, type PaneSpec, type View,
} from "../lib/workspace";
import { Separator, type LayoutControl } from "./Layout";
import { Panel } from "./Panel";

/** Under this, a split pane's File and Diff views hide their tree behind a toggle. */
export const NARROW_W = 560;
/** Under this, a split pane's view strip becomes a `view ▾` menu. */
export const COMPACT_W = 520;

export interface PaneInfo {
  pane: PaneNo;
  /** Drawn by this render (a mounted pane may be hidden). */
  drawn: boolean;
  /** More than one pane is drawn. */
  split: boolean;
  /** The pane's width in the HUD's own pixels (NaN with no window to fit). */
  width: number;
  /** A split pane too narrow for a tree beside its editor. */
  narrow: boolean;
  /** A split pane too narrow for the five view words. */
  compact: boolean;
  focused: boolean;
}

/** Which column a slot sits in, read off the shape's grid areas. */
function slotColumn(areas: readonly string[], slot: number): number {
  for (const row of areas) {
    const i = row.split(" ").indexOf(`s${slot}`);
    if (i >= 0) return i;
  }
  return 0;
}

export function Workspace(props: {
  view: LayoutControl;
  blocked: boolean;
  /** The view's body, under the pane's header. */
  render: (spec: PaneSpec, info: PaneInfo) => ReactNode;
  /** The right of the pane's header. */
  extras: (spec: PaneSpec, info: PaneInfo) => ReactNode;
  /** The pane's context in a split: the thread title, the file path, the URL. */
  context: (spec: PaneSpec, info: PaneInfo) => string;
}) {
  const v = props.view;
  const { fit, ws } = v;
  const grid = useRef<HTMLDivElement>(null);
  // Every pane any render has drawn stays mounted (hidden when not drawn).
  const ever = useRef<Set<PaneNo>>(new Set<PaneNo>([1]));
  for (const n of fit.panes) ever.current.add(n);

  const shape = SHAPES[fit.drawn];
  const split = fit.panes.length > 1;
  const fracs = columnWidths(fit.cols, 1);
  const colPx = columnWidths(fit.cols, fit.width);
  const areas = shape.areas
    .map((row) => `"${row.replace(/s(\d)/g, (_, i) => `p${fit.panes[Number(i) - 1]}`)}"`)
    .join(" ");
  const gridStyle: React.CSSProperties = {
    gridTemplateColumns: fracs.map((f) => `minmax(0, ${f}fr)`).join(" "),
    gridTemplateRows: shape.rows === 2 ? `minmax(0, ${fit.row}fr) minmax(0, ${1 - fit.row}fr)` : "minmax(0, 1fr)",
    gridTemplateAreas: areas,
  };
  // The room a drag measures against: what is on screen, else what was fitted.
  const roomW = () => grid.current?.offsetWidth || fit.width;
  const roomH = () => grid.current?.offsetHeight || fit.height;

  const info = (n: PaneNo): PaneInfo => {
    const slot = fit.panes.indexOf(n);
    const drawn = slot >= 0;
    const width = drawn ? (split ? colPx[slotColumn(shape.areas, slot + 1)] : fit.width) : NaN;
    return {
      pane: n, drawn, split, width,
      narrow: split && width < NARROW_W,
      compact: split && width < COMPACT_W,
      focused: ws.focused === n,
    };
  };

  const panelMax = maxPanelHeight(fit, v.availableH);

  return (
    <>
      <div
        ref={grid}
        id="workspace"
        data-testid="workspace"
        data-preset={ws.preset}
        data-drawn-preset={fit.drawn}
        className={split ? "split" : undefined}
        style={gridStyle}
      >
        {([1, 2, 3, 4] as PaneNo[]).filter((n) => ever.current.has(n)).map((n) => {
          const spec = ws.panes[n - 1];
          const at = info(n);
          const ctx = at.split ? props.context(spec, at) : "";
          return (
            <section
              key={n}
              className={"wpane" + (at.split && at.focused ? " focused" : "")}
              data-testid={`pane-${n}`}
              data-view={spec.view}
              data-focused={at.focused ? "true" : "false"}
              data-voice={spec.view === "chat" ? "true" : undefined}
              aria-label={`Pane ${n}: ${spec.view}`}
              tabIndex={-1}
              style={at.drawn ? { gridArea: `p${n}` } : { display: "none" }}
              onPointerDownCapture={() => v.focus(n)}
              onFocusCapture={() => v.focus(n)}
            >
              <div className="tabs" id={n === 1 ? "tabs" : undefined}>
                <div className="viewstrip" data-testid={`pane-${n}-view`}>
                  {at.compact ? (
                    <select
                      data-testid={`pane-${n}-view-select`}
                      aria-label={`What pane ${n} shows`}
                      value={spec.view}
                      onChange={(e) => v.setView(n, e.target.value as View)}
                      onKeyDown={(e) => e.stopPropagation()}
                      onKeyUp={(e) => e.stopPropagation()}
                    >
                      {PANE_VIEWS.map((view) => (
                        <option key={view} value={view}>{view}</option>
                      ))}
                    </select>
                  ) : (
                    PANE_VIEWS.map((view) => (
                      <button
                        type="button"
                        key={view}
                        data-testid={`tab-${view}`}
                        className={spec.view === view ? "on" : ""}
                        onClick={() => v.setView(n, view)}
                      >
                        {view}
                      </button>
                    ))
                  )}
                </div>
                {ctx ? <span className="panectx" title={ctx}>{ctx}</span> : null}
                <div className="paneextras">{props.extras(spec, at)}</div>
              </div>
              {props.render(spec, at)}
            </section>
          );
        })}

        {fit.cols.map((c, i) => {
          const w = Number.isFinite(fit.width) ? fit.width : 0;
          const lo = (i === 0 ? 0 : fit.cols[i - 1] * w) + PANE_MIN_W;
          const hi = (i === fit.cols.length - 1 ? w : fit.cols[i + 1] * w) - PANE_MIN_W;
          return (
            <Separator
              key={`col-${i}`}
              testid={`split-col-${i + 1}`}
              className="wsplit col"
              orientation="vertical"
              label="Resize the panes"
              title="Resize the panes · double-click to make them equal"
              value={c * w}
              min={lo}
              max={Math.max(lo, hi)}
              zoom={v.zoom}
              blocked={props.blocked}
              style={{ left: `calc(${c * 100}% - 3px)` }}
              onDrag={(start, delta) => {
                const room = roomW();
                v.setSplit(fit.drawn, { cols: moveColEdge(fit.cols, i, (start + delta) / room, room) });
              }}
              onKey={(key, shift) => {
                const room = roomW();
                const min = (i === 0 ? 0 : fit.cols[i - 1] * room) + PANE_MIN_W;
                const max = (i === fit.cols.length - 1 ? room : fit.cols[i + 1] * room) - PANE_MIN_W;
                const next = keyStep("x", c * room, key, shift, min, Math.max(min, max));
                if (next === null) return false;
                v.setSplit(fit.drawn, { cols: moveColEdge(fit.cols, i, next / room, room) });
                return true;
              }}
              onReset={() => v.equalSplit(fit.drawn)}
            />
          );
        })}
        {shape.rows === 2 ? (
          <Separator
            testid="split-row"
            className="wsplit row"
            orientation="horizontal"
            label="Resize the panes"
            title="Resize the panes · double-click to make them equal"
            value={fit.row * (Number.isFinite(fit.height) ? fit.height : 0)}
            min={PANE_MIN_H}
            max={Math.max(PANE_MIN_H, (Number.isFinite(fit.height) ? fit.height : 0) - PANE_MIN_H)}
            zoom={v.zoom}
            blocked={props.blocked}
            style={{
              left: fit.drawn === "main2" ? `${fit.cols[0] * 100}%` : 0,
              right: 0,
              top: `calc(${fit.row * 100}% - 3px)`,
            }}
            onDrag={(start, delta) => {
              const room = roomH();
              v.setSplit(fit.drawn, { row: Math.min(1 - PANE_MIN_H / room, Math.max(PANE_MIN_H / room, (start + delta) / room)) });
            }}
            onKey={(key, shift) => {
              const room = roomH();
              const next = keyStep("y", fit.row * room, key, shift, PANE_MIN_H, Math.max(PANE_MIN_H, room - PANE_MIN_H));
              if (next === null) return false;
              v.setSplit(fit.drawn, { row: next / room });
              return true;
            }}
            onReset={() => v.equalSplit(fit.drawn)}
          />
        ) : null}
      </div>

      {fit.panel.open ? (
        <Separator
          testid="panel-split"
          className="hsplit"
          orientation="horizontal"
          label="Resize the panel"
          title="Resize the panel · double-click for the default height"
          value={fit.panel.height}
          min={PANEL.min}
          max={panelMax}
          zoom={v.zoom}
          blocked={props.blocked}
          // The edge is the panel's top: dragging it up makes the panel taller.
          onDrag={(start, delta) => v.setPanelHeight(Math.min(panelMax, Math.max(PANEL.min, Math.round(start - delta))))}
          onKey={(key, shift) => {
            const next = keyStep("y", fit.panel.height, key, shift, PANEL.min, panelMax, -1);
            if (next === null) return false;
            v.setPanelHeight(next);
            return true;
          }}
          onReset={v.resetPanelHeight}
        />
      ) : null}
      <Panel open={fit.panel.open} height={fit.panel.height} blocked={props.blocked} onHide={v.togglePanel} />
    </>
  );
}
