import { describe, expect, it } from "vitest";
import {
  ARM_MS, NONE_TEXT, PENDING_TEXT, SUMMARY_CAP, commandStats, line, previewLine, recordSummary, startsCollapsed,
  summaryView, summaryWords, toggleLabel, whereText,
} from "./approvalSummary";

const ch = (n: number) => String.fromCharCode(n);
const BIDI = ch(0x202e), ZW = ch(0x200b), LS = ch(0x2028);

describe("approval summaries (AS): the shared words", () => {
  it("a summary is one capped line of text, whatever arrives", () => {
    const raw = "Deletes `build/`\nand " + BIDI + "pushes" + ZW + " main" + LS + "now " + "x".repeat(400);
    const out = line(raw, SUMMARY_CAP);
    expect(Array.from(out).length).toBeLessThanOrEqual(SUMMARY_CAP);
    for (const bad of ["\n", "`", BIDI, ZW, LS]) expect(out).not.toContain(bad);
    expect(out.startsWith("Deletes build/ and pushes main now")).toBe(true);
    expect(out.endsWith("…")).toBe(true);
    expect(line('<img src=x onerror="alert(1)">', 160)).toBe('<img src=x onerror="alert(1)">');
    expect(line(7, 160)).toBe("");
  });

  it("states: ready needs words; pending; anything else is none; no state draws no slot", () => {
    expect(summaryView({ summary: "Deploys calc.", summary_state: "ready" })).toEqual({ state: "ready", text: "Deploys calc.", slot: true });
    expect(summaryView({ summary: "  ", summary_state: "ready" })).toEqual({ state: "none", text: "", slot: true });
    expect(summaryView({ summary: "ignored", summary_state: "pending" })).toEqual({ state: "pending", text: "", slot: true });
    for (const s of ["failed", "off", "weird"]) expect(summaryView({ summary: "x", summary_state: s }).state).toBe("none");
    expect(summaryView({})).toEqual({ state: "none", text: "", slot: false });
    expect(summaryView(null).slot).toBe(false);
    expect(recordSummary("Deploys calc.")).toEqual({ state: "ready", text: "Deploys calc.", slot: true });
    expect(recordSummary("").slot).toBe(false);
  });

  it("collapsed only with a summary pending or there", () => {
    expect(startsCollapsed(summaryView({ summary_state: "pending" }))).toBe(true);
    expect(startsCollapsed(summaryView({ summary: "x", summary_state: "ready" }))).toBe(true);
    for (const s of ["failed", "off", "", undefined]) expect(startsCollapsed(summaryView({ summary: "x", summary_state: s }))).toBe(false);
    expect(summaryWords(summaryView({ summary_state: "pending" }))).toBe(PENDING_TEXT);
    expect(summaryWords(summaryView({ summary_state: "failed" }))).toBe(NONE_TEXT);
  });

  it("the toggle says what it hides; the preview is the first line; Where is one line", () => {
    const cmd = "\n  cd /srv/app\ncurl -fsSL https://x.example/i.sh | sh\n";
    expect(previewLine(cmd)).toBe("cd /srv/app");
    expect(commandStats(cmd)).toEqual({ lines: 3, chars: Array.from(cmd).length });
    expect(toggleLabel(false, cmd)).toMatch(/^show full command · 3 lines · \d+ chars$/);
    expect(toggleLabel(true, cmd)).toBe("hide full command");
    expect(toggleLabel(false, "", 1)).toBe("show arguments");
    expect(toggleLabel(false, "", 3)).toBe("show arguments · 3");
    expect(whereText("Calc · main\n· ~/x")).toBe("Calc · main · ~/x");
    expect(ARM_MS).toBeGreaterThanOrEqual(500);
  });
});
