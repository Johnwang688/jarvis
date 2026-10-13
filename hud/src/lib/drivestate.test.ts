import { DriveRegistry, DRIVE_EMPTY } from "./drivestate";
import { LIST_MAX } from "./drive";
const at = 2_000_000;
const driver = { t: "agent", kind: "driver", thread_id: "thread_1", provider: "codex" };
const paused = (value = false) => ({ t: "agent", kind: "paused", paused: value, by: value ? "owner" : null });
const list = { t: "agent", kind: "list", actions: [] };
const action = (id = "a") => ({ t: "agent", kind: "action", id, at, action: "click", outcome: "running", target: "button", text: "" });
function ready(registry: DriveRegistry, id = "0123abcd") {
  const lease = registry.attach(id)!; registry.connected(lease);
  registry.receive(lease, driver, at); registry.receive(lease, paused(), at); registry.receive(lease, list, at);
  return lease;
}
it("requires a connected exact attachment and complete ordered replay before controls", () => {
  const r = new DriveRegistry(), lease = r.attach("0123abcd")!, send = vi.fn(() => true);
  expect(r.receive(lease, driver, at)).toBe(false);
  r.connected(lease); r.receive(lease, driver, at); r.receive(lease, list, at);
  expect(r.control(lease, "stop", false, at, send)).toBe(false);
  r.receive(lease, paused(), at); expect(r.get(lease.tabId).ready).toBe(false);
  r.receive(lease, list, at); expect(r.get(lease.tabId).ready).toBe(true);
  expect(r.control(lease, "stop", false, at, send)).toBe(true);
  expect(send).toHaveBeenCalledExactlyOnceWith({ t: "stop" });
});
it("old sockets cannot update or disconnect a new generation, even with a copied lease", () => {
  const r = new DriveRegistry(), first = ready(r), second = ready(r);
  r.receive(second, action(), at);
  expect(r.receive(first, paused(true), at)).toBe(false);
  expect(r.receive({ ...second }, paused(true), at)).toBe(false);
  r.detach(first); r.owner(first, { t: "text" }, at);
  expect(r.get(second.tabId).connected).toBe(true); expect(r.get(second.tabId).state.paused).toBe(false);
  expect(r.control(first, "stop", false, at, vi.fn())).toBe(false);
});
it("drops old histories on reattach and refuses controls until the new replay", () => {
  const r = new DriveRegistry(), old = ready(r); r.receive(old, action(), at); r.owner(old, { t: "text" }, at);
  r.detach(old); expect(r.get(old.tabId).state.paused).toBe(true); expect(r.get(old.tabId).ready).toBe(false);
  const fresh = r.attach(old.tabId)!; r.connected(fresh);
  expect(r.get(fresh.tabId).state.actions).toEqual([]); expect(r.get(fresh.tabId).state.paused).toBe(false);
  expect(r.control(fresh, "resume", false, at, vi.fn())).toBe(false);
});
it("mirrors only actual input after replay, leaving move, modifiers and controls alone", () => {
  const r = new DriveRegistry(), lease = ready(r); r.receive(lease, action(), at);
  for (const msg of [{t:"mouse",e:{type:"mouseMoved",buttons:0}}, {t:"key",e:{key:"Control"}}, ...["resize","ack","focus","blur","ping","visible"].map(t=>({t}))]) r.owner(lease,msg,at);
  expect(r.get(lease.tabId).state.paused).toBe(false);
  r.owner(lease,{t:"mouse",e:{type:"mouseMoved",buttons:1}},at);
  expect(r.get(lease.tabId).state.pausedBy).toBe("owner");
});
it.each([{t:"mouse",e:{type:"mousePressed"}}, {t:"mouse",e:{type:"mouseReleased"}}, {t:"mouse",e:{type:"mouseWheel"}}, {t:"key",e:{key:"a"}}, {t:"text"}, {t:"dialog"}, {t:"files"}])("mirrors sent owner input %j",msg=>{
  const r=new DriveRegistry(), lease=ready(r);r.owner(lease,msg,at);expect(r.get(lease.tabId).state.paused).toBe(true);
});
it("Resume requires successful send, waits for the daemon, and cannot repeat", () => {
  const r = new DriveRegistry(), lease = ready(r); r.receive(lease, paused(true), at);
  const failed = vi.fn(() => false), send = vi.fn(() => true);
  expect(r.control(lease,"resume",false,at,failed)).toBe(false); expect(r.get(lease.tabId).state.paused).toBe(true);
  expect(r.control(lease,"resume",true,at,send)).toBe(false); expect(send).not.toHaveBeenCalled();
  expect(r.control(lease,"resume",false,at,send)).toBe(true); expect(r.get(lease.tabId).state.paused).toBe(true);
  expect(r.get(lease.tabId).pending).toBe("resume"); expect(r.control(lease,"resume",false,at,send)).toBe(false);
  r.receive(lease,paused(),at); expect(r.get(lease.tabId).state.paused).toBe(false); expect(r.get(lease.tabId).pending).toBeNull();
});
it("a replacement or disconnect during control sending cannot mutate the new attachment", () => {
  const r=new DriveRegistry(), lease=ready(r);
  expect(r.control(lease,"stop",false,at,()=>{ready(r);return true;})).toBe(false);
  expect(r.get(lease.tabId).state.paused).toBe(false);
  const fresh=ready(r); expect(r.control(fresh,"stop",false,at,()=>{r.detach(fresh);return true;})).toBe(false);
  expect(r.get(fresh.tabId).state.paused).toBe(false);
});
it("retains only bounded clean private action text, and active status expires or disconnects", () => {
  const r=new DriveRegistry(), lease=ready(r);
  for(let i=0;i<40;i++)r.receive(lease,{...action(String(i)),outcome:"done",target:"\u202eevil".repeat(100),text:"x".repeat(100)},at);
  const state=r.get(lease.tabId).state;
  expect(state.actions).toHaveLength(LIST_MAX);expect(state.actions[0].id).toBe("20");
  expect(state.actions.every(a=>a.target.length<=60&&a.text.length<=60&&!a.target.includes("\u202e"))).toBe(true);
  expect(r.active(lease.tabId,at)).toBe(true);expect(r.active(lease.tabId,at+30_001)).toBe(false);
  r.detach(lease);expect(r.active(lease.tabId,at)).toBe(false);
});
it("removes closed tabs, rejects orphan callbacks and caps retained rows", () => {
  const r=new DriveRegistry(), lease=ready(r);r.retain([]);expect(r.get(lease.tabId)).toBe(DRIVE_EMPTY);
  expect(r.receive(lease,driver,at)).toBe(false);
  for(let i=0;i<96;i++)expect(r.attach(i.toString(16).padStart(8,"0"))).not.toBeNull();
  expect(r.attach("ffffffff")).toBeNull();expect(r.attach("../bad")).toBeNull();
  r.retain(["00000000"]);expect(r.attach("ffffffff")).not.toBeNull();
});
it("subscriptions are removable and unrelated malformed frames publish nothing", () => {
  const r=new DriveRegistry(), fn=vi.fn(), stop=r.subscribe(fn), lease=ready(r);fn.mockClear();
  const version=r.revision();expect(r.receive(lease,{t:"agent",kind:"paused",paused:"true"},at)).toBe(false);
  expect(fn).not.toHaveBeenCalled();expect(r.revision()).toBe(version);stop();r.detach(lease);expect(fn).not.toHaveBeenCalled();
});
