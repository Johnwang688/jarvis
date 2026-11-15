// Settings -> Inbox (WS-4, docs/plans/2026-11-14-inbox-working-set-plan.md O-1, §9 Q1): how
// many idle days pass before a thread leaves the In progress list on its own.
// The daemon owns the value and its bounds (`/working/settings`); this shows
// the daemon's answer, never a guess. A page that is not the owner's (403) or
// an older daemon (404) has no setting, and says so quietly.
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError } from "../api";
import { checkIdleDays, parseWorkingSettings, type WorkingSettings } from "../lib/working";
import { working } from "../state/working";

type Load = "loading" | "ready" | "unavailable" | "error";

export function InboxSettings({ blocked = false, changes = working.onRead }: {
  blocked?: boolean;
  /** Told when a `working_changed` read lands; defaults to the working store's `onRead`. */
  changes?: (fn: () => void) => () => void;
}) {
  const [load, setLoad] = useState<Load>("loading");
  const [loadError, setLoadError] = useState("");
  const [server, setServer] = useState<WorkingSettings | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ kind: "saved" | "refused" | "failed"; text: string } | null>(null);
  const [typed, setTyped] = useState(false);
  const live = useRef(true);
  // What the box last matched on the daemon: the box is the owner's while it differs.
  const synced = useRef("");
  const draftNow = useRef("");
  draftNow.current = draft;
  const reading = useRef(0);

  const read = useCallback(async () => {
    const mine = ++reading.current;
    try {
      const next = parseWorkingSettings(await api.workingSettings());
      if (!live.current || mine !== reading.current) return;
      if (!next) { setLoad("error"); setLoadError("The daemon sent a setting this window cannot read."); return; }
      setServer(next); setLoad("ready"); setLoadError("");
      // A refresh never overwrites what the owner is typing.
      if (draftNow.current === synced.current) setDraft(String(next.idle_days));
      synced.current = String(next.idle_days);
    } catch (e) {
      if (!live.current || mine !== reading.current) return;
      if (e instanceof ApiError && (e.status === 403 || e.status === 404)) { setServer(null); setLoad("unavailable"); return; }
      setLoad((was) => (was === "ready" ? was : "error"));
      setLoadError(e instanceof Error && e.message ? e.message : "The daemon could not be reached.");
    }
  }, []);

  useEffect(() => { live.current = true; void read(); return () => { live.current = false; }; }, [read]);
  useEffect(() => changes(() => { void read(); }), [changes, read]);

  const checked = server ? checkIdleDays(draft, server.min, server.max) : null;
  const invalid = typed && checked && "error" in checked ? checked.error : "";

  const save = async (e?: FormEvent) => {
    e?.preventDefault();
    if (blocked || busy || !server || !checked) return;
    setTyped(true);
    if ("error" in checked) { setResult(null); return; }
    setBusy(true); setResult(null);
    try {
      const next = parseWorkingSettings(await api.setWorkingSettings(checked.value));
      if (!live.current) return;
      if (!next) {
        setResult({ kind: "failed", text: "The daemon's answer could not be read; the limit may not have been saved." });
        void read();
        return;
      }
      setServer(next); setDraft(String(next.idle_days)); synced.current = String(next.idle_days); setTyped(false);
      setResult({ kind: "saved", text: `Saved: ${next.idle_days} ${next.idle_days === 1 ? "day" : "days"}.` });
    } catch (err) {
      if (!live.current) return;
      const text = err instanceof Error && err.message ? err.message : "The limit could not be saved.";
      setResult(err instanceof ApiError && err.status === 400
        ? { kind: "refused", text } : { kind: "failed", text: `Not saved: ${text}` });
    } finally { if (live.current) setBusy(false); }
  };

  const off = blocked || busy || load !== "ready";
  const dirty = server !== null && draft.trim() !== String(server.idle_days);
  return (
    <section className="inbox-settings" data-testid="inbox-settings" aria-label="In progress">
      <h3>In progress</h3>
      {load === "loading" ? <p className="muted" data-testid="inbox-settings-loading">Reading the setting…</p> : null}
      {load === "unavailable" ? (
        <p className="muted" role="status" data-testid="inbox-settings-unavailable">
          Not available: only the owner's HUD, on a daemon that keeps the In progress list, has this setting.
        </p>
      ) : null}
      {load === "error" ? (
        <p className="err" role="status" data-testid="inbox-settings-error">
          The setting could not be read: {loadError}{" "}
          <button type="button" data-testid="inbox-settings-retry" onClick={() => { void read(); }}>Retry</button>
        </p>
      ) : null}
      {server && load === "ready" ? (
        <form onSubmit={save} noValidate data-testid="inbox-settings-form">
          <div className="inbox-idle-row">
            <label htmlFor="inbox-idle-days">Leave In progress after</label>
            <input id="inbox-idle-days" data-testid="inbox-idle-days" type="text" inputMode="numeric" autoComplete="off"
              value={draft} disabled={off} aria-invalid={invalid ? true : undefined}
              aria-describedby={`inbox-idle-help${invalid || result ? " inbox-idle-result" : ""}`}
              onChange={(e) => { setDraft(e.target.value); setTyped(true); setResult(null); }} />
            <span aria-hidden="true">idle days ({server.min}–{server.max}, default {server.default})</span>
            <button type="submit" data-testid="inbox-idle-save" disabled={off || !dirty}>{busy ? "Saving…" : "Save"}</button>
          </div>
          <p id="inbox-idle-result" role="status" aria-live="polite" data-testid="inbox-idle-result"
            className={result?.kind === "saved" ? "inbox-saved" : invalid || result ? "err" : undefined}>
            {invalid || result?.text || ""}
          </p>
        </form>
      ) : null}
      <p id="inbox-idle-help" className="inbox-help" data-testid="inbox-idle-help">
        A thread also leaves when you press Done, and a thread waiting on you (a pending approval or question)
        never leaves on its own.
      </p>
    </section>
  );
}
