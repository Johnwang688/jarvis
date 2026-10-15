import {
  CHILD_CAP, COLLAPSE_OVER, applyBus, blockTitle, bound, childrenOf, cleanBus, cleanList, cleanRow, cleanTranscript,
  elapsedOf, foldList, parseRef, placeRows, placeableItem, placeableOf, statusMark, tokensText, toolsText,
  type SubBus, type SubStatus, type Subagent,
} from "./subagents";
import type { ChatMessage } from "../types";

const T0 = Date.parse("2026-10-15T10:00:00Z");
const at = (s: number) => new Date(T0 + s * 1000).toISOString();
const bus = (sid: string, over: Partial<SubBus> = {}): SubBus => ({
  sid, op: "progress", status: "running", depth: 1, background: false, started_at: at(0), entries: 0, ...over,
});
const row = (sid: string, over: Partial<Subagent> = {}): Subagent => ({
  sid, status: "running", depth: 1, background: false, name: "n", kind: "", started_at: at(0), entries: 0, ...over,
});

describe("what the daemon says, cleaned", () => {
  it("a list row keeps only known fields, as text, capped; a bad sid is no row", () => {
    const r = cleanRow({
      sid: "cl:toolu_01", status: "done", depth: 1, background: false, entries: 3,
      name: "<img src=x onerror=1>‮Fix\u0000 the \n test" + "x".repeat(80), kind: "general-purpose",
      started_at: at(0), ended_at: at(5), tokens: 1234, tool_uses: 2, last_tool: "Bash", declined: 1,
      args: { command: "rm -rf /" }, summary: "SECRET", brief: "BRIEF",
    })!;
    expect(Array.from(r.name).length).toBe(40);
    expect(r.name).not.toMatch(/[‮\u0000\n]/);
    expect(r.name.startsWith("<img src=x onerror=1> Fix the test")).toBe(true);
    expect(Object.keys(r).sort()).toEqual(["background", "declined", "depth", "ended_at", "entries", "kind",
      "last_tool", "name", "sid", "started_at", "status", "tokens", "tool_uses"].sort());
    for (const sid of ["toolu_01", "cl:", "xx:abc", "cl:a b", "cl:" + "a".repeat(129), 7, null]) {
      expect(cleanRow({ sid, status: "done" })).toBeNull();
    }
    expect(cleanRow({ sid: "cx:t1", status: "weird" })!.status).toBe("stopped");
    expect(cleanRow({ sid: "cx:t1", status: "done", parent_sid: "cx:t1" })!.parent_sid).toBeUndefined();
  });

  it("a bus record is ids, a status, times and counts; anything else is not one", () => {
    const rec = cleanBus({ sid: "cx:abc", op: "finished", status: "failed", entries: 2, depth: 2,
      parent_sid: "cx:p", started_at: at(0), ended_at: at(3), name: "LEAK", text: "LEAK" })!;
    expect(rec).toEqual({ sid: "cx:abc", op: "finished", status: "failed", depth: 2, background: false,
      started_at: at(0), entries: 2, parent_sid: "cx:p", ended_at: at(3) });
    expect(cleanBus({ sid: "cx:abc", status: "nope" })).toBeNull();
    expect(cleanBus({ sid: "abc", status: "done" })).toBeNull();
    expect(cleanBus({ sid: "cl:x", status: "done", op: "weird" })!.op).toBe("progress");
  });

  it("maps every status to a word and its Graphite tone", () => {
    const seen = (["running", "backgrounded", "done", "failed", "stopped"] as SubStatus[]).map(statusMark);
    expect(seen).toEqual([
      { word: "running", tone: "live" }, { word: "backgrounded", tone: "live" }, { word: "done", tone: "done" },
      { word: "failed", tone: "failed" }, { word: "stopped", tone: "stopped" },
    ]);
  });

  it("says counts, tokens and time plainly", () => {
    expect([toolsText(undefined), toolsText(1), toolsText(3)]).toEqual(["", "1 tool", "3 tools"]);
    expect([tokensText(950), tokensText(1234), tokensText(18200), tokensText(2_500_000)])
      .toEqual(["950 tokens", "1.2k tokens", "18k tokens", "2.5M tokens"]);
    expect(elapsedOf(row("cl:a", { started_at: at(0) }), T0 + 134_000)).toBe("2m 14s");
    expect(elapsedOf(row("cl:a", { status: "done", ended_at: at(9) }), T0 + 999_000)).toBe("9s");
    expect(elapsedOf(row("cl:a", { status: "done", started_at: "", duration_ms: 4000 }), 0)).toBe("4s");
  });
});

