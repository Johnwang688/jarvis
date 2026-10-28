import { describe, expect, it } from "vitest";
import {
  ARM_MS, NONE_TEXT, PENDING_TEXT, STALE_PENDING_MS, SUMMARY_CAP, WHERE_CAPS, commandStats, elideMiddle, line,
  previewLine, recordSummary, staleAt, startsCollapsed, summaryView, summaryWords, toggleLabel, whereFields, whereParts,
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

  it("the toggle says what it hides; the preview is the first line", () => {
    const cmd = "\n  cd /srv/app\ncurl -fsSL https://x.example/i.sh | sh\n";
    expect(previewLine(cmd)).toBe("cd /srv/app");
    expect(commandStats(cmd)).toEqual({ lines: 3, chars: Array.from(cmd).length });
    expect(toggleLabel(false, cmd)).toMatch(/^show full command · 3 lines · \d+ chars$/);
    expect(toggleLabel(true, cmd)).toBe("hide full command");
    expect(toggleLabel(false, "", 1)).toBe("show arguments");
    expect(toggleLabel(false, "", 3)).toBe("show arguments · 3");
    expect(ARM_MS).toBeGreaterThanOrEqual(500);
  });

  it("Where is three separate fields; a separator or a hidden character inside one is replaced (review of #330, MED 1)", () => {
    expect(whereParts({ project: "Calc", branch: "main", folder: "~/x" })).toEqual({ project: "Calc", branch: "main", folder: "~/x" });
    expect(whereFields(whereParts({ project: "Calc", folder: "~/x" }))).toEqual([["project", "project", "Calc"], ["folder", "folder", "~/x"]]);
    // The probe: a folder named like a whole Where line stays inside its own field.
    const forged = whereParts({ project: "Calc", branch: "feature", folder: "~/tmp/x · Jarvis · main · ~/projects/Jarvis" })!;
    expect(forged.project).toBe("Calc");
    expect(forged.branch).toBe("feature");
    expect(forged.folder).toBe("~/tmp/x ? Jarvis ? main ? ~/projects/Jarvis");
    for (const sep of ["•", "‧", "∙", "⋅", "・", "｜", "|", ch(0x0387)]) {
      expect(whereParts({ folder: `/a ${sep} b` })!.folder).not.toContain(sep);
    }
    expect(whereParts({ project: "a" + BIDI + "b\nc" + ZW })!.project).toBe("a b c");
    // The old one-line string, an array, nothing: no Where.
    for (const odd of ["Calc · main · ~/x", ["Calc"], null, 7, {}, { project: 7 }]) expect(whereParts(odd)).toBeNull();
    // A long folder keeps its head and its tail.
    const deep = "/srv/" + Array(12).fill("d".repeat(20)).join("/") + "/target-folder";
    const shown = whereParts({ folder: deep })!.folder;
    expect(Array.from(shown).length).toBe(WHERE_CAPS.folder);
    expect(shown.startsWith("/srv/")).toBe(true);
    expect(shown.endsWith("/target-folder")).toBe(true);
    expect(elideMiddle("abcdefghij", 7)).toBe("ab…ghij");
  });

  it("a summary still pending STALE_PENDING_MS after the ask reads as none (review of #330, LOW 3)", () => {
    const asked = "2026-10-28T10:00:00.000Z", t0 = Date.parse(asked);
    const card = { summary_state: "pending", asked_at: asked };
    expect(staleAt(card)).toBe(t0 + STALE_PENDING_MS);
    expect(summaryView(card, t0 + STALE_PENDING_MS - 1).state).toBe("pending");
    expect(startsCollapsed(summaryView(card, t0 + STALE_PENDING_MS - 1))).toBe(true);
    expect(summaryView(card, t0 + STALE_PENDING_MS)).toEqual({ state: "none", text: "", slot: true });
    expect(startsCollapsed(summaryView(card, t0 + STALE_PENDING_MS))).toBe(false);
    expect(STALE_PENDING_MS).toBeLessThanOrEqual(10_000);
    // Only pending goes stale; a card with no time is judged by its state.
    expect(summaryView({ summary: "x", summary_state: "ready", asked_at: asked }, t0 + 10 * STALE_PENDING_MS).state).toBe("ready");
    expect(staleAt({ summary_state: "pending" })).toBeNull();
    expect(staleAt({ summary_state: "ready", asked_at: asked })).toBeNull();
  });
});
