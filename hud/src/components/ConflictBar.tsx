// The conflict bar (ED-2, editor plan §2.4): the file on disk moved under an
// unsaved edit (or a save answered 409), or the file is gone. Amber, because
// it waits on the owner's decision — this is HUD chrome, not syntax.
//
//  - **Compare**: the disk's text on the left, read-only; the buffer on the
//    right, editable — merge there, then Keep mine.
//  - **Keep mine**: save against the disk etag this bar was shown for, so a
//    write that lands meanwhile brings the bar back rather than being lost.
//  - **Take theirs**: asks once, then loads the disk's text (undoably: Ctrl+Z
//    brings the edit back).
//  - **Gone**: Save to recreate (`expected_etag: null`), or Close.
// Nothing here is a force-save, and there is none anywhere.

import { useState } from "react";
import type { Conflict } from "../lib/buffers";

export function ConflictBar(props: {
  path: string;
  conflict: Conflict;
  dirty: boolean;
  comparing: boolean;
  busy: boolean;
  onCompare: () => void;
  onKeep: () => void;
  onTake: () => void;
  onRecreate: () => void;
  onClose: () => void;
}) {
  const [asking, setAsking] = useState(false);
  const { conflict } = props;
  if (conflict.state === "gone") {
    return (
      <div className="toolbar conflictbar" data-testid="editor-conflict" data-state="gone" role="alert">
        <span className="msg">⚠ {props.path} is gone from disk{props.dirty ? " — your edit is still here" : ""}.</span>
        <button type="button" data-testid="conflict-recreate" disabled={props.busy} onClick={props.onRecreate}>
          Save to recreate
        </button>
        <button type="button" data-testid="conflict-close" onClick={props.onClose}>Close</button>
      </div>
    );
  }
  return (
    <div className="toolbar conflictbar" data-testid="editor-conflict" data-state="changed"
         data-etag={conflict.etag} data-again={String(!!conflict.again)} role="alert">
      <span className="msg">
        ⚠ {props.path} changed on disk{conflict.again ? " again" : ""}{" "}
        {props.dirty ? "while you were editing" : "since it was read"}.
      </span>
      {asking ? (
        <>
          <span className="msg">Throw your edit away and load the disk's version?</span>
          <button type="button" data-testid="conflict-take-yes" onClick={() => { setAsking(false); props.onTake(); }}>
            Take theirs
          </button>
          <button type="button" data-testid="conflict-take-no" onClick={() => setAsking(false)}>Cancel</button>
        </>
      ) : (
        <>
          <button
            type="button"
            data-testid="conflict-compare"
            aria-pressed={props.comparing}
            className={props.comparing ? "on" : undefined}
            onClick={props.onCompare}
          >
            {props.comparing ? "Close compare" : "Compare"}
          </button>
          <button type="button" data-testid="conflict-keep" disabled={props.busy} onClick={props.onKeep}>
            Keep mine
          </button>
          <button type="button" data-testid="conflict-take" disabled={props.busy} onClick={() => setAsking(true)}>
            Take theirs
          </button>
        </>
      )}
    </div>
  );
}
