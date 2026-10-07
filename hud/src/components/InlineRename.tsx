// Inline rename for a sidebar row (decisions B4): Enter saves, Escape
// cancels, and so does leaving the box — a rename nobody confirmed is not
// one. The box swallows its own clicks and keys, so typing a space is not
// push-to-talk and clicking into it does not collapse the row under it.
//
// The backend numbers a colliding name; the box says what it will be saved
// as before Enter (`uniqueName`, mirrored from the backend), and the row shows
// what the PATCH returned after.

import { useEffect, useRef, useState } from "react";
import { uniqueName } from "../lib/projects";

export function InlineRename(props: {
  value: string;
  taken: string[];
  testid: string;
  onSave: (name: string) => Promise<unknown>;
  onCancel: () => void;
}) {
  const [text, setText] = useState(props.value);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLInputElement | null>(null);
  const done = useRef(false);

  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);

  const finish = (save: boolean) => {
    if (done.current || busy) return;
    const name = text.trim();
    if (!save || !name || name === props.value) {
      done.current = true;
      props.onCancel();
      return;
    }
    setBusy(true);
    done.current = true;
    Promise.resolve(props.onSave(name)).finally(() => setBusy(false));
  };

  const savesAs = text.trim() && text.trim() !== props.value ? uniqueName(text, props.taken) : "";

  return (
    <span className="rename" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
      <input
        ref={ref}
        data-testid={props.testid}
        value={text}
        disabled={busy}
        aria-label="new name"
        onChange={(e) => setText(e.target.value)}
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter") {
            e.preventDefault();
            finish(true);
          } else if (e.key === "Escape") {
            e.preventDefault();
            finish(false);
          }
        }}
        onBlur={() => finish(false)}
      />
      {savesAs && savesAs !== text.trim() ? (
        <span className="savesas" data-testid={`${props.testid}-saves-as`}>→ {savesAs}</span>
      ) : null}
    </span>
  );
}
