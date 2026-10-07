// Archiving a project, and the Archive view (decisions B1, B2, B3).
//
// **The confirmation is read from the backend, never counted here.** The
// dialog shows what `/impact` says archiving would touch and sends back the
// token it read, so a project that gained a task while the dialog was open
// refuses rather than archiving something the owner never saw.
//
// **Enter does nothing on these confirmations**, the approval card's rule:
// archiving and deleting take a deliberate click, and Escape (the cheap
// answer) closes. Unfinished tasks block an archive and are listed with a
// Cancel button each; the dialog reads `/impact` again when a task changes.
//
// **Permanent delete is only reachable from here**, only for something
// already archived, and goes to the trash (`jarvis/v2/trash.py`): nothing in
// the project's own folder and no worktree is touched, and the confirmation
// says which worktrees stay on disk.
//
// Every name and transcript line is React text: they come off disk, and this
// window draws authorization cards.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { ArchiveView as View, ArchivedProject, ChatMessage, Project, ProjectImpact, Thread } from "../types";
import { archiveSummary, deleteSummary, plural } from "../lib/projects";

/** Escape closes; Enter is swallowed before any focused button sees it. */
function useConfirmKeys(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);
}

export function ArchiveConfirm(props: {
  project: Project;
  /** Bumped by the window on every task event, so blockers re-read. */
  version: number;
  onCancelTask: (taskId: string) => Promise<unknown>;
  onArchived: (projectId: string) => void;
  onClose: () => void;
}) {
  const [impact, setImpact] = useState<ProjectImpact | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useConfirmKeys(props.onClose);

  const load = useCallback(() => {
    api
      .projectImpact(props.project.id)
      .then((i) => setImpact(i))
      .catch((e) => setError(e.message));
  }, [props.project.id]);
  useEffect(load, [load, props.version]);

  const blocked = !impact || impact.blockers.length > 0 || busy;
  const archive = () => {
    if (!impact || blocked) return;
    setBusy(true);
    setError("");
    api
      .archiveProject(props.project.id, impact.token)
      .then(() => props.onArchived(props.project.id))
      .catch((e) => {
        // A refusal is shown with the backend's words, and the counts re-read:
        // it is usually "something changed since you looked".
        setError(e.message);
        load();
      })
      .finally(() => setBusy(false));
  };

  return (
    <div className="pickerveil" data-testid="archive-confirm" onClick={(e) => {
      if (e.target === e.currentTarget) props.onClose();
    }}>
      <div className="picker confirm">
        <h3>Archive project · {props.project.name}</h3>
        <div className="rows pad col">
          {!impact ? <div className="muted small">reading what this touches…</div> : (
            <>
              <div className="small" data-testid="archive-summary">
                {archiveSummary(impact).map((line) => <div key={line}>{line}</div>)}
              </div>
              {impact.blockers.length ? (
                <div className="blockers" data-testid="archive-blockers">
                  <div className="err">It cannot be archived while work is unfinished:</div>
                  {impact.tasks.active.map((t) => (
                    <div className="row small" key={t.id} data-testid={`archive-task-${t.id}`}>
                      <span className={`phase ${t.state}`}>{t.state}</span>
                      <span style={{ flex: "1 1 auto", minWidth: 0 }}>{t.brief.slice(0, 70)}</span>
                      <button type="button" data-testid={`archive-cancel-${t.id}`}
                              onClick={() => props.onCancelTask(t.id).then(load).catch((e) => setError(e.message))}>
                        Cancel task
                      </button>
                    </div>
                  ))}
                  {impact.running_turns.map((r) => (
                    <div className="small" key={r.thread_id} data-testid={`archive-turn-${r.thread_id}`}>
                      A chat turn is running in “{r.title}”. Let it finish, or stop it, first.
                    </div>
                  ))}
                </div>
              ) : null}
            </>
          )}
          {error ? <div className="err" data-testid="archive-error">{error}</div> : null}
        </div>
        <div className="foot">
          <button type="button" data-testid="archive-keep" onClick={props.onClose}>Keep it</button>
          <span style={{ flex: "1 1 auto" }} />
          <button type="button" className="danger" data-testid="archive-confirm-btn"
                  disabled={blocked} onClick={archive}>
            Archive
          </button>
        </div>
      </div>
    </div>
  );
}

