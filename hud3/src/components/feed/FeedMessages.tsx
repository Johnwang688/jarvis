import { WorkNotice, WorkRow } from "../work/WorkRows";
import type { WorkResult, WorkThread } from "../work/workApi";

function messageStatus({ status, waiting_on_owner }: WorkThread) {
  // A message that was not delivered says so even if T3 also reports a pending request.
  if (waiting_on_owner && !["failed", "interrupted", "cancelled"].includes(status)) {
    return { state: "waiting", label: "waiting on you" };
  }
  switch (status) {
    case "started":
    case "queued":
    case "preparing":
    case "starting":
    case "running":
      return { state: "running", label: "sending" };
    case "completed":
    case "idle":
    case "exported":
      return { state: "done", label: "in T3" };
    case "waiting":
      return { state: "waiting", label: "waiting on you" };
    case "failed":
    case "interrupted":
    case "cancelled":
      return { state: "error", label: "not delivered" };
    default:
      return { state: "unknown", label: "unknown" };
  }
}

function MessageList({ work }: { work: WorkResult | undefined }) {
  if (!work || typeof work === "string") return <WorkNotice work={work} />;
  const messages = work.threads.filter((thread) => thread.message === true)
    .sort((a, b) => b.started_at - a.started_at).slice(0, 12);
  if (!messages.length) return <p>No messages yet.</p>;

  const now = Date.now() / 1000;
  return (
    <ul>
      {messages.map((thread) => (
        <WorkRow key={thread.thread_id} thread={thread} {...messageStatus(thread)}
          age={now - thread.started_at} showProject />
      ))}
    </ul>
  );
}

export function FeedMessages({ work }: { work: WorkResult | undefined }) {
  return (
    <section className="feed-messages" aria-labelledby="feed-messages">
      <h3 id="feed-messages">Messages</h3>
      <p>Read and answer these in T3.</p>
      <MessageList work={work} />
    </section>
  );
}
