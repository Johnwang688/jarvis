import { useState } from "react";
import { BriefCard, type BriefDraft } from "./BriefCard";
import { FeedMessages } from "./FeedMessages";
import { useFeed } from "./useFeed";
import type { Brief, BriefKind } from "./feedApi";
import type { WorkResult } from "../work/workApi";

const filters = [
  ["decision", "Decisions"],
  ["news", "News"],
  ["digest", "Digests"],
  ["all", "All"],
] as const;

export function FeedPanel({ work }: { work?: WorkResult }) {
  const { snapshot, error, pending, writeErrors, write } = useFeed();
  const [filter, setFilter] = useState<BriefKind | "all">("all");
  const [drafts, setDrafts] = useState<ReadonlyMap<string, BriefDraft>>(new Map());
  const matches = (brief: Brief) => filter === "all" || filter === brief.kind;
  const briefs = snapshot?.briefs.filter(matches) ?? [];
  const openIds = new Set(briefs.map(({ id }) => id));
  const recentById = new Map(snapshot?.recent.map((brief) => [brief.id, brief]));
  const recent = snapshot?.recent.filter((brief) => matches(brief) && !drafts.has(brief.id)) ?? [];
  const closedDrafts = [...drafts.values()].filter(({ brief }) => matches(brief) && !openIds.has(brief.id));
  const populated = snapshot && (snapshot.briefs.length > 0 || snapshot.recent.length > 0);

  function setDraft(id: string, draft: BriefDraft | null) {
    setDrafts((current) => {
      if (!draft && !current.has(id)) return current;
      const next = new Map(current);
      if (draft) next.set(id, draft);
      else next.delete(id);
      return next;
    });
  }

  const card = (brief: Brief, missing = false) => (
    <BriefCard key={brief.id} brief={brief} busy={pending.has(brief.id)} error={writeErrors.get(brief.id)}
      write={write} draft={drafts.get(brief.id)} setDraft={setDraft} missing={missing} />
  );

  return (
    <section className="agent-panel agent-feed" data-panel="feed" aria-labelledby="panel-feed">
      <h2 id="panel-feed">Feed</h2>
      <FeedMessages work={work} />
      {error && <p className="feed-error" role="status">{error}</p>}
      {!snapshot && !error && <p role="status">Loading briefs…</p>}
      {(populated || drafts.size > 0) && (
        <div className="feed-filters" role="group" aria-label="Filter briefs">
          {filters.map(([kind, label]) => (
            <button key={kind} type="button" aria-pressed={filter === kind} onClick={() => setFilter(kind)}>{label}</button>
          ))}
        </div>
      )}
      {!briefs.length && snapshot && !error && <p role="status">{populated && filter !== "all" ? "No open briefs in this view." : "No open briefs."}</p>}
      {(briefs.length > 0 || closedDrafts.length > 0) && (
        <div className="feed-list">
          {[...briefs.map((brief) => card(brief)), ...closedDrafts.map(({ brief }) => {
            const closed = recentById.get(brief.id);
            return card(closed ?? brief, !closed);
          })]}
        </div>
      )}
      {recent.length > 0 && (
        <section className="feed-recent" aria-labelledby="feed-recent">
          <h3 id="feed-recent">Recent</h3>
          <div className="feed-list">{recent.map((brief) => card(brief))}</div>
        </section>
      )}
    </section>
  );
}
