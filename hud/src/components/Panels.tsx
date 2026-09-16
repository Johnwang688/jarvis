// Usage, Schedules and Route — the three read-mostly panels.
//
// The usage rule that matters: **quota is filled only from a provider's own
// report and is never computed**, so an absent quota says "not reported"
// rather than being invented from the ledger. A number in a dashboard that
// looks measured but is not is how a wrong number gets quoted.
//
// Which is also why there are two kinds of bar and they are labelled apart:
// the provider's reported window, and our own local allowance. One bar that
// sometimes meant either would be exactly the number nobody could quote.

import type { Project, RouteView, Schedule, Usage } from "../types";
import { allowanceBar, barsFor, type Bar } from "../lib/quota";

function QuotaBar(props: { bar: Bar; testid: string; thin?: boolean }) {
  const b = props.bar;
  return (
    <div className="qbar" data-testid={props.testid} data-percent={String(b.percent)}>
      <div className="qhead">
        <span className="qname">{b.name}</span>
        <span className="qpct">{Math.round(b.percent)}%</span>
      </div>
      <div className={"qtrack" + (props.thin ? " thin" : "")}>
        <i className={`qfill ${b.level}`} style={{ width: `${b.percent}%` }} />
      </div>
      <div className="qnote">{b.note}</div>
    </div>
  );
}

export function UsagePanel(props: { usage: Usage | null }) {
  if (!props.usage) return <div className="block muted" data-testid="usage">usage not loaded</div>;
  const rows = Object.entries(props.usage.providers || {});
  return (
    <div className="block" data-testid="usage">
      <h3>Usage</h3>
      {rows.length === 0 ? <div className="muted small">nothing reported</div> : null}
      {rows.map(([name, p]) => {
        const bars = barsFor(p);
        const local = allowanceBar(p);
        return (
          <div key={name} style={{ marginBottom: 10 }} data-testid={`usage-${name}`}>
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
            <div data-testid={`quota-${name}`}>
              {bars.length ? (
                bars.map((b) => (
                  <QuotaBar key={b.name} bar={b} testid={`quota-bar-${name}-${b.name}`} />
                ))
              ) : (
                <span className="muted small">not reported</span>
              )}
            </div>
            {local ? (
              <QuotaBar bar={local} testid={`allowance-${name}`} thin />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

export function SchedulesPanel(props: {
  schedules: Schedule[];
  projects: Project[];
  onNew: () => void;
  onEdit: (s: Schedule) => void;
  onToggle: (id: string, enabled: boolean) => void;
  onRunNow: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const named = (id: string) => props.projects.find((p) => p.id === id)?.name || id;
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
            <span className="k">project</span>
            <span className="v">{named(s.project_id)}</span>
          </div>
          <div className="kv">
            <span className="k">last / next</span>
            <span className="v">{(s.last_run_at || "never") + " / " + (s.next_run_at || "—")}</span>
          </div>
          <div className="row">
            <button type="button" data-testid={`schedule-edit-${s.id}`} onClick={() => props.onEdit(s)}>
              Edit
            </button>
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
      {props.schedules.length === 0 ? (
        <div className="muted small" data-testid="schedules-empty">
          Nothing scheduled. You can also just ask in chat — "schedule a morning briefing at 8 on
          weekdays" — and Jarvis will create it here.
        </div>
      ) : null}
      <div className="row" style={{ marginTop: 8 }}>
        <button type="button" data-testid="schedule-new" onClick={props.onNew}>
          + New schedule
        </button>
      </div>
      <div className="muted small" style={{ marginTop: 6 }} data-testid="schedules-chat-note">
        Or ask in chat: "schedule a morning briefing at 8 on weekdays".
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
