// SV-5: the spoken notice for native sub-agents, alone (notices.ts) with a fake clock.
// The controller's side -- which threads are read, a send, Stop, barge-in -- is in
// noticesController.test.ts.

import { beforeEach, describe, expect, it } from "vitest";
import source from "./notices.ts?raw";
import {
  BUSY_MAX_MS, BUSY_RETRY_MS, NOTICE_WINDOW_MS, SEEN_CAP, SubagentNotices, noticeText,
} from "./notices";
import { FakeClock } from "./testkit";

let clock: FakeClock;
let said: { thread: string; text: string }[];
let can: boolean;
let busy: boolean;
let n: SubagentNotices;

const T = "t1";
const rec = (sid: string, status: string, op = "finished", extra: Record<string, unknown> = {}) =>
  ({ sid, op, status, depth: 1, background: false, started_at: "2026-10-16T10:00:00Z", entries: 3, ...extra });

beforeEach(() => {
  clock = new FakeClock();
  said = [];
  can = true;
  busy = false;
  n = new SubagentNotices({
    clock,
    speak: (thread, text) => said.push({ thread, text }),
    canSpeak: () => can,
    busy: () => busy,
  });
});

describe("the sentence", () => {
  it("is a fixed vocabulary and a number", () => {
    expect(noticeText(1, 0)).toBe("A sub-agent finished.");
    expect(noticeText(0, 1)).toBe("A sub-agent failed.");
    expect(noticeText(2, 0)).toBe("Two sub-agents finished.");
    expect(noticeText(0, 3)).toBe("Three sub-agents failed.");
    expect(noticeText(2, 1)).toBe("Two sub-agents finished, one failed.");
    expect(noticeText(1, 2)).toBe("One sub-agent finished, two failed.");
    expect(noticeText(12, 0)).toBe("12 sub-agents finished.");
    expect(noticeText(0, 0)).toBe("");
  });
});

describe("coalescing", () => {
  it("says one ending once, when the window closes", async () => {
    n.heard(T, rec("cl:a", "done"));
    await clock.advance(NOTICE_WINDOW_MS - 1);
    expect(said).toEqual([]);
    await clock.advance(1);
    expect(said).toEqual([{ thread: T, text: "A sub-agent finished." }]);
    await clock.advance(60_000);
    expect(said.length).toBe(1);
  });

  it("makes a burst one sentence, with the failures counted apart", async () => {
    n.heard(T, rec("cl:a", "done"));
    await clock.advance(1000);
    n.heard(T, rec("cl:b", "done"));
    await clock.advance(1000);
    n.heard(T, rec("cx:c", "failed"));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said).toEqual([{ thread: T, text: "Two sub-agents finished, one failed." }]);
  });

  it("an ending after the window is a new sentence (the window is fixed, not sliding)", async () => {
    n.heard(T, rec("cl:a", "done"));
    await clock.advance(NOTICE_WINDOW_MS + 500);
    n.heard(T, rec("cl:b", "failed"));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said.map((s) => s.text)).toEqual(["A sub-agent finished.", "A sub-agent failed."]);
  });

  it("keeps each chat's burst apart", async () => {
    n.heard("t1", rec("cl:a", "done"));
    n.heard("t2", rec("cl:b", "done"));
    n.heard("t2", rec("cl:c", "done"));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said).toEqual([{ thread: "t1", text: "A sub-agent finished." }, { thread: "t2", text: "Two sub-agents finished." }]);
  });

  it("counts a nested child and a Codex child like any other", async () => {
    n.heard(T, rec("cl:outer", "done", "finished", { depth: 1 }));
    n.heard(T, rec("cl:inner", "done", "finished", { depth: 2, parent_sid: "cl:outer" }));
    n.heard(T, rec("cx:thread-9", "done"));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said.map((s) => s.text)).toEqual(["Three sub-agents finished."]);
  });
});

