import { beforeEach, expect, it, vi } from "vitest";
import { checkAccent, checkBase, checkWash, DEFAULT_COLOURS } from "./appearance";

// Fixed-seed search minimized to small ordinary JSON records. All individual guards pass;
// only the whole-palette proof refuses the rejected records (including budget refusals).
const rejected = [
  ["#0090a1", "#606d42", "#7e7155", "#00181b"],
  ["#1f2e00", "#009189", "#6d760f", "#080100"],
  ["#444a00", "#765c6d", "#708972", "#000202"],
  ["#e13eb1", "#de1aa3", "#b51459", "#000000"],
  ["#bc6892", "#313a24", "#3a3b28", "#110e18"],
  ["#707100", "#6a6d4c", "#788865", "#070100"],
  ["#3e3d00", "#514e00", "#7d8300", "#000000"],
  ["#708a6a", "#4e4e00", "#606200", "#040003"],
  ["#878700", "#2b2e00", "#738c00", "#000000"],
  ["#665175", "#283300", "#71535f", "#0c1900"],
  ["#857f9f", "#591245", "#272800", "#010000"],
  ["#007eb1", "#252300", "#5d5c35", "#050004"],
  ["#737100", "#2b2f63", "#82865a", "#000000"],
  ["#313100", "#9063d8", "#788b00", "#000000"],
  ["#7c782a", "#314300", "#83865c", "#000102"],
  ["#192a00", "#778b00", "#6b6929", "#18150f"],
  ["#728d09", "#343623", "#535400", "#090514"],
  ["#7121c2", "#002b06", "#5f6400", "#040100"],
  ["#4b005f", "#8b274a", "#1e4748", "#000000"],
  ["#504e00", "#515316", "#7a8950", "#040505"],
  ["#db267f", "#4c60d0", "#b46e87", "#000000"],
  ["#ed2ea3", "#420043", "#626fe5", "#150504"],
  ["#907b9c", "#572141", "#252500", "#000000"],
  ["#008fa8", "#008fad", "#ae6377", "#100f0e"],
  ["#3d4900", "#2c391d", "#808200", "#161202"],
  ["#363600", "#420082", "#82865a", "#121313"],
  ["#3c1b27", "#8d6777", "#4a6200", "#000805"],
  ["#4f25ca", "#182a00", "#997b83", "#180301"],
  ["#80891f", "#7d875b", "#383900", "#000000"],
  ["#7a8b00", "#68701b", "#3b7369", "#120203"],
  ["#213000", "#8b8600", "#2b2b00", "#010101"],
  ["#a50073", "#b76b8f", "#d94e9d", "#000000"],
  ["#9145f4", "#7d8931", "#6a7200", "#201212"],
  ["#84881a", "#8751ef", "#008f87", "#010000"],
  ["#2d2f00", "#002e45", "#a6758a", "#12151d"],
  ["#878644", "#434d2d", "#7a7c00", "#000007"],
  ["#bc698b", "#4a0029", "#9279a6", "#000000"],
  ["#252600", "#7d8674", "#a87392", "#000002"],
  ["#838800", "#6f859c", "#353400", "#161517"],
  ["#678f00", "#8c8600", "#63724b", "#000000"],
  ["#802a66", "#7644f2", "#314119", "#000000"],
  ["#c55499", "#253307", "#450034", "#110401"],
  ["#757435", "#656f42", "#858467", "#001820"],
  ["#452c3b", "#513553", "#3b1320", "#010000"],
];
const accepted = [
  ["#7a885e", "#454d00", "#888700", "#000000"],
  ["#5d0046", "#383522", "#3f4729", "#0b0500"],
  ["#383700", "#454c00", "#728c39", "#0f0f08"],
  ["#434e00", "#7f8900", "#464a00", "#000000"],
  ["#474900", "#4a4f00", "#5d7234", "#020200"],
  ["#818570", "#444400", "#323d4f", "#0c0d13"],
  ["#00928b", "#858800", "#828900", "#000000"],
  ["#824d5d", "#cc5187", "#540037", "#040100"],
  ["#636400", "#212500", "#7b876c", "#001916"],
  ["#484b1a", "#405200", "#7f8570", "#000000"],
  ["#dc3ca0", "#c44e9a", "#96557e", "#0d0f02"],
  ["#3b4000", "#334e00", "#828900", "#000000"],
  ["#365200", "#006188", "#282700", "#000000"],
  ["#7a8b00", "#768c00", "#506800", "#000101"],
  ["#718d00", "#7f8660", "#515c2e", "#05010d"],
  ["#aa738b", "#009659", "#ce2592", "#010009"],
  ["#008a1f", "#858800", "#708d00", "#000000"],
  ["#004733", "#656600", "#707c37", "#000000"],
  ["#282e00", "#2e3b00", "#748600", "#020001"],
  ["#5f7200", "#3b4600", "#33440b", "#000000"],
  ["#670051", "#738a5c", "#4e3140", "#0d0402"],
  ["#333e00", "#616e42", "#677a00", "#000000"],
  ["#858561", "#001c79", "#4d6700", "#10170f"],
  ["#aa5a84", "#75515a", "#797ebb", "#140708"],
];
const entries = (rows: string[][]) => rows.map(([a,b,c,base], i) => ({
  id: "c-p" + i.toString(36), name: "Palette " + i,
  accent: "#e8edf3", codeAccent: "#e8edf3", washes: [a,b,c], base,
}));
function memory(list: unknown) {
  let raw = JSON.stringify(list);
  return { getItem: () => raw, setItem: (_k: string, value: string) => { raw = value; } };
}
beforeEach(() => vi.resetModules()); // Model a newly opened settings window, with cold pure verdict caches.