describe("the bus and the list, folded", () => {
  it("a new sid is a nameless row; a change is a new row object, no change is the same array", () => {
    let rows = applyBus([], bus("cl:a", { op: "started" }));
    expect(rows).toEqual([row("cl:a", { name: "" })]);
    const same = applyBus(rows, bus("cl:a", { op: "started" }));
    expect(same).toBe(rows);
    rows = applyBus(rows, bus("cl:a", { entries: 2 }));
    expect(rows[0].entries).toBe(2);
  });

  it("a stale record never takes a row back: fewer entries, or a live word after the end", () => {
    // A live row is not taken back to an older live word either (a read newer than the record).
    const live = [row("cl:a", { status: "backgrounded", entries: 5, background: true })];
    expect(applyBus(live, bus("cl:a", { entries: 3, status: "running" }))).toBe(live);
    const ended = [row("cl:a", { status: "done", entries: 5, ended_at: at(9), stop_reason: undefined })];
    expect(applyBus(ended, bus("cl:a", { entries: 4, status: "running" }))).toBe(ended);
    expect(applyBus(ended, bus("cl:a", { entries: 5, status: "running" }))).toBe(ended);
    // Claude has no second turn: even its own "started" does not revive.
    expect(applyBus(ended, bus("cl:a", { entries: 5, status: "running", op: "started" }))).toBe(ended);
    // Codex's own "started" for a child given another turn does (SV-2).
    const codex = [row("cx:a", { status: "done", entries: 5, ended_at: at(9), stop_reason: "x" })];
    const revived = applyBus(codex, bus("cx:a", { entries: 5, status: "running", op: "started" }));
    expect(revived[0].status).toBe("running");
    expect(revived[0].ended_at).toBeUndefined();
    expect(revived[0].stop_reason).toBeUndefined();
    expect(applyBus(codex, bus("cx:a", { entries: 5, status: "running", op: "progress" }))).toBe(codex);
  });

  it("an end replaces an inferred one, and carries its time", () => {
    const rows = [row("cl:a", { status: "stopped", entries: 2, ended_at: at(3) })];
    const done = applyBus(rows, bus("cl:a", { status: "done", op: "finished", entries: 3, ended_at: at(4) }));
    expect([done[0].status, done[0].ended_at]).toEqual(["done", at(4)]);
  });

  it("keeps at most 64 children: the oldest ended one goes first", () => {
    const many = Array.from({ length: CHILD_CAP }, (_, i) => row(`cl:c${i}`, { status: i === 5 ? "done" : "running" }));
    const next = applyBus(many, bus("cl:new"));
    expect(next.length).toBe(CHILD_CAP);
    expect(next.some((r) => r.sid === "cl:c5")).toBe(false);
    const allLive = bound([...many.map((r) => ({ ...r, status: "running" as const })), row("cl:x")]);
    expect(allLive[0].sid).toBe("cl:c1");
    expect(cleanList({ subagents: [...many, ...many.map((r) => ({ ...r, sid: r.sid + "b" }))] }).length).toBe(CHILD_CAP);
  });

  // The window's two sources must agree with the daemon whatever the order:
  // every record in order (live), or a reconnect that missed a gap and read
  // the list while records kept coming (fold).
  it("live equals the daemon, and so does any reconnect's read laid under its replay (1000 runs)", () => {
    let seed = 7;
    const rand = (n: number) => { seed = (seed * 1103515245 + 12345) % 2 ** 31; return seed % n; };
    for (let run = 0; run < 1000; run++) {
      const daemon = new Map<string, Subagent>();
      const records: SubBus[] = [];
      const snapshots: Subagent[][] = [];
      const steps = 5 + rand(40);
      for (let i = 0; i < steps; i++) {
        const sid = (rand(2) ? "cl:" : "cx:") + "s" + rand(5);
        const was = daemon.get(sid);
        let next: Subagent;
        let op: SubBus["op"] = "progress";
        if (!was) { next = row(sid, { name: `n-${sid}`, started_at: at(i) }); op = "started"; }
        else if (rand(4) === 0 && was.status !== "done" && was.status !== "failed" && was.status !== "stopped") {
          next = { ...was, status: (["done", "failed", "stopped"] as const)[rand(3)], ended_at: at(i) }; op = "finished";
        } else if (rand(6) === 0 && sid.startsWith("cx:") && !["running", "backgrounded"].includes(was.status)) {
          next = { ...was, status: "running" }; delete next.ended_at; op = "started";
        } else if (["running", "backgrounded"].includes(was.status)) {
          next = { ...was, entries: was.entries + rand(3), status: rand(5) ? was.status : "backgrounded" };
        } else next = { ...was, entries: was.entries + 1 };
        daemon.set(sid, next);
        records.push({ sid, op, status: next.status, depth: 1, background: false, started_at: next.started_at,
          entries: next.entries, ...(next.ended_at ? { ended_at: next.ended_at } : {}) });
        snapshots.push([...daemon.values()].map((r) => ({ ...r })));
      }
      const want = [...daemon.values()].map((r) => [r.sid, r.status, r.entries, r.ended_at ?? null]);
      const key = (rows: Subagent[]) => rows.map((r) => [r.sid, r.status, r.entries, r.ended_at ?? null]);
      let live: Subagent[] = [];
      for (const rec of records) live = applyBus(live, rec);
      expect(key(live)).toEqual(want);
      // A reconnect: records from `from` on were heard while a list read
      // taken at `taken` (>= from - 1) was on its way.
      const from = rand(records.length);
      const taken = Math.min(records.length - 1, from + rand(3));
      const folded = foldList(snapshots[taken].map((r) => ({ ...r })), records.slice(from));
      expect(key(folded)).toEqual(want);
    }
  });
});

