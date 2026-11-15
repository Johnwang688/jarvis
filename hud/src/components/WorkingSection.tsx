// The Inbox section under the owner Inbox row (WS-2): the chat threads the
// owner is working on now, from every project, as a second view of the same
// threads (they stay in their projects). Presentational, except for
// `WorkingBlock`, the one place that subscribes to the working-set store: the
// Sidebar does not, so a refresh redraws this block and never the whole tree.
//
// What it draws, top to bottom (D-5, D-9, D-10, and WS-3):
//   - the owner Inbox row, the list's header (`InboxHeadRow`): the rainbow row keeps its look and its
//     count; a click folds and unfolds the list below it (the choice lasts, lib/working), and carries
//     `aria-expanded`/`aria-controls`. Where the daemon has no list (an older one, or before the first
//     read) it is a plain button that opens the Needs you page, as before;
//   - a "Needs you" line, always, whenever the list is unfolded, even at 0: it carries the waiting
//     count (the row's own) and opens the Needs you page;
//   - a list with a height of its own (it scrolls, and never takes the project tree's room):
//     - the Needs-you rows that have no thread entry to sit on, as a kind word and a title (text, never
//       HTML); a click opens the Needs you page, where answering happens. The group folds on its own
//       chevron, beside the Needs you line (the choice lasts). Each row has a ✓ (on hover or focus) that
//       marks it done, the inbox's dismissal: only for what may be hidden (ranks 5 to 8), else it says
//       why. A row already hidden is not drawn here (the Needs you page's Show hidden still lists it);
//     - one row per thread, ordered by label then newest first: the same dot
//       as its row in the project tree (the Sidebar's activity store), its
//       title, a hint naming what waits on it (a click opens the page at that thread), and a chip
//       with its project's name. Its title is a real button (Tab, Enter, Space); a click
//       anywhere on the row opens it like a tree row. A ✓ (shown on hover or focus) marks it Done
//       in one press; while the daemon's last read says Done would be refused it does nothing and
//       says why.
// A row that waits on a thread is drawn on that thread only, never also in the Needs-you rows.

import { memo, useEffect, useRef, useState } from "react";
import type { Project, Thread } from "../types";
import type { ActivityView } from "../lib/activity";
import { isHidden, type InboxRow } from "../lib/inbox";
import type { PaneNo } from "../lib/workspace";
import { threadLabel } from "../lib/threadTitle";
import { threadTooltip } from "../lib/threadmodel";
import {
  LABEL_TEXT, attachedHint, kindLabel, loadNeedsOpen, needDoneBlock, orderEntries, rowPending, saveNeedsOpen,
  type WorkingEntry,
} from "../lib/working";
import { useWorking } from "../state/working";
import { Dot } from "./ActivityDot";
import { HoverTitle } from "./HoverTitle";

export interface WorkingSectionProps {
  open: boolean;
  onToggle: () => void;
  /** The owner Inbox row's count (`waitingCount`; null when there is none to read) and amber/selected state. */
  inboxCount: number | null;
  pending: boolean;
  shown: boolean;
  entries: WorkingEntry[];
  needsYou: InboxRow[];
  threads: Thread[];
  projects: Project[];
  activity: ActivityView;
  selClass: (threadId: string) => string;
  paneOf: (threadId: string) => PaneNo | undefined;
  dragging: string | null;
  onPick: (id: string, beside: boolean) => void;
  /** Opens the Needs you page; with a thread id, at that thread's group. */
  onOpenInbox: (threadId?: string) => void;
  onMenu: (e: React.MouseEvent, id: string) => void;
  /** The row's ✓: Done for that thread (the Sidebar posts it and shows the Undo toast). */
  onDone: (id: string) => void;
  /** A Needs you row's ✓: Done for that row (the inbox's dismissal; the Sidebar posts it and shows the toast). */
  onDoneRow: (row: InboxRow) => void;
  onDragStart: (e: React.DragEvent, id: string) => void;
  onDragEnd: () => void;
}

export const SECTION_ID = "working-section-body";

