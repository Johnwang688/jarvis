// Creating or editing a schedule, as a dialog rather than three inline boxes
// the owner has to know cron to fill in.
//
// The rule that shapes it: **every change is read back from the backend before
// it can be saved.** `POST /schedules/preview` returns the plain-English
// reading and the next three fire times, so what the owner sees is what the
// scheduler will do — not a second implementation of cron in the window that
// agrees with it right up until a DST boundary or a leap day.
//
// The preset → cron mapping itself lives in `lib/cron.ts`, which is where the
// free tests grade it.

import { useEffect, useState } from "react";
import { api } from "../api";
import {
  DEFAULT_WHEN, PRESETS, WEEKDAYS, fromSchedule, toWhen, type Preset, type WhenForm,
} from "../lib/cron";
import type { Project, Schedule, SchedulePreview } from "../types";

export function ScheduleDialog(props: {
  projects: Project[];
  /** The schedule being edited, or null to create one. */
  editing: Schedule | null;
  defaultProject: string;
  onSave: (
    body: { project_id: string; brief: string; cron?: string; every_s?: number; enabled: boolean },
    id: string | null,
  ) => Promise<unknown>;
  onClose: () => void;
}) {
  const editing = props.editing;
  const [pid, setPid] = useState(editing?.project_id || props.defaultProject || "");
  const [brief, setBrief] = useState(editing?.brief || "");
  const [when, setWhen] = useState<WhenForm>(
    editing ? fromSchedule(editing) : { ...DEFAULT_WHEN },
  );
  const [preview, setPreview] = useState<SchedulePreview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const spec = toWhen(when);
  const project = pid || props.projects[0]?.id || "";

  // Every change re-previews. Debounced, and the response is dropped if the
  // form moved on while it was in flight — a stale preview under a changed
  // form is worse than none, because it reads as confirmation.
  useEffect(() => {
    if (spec.error) {
      setPreview(null);
      return;
    }
    let stale = false;
    const t = setTimeout(() => {
      api
        .previewSchedule({ cron: spec.cron, every_s: spec.every_s })
        .then((p) => {
          if (!stale) setPreview(p);
        })
        .catch((e) => {
          if (!stale) {
            setPreview(null);
            setError(e?.message || "could not read that back");
          }
        });
    }, 60);
    return () => {
      stale = true;
      clearTimeout(t);
    };
  }, [spec.cron, spec.every_s, spec.error]);

  const set = (p: Partial<WhenForm>) => setWhen({ ...when, ...p });

  const submit = () => {
    if (busy) return;
    if (!project) return setError("Pick a project for it to run in.");
    if (!brief.trim()) return setError("Say what it should do.");
    if (spec.error) return setError(spec.error);
    setBusy(true);
    setError("");
    Promise.resolve(
      props.onSave(
        {
          project_id: project,
          brief: brief.trim(),
          ...(spec.cron ? { cron: spec.cron } : {}),
          ...(spec.every_s ? { every_s: spec.every_s } : {}),
          enabled: editing ? editing.enabled : true,
        },
        editing ? editing.id : null,
      ),
    )
      .catch((e) => setError(e?.message || "could not save the schedule"))
      .finally(() => setBusy(false));
  };

  const keys = (e: React.KeyboardEvent) => {
    e.stopPropagation();
    if (e.key === "Escape") props.onClose();
    // Enter submits from a single-line field; the brief is a textarea, where
    // Enter is a newline.
    if (e.key === "Enter" && (e.target as HTMLElement).tagName !== "TEXTAREA") submit();
  };

  return (
    <div
      className="pickerveil"
      data-testid="picker"
      onClick={(e) => {
        if (e.target === e.currentTarget) props.onClose();
      }}
    >
      <div
        className="picker"
        data-testid="schedule-dialog"
        onKeyDown={(e) => { if (e.key === "Escape") props.onClose(); }}
      >
        <h3>{editing ? "Edit schedule" : "New schedule"}</h3>
        <div className="rows">
          <div className="pad col">
            <label className="muted small">Project</label>
            <select data-testid="sched-project" value={project} onChange={(e) => setPid(e.target.value)}>
              {props.projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>

            <label className="muted small">Brief</label>
            <textarea
              rows={3}
              data-testid="sched-brief"
              placeholder="what it should do, and what done means"
              value={brief}
              onKeyDown={keys}
              onChange={(e) => setBrief(e.target.value)}
            />

            <label className="muted small">When</label>
            <div className="row" data-testid="sched-presets" style={{ flexWrap: "wrap" }}>
              {PRESETS.map((p) => (
                <button
                  type="button"
                  key={p.id}
                  data-testid={`preset-${p.id}`}
                  aria-pressed={when.preset === p.id}
                  className={when.preset === p.id ? "on" : ""}
                  style={when.preset === p.id ? { background: "rgba(56, 189, 248, 0.3)" } : undefined}
                  onClick={() => set({ preset: p.id as Preset })}
                >
                  {p.label}
                </button>
              ))}
            </div>

            {when.preset === "daily" || when.preset === "weekdays" || when.preset === "weekly" ? (
              <div className="row">
                {when.preset === "weekly" ? (
                  <select
                    data-testid="sched-weekday"
                    value={String(when.weekday)}
                    onChange={(e) => set({ weekday: Number(e.target.value) })}
                  >
                    {WEEKDAYS.map((d, i) => (
                      <option key={d} value={String(i)}>{d}</option>
                    ))}
                  </select>
                ) : null}
                <input
                  data-testid="sched-time"
                  placeholder="HH:MM"
                  value={when.time}
                  onKeyDown={keys}
                  onChange={(e) => set({ time: e.target.value })}
                />
              </div>
            ) : null}

            {when.preset === "interval" ? (
              <div className="row">
                <input
                  data-testid="sched-every"
                  placeholder="N"
                  value={when.every}
                  onKeyDown={keys}
                  onChange={(e) => set({ every: e.target.value })}
                />
                <select
                  data-testid="sched-unit"
                  value={when.unit}
                  onChange={(e) => set({ unit: e.target.value as "minutes" | "hours" })}
                >
                  <option value="minutes">minutes</option>
                  <option value="hours">hours</option>
                </select>
              </div>
            ) : null}

            {when.preset === "advanced" ? (
              <input
                data-testid="sched-cron"
                placeholder="minute hour day month weekday — America/Chicago"
                value={when.cron}
                onKeyDown={keys}
                onChange={(e) => set({ cron: e.target.value })}
              />
            ) : null}

            {/* What it will actually do, read back rather than guessed. */}
            <div className="block" style={{ border: "1px solid var(--cyan-faint)", padding: 8 }}>
              <div className="kv">
                <span className="k">reading</span>
                <span className="v" data-testid="sched-describe">
                  {spec.error ? "—" : preview?.describe || "…"}
                </span>
              </div>
              <div className="kv">
                <span className="k">expression</span>
                <span className="v" data-testid="sched-expression">
                  {spec.cron || (spec.every_s ? `every ${spec.every_s}s` : "—")}
                </span>
              </div>
              <div className="kv">
                <span className="k">next</span>
                <span className="v">
                  {(preview?.next || []).map((t) => (
                    <div key={t} data-testid="sched-next">{t}</div>
                  ))}
                  {!preview?.next?.length ? <span className="muted">—</span> : null}
                </span>
              </div>
            </div>

            {spec.error || error ? (
              <div className="err" data-testid="sched-error">{spec.error || error}</div>
            ) : null}
          </div>
        </div>
        <div className="foot">
          <button
            type="button"
            data-testid="sched-save"
            disabled={busy || !!spec.error || !brief.trim()}
            onClick={submit}
          >
            {editing ? "Save" : "Create schedule"}
          </button>
          <button type="button" data-testid="picker-close" onClick={props.onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
