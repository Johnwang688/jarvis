import { describe, expect, it } from "vitest";
import {
  DRIVE_IDLE, LINGER_MS, LIST_MAX, RUNNING_STALE_MS, TEXT_CAP, actionLine, agentActive, bannerOf, clockOf,
  driveReducer, isTakeover, outlineOf, parseAgent, runningOf, type AgentMsg, type DriveState,
} from "./drive";
import { cdpKey, cdpMouse } from "./remote";

const T0 = Date.parse("2026-10-12T15:42:03");

const act = (over: Record<string, unknown> = {}) => ({
  t: "agent", kind: "action", id: "a1", at: T0, action: "click", target: "Sign in", text: "",
  outcome: "running", rect: { x: 10, y: 20, width: 80, height: 24 }, ...over,
});

function feed(s: DriveState, raw: unknown, now: number): DriveState {
  const msg = parseAgent(raw, now);
  expect(msg).not.toBeNull();
  return driveReducer(s, { type: "agent", msg: msg as AgentMsg, now });
}

const slots = (map: Record<string, number>) => (id: string) => map[id] ?? null;

describe("the daemon's {t: agent} frames", () => {
  it("are read in their four kinds", () => {
    expect(parseAgent({ t: "agent", kind: "driver", thread_id: "th1", provider: "claude" }, T0))
      .toEqual({ kind: "driver", threadId: "th1", provider: "claude" });
    expect(parseAgent({ t: "agent", kind: "driver", thread_id: null, provider: null }, T0))
      .toEqual({ kind: "driver", threadId: null, provider: null });
    expect(parseAgent(act(), T0)).toEqual({
      kind: "action",
      action: { id: "a1", at: T0, action: "click", target: "Sign in", text: "", phase: "running",
                rect: { x: 10, y: 20, width: 80, height: 24 } },
    });
    expect(parseAgent({ t: "agent", kind: "paused", paused: true, by: "owner" }, T0))
      .toEqual({ kind: "paused", paused: true, by: "owner" });
    const list = parseAgent({ t: "agent", kind: "list", actions: [act({ id: "a" }), { bad: 1 }, act({ id: "b" })] }, T0);
    expect(list?.kind === "list" && list.actions.map((a) => a.id)).toEqual(["a", "b"]);
  });

  it("drop anything of the wrong shape whole", () => {
    for (const raw of [
      null, "agent", { t: "frame" }, { t: "agent" }, { t: "agent", kind: "other" },
      { t: "agent", kind: "driver", thread_id: "../x" }, { t: "agent", kind: "paused", paused: "yes" },
      act({ id: "" }), act({ id: "x".repeat(65) }), act({ action: "delete_everything" }), act({ outcome: "approved" }),
      { t: "agent", kind: "list", actions: "a" },
    ]) {
      expect(parseAgent(raw, T0)).toBeNull();
    }
  });

  it("clean what the page chose to one capped line", () => {
    const m = parseAgent(act({ target: "Sign\nin\u202E\u200B now", text: "x".repeat(500) }), T0);
    const a = m?.kind === "action" ? m.action : null;
    expect(a?.target).toBe("Sign in now");
    expect(a?.text.length).toBe(TEXT_CAP);
  });

  it("keep a target's outline only while it runs; take an unknown provider and a bad time as unknown", () => {
    const done = parseAgent(act({ outcome: "done" }), T0);
    expect(done?.kind === "action" && done.action.rect).toBeNull();
    const bad = parseAgent(act({ rect: { x: 1, y: 1, width: -3, height: 2 } }), T0);
    expect(bad?.kind === "action" && bad.action.rect).toBeNull();
    expect(parseAgent({ t: "agent", kind: "driver", thread_id: "t", provider: "gpt" }, T0))
      .toEqual({ kind: "driver", threadId: "t", provider: null });
    const iso = parseAgent(act({ at: "2026-10-12T15:42:03" }), 5);
    expect(iso?.kind === "action" && iso.action.at).toBe(T0);
    const none = parseAgent(act({ at: "soon" }), 5);
    expect(none?.kind === "action" && none.action.at).toBe(5);
  });
});

