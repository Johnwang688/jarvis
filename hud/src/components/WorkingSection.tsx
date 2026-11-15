// The Inbox section under the owner Inbox row (WS-2): the chat threads the
// owner is working on now, from every project, as a second view of the same
// threads (they stay in their projects). Presentational: the Sidebar owns the
// store, the thread menu, the drag state and the Undo toast.
//
// What it draws, top to bottom (D-5, D-9, D-10):
//   - a header with the fold chevron (the choice lasts, lib/working);
//   - the "Needs you" group: every row that has no thread entry to sit on, as
//     a kind word and a title (text, never HTML); a click opens today's
//     Inbox page, where answering happens;
//   - one row per thread, ordered by label then newest first: the same dot as
//     its row in the project tree (the Sidebar's activity store), its title
//     and a chip with its project's name. A click opens it like a tree row.

import { memo } from "react";
import type { Project, Thread } from "../types";
import type { ActivityView } from "../lib/activity";
import { Dot } from "./ActivityDot";
import type { InboxRow } from "../lib/inbox";
import type { PaneNo } from "../lib/workspace";
import { threadLabel } from "../lib/threadTitle";
import { threadTooltip } from "../lib/threadmodel";
import { LABEL_TEXT, kindLabel, orderEntries, rowPending, type WorkingEntry } from "../lib/working";
import { HoverTitle } from "./HoverTitle";

export interface WorkingSectionProps {
  open: boolean;
  onToggle: () => void;
  entries: WorkingEntry[];
  needsYou: InboxRow[];
  /** Whether the daemon could be read at all just now (a failed read keeps the last list). */
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
        <span className="wlabel">In progress</span>
        <span className="wcount" data-testid="working-count">{rows.length}</span>
      </div>
      {p.open ? (
        <>
          {p.needsYou.length ? (
            <div className="wneeds" data-testid="working-needs">
              <div className="whead">Needs you</div>
              {p.needsYou.map((r) => (
                <div
                  key={r.id}
                  role="button"
                  tabIndex={0}
                  className={"tree-row indent-1 wrow wneed" + (rowPending(r) ? " pending" : "")}
                  data-testid="working-need"
                  data-id={r.id}
                  title={r.title}
                  onClick={p.onOpenInbox}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      p.onOpenInbox();
                    }
                  }}
                >
                  <span className="wkind">{kindLabel(r)}</span>
                  <span className="nm">{r.title}</span>
                </div>
              ))}
            </div>
          ) : null}
          {rows.map(({ e, t }) => {
            const status = p.activity.threads[t.id] || "idle";
            const pid = t.project_id || e.project_id || "";
            const chip = projectName.get(pid) || "";
            const label = threadLabel(t);
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
                <HoverTitle text={label.text} untitled={label.untitled} testid={`working-name-${t.id}`}
                  onDoubleClick={() => {}} />
                {chip ? (
                  <span className="wchip" data-testid={`working-chip-${t.id}`} title={`Project: ${chip}`}>{chip}</span>
                ) : null}
                <button
                  type="button"
                  className="menu"
                  data-testid={`working-menu-${t.id}`}
                  title="Done, move to…"
                  aria-label={`${label.text}: menu`}
                  onClick={(ev) => p.onMenu(ev, t.id)}
                >
                  ⋯
                </button>
              </div>
            );
          })}
          {!rows.length && !p.needsYou.length ? (
            <div className="wempty muted small" data-testid="working-empty">Nothing in progress</div>
          ) : null}
          {p.stale ? (
            <div className="wempty muted small" data-testid="working-stale">Could not refresh this list</div>
          ) : null}
        </>
      ) : null}
    </div>
  );
});
