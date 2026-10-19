// Binary click sparkle (owner, 2026-10-15): on each click of a button, a few tiny
// glacier-cyan `1` and `0` glyphs burst outward from the click point and fade in
// about half a second. Decoration only, so every rule here is about staying out of
// the way:
//
//   - one capture-phase listener that never calls preventDefault or
//     stopPropagation and never awaits: the click's own action is untouched, and
//     the glyphs are drawn a frame later (requestAnimationFrame);
//   - the glyphs live in one fixed, `pointer-events: none`, `aria-hidden` layer
//     appended to <body> (outside #root, so the approval card's inert pass and the
//     zoom never touch it). Nothing in it is focusable, so it can never take focus
//     off a card, the editor or a Browser frame, and it never changes layout;
//   - a hard cap on live glyphs (the oldest go first) and a timer that removes
//     each one even if `animationend` never arrives, so rapid clicking or a
//     hidden tab cannot leak nodes;
//   - off under `prefers-reduced-motion: reduce` (checked at every click, and the
//     layer is also hidden by CSS), and off when the Settings -> General switch is
//     off. The switch is a per-window convenience in localStorage like the other
//     HUD preferences (read behind try/catch; default on).
//
// A keyboard activation (Enter/Space on a focused button) has no pointer
// position, so it bursts from the button's centre.

import { readKey, writeKey } from "./storage";

export const SPARKLE_KEY = "jarvis.hud.sparkle";
/** Glyphs per click: "a few". */
export const BURST_MIN = 5;
export const BURST_MAX = 7;
/** The most glyphs alive at once, across every burst. */
export const MAX_PARTICLES = 24;
/** How long a glyph lives; matches the CSS animation (.sparkle-bit). */
export const LIFETIME_MS = 500;
/** How far a glyph travels, px. */
const REACH_MIN = 14;
const REACH_MAX = 30;
export const LAYER_ID = "sparkle-layer";

export function loadSparkle(storage?: Pick<Storage, "getItem">): boolean {
  return readKey(SPARKLE_KEY, storage) !== "0";
}

export function saveSparkle(on: boolean, storage?: Pick<Storage, "setItem">): void {
  writeKey(SPARKLE_KEY, on ? "1" : "0", storage);
}

export interface SparkleHost {
  doc: Document;
  /** Defaults to window.matchMedia; absent means "not reduced". */
  reducedMotion?: () => boolean;
  raf?: (cb: () => void) => unknown;
  setTimer?: (cb: () => void, ms: number) => unknown;
  clearTimer?: (id: unknown) => void;
  random?: () => number;
  storage?: Pick<Storage, "getItem" | "setItem">;
  /** Where the storage event comes from (the window); defaults to doc.defaultView. */
  events?: Pick<Window, "addEventListener" | "removeEventListener"> | null;
}

export interface Sparkle {
  enabled(): boolean;
  /** The Settings switch: remembered, and applied at once. */
  setEnabled(on: boolean): void;
  /** Glyphs currently alive. */
  live(): number;
  dispose(): void;
}

const BUTTON = 'button, [role="button"]';

