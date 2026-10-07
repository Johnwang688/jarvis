// Schedules, as one dialog: a list in the scheduler's own words, and a form
// that does not ask the owner to write cron.
//
// The rule that shapes the form: **every change is read back from the backend
// before it can be saved.** `POST /schedules/preview` returns the plain-English
// reading and the next three fire times, so what the owner sees is what the
// scheduler will do — not a second implementation of cron in the window that
// agrees with it right up until a DST boundary or a leap day.
//
// The preset → cron mapping itself lives in `lib/cron.ts`, which is where the
// free tests grade it. The list's sentence is `describe` from the backend,
// for the same reason.

import { useEffect, useState } from "react";
import { api } from "../api";
import {
  DEFAULT_WHEN, PRESETS, WEEKDAYS, fromSchedule, toWhen, type Preset, type WhenForm,
} from "../lib/cron";
import type { Project, Schedule, SchedulePreview } from "../types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** The digits already in the timestamp, so the label does not depend on the
 *  machine's timezone. */
function nextLabel(iso: string | null): string {
  if (!iso) return "no next run";
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return iso;
  return `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}, ${m[4]}:${m[5]}`;
}

function ScheduleForm(props: {
  projects: Project[];
  editing: Schedule | null;
  defaultProject: string;
  onSave: (
    body: { project_id: string; brief: string; cron?: string; every_s?: number; enabled: boolean },
    id: string | null,
  ) => Promise<unknown>;
  onBack: () => void;
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
    <>
      <div className="rows">
        <div className="pad col">
          <label className="muted small">Project</label>
          <select data-testid="sched-project" value={project} onChange={(e) => setPid(e.target.value)}>
            {props.projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>

          <label className="muted small">What should it do?</label>
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
                type="time"
                data-testid="sched-time"
                value={when.time}
                onKeyDown={keys}
                onChange={(e) => set({ time: e.target.value.slice(0, 5) })}
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
            <>
              <input
                data-testid="sched-cron"
                placeholder="minute hour day month weekday"
                value={when.cron}
                onKeyDown={keys}
                onChange={(e) => set({ cron: e.target.value })}
              />
              <div className="muted small" data-testid="sched-expression">
                {spec.cron || (spec.every_s ? `every ${spec.every_s}s` : "—")}
              </div>
            </>
          ) : null}

          <div className="sched-read" data-testid="sched-reading">
            <div className="sched-sentence" data-testid="sched-describe">
              {spec.error ? "—" : preview?.describe || "…"}
            </div>
            <div className="muted small">
              {(preview?.next || []).map((t) => (
                <div key={t} data-testid="sched-next">{nextLabel(t)}</div>
              ))}
              {!preview?.next?.length ? <span className="muted">—</span> : null}
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
        <button type="button" data-testid="sched-back" onClick={props.onBack}>Back</button>
        <button type="button" data-testid="picker-close" onClick={props.onClose}>Close</button>
      </div>
    </>
  );
}

export function ScheduleDialog(props: {
  projects: Project[];
  schedules: Schedule[];
  /** The schedule being edited, or null to open on the list. */
  editing: Schedule | null;
  defaultProject: string;
  onSave: (
    body: { project_id: string; brief: string; cron?: string; every_s?: number; enabled: boolean },
    id: string | null,
  ) => Promise<unknown>;
  onToggle: (id: string, enabled: boolean) => void;
  onRunNow: (id: string) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}) {
  const [view, setView] = useState<"list" | "form">(props.editing ? "form" : "list");
  const [draft, setDraft] = useState<Schedule | null>(props.editing);
  const named = (id: string) => props.projects.find((p) => p.id === id)?.name || id;

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
        <h3>{view === "form" ? (draft ? "Edit schedule" : "New schedule") : "Schedules"}</h3>
        {view === "list" ? (
          <>
            <div className="rows" data-testid="schedules">
              {props.schedules.length === 0 ? (
                <div className="pad muted small" data-testid="schedules-empty">Nothing scheduled.</div>
              ) : null}
              {props.schedules.map((s) => (
                <div key={s.id} className="sched-row" data-testid={`schedule-${s.id}`}>
                  <div className="sched-main">
                    <div>{s.brief}</div>
                    <div className="muted small">{s.describe || "—"} · {named(s.project_id)}</div>
                    <div className="muted small">Next {nextLabel(s.next_run_at)}</div>
                  </div>
                  <div className="sched-actions">
                    <button
                      type="button"
                      className="quiet"
                      data-testid={`schedule-toggle-${s.id}`}
                      onClick={() => props.onToggle(s.id, !s.enabled)}
                    >
                      {s.enabled ? "Pause" : "Resume"}
                    </button>
                    <button
                      type="button"
                      className="quiet"
                      data-testid={`schedule-edit-${s.id}`}
                      onClick={() => {
                        setDraft(s);
                        setView("form");
                      }}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="quiet"
                      data-testid={`schedule-run-${s.id}`}
                      onClick={() => props.onRunNow(s.id)}
                    >
                      Run
                    </button>
                    <button
                      type="button"
                      className="quiet"
                      data-testid={`schedule-delete-${s.id}`}
                      onClick={() => props.onDelete(s.id)}
                    >
                      Delete
                    </button>
                  </div>
                </div>
              ))}
            </div>
            <div className="foot">
              <button
                type="button"
                data-testid="schedule-new"
                onClick={() => {
                  setDraft(null);
                  setView("form");
                }}
              >
                New schedule
              </button>
              <button type="button" data-testid="picker-close" onClick={props.onClose}>Close</button>
            </div>
          </>
        ) : (
          <ScheduleForm
            key={draft?.id || "new"}
            projects={props.projects}
            editing={draft}
            defaultProject={props.defaultProject}
            onSave={props.onSave}
            onBack={() => {
              setDraft(null);
              setView("list");
            }}
            onClose={props.onClose}
          />
        )}
      </div>
    </div>
  );
}