describe("what is never said", () => {
  it("a child the CLI or Jarvis stopped (the owner's Stop, the main Stop, a session that ended)", async () => {
    n.heard(T, rec("cl:a", "stopped", "finished", { stop_reason: "stopped by the owner" }));
    n.heard(T, rec("cx:b", "stopped"));
    await clock.advance(60_000);
    expect(said).toEqual([]);
  });

  it("a child the owner asked to stop, however it ends (a killed child can be reported as failed)", async () => {
    n.heard(T, rec("cl:a", "running", "progress", { stop: "stopping" }));
    n.heard(T, rec("cl:a", "failed"));
    n.heard(T, rec("cl:b", "backgrounded", "progress", { stop: "unconfirmed" }));
    n.heard(T, rec("cl:b", "done"));
    await clock.advance(60_000);
    expect(said).toEqual([]);
  });

  it("the CLI's refusal of the owner's Stop leaves the child an ordinary one", async () => {
    n.heard(T, rec("cl:a", "running", "progress", { stop: "stopping" }));
    n.heard(T, rec("cl:a", "running", "progress", { stop: "refused" }));
    n.heard(T, rec("cl:a", "done"));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said.map((s) => s.text)).toEqual(["A sub-agent finished."]);
  });

  it("an ending already said is not said again; a progress record about an ended child is not an ending", async () => {
    n.heard(T, rec("cl:a", "done"));
    n.heard(T, rec("cl:a", "done"));
    n.heard(T, rec("cl:a", "done", "progress"));
    n.heard(T, rec("cl:z", "done", "progress"));
    n.heard(T, rec("cl:y", "done", "started"));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said.map((s) => s.text)).toEqual(["A sub-agent finished."]);
  });

  it("a child that ran again (Codex) and ended again is said again; one ended by an inferred stop and later finished is said", async () => {
    n.heard(T, rec("cx:a", "done"));
    await clock.advance(NOTICE_WINDOW_MS);
    n.heard(T, rec("cx:a", "running", "started"));
    n.heard(T, rec("cx:a", "done"));
    n.heard(T, rec("cl:b", "stopped", "finished", { stop_reason: "the turn ended" }));
    n.heard(T, rec("cl:b", "done"));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said.map((s) => s.text)).toEqual(["A sub-agent finished.", "Two sub-agents finished."]);
  });

  it("anything that is not a bus record of a known child and status", async () => {
    for (const bad of [null, undefined, "done", 7, [], {}, rec("toolu_a", "done"), rec("cl:a b", "done"), rec("cl:a", "weird"), rec("cl:a", "")]) n.heard(T, bad);
    await clock.advance(60_000);
    expect(said).toEqual([]);
  });

  it("a child's name, kind, last tool, reason or words -- whatever a record carries -- never reaches the sentence", async () => {
    const hostile = {
      name: "SECRET-NAME rm -rf", kind: "reviewer", last_tool: "Bash", stop_reason: "SECRET-REASON",
      text: "SECRET-TEXT", brief: "SECRET-BRIEF", summary: "SECRET-SUMMARY", label: "SECRET-LABEL", prompt: "SECRET-PROMPT",
    };
    n.heard(T, rec("cl:a", "done", "finished", hostile));
    n.heard(T, rec("cx:b", "failed", "finished", hostile));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said).toEqual([{ thread: T, text: "One sub-agent finished, one failed." }]);
    expect(JSON.stringify(said)).not.toMatch(/SECRET|reviewer|Bash/);
  });

  it("the file reads nothing of a record but sid, status, op and stop", () => {
    const code = source.replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const field of ["name", "kind", "last_tool", "stop_reason", "brief", "text", "summary", "label", "prompt"]) {
      expect(code, field).not.toMatch(new RegExp(`\\brec\\.${field}\\b|\\["${field}"\\]|data\\.${field}\\b`));
    }
    expect([...new Set([...code.matchAll(/\brec\.(\w+)/g)].map((m) => m[1]))].sort()).toEqual(["op", "sid", "status", "stop"]);
  });

  it("the file imports only the clock's type: no route, no Discord, no audio", () => {
    expect([...source.matchAll(/^import .* from "(.*)";$/gm)].map((m) => m[1])).toEqual(["./queue"]);
    expect(source).not.toMatch(/fetch\(|XMLHttpRequest|discord/i);
  });
});