/** The owner Inbox row (the rainbow one) as the list's header. */
export function InboxHeadRow(p: {
  count: number | null; pending: boolean; shown: boolean; stale: boolean;
  /** Whether there is a list to fold; when not, a click opens the Needs you page. */
  foldable: boolean; open: boolean; onToggle: () => void; onOpen: () => void;
}) {
  const label = p.count ? `Inbox, ${p.count} waiting on you` : "Inbox";   // null (unread) and 0 both say nothing
  return (
    <button
      type="button"
      // The RGB wave (theme.css) is this row only; it is calm with nothing waiting, dimmed while
      // something pending keeps the count amber.
      className={"tree-row ownerinbox aurora" + (p.shown ? " sel" : "")}
      data-aurora={p.pending ? "pending" : p.count ? "live" : "calm"}
      data-testid="owner-inbox"
      aria-label={label}
      aria-expanded={p.foldable ? p.open : undefined}
      aria-controls={p.foldable ? SECTION_ID : undefined}
      title={p.foldable ? (p.open ? "Fold the In progress list" : "Unfold the In progress list") : "Everything waiting on you, most urgent first"}
      onClick={p.foldable ? p.onToggle : p.onOpen}
    >
      {p.foldable ? (
        <span className="wchev" data-testid="working-chevron" aria-hidden="true">{p.open ? "▾" : "▸"}</span>
      ) : null}
      <span className="tw">✉</span>
      <span className="nm">Inbox</span>
      {p.stale ? (
        <span className="wstale" data-testid="working-stale" title="Could not refresh this list">!</span>
      ) : null}
      {p.count ? (
        <span className={"badge inboxcount" + (p.pending ? " pending" : "")}
              data-testid="owner-inbox-count" data-pending={p.pending ? "true" : "false"}>
          {p.count}
        </span>
      ) : null}
    </button>
  );
}

