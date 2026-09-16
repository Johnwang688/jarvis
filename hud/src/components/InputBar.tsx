// Typed text, attachments and the dictation mode control.
//
// v1's staging contract holds: whatever is staged when a turn goes out — typed
// *or spoken* — rides that turn. `@path` in the text attaches a server-side
// file, which the backend resolves with the v1 `_assemble_turn` rules.
//
// Space typed in the box must never trigger push-to-talk, so the textarea
// stops that key from reaching the document handler.

import { useEffect, useRef, useState } from "react";
import type { Attachment } from "../types";
import { DICTATION_MODES, HINTS, type DictationMode } from "../lib/dictation";

const MAX_FILES = 8;
const MAX_BYTES = 4 * 1024 * 1024;

async function toAttachment(file: File): Promise<Attachment | string> {
  if (file.size > MAX_BYTES) return `[${file.name} skipped: over 4MB]`;
  const buf = await file.arrayBuffer();
  let bin = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return { name: file.name, mime: file.type || "application/octet-stream", data_b64: btoa(bin) };
}

export function InputBar(props: {
  mode: DictationMode;
  level: number;
  hint: string;
  pendingTranscript: string;
  disabled?: boolean;
  onModeChange: (m: DictationMode) => void;
  onSend: (text: string, attachments: Attachment[]) => void;
  onTranscriptTaken: () => void;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const box = useRef<HTMLTextAreaElement>(null);

  // REVIEW dictation puts the transcript in the box and focuses it. Nothing is
  // ever sent on its own in this mode — that is the whole point of the mode.
  useEffect(() => {
    if (!props.pendingTranscript) return;
    setText((t) => (t ? `${t} ${props.pendingTranscript}` : props.pendingTranscript));
    box.current?.focus();
    props.onTranscriptTaken();
  }, [props.pendingTranscript, props]);

  const stage = async (list: FileList | File[] | null) => {
    if (!list) return;
    const incoming = Array.from(list);
    const msgs: string[] = [];
    const next = [...files];
    for (const f of incoming) {
      if (next.length >= MAX_FILES) {
        msgs.push(`[${f.name} skipped: 8 files per turn]`);
        continue;
      }
      const a = await toAttachment(f);
      // Every refusal becomes a visible note, never a silent drop.
      if (typeof a === "string") msgs.push(a);
      else next.push(a);
    }
    setFiles(next);
    setNotes(msgs);
  };

  const send = () => {
    const body = text.trim();
    if (!body && files.length === 0) return;
    props.onSend(body, files);
    setText("");
    setFiles([]);
    setNotes([]);
  };

  return (
    <div
      id="inputbar"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        void stage(e.dataTransfer?.files || null);
      }}
    >
      {files.length || notes.length ? (
        <div id="chips" data-testid="chips">
          {files.map((f, i) => (
            <span className="chip" key={f.name + i}>
              {f.name}
              <b onClick={() => setFiles(files.filter((_, j) => j !== i))}>×</b>
            </span>
          ))}
          {notes.map((n, i) => (
            <span className="chip muted" key={"n" + i}>{n}</span>
          ))}
        </div>
      ) : null}
      <div id="inputrow">
        <textarea
          ref={box}
          id="input"
          data-testid="input"
          value={text}
          placeholder="Message, or @path to attach"
          disabled={props.disabled}
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => {
            const fl = Array.from(e.clipboardData?.files || []);
            if (fl.length) {
              e.preventDefault();
              void stage(fl);
            }
          }}
          onKeyDown={(e) => {
            // Space in the box is a space, never push-to-talk.
            e.stopPropagation();
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          onKeyUp={(e) => e.stopPropagation()}
        />
        <button type="button" data-testid="send" onClick={send} disabled={props.disabled}>
          Send
        </button>
        <label className="chip" style={{ cursor: "pointer" }}>
          Attach
          <input
            type="file"
            multiple
            data-testid="filepicker"
            style={{ display: "none" }}
            onChange={(e) => void stage(e.target.files)}
          />
        </label>
      </div>
      <div className="row">
        <div id="dictation" data-testid="dictation">
          {DICTATION_MODES.map((m) => (
            <button
              type="button"
              key={m}
              data-testid={`dictation-${m}`}
              className={(props.mode === m ? "on " : "") + (m === "off" ? "off" : "")}
              aria-pressed={props.mode === m}
              onClick={() => props.onModeChange(m)}
            >
              {m}
            </button>
          ))}
        </div>
        <div id="level" data-testid="level" data-level={props.level.toFixed(3)}>
          <i style={{ width: `${Math.min(100, props.level * 100)}%` }} />
        </div>
        <span className="hint" data-testid="hint">
          {props.hint || HINTS[props.mode]}
        </span>
      </div>
    </div>
  );
}
