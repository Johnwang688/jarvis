// PM-F's shared PR-row rules (lib/prRow.ts): what both surfaces read from a
// PR row, which buttons they draw, and in whose words.
import { describe, expect, it } from "vitest";
import { parseInbox } from "./inbox";
import {
  actionBody, actionLabel, fixCardLines, fixSummaryWords, methodWords, prArmKey, prButtons, prFixWords, prRowOf,
  prSummaryWords, questionRow, resultOk, resultWords,
} from "./prRow";
import { FLAGGED, HOSTILE, cardOf, facts, fixRow, headOf, prRow, tqRow } from "./prRow.fixtures";

const view = (raw: unknown) => prRowOf(parseInbox([raw])![0])!;

describe("prRowOf", () => {
  it("reads the row's binding, facts, flags, summary and state; nothing else is a PR", () => {
    const v = view(prRow(11));
    expect(v.head).toBe(headOf(11));
    expect(v.card).toBe(cardOf(11));
    expect(v.actions).toEqual(["merge"]);
    expect(v.facts!.map((p) => p.key)).toEqual(["pr", "branch", "checks", "files", "head", "method"]);
    expect(v.summary).toEqual({ does: "Adds a multiply button " + HOSTILE.replace("‮", ""), blocked: "" });
    expect(v.title).toBe("Add multiply " + HOSTILE.replace("‮", ""));
    expect(prRowOf(parseInbox([{ ...prRow(1), kind: "task" }])![0])).toBeNull();
    expect(prRowOf(parseInbox([prRow(1, { number: "1" })])![0])).toBeNull();
  });

  it("binds only a 40-hex head and a 64-hex card; anything else offers nothing", () => {
    for (const bad of [{ head: "abc" }, { head: "G".repeat(40) }, { card: "x" }, { card: null }, { head: null }]) {
      const v = view(prRow(11, bad));
      expect(prButtons(v).actions, JSON.stringify(bad)).toEqual([]);
      expect(actionBody(v, "merge")).toBeNull();
    }
  });

  it("keeps only known actions, in the HUD's order", () => {
    expect(view(prRow(11, { actions: ["update", "admin", "merge", "fix", "merge_as_is", "rebase"] })).actions)
      .toEqual(["merge", "merge_as_is", "fix", "update"]);
    expect(view(prRow(11, { actions: "merge" })).actions).toEqual([]);
  });

  it("labels a scanner by code only when the row's own `failing` agrees; a lookalike stays a name", () => {
    const v = view(prRow(14, {
      facts: facts(14, { checks: "checks: failing", checkItems: ["GitGuardian [secret scanner]", "lint [secret scanner]"] }),
      failing: [{ name: "GitGuardian", scanner: true }, { name: "lint", scanner: false }],
    }));
    const checks = v.facts!.find((p) => p.key === "checks")!;
    expect(checks.items).toEqual([{ name: "GitGuardian", scanner: true }, { name: "lint [secret scanner]", scanner: false }]);
  });

  it("drops a facts part whose key code does not know, and flag families code does not know", () => {
    const v = view(prRow(15, { facts: [{ key: "evil", text: "checks: passing", items: [], more: 0 }, ...facts(15)],
                               flags: FLAGGED }));
    expect(v.facts!.map((p) => p.key)).not.toContain("evil");
    expect(v.flags!.items[1]).toEqual({ name: ".github/workflows/ci.yml", families: ["CI"] });
  });

  it("reads a hold as its note; held means no buttons, whatever `actions` says (Q3)", () => {
    const v = view(prRow(22, { hold: { note: "waits on the robot", set_at: "x" }, actions: ["merge"] }));
    expect(v.hold).toBe("waits on the robot");
    expect(prButtons(v).actions).toEqual([]);
    expect(prButtons(v, { limited: true })).toEqual({ actions: [], desk: false });
  });
});

describe("prButtons (Q1)", () => {
  it("a limited phone gets nothing on a flagged PR, or one whose flags are not known", () => {
    expect(prButtons(view(prRow(15, { flags: FLAGGED })), { limited: true })).toEqual({ actions: [], desk: true });
    expect(prButtons(view(prRow(15, { flags: null })), { limited: true })).toEqual({ actions: [], desk: true });
    expect(prButtons(view(prRow(15, { flags: FLAGGED }))).actions).toEqual(["merge"]);
    expect(prButtons(view(prRow(11)), { limited: true }).actions).toEqual(["merge"]);
    // Nothing to hide: no desk line either.
    expect(prButtons(view(prRow(15, { flags: FLAGGED, actions: [] })), { limited: true }).desk).toBe(false);
  });
});