describe("who and when", () => {
  it("says nothing for a chat that is not read when the window closes", async () => {
    n.heard(T, rec("cl:a", "done"));
    can = false;
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said).toEqual([]);
    expect(n.waiting(T)).toEqual({ done: 0, failed: 0 });
  });

  it("waits while the owner is talking, then says it; gives up after BUSY_MAX_MS", async () => {
    busy = true;
    n.heard(T, rec("cl:a", "done"));
    await clock.advance(NOTICE_WINDOW_MS + 5 * BUSY_RETRY_MS);
    expect(said).toEqual([]);
    n.heard(T, rec("cl:b", "done"));                // joins the waiting sentence
    busy = false;
    await clock.advance(BUSY_RETRY_MS);
    expect(said.map((s) => s.text)).toEqual(["Two sub-agents finished."]);

    said.length = 0;
    busy = true;
    n.heard(T, rec("cl:c", "done"));
    await clock.advance(NOTICE_WINDOW_MS + BUSY_MAX_MS + BUSY_RETRY_MS);
    busy = false;
    await clock.advance(60_000);
    expect(said).toEqual([]);
    expect(clock.pendingTimers()).toBe(0);
  });

  it("a busy check that throws counts as busy", async () => {
    const boom = new SubagentNotices({ clock, speak: (t, x) => said.push({ thread: t, text: x }), canSpeak: () => true, busy: () => { throw new Error("x"); } });
    boom.heard(T, rec("cl:a", "done"));
    await clock.advance(NOTICE_WINDOW_MS + BUSY_MAX_MS + BUSY_RETRY_MS * 2);
    expect(said).toEqual([]);
  });

  it("cancel drops what waits, for one chat or all", async () => {
    n.heard("t1", rec("cl:a", "done"));
    n.heard("t2", rec("cl:b", "done"));
    n.cancel("t1");
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said.map((s) => s.thread)).toEqual(["t2"]);
    n.heard("t1", rec("cl:c", "done"));
    n.cancel();
    await clock.advance(60_000);
    expect(said.length).toBe(1);
    expect(clock.pendingTimers()).toBe(0);
  });

  it("off drops what waits and queues nothing; on again resumes", async () => {
    n.heard(T, rec("cl:a", "done"));
    n.setEnabled(false);
    n.heard(T, rec("cl:b", "done"));
    await clock.advance(60_000);
    expect(said).toEqual([]);
    expect(n.isEnabled()).toBe(false);
    n.setEnabled(true);
    n.heard(T, rec("cl:c", "done"));
    await clock.advance(NOTICE_WINDOW_MS);
    expect(said.map((s) => s.text)).toEqual(["A sub-agent finished."]);
  });

  it("an owner Stop heard for a chat nobody reads still silences the ending later", async () => {
    can = false;
    n.heard(T, rec("cl:a", "running", "progress", { stop: "stopping" }));
    can = true;
    n.heard(T, rec("cl:a", "failed"));
    await clock.advance(60_000);
    expect(said).toEqual([]);
  });

  it("its memory is bounded", () => {
    for (let i = 0; i < SEEN_CAP * 3; i++) {
      n.heard(T, rec(`cl:s${i}`, "running", "progress", { stop: "stopping" }));
      n.heard(T, rec(`cl:d${i}`, "done"));
    }
    const mem = n as unknown as { announced: Set<string>; asked: Set<string> };
    expect(mem.announced.size).toBeLessThanOrEqual(SEEN_CAP);
    expect(mem.asked.size).toBeLessThanOrEqual(SEEN_CAP);
  });
});
