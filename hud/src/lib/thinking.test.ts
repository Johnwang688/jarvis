import { describe, expect, it } from "vitest";
import { TOOL_NAME_CAP, elapsed, thinkingLine, toolName, type TurnEntry } from "./thinking";
import { emptyChat, type ChatState } from "./chats";
import type { ToolOp } from "../types";

const T0 = 1_000_000;
const chat = (over: Partial<ChatState> = {}): ChatState => ({ ...emptyChat(), threadId: "t1", ...over });
const turn = (over: Partial<TurnEntry> = {}): TurnEntry => ({ since: T0, error: "", seen: T0, ...over });
const op = (name: string, over: Partial<ToolOp> = {}): ToolOp => ({ call_id: name, name, started: T0 + 10, ...over });

/** Everything true at once: each row of the table, removed in turn, gives the next. */
const ALL = {
  chat: chat({ busy: true, turnThreadId: "t1", ops: [op("ls")] }),
  threadId: "t1",
  turn: turn({ error: "during" }),
  activity: "needs_input" as const,
  approvalHere: true,
};

describe("the thinking line's precedence (plan §4)", () => {
  it("an error in the running turn first: red, still moving, with its time", () => {
    expect(thinkingLine(ALL)).toEqual({ word: "error", text: "Error", orb: "error", since: T0, moving: true });
  });

  it("then a turn that ended on an error: static, no time", () => {
    expect(thinkingLine({ ...ALL, turn: turn({ error: "ended" }) }))
      .toEqual({ word: "ended", text: "Ended on an error", orb: "error", since: null, moving: false });
  });

  it("then an approval for this thread — ahead of a tool call running in this pane", () => {
    const line = thinkingLine({ ...ALL, turn: turn() });
    expect(line).toMatchObject({ word: "approval", text: "Waiting for approval", orb: "approval", since: T0 });
  });

  it("then a question: needs_input with no approval", () => {
    const line = thinkingLine({ ...ALL, turn: turn(), approvalHere: false });
    expect(line).toMatchObject({ word: "answer", text: "Waiting for your answer", orb: "approval" });
  });

  it("then a tool call running in this pane", () => {
    const line = thinkingLine({ ...ALL, turn: turn(), approvalHere: false, activity: "working" });
    expect(line).toEqual({ word: "tool", text: "Running ls…", orb: "tool", since: T0, moving: true });
  });

  it("then the turn itself: Thinking…", () => {
    const line = thinkingLine({ ...ALL, turn: turn(), approvalHere: false, activity: "working",
                                chat: chat({ busy: true, turnThreadId: "t1", ops: [op("ls", { finished: T0 + 20 })] }) });
    expect(line).toEqual({ word: "thinking", text: "Thinking…", orb: "thinking", since: T0, moving: true });
  });

  it("and nothing at all when no turn runs", () => {
    expect(thinkingLine({ chat: chat(), threadId: "t1", approvalHere: false })).toBeNull();
    expect(thinkingLine({ chat: chat({ ops: [op("ls")] }), threadId: "t1", approvalHere: false, activity: "idle" }))
      .toBeNull();
  });

  it("a tool call ahead of plain thinking, and the newest one running is named", () => {
    const c = chat({ busy: true, turnThreadId: "t1", ops: [op("first"), op("second", { started: T0 + 30 })] });
    expect(thinkingLine({ chat: c, threadId: "t1", turn: turn(), approvalHere: false })?.text).toBe("Running second…");
  });
});

