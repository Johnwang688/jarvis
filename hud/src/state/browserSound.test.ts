import { describe, expect, it, vi } from "vitest";
import { BrowserSoundStore, SOUND_READ_ERROR, SOUND_WRITE_ERROR, soundMessage, soundStatus } from "./browserSound";
const packet = (desired = false, applied = desired, phase = "ready", reason: string | null = null, generation = 1) => ({ browser: {
  sound: { desired, generation, available: true, reason: null },
  instances: [{ profile: "p-12345678", sound: { requested: desired, applied, generation: 1, phase, reason } }],
} });
function deferred<T>() { let resolve!: (v: T) => void; let reject!: (e: unknown) => void;
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
async function flush() { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); }

describe("sound status contract", () => {
  it("keeps desired and applied modes distinct without retaining unrelated metadata", () => {
    const raw = packet(true, false, "pending", "busy");
    Object.assign(raw.browser, { url: "private-path", media: "private-title" });
    const value = soundStatus(raw)!;
    expect(value.desired).toBe(true); expect(value.instances[0].applied).toBe(false);
    expect(JSON.stringify(value)).not.toContain("private");
    expect(soundMessage(value)).toBe("Sound will change when current Browser work finishes.");
  });
  it.each([null, {}, { browser: {} }, { browser: { sound: { desired: true } } }])("unknown/old status never implies off or permission to save", raw => {
    expect(soundStatus(raw)).toBeNull();
  });
  it("rejects malformed booleans, generations, profiles, duplicate rows and uncontrolled reasons", () => {
    const cases: unknown[] = [];
    const add = (change: (v: any) => void) => { const value = packet(); change(value); cases.push(value); };
    add(v => { v.browser.sound.desired = 1; });
    add(v => { v.browser.sound.generation = -1; });
    add(v => { v.browser.instances[0].sound.generation = 0; });
    add(v => { v.browser.instances[0].profile = "private path"; });
    add(v => { v.browser.instances.push(v.browser.instances[0]); });
    add(v => { v.browser.instances[0].sound.reason = "private utility text"; });
    add(v => { v.browser.instances[0].sound.phase = "guess"; });
    add(v => { v.browser.instances = Array(10).fill(v.browser.instances[0]); });
    for (const value of cases) expect(soundStatus(value)).toBeNull();
  });
  it("uses fixed Private, unavailable and failure wording without claiming applied silence", () => {
    const privatePending = packet(true, false, "pending", "private"); privatePending.browser.instances[0].profile = "private";
    expect(soundMessage(soundStatus(privatePending))).toBe("Close all Private panes to apply sound.");
    const unavailable = packet(true, false); unavailable.browser.sound.available = false;
    expect(soundMessage(soundStatus(unavailable))).toBe("Sound isn't available on this machine yet.");
    expect(soundMessage(soundStatus(packet(false, true, "pending", "mute_uncertain")))).toContain("when current Browser work finishes");
    expect(soundMessage(soundStatus(packet(true, false, "failed", "restart_failed")))).toContain("could not restart");
  });
});

