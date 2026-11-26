import { span } from "../../Status";
import type { WorkResult, WorkThread } from "./workApi";

export function WorkNotice({ work }: { work: Extract<WorkResult, string> | undefined }) {
  if (work === "unreachable") return <p>T3 threads are not reachable.</p>;
  if (work === "unreadable") return <p>T3 threads could not be read.</p>;
  return <p>Checking…</p>;
}

export function WorkRow({ thread, state, label, age, showProject = false }: {
  thread: WorkThread;
  state: string;
  label: string;
  age: number;
  showProject?: boolean;
}) {
  return (
    <li className="work-row" data-state={state}>
      <bdi className="work-title">{thread.title.replace(/\p{Bidi_Control}/gu, "")}</bdi>
      {showProject && <span className="work-thread-project">{thread.project}</span>}
      <span className="work-status">{label}</span>
      <span className="work-when">{span(age)} ago</span>
    </li>
  );
}
