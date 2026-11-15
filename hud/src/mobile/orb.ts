import { CODE_SVG } from "./icons";
// Phone connection health, separate from the unlock window. Pure state selection;
// CSS owns all motion, and every colour has a visible word.
import { agoWords } from "./reltime";

export interface OrbSignals {
  signedOut: boolean;
  online: boolean;
  failStatus: number | null;
  streamOpen: boolean;
  everOpened: boolean;
  firstOpenTimedOut: boolean;
  secure: { ok: boolean } | null;
  working: number;
  lastRead: number | null;
  now: number;
}
export interface PhoneOrbState {
  state: "error" | "offline" | "reconnecting" | "connecting" | "working" | "live";
  tone: "err" | "wait" | "ok";
  word: string;
}

/** First match wins (spec §4): signed out > unreachable > offline > limited > reconnecting > live.
 * Offline outranks Limited: a limited phone in airplane mode shows amber Offline (the orb is about the
 * connection); Limited's action restrictions still apply, since they follow `secure`, never the orb. */
export function orbState(s: OrbSignals): PhoneOrbState {
  if (s.signedOut) return { state: "error", tone: "err", word: "Signed out" };
  if (!s.online && [502, 503, 504].includes(s.failStatus ?? 0))
    return { state: "error", tone: "err", word: "Jarvis unreachable" };
  if (!s.online) return { state: "offline", tone: "wait",
    word: s.lastRead === null ? "Offline · not read yet" : `Offline · read ${agoWords(s.lastRead, s.now)}` };
  if (s.secure && !s.secure.ok) return { state: "error", tone: "err", word: "Limited connection" };
  if (!s.streamOpen && (s.everOpened || s.firstOpenTimedOut)) return { state: "reconnecting", tone: "wait", word: "Reconnecting…" };
  if (!s.streamOpen) return { state: "connecting", tone: "ok", word: "Connecting…" };
  if (s.working > 0) return { state: "working", tone: "ok", word: `Live · ${s.working} working` };
  return { state: "live", tone: "ok", word: "Live" };
}

const NS = "http://www.w3.org/2000/svg";
const pt = (r: number, a: number) => `${(12 + Math.cos(a) * r).toFixed(2)} ${(12 + Math.sin(a) * r).toFixed(2)}`;
const arcs = (r: number, spans: [number, number][]) => spans.map(([a, b]) =>
  `M${pt(r, a)}A${r} ${r} 0 ${b - a > Math.PI ? 1 : 0} 1 ${pt(r, b)}`).join("");
// MiniOrb's 24-tick scale, heavy segmented band, two inner arcs and core.
const TICKS = Array.from({ length: 24 }, (_, i) => {
  const a = i / 24 * Math.PI * 2;
  return `M${pt(i % 6 === 0 ? 9.6 : 10.6, a)}L${pt(11.6, a)}`;
}).join("");
function orb(): HTMLElement {
  const node = document.createElement("span");
  node.className = "m-orb";
  node.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS(NS, "svg");
  CODE_SVG.add(svg);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const [name, d] of [
    ["outer", TICKS], ["band", arcs(8.2, [[.85, 1.9], [2.45, 3.6], [3.9, 4.6], [4.95, 5.9]])],
    ["inner", arcs(6, [[0, 2.4], [2.9, 5.6]])],
  ]) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("class", "m-o-" + name);
    path.setAttribute("d", d);
    svg.append(path);
  }
  const core = document.createElementNS(NS, "circle");
  for (const [key, value] of Object.entries({ class: "m-o-core", cx: "12", cy: "12", r: "2.6" })) core.setAttribute(key, value);
  svg.append(core);
  node.append(svg);
  return node;
}

/** Build the shared brand, including the stable m-conn word. No navigation until RD-6. */
export function brandButton(onBrand: () => void = () => {}): HTMLButtonElement {
  const brand = document.createElement("button");
  brand.type = "button";
  brand.className = "tap m-brand";
  brand.dataset.testid = "m-brand";
  const text = document.createElement("span");
  text.className = "m-brand-text";
  const name = document.createElement("span");
  name.className = "m-brand-name";
  name.textContent = "Jarvis";
  const word = document.createElement("span");
  word.className = "conn";
  word.dataset.testid = "m-conn";
  text.append(name, word);
  brand.append(orb(), text);
  brand.addEventListener("click", onBrand);
  return brand;
}

/** Update in place so signal changes preserve the brand's focus and animation. */
export function renderOrb(brand: HTMLButtonElement, state: PhoneOrbState): void {
  const node = brand.querySelector<HTMLElement>(".m-orb")!;
  const word = brand.querySelector<HTMLElement>('[data-testid="m-conn"]')!;
  node.dataset.state = word.dataset.state = state.state;
  word.dataset.tone = state.tone;
  word.textContent = state.word;
  brand.setAttribute("aria-label", `Jarvis, ${state.word}. Open navigation`);
}
