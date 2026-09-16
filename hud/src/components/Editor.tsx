// Monaco hosts. Both fall back to a plain <textarea>/<pre> if the editor
// cannot load: a File tab that shows nothing is worse than one that shows the
// text without syntax colour, and the save path must stay reachable.

import { useEffect, useRef, useState } from "react";
import { languageFor, loadMonaco } from "../lib/monaco";

export function CodeEditor(props: {
  path: string;
  value: string;
  readOnly?: boolean;
  onChange: (value: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<any>(null);
  const [failed, setFailed] = useState(false);
  const onChange = useRef(props.onChange);
  onChange.current = props.onChange;

  useEffect(() => {
    let dead = false;
    loadMonaco()
      .then((monaco) => {
        if (dead || !host.current) return;
        editor.current = monaco.editor.create(host.current, {
          value: props.value,
          language: languageFor(props.path),
          theme: "jarvis",
          automaticLayout: true,
          minimap: { enabled: false },
          fontSize: 12,
          readOnly: !!props.readOnly,
          scrollBeyondLastLine: false,
        });
        editor.current.onDidChangeModelContent(() => onChange.current(editor.current.getValue()));
      })
      .catch(() => setFailed(true));
    return () => {
      dead = true;
      editor.current?.dispose();
      editor.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.path]);

  useEffect(() => {
    const ed = editor.current;
    if (ed && ed.getValue() !== props.value) ed.setValue(props.value);
  }, [props.value]);

  if (failed) {
    return (
      <textarea
        className="editorhost"
        data-testid="editor-fallback"
        value={props.value}
        readOnly={props.readOnly}
        onKeyDown={(e) => e.stopPropagation()}
        onChange={(e) => props.onChange(e.target.value)}
      />
    );
  }
  return <div className="editorhost" data-testid="editor" ref={host} />;
}

export function DiffEditor(props: { path: string; before: string; after: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let dead = false;
    let ed: any = null;
    loadMonaco()
      .then((monaco) => {
        if (dead || !host.current) return;
        ed = monaco.editor.createDiffEditor(host.current, {
          theme: "jarvis",
          automaticLayout: true,
          readOnly: true,
          renderSideBySide: true,
          fontSize: 12,
          minimap: { enabled: false },
        });
        const lang = languageFor(props.path);
        ed.setModel({
          original: monaco.editor.createModel(props.before, lang),
          modified: monaco.editor.createModel(props.after, lang),
        });
      })
      .catch(() => setFailed(true));
    return () => {
      dead = true;
      ed?.dispose();
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
