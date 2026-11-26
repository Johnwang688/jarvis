import { span } from "../../Status";
import { useWork } from "./useWork";
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

function ThreadRows({ threads, now, showProject = false }: {
  threads: WorkThread[];
  now: number;
  showProject?: boolean;
}) {
  return (
    <ul>
      {threads.map((thread) => {
        const { state, label } = statusLabel(thread);
        return (
          <li key={thread.thread_id} className="work-row" data-state={state}>
            <bdi className="work-title">{thread.title.replace(/\p{Bidi_Control}/gu, "")}</bdi>
            {showProject && <span className="work-thread-project">{thread.project}</span>}
            <span className="work-status">{label}</span>
            <span className="work-when">{span(now - thread.updated_at)} ago</span>
          </li>
        );
      })}
    </ul>
  );
}

function WorkList({ work }: { work: WorkResult | undefined }) {
  if (!work) return <p>Checking…</p>;
  if (work === "unreachable") return <p>T3 threads are not reachable.</p>;
  if (work === "unreadable") return <p>T3 threads could not be read.</p>;
  if (!work.threads.length) {
    return work.projects.length ? <p>No threads yet.</p> : <p>No projects registered. Use <code>jarvis work add</code>.</p>;
  }

  // Registered names keep their order. Removed projects follow in thread order.
  const groups = new Map<string, WorkThread[]>(work.projects.map(({ name }) => [name, []]));
  const messages: WorkThread[] = [];
  for (const thread of work.threads) {
    if (thread.message) {
      messages.push(thread);
      continue;
    }
    const group = groups.get(thread.project);
    if (group) group.push(thread);
    else groups.set(thread.project, [thread]);
  }

  const now = Date.now() / 1000;
  return (
    <div className="work-list">
      {messages.length > 0 && (
        <div className="work-project">
          <h3>Messages from Jarvis</h3>
          <ThreadRows threads={messages} now={now} showProject />
        </div>
      )}
      {[...groups].map(([project, threads]) => threads.length > 0 && (
        <div key={project} className="work-project" data-work-project={project}>
          <h3>{project}</h3>
          <ThreadRows threads={threads} now={now} />
        </div>
      ))}
    </div>
  );
}

export function WorkPanel() {
  const work = useWork();
  return (
    <section className="agent-panel work-panel" data-panel="t3" aria-labelledby="panel-t3">
      <h2 id="panel-t3">T3</h2>
      <WorkList work={work} />
    </section>
  );
}
