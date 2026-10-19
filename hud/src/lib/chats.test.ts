import { describe, expect, it } from "vitest";
import {
  besideTarget, chatProjectId, chatTarget, composeRows, drawnShowing, eitherPane, emptyChat, emptyChats,
  giveBackPane, heldElsewhere, isEmptyChat, openRows, routeEvent, runningHere, selectedChatOf, shownThread,
  nonePerPane, sidebarPanes, type ChatState, type Chats,
} from "./chats";
import {
  PANE_NOS, defaultWorkspace, focusPane, orderOf, parseWorkspace, setPreset, setView, type PaneNo, type Preset, type View,
  type Workspace,
} from "./workspace";
import type { Thread } from "../types";

const chats = (over: Partial<Record<PaneNo, Partial<ChatState>>> = {}): Chats => {
  const all = emptyChats();
  for (const [n, c] of Object.entries(over)) all[Number(n) as PaneNo] = { ...all[Number(n) as PaneNo], ...c };
  return all;
};
const none = nonePerPane();

/** A workspace with these views and this focus. */
const shaped = (views: View[], preset: Preset = "cols2", focused: PaneNo = 1): Workspace => {
  let w = setPreset(defaultWorkspace(), preset);
  views.forEach((v, i) => {
    w = setView(w, (i + 1) as PaneNo, v);
  });
  return focusPane(w, focused);
};

const thread = (id: string, project_id: string): Thread => ({
  id, project_id, role: "chat", provider: "fast", provider_session_id: null, task_id: null,
  title: id, created: "", updated: "", turns: 0, cost_usd: 0, tokens: 0, model: null, effort: null,
});

describe("a pane's conversation", () => {
  it("starts empty, and is empty until it has a thread or a compose row", () => {
    expect(isEmptyChat(emptyChat())).toBe(true);
    expect(isEmptyChat({ ...emptyChat(), compose: { projectId: "p1" } })).toBe(false);
    expect(isEmptyChat({ ...emptyChat(), threadId: "t1" })).toBe(false);
  });

  it("shows its thread, else the one its compose row opened, else the one being opened", () => {
    expect(shownThread({ ...emptyChat(), threadId: "t1" }, "x")).toBe("t1");
    expect(shownThread({ ...emptyChat(), compose: { projectId: "p", openedId: "t9" } }, "x")).toBe("t9");
    expect(shownThread({ ...emptyChat(), compose: { projectId: "p" } }, "t5")).toBe("t5");
    expect(shownThread(emptyChat())).toBeNull();
  });

  it("is in its compose row's project, else its thread's", () => {
    const threads = [thread("t1", "p2")];
    expect(chatProjectId({ ...emptyChat(), compose: { projectId: "p3" } }, threads)).toBe("p3");
    expect(chatProjectId({ ...emptyChat(), threadId: "t1" }, threads)).toBe("p2");
    expect(chatProjectId({ ...emptyChat(), threadId: "gone" }, threads)).toBeNull();
  });

  it("offers Stop only for a turn running in the thread it shows", () => {
    expect(runningHere({ ...emptyChat(), threadId: "a", busy: true, turnThreadId: "a" })).toBe(true);
    // Switched away mid-turn: still waiting on it, but its Stop is not for this thread.
    expect(runningHere({ ...emptyChat(), threadId: "b", busy: true, turnThreadId: "a" })).toBe(false);
    expect(runningHere({ ...emptyChat(), threadId: "a", busy: false, turnThreadId: "a" })).toBe(false);
  });
});

