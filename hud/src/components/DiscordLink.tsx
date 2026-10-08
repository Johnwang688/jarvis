// A project's Discord channel, in the project dialog (B1, plan §3).
//
// A pill with the channel's state and who made the link, and the three owner
// actions: Create (a channel in the Jarvis category), Link (paste a channel
// id) and Unlink — which keeps the channel, always (decisions D3: channels are
// never deleted). The backend validates everything; a refusal is shown inline
// in its own words. Before `jarvis auth discord-guild` the buttons are off and
// the dialog says which command turns them on. The Inbox's channel is
// #ungrouped and only setup changes it, so it gets no buttons.
//
// Every name and reason is React text: channel names come off the network,
// and this window draws authorization cards.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Project, ProjectChannel } from "../types";
import { channelPill, originText } from "../lib/discord";

export function DiscordLink(props: { project: Project; onChanged?: () => void }) {
  const [view, setView] = useState<ProjectChannel | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [paste, setPaste] = useState("");
  const [linking, setLinking] = useState(false);

  const load = useCallback((refresh = false) => {
    api.projectDiscord(props.project.id, refresh).then(setView).catch((e) => setError(e.message));
  }, [props.project.id]);
  useEffect(() => load(), [load]);

  const act = (body: Parameters<typeof api.projectDiscordAction>[1]) => {
    if (busy) return;
    setBusy(true);
    setError("");
    api
      .projectDiscordAction(props.project.id, body)
      .then((next) => {
        setView(next);
        setLinking(false);
        setPaste("");
        props.onChanged?.();
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  };

  const pill = channelPill(view);
  const unconfigured = view?.state === "unconfigured";
  const linked = !!view?.channel_id;
  const inbox = props.project.inbox;
  const off = busy || !view || unconfigured;

  return (
    <div className="col discord-link" data-testid="discord-link">
      <div className="row">
        <span className={`pill ${pill.level}`} data-testid="discord-pill" data-state={view?.state || ""}>
          <i className={`discord-dot ${pill.level}`} aria-hidden="true" />
          {pill.text}
        </span>
        {view?.origin ? (
          <span className="muted small" data-testid="discord-origin">{originText(view.origin)}</span>
        ) : null}
        {view?.rename_pending ? (
          <span className="muted small" data-testid="discord-rename-pending">rename pending</span>
        ) : null}
        <button type="button" className="ghost" data-testid="discord-recheck" disabled={busy}
                onClick={() => load(true)} title="check the channel again">↻</button>
      </div>
      {unconfigured ? (
        <div className="muted small" data-testid="discord-setup">
          Set up the server first: run <code>jarvis auth discord-guild</code>.
        </div>
      ) : null}
      {inbox ? (
        <div className="muted small" data-testid="discord-inbox-note">
          The Inbox posts in #ungrouped; only <code>jarvis auth discord-guild</code> changes that.
        </div>
      ) : (
        <div className="row">
          {!linked ? (
            <button type="button" data-testid="discord-create" disabled={off || linking}
                    onClick={() => act({ action: "create" })}>
              Create channel
            </button>
          ) : null}
          {!linked && !linking ? (
            <button type="button" data-testid="discord-link-open" disabled={off}
                    onClick={() => setLinking(true)}>
              Link existing…
            </button>
          ) : null}
          {linked ? (
            <button type="button" data-testid="discord-unlink" disabled={off}
                    onClick={() => act({ action: "unlink" })}>
              Unlink (the channel is kept)
            </button>
          ) : null}
        </div>
      )}
      {linking && !linked && !inbox ? (
        <div className="row">
          <input data-testid="discord-link-id" placeholder="paste a channel id" value={paste}
                 onKeyDown={(e) => {
                   e.stopPropagation();
                   if (e.key === "Enter" && paste.trim()) act({ action: "link", channel_id: paste.trim() });
                   if (e.key === "Escape") setLinking(false);
                 }}
                 onChange={(e) => setPaste(e.target.value)} />
          <button type="button" data-testid="discord-link-submit" disabled={off || !paste.trim()}
                  onClick={() => act({ action: "link", channel_id: paste.trim() })}>
            Link
          </button>
        </div>
      ) : null}
      {error ? <div className="err" data-testid="discord-error">{error}</div> : null}
    </div>
  );
}
