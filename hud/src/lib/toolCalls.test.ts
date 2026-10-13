import { applyEvent, applyFinish, applyStart, fold, groupTitle, stopTurn, toolLabel, type TranscriptEvent } from "./toolCalls";
import { toolName } from "./thinking";
import type { ChatMessage, ToolCall, ToolEntry } from "../types";
const call = (id: string, turn = "t"): ToolCall => ({ call_id: id, turn_id: turn, name: "Read", label: "src/a.ts", at: "2026-10-13T00:00:00Z", outcome: "running" });

describe("tool transcript rule", () => {
  it("groups consecutive calls only within a turn; all message roles split", () => {
    for (const role of ["assistant", "user", "system"] as const) {
      let e = applyStart([], call("1"));
      e = applyStart(e, call("2"));
      e = applyEvent(e, { kind: "message", message: { role, text: "boundary" } });
      e = applyStart(e, call("3"));
      e = applyStart(e, call("1", "other"));
      expect(e.map(x => x.calls?.length ?? x.role)).toEqual([2, role, 1, 1]);
      const done = applyFinish(e, { kind: "finish", call_id: "1", turn_id: "t", outcome: "denied", at: "done" });
      expect(done[0].calls![0].outcome).toBe("denied");
      expect(done[3].calls![0].outcome).toBe("running");
      expect(done[1]).toBe(e[1]); expect(done[2]).toBe(e[2]);
      expect(applyFinish(e, { kind: "finish", call_id: "missing", turn_id: "t", outcome: "ok", at: "done" })).toBe(e);
      expect(stopTurn(e, "t", "end")[3]).toBe(e[3]);
    }
  });
  it("deduplicates overlapping history and replay without reviving stopped calls", () => {
    const row: ToolEntry = { role: "tool", ...call("1") };
    const message: ChatMessage = { role: "user", text: "steer", message_id: "m", at: "same" };
    const events: TranscriptEvent[] = [{ kind: "start", call: call("1") }, { kind: "message", message },
      { kind: "start", call: call("2") }, { kind: "stop", turn_id: "t", at: "end" }];
    expect(fold([row, message], events)).toEqual(fold([], events));
    expect(fold([row], events).flatMap(e => e.calls ?? []).map(c => c.outcome)).toEqual(["stopped", "stopped"]);
  });
  it("never lets an older replayed finish overwrite a newer history finish", () => {
    const rows: ToolEntry[] = [{ role: "tool", ...call("1"), outcome: "ok", finished_at: "2026-10-13T00:00:03Z" }];
    const result = fold(rows, [{ kind: "finish", call_id: "1", turn_id: "t", outcome: "failed", at: "2026-10-13T00:00:02Z" }]);
    expect(result[0].calls![0].outcome).toBe("ok");
    expect(stopTurn(result, "t", "2026-10-13T00:00:02Z")).toBe(result);
    expect(stopTurn(result, "t", "2026-10-13T00:00:04Z")).toBe(result);
  });
  it("uses singular/plural, continuous running wording and outcome suffix counts", () => {
    let e = applyStart([], call("1"));
    expect(groupTitle(e[0], false).text).toBe("Used 1 tool");
    e = applyStart(e, { ...call("2"), outcome: "failed" });
    e = applyStart(e, { ...call("3"), outcome: "denied" });
    e = applyStart(e, { ...call("4"), outcome: "stopped" });
    expect(groupTitle(e[0], true)).toEqual({ text: "Using tools… 4", failed: 1, denied: 1, stopped: 1 });
    expect(groupTitle(e[0], false).text).toBe("Used 4 tools");
    expect(JSON.stringify(groupTitle(e[0], false))).not.toMatch(/Authorize|Approve|Deny/);
  });
  it("cleans both names and labels as bounded text, without interpreting markup", () => {
    expect(toolLabel("a\n\u202Eb <img src=x onerror=boom>")).toBe("a b <img src=x onerror=boom>");
    expect(Array.from(toolLabel("😀".repeat(200))).length).toBe(120);
    expect(Array.from(toolName("n".repeat(100))).length).toBe(40);
  });
  it("matches history folding over 1000 seeded event streams with steers, queues and compacts", () => {
    let seed = 941;
    const rand = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32;
    for (let run = 0; run < 1000; run++) {
      let live: ChatMessage[] = [], turn = 0, serial = 0;
      const rows: (ChatMessage | ToolEntry)[] = [];
      const pending = new Map<string, ToolEntry>();
      for (let step = 0; step < 30; step++) {
        const n = rand();
        if (n < .45) {
          const c = { ...call(String(serial++ % 5), String(turn)), name: n < .05 ? "compact" : "Read" };
          // Ids can repeat in later turns, but not within a single provider turn.
          c.call_id += `-${serial}`;
          const row: ToolEntry = { role: "tool", ...c }; rows.push(row); pending.set(c.call_id, row);
          live = applyStart(live, c);
        } else if (n < .65 && pending.size) {
          const row = [...pending.values()][Math.floor(rand() * pending.size)];
          row.outcome = rand() < .5 ? "ok" : "denied"; row.finished_at = "finish";
          live = applyFinish(live, { kind: "finish", call_id: row.call_id, turn_id: row.turn_id, outcome: row.outcome, at: "finish" });
          pending.delete(row.call_id);
        } else if (n < .85) {
          const msg: ChatMessage = { role: (["user", "assistant", "system"] as const)[Math.floor(rand() * 3)], text: `boundary ${step}` };
          rows.push(msg); live = applyEvent(live, { kind: "message", message: msg });
        } else {
          live = stopTurn(live, String(turn), "stop");
          for (const row of pending.values()) if (row.turn_id === String(turn)) { row.outcome = "stopped"; row.finished_at = "stop"; pending.delete(row.call_id); }
          turn++;
        }
      }
      expect(fold(rows)).toEqual(live);
    }
  });
});