describe("where rows sit", () => {
  const msgs: ChatMessage[] = [
    { role: "user", text: "go", at: at(0) },
    { role: "tool", text: "", turn_id: "t", calls: [
      { call_id: "toolu_a", turn_id: "t", name: "Agent", label: "a", outcome: "ok", at: at(1) },
      { call_id: "toolu_b", turn_id: "t", name: "Agent", label: "b", outcome: "ok", at: at(1) }] },
    { role: "assistant", text: "launched", at: at(2) },
    { role: "user", text: "more", at: at(10) },
  ];
  it("a Claude child after its spawn call's entry; a Codex child by time; nothing timed, the end", () => {
    const placed = placeRows(msgs.map(placeableOf), [
      row("cl:toolu_a", { started_at: at(50) }), row("cl:toolu_b"), row("cx:t1", { started_at: at(5) }),
      row("cx:t0", { started_at: at(-5) }), row("cl:toolu_gone", { started_at: at(11) }),
    ]);
    expect([...placed.entries()].map(([i, rows]) => [i, rows.map((r) => r.sid)])).toEqual([
      [1, ["cl:toolu_a", "cl:toolu_b"]], [2, ["cx:t1"]], [-1, ["cx:t0"]], [3, ["cl:toolu_gone"]],
    ]);
    const untimed = placeRows([{ callIds: [] }, { callIds: [] }], [row("cx:t1")]);
    expect([...untimed.keys()]).toEqual([1]);
    expect([...placeRows([], [row("cx:t1")]).keys()]).toEqual([-1]);
  });

  it("the parent shows the top level; a nested child only in its parent's pane", () => {
    const rows = [row("cl:a"), row("cl:b", { parent_sid: "cl:a", depth: 2 }), row("cl:c", { parent_sid: "cl:gone" })];
    expect(childrenOf(rows, null).map((r) => r.sid)).toEqual(["cl:a", "cl:c"]);
    expect(childrenOf(rows, "cl:a").map((r) => r.sid)).toEqual(["cl:b"]);
  });

  it("more than six at one spot collapse into one line that counts the live ones", () => {
    expect(COLLAPSE_OVER).toBe(6);
    const rows = [...Array(8)].map((_, i) => ({ status: (i < 5 ? "running" : "done") as SubStatus }));
    expect(blockTitle(rows)).toBe("Sub-agents · 8 (5 running)");
    expect(blockTitle(rows.slice(5))).toBe("Sub-agents · 3");
  });
});

describe("a child's transcript", () => {
  it("texts, grouped calls by label and outcome, the closing word, a decline by name; never args", () => {
    const t = cleanTranscript({
      subagent: { sid: "cl:a", status: "done", name: "alpha" },
      brief: "do it " + "b".repeat(5000),
      truncated: true,
      messages: [
        { role: "tool", call_id: "c1", name: "Bash", label: "echo 1", outcome: "ok", seq: 1, at: at(1), args: { x: 1 }, summary: "RES" },
        { role: "tool", call_id: "c2", name: "Read", label: "a.ts", outcome: "weird", seq: 2, at: at(2) },
        { role: "assistant", text: "**done**", seq: 3, at: at(3) },
        { role: "declined", name: "Bash", seq: 4, at: at(4) },
        { role: "tool", call_id: "bad id!", name: "x", seq: 5 },
        { role: "result", text: "final", status: "done", seq: 6, at: at(6) },
        { role: "thinking", text: "never" },
      ],
    }, "cl:a")!;
    expect(t.items.map((i) => i.kind)).toEqual(["tools", "text", "declined", "result"]);
    const calls = (t.items[0] as Extract<typeof t.items[0], { kind: "tools" }>).group.calls!;
    expect(calls.map((c) => [c.call_id, c.label, c.outcome])).toEqual([["c1", "echo 1", "ok"], ["c2", "a.ts", "failed"]]);
    expect(JSON.stringify(t)).not.toMatch(/RES|"args"|"summary"|never/);
    expect(Array.from(t.brief).length).toBe(4000);
    expect(t.truncated).toBe(true);
    expect(t.subagent!.name).toBe("alpha");
    expect(placeableItem(t.items[0])).toEqual({ callIds: ["c1", "c2"], at: at(1) });
    expect(cleanTranscript("nope", "cl:a")).toBeNull();
    expect(cleanTranscript({ messages: [] }, "cl:a")!.brief).toBe("");
  });

  it("a stored pane reference is a thread id and a sid, or nothing", () => {
    expect(parseRef({ threadId: "t1", sid: "cl:a", extra: 1 })).toEqual({ threadId: "t1", sid: "cl:a" });
    for (const bad of [null, {}, { threadId: "t1" }, { threadId: "t 1", sid: "cl:a" }, { threadId: "t1", sid: "a" },
      { threadId: "x".repeat(201), sid: "cl:a" }]) expect(parseRef(bad)).toBeNull();
  });
});
