// Which model and effort a chat thread runs on (decisions 2026-10-06, part A).
//
// A chat thread runs on one of three providers: OpenRouter (the fast path),
// Claude or Codex. The provider is chosen while composing and fixed by the
// first message; the model and the effort can change at any time and apply
// from the next message. Every rule here mirrors `jarvis/v2/thread_model.py`
// and is pure, so the free tests grade it.
//
// A model name comes off the network and this window draws authorization
// cards, so everything here produces **text** for React to render as text.

import type { Project, ProviderName, Thread } from "../types";

/** One row of a provider's model list (`GET /thread-models`). */
export interface ModelEntry {
  id: string;
  name?: string;
  efforts?: string[];
  /** The roster's per-model pin (OpenRouter only). */
  effort?: string | null;
  vision?: boolean;
  prompt_usd?: number | null;
  completion_usd?: number | null;
  unlisted?: boolean;
}

export interface ProviderModels {
  label: string;
  default: string | null;
  default_effort: string | null;
  models: ModelEntry[];
  note?: string;
  /** The permission profiles this provider can run a chat thread under. */
  profiles?: string[];
  /** Whether it can enforce a project's always-ask commands. */
  always_ask?: boolean;
}

/** `GET /thread-models`. */
export interface ThreadModels {
  providers: Partial<Record<ProviderName, ProviderModels>>;
  effort_default?: string;
}

/** What a thread runs on, as stored: `model: null` is the default, and
 * `effort: null` is that model's default effort. */
export interface Choice {
  provider: ProviderName;
  model: string | null;
  effort: string | null;
}

export const PROVIDERS: ProviderName[] = ["fast", "claude", "codex"];
export const PROVIDER_LABELS: Record<ProviderName, string> = {
  fast: "OpenRouter",
  claude: "Claude",
  codex: "Codex",
};
export const EFFORT_LADDER = ["max", "xhigh", "high", "medium", "low", "minimal", "none"];
export const DEFAULT_EFFORT = "high";
/** The model select's last entry on OpenRouter. */
export const SEARCH = "__search__";

/** `deepseek/deepseek-v4-flash-0731` -> `deepseek-v4-flash-0731`. */
export function shortId(id: string | null | undefined): string {
  if (!id) return "";
  const i = id.lastIndexOf("/");
  return i >= 0 ? id.slice(i + 1) : id;
}

export function entry(tm: ThreadModels | null, provider: ProviderName, model: string | null): ModelEntry | undefined {
  if (!tm || !model) return undefined;
  return tm.providers[provider]?.models.find((m) => m.id === model);
}

/** The model's own effort ladder; [] for none; null when unknown — a model
 * the list does not carry (an off-roster pin) or carries without a ladder (a
 * roster model the catalog has never described, `unlisted`). Unknown is not
 * "no reasoning control": the backend sends such a model the default effort
 * and accepts any level (`thread_model.efforts_of` returns None). */
export function effortsFor(tm: ThreadModels | null, provider: ProviderName, model: string | null): string[] | null {
  const e = entry(tm, provider, model);
  return e && Array.isArray(e.efforts) ? e.efforts : null;
}

function clamp(wanted: string, ladder: string[]): string | null {
  if (ladder.includes(wanted)) return wanted;
  const i = EFFORT_LADDER.indexOf(wanted);
  const order = i < 0 ? [] : [...EFFORT_LADDER.slice(i + 1), ...EFFORT_LADDER.slice(0, i).reverse()];
  return order.find((level) => ladder.includes(level)) ?? ladder[0] ?? null;
}

/** A4: high for every model, or the roster's own effort for it, within its
 * ladder; nothing for a model with no reasoning control. */
export function defaultEffort(tm: ThreadModels | null, provider: ProviderName, model: string | null): string | null {
  const e = entry(tm, provider, model);
  const wanted = (provider === "fast" && e?.effort) || tm?.effort_default || DEFAULT_EFFORT;
  const ladder = effortsFor(tm, provider, model);
  // An unknown ladder is sent as asked, as the backend does.
  if (ladder === null) return wanted;
  return ladder.length ? clamp(wanted, ladder) : null;
}

/** What `model: null` means right now for this provider. */
export function defaultModel(tm: ThreadModels | null, provider: ProviderName): string | null {
  return tm?.providers[provider]?.default ?? null;
}

