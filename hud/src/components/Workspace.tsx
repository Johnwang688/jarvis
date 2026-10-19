// The centre's panes (plan §2.2; 2026-10-19-hud-free-split-plan.md §10): the
// drawn tree, each pane placed absolutely from its own box, a header per pane
// (its view strip, its context in a split, and whatever the caller puts on
// the right — the profile select, a pinned project), the edges between panes,
// and the bottom panel with its own edge. lib/workspace.ts decides every
// number (`fitWorkspace`: `fit.boxes`, `fit.edges`); App decides what each
// view shows.
//
// **Sections never move in the DOM** (FS3, plan P-4 and C5). Every section
// is a direct child of #workspace, one flat list keyed `${id}.${incarnation}`
// in id order (PANE_NOS), never slot order, and placed from its own fractions
// only. A split, a close, ⊞, a drag or a resize changes only a section's
// `style`: React never moves or re-parents one, because moving an iframe's
// ancestor reloads it (a Browser page, its focus guard and its state), and a
// shared grid would move unrelated panes by sub-pixels.
//
// A pane is mounted the first time it is drawn and is then **hidden, never
// unmounted**, as the side panes are: an unsaved edit or a loaded preview in
// pane 3 survives switching to one pane and back. A pane is known by its id
// (`pane-<id>`, its React key) and carries its slot, the number the owner
// reads, as `data-slot`.
//
// In the single layout — the default — one pane is mounted and its header is
// the old tab row: the view buttons, the split button (WP-G2: that is how one
// pane becomes two) and, on the right, the profile select. The split-only
// chrome (the focus line, the context, the compact view menu, the ×) is drawn
// only when there is more than one pane.
//
// Split and × (WP-G2): the split button adds a copy of the pane after it (App
// decides what the copy shows and whether one fits); × closes exactly that
// pane, asking first in an inline strip when App says it holds something that
// cannot wait (an unsaved edit, a compose row's unsent words). A × clicked
// within CLOSE_GUARD_MS of a close, or as a double-click's second click,
// closes nothing. A pane neither drawn nor hidden by ⊞ is unmounted.
//
// Several chats (WP-B): any pane may show chat, each its own conversation.
// The selected chat's pane (decisions W-6: the chat pane most recently
// clicked, where ambiguous input goes) carries `data-selected` and, in a
// split, an accent down its header's edge and a mic mark. A view switch goes through the caller (`onView`), which refuses
// to switch a File pane holding an unsaved edit away.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { keyStep } from "../lib/layout";
import {
  PANEL, PANE_NOS, PANE_VIEWS, VIEW_LABEL, inWorkspace, maxPanelHeight, presetOfWs, slotOf,
  type PaneNo, type PaneSpec, type View,
} from "../lib/workspace";
import type { Rect } from "../lib/splittree";
import { placeTerminals, type InSpec } from "../lib/terminal";
import type { Project } from "../types";
import { AreaIcon, Separator, type LayoutControl } from "./Layout";
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
                           openBrowser?: (url: string) => boolean, slots?: Map<number, number>) {
  const latest = useRef({ v, terminalIn, openBrowser });
  latest.current = { v, terminalIn, openBrowser };
  useEffect(() => {
    // A terminal link's "Open in Browser" (WP-E): App's click rule decides the pane.
    terminals.openBrowser = (url) => latest.current.openBrowser?.(url) ?? false;
    terminals.defaultIn = () => latest.current.terminalIn?.() ?? "home";
    terminals.setPaneTerminal = (pane, id) => latest.current.v.setTerminal(pane, id);
    terminals.forgetTerminal = (id) => latest.current.v.forgetTerminal(id);
    terminals.renameTerminal = (from, to) => latest.current.v.renameTerminal(from, to);
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
  // "in pane N" names the slot, never the id (plan G-1).
  const slotKey = [...(slots ?? [])].map(([n, s]) => `${n}@${s}`).join(",");
  useEffect(() => terminals.setZoom(v.zoom), [v.zoom]);
  useEffect(() => terminals.setPanelOpen(v.fit.panel.open), [v.fit.panel.open]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => terminals.setPlace(place), [key]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => terminals.setSlots(slots ?? new Map()), [slotKey]);
}

/** A click on × this soon after a close closes nothing: the second click of a double-click lands on whatever × moved under the pointer. */
export const CLOSE_GUARD_MS = 400;

/** Under this, a split pane's File and Diff views hide their tree behind a toggle. */
export const NARROW_W = 560;
/** Under this, a split pane's view strip becomes a `view ▾` menu. */
export const COMPACT_W = 520;

export interface PaneInfo {
  pane: PaneNo;
  /** Where the owner sees it: 1-based, reading order (lib/workspace `slotOf`). */
  slot: number;
  /** Drawn by this render (a mounted pane may be hidden). */
  drawn: boolean;
  /** More than one pane is drawn. */
  split: boolean;
  /** The pane's width and height in the HUD's own pixels (NaN with no window to fit). */
  width: number;
  height: number;
  /** A split pane too narrow for a tree beside its editor. */
  narrow: boolean;
  /** A split pane too narrow for the five view words. */
  compact: boolean;
  focused: boolean;
}

/**
 * A drawn section's place, from its own box only (C5). The 1px rule between
 * panes is shared out as the CSS grid's 1px gap was before FS3: every
 * fraction is of the area plus one gap (`100% + 1px`) and each pane gives 1px
 * back, so equal panes are equal to the sub-pixel, exactly where the grid drew
 * them, and a pane's place still reads nothing but its own fractions.
 */
const unit = (f: number) => `${f} * (100% + 1px)`;
function boxStyle(r: Rect): React.CSSProperties {
  return {
    position: "absolute",
    left: `calc(${unit(r.left)})`,
    top: `calc(${unit(r.top)})`,
    width: `calc(${unit(r.width)} - 1px)`,
    height: `calc(${unit(r.height)} - 1px)`,
  };
}

export function Workspace(props: {
  view: LayoutControl;
  covered?: boolean;
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
  /**
   * Whether the strip offers the VEX view in this pane (VX-2): only when the
   * pane's project is a VEX project (lib/vex.ts `offersVex`). A pane already
   * showing it keeps the button, so the owner can see where they are.
   */
  offersVex?: (spec: PaneSpec, info: PaneInfo) => boolean;
  /** Where `+` opens a terminal: the focused pane's folder, as an id (lib/terminal.ts, terminalSpecFor). */
  terminalIn?: () => InSpec;
  /** A terminal link's "Open in Browser": load it in a Browser pane (WP-E); false when it did not. */
  openBrowser?: (url: string) => boolean;
  /** `+ ▾`'s choices besides Home. */
  projects?: Project[];
  /**
   * The split button (WP-G2): whether pane `pane` can be split now, and its
   * title — what it does, or why not (lib/workspace `splitTitle`). Without
   * it there is no button.
   */
  splitFor?: (pane: PaneNo) => { ok: boolean; title: string };
  onSplit?: (pane: PaneNo) => void;
  /** × first asks this (an unsaved edit, unsent words): the question, or null to close at once. */
  closeQuestion?: (pane: PaneNo) => string | null;
  /**
   * For a question about unsaved edits (ED-2): saving them, true once all are
   * saved — the question then offers Save / Don't save / Cancel, as closing
   * the last tab on one does. Null for a question with nothing to save.
   */
  closeSave?: (pane: PaneNo) => (() => Promise<boolean>) | null;
  onClose?: (pane: PaneNo) => void;
}) {
  const v = props.view;
  const { fit, ws } = v;
  // One terminal is drawn in one place at a time (W-1): the first drawn pane
  // holding it, else the panel.
  const place = placeTerminals(ws.panes, fit.panes);
  const slots = new Map<number, number>(PANE_NOS.map((n) => [n, slotOf(ws, n)]));
  useTerminalBridge(v, place, props.terminalIn, props.openBrowser, slots);
  const mgr = useTerminals();
  /** A terminal pane's header: the title its program set (text, capped), else its own. */
  const terminalContext = (spec: PaneSpec) => {
    const id = spec.terminalId;
    if (!id) return "terminal";
    return mgr.sessions.get(id)?.title || mgr.row(id)?.title || `terminal ${id}`;
  };
  // Every pane any render has drawn stays mounted (hidden when not drawn),
  // until it is closed: a pane neither drawn nor hidden by ⊞ is unmounted, and
  // a split that reuses its id later mounts it fresh (its incarnation).
  const ever = useRef<Set<PaneNo>>(new Set<PaneNo>());
  for (const n of fit.panes) ever.current.add(n);
  for (const n of Array.from(ever.current)) {
    if (!inWorkspace(ws, n) && !fit.panes.includes(n)) ever.current.delete(n);
  }
  // × that asks first (an unsaved edit, unsent words): which pane, and what it asks.
  const [asking, setAsking] = useState<{ pane: PaneNo; question: string; save: (() => Promise<boolean>) | null } | null>(null);
  // The last close: a click on × this soon after one (a double-click, the
  // second click landing on the × that moved under the pointer) closes nothing.
  const lastClose = useRef(0);
  const closeNow = (n: PaneNo) => {
    lastClose.current = Date.now();
    setAsking(null);
    props.onClose?.(n);
  };
  const onCloseClick = (n: PaneNo, e: React.MouseEvent) => {
    if (props.blocked || e.detail > 1 || Date.now() - lastClose.current < CLOSE_GUARD_MS) return;
    const question = props.closeQuestion?.(n) ?? null;
    if (question) {
      setAsking({ pane: n, question, save: props.closeSave?.(n) ?? null });
      return;
    }
    closeNow(n);
  };
  // The question takes focus on Keep: Enter there never discards anything.
  useEffect(() => {
    if (!asking) return;
    document.querySelector<HTMLElement>(`[data-testid="pane-${asking.pane}-close-keep"]`)?.focus({ preventScroll: true });
  }, [asking]);
  // A question about what the pane no longer holds is not left standing
  // (review of PR #47): the pane switched to a File with an edit, the words
  // were sent — "Discard and close" must never answer a question nobody asked.
  const question = asking ? props.closeQuestion?.(asking.pane) ?? null : null;
  useEffect(() => {
    if (asking && question !== asking.question) setAsking(null);
  }, [asking, question]);
  /** Split and × are buttons: Space and Enter press them and go no further; a held Enter presses once. */
  const buttonKeys = {
    onKeyDown: (e: React.KeyboardEvent) => {
      e.stopPropagation();
      if (e.repeat) e.preventDefault();
    },
    onKeyUp: (e: React.KeyboardEvent) => e.stopPropagation(),
  };

  const split = fit.panes.length > 1;

  const info = (n: PaneNo): PaneInfo => {
    const box = fit.boxes[n];
    const drawn = !!box;
    const width = box ? box.px.width : NaN;
    return {
      pane: n, slot: slotOf(ws, n), drawn: drawn && !props.covered, split, width, height: box ? box.px.height : NaN,
      narrow: split && width < NARROW_W,
      compact: split && width < COMPACT_W,
      focused: ws.focused === n,
    };
  };

  const panelMax = maxPanelHeight(fit, v.availableH);

  return (
    <>
      <div
        id="workspace"
        data-testid="workspace"
        data-preset={presetOfWs(ws) ?? "free"}
        data-drawn-preset={fit.drawnPreset ?? "free"}
        className={split ? "split" : undefined}
      >
        {PANE_NOS.filter((n) => ever.current.has(n)).map((n) => {
          const spec = ws.panes[n - 1];
          const at = info(n);
          const ctx = at.split ? (spec.view === "terminal" ? terminalContext(spec) : props.context(spec, at)) : "";
          // The sub-agent view is never offered: only a pane showing one keeps its button (SV-3).
          const views = PANE_VIEWS.filter((view) => (view === "subagent" ? spec.view === "subagent"
            : view !== "vex" || spec.view === "vex" || (at.drawn && !!props.offersVex?.(spec, at))));
          return (
            <section
              // Keyed by id and incarnation: a pane keeps its id for life, and
              // one closed and reused later (WP-G2) mounts fresh.
              key={`${n}.${v.incarnation[n] ?? 0}`}
              className={
                "wpane" + (at.split && at.focused ? " focused" : "")
                + (at.split && spec.view === "chat" && n === props.selectedPane ? " selected" : "")
              }
              data-testid={`pane-${n}`}
              data-slot={at.slot}
              data-view={spec.view}
              data-focused={at.focused ? "true" : "false"}
              data-selected={spec.view === "chat" && n === props.selectedPane ? "true" : undefined}
              aria-label={`Pane ${at.slot}: ${VIEW_LABEL[spec.view]}`}
              tabIndex={-1}
              style={fit.boxes[n] && !props.covered ? boxStyle(fit.boxes[n]!.rect) : { display: "none" }}
              onPointerDownCapture={() => v.focus(n)}
              onFocusCapture={() => v.focus(n)}
            >
              <div className="tabs" id={at.drawn && at.slot === 1 ? "tabs" : undefined}>
                <div className="viewstrip" data-testid={`pane-${n}-view`}>
                  {at.compact ? (
                    <select
                      data-testid={`pane-${n}-view-select`}
                      aria-label={`What pane ${at.slot} shows`}
                      value={spec.view}
                      onChange={(e) => props.onView(n, e.target.value as View)}
                      onKeyDown={(e) => e.stopPropagation()}
                      onKeyUp={(e) => e.stopPropagation()}
                    >
                      {views.map((view) => (
                        <option key={view} value={view}>{VIEW_LABEL[view]}</option>
                      ))}
                    </select>
                  ) : (
                    views.map((view) => (
                      <button
                        type="button"
                        key={view}
                        data-testid={`tab-${view}`}
                        className={spec.view === view ? "on" : ""}
                        onClick={() => props.onView(n, view)}
                      >
                        {VIEW_LABEL[view]}
                      </button>
                    ))
                  )}
                </div>
                {props.splitFor ? (() => {
                  const sp = props.splitFor(n);
                  return (
                    <button
                      type="button"
                      className="tb-icon panesplit"
                      data-testid={`pane-${n}-split`}
                      aria-label="Split pane"
                      title={sp.title}
                      disabled={props.blocked || !sp.ok}
                      onClick={() => {
                        if (!props.blocked && sp.ok) props.onSplit?.(n);
                      }}
                      {...buttonKeys}
                    >
                      <AreaIcon area="split" open={false} />
                    </button>
                  );
                })() : null}
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
                {split && at.drawn && props.onClose ? (
                  <button
                    type="button"
                    className="tb-icon paneclose"
                    data-testid={`pane-${n}-close`}
                    aria-label={`Close pane ${at.slot}`}
                    title={spec.view === "chat"
                      ? "Close this pane. The thread keeps running and stays in the sidebar"
                      : "Close this pane"}
                    disabled={props.blocked}
                    onClick={(e) => onCloseClick(n, e)}
                    {...buttonKeys}
                  >
                    ×
                  </button>
                ) : null}
              </div>
              {asking && asking.pane === n ? (
                <div className="closeask" data-testid={`pane-${n}-close-ask`} role="group" aria-label="Close this pane?">
                  <span>{asking.question}</span>
                  {asking.save ? (
                    <button type="button" data-testid={`pane-${n}-close-save`} disabled={props.blocked}
                            onClick={() => {
                              const save = asking.save;
                              if (props.blocked || !save) return;
                              // Closed only once every edit is on disk; a
                              // conflict stays in the pane, which stays open.
                              void save().then((ok) => {
                                if (ok) closeNow(n);
                                else setAsking(null);
                              });
                            }}
                            {...buttonKeys}>
                      Save
                    </button>
                  ) : null}
                  <button type="button" data-testid={`pane-${n}-close-discard`} disabled={props.blocked}
                          onClick={() => {
                            // Asked again at the click: only the question shown is answered.
                            if (props.blocked || (props.closeQuestion?.(n) ?? null) !== asking.question) return;
                            closeNow(n);
                          }}
                          {...buttonKeys}>
                    {asking.save ? "Don't save" : "Discard and close"}
                  </button>
                  {/* Not disabled under a card (the veil makes it inert): a disabled button drops
                      focus to the page before the card can record it, so focus could not come back. */}
                  <button type="button" data-testid={`pane-${n}-close-keep`}
                          onClick={() => setAsking(null)} {...buttonKeys}>
                    {asking.save ? "Cancel" : "Keep"}
                  </button>
                </div>
              ) : null}
              {spec.view === "terminal" ? (
                <TerminalPane pane={n} terminalId={spec.terminalId} drawn={at.drawn} />
              ) : (
                props.render(spec, at)
              )}
            </section>
          );
        })}

        {/* One separator per boundary between a node's children (§9), named by the stored tree; linked when collinear (P-5). */}
        {fit.edges.map((edge) => {
          const x = edge.axis === "x";
          const style: React.CSSProperties = x
            // Centred on the 1px rule (`boxStyle`'s gap), spanning its node.
            ? { left: `calc(${unit(edge.at.left)} - 4px)`, top: `calc(${unit(edge.at.top)})`, bottom: "auto",
                height: `calc(${unit(edge.at.height)} - 1px)` }
            : { top: `calc(${unit(edge.at.top)} - 4px)`, left: `calc(${unit(edge.at.left)})`,
                width: `calc(${unit(edge.at.width)} - 1px)` };
          return (
            <Separator
              key={edge.testid}
              testid={edge.testid}
              linked={edge.members.length > 1 ? edge.members.length : undefined}
              className={x ? "wsplit col" : "wsplit row"}
              orientation={x ? "vertical" : "horizontal"}
              label="Resize the panes"
              title="Resize the panes · double-click to make them equal"
              value={Number.isFinite(edge.value) ? edge.value : 0}
              min={edge.min}
              max={edge.max}
              zoom={v.zoom}
              blocked={props.blocked}
              style={style}
              onDrag={(start, delta) => v.moveEdge(edge, start + delta)}
              onKey={(key, shift) => {
                const next = keyStep(x ? "x" : "y", edge.value, key, shift, edge.min, edge.max);
                if (next === null) return false;
                v.moveEdge(edge, next);
                return true;
              }}
              onReset={() => v.equalize(edge.members.map((m) => m.path))}
            />
          );
        })}
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
      <Panel covered={props.covered} open={fit.panel.open} height={fit.panel.height} blocked={props.blocked} zoom={v.zoom}
             projects={props.projects ?? []} onHide={v.togglePanel} />
      <TerminalToasts blocked={props.blocked} />
      <TerminalLinkMenu blocked={props.blocked} zoom={v.zoom} />
    </>
  );
}