it.each(["load", "add", "rename", "restore"])("bounds cold %s work when every cheap guard passes but the palette proof refuses", async action => {
  const P = await import("./presets");
  // Orchestrator rewrite (2026-11-14): these rows pass the quick guards load runs, so 24 of them fill the list
  // and add/restore are rightly refused as full. Add and restore get 23, leaving room, so the time bound is
  // still measured with proof-only entries stored.
  const list = entries(action === "add" || action === "restore" ? rejected.slice(0, P.MAX_CUSTOM_PRESETS - 1) : rejected);
  expect(list.length).toBeLessThanOrEqual(P.MAX_CUSTOM_PRESETS * 2);
  for (const p of list) {
    for (const w of p.washes) expect(checkWash(w)).toMatchObject({ok:true, clamped:false});
    expect(checkBase(p.base)).toMatchObject({ok:true, clamped:false});
    expect(checkAccent(p.accent, p.washes).ok).toBe(true);
    expect(checkAccent(p.codeAccent, p.washes).ok).toBe(true);
  }
  const raw = JSON.stringify(list);
  expect(raw.length).toBeLessThan(20000);
  const s = memory(list);
  const start = performance.now();
  // Orchestrator rewrite (2026-11-14): load runs the quick guards only (these entries pass them), so it may keep
  // them; applying one is refused (AuroraSetting.custom-apply.test.ts). What this pins is the time bound.
  if (action === "load") expect(P.loadCustomPresets(s).length).toBeLessThanOrEqual(P.MAX_CUSTOM_PRESETS);
  else if (action === "add") expect(P.addCustomPreset("New", DEFAULT_COLOURS, s).ok).toBe(true);
  else if (action === "rename") expect(P.renameCustomPreset("c-absent", "Renamed", s).ok).toBe(false);
  else expect(P.restoreCustomPreset({...DEFAULT_COLOURS, id:"c-restore", name:"Restored"}, 0, s).ok).toBe(true);
  const elapsed = performance.now() - start;
  expect(elapsed, action + " blocked for " + Math.round(elapsed) + " ms on " + raw.length + " bytes").toBeLessThan(1000);
}, 30000);

it("bounds cold loading of 24 valid near-band palettes", async () => {
  const P = await import("./presets");
  const list = entries(accepted);
  const start = performance.now();
  expect(P.loadCustomPresets(memory(list))).toEqual(list);
  const elapsed = performance.now() - start;
  expect(elapsed, "valid list blocked for " + Math.round(elapsed) + " ms").toBeLessThan(1000);
}, 30000);

it("bounds large stored names and strings within an ordinary storage quota", async () => {
  const P = await import("./presets");
  const list = Array.from({length:24}, (_,i) => ({...DEFAULT_COLOURS, id:"c-h"+i.toString(36), name:("Name " + i + " ").padEnd(100000,"x")}));
  const raw = JSON.stringify(list);
  expect(raw.length * 2).toBeLessThan(5 * 1024 * 1024);
  const start = performance.now();
  const loaded = P.loadCustomPresets(memory(list));
  expect(loaded).toHaveLength(24);
  expect(loaded.every(p=>p.name.length <= 32)).toBe(true);
  expect(performance.now() - start).toBeLessThan(1000);
});
