import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DevServerStore, devId, parseDevEvent, parseDevSource, type DevTransport } from "./devservers";

const WIN = "a".repeat(32), ID = "ds_12345678", URL = "http://localhost:5173/";
const source = { kind: "terminal", terminal_id: "term1" };
const listed = (id = ID, extra: Record<string, unknown> = {}) => ({ id, url: URL, source, state: "up", claimed: false, mine: false, ...extra });
const event = (id = ID, state = "up") => ({ kind: "dev_server", data: { id, state, source, at: "2026-10-13T00:00:00Z" } });
function deferred<T = unknown>() {
  let resolve!: (v: T) => void, reject!: (e: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
let stores: DevServerStore[];
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(1_000_000); stores = []; });
afterEach(() => { stores.forEach(s => s.disconnect()); vi.useRealTimers(); });
function setup() {
  const wire = {
    list: vi.fn<DevTransport["list"]>().mockResolvedValue({ auto_open: true, servers: [] }),
    claim: vi.fn<DevTransport["claim"]>().mockResolvedValue({ won: true, url: URL }),
    act: vi.fn<DevTransport["act"]>().mockResolvedValue({}),
  };
  const store = new DevServerStore(WIN, wire); stores.push(store);
  return { store, wire };
}

describe("owner wire parsing", () => {
  it("requires the fixed dev ID and valid source fields", () => {
    expect(devId(ID)).toBe(true);
    for (const id of ["ds_1234567", "ds_123456789", "ds_ABCDEFFF", "<b>", null]) expect(devId(id)).toBe(false);
    expect(parseDevSource(source)).toEqual({ kind: "terminal", id: "term1" });
    expect(parseDevSource({ kind: "thread", thread_id: "th1", task_id: "task1" })).toEqual({ kind: "task", id: "task1" });
    expect(parseDevSource({ kind: "thread", thread_id: "th1", task_id: null })).toEqual({ kind: "thread", id: "th1" });
    for (const raw of [null, {}, { kind: "terminal", terminal_id: "" }, { kind: "thread", thread_id: "a", task_id: 4 }]) expect(parseDevSource(raw)).toBeNull();
    expect(parseDevEvent({ ...event().data, url: "https://untrusted.example/" })).not.toHaveProperty("url");
    expect(parseDevEvent({ ...event().data, state: "other" })).toBeNull();
  });
  it("fails closed without crypto identity or effective preference", async () => {
    const { wire } = setup();
    const store = new DevServerStore("a".repeat(16), wire); stores.push(store);
    store.connect(); await flush(); expect(wire.list).not.toHaveBeenCalled();
    const next = setup();
    next.wire.list.mockResolvedValue({ servers: [listed()] });
    next.store.connect(); await flush();
    next.store.event(event()); await flush();
    expect(next.wire.claim).not.toHaveBeenCalled(); expect(next.store.rows()).toEqual([]);
  });
});

