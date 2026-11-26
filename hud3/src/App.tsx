import { useState, type ReactNode } from "react";
import BootOverlay from "@hud/components/BootOverlay";
import { Orb } from "./Orb";
import { Jobs, StatusView, useDaemonStatus } from "./Status";
import { ChatPanel } from "./components/chat/ChatPanel";
import { FeedPanel } from "./components/feed/FeedPanel";
import { WorkPanel } from "./components/work/WorkPanel";
import { VoicePanel } from "./components/voice/VoicePanel";

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
// The startup animation is v2's, unchanged: once per window, skipped by a click
// or a key, never with reduced motion, never over an approval card, and never
// under automation unless its own suite forces it. It is decorative: the
// window loads and polls underneath, and nothing waits on it.
function bootAllowed(): boolean {
  return typeof matchMedia === "function" && (!navigator.webdriver || window.__hudBootForce === true);
}

export default function App() {
  const status = useDaemonStatus();
  const [approvals, setApprovals] = useState(0);
  const [boot] = useState(bootAllowed);
  return (
    <main className="agent-window">
      {boot && <BootOverlay cardUp={approvals > 0} />}
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
        <ChatPanel onApprovalsChange={setApprovals} />
        <div className="agent-rail">
          <VoicePanel />
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
