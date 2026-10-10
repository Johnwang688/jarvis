// Typed text, attachments and the dictation mode control.
//
// v1's staging contract holds: whatever is staged when a turn goes out — typed
// *or spoken* — rides that turn. `@path` in the text attaches a server-side
// file, which the backend resolves with the v1 `_assemble_turn` rules.
//
// Space typed in the box must never trigger push-to-talk, so the textarea
// stops that key from reaching the document handler.

import { useEffect, useRef, useState } from "react";
import type { Attachment, Project } from "../types";
import { DICTATION_MODES, HINTS, type DictationMode } from "../lib/dictation";
import { folderName } from "../lib/compose";
import { joined, pendingAfter, type GiveBack } from "../lib/giveback";
import { MAX_FILES } from "../lib/chats";

export { MAX_FILES };

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

const MAX_BYTES = 4 * 1024 * 1024;

export async function toAttachment(file: File): Promise<Attachment | string> {
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
  placeholder?: string;
  projectChip?: ProjectChip | null;
  /** provider ▾ · model ▾ · effort ▾ (components/ModelChip), beside the project. */
  modelChip?: React.ReactNode;
  /** Shown while an image is staged, when the model cannot see one (A5). */
  imageNote?: string | null;
  onModeChange: (m: DictationMode) => void;
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
  /** This pane is the selected chat (WP-B, decisions W-6): it alone draws the dictation strip —
   * AUTO / REVIEW / OFF, the level meter and the hint. False draws only this
   * pane's own turn status in its place. */
  strip?: boolean;
  /** The unsent words and staged files, when the caller keeps them (WP-B):
   * they belong to the conversation, so a pane that trades conversations, or
   * opens another thread, shows that conversation's draft (review of PR #27).
   * Absent, the box keeps its own. */
  text?: string;
  files?: Attachment[];
  onText?: (text: string) => void;
  onFiles?: (files: Attachment[]) => void;
  /** The conversation this box shows (lib/chats `draftKey`), and where files
   * read in for it go once they are ready (the store's `stage`): to that
   * conversation wherever it is by then, not to this box's next one. */
  stageKey?: string | null;
  onStage?: (key: string, files: Attachment[]) => void;
}) {
  const [ownText, setOwnText] = useState("");
  const [ownFiles, setOwnFiles] = useState<Attachment[]>([]);
  const textControlled = props.text !== undefined;
  const filesControlled = props.files !== undefined;
  const text = textControlled ? props.text! : ownText;
  const files = filesControlled ? props.files! : ownFiles;
  // Several changes can land before a render (a hand-back and a transcript at
  // once): each reads what the one before it wrote, not the last render's.
  const textNow = useRef(text);
  textNow.current = text;
  const filesNow = useRef(files);
  filesNow.current = files;
  const setText = (next: string | ((t: string) => string)) => {
    const value = typeof next === "function" ? next(textNow.current) : next;
    textNow.current = value;
    if (textControlled) props.onText?.(value);
    else setOwnText(value);
  };
  const setFiles = (next: Attachment[] | ((f: Attachment[]) => Attachment[])) => {
    const value = typeof next === "function" ? next(filesNow.current) : next;
    filesNow.current = value;
    if (filesControlled) props.onFiles?.(value);
    else setOwnFiles(value);
  };
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
    // The conversation they are for, as it is now: reading them in takes a
    // while, and the box may show another conversation by then (re-review of
    // PR #27). They are added to what is staged then, never over it.
    const key = props.stageKey ?? null;
    const read: Attachment[] = [];
    const room = MAX_FILES - filesNow.current.length;
    for (const f of incoming) {
      if (read.length >= room) {
        msgs.push(`[${f.name} skipped: 8 files per turn]`);
        continue;
      }
      const a = await toAttachment(f);
      // Every refusal becomes a visible note, never a silent drop.
      if (typeof a === "string") msgs.push(a);
      else read.push(a);
    }
    if (key !== null && props.onStage) props.onStage(key, read);
    else setFiles((cur) => [...cur, ...read].slice(0, MAX_FILES));
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
      className="inputbar"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        void stage(e.dataTransfer?.files || null);
      }}
    >
      {files.length || notes.length ? (
        <div className="chips" data-testid="chips">
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
      <div className="inputrow">
        <textarea
          ref={box}
          className="input"
          data-testid="input"
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
        <button type="button" data-testid="send" onClick={send} disabled={props.disabled}>
          {props.running ? "Steer" : "Send"}
        </button>
        {props.running ? (
          <button
            type="button"
            className="stop"
            data-testid="stop"
            title="Stop the running turn. Anything queued behind it comes back to this box."
            onClick={() => props.onStop?.()}
          >
            Stop
          </button>
        ) : null}
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
        {props.projectChip ? <Chip chip={props.projectChip} /> : null}
        {props.modelChip ?? null}
        {props.strip === false ? (
          // Not the selected chat: no dictation strip, so the strip itself shows
          // where speech lands. This pane's own turn still says what it is doing.
          <span className="hint" data-testid="pane-status">{props.hint}</span>
        ) : (
        <>
        <div className="dictation" data-testid="dictation">
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
        <div className="level" data-testid="level" data-level={props.level.toFixed(3)}>
          <i style={{ width: `${Math.min(100, props.level * 100)}%` }} />
        </div>
        <span className="hint" data-testid="hint">
          {props.hint || HINTS[props.mode]}
        </span>
        </>
        )}
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
