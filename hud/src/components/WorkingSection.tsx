// The Inbox section under the owner Inbox row (WS-2): the chat threads the
// owner is working on now, from every project, as a second view of the same
// threads (they stay in their projects). Presentational, except for
// `WorkingBlock`, the one place that subscribes to the working-set store: the
// Sidebar does not, so a refresh redraws this block and never the whole tree.
//
// What it draws, top to bottom (D-5, D-9, D-10):
//   - a header with the fold chevron (the choice lasts, lib/working); with
//     nothing in the set the header itself says so and nothing else is drawn;
//   - a list with a height of its own (it scrolls, and never takes the project
//     tree's room):
//     - the "Needs you" group: every row that has no thread entry to sit on, as
//       a kind word and a title (text, never HTML); a click opens today's
//       Inbox page, where answering happens;
//     - one row per thread, ordered by label then newest first: the same dot
//       as its row in the project tree (the Sidebar's activity store), its
//       title, a hint for inbox rows attached to it, and a chip with its
//       project's name. Its title is a real button (Tab, Enter, Space); a click
//       anywhere on the row opens it like a tree row.

import { memo, useEffect, useRef } from "react";
import type { Project, Thread } from "../types";
import type { ActivityView } from "../lib/activity";
import type { InboxRow } from "../lib/inbox";
import type { PaneNo } from "../lib/workspace";
import { threadLabel } from "../lib/threadTitle";
import { threadTooltip } from "../lib/threadmodel";
import { LABEL_TEXT, attachedHint, kindLabel, orderEntries, rowPending, type WorkingEntry } from "../lib/working";
import { useWorking } from "../state/working";
import { Dot } from "./ActivityDot";
import { HoverTitle } from "./HoverTitle";

export interface WorkingSectionProps {
  open: boolean;
  onToggle: () => void;
  entries: WorkingEntry[];
  needsYou: InboxRow[];
  /** Whether the last read failed (the last list is kept and drawn). */
  stale: boolean;
  threads: Thread[];
  projects: Project[];
  activity: ActivityView;
  selClass: (threadId: string) => string;
  paneOf: (threadId: string) => PaneNo | undefined;
  dragging: string | null;
  onPick: (id: string, beside: boolean) => void;
  onOpenInbox: () => void;
  onMenu: (e: React.MouseEvent, id: string) => void;
  onDragStart: (e: React.DragEvent, id: string) => void;
  onDragEnd: () => void;
}