export const WorkingSection = memo(function WorkingSection(p: WorkingSectionProps) {
  const byId = new Map(p.threads.map((t) => [t.id, t]));
  const projectName = new Map(p.projects.map((pr) => [pr.id, pr.name]));
  // An entry whose chat this window does not hold (archived, or not read yet) has nothing to open.
  const rows = orderEntries(p.entries, p.activity)
    .map((e) => ({ e, t: byId.get(e.thread_id) }))
    .filter((x): x is { e: WorkingEntry; t: Thread } => !!x.t);
  // A row that waits on a thread drawn here is that thread's hint, never also a line in this group.
  const drawn = new Set(rows.map((x) => x.t.id));
  // A row the owner has hidden (marked done, dismissed, snoozed) is not drawn: the Needs you page still lists it.
  const needs = p.needsYou.filter((r) => !isHidden(r) && !(r.thread_id && drawn.has(r.thread_id)));
  const [needsOpen, setNeedsOpen] = useState<boolean>(() => loadNeedsOpen());
  const toggleNeeds = () =>
    setNeedsOpen((o) => {
      saveNeedsOpen(!o);
      return !o;
    });
  // null is "not read": a mark, never a zero (0 is only ever the daemon's answer).
  const known = p.inboxCount !== null;
  const waiting = p.inboxCount ?? 0;
  const hasList = !!rows.length || !!needs.length;
  return (
      <div className="workingsec" id={SECTION_ID} data-testid="working-section" data-open={p.open ? "true" : "false"}
           hidden={!p.open}>
        {p.open ? (
          <div className="wneedshead">
          {needs.length ? (
            <button
              type="button"
              className="wneedsfold"
              data-testid="working-needs-fold"
              aria-expanded={needsOpen}
              aria-controls="working-needs"
              aria-label={needsOpen ? "Fold the Needs you rows" : "Unfold the Needs you rows"}
              title={needsOpen ? "Fold these rows" : `Show ${needs.length} row${needs.length > 1 ? "s" : ""}`}
              onClick={toggleNeeds}
            >
              {needsOpen ? "▾" : "▸"}
            </button>
          ) : null}
          <button
            type="button"
            className={"tree-row indent-1 wneedsline" + (p.pending ? " pending" : "") + (known && !waiting ? " quiet0" : "")
              + (p.shown ? " sel" : "")}
            data-testid="working-needs-line"
            data-count={known ? waiting : "unknown"}
            aria-label={!known ? "Needs you, count not known yet" : waiting ? `Needs you, ${waiting} waiting` : "Needs you, nothing waiting"}
            title="Open the Needs you page"
            onClick={() => p.onOpenInbox()}
          >
            <span className="nm">Needs you<span className="wcount" data-testid="working-needs-count">{` · ${known ? waiting : "–"}`}</span></span>
          </button>
          </div>
        ) : null}
        {p.open && !hasList ? (
          <div className="wnone" data-testid="working-empty">Nothing in progress</div>
        ) : null}
        {p.open && hasList ? (
          <div className="wlist" data-testid="working-list">
            {needs.length && needsOpen ? (
              <div className="wneeds" id="working-needs" data-testid="working-needs">
                {needs.map((r) => {
                  const block = needDoneBlock(r);
                  return (
                  <div
                    key={r.id}
                    className={"tree-row indent-1 wrow wneed" + (rowPending(r) ? " pending" : "")}
                    data-testid="working-need"
                    data-id={r.id}
                    title={r.title}
                    onClick={() => p.onOpenInbox()}
                  >
                    <span className="wkind">{kindLabel(r)}</span>
                    <button type="button" className="wopen nm" onClick={(e) => { e.stopPropagation(); p.onOpenInbox(); }}>
                      {r.title}
                    </button>
                    {/* aria-disabled, as on a thread row: the click must not fall through to the row. */}
                    <button
                      type="button"
                      className="wdone"
                      data-testid={`working-need-done-${r.id}`}
                      aria-disabled={block ? "true" : undefined}
                      title={block ? `Can't mark done: ${block}` : "Mark done (the Needs you page's Show hidden still lists it)"}
                      aria-label={block ? `${r.title}: can't mark done, ${block}` : `Mark ${r.title} done`}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (!block) p.onDoneRow(r);
                      }}
                    >
                      ✓
                    </button>
                  </div>
                  );
                })}
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
                      title="Open the Needs you page at this chat"
                      aria-label={`${hint} waiting on ${label.text}: open the Needs you page at this chat`}
                      onClick={(ev) => { ev.stopPropagation(); p.onOpenInbox(t.id); }}
                    >
                      {hint}
                    </button>
                  ) : null}
                  {chip ? (
                    <span className="wchip" data-testid={`working-chip-${t.id}`} title={`Project: ${chip}`}>{chip}</span>
                  ) : null}
                  {/* aria-disabled, not disabled: a disabled button swallows the click, which would
                      then open the row instead of doing nothing, and hides its reason from hover. */}
                  <button
                    type="button"
                    className="wdone"
                    data-testid={`working-done-${t.id}`}
                    aria-disabled={e.why_not ? "true" : undefined}
                    title={e.why_not ? `Can't mark done: ${e.why_not}` : "Mark done (it stays in its project)"}
                    aria-label={e.why_not ? `${label.text}: can't mark done, ${e.why_not}` : `Mark ${label.text} done`}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      if (!e.why_not) p.onDone(t.id);
                    }}
                  >
                    ✓
                  </button>
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

/** The store's one subscriber: this block (the rainbow row and the list), and nothing else, redraws on a
 * refresh. The row is always the same element at the same place, so its focus survives the first read
 * landing, a fold and any error. It folds the list when there is one (a read has landed, even if the
 * last one failed); in every other state (before the first read, an older daemon, a failed first
 * read, whatever the error) it is a plain button that opens the Needs you page, as before. */
export const WorkingBlock = memo(function WorkingBlock(p: Omit<WorkingSectionProps, "entries" | "needsYou">) {
  const work = useWorking();
  const foldable = work.status === "ready" || (work.status === "error" && work.loaded);
  return (
    <>
      <InboxHeadRow count={p.inboxCount} pending={p.pending} shown={p.shown} stale={work.status === "error"}
                    foldable={foldable} open={p.open} onToggle={p.onToggle} onOpen={() => p.onOpenInbox()} />
      {foldable ? <WorkingSection {...p} entries={work.entries} needsYou={work.needsYou} /> : null}
    </>
  );
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