describe("the banner", () => {
  const driving = () => feed(feed(DRIVE_IDLE, { t: "agent", kind: "driver", thread_id: "th1", provider: "fast" }, T0),
    act({ outcome: "done" }), T0);

  it("is up from the agent taking the tab until 30 s after its last action", () => {
    expect(bannerOf(DRIVE_IDLE, T0, slots({ th1: 2 }))).toBeNull();
    const taken = feed(DRIVE_IDLE, { t: "agent", kind: "driver", thread_id: "th1", provider: "fast" }, T0);
    expect(bannerOf(taken, T0, slots({ th1: 2 }))?.text).toBe("Driven by the chat in pane 2 (fast path)");
    const s = driving();
    expect(bannerOf(s, T0 + 1000, slots({ th1: 2 }))).toEqual({
      text: "Driven by the chat in pane 2 (fast path)", paused: false, canStop: true, canResume: false,
    });
    expect(bannerOf(s, T0 + LINGER_MS, slots({ th1: 2 }))).not.toBeNull();
    expect(bannerOf(s, T0 + LINGER_MS + 1, slots({ th1: 2 }))).toBeNull();
  });

  // Review of PR #66 (LOW 1): a reload mid-drive hears driver, list and paused, but no action.
  it("comes back after a reattach, with its Stop and the outline", () => {
    let s = feed(DRIVE_IDLE, { t: "agent", kind: "driver", thread_id: "t1", provider: "claude" }, T0);
    s = feed(s, { t: "agent", kind: "list", actions: [act({ id: "x1", outcome: "done" }), act({ id: "x2" })] }, T0);
    s = feed(s, { t: "agent", kind: "paused", paused: false, by: null }, T0);
    expect(bannerOf(s, T0 + 1, slots({ t1: 3 }))).toEqual({
      text: "Driven by the chat in pane 3 (Claude)", paused: false, canStop: true, canResume: false,
    });
    expect(outlineOf(s, T0 + 1)).toEqual({ x: 10, y: 20, width: 80, height: 24 });
    // A list alone, with an action under way, outlines it too.
    const listOnly = feed(DRIVE_IDLE, { t: "agent", kind: "list", actions: [act({ outcome: "asking" })] }, T0);
    expect(outlineOf(listOnly, T0 + 1)).not.toBeNull();
    expect(bannerOf(listOnly, T0 + 1, slots({}))?.canStop).toBe(true);
    // A new driver never moves an earlier clock forward: a stale drive stays stale.
    const old = feed(DRIVE_IDLE, act({ outcome: "done" }), T0);
    expect(feed(old, { t: "agent", kind: "driver", thread_id: "t1", provider: null }, T0 + 5 * LINGER_MS).lastAt).toBe(T0);
  });

  it("goes when the turn ends, but never while an action still runs", () => {
    const s = feed(driving(), { t: "agent", kind: "driver", thread_id: null, provider: null }, T0 + 10);
    expect(bannerOf(s, T0 + 20, slots({ th1: 2 }))).toBeNull();
    const running = feed(s, act({ id: "a2", outcome: "asking" }), T0 + 30);
    expect(bannerOf(running, T0 + 40, slots({ th1: 2 }))?.text).toBe("Driven by the chat in pane 2 (fast path)");
  });

  it("names the pane by its slot, or says the chat is not on screen", () => {
    const s = feed(driving(), { t: "agent", kind: "driver", thread_id: "th1", provider: "codex" }, T0);
    expect(bannerOf(s, T0, slots({}))?.text).toBe("Driven by a chat not on screen (Codex)");
    expect(bannerOf(s, T0, slots({ th1: 13 }))?.text).toBe("Driven by a chat not on screen (Codex)");
    // FS0: a twelve-pane HUD names slots 9..12; only the upper bound moved.
    for (const n of [1, 8, 9, 10, 11, 12]) {
      expect(bannerOf(s, T0, slots({ th1: n }))?.text).toBe(`Driven by the chat in pane ${n} (Codex)`);
    }
    for (const n of [13, 14, 99, 0, -1, 1.5, 12.5, NaN, Infinity]) {
      expect(bannerOf(s, T0, slots({ th1: n }))?.text).toBe("Driven by a chat not on screen (Codex)");
    }
    const anon = feed(DRIVE_IDLE, act(), T0);
    expect(bannerOf(anon, T0, slots({}))?.text).toBe("Driven by a chat");
  });

  it("never says Authorize, Approve or Deny, in any state", () => {
    let s = driving();
    const states = [s];
    s = driveReducer(s, { type: "owner", now: T0 + 1 });
    states.push(s);
    s = driveReducer(driveReducer(s, { type: "resume", blocked: false }), { type: "stop" });
    states.push(s);
    for (const st of states) {
      for (const map of [{ th1: 3 }, {}] as Record<string, number>[]) {
        const b = bannerOf(st, T0 + 2, slots(map));
        expect(b).not.toBeNull();
        expect(b!.text).not.toMatch(/authori[sz]e|approve|deny|denied/i);
      }
    }
  });
});

