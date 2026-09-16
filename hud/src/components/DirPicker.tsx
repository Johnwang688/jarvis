// A directory picker over `GET /fs/dirs`. Names only — the route lists
// directories and never file contents, and this component asks for nothing
// else, so a picker cannot become a way to read the disk.
//
// The roots are the backend's (`$HOME` or `/mnt/<drive>/`) and it answers 403
// outside them. The window does **not** re-implement that rule: it shows the
// refusal. Two copies of "which paths are allowed" is the shape that drifts,
// and the copy in the window would be the one that is wrong.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { DirListing } from "../types";

function crumbs(path: string): { label: string; path: string }[] {
  const parts = (path || "/").split("/").filter(Boolean);
  const out = [{ label: "/", path: "/" }];
  let acc = "";
  for (const p of parts) {
    acc += "/" + p;
    out.push({ label: p, path: acc });
  }
  return out;
}

export function DirPicker(props: {
  /** Where to open. A path that 403s or 404s still opens the picker, on an error. */
  start?: string;
  title: string;
  confirm: string;
  onChoose: (path: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState(props.start || "");
  const [typed, setTyped] = useState(props.start || "");
  const [listing, setListing] = useState<DirListing | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback((p: string) => {
    setBusy(true);
    api
      .dirs(p)
      .then((l) => {
        setListing(l);
        setPath(l.path);
        setTyped(l.path);
        setError("");
      })
      // The backend's own words: "403" alone tells the owner nothing about
      // which folders they may pick.
      .catch((e) => setError(e.message || "could not list that folder"))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    load(props.start || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const keys = (e: React.KeyboardEvent) => {
    // Space in a text box is a space, never push-to-talk.
    e.stopPropagation();
    if (e.key === "Escape") props.onClose();
    if (e.key === "Enter") load(typed.trim());
  };

  return (
    <div
      className="pickerveil"
      data-testid="dirpicker"
      style={{ zIndex: 95 }}
      onClick={(e) => {
        if (e.target === e.currentTarget) props.onClose();
      }}
    >
      <div className="picker" onKeyDown={(e) => { if (e.key === "Escape") props.onClose(); }}>
        <h3>{props.title}</h3>
        <div className="pad col" style={{ gap: 8 }}>
          <div className="row" data-testid="dir-crumbs" style={{ flexWrap: "wrap", gap: 2 }}>
            {crumbs(path).map((c) => (
              <button
                type="button"
                key={c.path}
                data-testid={`crumb-${c.path}`}
                style={{ padding: "2px 6px", textTransform: "none", letterSpacing: 0 }}
                onClick={() => load(c.path)}
              >
                {c.label}
              </button>
            ))}
          </div>
          <input
            data-testid="dir-typed"
            placeholder="type a path, Enter to go"
            value={typed}
            onKeyDown={keys}
            onChange={(e) => setTyped(e.target.value)}
          />
          {error ? <div className="err" data-testid="dir-error">{error}</div> : null}
        </div>
        <div className="rows" data-testid="dir-list">
          {listing?.parent ? (
            <div className="prow" data-testid="dir-up" onClick={() => load(listing.parent!)}>
              <span className="tw">↑</span>
              <span>..</span>
            </div>
          ) : null}
          {(listing?.dirs || []).map((d) => {
            const child = (path.endsWith("/") ? path : path + "/") + d;
            return (
              <div
                className="prow"
                key={d}
                data-testid={`dir-${d}`}
                title="double-click to open"
                onDoubleClick={() => load(child)}
              >
                <span className="tw">▸</span>
                <span>{d}</span>
              </div>
            );
          })}
          {listing && !listing.dirs.length ? (
            <div className="pad muted small">no folders here</div>
          ) : null}
          {busy && !listing ? <div className="pad muted small">…</div> : null}
        </div>
        <div className="foot">
          <button
            type="button"
            data-testid="dir-choose"
            disabled={!path}
            onClick={() => props.onChoose(path)}
          >
            {props.confirm}
          </button>
          <button type="button" data-testid="dir-cancel" onClick={props.onClose}>
            Cancel
          </button>
          <span className="muted small" style={{ marginLeft: "auto", alignSelf: "center" }}>
            {path || "—"}
          </span>
        </div>
      </div>
    </div>
  );
}