describe("routing an event to its pane", () => {
  const c = chats({
    1: { threadId: "a" },
    2: { threadId: "b", busy: true, turnThreadId: "c" }, // moved on from c's turn
    3: { compose: { projectId: "p", openedId: "d" } },
  });

  it("sends a thread's deltas to the pane showing it, and only there", () => {
    expect(routeEvent("a", c, none, 1)).toEqual({ mine: [1], ours: [] });
    expect(routeEvent("b", c, none, 1).mine).toEqual([2]);
    expect(routeEvent("d", c, none, 1).mine).toEqual([3]);
  });

  it("sends a turn's finish to the pane waiting on it, even when it shows another thread", () => {
    expect(routeEvent("c", c, none, 1)).toEqual({ mine: [], ours: [2] });
  });

  it("knows a thread a pane is opening right now", () => {
    expect(routeEvent("e", c, { ...none, 4: "e" }, 1).mine).toEqual([4]);
  });

  it("sends an event with no thread to the selected chat, as it went to the one conversation", () => {
    expect(routeEvent(undefined, c, none, 3)).toEqual({ mine: [3], ours: [] });
    expect(routeEvent(null, c, none, 1)).toEqual({ mine: [1], ours: [] });
  });

  it("reaches no pane for a thread nobody shows or waits on", () => {
    expect(routeEvent("zz", c, none, 1)).toEqual({ mine: [], ours: [] });
  });

  it("joins the two sets once each, in pane order", () => {
    expect(eitherPane({ mine: [3, 1], ours: [1, 2] })).toEqual([1, 2, 3]);
  });
});

describe("handing words back", () => {
  const c = chats({ 1: { threadId: "a" }, 2: { threadId: "b" } });

  it("goes to the box of the pane showing the thread", () => {
    expect(giveBackPane("b", c, none)).toBe(2);
    expect(giveBackPane("a", c, none)).toBe(1);
  });

  it("holds words for a thread no pane shows, rather than writing them into another thread's box", () => {
    expect(giveBackPane("zz", c, none)).toBeNull();
  });

  it("with no thread yet, goes back to the pane holding its compose row, else to no box at all", () => {
    expect(giveBackPane(null, c, none, 2)).toBe(2);
    expect(giveBackPane(null, c, none, 2, [1])).toBeNull();
    expect(giveBackPane(null, c, none, 2, [])).toBeNull();
    // Never the selected chat, which may show another thread (W-6; re-review
    // of PR #27): the caller parks them under the compose row instead.
    expect(giveBackPane(null, c, none)).toBeNull();
    expect(giveBackPane(null, c, none, null)).toBeNull();
  });

  it("holds words for a thread only a pane off screen shows (review of PR #27)", () => {
    // A hidden box could be carried to another thread when two panes trade.
    expect(giveBackPane("b", c, none, null, [1])).toBeNull();
    expect(giveBackPane("b", c, none, 2, [1])).toBeNull();
    expect(giveBackPane("b", c, none, null, [1, 2])).toBe(2);
  });

  it("prefers the pane they were typed in when it shows the thread", () => {
    const both = chats({ 1: { threadId: "a" }, 2: { compose: { projectId: "p", openedId: "a" } } });
    expect(giveBackPane("a", both, none, 2)).toBe(2);
  });
});

describe("the selected chat: the chat pane most recently clicked (decisions W-6)", () => {
  it("is the focused pane when it is a drawn chat pane", () => {
    expect(selectedChatOf(shaped(["chat", "chat"], "cols2", 2), [1, 2], [1])).toBe(2);
  });

  it("stays put while the owner clicks a pane that is not a chat", () => {
    const w = shaped(["chat", "chat", "file"], "cols3", 3);
    expect(selectedChatOf(w, [1, 2, 3], [2, 1])).toBe(2);
    expect(selectedChatOf(w, [1, 2, 3], [1, 2])).toBe(1);
  });

  it("falls back to the most recently selected chat pane still drawn", () => {
    const w = shaped(["chat", "chat", "chat", "browser"], "grid4", 4);
    // pane 3 selected last, then the window dropped it: pane 1 was selected before pane 2
    expect(selectedChatOf(w, [2, 4], [3, 2, 1])).toBe(2);
    expect(selectedChatOf(w, [1, 4], [3, 2, 1])).toBe(1);
    expect(selectedChatOf(w, [1, 2, 3, 4], [3, 2, 1])).toBe(3);
  });

  it("is the one chat pane drawn, never selected before", () => {
    expect(selectedChatOf(shaped(["browser", "chat"], "cols2", 1), [1, 2], [])).toBe(2);
    expect(selectedChatOf(defaultWorkspace(), [1], [])).toBe(1);
  });

  it("is none with no chat pane drawn: ambiguous input has nowhere to go", () => {
    expect(selectedChatOf(shaped(["file"], "single", 1), [1], [1])).toBeNull();
    expect(selectedChatOf(shaped(["file", "chat"], "single", 1), [1], [2])).toBeNull();
  });
});

