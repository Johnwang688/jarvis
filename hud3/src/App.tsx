import type { ReactNode } from "react";
import { Orb } from "./Orb";
import { Jobs, StatusView, useDaemonStatus } from "./Status";
import { ChatPanel } from "./components/chat/ChatPanel";
import { FeedPanel } from "./components/feed/FeedPanel";
import { WorkPanel } from "./components/work/WorkPanel";

function Panel({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="agent-panel" data-panel={id} aria-labelledby={`panel-${id}`}>
      <h2 id={`panel-${id}`}>{title}</h2>
      {children}
    </section>
  );
}

// One screen, no page scroll: a top bar, then the feed, the chat and a rail.
// Each panel scrolls inside itself.
export default function App() {
  const status = useDaemonStatus();
  return (
    <main className="agent-window">
      <header className="agent-top">
        <Orb />
        <h1>J.A.R.V.I.S. Agent</h1>
        <section className="agent-statusbar" data-panel="status" aria-labelledby="panel-status">
          <h2 id="panel-status">Status</h2>
          <StatusView status={status} />
        </section>
      </header>
      <div className="agent-grid">
        <FeedPanel />
        <ChatPanel />
        <div className="agent-rail">
          <Panel id="cron" title="Cron">
            <Jobs status={status} />
          </Panel>
          <WorkPanel />
          <Panel id="vault" title="Vault">
            <p>Saved memories will appear here.</p>
          </Panel>
          <Panel id="skills" title="Skills">
            <p>Skills and their digest will appear here.</p>
          </Panel>
        </div>
      </div>
    </main>
  );
}