describe("taking over and resuming", () => {
  const busy = () => feed(feed(DRIVE_IDLE, { t: "agent", kind: "driver", thread_id: "th1", provider: "claude" }, T0),
    act(), T0);

  it("the owner's input pauses a driving agent until Resume, across turns", () => {
    let s = driveReducer(busy(), { type: "owner", now: T0 + 5 });
    expect(bannerOf(s, T0 + 5, slots({ th1: 1 }))).toEqual({
      text: "Paused: you took over from the chat in pane 1 (Claude)", paused: true, canStop: false, canResume: true,
    });
    s = feed(s, act({ outcome: "stopped" }), T0 + 6);
    s = feed(s, { t: "agent", kind: "driver", thread_id: null, provider: null }, T0 + 7);
    // Long after the turn ended, still paused, still offering Resume.
    expect(bannerOf(s, T0 + 10 * LINGER_MS, slots({}))?.canResume).toBe(true);
    s = driveReducer(s, { type: "resume", blocked: false });
    expect(s.paused).toBe(false);
    expect(bannerOf(s, T0 + 10 * LINGER_MS, slots({}))).toBeNull();
  });

  it("Resume does nothing under a card", () => {
    const s = driveReducer(busy(), { type: "owner", now: T0 });
    expect(driveReducer(s, { type: "resume", blocked: true })).toBe(s);
  });

  it("with no agent at work, the owner's input changes nothing", () => {
    expect(driveReducer(DRIVE_IDLE, { type: "owner", now: T0 })).toBe(DRIVE_IDLE);
    const old = feed(DRIVE_IDLE, act({ outcome: "done" }), T0);
    expect(driveReducer(old, { type: "owner", now: T0 + LINGER_MS + 1 }).paused).toBe(false);
  });

  it("Stop pauses; the daemon's word is final either way", () => {
    let s = driveReducer(busy(), { type: "stop" });
    expect(bannerOf(s, T0, slots({ th1: 4 }))?.text).toBe("Paused: you stopped the chat in pane 4 (Claude)");
    s = feed(s, { t: "agent", kind: "paused", paused: false, by: null }, T0 + 1);
    expect(s.paused).toBe(false);
    s = feed(s, { t: "agent", kind: "paused", paused: true, by: "owner" }, T0 + 2);
    expect([s.paused, s.pausedBy]).toEqual([true, "owner"]);
  });

  it("a closed tab forgets everything", () => {
    expect(driveReducer(driveReducer(busy(), { type: "owner", now: T0 }), { type: "closed" })).toEqual(DRIVE_IDLE);
  });
});

describe("isTakeover", () => {
  const at = { x: 1, y: 1 };
  const m = { button: 0, buttons: 0, detail: 1 };

  it("keys, text, the wheel, presses and releases, dialogs and files take over", () => {
    expect(isTakeover({ t: "key", e: cdpKey({ key: "a", code: "KeyA", ctrlKey: false, altKey: false }, "down") })).toBe(true);
    expect(isTakeover({ t: "key", e: cdpKey({ key: "a", code: "KeyA", ctrlKey: false, altKey: false }, "up") })).toBe(true);
    expect(isTakeover({ t: "text", text: "pasted" })).toBe(true);
    expect(isTakeover({ t: "mouse", e: cdpMouse("down", m, at) })).toBe(true);
    expect(isTakeover({ t: "mouse", e: cdpMouse("up", m, at) })).toBe(true);
    expect(isTakeover({ t: "mouse", e: { ...cdpMouse("move", m, at), type: "mouseWheel", deltaY: 40 } })).toBe(true);
    expect(isTakeover({ t: "dialog", accept: true })).toBe(true);
    expect(isTakeover({ t: "files", names: ["a.txt"] })).toBe(true);
  });

  // The plan's BR-9c mutation: pointer moves counted as taking over.
  it("a pointer move does not — a drag does", () => {
    expect(isTakeover({ t: "mouse", e: cdpMouse("move", m, at) })).toBe(false);
    expect(isTakeover({ t: "mouse", e: cdpMouse("move", { ...m, buttons: 1 }, at) })).toBe(true);
  });

  it("the socket's own messages do not; an unknown kind of input does", () => {
    for (const t of ["ack", "resize", "resume", "stop", "focus", "blur", "visible", "ping", "pong"]) {
      expect(isTakeover({ t })).toBe(false);
    }
    expect(isTakeover(null)).toBe(false);
    expect(isTakeover({ t: "touch" })).toBe(true);
  });

  // Review of PR #66 (MED): push-to-talk's Ctrl, pressed before its D, is not the owner taking over.
  it("a lone modifier does not, down or up", () => {
    for (const k of ["Control", "Alt", "Shift", "Meta", "AltGraph"]) {
      for (const phase of ["down", "up"] as const) {
        expect(isTakeover({ t: "key", e: cdpKey({ key: k, code: "", ctrlKey: k === "Control", altKey: false }, phase) }))
          .toBe(false);
      }
    }
    expect(isTakeover({ t: "key", e: cdpKey({ key: "a", code: "KeyA", ctrlKey: true, altKey: false }, "down") })).toBe(true);
  });
});

