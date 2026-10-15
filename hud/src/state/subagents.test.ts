import { ApiError } from "../api";
import { LAZY_MS, REPLAY_CAP, SOON_MS, THREAD_CAP, createSubagentStore, type Fetchers } from "./subagents";

const at = (s: number) => new Date(Date.parse("2026-10-15T10:00:00Z") + s * 1000).toISOString();
const listRow = (sid: string, over: Record<string, unknown> = {}) => ({
  sid, status: "running", depth: 1, background: false, name: `name ${sid}`, kind: "general-purpose",
  started_at: at(0), entries: 0, ...over,
});
const rec = (sid: string, over: Record<string, unknown> = {}) => ({
  sid, op: "progress", status: "running", depth: 1, background: false, started_at: at(0), entries: 0, ...over,
});

/** Fetchers the test answers by hand: every call waits for `answer`. */
function manual() {
  const lists: { threadId: string; resolve: (v: unknown) => void; reject: (e: unknown) => void }[] = [];
  const panes: { threadId: string; sid: string; resolve: (v: unknown) => void; reject: (e: unknown) => void }[] = [];
  const fetchers: Fetchers = {
    list: (threadId) => new Promise((resolve, reject) => lists.push({ threadId, resolve, reject })),
    transcript: (threadId, sid) => new Promise((resolve, reject) => panes.push({ threadId, sid, resolve, reject })),
  };
  return { fetchers, lists, panes };
}
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("the window's sub-agent store", () => {
  beforeEach(() => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] }));
  afterEach(() => vi.useRealTimers());
  const settle = async () => { await vi.advanceTimersByTimeAsync(0); };

  it("reads a thread's list once when first watched, and ignores records for threads nothing shows", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    s.heard("t1", rec("cl:a", { op: "started" }));
    expect(s.rows("t1")).toEqual([]);
    const stop = s.watch("t1");
    const again = s.watch("t1");
    expect(m.lists.length).toBe(1);
    m.lists[0].resolve({ subagents: [listRow("cl:a")] });
    await settle();
    expect(s.rows("t1").map((r) => [r.sid, r.name])).toEqual([["cl:a", "name cl:a"]]);
    stop(); again();
  });

  it("records heard during a read are laid over it; a stale one never takes a row back", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    s.watch("t1");
    s.heard("t1", rec("cl:a", { status: "running", entries: 1 }));     // older than the read
    s.heard("t1", rec("cl:b", { op: "started", entries: 0 }));         // newer than it
    m.lists[0].resolve({ subagents: [listRow("cl:a", { status: "done", entries: 4, ended_at: at(9) })] });
    await settle();
    expect(s.rows("t1").map((r) => [r.sid, r.status, r.entries])).toEqual([["cl:a", "done", 4], ["cl:b", "running", 0]]);
  });

  it("a new child or an end reads the list soon; progress at most every LAZY_MS", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    s.watch("t1");
    m.lists[0].resolve({ subagents: [listRow("cl:a")] });
    await settle();
    s.heard("t1", rec("cl:a", { entries: 1 }));
    s.heard("t1", rec("cl:a", { entries: 2 }));
    await vi.advanceTimersByTimeAsync(SOON_MS);
    expect(m.lists.length).toBe(1);
    await vi.advanceTimersByTimeAsync(LAZY_MS);
    expect(m.lists.length).toBe(2);
    m.lists[1].resolve({ subagents: [listRow("cl:a", { entries: 2, tokens: 99 })] });
    await settle();
    s.heard("t1", rec("cl:new", { op: "started" }));
    await vi.advanceTimersByTimeAsync(SOON_MS);
    expect(m.lists.length).toBe(3);
    expect(s.rows("t1").find((r) => r.sid === "cl:new")!.name).toBe("");
    m.lists[2].resolve({ subagents: [listRow("cl:a", { entries: 2 }), listRow("cl:new")] });
    await settle();
    expect(s.rows("t1").find((r) => r.sid === "cl:new")!.name).toBe("name cl:new");
  });

  it("a tick redraws its own row only, and leaves the placement alone", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    s.watch("t1");
    m.lists[0].resolve({ subagents: [listRow("cl:a"), listRow("cl:b")] });
    await settle();
    const heard: string[] = [];
    for (const key of ["r:t1|cl:a", "r:t1|cl:b", "a:t1", "t:t1"]) s.subscribe(key, () => heard.push(key));
    const anchors = s.anchors("t1");
    const b = s.row("t1", "cl:b");
    s.heard("t1", rec("cl:a", { entries: 3 }));
    expect(heard).toEqual(["r:t1|cl:a", "t:t1"]);
    expect(s.anchors("t1")).toBe(anchors);
    expect(s.row("t1", "cl:b")).toBe(b);
    heard.length = 0;
    s.heard("t1", rec("cl:c", { op: "started", started_at: at(4) }));
    expect(heard).toEqual(["a:t1", "t:t1"]);
  });

  it("a reconnect reads every watched thread and pane again; unwatched ones on their next watch", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    const stop = s.watch("t1");
    s.watch("t2");
    m.lists.forEach((l) => l.resolve({ subagents: [listRow("cl:a")] }));
    await settle();
    const pane = s.watchPane("t2", "cl:a");
    expect(m.panes.length).toBe(1);
    m.panes[0].resolve({ subagent: listRow("cl:a"), messages: [] });
    await settle();
    stop();
    s.reconnect();
    expect(m.lists.map((l) => l.threadId)).toEqual(["t1", "t2", "t2"]);
    expect(m.panes.length).toBe(2);
    // After a restart the daemon reads its live children back stopped.
    m.lists[2].resolve({ subagents: [listRow("cl:a", { status: "stopped", stop_reason: "the session that ran it ended" })] });
    await settle();
    expect(s.row("t2", "cl:a")!.status).toBe("stopped");
    s.watch("t1");
    expect(m.lists.map((l) => l.threadId)).toEqual(["t1", "t2", "t2", "t1"]);
    pane();
  });

  it("a read's refusal is said by kind: gone, an older daemon, not the owner", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    s.watch("t1");
    m.lists[0].reject(new ApiError(400, "unknown"));
    await settle();
    expect(s.failed("t1")).toBe("old");
    s.watchPane("t1", "cl:a");
    m.panes[0].reject(new ApiError(404, "no such sub-agent"));
    await settle();
    expect(s.pane("t1", "cl:a")).toEqual({ data: null, failed: "gone", loading: false });
    s.watchPane("t1", "cl:b");
    m.panes[1].reject(new ApiError(403, "only the owner"));
    await settle();
    expect(s.pane("t1", "cl:b").failed).toBe("refused");
  });

  it("a shown child's transcript is read again behind each record about it, and dropped when hidden", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    s.watch("t1");
    m.lists[0].resolve({ subagents: [listRow("cl:a"), listRow("cl:b")] });
    await settle();
    const hide = s.watchPane("t1", "cl:a");
    m.panes[0].resolve({ subagent: listRow("cl:a"), brief: "B", messages: [{ role: "assistant", text: "hi", seq: 1, at: at(1) }] });
    await settle();
    expect(s.pane("t1", "cl:a").data!.items.length).toBe(1);
    expect(s.pane("t1", "cl:a").data!.brief).toBe("B");
    s.heard("t1", rec("cl:b", { entries: 1 }));
    s.heard("t1", rec("cl:a", { entries: 1 }));
    s.heard("t1", rec("cl:a", { entries: 2 }));
    await vi.advanceTimersByTimeAsync(SOON_MS);
    expect(m.panes.map((p) => p.sid)).toEqual(["cl:a", "cl:a"]);
    hide();
    expect(s.snapshot().panes).toEqual([]);
    s.heard("t1", rec("cl:a", { entries: 3 }));
    await vi.advanceTimersByTimeAsync(SOON_MS * 4);
    expect(m.panes.length).toBe(2);
  });

  it("is bounded: threads, and records waiting on a read", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    const stops = Array.from({ length: THREAD_CAP + 5 }, (_, i) => s.watch(`t${i}`));
    expect(Object.keys(s.snapshot().threads).length).toBe(THREAD_CAP + 5);   // all watched: kept
    stops.slice(0, 10).forEach((stop) => stop());
    expect(Object.keys(s.snapshot().threads).length).toBe(THREAD_CAP);
    expect(Object.keys(s.snapshot().threads)).not.toContain("t0");
    // A read on its way, and more records than it can carry: read again.
    const w = s.watch("big");
    for (let i = 0; i < REPLAY_CAP + 10; i++) s.heard("big", rec("cl:a", { entries: i }));
    m.lists.find((l) => l.threadId === "big")!.resolve({ subagents: [] });
    await settle();
    await vi.advanceTimersByTimeAsync(SOON_MS);
    expect(m.lists.filter((l) => l.threadId === "big").length).toBe(2);
    w();
    await flush;
  });

  it("the snapshot hook carries ids, statuses and counts, never a word", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    s.watch("t1");
    m.lists[0].resolve({ subagents: [listRow("cl:a", { name: "SECRET NAME", last_tool: "SECRET TOOL" })] });
    await settle();
    expect(JSON.stringify(s.snapshot())).not.toMatch(/SECRET/);
  });
});
