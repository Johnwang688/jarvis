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

import { useEffect, useRef, useState } from "react";
import {
  diffUri, editorsBlocked, fileUri, loadMonaco, onEditorsBlocked, type Monaco,
} from "../lib/monaco";

export function CodeEditor(props: {
  /** The project the file was read through: part of the model's URI. */
  projectId: string;
  path: string;
  value: string;
  readOnly?: boolean;
  onChange: (value: string) => void;
  /** Ctrl+S in this editor, with its text. */
  onSave?: (value: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<any>(null);
  const [failed, setFailed] = useState(false);
  const [language, setLanguage] = useState<string | null>(null);
  const [locked, setLocked] = useState(editorsBlocked());
  const onChange = useRef(props.onChange);
  onChange.current = props.onChange;
  const onSave = useRef(props.onSave);
  onSave.current = props.onSave;
  const readOnly = useRef(!!props.readOnly);
  readOnly.current = !!props.readOnly;

  useEffect(() => {
    let dead = false;
    let model: any = null;
    let unhold = () => {};
    let saveKey: { dispose(): void } | null = null;
    setLanguage(null);
    loadMonaco()
      .then((monaco: Monaco) => {
        if (dead || !host.current) return;
        model = monaco.editor.createModel(props.value, undefined, fileUri(monaco, props.projectId, props.path));
        const ed = monaco.editor.create(host.current, {
          model,
          theme: "jarvis",
          automaticLayout: true,
          minimap: { enabled: false },
          fontSize: 12,
          readOnly: readOnly.current || editorsBlocked(),
          scrollBeyondLastLine: false,
        });
        editor.current = ed;
        ed.onDidChangeModelContent(() => onChange.current(ed.getValue()));
        // Ctrl+S saves *this* editor. An action, never `addCommand`: in 0.57
        // `addCommand` is a global keybinding with no condition, so the last
        // editor created took Ctrl+S for every editor, and outlived itself —
        // a disposed editor's text is "", and its FileTab's `save` still
        // named that file (review of PR #43: Ctrl+S in a Diff wrote the file
        // blank). An action's keybinding holds only while this editor has
        // focus (Monaco scopes it by `editorId`), and is disposed with it.
        saveKey = ed.addAction({
          id: "jarvis.saveFile",
          label: "Save",
          keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
          run: () => {
            if (dead || !ed.getModel()) return;
            onSave.current?.(ed.getValue());
          },
        });
        unhold = onEditorsBlocked((b) => {
          ed.updateOptions({ readOnly: b || readOnly.current });
          setLocked(b);
        });
        setLanguage(model.getLanguageId());
      })
      .catch(() => setFailed(true));
    return () => {
      dead = true;
      unhold();
      saveKey?.dispose();
      editor.current?.dispose();
      editor.current = null;
      model?.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.projectId, props.path]);

  useEffect(() => {
    const ed = editor.current;
    if (ed && ed.getValue() !== props.value) ed.setValue(props.value);
  }, [props.value]);

  useEffect(() => {
    editor.current?.updateOptions({ readOnly: !!props.readOnly || editorsBlocked() });
  }, [props.readOnly]);

  if (failed) {
    return (
      <textarea
        className="editorhost"
        data-testid="editor-fallback"
        value={props.value}
        readOnly={props.readOnly || locked}
        onKeyDown={(e) => e.stopPropagation()}
        onChange={(e) => props.onChange(e.target.value)}
      />
    );
  }
  return (
    <div
      className="editorhost"
      data-testid="editor"
      data-path={props.path}
      // Set once Monaco holds this file: what a test waits on, so it never
      // types into an editor that is going away or not there yet.
      data-language={language ?? undefined}
      data-readonly={String(!!props.readOnly || locked)}
      ref={host}
    />
  );
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
