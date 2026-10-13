import { useEffect, useState } from "react";
import { actionLine, agentActive, bannerOf } from "../lib/drive";
import type { DriveSnapshot } from "../lib/drivestate";
import "./DriveStatus.css";

/** Neutral status, never an authorization surface. No frame identity means no outline. */
export function DriveStatus({ snapshot, blocked, slotOf, onControl }: {
  snapshot: DriveSnapshot; blocked: boolean;
  slotOf: (threadId: string) => number | null;
  onControl: (action: "stop" | "resume") => void;
}) {
  const [now, setNow] = useState(Date.now);
  const [expanded, setExpanded] = useState(false);
  const ticking = snapshot.connected && snapshot.ready && agentActive(snapshot.state, now);
  useEffect(() => {
    setNow(Date.now());
    if (!ticking) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [ticking, snapshot.state.lastAt]);
  const banner = bannerOf(snapshot.state, now, slotOf);
  if (!banner && !snapshot.state.actions.length) return null;
  const unavailable = blocked || !snapshot.connected || !snapshot.ready || !!snapshot.pending;
  return <div className="drivestatus" data-testid="drive-status">
    <div className="row">
      <span role="status">{banner?.text ?? "Recent Browser actions"}{!snapshot.connected ? " · disconnected" : !snapshot.ready ? " · reconnecting" : ""}</span>
      {banner?.canStop ? <button type="button" disabled={unavailable} onClick={() => { if (!unavailable) onControl("stop"); }} data-testid="drive-stop">Stop</button> : null}
      {banner?.canResume ? <button type="button" disabled={unavailable} onClick={() => { if (!unavailable) onControl("resume"); }} data-testid="drive-resume">{snapshot.pending === "resume" ? "Resuming…" : "Resume Jarvis"}</button> : null}
      {snapshot.state.actions.length ? <button type="button" disabled={blocked} aria-expanded={expanded} onClick={() => { if (!blocked) setExpanded(v => !v); }} data-testid="drive-actions-toggle">Actions ({snapshot.state.actions.length})</button> : null}
    </div>
    {expanded ? <ol className="driveactions" data-testid="drive-actions">{snapshot.state.actions.slice().reverse().map(action =>
      <li key={action.id}><bdi>{actionLine(action)}</bdi></li>)}</ol> : null}
  </div>;
}
