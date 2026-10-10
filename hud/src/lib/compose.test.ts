import { describe, expect, it } from "vitest";
import {
  activeProjectId, composeMovable, conversationThreadId, folderName, lastProject, loadLastProject, movedAway,
  saveLastProject, LAST_PROJECT_KEY,
} from "./compose";
import type { Project, Task, Thread } from "../types";
import { chatProjectId, emptyChat, emptyChats, type ChatState } from "./chats";
import { currentProjectId, initialState, type State } from "../state/store";

const project = (id: string, over: Partial<Project> = {}): Project => ({
  id, name: id, root: `/home/johnw/${id}`, created: "", profile: "auto",
  routing: { chains: {}, models: {}, no_new_work: null } as any,
  discord_channel_id: null, extra_dirs: [], always_ask: [], inbox: false, ...over,
});

const thread = (id: string, project_id: string, updated: string, over: Partial<Thread> = {}): Thread => ({
  id, project_id, role: "chat", provider: "fast", provider_session_id: null, task_id: null,
  title: "", created: "", updated, turns: 0, cost_usd: 0, tokens: 0, model: null, effort: null,
  ...over,
});

const PROJECTS = [project("p1"), project("p2"), project("inbox", { inbox: true })];

describe("the project a new thread starts in", () => {
  it("is the last project this window sent in", () => {
    expect(lastProject(PROJECTS, [], "p2")).toBe("p2");
  });

  it("ignores a remembered project that no longer exists", () => {
    const threads = [thread("t1", "p1", "2026-10-01T00:00:00Z")];
    expect(lastProject(PROJECTS, threads, "gone")).toBe("p1");
  });

  it("otherwise follows the most recently active chat thread", () => {
    const threads = [
      thread("t1", "p1", "2026-10-01T00:00:00Z"),
      thread("t2", "p2", "2026-10-03T00:00:00Z"),
      // A task's thread is work the runner did, not the owner's conversation.
      thread("kt", "p1", "2026-10-05T00:00:00Z", { task_id: "k1", role: "implementer" }),
    ];
    expect(lastProject(PROJECTS, threads, null)).toBe("p2");
  });

  it("falls back to the inbox, then the first project", () => {
    expect(lastProject(PROJECTS, [], null)).toBe("inbox");
    expect(lastProject([project("a"), project("b")], [], null)).toBe("a");
  });

  it("is null with no projects at all", () => {
    expect(lastProject([], [], "p1")).toBeNull();
  });
});

describe("the remembered project", () => {
  it("round-trips through storage", () => {
    const store = new Map<string, string>();
    const fake = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) } as any;
    saveLastProject("p2", fake);
    expect(store.get(LAST_PROJECT_KEY)).toBe("p2");
    expect(loadLastProject(fake)).toBe("p2");
  });

  it("treats storage that throws as nothing remembered", () => {
    const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } } as any;
    expect(loadLastProject(broken)).toBeNull();
    expect(() => saveLastProject("p1", broken)).not.toThrow();
  });
});

describe("the project the window is about", () => {
  const threads = [thread("t1", "p1", ""), thread("t2", "p2", "")];
  const tasks = [{ id: "k1", project_id: "inbox" } as Task];
  const base = { compose: null, threadId: null, taskId: null, taskFocus: false, threads, tasks };

  it("is the compose project while composing", () => {
    expect(activeProjectId({ ...base, compose: { projectId: "p2" }, threadId: "t1" })).toBe("p2");
  });

  it("is the open thread's project, which a move changes", () => {
    expect(activeProjectId({ ...base, threadId: "t2" })).toBe("p2");
    const moved = [thread("t1", "p2", ""), threads[1]];
    expect(activeProjectId({ ...base, threadId: "t1", threads: moved })).toBe("p2");
  });

  it("is the task's project when a task was picked last", () => {
    expect(activeProjectId({ ...base, threadId: "t1", taskId: "k1", taskFocus: true })).toBe("inbox");
    expect(activeProjectId({ ...base, threadId: "t1", taskId: "k1", taskFocus: false })).toBe("p1");
  });

  it("is null with nothing open", () => {
    expect(activeProjectId(base)).toBeNull();
  });
});

describe("several chats: the project per pane (WP-B)", () => {
  const threads = [thread("t1", "p1", ""), thread("t2", "p2", "")];
  const tasks = [{ id: "k1", project_id: "inbox" } as Task];
  const pane = (over: Partial<ChatState>): ChatState => ({ ...emptyChat(), ...over });
  const window = (chats: Partial<Record<1 | 2, ChatState>>, voiceTarget: 1 | 2, over: Partial<State> = {}): State => ({
    ...initialState, threads, tasks, chats: { ...emptyChats(), ...chats }, voiceTarget, ...over,
  });

  it("each chat pane's chip is its own conversation's project", () => {
    expect(chatProjectId(pane({ threadId: "t1" }), threads)).toBe("p1");
    expect(chatProjectId(pane({ compose: { projectId: "p2" } }), threads)).toBe("p2");
  });

  it("the project the window is about is the voice target's conversation's — the chat used last", () => {
    const s = window({ 1: pane({ threadId: "t1" }), 2: pane({ compose: { projectId: "p2" } }) }, 1);
    expect(currentProjectId(s)).toBe("p1");
    expect(currentProjectId({ ...s, voiceTarget: 2 })).toBe("p2");
  });

  it("and a task picked more recently still wins, as it did", () => {
    const s = window({ 1: pane({ threadId: "t1" }) }, 1, { taskId: "k1", taskFocus: true });
    expect(currentProjectId(s)).toBe("inbox");
  });

  it("a pane's thread is what its chips read, never another pane's", () => {
    expect(conversationThreadId(pane({ threadId: "t2" }))).toBe("t2");
    expect(conversationThreadId(pane({ compose: { projectId: "p1", openedId: "t9" } }))).toBe("t9");
  });
});

describe("dragging the compose row", () => {
  it("moves to another project, with nothing on the server yet", () => {
    expect(composeMovable({ projectId: "p1" }, "p2")).toBe(true);
  });

  it("not onto its own project", () => {
    expect(composeMovable({ projectId: "p1" }, "p1")).toBe(false);
  });

  it("not once the thread exists: then it is a thread move", () => {
    expect(composeMovable({ projectId: "p1", openedId: "t9" }, "p2")).toBe(false);
  });

  it("not when there is no compose at all", () => {
    expect(composeMovable(null, "p2")).toBe(false);
  });
});

describe("the folder a thread works in", () => {
  it("names the last path component", () => {
    expect(folderName("/home/johnw/projects/Jarvis/")).toBe("Jarvis");
    expect(folderName("/mnt/c/myday/schoolwork")).toBe("schoolwork");
    expect(folderName(null)).toBe("");
  });

  it("is flagged when it is not the project's root (the thread was moved)", () => {
    const p1 = project("p1");
    expect(movedAway(thread("t", "p1", "", { cwd: "/home/johnw/p1/" }), p1)).toBe(false);
    expect(movedAway(thread("t", "p1", "", { cwd: "/home/johnw/p2" }), p1)).toBe(true);
    expect(movedAway(thread("t", "p1", "", { cwd: null }), p1)).toBe(false);
  });
});

describe("conversationThreadId", () => {
  it("is the open thread, else the thread a failed first send opened", () => {
    expect(conversationThreadId({ threadId: "t1", compose: null })).toBe("t1");
    expect(conversationThreadId({ threadId: null, compose: { projectId: "p1", openedId: "t9" } })).toBe("t9");
    expect(conversationThreadId({ threadId: null, compose: { projectId: "p1" } })).toBeNull();
  });
});
