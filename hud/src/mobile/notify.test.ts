import { describe, expect, it, vi } from "vitest";
import { keyBytes, Notifications, PUSH_API, SUBSCRIBE_API, WORDS, type PushEnv, type RegistrationLike,
  type Send, type SubscriptionLike } from "./notify";
import { validTime } from "../lib/phone";

// A real-shaped applicationServerKey: 65 bytes, uncompressed point (0x04 first).
const RAW = new Uint8Array(65).map((_, i) => (i === 0 ? 4 : i));
const KEY = btoa(String.fromCharCode(...RAW)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function sub(endpoint = "https://web.push.apple.com/abc", key: Uint8Array | null = RAW):
    SubscriptionLike & { unsubscribe: ReturnType<typeof vi.fn> } {
  return { endpoint, options: { applicationServerKey: key ? key.slice().buffer : null },
    toJSON: () => ({ endpoint, keys: { p256dh: "p", auth: "a" } }), unsubscribe: vi.fn(async () => true) };
}

function setup(over: Partial<PushEnv> = {}, opts: { held?: SubscriptionLike | null; status?: Record<string, unknown>;
  refuse?: number; getStatus?: number | null } = {}) {
  let held: SubscriptionLike | null = opts.held ?? null;
  const order: string[] = [];
  const reg: RegistrationLike = {
    pushManager: {
      getSubscription: async () => held,
      subscribe: vi.fn(async (options) => {
        order.push("subscribe");
        expect(options.userVisibleOnly).toBe(true);
        expect(Array.from(options.applicationServerKey)).toEqual(Array.from(RAW));
        held = sub();
        return held;
      }),
    },
  };
  let permission: "default" | "granted" | "denied" = held ? "granted" : "default";
  const env: PushEnv = {
    supported: () => true, ios: () => true, standalone: () => true, permission: () => permission,
    register: vi.fn(async () => reg), onOpenInbox: vi.fn(), ...over,
  };
  const calls: [string, string, unknown][] = [];
  const send: Send = vi.fn(async (method, path, body) => {
    order.push(method + " " + path);
    calls.push([method, path, body]);
    if (method === "GET" && opts.getStatus === null) return null;
    if (method === "GET" && opts.getStatus) return { status: opts.getStatus, body: { error: "notifications unavailable" } };
    if (method === "GET") return { status: 200, body: { key: KEY, enabled: true, subscribed: false, ...opts.status } };
    if (method === "POST" && opts.refuse) return { status: opts.refuse, body: { error: "this push service is not accepted" } };
    return { status: 200, body: {} };
  });
  const changed = vi.fn();
  const open = vi.fn();
  const n = new Notifications(env, send, changed, open);
  return { n, env, reg, send, calls, order, changed, open, grant: () => { permission = "granted"; },
    deny: () => { permission = "denied"; } };
}

describe("Turn on notifications (P-6)", () => {
  it("explains the Home Screen on an iPhone outside the installed app", async () => {
    const { n, env, send } = setup({ standalone: () => false });
    expect(n.state).toBe("install");
    await n.sync();
    expect(env.register).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
    const view = n.view();
    expect(view.textContent).toContain("Add to Home Screen");
    expect(view.querySelector("button")).toBeNull();
  });

  it("says when the browser has no push at all", () => {
    expect(setup({ supported: () => false, ios: () => false }).n.state).toBe("unsupported");
    expect(setup({ supported: () => false, ios: () => true, standalone: () => false }).n.state).toBe("install");
  });

  it("subscribes on the tap, the subscribe call first, then tells Jarvis", async () => {
    const t = setup();
    await t.n.sync();
    expect(t.env.register).toHaveBeenCalledTimes(1);
    expect(t.n.state).toBe("off");
    const button = t.n.view().querySelector("button")!;
    expect(button.textContent).toBe("Turn on notifications");
    t.order.length = 0;
    t.grant();
    await t.n.turnOn();
    expect(t.order).toEqual(["subscribe", "POST " + SUBSCRIBE_API]);
    expect(t.calls.at(-1)).toEqual(["POST", SUBSCRIBE_API, { subscription: { endpoint: "https://web.push.apple.com/abc",
      keys: { p256dh: "p", auth: "a" } } }]);
    expect(t.n.state).toBe("on");
    expect(t.n.view().querySelector("button")!.textContent).toBe("Turn off notifications");
  });

  it("the tap's first await is the subscribe call (the gesture holds)", async () => {
    const t = setup();
    await t.n.sync();
    const pending = t.n.turnOn();
    // Synchronously after the tap: subscribe was already called, nothing else.
    expect(t.reg.pushManager.subscribe).toHaveBeenCalledTimes(1);
    expect(t.n.state).toBe("busy");
    await pending;
  });

  it("a refusal from Jarvis is shown and the subscription undone", async () => {
    const t = setup({}, { refuse: 400 });
    await t.n.sync();
    await t.n.turnOn();
    expect(t.n.state).toBe("error");
    expect(t.n.view().textContent).toContain("this push service is not accepted");
    const held = await t.reg.pushManager.getSubscription();
    expect((held as ReturnType<typeof sub>).unsubscribe).toHaveBeenCalled();
  });

  it("blocked permission says where to allow it", async () => {
    const t = setup();
    t.deny();
    await t.n.sync();
    expect(t.n.state).toBe("blocked");
    expect(t.n.view().textContent).toBe(WORDS.blocked);
    expect(t.n.view().querySelector("button")).toBeNull();
  });

  it("hands a subscription it holds to a new session at start", async () => {
    const held = sub("https://web.push.apple.com/held");
    const t = setup({}, { held, status: { subscribed: false } });
    await t.n.sync();
    expect(t.calls.map(([m, p]) => m + " " + p)).toEqual(["GET " + PUSH_API, "POST " + SUBSCRIBE_API]);
    expect(t.n.state).toBe("on");
    const again = setup({}, { held, status: { subscribed: true } });
    await again.n.sync();
    expect(again.calls.map(([m]) => m)).toEqual(["GET"]);
    expect(again.n.state).toBe("on");
  });

  it("turning off unsubscribes here and tells Jarvis which", async () => {
    const held = sub("https://web.push.apple.com/held");
    const t = setup({}, { held, status: { subscribed: true } });
    await t.n.sync();
    await t.n.turnOff();
    expect(held.unsubscribe).toHaveBeenCalled();
    expect(t.calls.at(-1)).toEqual(["DELETE", SUBSCRIBE_API, { endpoint: "https://web.push.apple.com/held" }]);
    expect(t.n.state).toBe("off");
  });

  it("says when the desk turned notifications off", async () => {
    const held = sub();
    const t = setup({}, { held, status: { subscribed: true, enabled: false } });
    await t.n.sync();
    expect(t.n.view().textContent).toContain("turned off at the desk");
  });

  it("a notification's tap asks the app for the Inbox", () => {
    const t = setup();
    expect(t.env.onOpenInbox).toHaveBeenCalledWith(t.open);
  });

  it("a subscription made for another key is undone and asked for again (LOW-4)", async () => {
    const other = new Uint8Array(RAW);
    other[10] ^= 0xff;
    const held = sub("https://web.push.apple.com/old", other);
    const t = setup({}, { held, status: { subscribed: true } });
    await t.n.sync();
    expect(held.unsubscribe).toHaveBeenCalled();
    expect(t.calls.at(-1)).toEqual(["DELETE", SUBSCRIBE_API, { endpoint: "https://web.push.apple.com/old" }]);
    expect(t.n.state).toBe("off");
    expect(t.n.view().textContent).toContain("key changed");
    expect(t.n.view().querySelector("button")!.textContent).toBe("Turn on notifications");
    // One whose key the browser does not report is kept (it cannot tell);
    // the same key stays on.
    const unknown = setup({}, { held: sub("https://web.push.apple.com/x", null), status: { subscribed: true } });
    await unknown.n.sync();
    expect(unknown.n.state).toBe("on");
    const same = setup({}, { held: sub(), status: { subscribed: true } });
    await same.n.sync();
    expect(same.n.state).toBe("on");
  });

  it("says so when the worker cannot register or Jarvis has no notifications (503)", async () => {
    const t = setup({ register: vi.fn(async () => { throw new Error("SecurityError"); }) });
    await t.n.sync();
    expect(t.n.state).toBe("error");
    expect(t.n.view().textContent).toContain("could not set notifications up");
    for (const getStatus of [503, null]) {
      const u = setup({}, { getStatus });
      await u.n.sync();
      expect(u.n.state).toBe("unavailable");
      expect(u.n.view().textContent).toBe(WORDS.unavailable);
      expect(u.n.view().querySelector("button")).toBeNull();
    }
  });

  it("reads only a real P-256 key", () => {
    expect(keyBytes(KEY)).toEqual(RAW);
    expect(keyBytes("short")).toBeNull();
    expect(keyBytes(KEY.slice(0, -2) + "!!")).toBeNull();
    const notPoint = btoa(String.fromCharCode(...new Uint8Array(65).fill(5))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    expect(keyBytes(notPoint)).toBeNull();
  });

  it("the desk's quiet-hours times are HH:MM", () => {
    expect(["23:00", "07:00", "00:00", "23:59"].every(validTime)).toBe(true);
    expect(["7:00", "24:00", "07:60", "", "07:00 "].some(validTime)).toBe(false);
  });
});
