import { useEffect, useState } from "react";
import { api } from "../api";
import type { Diff } from "../types";
import { churn, statusLabel, summaryLine } from "../lib/difflist";
import { DiffEditor } from "./Editor";

export function DiffTab(props: { taskId: string | null }) {
  const [diff, setDiff] = useState<Diff | null>(null);
  const [sel, setSel] = useState<string>("");
  const [sides, setSides] = useState<{ before: string; after: string } | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    setDiff(null);
    setSel("");
    setSides(null);
    setErr("");
    if (!props.taskId) return;
    api.diff(props.taskId).then(setDiff).catch((e) => setErr(e.message));
  }, [props.taskId]);

  useEffect(() => {
    if (!props.taskId || !sel) return;
    api
      .diffFile(props.taskId, sel)
      .then(setSides)
      .catch((e) => setErr(e.message));
  }, [props.taskId, sel]);

  if (!props.taskId) return <div className="pad muted">Pick a task to see its diff.</div>;
  if (err) return <div className="pad err" data-testid="diff-error">{err}</div>;
  if (!diff) return <div className="pad muted">…</div>;

  return (
    <div className="split">
      <div className="filetree" data-testid="difflist">
        <div className="pad small muted" data-testid="diff-summary">
          {summaryLine(diff.files, diff.truncated)}
        </div>
        {diff.files.map((f) => (
          <div
            key={f.path}
            className={"tree-row" + (sel === f.path ? " sel" : "")}
            data-testid={`diff-${f.path}`}
            onClick={() => setSel(f.path)}
          >
            <span className="tw">{f.status}</span>
            <span className="nm" title={statusLabel(f.status)}>{f.path}</span>
            <span className="badge">{churn(f)}</span>
          </div>
        ))}
      </div>
      <div className="editorwrap">
        <div className="toolbar">
          <span className="path">{sel || "no file selected"}</span>
          <span className="muted small">
            {diff.base} … {diff.head}
          </span>
        </div>
        {sel && sides ? (
          <DiffEditor path={sel} before={sides.before} after={sides.after} />
        ) : (
          <div className="pad muted">Pick a changed file.</div>
        )}
      </div>
    </div>
  );
}
