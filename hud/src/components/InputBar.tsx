// Typed text and attachments. (The dictation mode and the mic meter live on the
// orb: components/Orb.tsx.)
//
// v1's staging contract holds: whatever is staged when a turn goes out — typed
// *or spoken* — rides that turn. `@path` in the text attaches a server-side
// file, which the backend resolves with the v1 `_assemble_turn` rules.
//
// Space typed in the box must never trigger push-to-talk, so the textarea
// stops that key from reaching the document handler.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Attachment, Project } from "../types";
import { folderName } from "../lib/compose";
import { joined, pendingAfter, type GiveBack } from "../lib/giveback";

/**
 * `in: <project>`, where this conversation lives. Editable only while a new
 * thread is being composed, because that is the only time the choice is still
 * free. After the first message, moving is a sidebar action, and the chip
 * shows the folder the thread works in, which a move never changes.
 */
export interface ProjectChip {
  projects: Project[];
  value: string | null;
  editable: boolean;
  /** The folder an existing thread works in; absent while composing. */
  folder?: string | null;
  onChange: (projectId: string) => void;
  onNewProject: () => void;
}

/** The box grows with what is typed, up to this many lines, then scrolls. */
const MAX_LINES = 15;

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

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor"
         strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 19V5" />
      <path d="M5.5 11.5 12 5l6.5 6.5" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="currentColor">
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  );
}

function ClipIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="m21.4 11.1-8.5 8.5a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.8-7.8" />
    </svg>
  );
}

export function InputBar(props: {
  pendingTranscript: string;
  disabled?: boolean;
  placeholder?: string;
  projectChip?: ProjectChip | null;
  /** provider ▾ · model ▾ · effort ▾ (components/ModelChip), beside the project. */
  modelChip?: React.ReactNode;
  /** Shown while an image is staged, when the model cannot see one (A5). */
  imageNote?: string | null;
  onSend: (text: string, attachments: Attachment[]) => void;
  onTranscriptTaken: () => void;
  /** A turn is running in the open thread (2026-10-08): Enter steers it, and
   * a Stop button ends it. */
  running?: boolean;
  onStop?: () => void;
  /** Words and files handed back (a failed send, a dropped message): put back
   * in the box ahead of anything typed since, each exactly once. */
  restore?: GiveBack[];
  /** Every hand-back up to this nonce is in the box. */
  onRestoreTaken?: (nonce: number) => void;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const box = useRef<HTMLTextAreaElement>(null);

  // The box flexes up with what is typed, to MAX_LINES lines, and scrolls past
  // that. Re-measured when the text changes and when the box's width does (a
  // pane resize rewraps the same text), never on its own height change.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const fit = () => {
      el.style.height = "auto";
      const cs = getComputedStyle(el);
      const line = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.4;
      const edge = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
      const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
      const cap = line * MAX_LINES + pad + edge;
      const want = el.scrollHeight + edge;
      el.style.height = `${Math.min(want, cap)}px`;
      el.style.overflowY = want > cap ? "auto" : "hidden";
    };
    fit();
    if (typeof ResizeObserver === "undefined") return;
    let width = el.clientWidth;
    const ro = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      fit();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [text]);

  // REVIEW dictation puts the transcript in the box and focuses it. Nothing is
  // ever sent on its own in this mode — that is the whole point of the mode.
  useEffect(() => {
    if (!props.pendingTranscript) return;
    setText((t) => (t ? `${t} ${props.pendingTranscript}` : props.pendingTranscript));
    box.current?.focus();
    props.onTranscriptTaken();
  }, [props.pendingTranscript, props]);

  // A send that failed, or words the owner's Stop dropped: nothing typed is
  // ever lost, files included. Each hand-back is taken once, by its nonce: a
  // parent render while one is still pending must not prepend the same words
  // again (Bugbot on PR #22 — the effect used to rerun on every render).
  const restoreTaken = useRef(0);
  const restore = props.restore ?? [];
  const newest = restore.length ? restore[restore.length - 1].nonce : 0;
  const onRestoreTaken = props.onRestoreTaken;
  useEffect(() => {
    const fresh = pendingAfter(restore, restoreTaken.current);
    if (!fresh.length) return;
    restoreTaken.current = fresh[fresh.length - 1].nonce;
    const back = joined(fresh);
    if (back.text) setText((t) => (t ? `${back.text}\n${t}` : back.text));
    if (back.files.length) setFiles((f) => [...back.files, ...f].slice(0, MAX_FILES));
    box.current?.focus();
    onRestoreTaken?.(restoreTaken.current);
    // Keyed by the newest nonce alone: a new callback or props object is not
    // a new hand-back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newest]);

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

  const hasImage = files.some((f) => f.mime.startsWith("image/"));
  const imageNote = hasImage ? props.imageNote : null;

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
          {imageNote ? (
            <span className="chip warn" data-testid="image-note">{imageNote}</span>
          ) : null}
        </div>
      ) : null}
      <div id="inputrow">
        <label className="iconbtn attach" title="Attach files" aria-label="Attach files">
          <ClipIcon />
          <input
            type="file"
            multiple
            data-testid="filepicker"
            style={{ display: "none" }}
            onChange={(e) => void stage(e.target.files)}
          />
        </label>
        <textarea
          ref={box}
          id="input"
          data-testid="input"
          rows={1}
          value={text}
          placeholder={
            props.placeholder ||
            (props.running ? "Steer him while he works · Enter sends, Stop ends the turn" : "Message, or @path to attach")
          }
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
        {props.running ? (
          <button
            type="button"
            className="iconbtn stop"
            data-testid="stop"
            aria-label="Stop"
            title="Stop the running turn. Anything queued behind it comes back to this box."
            onClick={() => props.onStop?.()}
          >
            <StopIcon />
          </button>
        ) : null}
        <button
          type="button"
          className="iconbtn send"
          data-testid="send"
          aria-label={props.running ? "Steer" : "Send"}
          title={props.running ? "Steer the running turn (Enter)" : "Send (Enter)"}
          onClick={send}
          disabled={props.disabled}
        >
          <SendIcon />
        </button>
      </div>
      <div className="row">
        {props.projectChip ? <Chip chip={props.projectChip} /> : null}
        {props.modelChip ?? null}
      </div>
    </div>
  );
}

function Chip({ chip }: { chip: ProjectChip }) {
  const current = chip.projects.find((p) => p.id === chip.value);
  if (chip.editable && chip.projects.length === 0) {
    return (
      <span className="chip projchip" data-testid="project-chip">
        in:{" "}
        <button type="button" data-testid="project-chip-create" onClick={chip.onNewProject}>
          create a project
        </button>
      </span>
    );
  }
  if (chip.editable) {
    return (
      <span className="chip projchip" data-testid="project-chip" title="The project this new thread starts in">
        in:{" "}
        <select
          data-testid="project-chip-select"
          style={{ width: "auto" }}
          value={chip.value || ""}
          onChange={(e) => chip.onChange(e.target.value)}
          // Space on the select opens it; it must never reach push-to-talk.
          onKeyDown={(e) => e.stopPropagation()}
          onKeyUp={(e) => e.stopPropagation()}
        >
          {chip.projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </span>
    );
  }
  return (
    <span
      className="chip projchip ro"
      data-testid="project-chip"
      title={chip.folder ? `works in ${chip.folder}` : undefined}
    >
      in: {current?.name || "?"}
      {chip.folder ? <span className="muted"> · {folderName(chip.folder)}</span> : null}
    </span>
  );
}
