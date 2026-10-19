// Usage, schedules and the decisions log — the status column.
//
// Quota is filled only from a provider's own report and is never computed.
// An absent window is a dash, not a bar at 0%: a bar is a number somebody
// will quote, and 0% used is a different fact from "we do not know".

import { useState } from "react";
import type { BackfillResult, DiscordStatus, Project, RouteView, Usage } from "../types";
import { meterFor, partialNote, partialTurns, type Bar } from "../lib/quota";
import { backfillTargets, discordLight, guildConfigured } from "../lib/discord";

// The providers' own marks, as Simple Icons draws them (CC0 path data,
// simple-icons 13.21.0). Claude keeps its brand coral; OpenAI's mark is
// monochrome, so it takes the text colour.
function ClaudeMark() {
  return (
    <svg className="claude-mark" viewBox="0 0 24 24" width="18" height="18" role="img" aria-label="Claude">
      <path fill="#D97757" d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z" />
    </svg>
  );
}

function OpenAIMark() {
  return (
    <svg className="openai-mark" viewBox="0 0 24 24" width="18" height="18" role="img" aria-label="Codex (OpenAI)">
      <path fill="currentColor" d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
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
  const partial = partialTurns(codex);
  return (
    <div className="block usage-meters" data-testid="usage">
      <div className="meter-row" data-testid="usage-claude">
        <span className="meter-brand" data-testid="claude-mark" title="Claude">
          <ClaudeMark />
        </span>
        <div className="meter-bars" data-testid="quota-claude">
          <Meter name="5h" bar={meterFor(claude, "5h")} testid="quota-bar-claude-5h" />
          <Meter name="week" bar={meterFor(claude, "week")} testid="quota-bar-claude-week" />
        </div>
      </div>
      <div className="meter-row" data-testid="usage-codex">
        <span className="meter-brand" data-testid="codex-mark" title="Codex">
          <OpenAIMark />
        </span>
        <div className="meter-bars" data-testid="quota-codex">
          <Meter name="week" bar={meterFor(codex, "weekly")} testid="quota-bar-codex-weekly" />
        </div>
      </div>
      {partial ? (
        // Codex's own weekly meter above is its report and unaffected; this
        // qualifies Jarvis's local token count (ledger, task ceilings).
        <div className="muted small" data-testid="usage-codex-partial" title={partialNote(partial)}>
          tokens may be low (≥)
        </div>
      ) : null}
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
