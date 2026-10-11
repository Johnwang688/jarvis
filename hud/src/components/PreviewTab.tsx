// The preview pane: an iframe onto the workshop origin (8403) or onto a
// localhost dev server the owner names — never onto the HUD's own origin, and
// never onto the daemon's API listener (WP-E).
//
// Two mechanical confinements, both deliberate. The URL is judged by
// `judgePreviewUrl` before it is loaded at all, so the HUD origin is refused
// with a message rather than framed. And the frame's sandbox is
// `allow-scripts allow-forms` **without** `allow-same-origin`: the two
// together are no sandbox at all when the framed page is the HUD's origin,
// because it could then reach out of the frame and script the window that
// gates approvals.
//
// **Keep its own origin (WP-E, decisions W-4).** A dev app needs its origin
// for `localStorage`, cookies and HMR. The pane's "Keep its origin" switch
// adds `allow-same-origin` — off by default, offered only for a local dev
// server on a port that is none of the daemon's three nor this window's own,
// and only while `/status` says every HUD and API response refuses to be
// framed (`frame_hardened`). It is stored as the origin it was given for, so
// another port, host or scheme turns it off, and a stored one is judged again
// on every load and dropped when it no longer passes (lib/preview.ts). The
// frame never gets top navigation, popups or downloads, either way.
//
// **The URL is the pane's** (2026-10-09): what was last loaded is stored with
// the workspace (`url`, `onLoaded`), so a reload or a preset change brings it
// back — and it is **judged again** before it is loaded again, never trusted
// because it was once allowed. Loading the project root pins the pane to that
// project (`onProjectRoot`), as opening a file pins a File pane. A terminal's
// "Open in Preview" arrives as a `request` (WP-E), judged like a typed URL.

import { useEffect, useRef, useState } from "react";
import { judgeKeepOrigin, judgePreviewUrl, keptOrigin, originOf, sandboxFor, workshopUrl } from "../lib/preview";
import { useDaemonPorts } from "../state/daemonPorts";

/** "Open in Preview" from a terminal link: a URL, and a number so the same URL twice loads twice. */
export interface PreviewRequest {
  url: string;
  n: number;
}

export function PreviewTab(props: {
  projectId: string | null;
  /** The URL this pane last loaded (stored), loaded again at mount if it still passes. */
  url?: string;
  /** The origin this pane lets its page keep (stored), applied only while it still passes. */
  keepOrigin?: string;
  /** A URL to load now (a terminal's "Open in Preview"). */
  request?: PreviewRequest | null;
  /** An authorization card is up: the switch is disabled (and, behind the veil, unreachable). */
  blocked?: boolean;
  onLoaded?: (url: string) => void;
  onKeepOrigin?: (origin: string | null) => void;
  onRequestHandled?: (n: number) => void;
  onProjectRoot?: (projectId: string) => void;
}) {
  // The ports of *the daemon serving this window*, from /status. Nothing is
  // restored or requested until it has answered: the API port is judged only
  // once it is known, and a failed /status never stands in for 8403 (that
  // aimed the HUD suite at the live daemon once).
  const { status, ports } = useDaemonPorts();
  const known = status !== "pending";
  const [typed, setTyped] = useState(props.url || "");
  const [src, setSrc] = useState("");
  const [reason, setReason] = useState("");
  const restored = useRef(false);
  const handled = useRef<number | null>(null);
  const latest = useRef(props);
  latest.current = props;

  const go = (raw: string) => {
    const p = latest.current;
    const v = judgePreviewUrl(raw, ports);
    if (!v.ok) {
      setSrc("");
      setReason(v.reason);
      return false;
    }
    setReason("");
    setSrc(v.url);
    p.onLoaded?.(v.url);
    // Another port, host or scheme: the grant stays with the origin it was given for.
    if (p.keepOrigin && originOf(v.url) !== p.keepOrigin) p.onKeepOrigin?.(null);
    return true;
  };

  // The pane's last URL, or a terminal's request, through the same judge as a
  // typed one — once the daemon has said which ports are its own.
  useEffect(() => {
    if (!known) return;
    const req = latest.current.request;
    if (req && handled.current !== req.n) {
      handled.current = req.n;
      restored.current = true;
      setTyped(req.url);
      go(req.url);
      latest.current.onRequestHandled?.(req.n);
      return;
    }
    if (restored.current) return;
    restored.current = true;
    if (latest.current.url) go(latest.current.url);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [known, props.request?.n]);

  // A stored grant is judged again on every load: once /status has answered,
  // one that no longer passes (another origin, a daemon port, a daemon that
  // does not say it is frame-hardened) is dropped, not just unused.
  const target = src || props.url || "";
  useEffect(() => {
    if (status !== "ok" || !props.keepOrigin || !target) return;
    if (!keptOrigin(props.keepOrigin, target, ports)) props.onKeepOrigin?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, props.keepOrigin, target]);

  const grant = status === "ok" && src ? keptOrigin(props.keepOrigin, src, ports) : null;
  const offer = status === "ok" && src ? judgeKeepOrigin(src, ports) : null;
  const workshopPort = status === "ok" ? ports.workshop : null;

  return (
    <div className="tabbody">
      <div className="toolbar">
        <input
          data-testid="preview-url"
          value={typed}
          placeholder="http://localhost:5173/ or the project preview path"
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter" && known) go(typed);
          }}
        />
        <button type="button" data-testid="preview-go" disabled={!known} onClick={() => go(typed)}>
          Load
        </button>
        <button
          type="button"
          data-testid="preview-project"
          disabled={!props.projectId || workshopPort === null}
          onClick={() => {
            if (!props.projectId || workshopPort === null) return;
            const u = workshopUrl(props.projectId, "index.html", workshopPort);
            setTyped(u);
            if (go(u)) props.onProjectRoot?.(props.projectId);
          }}
        >
          Project root
        </button>
        {offer?.ok ? (
          <label
            className="keeporigin"
            title={
              "Let this page keep its own origin, so its storage, cookies and live reload work. " +
              "Only for a local dev server: never Jarvis's own ports, and it turns itself off " +
              "when the pane loads another port, host or scheme."
            }
          >
            <input
              type="checkbox"
              data-testid="preview-keep-origin"
              data-origin={offer.origin}
              checked={!!grant}
              disabled={!!props.blocked}
              onChange={(e) => props.onKeepOrigin?.(e.target.checked ? offer.origin : null)}
              onKeyDown={(e) => e.stopPropagation()}
              onKeyUp={(e) => e.stopPropagation()}
            />
            Keep its origin
          </label>
        ) : null}
      </div>
      {reason ? (
        <div className="pad err" data-testid="preview-refused">
          {reason}
        </div>
      ) : null}
      {src ? (
        // Keyed by the sandbox: its flags apply when the frame navigates, so a
        // change reloads the page under the new ones.
        <iframe
          key={grant ? "keep" : "opaque"}
          className="previewframe"
          data-testid="preview-frame"
          data-keep-origin={grant ? "true" : "false"}
          title="preview"
          src={src}
          sandbox={sandboxFor(!!grant)}
        />
      ) : (
        <div className="pad muted">Nothing loaded.</div>
      )}
    </div>
  );
}
