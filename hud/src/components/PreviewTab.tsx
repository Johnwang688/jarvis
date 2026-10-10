// The preview pane: an iframe onto the workshop origin (8403) or onto a
// localhost dev server the owner names — never onto the HUD's own origin.
//
// Two mechanical confinements, both deliberate. The URL is judged by
// `judgePreviewUrl` before it is loaded at all, so the HUD origin is refused
// with a message rather than framed. And the frame's sandbox is
// `allow-scripts allow-forms` **without** `allow-same-origin`: the two
// together are no sandbox at all, because a framed page could then reach out
// of it and script the window that gates approvals.
//
// **The URL is the pane's** (2026-10-09): what was last loaded is stored with
// the workspace (`url`, `onLoaded`), so a reload or a preset change brings it
// back — and it is **judged again** before it is loaded again, never trusted
// because it was once allowed. Loading the project root pins the pane to that
// project (`onProjectRoot`), as opening a file pins a File pane.

import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { judgePreviewUrl, workshopPortFrom, workshopUrl } from "../lib/preview";

export function PreviewTab(props: {
  projectId: string | null;
  /** The URL this pane last loaded (stored), loaded again at mount if it still passes. */
  url?: string;
  onLoaded?: (url: string) => void;
  onProjectRoot?: (projectId: string) => void;
}) {
  const [typed, setTyped] = useState(props.url || "");
  const [src, setSrc] = useState("");
  const [reason, setReason] = useState("");
  const restored = useRef(false);
  // The workshop origin of *the daemon serving this window*, from /status.
  // It used to be a constant 8403, so a HUD under test (served by a mock on
  // another port) loaded the owner's live daemon's preview for the fixture
  // project `p1`, and the live log filled with 400s.
  const [workshopPort, setWorkshopPort] = useState<number | null>(null);
  // A failed /status is *not* "a daemon too old to say": falling back to 8403
  // there would aim the preview at the live port again. It retries, and the
  // button stays disabled until the daemon has answered.
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ask = (attempt: number) => {
      api
        .status()
        .then((status) => live && setWorkshopPort(workshopPortFrom(status)))
        .catch(() => {
          if (live && attempt < 5) timer = setTimeout(() => ask(attempt + 1), 1000 * (attempt + 1));
        });
    };
    ask(0);
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const go = (raw: string) => {
    const v = judgePreviewUrl(raw);
    if (!v.ok) {
      setSrc("");
      setReason(v.reason);
      return false;
    }
    setReason("");
    setSrc(v.url);
    props.onLoaded?.(v.url);
    return true;
  };

  // The pane's last URL, through the same judge as a typed one.
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    if (props.url) go(props.url);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
            if (e.key === "Enter") go(typed);
          }}
        />
        <button type="button" data-testid="preview-go" onClick={() => go(typed)}>
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
      </div>
      {reason ? (
        <div className="pad err" data-testid="preview-refused">
          {reason}
        </div>
      ) : null}
      {src ? (
        <iframe
          className="previewframe"
          data-testid="preview-frame"
          title="preview"
          src={src}
          sandbox="allow-scripts allow-forms"
        />
      ) : (
        <div className="pad muted">Nothing loaded.</div>
      )}
    </div>
  );
}
