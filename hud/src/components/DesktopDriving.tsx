import "./DriveStatus.css";
import type { DesktopDrivingState } from "../api";
import type { Thread } from "../types";

export function DesktopDriving({ state, thread, busy, canCreate, onToggle, onStop, onCreate }: {
  state: DesktopDrivingState;
  thread?: Thread | null;
  busy: boolean;
  canCreate: boolean;
  onToggle: (threadId: string, on: boolean) => void;
  onStop: (threadId: string) => void;
  onCreate: () => void;
}) {
  if (state.enabled !== true) return null;
  const chat = thread?.role === "chat" && !thread.task_id && !thread.archived
    && (thread.provider === "codex" || thread.provider === "claude") ? thread : null;
  const on = !!chat && state.thread_id === chat.id;
  return <div className="drivestatus" data-testid="desktop-driving">
    <div className="row">
      {chat ? <label><input type="checkbox" checked={on} disabled={busy}
        onChange={e => onToggle(chat.id, e.currentTarget.checked)} /> Drive desktop</label> : null}
      {chat && on ? <>
        <span role="status">Driving the desktop{state.badge_showing !== true &&
          <span className="drivebadge-warning">Stop badge and Esc unavailable: the voice helper is not connected</span>}
        </span>
        <button type="button" aria-label="Stop desktop driving" onClick={() => onStop(chat.id)}>Stop</button>
      </> : null}
      <button type="button" disabled={busy || !canCreate} onClick={onCreate}>New desktop driver chat</button>
    </div>
  </div>;
}
