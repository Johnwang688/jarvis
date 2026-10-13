// The Browser pane (called Preview until BR-3a): an iframe onto the workshop
// origin (8403) or onto a localhost dev server the owner names — never onto
// the HUD's own origin, and never onto the daemon's API listener (WP-E).
//
// Two mechanical confinements, both deliberate. The URL is judged by
// `judgeBrowserUrl` before it is loaded at all, so the HUD origin is refused
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
// on every load and dropped when it no longer passes (lib/browser.ts). The
// frame never gets top navigation, popups or downloads, either way.
//
// **The URL is the pane's** (2026-10-09): what was last loaded is stored with
// the workspace (`url`, `onLoaded`), so a reload or a preset change brings it
// back — and it is **judged again** before it is loaded again, never trusted
// because it was once allowed. Loading the project root pins the pane to that
// project (`onProjectRoot`), as opening a file pins a File pane. A terminal's
// "Open in Browser" arrives as a `request` (WP-E), judged like a typed URL.
//
// **A framed page never takes the keyboard on its own** (R-1, amendment 2
// §E.8). The iframe is FrameHost's, made imperatively, and the frame guard
// (state/frameguard.ts) removes it the moment it gains focus without the
// owner's gesture: the pane then says "This page took the keyboard · Reload".
// The owner's own load — Load, Enter in the URL box, Project root, the
// keep-origin switch, Reload — is a gesture for that frame until 2 s after
// its `load`; a stored URL restored at mount and a terminal's "Open in
// Browser" are not. Reload loads the same URL again, judged again.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { judgeBrowserUrl, judgeKeepOrigin, keptOrigin, originOf, sandboxFor, workshopUrl } from "../lib/browser";
import { frameKey } from "../lib/frameguard";
import { noPointerFocus } from "../lib/talkkey";
import { useDaemonPorts } from "../state/daemonPorts";
import { cancelLoad, ownLoad } from "../state/frameguard";
import { FrameHost } from "./FrameHost";

/** "Open in Browser" from a terminal link: a URL, and a number so the same URL twice loads twice. */
export interface BrowserRequest {
  url: string;
  n: number;
}

export function BrowserTab(props: {
  /** The pane's id, its slot (what the owner reads) and whether this render draws it. */
  pane: number;
  slot: number;
  drawn: boolean;
  projectId: string | null;
  /** The URL this pane last loaded (stored), loaded again at mount if it still passes. */
  url?: string;
  /** The origin this pane lets its page keep (stored), applied only while it still passes. */
  keepOrigin?: string;
  /** A URL to load now (a terminal's "Open in Browser"). */
  request?: BrowserRequest | null;
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
  // The frame guard removed this tab's page: it took the keyboard (memory
  // only, never stored — the tab and its URL stay).
  const [stolen, setStolen] = useState(false);
  // When a theft gave focus back to nothing (the keyboard was taken from
  // another page, review of PR #78 H1), the notice's container takes it —
  // never its Reload button, and never a box an Enter would send from.
  const notice = useRef<HTMLDivElement>(null);
  const noticeWants = useRef(false);
  useLayoutEffect(() => {
    if (!stolen || !noticeWants.current) return;
    noticeWants.current = false;
    const a = document.activeElement;
    if (!a || a === document.body) notice.current?.focus({ preventScroll: true });
  }, [stolen]);
  // Every load asked for is a fresh frame (FrameHost), the same URL included.
  const [load, setLoad] = useState(0);
  const restored = useRef(false);
  const handled = useRef<number | null>(null);
  const latest = useRef(props);
  latest.current = props;
  const key = frameKey(props.pane, 0);

  /** Load `raw`, judged; `own` when it is the owner's own action on this pane (a gesture for its frame). */
  const go = (raw: string, own = false) => {
    const p = latest.current;
    const v = judgeBrowserUrl(raw, ports);
    if (!v.ok) {
      setSrc("");
      setReason(v.reason);
      return false;
    }
    if (own) ownLoad(key);
    else cancelLoad(key);
    setReason("");
    setStolen(false);
    setLoad((n) => n + 1);
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
          data-testid="browser-url"
          value={typed}
          placeholder="http://localhost:5173/ or the project's workshop path"
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter" && known) go(typed, true);
          }}
        />
        <button type="button" data-testid="browser-go" disabled={!known} onClick={() => go(typed, true)}>
          Load
        </button>
        <button
          type="button"
          data-testid="browser-project"
          disabled={!props.projectId || workshopPort === null}
          onClick={() => {
            if (!props.projectId || workshopPort === null) return;
            const u = workshopUrl(props.projectId, "index.html", workshopPort);
            setTyped(u);
            if (go(u, true)) props.onProjectRoot?.(props.projectId);
          }}
        >
          Project root
        </button>
        {offer?.ok ? (
          <label
            className="keeporigin"
            // Never focused by a pointer, so a habitual Space cannot flip the
            // grant back (lib/talkkey.ts, noPointerFocus); a click still does.
            onMouseDown={noPointerFocus}
            title={
              "Let this page keep its own origin, so its storage, cookies and live reload work. " +
              "Only for a local dev server: never Jarvis's own ports, and it turns itself off " +
              "when the pane loads another port, host or scheme."
            }
          >
            <input
              type="checkbox"
              data-testid="browser-keep-origin"
              data-origin={offer.origin}
              checked={!!grant}
              disabled={!!props.blocked}
              onChange={(e) => {
                // The page reloads under the new sandbox: the owner's own load.
                if (!stolen) ownLoad(key);
                props.onKeepOrigin?.(e.target.checked ? offer.origin : null);
              }}
              onKeyDown={(e) => e.stopPropagation()}
              onKeyUp={(e) => e.stopPropagation()}
            />
            Keep its origin
          </label>
        ) : null}
      </div>
      {reason ? (
        <div className="pad err" data-testid="browser-refused">
          {reason}
        </div>
      ) : null}
      {src && stolen ? (
        // HUD chrome, React text only: never the page's words, never a card's
        // colours or buttons. Under a card it is disabled (and inert).
        <div className="pad stolen" data-testid="browser-stolen" role="status" tabIndex={-1} ref={notice}>
          <span>This page took the keyboard</span>
          <span aria-hidden="true">·</span>
          <button
            type="button"
            className="quiet"
            data-testid="browser-stolen-reload"
            disabled={!!props.blocked || !known}
            title="Load this page again"
            onClick={() => go(src, true)}
          >
            Reload
          </button>
        </div>
      ) : src ? (
        // The sandbox's flags apply when the frame navigates, so a change of
        // them makes a fresh frame (FrameHost), reloading the page under them.
        <FrameHost
          pane={props.pane}
          slot={props.slot}
          drawn={props.drawn}
          src={src}
          sandbox={sandboxFor(!!grant)}
          keepOrigin={!!grant}
          load={load}
          onStolen={(focusNotice) => {
            noticeWants.current = focusNotice;
            setStolen(true);
          }}
        />
      ) : (
        <div className="pad muted">Nothing loaded.</div>
      )}
    </div>
  );
}
