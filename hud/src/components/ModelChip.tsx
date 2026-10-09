// provider ▾ · model ▾ · effort ▾, beside `in: <project> ▾` in the input bar
// (decisions 2026-10-06, part A).
//
// While composing every chip is free and nothing reaches the server: the
// choice rides the first message's `POST /threads`. After it the provider is
// fixed (a session cannot change provider), and a model or effort change is a
// `PATCH /threads/{id}` that applies from the next message. A refused change
// leaves the chip on what the server holds and shows the server's reason.
//
// Native selects only, so every option label is text: model names come off
// the network and this window draws authorization cards. Space on a focused
// chip must never start push-to-talk, so key events stop here.

import { useMemo, useState } from "react";
import type { ModelRow, ProviderName } from "../types";
import {
  PROVIDERS, PROVIDER_LABELS, SEARCH, applyEffort, applyModel, applyProvider, canSetDefault,
  defaultChosenHere, defaultEffortOptions, defaultModel, defaultRows, defaultSourceLabel, effective,
  effortOptions, modelLabel, modelOptions, resetTitle, shortId, type Choice, type ThreadModels,
} from "../lib/threadmodel";

const stop = (e: React.KeyboardEvent) => e.stopPropagation();

export function ModelChip(props: {
  models: ThreadModels | null;
  choice: Choice;
  /** The provider can be chosen (composing only). */
  providerEditable: boolean;
  /** The model and effort can be chosen (composing, or an owner's chat thread). */
  modelEditable: boolean;
  disabled?: boolean;
  error?: string;
  onChange: (next: Choice) => void;
  onSearch: () => void;
  /** Per provider, why the project cannot use it (greyed out), or null. */
  refusals?: Partial<Record<ProviderName, string | null>>;
  /** Open this provider's default menu (Claude and Codex, 2026-10-08). */
  onDefaults?: (provider: ProviderName) => void;
}) {
  const c = props.choice;
  const refused = props.refusals?.[c.provider] || null;
  const eff = effective(props.models, c);
  const efforts = effortOptions(props.models, c);
  const title = props.modelEditable
    ? props.providerEditable
      ? "Chosen for this new thread; sent with its first message"
      : "Applies from the next message"
    : "Set by routing for a task's thread; read-only here";
  const summary = `${modelLabel(props.models, c)}${eff.effort ? ` · ${eff.effort}` : ""}`;

  return (
    <span className="chip projchip modelchip" data-testid="model-chip" title={title}>
      {props.providerEditable && !props.disabled ? (
        <select
          data-testid="provider-chip-select"
          style={{ width: "auto" }}
          value={c.provider}
          onKeyDown={stop}
          onKeyUp={stop}
          onChange={(e) => props.onChange(applyProvider(c, e.target.value as ProviderName))}
        >
          {PROVIDERS.map((p) => {
            const why = props.refusals?.[p] || null;
            return (
              <option key={p} value={p} disabled={!!why} title={why || undefined} data-refused={why ? "1" : undefined}>
                {PROVIDER_LABELS[p]}
              </option>
            );
          })}
        </select>
      ) : (
        <span data-testid="provider-chip">{PROVIDER_LABELS[c.provider] || c.provider}</span>
      )}
      {props.modelEditable ? (
        <>
          <select
            data-testid="model-chip-select"
            style={{ width: "auto", maxWidth: 220 }}
            value={c.model ?? ""}
            disabled={props.disabled}
            onKeyDown={stop}
            onKeyUp={stop}
            onChange={(e) => {
              const v = e.target.value;
              if (v === SEARCH) {
                props.onSearch();
                return;
              }
              props.onChange(applyModel(c, v || null));
            }}
          >
            {modelOptions(props.models, c).map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          {efforts.length ? (
            <select
              data-testid="effort-chip-select"
              style={{ width: "auto" }}
              value={c.effort ?? ""}
              disabled={props.disabled}
              onKeyDown={stop}
              onKeyUp={stop}
              onChange={(e) => props.onChange(applyEffort(props.models, c, e.target.value || null))}
            >
              {efforts.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          ) : null}
          {props.onDefaults && canSetDefault(props.models, c.provider) ? (
            <button
              type="button"
              data-testid="provider-defaults-open"
              disabled={props.disabled}
              title={`Choose the model every default ${PROVIDER_LABELS[c.provider]} thread runs on`}
              onKeyDown={stop}
              onKeyUp={stop}
              onClick={() => props.onDefaults?.(c.provider)}
            >
              default ▾
            </button>
          ) : null}
        </>
      ) : (
        <span data-testid="model-chip-ro" className="muted"> {summary}</span>
      )}
      {props.error ? (
        <span className="err" data-testid="model-error"> {props.error}</span>
      ) : props.providerEditable && refused ? (
        // The chosen provider cannot run in this project (it was chosen
        // before the project changed): say so before the send is refused.
        <span className="err" data-testid="provider-refused"> {refused}</span>
      ) : null}
    </span>
  );
}

/**
 * The OpenRouter catalogue, searched (A2). "Use" pins the model to the roster
 * and puts this thread on it; "Pin" only adds it to the roster, and "Unpin"
 * takes a pinned one off it (2026-10-08) — refused by the backend, with its
 * reason shown, when that would leave the default unlisted. Eligibility is
 * the backend's refusal: a model that cannot call tools is never listed.
 */
export function CatalogPicker(props: {
  catalog: ModelRow[] | null;
  roster: string[];
  error?: string;
  onUse: (id: string) => void;
  onPin: (id: string) => void;
  onUnpin: (id: string) => void;
  /** Opened from the Model picker: pin and unpin only — "Use" would also
   * move the open thread, which that picker is not about. */
  pinOnly?: boolean;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (props.catalog || [])
      .filter((m) => !needle || m.id.toLowerCase().includes(needle) || (m.name || "").toLowerCase().includes(needle))
      .slice(0, 60);
  }, [q, props.catalog]);
  const total = (props.catalog || []).length;

  return (
    <div className="pickerveil" data-testid="catalog" onClick={(e) => {
      if (e.target === e.currentTarget) props.onClose();
    }}>
      <div className="picker">
        <h3>Search catalogue</h3>
        <div className="pad col">
          <input
            data-testid="catalog-search"
            placeholder="name or id"
            value={q}
            autoFocus
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Escape") props.onClose();
            }}
            onKeyUp={stop}
          />
          <span className="muted small">
            Only models that can call tools are listed.{props.pinOnly ? "" : " Using one pins it to your roster."}
            {total > rows.length ? ` Showing ${rows.length} of ${total}; narrow the search.` : ""}
          </span>
          {props.catalog === null ? <span className="muted small">loading…</span> : null}
          {props.error ? <div className="err" data-testid="catalog-error">{props.error}</div> : null}
        </div>
        <div className="rows">
          {rows.map((m) => {
            const pinned = props.roster.includes(m.id);
            return (
              <div key={m.id} className="prow" data-testid={`catalog-row-${m.id}`}>
                <span>{m.name || shortId(m.id)}</span>
                <span className="sub">{m.id}{m.vision === false ? " · text-only" : ""}</span>
                {props.pinOnly ? null : (
                  <button type="button" data-testid={`catalog-use-${m.id}`} onClick={() => props.onUse(m.id)}>
                    Use
                  </button>
                )}
                {pinned ? (
                  <button type="button" data-testid={`catalog-unpin-${m.id}`}
                          title="Take it off your roster" onClick={() => props.onUnpin(m.id)}>
                    Unpin
                  </button>
                ) : (
                  <button type="button" data-testid={`catalog-pin-${m.id}`} onClick={() => props.onPin(m.id)}>
                    Pin
                  </button>
                )}
              </div>
            );
          })}
        </div>
        <div className="foot">
          <button type="button" data-testid="catalog-close" onClick={props.onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

/**
 * Claude's or Codex's default (2026-10-08): the model every default-following
 * thread on that provider runs on, chosen here as the OpenRouter one is in the
 * Model picker. "Set as default" moves the badge and relabels every default
 * thread at once; pinned threads keep their own model. "Reset to built-in
 * default" returns Claude to Opus 5.5 and Codex to routing's default. The
 * Codex default never rewrites routing (Settings) — it wins for chat threads
 * only. A refusal is shown here in the backend's words, never swallowed.
 * Every label is text: model names come off the network.
 */
export function ProviderDefaults(props: {
  models: ThreadModels | null;
  provider: ProviderName;
  error?: string;
  onSet: (model: string, effort: string) => void;
  onReset: () => void;
  onClose: () => void;
}) {
  const p = props.provider;
  const label = PROVIDER_LABELS[p] || p;
  const info = props.models?.providers[p];
  const current = defaultModel(props.models, p);
  const eff = defaultEffortOptions(props.models, p);
  return (
    <div className="pickerveil" data-testid="provider-defaults" onClick={(e) => {
      if (e.target === e.currentTarget) props.onClose();
    }}>
      <div className="picker" onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Escape") props.onClose();
      }} onKeyUp={stop}>
        <h3>{label} default</h3>
        <div className="pad col">
          <span className="muted small" data-testid="pd-now">
            {`Default ${label} threads run on ${current || "?"}`}
            {info?.default_effort ? ` · ${info.default_effort}` : ""}
            {` (${defaultSourceLabel(props.models, p)}). Pinned threads keep their own model.`}
            {p === "codex" ? " Routing for tasks is unchanged." : ""}
          </span>
          {info?.note ? <span className="muted small" data-testid="pd-note">{info.note}</span> : null}
          {props.error ? <div className="err" data-testid="pd-error">{props.error}</div> : null}
        </div>
        <div className="rows">
          {defaultRows(props.models, p).map((r) => (
            <div key={r.id} className={"prow" + (r.isDefault ? " sel" : "")} data-testid={`pd-row-${r.id}`}>
              <span>{r.name}</span>
              {r.isDefault ? <span className="badge" data-testid={`pd-badge-${r.id}`}>default</span> : null}
              <span className="sub">{r.id}</span>
              {r.isDefault ? (
                eff.options.length ? (
                  <select
                    data-testid="pd-effort"
                    style={{ width: "auto" }}
                    value={eff.value}
                    title="The effort a default thread runs at"
                    onChange={(e) => props.onSet(r.id, e.target.value)}
                  >
                    {eff.options.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                ) : null
              ) : (
                <button type="button" data-testid={`pd-set-${r.id}`} onClick={() => props.onSet(r.id, "")}>
                  Set as default
                </button>
              )}
            </div>
          ))}
        </div>
        <div className="foot">
          <button
            type="button"
            data-testid="pd-reset"
            disabled={!defaultChosenHere(props.models, p)}
            title={resetTitle(props.models, p)}
            onClick={props.onReset}
          >
            Reset to built-in default
          </button>
          <button type="button" data-testid="pd-close" onClick={props.onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
