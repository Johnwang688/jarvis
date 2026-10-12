// Monaco hosts. Both fall back to a plain <textarea>/<pre> if the editor
// cannot load: a File tab that shows nothing is worse than one that shows the
// text without syntax colour, and the save path must stay reachable.
//
// ED-1 (editor plan §2.2):
//  - a model's language comes from Monaco's registry, by its URI (extension,
//    file name, shebang), never from a table of ours;
//  - a diff view's two models live under `jarvis-diff:` and are disposed with
//    it (they used to leak two per file viewed);
//  - under an approval card every editor is read-only (`setEditorsBlocked`,
//    from App). Focus is the card's own doing: it takes focus off whatever
//    had it and makes everything behind it inert (Approvals.tsx), so Escape
//    is the card's and nothing typed lands in a buffer behind it;
//  - Ctrl+S in an editor saves it (the window's capture handler keeps
//    Chrome's "Save page as" from ever opening). Under a card no editor has
//    focus, so no editor command can run.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  diffUri, editorsBlocked, loadMonaco, onEditorsBlocked, type Monaco,
} from "../lib/monaco";
import type { TextModel } from "../state/buffers";

/**
 * Every File pane's live editor (ED-7): the palette lists the actions of the
 * one that had focus when it opened, and runs them there.
 */
const live = new Set<any>();
let lastFocused: any = null;

/** The File pane editor that has, or last had, keyboard focus and is still alive. */
export function focusedEditor(): any | null {
  for (const ed of live) if (ed.hasTextFocus?.()) return ed;
  return lastFocused && live.has(lastFocused) ? lastFocused : null;
}

/** A Monaco model, as opposed to the fallback's shim. */
const isMonacoModel = (m: TextModel | null): boolean => !!m && typeof m.pushEditOperations === "function";

/**
 * A File pane's one editor (ED-2): created once, its model swapped as the
 * pane's tabs change. The models belong to the buffer registry
 * (`state/buffers.ts`) — two panes on one file share one, so the text, the
 * dirty dot and the undo stack are shared — and each pane keeps its own view
 * state (cursor, selection, scroll, folds) per buffer, in memory.
 *
 * It reads the text only on Ctrl+S (through `onSave`): nothing here copies
 * the buffer per keystroke (#43's L3).
 */
