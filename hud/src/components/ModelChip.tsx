// provider ▾ · model · effort ▾, beside `in: <project> ▾` in the input bar
// (decisions 2026-10-06, part A). Since 2026-10-10 the model and effort are one
// button that opens one popover with a Model section and an Effort section
// (and the provider's "set default"); the provider stays a select. The effort
// is a slider (components/EffortSlider), and the popover stays open while it
// moves (PR #30); Escape, a click outside or focus leaving closes it.
//
// While composing every chip is free and nothing reaches the server: the
// choice rides the first message's `POST /threads`. After it the provider is
// fixed (a session cannot change provider), and a model or effort change is a
// `PATCH /threads/{id}` that applies from the next message. A refused change
// leaves the chip on what the server holds and shows the server's reason.
//
// Every option label is text (a native select or a plain button): model names come off
// the network and this window draws authorization cards. Space on a focused
// chip must never start push-to-talk, so key events stop here.
//
// Several chat panes (WP-B) draw several chips at once, so each popover says
// whose it is (`data-pane`). It is reachable by keyboard: it opens on the
// chosen option, the arrows move within a list (on the effort slider they move
// the slider), Tab goes round the popover — each list's chosen option, the
// slider, then the footer — and Escape closes it and gives focus back to the
// button. It never outlives its button: a card, a click outside, focus leaving
// it, a resize, a zoom, a fold or a re-layout that moves the button closes it
// (review of PR #29).

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ModelRow, ProviderName } from "../types";
import { toCss } from "../lib/layout";
import {
  PROVIDERS, PROVIDER_LABELS, SEARCH, applyEffort, applyModel, applyProvider, canSetDefault,
  defaultChosenHere, defaultEffortOptions, defaultModel, defaultRows, defaultSourceLabel, effective,
  modelLabel, modelOptions, resetTitle, shortId, type Choice, type ThreadModels,
} from "../lib/threadmodel";
import { EffortSlider } from "./EffortSlider";

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
  /** May return the request's promise: the effort slider draws its change
   * until that settles. `from` is the conversation an effort move was made
   * on (`conversation` when it started); the change is dropped if the chip
   * shows another by then. */
  onChange: (next: Choice, from?: string) => void | Promise<unknown>;
  onSearch: () => void;
  /** Per provider, why the project cannot use it (greyed out), or null. */
  refusals?: Partial<Record<ProviderName, string | null>>;
  /** Open this provider's default menu (Claude and Codex, 2026-10-08). */
  onDefaults?: (provider: ProviderName) => void;
  /** The chat pane this chip belongs to: its popover carries it (`data-pane`). */
  pane?: number;
  /** The HUD zoom in percent, for placing the popover (lib/layout `toCss`). */
  zoom?: number;
  /** The conversation the chip shows (its pane's thread or compose row): an
   * effort move is tied to it (review of PR #30). */
  conversation?: string;
}) {
  const c = props.choice;
  const refused = props.refusals?.[c.provider] || null;
  const eff = effective(props.models, c);
  const title = props.modelEditable
    ? props.providerEditable
      ? "Chosen for this new thread; sent with its first message"
      : "Applies from the next message"
    : "Set by routing for a task's thread; read-only here";
  const summary = `${modelLabel(props.models, c)}${eff.effort ? ` · ${eff.effort}` : ""}`;
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  // A popover must not outlive the control it belongs to: a card arriving
  // disables the chip.
  useEffect(() => {
    if (props.disabled) setOpen(false);
  }, [props.disabled]);
  /** Close; `back` gives focus back to the button (Escape, a choice made). */
  const close = (back = false) => {
    setOpen(false);
    if (back) btn.current?.focus({ preventScroll: true });
  };

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
          <button
            type="button"
            ref={btn}
            className="modelbtn"
            data-testid="model-chip-btn"
            data-model={c.model ?? ""}
            data-effort={c.effort ?? ""}
            aria-haspopup="dialog"
            aria-expanded={open}
            disabled={props.disabled}
            title="Model and effort for this thread"
            onKeyDown={stop}
            onKeyUp={stop}
            onClick={() => setOpen((o) => !o)}
          >
            <span className="mb-label">{summary}</span> <span aria-hidden="true">▾</span>
          </button>
          {open && btn.current && !props.disabled ? (
            <Popover anchor={btn.current} label="Model and effort" pane={props.pane} zoom={props.zoom} onClose={close}>
              <div className="msec">Model</div>
              <div className="mlist" role="listbox" aria-label="Model" data-testid="model-list">
                {modelOptions(props.models, c).map((o) => (
                  <button
                    type="button"
                    key={o.value}
                    role="option"
                    aria-selected={o.value === (c.model ?? "")}
                    className={"mopt" + (o.value === (c.model ?? "") ? " sel" : "") + (o.value === SEARCH ? " search" : "")}
                    data-testid="model-opt"
                    data-value={o.value}
                    tabIndex={o.value === (c.model ?? "") ? 0 : -1}
                    onClick={() => {
                      if (o.value === SEARCH) {
                        close();
                        props.onSearch();
                        return;
                      }
                      props.onChange(applyModel(c, o.value || null));
                    }}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
              <EffortSlider
                models={props.models}
                choice={c}
                disabled={props.disabled}
                conversation={props.conversation}
                onCommit={(effort, from) => props.onChange(applyEffort(props.models, c, effort), from)}
              />
              {props.onDefaults && canSetDefault(props.models, c.provider) ? (
                <div className="mfoot">
                  <button
                    type="button"
                    data-testid="provider-defaults-open"
                    title={`Choose the model every default ${PROVIDER_LABELS[c.provider]} thread runs on`}
                    onClick={() => {
                      close();
                      props.onDefaults?.(c.provider);
                    }}
                  >
                    Set {PROVIDER_LABELS[c.provider]} default…
                  </button>
                </div>
              ) : null}
            </Popover>
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

/** The popover's width, its tallest, and the margin it keeps from the window's edges (zoomed px). */
const POP_W = 300;
const POP_MAX_H = 440;
const POP_EDGE = 8;
const POP_GAP = 6;
/** Opened upward unless there is less room than this above the button and more below. */
const POP_MIN_UP = 220;

/** The zoom in percent, read off the window when no caller said it. */
function zoomNow(): number {
  return (parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--ui-zoom")) || 1) * 100;
}

/**
 * Where the popover goes for a button at screen rect `r`: upward, as it was
 * designed, unless there is not enough room above and more below — then
 * downward. Never taller than the room it opens into, so it never runs off
 * the window's top or bottom (a fixed floor used to push it past the top).
 * Screen px in, zoomed px out, through `toCss` like every other menu here.
 */
export function placePopover(r: { left: number; top: number; bottom: number }, zoom: number,
                             win: { w: number; h: number }) {
  const vw = toCss(win.w, zoom);
  const vh = toCss(win.h, zoom);
  const top = toCss(r.top, zoom);
  const bottom = toCss(r.bottom, zoom);
  const width = Math.max(0, Math.min(POP_W, vw - 2 * POP_EDGE));
  const left = Math.max(POP_EDGE, Math.min(toCss(r.left, zoom), vw - width - POP_EDGE));
  const above = Math.max(0, top - POP_GAP - POP_EDGE);
  const below = Math.max(0, vh - bottom - POP_GAP - POP_EDGE);
  const up = above >= POP_MIN_UP || above >= below;
  return up
    ? { up, left, width, bottom: vh - top + POP_GAP, maxHeight: Math.min(POP_MAX_H, above) }
    : { up, left, width, top: bottom + POP_GAP, maxHeight: Math.min(POP_MAX_H, below) };
}

/**
 * A small panel opened from `anchor`, drawn inside #root so the zoom applies,
 * fixed-positioned in the zoomed space (`placePopover`). Closed by Escape
 * (focus back on the button), a click outside, focus leaving it, a resize, a
 * zoom change, or the button moving or going (a fold, a re-layout). Keys
 * inside it never reach push-to-talk; an Escape under an authorization card is
 * the card's (it denies) and is never swallowed here.
 */
function Popover(props: {
  anchor: HTMLElement;
  label: string;
  pane?: number;
  zoom?: number;
  onClose: (back?: boolean) => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Listeners are registered once: the parent's close is read through a ref,
  // not re-subscribed on every render.
  const onClose = useRef(props.onClose);
  onClose.current = props.onClose;
  const { anchor } = props;
  const zoom = props.zoom ?? zoomNow();
  const [at] = useState(() => ({
    zoom,
    rect: anchor.getBoundingClientRect(),
    place: placePopover(anchor.getBoundingClientRect(), zoom, { w: window.innerWidth, h: window.innerHeight }),
  }));

  // Opened on the chosen option (else the first), so the keyboard starts there.
  useLayoutEffect(() => {
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>('[role="option"][aria-selected="true"]')
      ?? el?.querySelector<HTMLElement>('[role="option"]');
    first?.focus({ preventScroll: true });
  }, []);

  // A zoom change closes it (it was placed for the old one).
  useEffect(() => {
    if (props.zoom !== undefined && props.zoom !== at.zoom) onClose.current();
  }, [props.zoom, at.zoom]);

  useEffect(() => {
    const down = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!ref.current?.contains(t) && !anchor.contains(t)) onClose.current();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // Under an authorization card Escape denies it (Approvals.tsx, on the
      // document): this must never stop that — even if a popover somehow
      // survived the card coming up.
      if (document.getElementById("authveil")) {
        onClose.current();
        return;
      }
      e.stopPropagation();
      onClose.current(true);
    };
    const gone = () => onClose.current();
    window.addEventListener("pointerdown", down, true);
    window.addEventListener("keydown", key, true);
    window.addEventListener("resize", gone);
    // The button moved or went — a fold, a split, the layout, the zoom: the
    // popover would be left pointing at nothing, so it closes.
    let raf = 0;
    const watch = () => {
      const r = anchor.getBoundingClientRect();
      if (!anchor.isConnected || Math.abs(r.left - at.rect.left) > 0.5 || Math.abs(r.top - at.rect.top) > 0.5
          || (r.width === 0 && r.height === 0) || Math.abs(zoomNow() - at.zoom) > 0.01) {
        onClose.current();
        return;
      }
      raf = requestAnimationFrame(watch);
    };
    raf = requestAnimationFrame(watch);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointerdown", down, true);
      window.removeEventListener("keydown", key, true);
      window.removeEventListener("resize", gone);
    };
  }, [anchor, at]);

  /** Within a list the arrows move (and wrap); the effort slider takes its
   * own arrows, Home and End (components/EffortSlider stops them there). Tab
   * goes round the popover's stops — each list's chosen option, the slider,
   * then the footer — and never leaves it. */
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    // Never to push-to-talk, nor any other window key handler below #root.
    e.stopPropagation();
    const el = ref.current;
    if (!el) return;
    const active = document.activeElement as HTMLElement | null;
    const list = active?.closest<HTMLElement>('[role="listbox"]');
    const move: Record<string, number> = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
    if (list && (e.key in move || e.key === "Home" || e.key === "End")) {
      e.preventDefault();
      const opts = Array.from(list.querySelectorAll<HTMLElement>('[role="option"]'));
      const i = opts.indexOf(active!);
      const next = e.key === "Home" ? 0 : e.key === "End" ? opts.length - 1
        : (i + move[e.key] + opts.length) % opts.length;
      opts[next]?.focus({ preventScroll: true });
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      const stops: HTMLElement[] = [];
      const sel = '[role="listbox"], [role="slider"]:not([aria-disabled="true"]), .mfoot button';
      for (const child of Array.from(el.querySelectorAll<HTMLElement>(sel))) {
        if (child.getAttribute("role") === "listbox") {
          const opt = child.querySelector<HTMLElement>('[role="option"][aria-selected="true"]')
            ?? child.querySelector<HTMLElement>('[role="option"]');
          if (opt) stops.push(opt);
        } else stops.push(child);
      }
      if (!stops.length) return;
      // Where focus is now: the stop itself, or (an unchosen option) its list's stop.
      const here = stops.findIndex((st) => st === active || (!!list && list.contains(st)));
      const next = e.shiftKey
        ? (here <= 0 ? stops.length - 1 : here - 1)
        : (here < 0 || here === stops.length - 1 ? 0 : here + 1);
      stops[next].focus({ preventScroll: true });
    }
  };

  const style: React.CSSProperties = {
    left: at.place.left, width: at.place.width, maxHeight: at.place.maxHeight,
    ...(at.place.up ? { bottom: at.place.bottom } : { top: at.place.top }),
  };
  return createPortal(
    <div
      ref={ref}
      className={"mpop" + (at.place.up ? " up" : " down")}
      role="dialog"
      aria-label={props.label}
      data-testid="model-pop"
      data-pane={props.pane}
      // Focusable, so a click on its padding or a heading keeps focus inside
      // (and does not read as focus leaving it).
      tabIndex={-1}
      style={style}
      onKeyDown={onKeyDown}
      onKeyUp={stop}
      onBlur={(e) => {
        // Focus left it (not for one of its own options, nor its button): it closes.
        const to = e.relatedTarget as Node | null;
        if (to && (ref.current?.contains(to) || anchor.contains(to))) return;
        if (!to && !document.hasFocus()) return; // the window lost focus, not the popover
        onClose.current();
      }}
    >
      {props.children}
    </div>,
    document.getElementById("root") || document.body,
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