/** A stored effort as `model` can run it: itself, the nearest level the
 * model offers (down first), nothing for a model with no reasoning control,
 * and as asked when the ladder is unknown (`thread_model.clamp_effort`). */
export function clampEffort(tm: ThreadModels | null, provider: ProviderName, model: string | null, wanted: string): string | null {
  const ladder = effortsFor(tm, provider, model);
  if (ladder === null) return wanted;
  return ladder.length ? clamp(wanted, ladder) : null;
}

/** The model and effort the thread's next message runs on. A default thread
 * follows the default model, and an effort chosen on it is clamped to
 * whatever that model is now (A4 amendment). */
export function effective(tm: ThreadModels | null, c: Choice): { model: string | null; effort: string | null } {
  if (c.model === null) {
    const p = tm?.providers[c.provider];
    const model = p?.default ?? null;
    const effort = c.effort === null
      ? p?.default_effort ?? null
      : model === null ? c.effort : clampEffort(tm, c.provider, model, c.effort);
    return { model, effort };
  }
  return { model: c.model, effort: c.effort ?? defaultEffort(tm, c.provider, c.model) };
}

/** Why `provider` cannot run a chat thread in this project, or null when it
 * can. The rules are the backend's (`thread_model.refusal`), read from
 * `GET /thread-models` rather than restated here, so the chip greys out
 * exactly what `POST /threads` would refuse. An older backend that sends no
 * table greys out nothing; the daemon still refuses with its reason. */
export function providerRefusal(
  tm: ThreadModels | null,
  provider: ProviderName,
  project: Pick<Project, "profile" | "always_ask"> | null | undefined,
): string | null {
  const p = tm?.providers[provider];
  if (!p || !project) return null;
  const label = PROVIDER_LABELS[provider] || provider;
  if (p.profiles && !p.profiles.includes(project.profile)) {
    if (project.profile === "strict" && provider !== "codex")
      return `${label} cannot run a strict project: it has no strict confinement`;
    return `${label} can only run a project on the ${p.profiles.join(" or ")} profile; this project is on ${project.profile}`;
  }
  if (p.always_ask === false && project.always_ask?.length)
    return `${label} cannot enforce this project's always-ask commands (${project.always_ask.join(", ")})`;
  return null;
}

/** A compose row's choice; absent fields are the defaults (A5). */
export function composeChoice(c: { provider?: ProviderName; model?: string | null; effort?: string | null } | null): Choice {
  return { provider: c?.provider || "fast", model: c?.model ?? null, effort: c?.effort ?? null };
}

export function choiceOf(thread: Thread): Choice {
  return { provider: thread.provider, model: thread.model ?? null, effort: thread.effort ?? null };
}

/** True when a pinned OpenRouter model has since left the roster (A3). */
export function offRoster(tm: ThreadModels | null, c: Choice): boolean {
  return c.provider === "fast" && !!c.model && !!tm && !entry(tm, "fast", c.model);
}

function price(e: ModelEntry): string {
  if (e.prompt_usd == null || e.completion_usd == null) return "";
  if (!e.prompt_usd && !e.completion_usd) return "free";
  const f = (n: number) => (n >= 10 ? n.toFixed(0) : n.toFixed(2).replace(/0$/, ""));
  return `$${f(e.prompt_usd)}/$${f(e.completion_usd)}`;
}

/** The chip's text: `default · deepseek-v4-flash-0731`, `kimi-k3`, or
 * `kimi-k3 (not on roster)`. */
export function modelLabel(tm: ThreadModels | null, c: Choice): string {
  if (c.model === null) return `default · ${shortId(defaultModel(tm, c.provider)) || "?"}`;
  return shortId(c.model) + (offRoster(tm, c) ? " (not on roster)" : "");
}

export interface Option {
  value: string;
  label: string;
}

/** The model select: default, the provider's models, a kept off-roster pin,
 * and on OpenRouter the catalogue search. */
export function modelOptions(tm: ThreadModels | null, c: Choice): Option[] {
  const out: Option[] = [{ value: "", label: `default · ${shortId(defaultModel(tm, c.provider)) || "?"}` }];
  for (const m of tm?.providers[c.provider]?.models || []) {
    const p = c.provider === "fast" ? price(m) : "";
    out.push({ value: m.id, label: shortId(m.id) + (p ? ` · ${p}` : "") });
  }
  if (c.model && !out.some((o) => o.value === c.model)) {
    out.push({ value: c.model, label: `${shortId(c.model)} (not on roster)` });
  }
  if (c.provider === "fast") out.push({ value: SEARCH, label: "search catalogue…" });
  return out;
}

