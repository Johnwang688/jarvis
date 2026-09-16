// The preview pane: an iframe onto the workshop origin (8403) or onto a
// localhost dev server the owner names — never onto the HUD's own origin.
//
// Two mechanical confinements, both deliberate. The URL is judged by
// `judgePreviewUrl` before it is loaded at all, so the HUD origin is refused
// with a message rather than framed. And the frame's sandbox is
// `allow-scripts allow-forms` **without** `allow-same-origin`: the two
// together are no sandbox at all, because a framed page could then reach out
// of it and script the window that gates approvals.

import { useState } from "react";
import { judgePreviewUrl, workshopUrl } from "../lib/preview";

export function PreviewTab(props: { projectId: string | null }) {
  const [typed, setTyped] = useState("");
  const [src, setSrc] = useState("");
  const [reason, setReason] = useState("");

  const go = (raw: string) => {
    const v = judgePreviewUrl(raw);
    if (!v.ok) {
      setSrc("");
      setReason(v.reason);
      return;
    }
    setReason("");
    setSrc(v.url);
  };

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
          disabled={!props.projectId}
          onClick={() => {
            if (!props.projectId) return;
            const u = workshopUrl(props.projectId, "index.html");
            setTyped(u);
            go(u);
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
          id="previewframe"
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