describe("where a thread click lands", () => {
  it("in the focused pane when it shows chat", () => {
    expect(chatTarget(shaped(["chat", "chat"], "cols2", 2), [1, 2], 1)).toEqual({ pane: 2, switches: false });
  });

  it("else in the selected chat", () => {
    expect(chatTarget(shaped(["chat", "chat", "file"], "cols3", 3), [1, 2, 3], 2)).toEqual({ pane: 2, switches: false });
  });

  it("else in a drawn chat pane, before the selected chat is known", () => {
    expect(chatTarget(shaped(["browser", "chat"], "cols2", 1), [1, 2], 1)).toEqual({ pane: 2, switches: false });
  });

  it("else the focused pane switches to chat — in the single layout, today's tab switch", () => {
    expect(chatTarget(shaped(["file", "browser"], "cols2", 2), [1, 2], 1)).toEqual({ pane: 2, switches: true });
    expect(chatTarget(shaped(["task"], "single", 1), [1], 1)).toEqual({ pane: 1, switches: true });
  });

  it("finds the drawn chat pane already showing a thread", () => {
    const w = shaped(["chat", "chat", "chat"], "cols3", 1);
    const c = chats({ 1: { threadId: "a" }, 3: { threadId: "b" } });
    expect(drawnShowing(w, [1, 2, 3], c, "b", none)).toBe(3);
    expect(drawnShowing(w, [1, 2], c, "b", none)).toBeNull(); // pane 3 not drawn
    expect(drawnShowing(shaped(["chat", "chat", "file"], "cols3", 1), [1, 2, 3], c, "b", none)).toBeNull();
  });

  it("finds a pane holding the thread off screen, never the pane it opens in", () => {
    const c = chats({ 1: { threadId: "a" }, 4: { compose: { projectId: "p", openedId: "b" } } });
    expect(heldElsewhere(c, 1, "b", none)).toBe(4);
    expect(heldElsewhere(c, 1, "a", none)).toBeNull();
    expect(heldElsewhere(c, 2, "a", none)).toBe(1);
  });
});

// FS4 (the FS4 card): Open beside's two grows are a split right — `{ pane, source, axis: "x" }` —
// and the `preset` field is gone; the other cases name only the pane.
describe("open beside", () => {
  it("from one pane, two columns: pane 2", () => {
    // A split since WP-G2: pane 2 goes right after pane 1; a split right since FS4 (no preset any more).
    expect(besideTarget(defaultWorkspace(), [1])).toEqual({ pane: 2, source: 1, axis: "x" });
  });

  it("the next drawn pane to the right of the focused one", () => {
    expect(besideTarget(shaped(["chat", "chat", "file"], "cols3", 1), [1, 2, 3])).toEqual({ pane: 2 });
    expect(besideTarget(shaped(["chat", "chat", "file"], "cols3", 2), [1, 2, 3])).toEqual({ pane: 3 });
  });

  it("from the right of two columns, a third column", () => {
    expect(besideTarget(shaped(["chat", "chat"], "cols2", 2), [1, 2])).toEqual({ pane: 3, source: 2, axis: "x" });
  });

  it("from the last pane of a grid, round to the first", () => {
    const w = shaped(["chat", "chat", "chat", "chat"], "grid4", 4);
    expect(besideTarget(w, [1, 2, 3, 4])).toEqual({ pane: 1 });
  });

  it("in a preset drawn as one pane for room, the next of its own panes", () => {
    expect(besideTarget(shaped(["chat", "chat"], "cols2", 1), [1])).toEqual({ pane: 2 });
  });
});

