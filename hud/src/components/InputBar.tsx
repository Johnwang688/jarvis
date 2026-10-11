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

/** The box grows with what is typed, up to this many lines, then scrolls. */
export const MAX_LINES = 15;
/** …and never past this share of its pane's height: in a short pane (a split
 * of rows, a small window at a high zoom) fifteen lines pushed Send and the
 * chips out of the pane and squeezed the conversation to nothing (review of
 * PR #29). */
export const MAX_PANE_SHARE = 0.35;
/** …and never so tall that the conversation above it gets less than this
 * (zoomed px) or this share of the pane, whichever is more: at the pane's
 * minimum, with the bottom panel open, 35% still left the log 36px. */
export const MIN_LOG_PX = 96;
export const MIN_LOG_SHARE = 0.3;
/** But the box itself never stops short of this many lines: a pane that
 * cannot fit the conversation's minimum gets a two-line box that scrolls. */
export const MIN_LINES = 2;

const MAX_BYTES = 4 * 1024 * 1024;

export async function toAttachment(file: File): Promise<Attachment | string> {
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

/** Steer: the arrow bends into the turn already running (Send's look is "start
 * one"; a steer must not look like it). */
function SteerIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor"
         strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 19v-5a6 6 0 0 1 6-6h8" />
      <path d="m15 4 4 4-4 4" />
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
  /** A status line under the box (`pane-status`), or null for none. The mic
   * and its words live on the orb (PR #29): the selected chat's status rides
   * the orb's line, so its bar draws none — unless the sidebar is folded and
   * the orb with it, when this is the only place left to say it. Every other
   * chat pane says here what its own turn is doing (WP-B). */
  status?: string | null;
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

  // The box flexes up with what is typed, and scrolls past the least of:
  // MAX_LINES lines, MAX_PANE_SHARE of its pane, and what the pane has left
  // once its fixed parts (the header, the ticker, the rest of this bar) and
  // the conversation's minimum (MIN_LOG_PX or MIN_LOG_SHARE of the pane) are
  // counted — but never less than MIN_LINES lines. Re-measured when the text
  // changes, when this bar's own parts do (staged files, notes, a status
  // line), when the box's width does (a pane resize rewraps the same text)
  // and when its pane's height does (a split, a fold, the zoom, the bottom
  // panel) — never on the box's own height change, which it set.
  const fit = useRef<() => void>(() => {});
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const pane = el.closest<HTMLElement>(".wpane");
    fit.current = () => {
      const cs = getComputedStyle(el);
      const line = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.45;
      const edge = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
      const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
      const lines = (n: number) => line * n + pad + edge;
      let cap = lines(MAX_LINES);
      // A hidden pane measures 0: its box is refitted when the pane is drawn.
      const paneH = pane?.clientHeight ?? 0;
      if (pane && paneH > 0) {
        cap = Math.min(cap, paneH * MAX_PANE_SHARE);
        const log = pane.querySelector<HTMLElement>('[data-testid="log"]');
        if (log) {
          // The pane's fixed parts, measured with the box at one line, when
          // nothing overflows: everything but the conversation and the box.
          el.style.height = `${lines(1)}px`;
          const fixed = paneH - log.offsetHeight - el.offsetHeight;
          const minLog = Math.max(MIN_LOG_PX, paneH * MIN_LOG_SHARE);
          cap = Math.min(cap, paneH - fixed - minLog);
        }
      }
      cap = Math.max(lines(MIN_LINES), cap);
      el.style.height = "auto";
      const want = el.scrollHeight + edge;
      el.style.height = `${Math.min(want, cap)}px`;
      el.style.overflowY = want > cap ? "auto" : "hidden";
    };
    fit.current();
    if (typeof ResizeObserver === "undefined") return;
    let width = el.clientWidth;
    let height = pane?.clientHeight ?? 0;
    const ro = new ResizeObserver(() => {
      const w = el.clientWidth;
      const h = pane?.clientHeight ?? 0;
      if (w === width && h === height) return;
      width = w;
      height = h;
      fit.current();
    });
    ro.observe(el);
    if (pane) ro.observe(pane);
    return () => ro.disconnect();
  }, []);
  useLayoutEffect(() => fit.current(), [text, files.length, notes.length, props.imageNote, props.status != null]);

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
          className="input"
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
          // Steering looks like steering (main's button said "Steer"): an
          // outlined, bent arrow, not the filled "start a turn" one.
          className={"iconbtn send" + (props.running ? " steer" : "")}
          data-testid="send"
          data-steer={props.running ? "true" : undefined}
          aria-label={props.running ? "Steer" : "Send"}
          title={props.running ? "Steer the running turn (Enter)" : "Send (Enter)"}
          onClick={send}
          disabled={props.disabled}
        >
          {props.running ? <SteerIcon /> : <SendIcon />}
        </button>
      </div>
      <div className="row">
        {props.projectChip ? <Chip chip={props.projectChip} /> : null}
        {props.modelChip ?? null}
        {props.status != null ? (
          // This pane's own turn says what it is doing (WP-B); the selected
          // chat's says it on the orb, and here only while the orb is folded.
          <span className="hint" data-testid="pane-status" title={props.status || undefined}>
            {props.status}
          </span>
        ) : null}
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
