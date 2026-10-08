// Usage, schedules and the decisions log — the status column.
//
// Quota is filled only from a provider's own report and is never computed.
// An absent window is a dash, not a bar at 0%: a bar is a number somebody
// will quote, and 0% used is a different fact from "we do not know".

import { useState } from "react";
import type { BackfillResult, DiscordStatus, Project, RouteView, Usage } from "../types";
import { meterFor, type Bar } from "../lib/quota";
import { backfillTargets, discordLight, guildConfigured } from "../lib/discord";

function ClaudeMark() {
  return (
    <svg className="claude-mark" viewBox="0 0 24 24" width="18" height="18" role="img" aria-label="Claude">
      <path
        fill="#D97757"
        d="M12 1.2c.4 3.6 1.8 5.8 4.9 7-3.1 1.2-4.5 3.4-4.9 7-.4-3.6-1.8-5.8-4.9-7 3.1-1.2 4.5-3.4 4.9-7zm0 6.6c.25 2.2 1.15 3.6 3.1 4.3-1.95.7-2.85 2.1-3.1 4.3-.25-2.2-1.15-3.6-3.1-4.3 1.95-.7 2.85-2.1 3.1-4.3z"
      />
    </svg>
  );
}

function Meter(props: { name: string; bar: Bar | null; testid: string }) {
  const b = props.bar;
  if (!b) {
    return (
      <div className="meter" data-testid={props.testid} data-percent="" title="not reported">
        <span className="mlabel">{props.name}</span>
        <span className="mdash">—</span>
      </div>
    );
  }
  return (
    <div className="meter" data-testid={props.testid} data-percent={String(b.percent)} title={b.note}>
      <span className="mlabel">{props.name}</span>
      <span className="mtrack">
        <i className={`qfill ${b.level}`} style={{ width: `${b.percent}%` }} />
      </span>
      <span className="mpct">{Math.round(b.percent)}%</span>
    </div>
  );
}

export function UsagePanel(props: { usage: Usage | null }) {
  if (!props.usage) return <div className="block muted" data-testid="usage">usage not loaded</div>;
  const providers = props.usage.providers || {};
  const claude = providers.claude;
  const codex = providers.codex;
  return (
    <div className="block usage-meters" data-testid="usage">
      <div className="meter-row" data-testid="usage-claude">
        <span className="meter-brand" data-testid="claude-mark">
          <ClaudeMark />
        </span>
        <div className="meter-bars" data-testid="quota-claude">
          <Meter name="5h" bar={meterFor(claude, "5h")} testid="quota-bar-claude-5h" />
          <Meter name="week" bar={meterFor(claude, "week")} testid="quota-bar-claude-week" />
        </div>
      </div>
      <div className="meter-row" data-testid="usage-codex">
        <span className="meter-brand">Codex</span>
        <div className="meter-bars" data-testid="quota-codex">
          <Meter name="week" bar={meterFor(codex, "weekly")} testid="quota-bar-codex-weekly" />
        </div>
      </div>
    </div>
  );
}

/** The Discord light (PR A): green ok, amber with the reason, red down.
 * The text is the backend's sentence or an operation/status/code triple —
 * rendered as text, never markup, like everything else in this window. */
export function DiscordPanel(props: {
  discord: DiscordStatus | null;
  /** B1: the live projects, for the one-time "Create channels" button. */
  projects?: Project[];
  onBackfill?: () => Promise<BackfillResult>;
}) {
  const light = discordLight(props.discord);
  const counters = props.discord?.reporter?.counters || {};
  const title = Object.entries(counters)
    .map(([k, v]) => `${k}: ${v}`)
    .join(" · ");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  // Offered once the server is set up and a surface is running, for as long
  // as a project has no channel — so after the backfill it is gone.
  const targets = backfillTargets(props.projects || []);
  const offer = !!props.onBackfill && guildConfigured(props.discord) && !!props.discord?.linker
    && targets.length > 0;
  const backfill = () => {
    if (!props.onBackfill || busy) return;
    setBusy(true);
    setNote("");
    props.onBackfill()
      .then((r) => {
        const failed = r.results.filter((x) => x.status === "failed");
        const parts = [`Created ${r.created} channel${r.created === 1 ? "" : "s"}.`];
        if (failed.length) parts.push(`Failed: ${failed.map((x) => `${x.name} (${x.error || "error"})`).join("; ")}.`);
        if (r.skipped.length) parts.push(`Skipped: ${r.skipped.map((x) => `${x.name} (${x.reason})`).join("; ")}.`);
        setNote(parts.join(" "));
      })
      .catch((e) => setNote(e?.message || "could not create the channels"))
      .finally(() => setBusy(false));
  };
  return (
    <div className="block discord-panel" data-testid="discord" data-level={light.level} title={title}>
      <i className={`discord-dot ${light.level}`} aria-hidden="true" />
      <span className="discord-text" data-testid="discord-text">{light.text}</span>
      {offer ? (
        <button type="button" className="discord-backfill" data-testid="discord-backfill"
                disabled={busy} onClick={backfill}>
          Create channels for {targets.length} project{targets.length === 1 ? "" : "s"}
        </button>
      ) : null}
      {note ? <span className="muted small" data-testid="discord-backfill-result">{note}</span> : null}
    </div>
  );
}

export function SchedulesButton(props: { count: number; onOpen: () => void }) {
  return (
    <div className="block">
      <button type="button" className="sched-open" data-testid="schedule-open" onClick={props.onOpen}>
        Schedules{props.count ? ` · ${props.count}` : ""}
      </button>
    </div>
  );
}

export function DecisionsLog(props: { route: RouteView | null }) {
  const decisions = [...(props.route?.decisions || [])].slice(-10).reverse();
  const states = Object.entries(props.route?.states || {})
    .map(([k, v]) => `${k}: ${v}`)
    .join(" · ");
  return (
    <details className="block decisions" data-testid="decisions">
      <summary data-testid="decisions-summary">
        Decisions{decisions.length ? ` · ${decisions.length}` : ""}
      </summary>
      {states ? (
        <div className="muted small" data-testid="route-states">{states}</div>
      ) : null}
      {decisions.map((d, i) => (
        <div className="step" key={i} data-testid="route-decision">
          {d.role} → {d.provider} ({d.reason})
        </div>
      ))}
      {decisions.length === 0 ? <div className="muted small">none yet</div> : null}
    </details>
  );
}
