import type { ReactNode } from "react";
import { Orb } from "./Orb";
import { Status } from "./Status";
import { FeedPanel } from "./components/feed/FeedPanel";

function Panel({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="agent-panel" data-panel={id} aria-labelledby={`panel-${id}`}>
      <h2 id={`panel-${id}`}>{title}</h2>
      {children}
    </section>
  );
}

export default function App() {
  return (
    <main className="agent-window">
      <h1>J.A.R.V.I.S. Agent</h1>
      <div className="agent-grid">
        <Orb />
        <div className="agent-column">
          <Panel id="t3" title="T3"><p>T3 threads will appear here.</p></Panel>
          <FeedPanel />
          <Panel id="vault" title="Vault"><p>Saved memories will appear here.</p></Panel>
        </div>
        <div className="agent-column">
          <Panel id="skills" title="Skills"><p>Skills and their digest will appear here.</p></Panel>
          <Panel id="cron" title="Cron"><p>Scheduled jobs will appear here.</p></Panel>
          <Panel id="status" title="Status"><Status /></Panel>
        </div>
      </div>
    </main>
  );
}