export const WorkingSection = memo(function WorkingSection(p: WorkingSectionProps) {
  const byId = new Map(p.threads.map((t) => [t.id, t]));
  const projectName = new Map(p.projects.map((pr) => [pr.id, pr.name]));
  // An entry whose chat this window does not hold (archived, or not read yet) has nothing to open.
  const rows = orderEntries(p.entries, p.activity)
    .map((e) => ({ e, t: byId.get(e.thread_id) }))
    .filter((x): x is { e: WorkingEntry; t: Thread } => !!x.t);
  const empty = !rows.length && !p.needsYou.length;
  return (
    <div className="workingsec" data-testid="working-section" data-open={p.open ? "true" : "false"}>
      <div className="workinghead">
        <button
          type="button"
          className="wtoggle"
          data-testid="working-toggle"
          aria-expanded={p.open}
          aria-label={p.open ? "Collapse the Inbox list" : "Expand the Inbox list"}
          title={p.open ? "Collapse" : "Expand"}
          onClick={p.onToggle}
        >
          {p.open ? "▾" : "▸"}
        </button>
        {empty ? (
          <span className="wlabel wnone" data-testid="working-empty">Nothing in progress</span>
        ) : (
          <>
            <span className="wlabel">In progress</span>
            <span className="wcount" data-testid="working-count">{rows.length}</span>
          </>
        )}
        {p.stale ? (
          <span className="wstale" data-testid="working-stale" title="Could not refresh this list">!</span>
        ) : null}
      </div>
      {p.open && !empty ? (
        <div className="wlist" data-testid="working-list">
          {p.needsYou.length ? (
            <div className="wneeds" data-testid="working-needs">
              <div className="whead">Needs you</div>
              {p.needsYou.map((r) => (
                <div
                  key={r.id}
                  className={"tree-row indent-1 wrow wneed" + (rowPending(r) ? " pending" : "")}
                  data-testid="working-need"
                  data-id={r.id}
                  title={r.title}
                  onClick={p.onOpenInbox}
                >
                  <span className="wkind">{kindLabel(r)}</span>
                  <button type="button" className="wopen nm" onClick={(e) => { e.stopPropagation(); p.onOpenInbox(); }}>
                    {r.title}
                  </button>
                </div>
              ))}
            </div>
          ) : null}
          {rows.map(({ e, t }) => {
            const status = p.activity.threads[t.id] || "idle";
            const pid = t.project_id || e.project_id || "";
            const chip = projectName.get(pid) || "";
            const label = threadLabel(t);
            const hint = attachedHint(e.attached);
            return (
              <div
                key={t.id}
                className={
                  "tree-row indent-1 titled wrow" + p.selClass(t.id) + (p.dragging === t.id ? " dragging" : "")
                  + (status === "working" ? " act-working" : "")
                }
                data-testid="working-row"
                data-id={t.id}
                data-label={status}
                data-pane={p.paneOf(t.id)}
                draggable
                onDragStart={(ev) => p.onDragStart(ev, t.id)}
                onDragEnd={p.onDragEnd}
                onContextMenu={(ev) => p.onMenu(ev, t.id)}
                onClick={(ev) => p.onPick(t.id, ev.altKey)}
                title={threadTooltip(t, t.cwd) + (status === "idle" ? "" : ` · ${LABEL_TEXT[status]}`)}
              >
                <span className="tw">
                  <Dot status={status} testid={`working-activity-${t.id}`} />
                </span>
                {/* The row's tab stop: Enter and Space click it, and the click opens the thread. */}
                <button
                  type="button"
                  className="wopen nm ttl-btn"
                  data-testid={`working-open-${t.id}`}
                  aria-label={`${label.text}${status === "idle" ? "" : `, ${LABEL_TEXT[status]}`}${chip ? `, ${chip}` : ""}`}
                >
                  <HoverTitle text={label.text} untitled={label.untitled} testid={`working-name-${t.id}`}
                    onDoubleClick={() => {}} />
                </button>
                {hint ? (
                  <button
                    type="button"
                    className={"whint" + (e.attached.some(rowPending) ? " pending" : "")}
                    data-testid={`working-hint-${t.id}`}
                    title="Open the Inbox page"
                    onClick={(ev) => { ev.stopPropagation(); p.onOpenInbox(); }}
                  >
                    {hint}
                  </button>
                ) : null}
                {chip ? (
                  <span className="wchip" data-testid={`working-chip-${t.id}`} title={`Project: ${chip}`}>{chip}</span>
                ) : null}
                <button
                  type="button"
                  className="menu"
                  data-testid={`working-menu-${t.id}`}
                  title="Done, move to…"
                  aria-label={`${label.text}: menu`}
                  aria-haspopup="menu"
                  onClick={(ev) => p.onMenu(ev, t.id)}
                >
                  ⋯
                </button>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
});

/** The store's one subscriber: the section, and nothing else, redraws on a refresh. Nothing is
 * drawn for an older daemon, or until the first read has landed. */
export const WorkingBlock = memo(function WorkingBlock(p: Omit<WorkingSectionProps, "entries" | "needsYou" | "stale">) {
  const work = useWorking();
  if (work.status === "unavailable" || work.status === "loading") return null;
  return <WorkingSection {...p} entries={work.entries} needsYou={work.needsYou} stale={work.status === "error"} />;
});

/** Done in a thread's menu: enabled only while the daemon's last read says it may be (a hint:
 * Done checks again), with the reason a screen reader hears too. Subscribes while the menu is open. */
export function WorkingDoneItem(p: { threadId: string; onDone: (id: string) => void }) {
  const work = useWorking();
  const entry = work.entries.find((e) => e.thread_id === p.threadId);
  const ref = useRef<HTMLButtonElement | null>(null);
  const disabled = !!entry?.why_not;
  // A read can disable the very item the keyboard is on (Done stops being allowed): focus then
  // goes to the next enabled item of the menu, or the menu itself, never to the page behind it.
  useEffect(() => {
    if (!disabled) return;
    const btn = ref.current;
    const a = document.activeElement;
    if (!btn || !(a === btn || a === document.body || !a)) return;
    const menu = btn.closest<HTMLElement>('[role="menu"]');
    if (!menu) return;
    const items = Array.from(menu.querySelectorAll<HTMLButtonElement>("button.mrow"));
    const after = items.slice(items.indexOf(btn) + 1).find((b) => !b.disabled);
    (after || items.find((b) => !b.disabled) || menu).focus();
  }, [disabled]);
  if (!entry) return null;
  return (
    <>
      <button
        type="button"
        className="mrow"
        role="menuitem"
        ref={ref}
        style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                 background: "none", textTransform: "none", letterSpacing: 0 }}
        data-testid="tmenu-done"
        disabled={!!entry.why_not}
        aria-describedby={entry.why_not ? "tmenu-done-why" : undefined}
        title="Take it out of the Inbox; it stays in its project"
        onClick={() => p.onDone(p.threadId)}
      >
        Done
      </button>
      {entry.why_not ? <div className="mwhy" id="tmenu-done-why" data-testid="tmenu-done-why">{entry.why_not}</div> : null}
      <div className="msep" />
    </>
  );
}