describe("lines the pane did not start (activity, the window's turns)", () => {
  it("a question with no turn tracked anywhere still says so", () => {
    expect(thinkingLine({ chat: chat(), threadId: "t1", activity: "needs_input", approvalHere: false }))
      .toMatchObject({ word: "answer", since: null });
  });

  it("an approval for this thread with nothing tracked", () => {
    expect(thinkingLine({ chat: chat(), threadId: "t1", approvalHere: true })).toMatchObject({ word: "approval" });
  });

  it("a turn the window holds an entry for, with the pane not busy (Discord, a reload): Thinking…", () => {
    expect(thinkingLine({ chat: chat(), threadId: "t1", turn: turn({ since: T0 - 5000 }), approvalHere: false }))
      .toEqual({ word: "thinking", text: "Thinking…", orb: "thinking", since: T0 - 5000, moving: true });
  });

  it("'working' alone is not a turn: the entry it makes is (so a finish heard first is never outlived)", () => {
    expect(thinkingLine({ chat: chat(), threadId: "t1", activity: "working", approvalHere: false })).toBeNull();
  });

  it("the static ended error, whatever the pane is doing", () => {
    for (const c of [chat(), chat({ busy: true, turnThreadId: "t1" })]) {
      expect(thinkingLine({ chat: c, threadId: "t1", turn: turn({ error: "ended" }), approvalHere: false }))
        .toMatchObject({ text: "Ended on an error", moving: false, since: null });
    }
  });

  it("a pane tracking another thread's turn draws nothing for the thread it shows", () => {
    const c = chat({ threadId: "t2", busy: true, turnThreadId: "t1" });
    expect(thinkingLine({ chat: c, threadId: "t2", approvalHere: false })).toBeNull();
  });

  it("an approval or a question for another thread is not this pane's", () => {
    // The caller says whether an approval is this thread's; a compose row has no thread to own one.
    expect(thinkingLine({ chat: chat({ threadId: null }), threadId: null, approvalHere: true, activity: "needs_input" }))
      .toBeNull();
  });

  it("a first send on its way, before its thread exists: Thinking… with no time", () => {
    const c = chat({ threadId: null, compose: { projectId: "p1", sending: "local-1" } as any, busy: true,
                     turnThreadId: "local-1" });
    expect(thinkingLine({ chat: c, threadId: null, approvalHere: false }))
      .toEqual({ word: "thinking", text: "Thinking…", orb: "thinking", since: null, moving: true });
  });

  it("a tool call an earlier turn left unfinished is not this turn's", () => {
    const c = chat({ busy: true, turnThreadId: "t1", ops: [op("stale", { started: T0 - 60_000 })] });
    expect(thinkingLine({ chat: c, threadId: "t1", turn: turn(), approvalHere: false })?.text).toBe("Thinking…");
  });
});

describe("what the line says, as text", () => {
  it("a tool name with control characters and 200 characters is one line, capped", () => {
    const hostile = "rm\n-rf\r\u0007\u202e\u200b/\u2028tmp " + "x".repeat(200);
    const name = toolName(hostile);
    expect(name).not.toMatch(/[\n\r\u0007\u202e\u200b\u2028]/);
    expect(Array.from(name).length).toBeLessThanOrEqual(TOOL_NAME_CAP);
    expect(name.startsWith("rm -rf / tmp x")).toBe(true);
    expect(name.endsWith("…")).toBe(true);
    const c = chat({ busy: true, turnThreadId: "t1", ops: [op(hostile)] });
    const line = thinkingLine({ chat: c, threadId: "t1", turn: turn(), approvalHere: false })!;
    expect(line.text).toBe(`Running ${name}…`);
    expect(line.text).not.toMatch(/[\n\r]/);
  });

  it("a short name is left as it is, and an empty one reads 'tool'", () => {
    expect(toolName("grep_files")).toBe("grep_files");
    expect(toolName("")).toBe("tool");
    expect(toolName("\u200b\n")).toBe("tool");
  });

  it("never says Authorize, Approve or Deny: it is not the card", () => {
    const lines = [
      thinkingLine(ALL), thinkingLine({ ...ALL, turn: turn({ error: "ended" }) }), thinkingLine({ ...ALL, turn: turn() }),
      thinkingLine({ ...ALL, turn: turn(), approvalHere: false }),
      thinkingLine({ ...ALL, turn: turn(), approvalHere: false, activity: "working" }),
      thinkingLine({ chat: chat(), threadId: "t1", turn: turn(), approvalHere: false }),
    ];
    for (const l of lines) expect(l!.text).not.toMatch(/authori[sz]e|approve|deny/i);
  });
});

describe("the elapsed time", () => {
  it("is 12s, 3m 07s, 1h 02m", () => {
    expect(elapsed(0)).toBe("0s");
    expect(elapsed(12_400)).toBe("12s");
    expect(elapsed(59_999)).toBe("59s");
    expect(elapsed(60_000)).toBe("1m 00s");
    expect(elapsed(187_000)).toBe("3m 07s");
    expect(elapsed(3_599_000)).toBe("59m 59s");
    expect(elapsed(3_720_000)).toBe("1h 02m");
    expect(elapsed(36_000_000 + 5 * 60_000)).toBe("10h 05m");
  });

  it("never runs backwards: a start ahead of the clock reads 0s", () => {
    expect(elapsed(-4000)).toBe("0s");
  });

  it("no start, no time: the line's since is null", () => {
    expect(thinkingLine({ chat: chat(), threadId: "t1", turn: turn({ since: null }), approvalHere: false })?.since)
      .toBeNull();
  });
});