describe("bootstrap and live events", () => {
  it("shows old unclaimed servers as quiet notices and only owner Open claims", async () => {
    const { store, wire } = setup();
    wire.list.mockResolvedValue({ auto_open: true, servers: [listed()] });
    store.connect(); await flush();
    expect(store.rows()[0].phase).toBe("notice"); expect(wire.claim).not.toHaveBeenCalled();
    store.shows = s => s.kind === "terminal" && s.id === "term1"; store.focus(12345);
    await store.claim(ID);
    expect(wire.claim).toHaveBeenCalledWith(ID, { window: WIN, shows: true, focused_at: 12345 });
    expect(store.rows()[0].phase).toBe("queued");
  });
  it("preserves live provenance while the initial snapshot is in flight", async () => {
    const { store, wire } = setup(), list = deferred();
    wire.list.mockReturnValueOnce(list.promise);
    store.connect(); store.event(event()); expect(wire.claim).not.toHaveBeenCalled();
    list.resolve({ auto_open: true, servers: [] }); await flush();
    expect(wire.claim).toHaveBeenCalledTimes(1); expect(store.rows()[0].phase).toBe("queued");
  });
  it("a duplicate live event has one arbitration and one queued placement", async () => {
    const { store, wire } = setup(), claim = deferred();
    wire.claim.mockReturnValueOnce(claim.promise);
    store.connect(); await flush();
    store.event(event()); store.event(event()); store.event(event(ID, "claimed"));
    expect(wire.claim).toHaveBeenCalledTimes(1);
    claim.resolve({ won: true, url: URL }); await flush();
    expect(store.rows().map(e => e.phase)).toEqual(["queued"]);
  });
  it("a notice that loses arbitration says opened elsewhere without placement", async () => {
    const { store, wire } = setup();
    wire.list.mockResolvedValue({ auto_open: true, servers: [listed()] }); wire.claim.mockResolvedValue({ won: false });
    store.connect(); await flush(); await store.claim(ID);
    expect(store.rows()[0].phase).toBe("lost");
    const mutate = vi.fn(); await store.attempt(mutate); expect(mutate).not.toHaveBeenCalled();
  });
  it("malformed bus data is consumed before chat routing without claiming", async () => {
    const { store, wire } = setup(); store.connect(); await flush();
    expect(store.event({ kind: "dev_server", data: { id: ID, url: URL } })).toBe(true);
    expect(store.event({ kind: "user_message", data: {} })).toBe(false);
    expect(wire.claim).not.toHaveBeenCalled();
  });
});

describe("terminal receipts", () => {
  it("uses a valid current-socket preclaim without claiming a second time", async () => {
    const { store, wire } = setup(); store.connect(); await flush();
    store.terminal("term1", URL, ID); await flush();
    expect(wire.claim).not.toHaveBeenCalled();
    const action = vi.fn().mockReturnValue("placed");
    await store.attempt(action);
    expect(action).toHaveBeenCalledOnce();
    expect(wire.act.mock.calls.map(c => c[1])).toEqual(["renew", "placed"]);
    expect(store.rows()).toEqual([]);
  });
  it("legacy idless frames reconcile source+origin and use normal arbitration", async () => {
    const { store, wire } = setup(); store.connect(); await flush();
    wire.list.mockResolvedValue({ auto_open: true, servers: [listed()] });
    store.terminal("wrong-term", URL); await flush(); expect(wire.claim).not.toHaveBeenCalled();
    store.terminal("term1", "http://localhost:9999/"); await flush(); expect(wire.claim).not.toHaveBeenCalled();
    store.terminal("term1", URL); await flush(); expect(wire.claim).toHaveBeenCalledTimes(1);
  });
  it("an old daemon without registry support cannot turn idless output into a claim", async () => {
    const { store, wire } = setup(); wire.list.mockRejectedValue(Error("404"));
    store.connect(); await flush(); store.terminal("term1", URL); await flush();
    expect(store.rows()).toEqual([]); expect(wire.claim).not.toHaveBeenCalled();
  });
});

