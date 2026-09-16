// Usage, Schedules and Route — the three read-mostly panels.
//
// The usage rule that matters: **quota is filled only from a provider's own
// report and is never computed**, so an absent quota says "not reported"
// rather than being invented from the ledger. A number in a dashboard that
// looks measured but is not is how a wrong number gets quoted.

import { useState } from "react";
import type { Project, RouteView, Schedule, Usage } from "../types";

export function UsagePanel(props: { usage: Usage | null }) {
  if (!props.usage) return <div className="block muted" data-testid="usage">usage not loaded</div>;
  const rows = Object.entries(props.usage.providers || {});
  return (
    <div className="block" data-testid="usage">
      <h3>Usage</h3>
      {rows.length === 0 ? <div className="muted small">nothing reported</div> : null}
      {rows.map(([name, p]) => (
        <div key={name} style={{ marginBottom: 8 }} data-testid={`usage-${name}`}>
          <div className="kv">
            <span className="k">{name}</span>
            {/* The state is a label and is styled like one; the reason is a
                sentence the provider wrote, so it is left as prose — running it
                through the label's uppercase makes it read as a shout. */}
            <span className="v">
              <span className={`phase ${p.state === "available" ? "done" : "blocked"}`}>
                {p.state}
              </span>
              {p.reason ? ` · ${p.reason}` : ""}
            </span>
          </div>
          <div className="kv">
            <span className="k">today</span>
            <span className="v">
              {(p.today?.work_tokens ?? 0).toLocaleString()} tok · ${(p.today?.spend_usd ?? 0).toFixed(4)}
              {p.today?.equivalent_usd ? ` (equiv $${p.today.equivalent_usd.toFixed(4)})` : ""}
            </span>
          </div>
          <div className="kv">
            <span className="k">quota</span>
            <span className="v" data-testid={`quota-${name}`}>
              {p.quota?.windows?.length
                ? p.quota.windows
                    .map((w) => `${w.name} ${w.used_percent}% → ${w.resets_at}`)
                    .join(" · ")
                : "not reported"}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

export function SchedulesPanel(props: {
  schedules: Schedule[];
  projects: Project[];
  onCreate: (body: { project_id: string; brief: string; cron?: string; every_s?: number; enabled: boolean }) => void;
  onToggle: (id: string, enabled: boolean) => void;
  onRunNow: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const [brief, setBrief] = useState("");
  const [cron, setCron] = useState("");
  const [every, setEvery] = useState("");
  // The panel mounts before the project list has loaded, so a `useState`
  // seeded from it would hold "" for the life of the window and every create
  // would silently do nothing. The choice is the owner's *override*; the
  // default is read from the list as it stands now.
  const [chosen, setPid] = useState("");
  const pid = chosen || props.projects[0]?.id || "";

  return (
    <div className="block" data-testid="schedules">
      <h3>Schedules</h3>
      {props.schedules.map((s) => (
        <div key={s.id} style={{ marginBottom: 6 }} data-testid={`schedule-${s.id}`}>
          <div className="kv">
            <span className="k">{s.enabled ? "on" : "off"}</span>
            <span className="v">{s.brief.slice(0, 60)}</span>
          </div>
          <div className="kv">
            <span className="k">when</span>
            <span className="v">{s.cron || (s.every_s ? `every ${s.every_s}s` : "—")}</span>
          </div>
          <div className="kv">
            <span className="k">last / next</span>
            <span className="v">{(s.last_run_at || "never") + " / " + (s.next_run_at || "—")}</span>
          </div>
          <div className="row">
            <button type="button" data-testid={`schedule-toggle-${s.id}`} onClick={() => props.onToggle(s.id, !s.enabled)}>
              {s.enabled ? "Disable" : "Enable"}
            </button>
            <button type="button" data-testid={`schedule-run-${s.id}`} onClick={() => props.onRunNow(s.id)}>
              Run now
            </button>
            <button type="button" onClick={() => props.onDelete(s.id)}>Delete</button>
          </div>
        </div>
      ))}
      <div className="col" style={{ marginTop: 8 }}>
        <select data-testid="schedule-project" value={pid} onChange={(e) => setPid(e.target.value)}>
          {props.projects.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <input data-testid="schedule-brief" placeholder="brief" value={brief}
               onKeyDown={(e) => e.stopPropagation()} onChange={(e) => setBrief(e.target.value)} />
        <input data-testid="schedule-cron" placeholder="cron (5 fields, America/Chicago)" value={cron}
               onKeyDown={(e) => e.stopPropagation()} onChange={(e) => setCron(e.target.value)} />
        <input data-testid="schedule-every" placeholder="or every N seconds" value={every}
               onKeyDown={(e) => e.stopPropagation()} onChange={(e) => setEvery(e.target.value)} />
        <button
          type="button"
          data-testid="schedule-create"
          onClick={() => {
            if (!pid || !brief.trim()) return;
            const body: any = { project_id: pid, brief: brief.trim(), enabled: true };
            if (cron.trim()) body.cron = cron.trim();
            else if (every.trim()) body.every_s = Number(every.trim());
            props.onCreate(body);
            setBrief("");
            setCron("");
            setEvery("");
          }}
        >
          Create schedule
        </button>
      </div>
    </div>
  );
}

export function RoutePanel(props: { route: RouteView | null }) {
  if (!props.route) return <div className="block muted" data-testid="route">route not loaded</div>;
  const r = props.route;
  return (
    <div className="block" data-testid="route">
      <h3>Route</h3>
      <div className="kv">
        <span className="k">ledger</span>
        <span className="v">
          {Object.entries(r.states || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "—"}
        </span>
      </div>
      <table className="plain" data-testid="route-table">
        <tbody>
          {Object.entries(r.table?.chains || r.table || {}).map(([role, chain]) => (
            <tr key={role}>
              <th>{role}</th>
              <td>{Array.isArray(chain) ? chain.join(" → ") : String(chain)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3 style={{ marginTop: 10 }}>Last decisions</h3>
      {(r.decisions || []).slice(-10).reverse().map((d, i) => (
        <div className="step" key={i} data-testid="route-decision">
          {d.role} → {d.provider} ({d.reason})
        </div>
      ))}
      {(r.decisions || []).length === 0 ? <div className="muted small">none yet</div> : null}
    </div>
  );
}
