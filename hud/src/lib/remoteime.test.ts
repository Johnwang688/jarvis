import { COMMIT_HOLD_MS, RemoteIme, type ImeSink } from "./remoteime";
import { routeKeyDown } from "./remote";

function harness() {
  const state = { can: true, epoch: 1, sent: [] as string[], clears: 0, showing: [] as boolean[] };
  const sink: ImeSink = {
    canInput: () => state.can, epoch: () => state.epoch,
    send: (t) => state.sent.push(t), clear: () => { state.clears += 1; },
    showing: (on) => state.showing.push(on),
  };
  return { state, ime: new RemoteIme(sink) };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("RemoteIme", () => {
  it("sends a commit once, after the hold, and nothing while composing", () => {
    const { state, ime } = harness();
    ime.start(); ime.update(); ime.input("insertCompositionText", "ni", true); ime.update();
    expect(ime.composing).toBe(true); expect(state.sent).toEqual([]); expect(state.showing).toEqual([true]);
    ime.end("你好");
    expect(state.sent).toEqual([]); expect(ime.waiting).toBe(true); expect(state.showing).toEqual([true, false]);
    vi.advanceTimersByTime(COMMIT_HOLD_MS);
    expect(state.sent).toEqual(["你好"]);
    vi.advanceTimersByTime(1000); expect(state.sent).toEqual(["你好"]);
  });

  it("Escape's empty end sends nothing and leaves no composition", () => {
    const { state, ime } = harness();
    ime.start(); ime.end(""); vi.runAllTimers();
    expect(state.sent).toEqual([]); expect(ime.composing).toBe(false); expect(ime.waiting).toBe(false);
  });

  it("a blur or press right after the commit Chrome makes for it drops the raw text", () => {
    for (const how of ["blur", "press"]) {
      const { state, ime } = harness();
      ime.start(); ime.end("ni"); // Chrome: compositionend("ni") precedes the blur, same task
      ime.interrupt();
      vi.runAllTimers();
      expect(state.sent, how).toEqual([]);
    }
  });

  it("the next keystroke or insertion sends a waiting commit first, once", () => {
    const { state, ime } = harness();
    ime.start(); ime.end("日本"); ime.flush();             // the next keydown
    expect(state.sent).toEqual(["日本"]);
    vi.runAllTimers(); expect(state.sent).toEqual(["日本"]);
    ime.start(); ime.end("語"); ime.input("insertText", "😀", false);
    expect(state.sent).toEqual(["日本", "語", "😀"]);
    ime.start(); ime.end("a"); ime.start();                // a new composition flushes the last
    expect(state.sent).toEqual(["日本", "語", "😀", "a"]);
  });

  it("a card, hold or reconnect mid-composition cancels it, and its later end sends nothing", () => {
    const { state, ime } = harness();
    ime.start(); state.can = false; ime.update();
    expect(ime.composing).toBe(false); expect(state.clears).toBeGreaterThan(0);
    state.can = true; ime.end("held text"); vi.runAllTimers(); expect(state.sent).toEqual([]);

    ime.start(); state.epoch += 1; ime.end("old epoch"); vi.runAllTimers();   // card came and went, or a reconnect
    expect(state.sent).toEqual([]);

    ime.start(); ime.end("late"); state.epoch += 1; vi.runAllTimers();       // between the end and its send
    expect(state.sent).toEqual([]);

    ime.start(); ime.end("held at send"); state.can = false; ime.flush(); state.can = true; vi.runAllTimers();
    expect(state.sent).toEqual([]);
  });

  it("a composition begun under a hold never commits", () => {
    const { state, ime } = harness();
    state.can = false; ime.start(); expect(state.showing).toEqual([false]);
    state.can = true; ime.end("typed under the menu"); vi.runAllTimers();
    expect(state.sent).toEqual([]);
  });

  it("text inserted with no composition is sent once; under a hold, never", () => {
    const { state, ime } = harness();
    expect(ime.input("insertText", "😀", false)).toBe(true);
    expect(state.sent).toEqual(["😀"]);
    state.can = false; expect(ime.input("insertText", "x", false)).toBe(true); expect(state.sent).toEqual(["😀"]);
    state.can = true;
    expect(ime.input("insertLineBreak", null, false)).toBe(true);
    expect(ime.input("insertCompositionText", "ni", true)).toBe(false);   // composition text is never sent here
    expect(state.sent).toEqual(["😀"]);
  });

  it("an insertText that Chrome marks as part of a composition goes with the commit, not twice", () => {
    const { state, ime } = harness();
    ime.start(); expect(ime.input("insertText", "ni", false)).toBe(false);
    ime.end("ni"); vi.runAllTimers(); expect(state.sent).toEqual(["ni"]);
  });

  it("interrupting always empties the proxy, which cancels the IME's composition", () => {
    const { state, ime } = harness();
    ime.start(); const before = state.clears; ime.interrupt();
    expect(state.clears).toBe(before + 1); expect(ime.composing).toBe(false); expect(state.showing.at(-1)).toBe(false);
  });
});

describe("routeKeyDown and the IME's keys", () => {
  const key = (k: string, extra: object = {}) => ({ key: k, code: "KeyN", ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...extra });
  it("drops any keydown with keyCode 229, whatever its key, so a composed key never also reaches the page", () => {
    expect(routeKeyDown(key("n", { keyCode: 229 }), false)).toEqual({ kind: "drop" });
    expect(routeKeyDown(key("n", { keyCode: 229, isComposing: true }), false)).toEqual({ kind: "drop" });
    expect(routeKeyDown(key("Process", { keyCode: 229 }), false)).toEqual({ kind: "drop" });
    expect(routeKeyDown(key("n", { keyCode: 78 }), false).kind).toBe("forward");
  });
});

describe("a composition cancelled under the IME", () => {
  it("drops the first late insertText, until a new composition or a forwarded key", () => {
    const { state, ime } = harness();
    ime.start(); ime.interrupt();                       // navigation, reconnect, card, press
    expect(ime.input("insertText", "到", false)).toBe(true);
    expect(state.sent).toEqual([]);
    ime.input("insertText", "😀", false); expect(state.sent).toEqual(["😀"]);   // only the first
    ime.start(); ime.interrupt(); ime.start(); ime.end("新"); vi.runAllTimers();
    expect(state.sent).toEqual(["😀", "新"]);
    ime.start(); ime.interrupt(); ime.forwarded(); ime.input("insertText", "😀", false);
    expect(state.sent).toEqual(["😀", "新", "😀"]);
  });
  it("an interruption with no composition open marks nothing stale", () => {
    const { state, ime } = harness();
    ime.start(); ime.end("ni"); ime.interrupt();        // Chrome's commit-on-blur, then the blur
    ime.input("insertText", "😀", false); vi.runAllTimers();
    expect(state.sent).toEqual(["😀"]);
  });
});

describe("a commit ending while an earlier one still waits", () => {
  // Unreachable through start() (which flushes first); pinned so a missed
  // compositionstart can never silently lose the earlier commit.
  it("sends the earlier one first, then the later, never losing either", () => {
    const { state, ime } = harness();
    ime.start(); ime.end("三");
    const raw = ime as unknown as { active: boolean; session: number | null };
    raw.active = true; raw.session = 1;          // a composition whose start Chrome never reported
    ime.end("四");
    expect(state.sent).toEqual(["三"]);
    vi.runAllTimers();
    expect(state.sent).toEqual(["三", "四"]);
  });
});