type Pending =
  | { kind: "project"; project: ArchivedProject; impact: ProjectImpact | null }
  | { kind: "thread"; thread: Thread };

/**
 * The Archive: archived projects and threads, each with Restore and Delete
 * permanently, and a read-only transcript. Nothing here can send a message.
 */
export function ArchiveView(props: {
  version: number;
  onRestored: () => void;
  onDeleted: () => void;
  onClose: () => void;
}) {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [reading, setReading] = useState<{ thread: Thread; messages: ChatMessage[] } | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [emptying, setEmptying] = useState(false);
  const [busy, setBusy] = useState(false);

  const close = useCallback(() => {
    if (reading) setReading(null);
    else if (pending) setPending(null);
    else props.onClose();
  }, [reading, pending, props]);
  useConfirmKeys(close);

  const load = useCallback(() => {
    api.archive().then(setView).catch((e) => setError(e.message));
  }, []);
  useEffect(load, [load, props.version]);

  const act = (p: Promise<unknown>, after: () => void, done: string) => {
    setBusy(true);
    setError("");
    p.then(() => {
      setNotice(done);
      after();
      load();
    })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  };

  const askDelete = (p: Pending) => {
    setPending(p);
    if (p.kind === "project") {
      api
        .projectImpact(p.project.id)
        .then((impact) => setPending({ ...p, impact }))
        .catch((e) => setError(e.message));
    }
  };

  const confirmDelete = () => {
    if (!pending) return;
    if (pending.kind === "project") {
      if (!pending.impact) return;
      act(api.deleteProject(pending.project.id, pending.impact.token), () => {
        setPending(null);
        props.onDeleted();
      }, `Deleted ${pending.project.name}; Jarvis's records are in the trash.`);
    } else {
      act(api.deleteThread(pending.thread.id), () => {
        setPending(null);
        props.onDeleted();
      }, `Deleted “${pending.thread.title || pending.thread.id}”; it is in the trash.`);
    }
  };

  const read = (t: Thread) =>
    api.transcript(t.id).then((r) => setReading({ thread: t, messages: r.messages || [] }))
      .catch((e) => setError(e.message));

  const days = view?.trash.retention_days ?? null;

  const threadRow = (t: Thread, inProject: boolean) => (
    <div className="prow arow" key={t.id} data-testid={`archived-thread-${t.id}`}>
      <span className="nm">{t.title || t.id}</span>
      <span className="sub">{plural(t.turns, "turn")}</span>
      <button type="button" data-testid={`archive-open-${t.id}`} onClick={() => read(t)}>Open</button>
      {inProject ? null : (
        <>
          <button type="button" data-testid={`archive-restore-thread-${t.id}`} disabled={busy}
                  onClick={() => act(api.restoreThread(t.id), props.onRestored,
                                     `Restored “${t.title || t.id}”.`)}>
            Restore
          </button>
          <button type="button" className="danger" data-testid={`archive-delete-thread-${t.id}`} disabled={busy}
                  onClick={() => askDelete({ kind: "thread", thread: t })}>
            Delete…
          </button>
        </>
      )}
    </div>
  );

  let body: React.ReactNode;
  if (reading) {
    body = (
      <div className="pad col" data-testid="archive-transcript">
        <div className="muted small">Read-only · {reading.thread.title || reading.thread.id}</div>
        {reading.messages.length === 0 ? <div className="muted small">nothing was said in it</div> : null}
        {reading.messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <span className="who">{m.role === "user" ? "you" : m.role}</span> {m.text}
          </div>
        ))}
      </div>
    );
  } else if (pending) {
    const lines = pending.kind === "project"
      ? (pending.impact ? deleteSummary(pending.impact, days) : ["reading what this touches…"])
      : [`“${pending.thread.title || pending.thread.id}” and its transcript go to the trash.`,
         ...(days ? [`The trash keeps it for ${days} days.`] : [])];
    body = (
      <div className="pad col" data-testid="delete-confirm">
        <div className="err">
          Delete permanently: {pending.kind === "project" ? pending.project.name : pending.thread.title || pending.thread.id}
        </div>
        <div className="small" data-testid="delete-summary">
          {lines.map((l) => <div key={l}>{l}</div>)}
        </div>
      </div>
    );
  } else {
    body = (
      <div className="pad col">
        {!view ? <div className="muted small">loading…</div> : null}
        {view && !view.projects.length && !view.threads.length ? (
          <div className="muted small" data-testid="archive-empty">Nothing is archived.</div>
        ) : null}
        {view?.projects.length ? <div className="mhead">Projects</div> : null}
        {view?.projects.map((p) => (
          <div key={p.id} data-testid={`archived-project-${p.id}`}>
            <div className="prow arow">
              <span className="tw" onClick={() => setOpen((o) => ({ ...o, [p.id]: !o[p.id] }))}
                    data-testid={`archive-expand-${p.id}`}>
                {open[p.id] ? "▾" : "▸"}
              </span>
              <span className="nm">{p.name}</span>
              <span className="sub">
                {plural(p.threads.length, "thread")} · {plural(p.tasks.length, "task")} · {p.root}
              </span>
              <button type="button" data-testid={`archive-restore-${p.id}`} disabled={busy}
                      onClick={() => act(api.restoreProject(p.id), props.onRestored, `Restored ${p.name}.`)}>
                Restore
              </button>
              <button type="button" className="danger" data-testid={`archive-delete-${p.id}`} disabled={busy}
                      onClick={() => askDelete({ kind: "project", project: p, impact: null })}>
                Delete…
              </button>
            </div>
            {open[p.id] ? p.threads.map((t) => threadRow(t, true)) : null}
          </div>
        ))}
        {view?.threads.length ? <div className="mhead">Threads</div> : null}
        {view?.threads.map((t) => (
          <div key={t.id}>
            {threadRow(t, false)}
            <div className="muted small indent-1">in {t.project_name || t.project_id}</div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="pickerveil" data-testid="archive-view" onClick={(e) => {
      if (e.target === e.currentTarget) close();
    }}>
      <div className="picker">
        <h3>Archive</h3>
        <div className="rows">
          {body}
          {notice ? <div className="pad small" data-testid="archive-notice">{notice}</div> : null}
          {error ? <div className="pad err" data-testid="archive-view-error">{error}</div> : null}
        </div>
        <div className="foot">
          {reading || pending ? (
            <button type="button" data-testid="archive-back" onClick={close}>Back</button>
          ) : null}
          {pending ? (
            <button type="button" className="danger" data-testid="delete-confirm-btn"
                    disabled={busy || (pending.kind === "project" && !pending.impact)}
                    onClick={confirmDelete}>
              Delete permanently
            </button>
          ) : null}
          {!reading && !pending && view ? (
            <span className="muted small" data-testid="trash-info" style={{ alignSelf: "center" }}>
              Trash: {plural(view.trash.entries, "item")} · purged after {view.trash.retention_days} days
            </span>
          ) : null}
          {!reading && !pending && view && view.trash.entries ? (
            emptying ? (
              <button type="button" className="danger" data-testid="trash-empty-confirm" disabled={busy}
                      onClick={() => act(api.emptyTrash(), () => setEmptying(false), "Emptied Jarvis's trash.")}>
                Empty it now
              </button>
            ) : (
              <button type="button" data-testid="trash-empty" onClick={() => setEmptying(true)}>Empty trash…</button>
            )
          ) : null}
          <span style={{ flex: "1 1 auto" }} />
          <button type="button" data-testid="archive-close" onClick={props.onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