describe("one authoritative window store", () => {
  it("starts once, never writes on discovery, and applies actual status after an explicit change", async () => {
    const status = vi.fn().mockResolvedValue(packet()), set = vi.fn().mockResolvedValue({ sound: true });
    const store = new BrowserSoundStore({ status, set }); store.start(); store.start(); await flush();
    expect(status).toHaveBeenCalledTimes(1); expect(set).not.toHaveBeenCalled();
    status.mockResolvedValue(packet(true, false, "pending", "busy", 2));
    await store.change(true);
    expect(set).toHaveBeenCalledExactlyOnceWith(true);
    expect(store.getSnapshot().value?.instances[0].applied).toBe(false);
    expect(store.getSnapshot().value?.desired).toBe(true);
  });
  it("keeps old readback during a pending save and refuses duplicate writes", async () => {
    const held = deferred<unknown>(); const set = vi.fn().mockReturnValue(held.promise);
    const status = vi.fn().mockResolvedValue(packet()); const store = new BrowserSoundStore({ status, set });
    await store.refresh(); const saving = store.change(true); const duplicate = store.change(false);
    expect(set).toHaveBeenCalledTimes(1); expect(store.getSnapshot().value?.desired).toBe(false);
    expect(store.getSnapshot().busy).toBe(true);
    status.mockResolvedValue(packet(true)); held.resolve({ sound: true }); await Promise.all([saving, duplicate]);
    expect(store.getSnapshot().value?.desired).toBe(true); expect(store.getSnapshot().busy).toBe(false);
  });
  it("a failed or ambiguous save requires a fresh explicit read before any next mutation", async () => {
    const status = vi.fn().mockResolvedValue(packet()), set = vi.fn().mockRejectedValue(Error("private backend reason"));
    const store = new BrowserSoundStore({ status, set }); await store.refresh(); await store.change(true);
    expect(store.getSnapshot().error).toBe(SOUND_WRITE_ERROR); expect(store.getSnapshot().status).toBe("error");
    await store.change(false); expect(set).toHaveBeenCalledTimes(1);
    status.mockResolvedValue(packet(true)); await store.refresh();
    expect(store.getSnapshot().value?.desired).toBe(true); expect(store.getSnapshot().status).toBe("ready");
  });
  it("missing PATCH readback or unavailable status cannot be called a successful save", async () => {
    for (const saved of [{}, { sound: "true" }]) {
      const store = new BrowserSoundStore({ status: async () => packet(), set: async () => saved });
      await store.refresh(); await store.change(true); expect(store.getSnapshot().error).toBe(SOUND_WRITE_ERROR);
    }
    const status = vi.fn().mockResolvedValueOnce(packet()).mockRejectedValue(Error("private"));
    const store = new BrowserSoundStore({ status, set: async () => ({ sound: true }) });
    await store.refresh(); await store.change(true); expect(store.getSnapshot().status).toBe("error");
  });
  it("late response from an older refresh cannot replace a newer receipt", async () => {
    const old = deferred<unknown>(); const status = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue(packet(true));
    const store = new BrowserSoundStore({ status, set: vi.fn() }); const first = store.refresh(); await store.refresh();
    old.resolve(packet(false)); await first; expect(store.getSnapshot().value?.desired).toBe(true);
  });
  it("disconnect fences a pending save and its late reply after reconnection", async () => {
    const held = deferred<unknown>(); const status = vi.fn().mockResolvedValue(packet());
    const store = new BrowserSoundStore({ status, set: () => held.promise }); store.start(); await flush();
    const saving = store.change(true); store.event({ kind: "_disconnected" });
    expect(store.getSnapshot().status).toBe("offline"); expect(store.getSnapshot().busy).toBe(false);
    store.event({ kind: "_connected" }); await flush();
    held.resolve({ sound: true }); await saving;
    expect(store.getSnapshot().value?.desired).toBe(false); expect(store.getSnapshot().status).toBe("ready");
  });
  it("coalesces all busy change events into one post-save read and ignores malformed events", async () => {
    const held = deferred<unknown>(), status = vi.fn().mockResolvedValue(packet());
    const store = new BrowserSoundStore({ status, set: () => held.promise }); store.start(); await flush();
    const saving = store.change(true);
    for (let n = 0; n < 20; ++n) store.event({ kind: "browser_sound_changed", data: { generation: 2 } });
    expect(status).toHaveBeenCalledTimes(1);
    held.resolve({ sound: true }); await saving; await flush();
    expect(status).toHaveBeenCalledTimes(3);
    store.event({ kind: "browser_sound_changed", data: { generation: "private" } });
    await flush(); expect(status).toHaveBeenCalledTimes(3);
  });
  it("read failure is fixed text, leaves no optimistic setting and never loops", async () => {
    const status = vi.fn().mockRejectedValue(Error("private URL"));
    const store = new BrowserSoundStore({ status, set: vi.fn() }); store.start(); await flush();
    expect(store.getSnapshot().error).toBe(SOUND_READ_ERROR); expect(store.getSnapshot().value).toBeNull();
    expect(status).toHaveBeenCalledTimes(1);
  });
});
describe("memory-only pane sound gesture",()=>{
  it("sends only owner tab sound route, then reads authoritative instance state",async()=>{
    const status=vi.fn().mockResolvedValue(packet(true,false));
    const set=vi.fn();const sound=vi.fn().mockResolvedValue({on:true});
    const store=new BrowserSoundStore({status,set,sound});await store.refresh();
    status.mockResolvedValue(packet(true,true));await store.toggle("1234abcd",true);
    expect(sound).toHaveBeenCalledExactlyOnceWith("1234abcd",true);
    expect(set).not.toHaveBeenCalled();expect(store.getSnapshot().value?.instances[0].applied).toBe(true);
  });
  it("ambiguous gesture readback does not allow another unmute",async()=>{
    const sound=vi.fn().mockResolvedValue({on:false});
    const store=new BrowserSoundStore({status:async()=>packet(true,false),set:vi.fn(),sound});
    await store.refresh();await store.toggle("1234abcd",true);await store.toggle("1234abcd",true);
    expect(sound).toHaveBeenCalledTimes(1);expect(store.getSnapshot().error).toBe(SOUND_WRITE_ERROR);
  });
});