/** The effort select for the effective model; empty when it has no control.
 * An unknown ladder offers every level: the backend accepts any of them for
 * a model it cannot describe, and refuses one a model it can describe lacks,
 * with the reason.
 *
 * On a default thread the ladder is the default model's, and an effort kept
 * from an earlier default that this one lacks is listed with what it runs
 * as now (A4 amendment: the choice is kept, and clamped per turn). */
export function effortOptions(tm: ThreadModels | null, c: Choice): Option[] {
  const eff = effective(tm, c);
  const ladder = effortsFor(tm, c.provider, eff.model);
  const dflt = c.model === null
    ? tm?.providers[c.provider]?.default_effort ?? null
    : defaultEffort(tm, c.provider, c.model);
  if (ladder !== null && ladder.length === 0) return [];
  const out: Option[] = [{ value: "", label: `default · ${dflt || "none"}` }];
  for (const level of ladder ?? EFFORT_LADDER) out.push({ value: level, label: level });
  if (c.effort && !out.some((o) => o.value === c.effort)) {
    const runs = eff.effort && eff.effort !== c.effort ? ` (runs as ${eff.effort})` : "";
    out.push({ value: c.effort, label: c.effort + runs });
  }
  return out;
}

/** A new model resets the effort to that model's default (A4). Going back to
 * the default (`null`) is a choice of model too, so it clears a stored
 * effort as well as the pin. */
export function applyModel(c: Choice, model: string | null): Choice {
  return { ...c, model, effort: null };
}

/** An effort alone never pins a model: a default thread keeps following the
 * default, and the effort is clamped to it per turn (A4 amendment). */
export function applyEffort(_tm: ThreadModels | null, c: Choice, effort: string | null): Choice {
  return { ...c, effort };
}

/** A new provider starts on its own defaults. */
export function applyProvider(_c: Choice, provider: ProviderName): Choice {
  return { provider, model: null, effort: null };
}

/** The `POST /threads` fields for a compose choice. A default sends no model. */
export function threadBody(c: Choice): { provider: ProviderName; brief?: { model?: string; effort?: string } } {
  const brief: { model?: string; effort?: string } = {};
  if (c.model) brief.model = c.model;
  if (c.effort) brief.effort = c.effort;
  return Object.keys(brief).length ? { provider: c.provider, brief } : { provider: c.provider };
}

/** The `PATCH /threads/{id}` body for a change, or null when nothing changed. */
export function patchBody(before: Choice, after: Choice): { model?: string | null; effort?: string | null } | null {
  if (before.model !== after.model) {
    return after.effort ? { model: after.model, effort: after.effort } : { model: after.model };
  }
  if (before.effort !== after.effort) return { effort: after.effort };
  return null;
}

/** Who may change what: the provider only while composing; the model and
 * effort on an owner's chat thread; nothing on a task's thread. */
export function chipEditable(thread: Thread | null, composing: boolean): { provider: boolean; model: boolean } {
  if (composing) return { provider: true, model: true };
  if (!thread || thread.task_id || thread.role !== "chat") return { provider: false, model: false };
  return { provider: false, model: true };
}

/** A5: a text-only model cannot see an attached image, and says so. */
export function visionNote(tm: ThreadModels | null, c: Choice): string | null {
  const eff = effective(tm, c);
  const e = entry(tm, c.provider, eff.model);
  if (!e || e.vision !== false) return null;
  return `${e.name || shortId(eff.model)} is text-only: it cannot see attached images.`;
}

/** The sidebar row's tooltip: provider and model, pinned or following. */
export function threadTooltip(thread: Thread, cwd?: string | null): string {
  const label = PROVIDER_LABELS[thread.provider] || thread.provider;
  const runs = thread.model
    ? `runs on ${label} · ${thread.model}${thread.effort ? ` · ${thread.effort}` : ""} (pinned)`
    : `runs on ${label} · follows the default${thread.effort ? ` · effort ${thread.effort}` : ""}`;
  return cwd ? `${runs}\nworks in ${cwd}` : runs;
}
