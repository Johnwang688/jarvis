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

  it("a thread nothing shows any more takes no work, and is read whole when shown again", async () => {
    const m = manual();
    const s = createSubagentStore(m.fetchers);
    const stop = s.watch("t1");
    m.lists[0].resolve({ subagents: [listRow("cl:a")] });
    await settle();
    stop();
    const heard: string[] = [];
    s.subscribe("t:t1", () => heard.push("t"));
    s.heard("t1", rec("cl:a", { status: "done", op: "finished", entries: 2 }));
    s.heard("t1", rec("cl:new", { op: "started" }));
    await vi.advanceTimersByTimeAsync(LAZY_MS * 2);
    expect(heard).toEqual([]);
    expect(m.lists.length).toBe(1);
    expect(s.rows("t1").map((r) => r.status)).toEqual(["running"]);
    s.watch("t1");
    expect(m.lists.length).toBe(2);
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
    // Progress: at most every LAZY_MS (review of #212, LOW-3).
    await vi.advanceTimersByTimeAsync(SOON_MS);
    expect(m.panes.length).toBe(1);
    await vi.advanceTimersByTimeAsync(LAZY_MS);
    expect(m.panes.map((p) => p.sid)).toEqual(["cl:a", "cl:a"]);
    m.panes[1].resolve({ subagent: listRow("cl:a"), messages: [] });
    await settle();
    // An end: soon.
    s.heard("t1", rec("cl:a", { op: "finished", status: "done", entries: 3, ended_at: at(9) }));
    await vi.advanceTimersByTimeAsync(SOON_MS);
    expect(m.panes.length).toBe(3);
    hide();
    expect(s.snapshot().panes).toEqual([]);
    s.heard("t1", rec("cl:a", { entries: 4 }));
    await vi.advanceTimersByTimeAsync(LAZY_MS * 2);
    expect(m.panes.length).toBe(3);
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

  describe("the owner's Stop (SV-4)", () => {
    /** A store whose list says `rows`, and whose Stop requests wait for the test. */
    async function ready(rows: unknown[]) {
      const m = manual();
      const posts: { threadId: string; sid: string; resolve: (v: unknown) => void; reject: (e: unknown) => void }[] = [];
      const s = createSubagentStore({ ...m.fetchers,
        stop: (threadId, sid) => new Promise((resolve, reject) => posts.push({ threadId, sid, resolve, reject })) });
      s.watch("t1");
      m.lists[0].resolve({ subagents: rows });
      await settle();
      return { s, posts };
    }

    it("one press is one request; \"stopping…\" from the press until the daemon's word", async () => {
      const { s, posts } = await ready([listRow("cl:a", { stoppable: true })]);
      void s.stop("t1", "cl:a");
      void s.stop("t1", "cl:a");                    // pressed again while on its way
      expect(posts.map((p) => p.sid)).toEqual(["cl:a"]);
      expect(s.stopUi("t1", "cl:a").pending).toBe(true);
      posts[0].resolve({ ok: true, stop: "stopping" });
      await settle();
      expect(s.stopUi("t1", "cl:a")).toEqual({ pending: false, error: "" });
      expect(s.row("t1", "cl:a")!.stop).toBe("stopping");
      void s.stop("t1", "cl:a");                    // the row says stopping: nothing more
      expect(posts.length).toBe(1);
      s.heard("t1", rec("cl:a", { op: "finished", status: "stopped", ended_at: at(5) }));
      expect(s.row("t1", "cl:a")!.stop).toBeUndefined();
      void s.stop("t1", "cl:a");                    // ended: nothing
      expect(posts.length).toBe(1);
    });

    it("nothing is sent under a card, for a row that offers no Stop, or with no route", async () => {
      const { s, posts } = await ready([listRow("cl:a", { stoppable: true }),
        listRow("cl:b", { stoppable: false, stop_unavailable: "Not by itself." }), listRow("cl:c")]);
      s.setBlocked(true);
      expect(s.isBlocked()).toBe(true);
      void s.stop("t1", "cl:a");
      s.setBlocked(false);
      void s.stop("t1", "cl:b");
      void s.stop("t1", "cl:c");
      void s.stop("t1", "cl:nobody");
      expect(posts).toEqual([]);
      const bare = createSubagentStore(manual().fetchers);
      await bare.stop("t1", "cl:a");                 // a store with no Stop route
    });

    it("a refusal keeps the daemon's sentence; an unconfirmed one marks the row", async () => {
      const { s, posts } = await ready([listRow("cl:a", { stoppable: true }), listRow("cx:b", { stoppable: true })]);
      void s.stop("t1", "cl:a");
      posts[0].reject(Object.assign(new Error("Claude Code can stop this sub-agent only by stopping the whole turn."),
        { status: 409, body: { supported: false } }));
      await settle();
      expect(s.stopUi("t1", "cl:a")).toEqual({ pending: false,
        error: "Claude Code can stop this sub-agent only by stopping the whole turn." });
      expect(s.row("t1", "cl:a")!.stop).toBeUndefined();
      void s.stop("t1", "cx:b");
      posts[1].reject(Object.assign(new Error("The stop was sent, but the CLI did not confirm it took it."),
        { status: 409, body: { unconfirmed: true } }));
      await settle();
      expect(s.row("t1", "cx:b")!.stop).toBe("unconfirmed");
      void s.stop("t1", "cx:b");                    // unconfirmed: Stop may be pressed again
      expect(posts.length).toBe(3);
      posts[2].reject(Object.assign(new Error("The CLI refused the stop: No task found."),
        { status: 409, body: { refused: true } }));
      await settle();
      // The CLI said no: a mark with its reason, no error line, and no Stop again (review of #216).
      expect([s.row("t1", "cx:b")!.stop, s.row("t1", "cx:b")!.stop_error]).toEqual(["refused", "No task found."]);
      expect(s.stopUi("t1", "cx:b")).toEqual({ pending: false, error: "" });
      void s.stop("t1", "cx:b");
      expect(posts.length).toBe(3);
    });
  });
});