describe("what the sidebar marks", () => {
  const w = shaped(["chat", "chat", "file", "chat"], "cols3", 2);

  it("every drawn chat pane, and the selected chat's conversation even off screen", () => {
    expect(sidebarPanes(w, [1, 2, 3], 2)).toEqual([1, 2]);
    expect(sidebarPanes(shaped(["file"], "single", 1), [1], 1)).toEqual([1]);
    expect(sidebarPanes(w, [1, 2, 3], 4)).toEqual([1, 2, 4]);
  });

  it("each open thread with its pane, the active one flagged", () => {
    const c = chats({ 1: { threadId: "a" }, 2: { threadId: "b" }, 4: { threadId: "c" } });
    expect(openRows(w, [1, 2, 3], c, 2)).toEqual([
      { pane: 1, threadId: "a", active: false },
      { pane: 2, threadId: "b", active: true },
    ]);
  });

  it("a compose row per composing pane, numbered by pane only when more than one pane is marked", () => {
    const c = chats({ 1: { compose: { projectId: "p1" } }, 2: { compose: { projectId: "p2" } } });
    expect(composeRows(w, [1, 2, 3], c, 1).map((r) => [r.pane, r.label, r.active])).toEqual([
      [1, "New thread · 1", true], [2, "New thread · 2", false],
    ]);
    const single = shaped(["chat"], "single", 1);
    expect(composeRows(single, [1], c, 1).map((r) => r.label)).toEqual(["New thread"]);
    // A pane with a thread is not composing, whatever its compose field says.
    expect(composeRows(single, [1], chats({ 1: { threadId: "t", compose: { projectId: "p" } } }), 1)).toEqual([]);
  });
});

