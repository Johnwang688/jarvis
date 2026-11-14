import { expect, it } from "vitest";
import { DEFAULT_COLOURS } from "./appearance";
import { CUSTOM_PRESETS_KEY, loadCustomPresets } from "./presets";

it("bounds synchronous validation of an untrusted list that never fills the 24-entry output", () => {
  // Fits easily within localStorage's ordinary quota. Every name/id is distinct; every code accent
  // must be refused. Changing the base prevents verdict caching from concealing the work.
  const list = Array.from({ length: 4_096 }, (_, i) => ({
    ...DEFAULT_COLOURS, id: `c-p${i}`, name: `Hostile ${i}`, codeAccent: "#101418", // unreadable on the glass
    base: "#" + [i & 15, (i >> 4) & 15, 16 + (i >> 8)].map(n => n.toString(16).padStart(2, "0")).join(""),
  }));
  const raw = JSON.stringify(list);
  expect(raw.length).toBeLessThan(1_000_000);
  const store = { getItem: (key: string) => key === CUSTOM_PRESETS_KEY ? raw : null };
  const start = performance.now();
  expect(loadCustomPresets(store as Storage)).toEqual([]);
  const elapsed = performance.now() - start;
  // A one-second freeze is already far beyond the 50 ms long-task threshold. The deliberately
  // loose budget avoids flagging normal machine noise while exposing unbounded per-entry work.
  expect(elapsed, `blocked synchronously for ${Math.round(elapsed)} ms on ${raw.length} bytes`).toBeLessThan(1_000);
}, 30_000);