describe("the list and the outline", () => {
  it("keeps the newest 20, updating an action in place as it ends", () => {
    let s = DRIVE_IDLE;
    for (let i = 0; i < LIST_MAX + 5; i++) s = feed(s, act({ id: `a${i}`, outcome: "done" }), T0 + i);
    expect(s.actions.length).toBe(LIST_MAX);
    expect(s.actions[0].id).toBe("a5");
    s = feed(s, act({ id: "a24", outcome: "stopped" }), T0 + 100);
    expect(s.actions.length).toBe(LIST_MAX);
    expect(s.actions[LIST_MAX - 1].phase).toBe("stopped");
    const replay = feed(DRIVE_IDLE, { t: "agent", kind: "list", actions: Array.from({ length: 50 }, (_, i) => act({ id: `r${i}` })) }, T0);
    expect(replay.actions.length).toBe(LIST_MAX);
    expect(replay.actions[LIST_MAX - 1].id).toBe("r49");
  });

  it("a replayed list never brings the banner back by itself", () => {
    const s = feed(DRIVE_IDLE, { t: "agent", kind: "list", actions: [act({ outcome: "done" })] }, T0);
    expect(bannerOf(s, T0, slots({}))).toBeNull();
  });

  it("outlines the target while its action runs or asks, and not once it ends or goes stale", () => {
    let s = feed(DRIVE_IDLE, act(), T0);
    expect(outlineOf(s, T0)).toEqual({ x: 10, y: 20, width: 80, height: 24 });
    s = feed(s, act({ outcome: "asking" }), T0 + 1);
    expect(outlineOf(s, T0 + 1)).not.toBeNull();
    expect(outlineOf(s, T0 + 1 + RUNNING_STALE_MS + 1)).toBeNull();
    expect(agentActive(s, T0 + 1 + RUNNING_STALE_MS + 1)).toBe(false);
    s = feed(s, act({ outcome: "asked_denied" }), T0 + 2);
    expect(runningOf(s, T0 + 2)).toBeNull();
    expect(outlineOf(s, T0 + 2)).toBeNull();
  });

  it("reads as a line", () => {
    const one = (over: Record<string, unknown>) => {
      const m = parseAgent(act(over), T0);
      return m?.kind === "action" ? m.action : null;
    };
    expect(actionLine(one({ outcome: "asked_allowed" })!, "15:42:03")).toBe("15:42:03 · click · 'Sign in' · asked · you allowed");
    expect(actionLine(one({ action: "type", target: "Search", text: "cheap flights", outcome: "done" })!, "15:42:04"))
      .toBe('15:42:04 · type · \'Search\' · "cheap flights" · done');
    expect(actionLine(one({ action: "scroll", target: "", text: "down", outcome: "denied" })!, "t"))
      .toBe('t · scroll · "down" · turned down');
    expect(actionLine(one({ outcome: "asked_denied" })!, "t")).not.toMatch(/deny|denied/i);
    expect(clockOf(T0)).toBe("15:42:03");
    expect(clockOf(NaN)).toBe("--:--:--");
  });
});