export function PaneEditor(props: {
  /** The active buffer's model; null shows nothing (no file, or withheld). */
  model: TextModel | null;
  /** Which buffer it is (its absolute path): the key its view state is kept under. */
  viewKey: string | null;
  path: string;
  readOnly: boolean;
  /** Ctrl+S in this editor. */
  onSave: () => void;
  /** Kept mounted but not drawn (markdown read view, compare). */
  hidden?: boolean;
  /** The live editor, for the selection ("Add to chat"); null once it is gone. */
  editorRef?: (ed: any | null) => void;
  /** "Add Selection to Chat" in the editor's own context menu (ED-7); off for an env file. */
  mayAddToChat?: boolean;
  onAddToChat?: () => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<any>(null);
  const shown = useRef<{ key: string | null; model: TextModel | null }>({ key: null, model: null });
  const views = useRef(new Map<string, any>());
  const [failed, setFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const [language, setLanguage] = useState<string | null>(null);
  const [locked, setLocked] = useState(editorsBlocked());
  const [, setTick] = useState(0);
  const onSave = useRef(props.onSave);
  onSave.current = props.onSave;
  const readOnly = useRef(props.readOnly);
  readOnly.current = props.readOnly;
  const editorRef = useRef(props.editorRef);
  editorRef.current = props.editorRef;
  const onAddToChat = useRef(props.onAddToChat);
  onAddToChat.current = props.onAddToChat;
  const mayAdd = useRef<{ set(v: boolean): void } | null>(null);
  const plain = !!props.model && !isMonacoModel(props.model);

  // The editor, once Monaco is here; never rebuilt for a new model.
  useEffect(() => {
    let dead = false;
    let unhold = () => {};
    let saveKey: { dispose(): void } | null = null;
    let addKey: { dispose(): void } | null = null;
    let focusSub: { dispose(): void } | null = null;
    loadMonaco()
      .then((monaco: Monaco) => {
        if (dead || !host.current) return;
        const ed = monaco.editor.create(host.current, {
          model: null,
          theme: "jarvis",
          automaticLayout: true,
          minimap: { enabled: false },
          fontSize: 12,
          readOnly: readOnly.current || editorsBlocked(),
          scrollBeyondLastLine: false,
          // Words from documents of the same language only: an env file's
          // model is `dotenv`, so its values are never offered elsewhere.
          wordBasedSuggestions: "matchingDocuments",
        });
        editor.current = ed;
        // Ctrl+S saves *this* editor's buffer (an action, scoped to this
        // editor's focus and disposed with it — never a global command:
        // #43's H1).
        saveKey = ed.addAction({
          id: "jarvis.saveFile",
          label: "Save",
          keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
          run: () => {
            if (dead || !ed.getModel()) return;
            onSave.current();
          },
        });
        // "Add Selection to Chat" in the context menu (ED-7): shown only while
        // the buffer is one Jarvis may read (`jarvisMayRead`; never an env
        // file), and run through the pane's own Add to chat, which asks again.
        const may = ed.createContextKey("jarvisMayAddToChat", false);
        mayAdd.current = may;
        addKey = ed.addAction({
          id: "jarvis.addSelectionToChat",
          label: "Add Selection to Chat",
          precondition: "jarvisMayAddToChat",
          contextMenuGroupId: "9_cutcopypaste",
          contextMenuOrder: 9,
          run: () => {
            if (dead || editorsBlocked()) return;
            onAddToChat.current?.();
          },
        });
        live.add(ed);
        focusSub = ed.onDidFocusEditorText(() => {
          lastFocused = ed;
        });
        unhold = onEditorsBlocked((b) => {
          ed.updateOptions({ readOnly: b || readOnly.current });
          setLocked(b);
        });
        editorRef.current?.(ed);
        setReady(true);
      })
      .catch(() => setFailed(true));
    return () => {
      dead = true;
      unhold();
      saveKey?.dispose();
      addKey?.dispose();
      focusSub?.dispose();
      mayAdd.current = null;
      if (editor.current) {
        live.delete(editor.current);
        if (lastFocused === editor.current) lastFocused = null;
      }
      editorRef.current?.(null);
      // The editor only: the models are the registry's.
      editor.current?.setModel(null);
      editor.current?.dispose();
      editor.current = null;
    };
  }, []);

  // Swap the model, keeping each buffer's view state for this pane.
  useLayoutEffect(() => {
    const ed = editor.current;
    if (!ed) return;
    const next = isMonacoModel(props.model) && !props.model!.isDisposed() ? props.model : null;
    const was = shown.current;
    if (was.model === next && was.key === props.viewKey) return;
    if (was.key && was.model && !was.model.isDisposed()) views.current.set(was.key, ed.saveViewState());
    ed.setModel(next);
    if (next && props.viewKey) {
      const state = views.current.get(props.viewKey);
      if (state) ed.restoreViewState(state);
    }
    shown.current = { key: props.viewKey, model: next };
    setLanguage(next?.getLanguageId?.() ?? null);
  }, [props.model, props.viewKey, ready]);

  useEffect(() => {
    editor.current?.updateOptions({ readOnly: props.readOnly || editorsBlocked() });
  }, [props.readOnly, ready]);

  useEffect(() => {
    mayAdd.current?.set(!!props.mayAddToChat);
  }, [props.mayAddToChat, ready]);

  // The fallback textarea redraws when its shim model changes.
  useEffect(() => {
    if (!plain || !props.model) return;
    const sub = props.model.onDidChangeContent(() => setTick((t) => t + 1));
    return () => sub.dispose();
  }, [plain, props.model]);

  if (failed || plain) {
    if (!props.model) return <div className="editorhost" data-testid="editor-fallback-empty" />;
    return (
      <textarea
        className="editorhost"
        data-testid="editor-fallback"
        style={props.hidden ? { display: "none" } : undefined}
        value={props.model.getValue()}
        readOnly={props.readOnly || locked}
        onKeyDown={(e) => {
          e.stopPropagation();
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
            e.preventDefault();
            props.onSave();
          }
        }}
        onChange={(e) => props.model?.setValue?.(e.target.value)}
      />
    );
  }
  return (
    <div
      className="editorhost"
      data-testid="editor"
      data-path={props.path}
      // Set once Monaco shows this buffer: what a test waits on, so it never
      // types into an editor that is not showing the file yet.
      data-language={language ?? undefined}
      data-readonly={String(props.readOnly || locked)}
      style={props.hidden ? { display: "none" } : undefined}
      ref={host}
    />
  );
}

/**
 * Compare (ED-2's conflict bar): the disk's text on the left, read-only, and
 * the buffer itself on the right — its own model, so what is merged here is
 * the edit that Keep mine saves.
 */
export function CompareEditor(props: { model: TextModel; disk: string; path: string; readOnly: boolean; onSave: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  const [locked, setLocked] = useState(editorsBlocked());
  const onSave = useRef(props.onSave);
  onSave.current = props.onSave;
  const readOnly = useRef(props.readOnly);
  readOnly.current = props.readOnly;

  useEffect(() => {
    if (!isMonacoModel(props.model)) {
      setFailed(true);
      return;
    }
    let dead = false;
    let ed: any = null;
    let original: any = null;
    let unhold = () => {};
    let saveKey: { dispose(): void } | null = null;
    loadMonaco()
      .then((monaco: Monaco) => {
        if (dead || !host.current) return;
        ed = monaco.editor.createDiffEditor(host.current, {
          theme: "jarvis",
          automaticLayout: true,
          readOnly: readOnly.current || editorsBlocked(),
          originalEditable: false,
          renderSideBySide: true,
          // Side by side even in a narrow pane: the disk's text on the left,
          // the buffer on the right, never folded into one inline view.
          useInlineViewWhenSpaceIsLimited: false,
          fontSize: 12,
          minimap: { enabled: false },
        });
        original = monaco.editor.createModel(
          props.disk, props.model.getLanguageId?.(), monaco.Uri.from({ scheme: "jarvis-disk", path: `/${props.path}`, query: `disk=${Date.now()}` }),
        );
        ed.setModel({ original, modified: props.model });
        const mod = ed.getModifiedEditor();
        mod.updateOptions({ wordBasedSuggestions: "matchingDocuments" });
        saveKey = mod.addAction({
          id: "jarvis.saveFile",
          label: "Save",
          keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
          run: () => {
            if (!dead) onSave.current();
          },
        });
        unhold = onEditorsBlocked((b) => {
          ed.updateOptions({ readOnly: b || readOnly.current });
          setLocked(b);
        });
      })
      .catch(() => setFailed(true));
    return () => {
      dead = true;
      unhold();
      saveKey?.dispose();
      // The diff editor and the disk's side; the buffer's model stays.
      ed?.setModel(null);
      ed?.dispose();
      original?.dispose();
    };
  }, [props.model, props.disk, props.path]);

  if (failed) {
    return (
      <div className="mdsplit" data-testid="editor-compare-fallback">
        <pre className="editorhost" style={{ overflow: "auto", padding: 8 }}>{props.disk}</pre>
        <pre className="editorhost" style={{ overflow: "auto", padding: 8 }}>{props.model.getValue()}</pre>
      </div>
    );
  }
  return <div className="editorhost" data-testid="editor-compare" data-readonly={String(props.readOnly || locked)} ref={host} />;
}

export function DiffEditor(props: { path: string; before: string; after: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let dead = false;
    let ed: any = null;
    let original: any = null;
    let modified: any = null;
    loadMonaco()
      .then((monaco: Monaco) => {
        if (dead || !host.current) return;
        ed = monaco.editor.createDiffEditor(host.current, {
          theme: "jarvis",
          automaticLayout: true,
          readOnly: true,
          renderSideBySide: true,
          fontSize: 12,
          minimap: { enabled: false },
        });
        original = monaco.editor.createModel(props.before, undefined, diffUri(monaco, props.path, "original"));
        modified = monaco.editor.createModel(props.after, undefined, diffUri(monaco, props.path, "modified"));
        ed.setModel({ original, modified });
      })
      .catch(() => setFailed(true));
    return () => {
      dead = true;
      // The editor first, so nothing still shows the models it lets go of.
      ed?.dispose();
      original?.dispose();
      modified?.dispose();
    };
  }, [props.path, props.before, props.after]);

  if (failed) {
    return (
      <pre className="editorhost" data-testid="diff-fallback" style={{ overflow: "auto", padding: 8 }}>
        {props.after}
      </pre>
    );
  }
  return <div className="editorhost" data-testid="diff-editor" ref={host} />;
}
