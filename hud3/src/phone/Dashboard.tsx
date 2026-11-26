import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { onPhoneNotice } from "../api";
import { Orb } from "../Orb";
import { Jobs, useDaemonStatus } from "../Status";
import { UnlockContext } from "../components/UnlockContext";
import { ChatPanel } from "../components/chat/ChatPanel";
import { FeedPanel } from "../components/feed/FeedPanel";
import { WorkPanel } from "../components/work/WorkPanel";
import { useWork } from "../components/work/useWork";
import { postAuth, readSession, unlockPhone } from "./auth";

const TABS = ["Feed", "Chat", "Work"] as const;
type Tab = typeof TABS[number];
const SESSION_POLL_MS = 15_000;
const MINUTE_MS = 60_000;
const endsAt = (seconds: number) => seconds ? performance.now() + seconds * 1000 : 0;

export function Dashboard({ initialSeconds, onSignedOut }: { initialSeconds: number; onSignedOut: () => void }) {
  const status = useDaemonStatus();
  const work = useWork();
  const [tab, setTab] = useState<Tab>("Feed");
  const [approvals, setApprovals] = useState(0);
  const [deadline, setDeadline] = useState(() => endsAt(initialSeconds));
  const [, tick] = useState(0);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"unlock" | "logout">();
  const operation = useRef<AbortController>();
  const sessionRead = useRef<AbortController>();
  const remaining = Math.max(0, deadline - performance.now());
  const minutes = Math.ceil(remaining / MINUTE_MS);
  const locked = minutes === 0;

  useEffect(() => {
    if (!minutes) return;
    const timer = setTimeout(() => tick((value) => value + 1), remaining - (minutes - 1) * MINUTE_MS);
    return () => clearTimeout(timer);
  }, [minutes, deadline, remaining]);

  // Subscribe before the panels start their requests.
  useLayoutEffect(() => {
    function refresh() {
      if (document.hidden || operation.current) return;
      sessionRead.current?.abort();
      const request = new AbortController();
      sessionRead.current = request;
      // Use the current server window after any data write that may slide it.
      void readSession(request.signal).then((seconds) => {
        if (!request.signal.aborted && seconds !== null) setDeadline(endsAt(seconds));
      }).catch(() => { /* The last confirmed deadline can still expire locally. */ });
    }
    const unsubscribe = onPhoneNotice((event) => {
      if (event === "session-expired") onSignedOut();
      if (event === "write") refresh();
      if (event === "locked") {
        sessionRead.current?.abort();
        setDeadline(0);
        setNotice("Phone locked. Unlock to try again.");
      }
    });
    const timer = setInterval(refresh, SESSION_POLL_MS);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      unsubscribe();
      clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
      sessionRead.current?.abort();
      operation.current?.abort();
    };
  }, [onSignedOut]);

  async function run(action: "unlock" | "logout") {
    if (operation.current) return;
    const request = new AbortController();
    operation.current = request;
    sessionRead.current?.abort();
    setBusy(action);
    setNotice("");
    try {
      if (action === "unlock") {
        const seconds = await unlockPhone(request.signal);
        if (!request.signal.aborted) setDeadline(endsAt(seconds));
      } else {
        const response = await postAuth("logout", {}, request.signal);
        if (!response.ok && response.status !== 401) throw new Error("Sign-out failed");
        if (!request.signal.aborted) onSignedOut();
      }
    } catch {
      if (!request.signal.aborted) {
        setNotice(action === "unlock" ? "Phone is still locked. Try again." : "Could not sign out. Try again.");
      }
    } finally {
      operation.current = undefined;
      if (!request.signal.aborted) setBusy(undefined);
    }
  }

  function moveTab(event: KeyboardEvent<HTMLDivElement>) {
    const index = TABS.indexOf(tab);
    const next = event.key === "ArrowRight" ? (index + 1) % TABS.length
      : event.key === "ArrowLeft" ? (index + TABS.length - 1) % TABS.length
      : event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    setTab(TABS[next]);
    event.currentTarget.querySelectorAll("button")[next]?.focus();
  }

  return (
    <UnlockContext.Provider value={{ locked, busy: !!busy, unlock: () => void run("unlock") }}>
      <main className="phone-shell phone-dashboard">
        <header className="phone-top">
          <Orb />
          <p role="status" className={status ? "phone-running" : status === null ? "phone-error" : undefined}>
            {status === undefined ? "Checking…" : status ? "Running" : "Not reachable"}
          </p>
          <span className="phone-lock" data-locked={locked} role="status">
            {locked ? "Locked" : `${minutes} min left`}
          </span>
          <button type="button" disabled={!!busy} onClick={() => void run("logout")}>Sign out</button>
          {(busy === "unlock" || notice) &&
            <p className="phone-notice" role="status">{busy === "unlock" ? "Waiting for passkey…" : notice}</p>}
        </header>
        <div className="phone-content">
          <div role="tabpanel" id="phone-feed" aria-labelledby="tab-feed" hidden={tab !== "Feed"}>
            <FeedPanel work={work} />
          </div>
          <div role="tabpanel" id="phone-chat" aria-labelledby="tab-chat" hidden={tab !== "Chat"}>
            <ChatPanel onApprovalsChange={setApprovals} />
          </div>
          <div role="tabpanel" id="phone-work" aria-labelledby="tab-work" hidden={tab !== "Work"} className="phone-work">
            <WorkPanel work={work} />
            <section className="agent-panel" data-panel="cron" aria-labelledby="phone-jobs">
              <h2 id="phone-jobs">Scheduled jobs</h2>
              <Jobs status={status} />
            </section>
          </div>
        </div>
        <div role="tablist" aria-label="Dashboard" className="phone-tabs" onKeyDown={moveTab}>
          {TABS.map((name) => {
            const id = name.toLowerCase();
            return (
              <button type="button" role="tab" key={name} id={`tab-${id}`} aria-controls={`phone-${id}`}
                aria-selected={name === tab} tabIndex={name === tab ? 0 : -1} onClick={() => setTab(name)}>
                {name}
                {name === "Chat" && approvals > 0 && (
                  <span className="phone-badge" aria-label={`${approvals} pending approvals`}>{approvals}</span>
                )}
              </button>
            );
          })}
        </div>
      </main>
    </UnlockContext.Provider>
  );
}