describe("eight panes (WP-G1)", () => {
  it("holds one conversation per pane id, twelve of them (FS2)", () => {
    expect(Object.keys(emptyChats()).map(Number)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    expect(Object.keys(emptyChats()).map(Number)).toEqual([...PANE_NOS]);
    expect(Object.values(nonePerPane())).toEqual(Array(12).fill(null));
  });

  it("routes an event to a pane above 8 showing its thread (FS2)", () => {
    const c = chats(Object.fromEntries(PANE_NOS.map((n) => [n, { threadId: `t${n}` }])));
    expect(routeEvent("t10", c, none, 1)).toEqual({ mine: [10], ours: [] });
    expect(routeEvent("t12", { ...c, 9: { ...c[9], turnThreadId: "t12" } }, none, 1)).toEqual({ mine: [12], ours: [9] });
    expect(heldElsewhere(c, 1, "t11", none)).toBe(11);
  });

  it("routes an event to the one of eight panes showing its thread, and nowhere else", () => {
    const c = chats(Object.fromEntries(PANE_NOS.map((n) => [n, { threadId: `t${n}` }])));
    expect(routeEvent("t7", c, none, 1)).toEqual({ mine: [7], ours: [] });
    expect(routeEvent("t8", { ...c, 6: { ...c[6], turnThreadId: "t8" } }, none, 1)).toEqual({ mine: [8], ours: [6] });
    expect(heldElsewhere(c, 1, "t5", none)).toBe(5);
    expect(giveBackPane("t6", c, none, null, [5, 6, 7])).toBe(6);
  });

  it("selects among eight drawn chat panes by the order they were used", () => {
    const w = focusPane(setPreset(defaultWorkspace(), "grid8"), 3);
    const all = setView(w, 3, "file");
    expect(selectedChatOf(all, [1, 2, 3, 4, 5, 6, 7, 8], [6, 2])).toBe(6);
    expect(selectedChatOf(all, [1, 2, 3, 5, 6, 7], [8, 6])).toBe(6);
  });

  it("numbers compose rows by slot — the place the owner sees — never by id", () => {
    let w = parseWorkspace(JSON.stringify({ preset: "cols3", order: [1, 5, 2],
                                            panes: [{ view: "chat" }, { view: "chat" }] }));
    w = focusPane(w, 5);
    const c = chats({ 1: { compose: { projectId: "p" } }, 2: { compose: { projectId: "p" } }, 5: { compose: { projectId: "p" } } });
    expect(composeRows(w, [1, 5, 2], c, 5).map((r) => [r.pane, r.label])).toEqual([
      [1, "New thread · 1"], [5, "New thread · 2"], [2, "New thread · 3"],
    ]);
    expect(sidebarPanes(w, [1, 5, 2], 5)).toEqual([1, 5, 2]);
  });

  it("opens beside by a split into a free id, so the panes ⊞ hid keep what they showed (WP-G2)", () => {
    const one = setPreset(parseWorkspace(JSON.stringify({ preset: "cols3", order: [1, 5, 2],
                                                          panes: [{ view: "chat" }, { view: "chat" }] })), "single");
    expect(one.kept).toEqual([5, 2]);
    expect(besideTarget(one, [1])).toEqual({ pane: 3, source: 1, axis: "x" });
    const two = setPreset(one, "cols2");
    expect(besideTarget(focusPane(two, 5), [1, 5])).toEqual({ pane: 3, source: 5, axis: "x" });
  });

  it("after ⊞ 8 → 1 opens beside into the free id 9, never a hidden pane (FS2)", () => {
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    expect(one.kept).toEqual([2, 3, 4, 5, 6, 7, 8]);
    expect(besideTarget(one, [1])).toEqual({ pane: 9, source: 1, axis: "x" });
    expect(besideTarget(one, [1], [8, 7])).toEqual({ pane: 9, source: 1, axis: "x" });
    expect(besideTarget(one, [1], [2, 3, 4, 5, 6, 7, 8])).toEqual({ pane: 9, source: 1, axis: "x" });
  });

  it("takes the oldest hidden pane with no unsaved edit when every id is used, never a dirty one", () => {
    // Twelve ids used, one drawn (FS2): 1 drawn, the other eleven hidden, 8 hidden longest (it is last).
    const one = parseWorkspace(JSON.stringify({ preset: "single", order: [1], kept: [9, 10, 11, 12, 2, 3, 4, 5, 6, 7, 8],
                                                panes: Array.from({ length: 12 }, () => ({ view: "chat" })) }));
    expect([orderOf(one), one.kept]).toEqual([[1], [9, 10, 11, 12, 2, 3, 4, 5, 6, 7, 8]]);
    expect(besideTarget(one, [1])).toEqual({ pane: 8, source: 1, axis: "x" });
    expect(besideTarget(one, [1], [8, 7])).toEqual({ pane: 6, source: 1, axis: "x" });
    expect(besideTarget(one, [1], [2, 3, 4, 5, 6, 7, 8])).toEqual({ pane: 12, source: 1, axis: "x" });
    // All of them dirty: no split and no ⊞ grow any more (FS4: no preset field). Whatever pane it
    // names is drawn, and nothing hidden with an unsaved edit is brought back.
    const none = besideTarget(one, [1], [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    expect(none.source).toBeUndefined();
    expect("preset" in none).toBe(false);
    expect([1]).toContain(none.pane);
  });
});

describe("with pane 1 closed (WP-G2): the fallbacks are the first pane in reading order (U36/U37)", () => {
  // Only pane 3 left, showing a file; nothing drawn yet (no window to fit).
  const w = { ...defaultWorkspace(), tree: { pane: 3 as PaneNo }, focused: 3 as PaneNo };

  it("a chat with nothing drawn lands in that pane, which switches to chat", () => {
    expect(chatTarget(w, [], null)).toEqual({ pane: 3, switches: true });
  });

  it("Open beside with nothing drawn splits after that pane", () => {
    expect(besideTarget(w, [])).toMatchObject({ source: 3, axis: "x" });
    expect("preset" in besideTarget(w, [])).toBe(false);
    expect(besideTarget(w, []).pane).not.toBe(3);
  });
});
