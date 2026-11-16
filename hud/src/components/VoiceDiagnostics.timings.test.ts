import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { Capture } from "../lib/capture";
import { VoiceDiagnostics } from "../lib/voiceDiagnostics";
import { VoiceDiagnosticsPanel } from "./VoiceDiagnostics";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root | undefined;
afterEach(() => { act(() => root?.unmount()); root = undefined; });

const capture = () => new Capture({
  suppressed: () => false, muted: () => false, onLevel: () => {}, onListening: () => {}, onIdle: () => {}, onChunk: () => {}, onUtteranceEnd: () => {},
});
function render(diagnostics: VoiceDiagnostics) {
  const host = document.createElement("div");
  root = createRoot(host);
  act(() => root!.render(createElement(VoiceDiagnosticsPanel, { diagnostics, capture: capture(), muted: false })));
  return host;
}

describe("voice timing diagnostics UI", () => {
  it("renders per-row seconds and per-stage nearest-rank summary, omitting unknown stages", () => {
    const d = new VoiceDiagnostics(() => 0), id = d.add(1000, "wake", "transcribing");
    d.mark(id, "speechEnd", 1000); d.mark(id, "endpoint", 1700); d.mark(id, "transcribed", 2100); d.mark(id, "sent", 2150);
    const host = render(d);
    expect(host.querySelector('[data-testid="voice-timing-receipt"]')?.textContent)
      .toBe(" · end 0.70 · stt 0.40 · send 0.05 = 1.15 s");
    const summary = host.querySelector('[data-testid="voice-timing-summary"]')!;
    expect(summary.textContent).toContain("end · p50 0.70 s · p90 0.70 s · n=1");
    expect(summary.textContent).toContain("total · p50 1.15 s · p90 1.15 s · n=1");
    expect(summary.textContent).not.toContain("token"); expect(summary.textContent).not.toContain("audio");
  });

  it("an empty ring has no fabricated timing summary or receipt", () => {
    const host = render(new VoiceDiagnostics());
    expect(host.querySelector('[data-testid="voice-timing-summary"]')).toBeNull();
    expect(host.querySelector('[data-testid="voice-timing-receipt"]')).toBeNull();
  });
});
