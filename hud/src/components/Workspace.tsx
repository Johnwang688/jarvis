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
//
// Several chats (WP-B): any pane may show chat, each its own conversation.
// The selected chat's pane (decisions W-6: the chat pane most recently
// clicked, where ambiguous input goes) carries `data-selected` and, in a
// split, an accent down its header's edge and a mic mark. A view switch goes through the caller (`onView`), which refuses
// to switch a File pane holding an unsaved edit away.

import { useEffect, useRef, type ReactNode } from "react";
import { keyStep } from "../lib/layout";
import {
  PANEL, PANE_MIN_H, PANE_MIN_W, PANE_VIEWS, SHAPES, columnWidths, maxPanelHeight, moveColEdge,
  type PaneNo, type PaneSpec, type View,
} from "../lib/workspace";
import { placeTerminals, type InSpec } from "../lib/terminal";
import type { Project } from "../types";
import { Separator, type LayoutControl } from "./Layout";
import { Panel } from "./Panel";
import { TerminalLinkMenu, TerminalPane, TerminalToasts, terminals, useTerminals } from "./Terminal";

/**
 * The terminals' view of the workspace (WP-D), handed over after each render:
 * which drawn pane shows which terminal, the zoom (the terminal's font
 * follows it), whether the panel is open, and how to put a terminal in a pane
 * or the panel. Whether a card is up (input held) is App's to say, above the
 * workspace's error boundary: a crash unmounts this, and the hold must not
 * freeze with it (review of PR #32).
 */
function useTerminalBridge(v: LayoutControl, place: Map<string, number>, terminalIn?: () => InSpec,
                           openPreview?: (url: string) => boolean) {
  const latest = useRef({ v, terminalIn, openPreview });
  latest.current = { v, terminalIn, openPreview };
  useEffect(() => {
    // A terminal link's "Open in Preview" (WP-E): App's click rule decides the pane.
    terminals.openPreview = (url) => latest.current.openPreview?.(url) ?? false;
    terminals.defaultIn = () => latest.current.terminalIn?.() ?? "home";
    terminals.setPaneTerminal = (pane, id) => latest.current.v.setTerminal(pane, id);
    terminals.forgetTerminal = (id) => latest.current.v.forgetTerminal(id);
    terminals.focusPane = (pane) => {
      latest.current.v.focus(pane);
      requestAnimationFrame(() =>
        document.querySelector<HTMLElement>(`[data-testid="pane-${pane}"]`)?.focus({ preventScroll: true }));
    };
    terminals.showPanel = () => {
      if (!latest.current.v.fit.panel.open) latest.current.v.togglePanel();
    };
    void terminals.refresh();
  }, []);
  const key = [...place].map(([id, n]) => `${id}:${n}`).join(",");
  useEffect(() => terminals.setZoom(v.zoom), [v.zoom]);
  useEffect(() => terminals.setPanelOpen(v.fit.panel.open), [v.fit.panel.open]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => terminals.setPlace(place), [key]);
}

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
  /** The selected chat (lib/chats `selectedChatOf`); null with no chat drawn. */
  selectedPane: PaneNo | null;
  /** Show `view` in `pane` — the strip's buttons and its menu. */
  onView: (pane: PaneNo, view: View) => void;
  /** Where `+` opens a terminal: the focused pane's folder, as an id (lib/terminal.ts, terminalSpecFor). */
  terminalIn?: () => InSpec;
  /** A terminal link's "Open in Preview": load it in a Preview pane (WP-E); false when it did not. */
  openPreview?: (url: string) => boolean;
  /** `+ ▾`'s choices besides Home. */
  projects?: Project[];
}) {
  const v = props.view;
  const { fit, ws } = v;
  const grid = useRef<HTMLDivElement>(null);
  // One terminal is drawn in one place at a time (W-1): the first drawn pane
  // holding it, else the panel.
  const place = placeTerminals(ws.panes, fit.panes);
  useTerminalBridge(v, place, props.terminalIn, props.openPreview);
  const mgr = useTerminals();
  /** A terminal pane's header: the title its program set (text, capped), else its own. */
  const terminalContext = (spec: PaneSpec) => {
    const id = spec.terminalId;
    if (!id) return "terminal";
    return mgr.sessions.get(id)?.title || mgr.row(id)?.title || `terminal ${id}`;
  };
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
          const ctx = at.split ? (spec.view === "terminal" ? terminalContext(spec) : props.context(spec, at)) : "";
          return (
            <section
              key={n}
              className={
                "wpane" + (at.split && at.focused ? " focused" : "")
                + (at.split && spec.view === "chat" && n === props.selectedPane ? " selected" : "")
              }
              data-testid={`pane-${n}`}
              data-view={spec.view}
              data-focused={at.focused ? "true" : "false"}
              data-selected={spec.view === "chat" && n === props.selectedPane ? "true" : undefined}
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
                      onChange={(e) => props.onView(n, e.target.value as View)}
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
                        onClick={() => props.onView(n, view)}
                      >
                        {view}
                      </button>
                    ))
                  )}
                </div>
                {at.split && spec.view === "chat" && n === props.selectedPane ? (
                  <span className="micmark" data-testid={`pane-${n}-mic`} role="img" aria-label="Selected chat"
                        title="The selected chat: what you say, the orb, and files dropped outside a chat go here">
                    <svg width="10" height="13" viewBox="0 0 10 13" aria-hidden="true" focusable="false">
                      <rect x="3" y="0.75" width="4" height="7" rx="2" fill="currentColor" />
                      <path d="M1 6.25a4 4 0 0 0 8 0M5 10.25v2" fill="none" stroke="currentColor" strokeWidth="1.2"
                            strokeLinecap="round" />
                    </svg>
                  </span>
                ) : null}
                {ctx ? <span className="panectx" title={ctx}>{ctx}</span> : null}
                <div className="paneextras">{props.extras(spec, at)}</div>
              </div>
              {spec.view === "terminal" ? (
                <TerminalPane pane={n} terminalId={spec.terminalId} drawn={at.drawn} />
              ) : (
                props.render(spec, at)
              )}
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
      <Panel open={fit.panel.open} height={fit.panel.height} blocked={props.blocked} zoom={v.zoom}
             projects={props.projects ?? []} onHide={v.togglePanel} />
      <TerminalToasts blocked={props.blocked} />
      <TerminalLinkMenu blocked={props.blocked} zoom={v.zoom} />
    </>
  );
}
