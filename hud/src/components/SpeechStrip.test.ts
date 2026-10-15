import { act, createElement } from "react";
import { describe, expect, it } from "vitest";
import { createRoot, type Root } from "react-dom/client";
import { SpeechStrip, stripShown } from "./SpeechStrip";
import themeCss from "../theme.css?raw";
import type { SpeechView } from "../lib/speech/controller";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement;
let root: Root;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

it("idle with skipped chunks, the strip still says which chat they were in", async () => {
  const view: SpeechView = { state: "idle", thread: "t1", queued: 0, failed: 2, blocked: false, active: false, summarizing: false, enabled: true, speaker: true };
  const noop = () => {};
  await act(async () => root.render(createElement(SpeechStrip, { view, label: "second chat", onToggle: noop, onSkip: noop, onStop: noop, onClearMarks: noop })));
  expect(host.querySelector('[data-testid="speech-note"]')!.textContent).toBe("2 SKIPPED · second chat");
});


describe("GUEST VOICE on the strip (V-14)", () => {
  const idle: SpeechView = { state: "idle", thread: null, queued: 0, failed: 0, blocked: false, active: false, summarizing: false, enabled: true, speaker: true };
  const playing: SpeechView = { ...idle, state: "playing", thread: "t1" };
  const noop = () => {};
  const render = (props: Record<string, unknown>) => act(async () => root.render(createElement(SpeechStrip, { view: idle, label: "", onToggle: noop, onSkip: noop, onStop: noop, onClearMarks: noop, ...props } as any)));

  it("shows for a guest even when Jarvis is silent, and names it", async () => {
    expect(stripShown(idle)).toBe(false);
    expect(stripShown(idle, true)).toBe(true);
    await render({});
    expect(host.querySelector('[data-testid="speech-strip"]')).toBeNull();
    await render({ guest: true, onGuest: noop });
    expect(host.querySelector('[data-testid="guest-label"]')!.textContent).toBe("GUEST VOICE");
    expect(host.querySelector('[data-testid="speech-toggle"]')).toBeNull();      // no playback to control
  });
  it("its button turns Guest voice off, even with a card up", async () => {
    const calls: boolean[] = [];
    await render({ guest: true, blocked: true, onGuest: (on: boolean) => calls.push(on) });
    const button = host.querySelector('[data-testid="guest-toggle"]') as HTMLButtonElement;
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(button.disabled).toBe(false);
    await act(async () => button.click());
    expect(calls).toEqual([false]);
  });
  it("beside speech, the label and the playback controls are both there", async () => {
    await render({ view: playing, guest: true, onGuest: noop });
    expect(host.querySelector('[data-testid="guest-label"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="speech-toggle"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="speech-note"]')!.textContent).toMatch(/SPEAKING/);
  });
  it("offers the one-click switch on only when a voice is enrolled and there is speech to control", async () => {
    const calls: boolean[] = [];
    await render({ view: playing, canGuest: true, onGuest: (on: boolean) => calls.push(on) });
    await act(async () => (host.querySelector('[data-testid="guest-toggle"]') as HTMLButtonElement).click());
    expect(calls).toEqual([true]);
    await render({ view: playing, canGuest: false, onGuest: noop });
    expect(host.querySelector('[data-testid="guest-toggle"]')).toBeNull();
    await render({ view: playing, onGuest: noop });
    expect(host.querySelector('[data-testid="guest-label"]')).toBeNull();
  });
  it("folded into the rail it is still one visible, labelled button", async () => {
    await render({ compact: true, guest: true, onGuest: noop });
    const strip = host.querySelector('[data-testid="speech-strip"]')!;
    expect(strip.getAttribute("data-guest")).toBe("true");
    expect(host.querySelector('[data-testid="guest-toggle"]')!.getAttribute("aria-label")).toMatch(/Guest voice is on/);
  });
  it("also says the check is off or blocked, in its own words, with no Guest button", async () => {
    for (const label of ["VOICE CHECK OFF (not enrolled)", "VOICE CHECK BLOCKED"]) {
      await render({ voiceLabel: label });
      expect(host.querySelector('[data-testid="guest-label"]')!.textContent).toBe(label);
      expect(host.querySelector('[data-testid="guest-toggle"]')).toBeNull();
    }
    await render({ voiceLabel: "" });
    expect(host.querySelector('[data-testid="speech-strip"]')).toBeNull();
  });
  it("is not the amber or red of a pending card or an error", () => {
    const css = themeCss;
    const rule = css.slice(css.indexOf("#speechstrip .guestnote"), css.indexOf("#speechstrip .btns"));
    expect(rule).toContain("var(--text)");
    expect(rule).not.toMatch(/amber|red|pending|warn|err/i);
  });
});