describe("ownership fences and leases", () => {
  it.each(["down", "dismissed", "placed"])("invalidates a late winner after %s", async state => {
    const { store, wire } = setup(), claim = deferred(); wire.claim.mockReturnValueOnce(claim.promise);
    store.connect(); await flush(); store.event(event()); store.event(event(ID, state));
    claim.resolve({ won: true, url: URL }); await flush();
    const mutate = vi.fn(); await store.attempt(mutate);
    expect(store.rows()).toEqual([]); expect(mutate).not.toHaveBeenCalled();
  });
  it("rejects old claim and listing replies after disconnect/reconnect", async () => {
    const { store, wire } = setup(), claim = deferred(), oldList = deferred();
    store.connect(); await flush(); wire.claim.mockReturnValueOnce(claim.promise); store.event(event());
    wire.list.mockReturnValueOnce(oldList.promise); void store.refresh();
    store.disconnect(); store.connect(); await flush();
    claim.resolve({ won: true, url: URL }); oldList.resolve({ auto_open: true, servers: [listed()] }); await flush();
    expect(store.rows()).toEqual([]);
  });
  it("does not apply an old successful renew after loss during a card wait", async () => {
    const { store, wire } = setup(), renew = deferred();
    store.connect(); await flush(); store.terminal("term1", URL, ID);
    wire.act.mockReturnValueOnce(renew.promise);
    const mutate = vi.fn().mockReturnValue("placed"); const attempt = store.attempt(mutate);
    store.event(event(ID, "down")); renew.resolve({}); await attempt;
    expect(mutate).not.toHaveBeenCalled();
  });
  it("renews a visible prompt every 30 seconds and never marks it placed just for being offered", async () => {
    const { store, wire } = setup(); store.connect(); await flush(); store.terminal("term1", URL, ID);
    await store.attempt(() => "prompt"); wire.act.mockClear();
    await vi.advanceTimersByTimeAsync(180_000);
    expect(wire.act.mock.calls.filter(c => c[1] === "renew").length).toBe(6);
    expect(wire.act.mock.calls.some(c => c[1] === "placed")).toBe(false);
    expect(store.rows()[0].phase).toBe("prompt");
    store.disconnect(); const n = wire.act.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000); expect(wire.act).toHaveBeenCalledTimes(n);
  });
  it("caps deferred claims at four and expires them after ten minutes without owner dismissal", async () => {
    const { store, wire } = setup(); store.connect(); await flush();
    for (let i = 0; i < 6; i++) store.terminal("term1", URL, "ds_" + i.toString().padStart(8, "0"));
    expect(store.rows().map(e => e.id)).toEqual(["ds_00000002", "ds_00000003", "ds_00000004", "ds_00000005"]);
    await vi.advanceTimersByTimeAsync(600_000);
    expect(store.rows()).toEqual([]);
    expect(wire.act.mock.calls.some(c => c[1] === "dismiss")).toBe(false);
  });
  it("serializes two concurrent attempts and never retries placement after failed ACK", async () => {
    const { store, wire } = setup(), renew = deferred();
    store.connect(); await flush(); store.terminal("term1", URL, ID);
    wire.act.mockReturnValueOnce(renew.promise).mockRejectedValueOnce(Error("ACK lost"));
    const mutate = vi.fn().mockReturnValue("placed");
    const first = store.attempt(mutate); await store.attempt(mutate); renew.resolve({}); await first; await flush();
    store.event(event()); await store.attempt(mutate);
    expect(mutate).toHaveBeenCalledTimes(1);
  });
  it("a foreign claimed listing removes our queued action", async () => {
    const { store, wire } = setup(); store.connect(); await flush(); store.terminal("term1", URL, ID);
    wire.list.mockResolvedValue({ auto_open: true, servers: [listed(ID, { state: "claimed", claimed: true, mine: false })] });
    await store.refresh(); const mutate = vi.fn(); await store.attempt(mutate);
    expect(mutate).not.toHaveBeenCalled(); expect(store.rows()).toEqual([]);
  });
});

describe("cross-window preference", () => {
  it("OFF invalidates pending card work and an already in-flight ownership renewal", async () => {
    const { store, wire } = setup(), renew = deferred();
    store.connect(); await flush(); store.terminal("term1", URL, ID);
    wire.act.mockReturnValueOnce(renew.promise);
    const mutate = vi.fn().mockReturnValue("placed"); const pending = store.attempt(mutate);
    store.event({ kind: "dev_server_settings", data: { auto_open: false } });
    expect(store.enabled).toBe(false); expect(store.rows()).toEqual([]);
    renew.resolve({}); await pending; expect(mutate).not.toHaveBeenCalled();
  });
  it("OFF then ON cannot resurrect an old successful claim", async () => {
    const { store, wire } = setup(), claim = deferred();
    store.connect(); await flush(); wire.claim.mockReturnValueOnce(claim.promise); store.event(event());
    store.setting(false); store.setting(true); await flush();
    claim.resolve({ won: true, url: URL }); await flush();
    expect(store.rows()).toEqual([]);
  });
  it("a stale bootstrap cannot turn an OFF event back on", async () => {
    const { store, wire } = setup(), list = deferred(); wire.list.mockReturnValueOnce(list.promise);
    store.connect(); store.setting(false); list.resolve({ auto_open: true, servers: [listed()] }); await flush();
    expect(store.enabled).toBe(false); expect(store.rows()).toEqual([]);
  });
});
