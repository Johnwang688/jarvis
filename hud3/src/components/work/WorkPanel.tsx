import { WorkNotice, WorkRow } from "./WorkRows";
import type { WorkResult, WorkThread } from "./workApi";

function statusLabel({ status, waiting_on_owner }: WorkThread) {
  if (waiting_on_owner || status === "waiting") return { state: "waiting", label: "waiting on you" };
  switch (status) {
    case "started":
    case "queued":
    case "preparing":
    case "starting":
    case "running":
      return { state: "running", label: "running" };
    case "failed":
    case "interrupted":
    case "cancelled":
      return { state: "error", label: status };
    case "completed":
    case "idle":
    case "exported":
      return { state: "done", label: status };
    case "rolled_back":
      return { state: "done", label: "rolled back" };
    default:
      return { state: "unknown", label: "unknown" };
  }
}

function WorkList({ work }: { work: WorkResult | undefined }) {
  if (!work || typeof work === "string") return <WorkNotice work={work} />;
  const threads = work.threads.filter((thread) => !thread.message);
  if (!threads.length) {
    return work.projects.length ? <p>No threads yet.</p> : <p>No projects registered. Use <code>jarvis work add</code>.</p>;
  }

  // Registered names keep their order. Removed projects follow in thread order.
  const groups = new Map<string, WorkThread[]>(work.projects.map(({ name }) => [name, []]));
  for (const thread of threads) {
    const group = groups.get(thread.project);
    if (group) group.push(thread);
    else groups.set(thread.project, [thread]);
  }

  const now = Date.now() / 1000;
  return (
    <div className="work-list">
      {[...groups].map(([project, threads]) => threads.length > 0 && (
        <div key={project} className="work-project" data-work-project={project}>
          <h3>{project}</h3>
          <ul>
            {threads.map((thread) => (
              <WorkRow key={thread.thread_id} thread={thread} {...statusLabel(thread)} age={now - thread.updated_at} />
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export function WorkPanel({ work }: { work: WorkResult | undefined }) {
  return (
    <section className="agent-panel work-panel" data-panel="t3" aria-labelledby="panel-t3">
      <h2 id="panel-t3">T3</h2>
      <WorkList work={work} />
    </section>
  );
}
