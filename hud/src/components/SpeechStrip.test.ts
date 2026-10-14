import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { SpeechStrip } from "./SpeechStrip";
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