export function createSparkle(host: SparkleHost): Sparkle {
  const doc = host.doc;
  const win = doc.defaultView;
  const raf = host.raf ?? ((cb: () => void) => (win ? win.requestAnimationFrame(cb) : cb()));
  const setTimer = host.setTimer ?? ((cb: () => void, ms: number) => setTimeout(cb, ms));
  const clearTimer = host.clearTimer ?? ((id: unknown) => clearTimeout(id as number));
  const random = host.random ?? Math.random;
  const reduced = host.reducedMotion ?? (() => {
    try { return !!win?.matchMedia?.("(prefers-reduced-motion: reduce)").matches; } catch { return false; }
  });
  let on = loadSparkle(host.storage);
  const live: { el: HTMLElement; timer: unknown }[] = [];
  let layer: HTMLElement | null = null;

  const drawLayer = (): HTMLElement | null => {
    if (layer && layer.isConnected) return layer;
    const parent = doc.body;
    if (!parent) return null;
    const el = doc.createElement("div");
    el.id = LAYER_ID;
    el.setAttribute("aria-hidden", "true");
    el.setAttribute("data-testid", "sparkle-layer");
    parent.appendChild(el);
    layer = el;
    return el;
  };

  const drop = (entry: { el: HTMLElement; timer: unknown }) => {
    clearTimer(entry.timer);
    entry.el.remove();
    const at = live.indexOf(entry);
    if (at >= 0) live.splice(at, 1);
  };

  const burst = (x: number, y: number) => {
    if (!on || reduced()) return;
    const root = drawLayer();
    if (!root) return;
    const n = BURST_MIN + Math.floor(random() * (BURST_MAX - BURST_MIN + 1));
    for (let i = 0; i < n; i++) {
      // Oldest first, so a flurry of clicks keeps the newest glyphs.
      while (live.length >= MAX_PARTICLES) drop(live[0]);
      const el = doc.createElement("span");
      el.className = "sparkle-bit";
      el.textContent = random() < 0.5 ? "0" : "1";
      const angle = ((i + random() * 0.8) / n) * Math.PI * 2;
      const reach = REACH_MIN + random() * (REACH_MAX - REACH_MIN);
      el.style.left = `${Math.round(x)}px`;
      el.style.top = `${Math.round(y)}px`;
      el.style.setProperty("--dx", `${Math.round(Math.cos(angle) * reach)}px`);
      el.style.setProperty("--dy", `${Math.round(Math.sin(angle) * reach)}px`);
      const entry = { el, timer: null as unknown };
      entry.timer = setTimer(() => drop(entry), LIFETIME_MS + 120);
      el.addEventListener("animationend", () => drop(entry), { once: true });
      root.appendChild(el);
      live.push(entry);
    }
  };

  const onClick = (e: MouseEvent) => {
    try {
      if (!on) return;
      const target = e.target as Element | null;
      const button = target && typeof target.closest === "function" ? target.closest(BUTTON) : null;
      if (!button) return;
      if ((button as HTMLButtonElement).disabled || button.getAttribute("aria-disabled") === "true") return;
      let x = e.clientX;
      let y = e.clientY;
      // Enter/Space (and a script's .click()) report detail 0 and no pointer:
      // burst from the middle of the button. Read now: the click's own action
      // may remove the button before the frame.
      if (e.detail === 0 || (x === 0 && y === 0)) {
        const r = button.getBoundingClientRect();
        x = r.left + r.width / 2;
        y = r.top + r.height / 2;
      }
      raf(() => { try { burst(x, y); } catch { /* decoration never throws into the app */ } });
    } catch {
      /* ditto */
    }
  };

  const onStorage = (e: StorageEvent) => {
    if (e.key === SPARKLE_KEY || e.key === null) on = loadSparkle(host.storage);
  };

  doc.addEventListener("click", onClick, true);
  const events = host.events === undefined ? win : host.events;
  events?.addEventListener("storage", onStorage as EventListener);

  return {
    enabled: () => on,
    setEnabled(next) {
      on = next;
      saveSparkle(next, host.storage);
      if (!next) while (live.length) drop(live[0]);
    },
    live: () => live.length,
    dispose() {
      doc.removeEventListener("click", onClick, true);
      events?.removeEventListener("storage", onStorage as EventListener);
      while (live.length) drop(live[0]);
      layer?.remove();
      layer = null;
    },
  };
}

let shared: Sparkle | null = null;

/** The window's one sparkle, installed from main.tsx. */
export function installSparkle(): Sparkle {
  if (!shared) shared = createSparkle({ doc: document });
  return shared;
}

export function sparkleEnabled(): boolean {
  return shared ? shared.enabled() : loadSparkle();
}

export function setSparkleEnabled(on: boolean): void {
  if (shared) shared.setEnabled(on);
  else saveSparkle(on);
}