describe("words", () => {
  it("names squash before the OK only when a Merge is offered (Q4)", () => {
    const sq = view(prRow(21, { method: "squash" }));
    expect(methodWords(sq, ["merge"])).toContain("squash merge");
    expect(methodWords(sq, ["fix"])).toBe("");
    expect(methodWords(view(prRow(11)), ["merge"])).toBe("");
  });

  it("the summary slot: two labelled lines, summarizing…, or why there is none", () => {
    expect(prSummaryWords(view(prRow(11, { summary: { does: "d", blocked: "b" } }))).lines)
      .toEqual([{ label: "Does", text: "d" }, { label: "Blocked", text: "b" }]);
    expect(prSummaryWords(view(prRow(12, { summary: null, summary_state: "pending" })))).toEqual(
      { state: "pending", lines: [], text: "summarizing…" });
    expect(prSummaryWords(view(prRow(18, { summary: null, summary_state: "failed", summary_reason: "the model timed out" })))
      .text).toBe("AI summary unavailable: the model timed out");
    expect(prSummaryWords(view(prRow(19, { summary: null, summary_state: "off", summary_reason: "" }))).text)
      .toBe("AI summary unavailable: no reason was given");
    expect(prSummaryWords(view(prRow(19, { summary: null, summary_state: undefined }))).state).toBe("none");
  });

  it("each fix state in code's words; READY never draws the daemon's note (it holds the model's words)", () => {
    const fix = (state: string, extra: Record<string, unknown> = {}) => view(fixRow(31, state, extra)).fix!;
    expect(prFixWords(fix("running", { note: "fix running" }))).toMatchObject(
      { text: "fix running (task t0000031)", cancel: true, question: false });
    expect(prFixWords(fix("starting"))).toMatchObject({ text: "fix starting (task t0000031)", cancel: true });
    expect(prFixWords(fix("question"))).toMatchObject({ text: "fix waits on your answer (task t0000031)", question: true, cancel: true });
    expect(prFixWords(fix("blocked", { note: "fix stopped: blocked: cancel it to Fix again" })))
      .toMatchObject({ cancel: true, text: "fix stopped: blocked: cancel it to Fix again (task t0000031)" });
    expect(prFixWords(fix("stopped", { note: "fix stopped: failed" }))).toMatchObject({ text: "fix stopped: failed", cancel: false });
    expect(prFixWords(fix("failing", { note: "fix pushed; checks still fail" })).text).toBe("fix pushed; checks still fail");
    expect(prFixWords(fix("pushed", { note: "fix pushed; checks running" })).text).toBe("fix pushed; checks running");
    expect(prFixWords(fix("rewritten"))).toMatchObject({ text: "branch rewritten since your Fix", tone: "amber" });
    expect(prFixWords(fix("start_failed", { note: "fix could not start: x" })).text).toBe("fix could not start: x");
    const ready = fix("ready", { note: "fixed: " + HOSTILE + " — merge?", summary: "s", summary_state: "ready" });
    expect(ready.note).toBe("");
    expect(prFixWords(ready)).toMatchObject({ text: "Fixed — merge?", card: true });
    expect(view(fixRow(31, "nonsense")).fix).toBeNull();
  });

  it("the fix card's code lines and its AI slot", () => {
    const f = (extra: Record<string, unknown>) => view(fixRow(37, "ready", extra)).fix!;
    expect(fixCardLines(f({ commits: 2, ci_changed: true }))).toEqual(
      { commits: "2 commits since your Fix", ci: "the fix changed CI configuration" });
    expect(fixCardLines(f({ commits: 1, ci_changed: false }))).toEqual({ commits: "1 commit since your Fix", ci: "" });
    expect(fixCardLines(f({ commits: null, ci_changed: null })).ci).toContain("could be checked");
    expect(fixSummaryWords(f({ summary: "did x", summary_state: "ready" }))).toEqual({ state: "ready", text: "did x" });
    expect(fixSummaryWords(f({ summary_state: "pending" })).text).toBe("summarizing…");
    expect(fixSummaryWords(f({ summary_state: "off" })).state).toBe("none");
  });

  it("Fix after an ended fix is Fix again; the question row is found by its task", () => {
    expect(actionLabel(view(fixRow(33, "stopped", {}, { actions: ["fix"] })), "fix")).toBe("Fix again");
    expect(actionLabel(view(prRow(13, { actions: ["fix"] })), "fix")).toBe("Fix");
    const q = view(fixRow(32, "question")).fix!;
    const rows = parseInbox([tqRow("t0000032abcdef"), tqRow("other")])!;
    expect(questionRow(q, rows)).toBe("tq:t0000032abcdef:0");
    expect(questionRow(q, [])).toBeNull();
  });

  it("an action's body is the drawn head and card; Merge as is names itself", () => {
    const v = view(prRow(11));
    expect(actionBody(v, "merge")).toEqual({ id: v.id, head: headOf(11), card: cardOf(11) });
    expect(actionBody(v, "merge_as_is")).toEqual({ id: v.id, head: headOf(11), card: cardOf(11), action: "merge_as_is" });
    expect(actionBody(v, "fix")).toEqual({ id: v.id, head: headOf(11), card: cardOf(11) });
  });

  it("the arm key moves with what an OK answers", () => {
    const a = prArmKey(view(prRow(11)));
    expect(prArmKey(view(prRow(11)))).toBe(a);
    for (const d of [{ head: headOf(12) }, { card: cardOf(12) }, { summary: { does: "other", blocked: "" } },
                     { actions: ["merge_as_is"] }, { method: "squash" }, { hold: { note: "h" } }]) {
      expect(prArmKey(view(prRow(11, d))), JSON.stringify(d)).not.toBe(a);
    }
    const fixed = prArmKey(view(fixRow(37, "ready", { summary_state: "pending" })));
    expect(prArmKey(view(fixRow(37, "ready", { summary: "lands", summary_state: "ready" })))).not.toBe(fixed);
  });

  it("result words come from the daemon's fixed result word", () => {
    expect(resultWords("merge", { result: "merged" })).toBe("Merged.");
    expect(resultWords("merge", { result: "merged", method: "squash" })).toBe("Merged (squash merge).");
    expect(resultWords("merge", { result: "head moved" })).toBe("Not merged: head moved.");
    expect(resultOk("merge", { result: "head moved" })).toBe(false);
    expect(resultWords("update", { result: "update requested" })).toContain("Update requested");
    expect(resultWords("fix", { task_id: "t0000031abcdef" })).toBe("Fix started (task t0000031).");
    expect(resultWords("cancel", {})).toBe("Fix cancelled.");
  });
});
