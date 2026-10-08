import { describe, expect, it } from "vitest";
import {
  afterProjectGone, afterThreadGone, archiveSummary, deletedNotice, deleteSummary, editEffects, forgetLastProject,
  formFrom, nameKey, projectEditBody, projectNamesTaken, threadTitlesTaken, uniqueName,
} from "./projects";
import { LAST_PROJECT_KEY } from "./compose";
import type { Project, ProjectImpact, Task, Thread } from "../types";

// The same table as tests/v2/archive_check.py NAME_TABLE: the window's
// "saves as" preview and the backend's decision must agree.
const NAME_TABLE: [string, string[], string][] = [
  ["site", [], "site"],
  ["site", ["site"], "site (1)"],
  ["  Site  ", ["site"], "Site (1)"],
  ["SITE", ["site", "site (1)"], "SITE (2)"],
  ["site", ["site", "site (2)"], "site (1)"],
  ["site (1)", ["site", "site (1)"], "site (2)"],
  ["e2e-calc", ["E2E-CALC "], "e2e-calc (1)"],
  ["Inbox", ["Inbox"], "Inbox (1)"],
  ["new", ["old"], "new"],
  ["straße", ["STRASSE"], "straße (1)"],
];

const project = (over: Partial<Project>): Project => ({
  id: "p1", name: "calc", root: "/home/o/calc", created: "", profile: "auto",
  routing: { chains: {}, models: {}, no_new_work: null }, discord_channel_id: null,
  extra_dirs: [], always_ask: [], inbox: false, ...over,
});

const thread = (over: Partial<Thread>): Thread => ({
  id: "t1", project_id: "p1", role: "chat", provider: "fast", provider_session_id: null,
  task_id: null, title: "", created: "", updated: "2026-10-01", turns: 0, cost_usd: 0, tokens: 0,
  model: null, effort: null, ...over,
});

const task = (over: Partial<Task>): Task => ({
  id: "k1", project_id: "p1", brief: "b", state: "done", created: "", updated: "",
  spec: { goal: "", deliverable: "", acceptance: [], constraints: [], questions: [] },
  plan: [], status: { phase: "done", step: 0, steps: 0, started: null, elapsed_s: 0, cost_usd: 0,
                      tokens: 0, last_tool: null, last_file: null, open_question: null, routing: [] },
  report: null, thread_ids: [], worktree: null, branch: null, profile: null, ceilings: {}, ...over,
} as Task);

const impact = (over: Partial<ProjectImpact> = {}): ProjectImpact => ({
  project_id: "p2", name: "calc", root: "/home/o/calc", inbox: false, archived: null, token: "t",
  blockers: [], chat_threads: { count: 1, archived: 0 },
  tasks: { total: 2, finished: 2, active: [] }, task_threads: 4, running_turns: [],
  schedules: [{ id: "s1", brief: "n", describe: "every day", enabled: true }],
  worktrees: [{ task_id: "k1", path: "/home/o/calc/.jarvis/worktrees/k1", branch: "jarvis/k1", exists: true }],
  on_root: { threads: 3, tasks: [] }, discord_channel_id: null, ...over,
});

function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    getItem: (k) => (m.has(k) ? m.get(k)! : null),
    setItem: (k, v) => void m.set(k, v),
    removeItem: (k) => void m.delete(k),
    clear: () => m.clear(),
    key: () => null,
    get length() { return m.size; },
  };
}

describe("names are numbered, never refused", () => {
  it.each(NAME_TABLE)("%s among %j saves as %s", (wanted, taken, expected) => {
    expect(uniqueName(wanted, taken)).toBe(expected);
  });

  it("compares ignoring case and surrounding spaces", () => {
    expect(nameKey("  Calc ")).toBe(nameKey("calc"));
  });

  it("folds case the way Python's casefold does, not just toLowerCase", () => {
    expect(nameKey("Straße")).toBe("strasse");
    expect(nameKey("ﬁle")).toBe("file");
    expect(nameKey("ΟΔΟΣ")).toBe(nameKey("οδος"));
    expect(nameKey("οδος")).toBe("οδοσ");
  });

  it("counts archived projects' names, as the backend does", () => {
    const projects = [project({ id: "p1", name: "calc" })];
    expect(projectNamesTaken(projects, null, ["Vault"])).toEqual(["calc", "Vault", "Inbox"]);
    expect(uniqueName("vault", projectNamesTaken(projects, "p1", ["Vault"]))).toBe("vault (1)");
  });

  it("reserves the Inbox's name and leaves the project itself out", () => {
    const projects = [project({ id: "p1", name: "calc" }), project({ id: "p2", name: "site" })];
    expect(projectNamesTaken(projects, "p1")).toEqual(["site", "Inbox"]);
    expect(uniqueName("calc", projectNamesTaken(projects, "p1"))).toBe("calc");
    expect(uniqueName("inbox", projectNamesTaken(projects))).toBe("inbox (1)");
  });

  it("thread titles are unique within their own project only", () => {
    const threads = [thread({ id: "t1", title: "plan" }), thread({ id: "t2", project_id: "p2", title: "notes" }),
                     thread({ id: "t3", title: "" })];
    expect(threadTitlesTaken(threads, "p1")).toEqual(["plan"]);
    expect(threadTitlesTaken(threads, "p1", "t1")).toEqual([]);
    expect(uniqueName("notes", threadTitlesTaken(threads, "p1"))).toBe("notes");
  });
});

describe("the edit body sends only what changed", () => {
  const p = project({ extra_dirs: ["/home/o/x"], always_ask: ["vercel --prod"] });

  it("nothing changed: nothing sent", () => {
    expect(projectEditBody(p, formFrom(p))).toEqual({});
  });

  it("each field on its own", () => {
    expect(projectEditBody(p, { ...formFrom(p), name: " calc v2 " })).toEqual({ name: "calc v2" });
    expect(projectEditBody(p, { ...formFrom(p), root: "/home/o/new" })).toEqual({ root: "/home/o/new" });
    expect(projectEditBody(p, { ...formFrom(p), profile: "ask" })).toEqual({ profile: "ask" });
    expect(projectEditBody(p, { ...formFrom(p), extra_dirs: [] })).toEqual({ extra_dirs: [] });
    expect(projectEditBody(p, { ...formFrom(p), always_ask: ["vercel --prod", " gh release* ", ""] }))
      .toEqual({ always_ask: ["vercel --prod", "gh release*"] });
  });

  it("blank lines in the always-ask box are not a change", () => {
    expect(projectEditBody(p, { ...formFrom(p), always_ask: ["vercel --prod", "", "  "] })).toEqual({});
  });

  it("never sends the Inbox's name or root", () => {
    const inbox = project({ inbox: true, name: "Inbox", root: "/home/o" });
    expect(projectEditBody(inbox, { ...formFrom(inbox), name: "Mine", root: "/tmp", profile: "ask" }))
      .toEqual({ profile: "ask" });
  });

  it("a root change with only not-yet-started tasks lists none on the old folder", () => {
    // The backend leaves an intake task with no pinned root and no worktree
    // out of on_root.tasks: it starts under the new root.
    const lines = editEffects(p, { root: "/home/o/new" }, impact({ on_root: { threads: 0, tasks: [] }, worktrees: [] }));
    expect(lines).toEqual(["0 threads keep working in /home/o/calc; new threads and tasks use /home/o/new."]);
  });

  it("says what a root change leaves on the old folder", () => {
    const lines = editEffects(p, { root: "/home/o/new" },
      impact({ on_root: { threads: 3, tasks: [{ id: "k9", brief: "b", state: "blocked" }] } }));
    expect(lines[0]).toBe("3 threads keep working in /home/o/calc; new threads and tasks use /home/o/new.");
    expect(lines[1]).toMatch(/^1 unfinished task finish and commit in \/home\/o\/calc/);
    expect(lines[2]).toMatch(/Worktrees stay/);
    expect(editEffects(p, { profile: "ask" }, null)[0]).toMatch(/from now on/);
    expect(editEffects(p, {}, null)).toEqual([]);
  });
});

describe("confirmations read back from /impact", () => {
  it("archive: kept, paused, left on disk, folder untouched", () => {
    const lines = archiveSummary(impact());
    expect(lines[0]).toBe("1 chat thread and 2 tasks are kept, hidden, with everything in them.");
    expect(lines).toContain("4 task threads go with their tasks.");
    expect(lines).toContain("1 schedule is paused until you restore it.");
    expect(lines.join(" ")).toContain("/home/o/calc/.jarvis/worktrees/k1 (jarvis/k1)");
    expect(lines[lines.length - 1]).toMatch(/^Nothing inside \/home\/o\/calc is touched/);
    expect(lines.join(" ")).not.toContain("Discord");
  });

  it("archive: says where a linked project's Discord channel goes (B1)", () => {
    const lines = archiveSummary(impact({ discord_channel_id: "830000000000000001" }));
    expect(lines).toContain(
      "Its Discord channel moves to the Jarvis Archive category, kept as it is; restoring moves it back.");
    expect(lines[lines.length - 1]).toMatch(/^Nothing inside/);
  });

  it("delete: to the trash, worktrees stay, singular and plural", () => {
    const lines = deleteSummary(impact({ chat_threads: { count: 2, archived: 0 }, task_threads: 1 }), 30);
    expect(lines[0]).toBe("Jarvis's records go to the trash: 2 chat threads, 2 tasks, 1 task thread, 1 schedule.");
    expect(lines[1]).toMatch(/^Left on disk, with their branches: \/home\/o\/calc\/\.jarvis\/worktrees\/k1/);
    expect(lines).toContain("The trash keeps them for 30 days.");
    expect(deleteSummary(impact({ worktrees: [] }), null)[1]).toBe("No worktree of it is on disk any more.");
    expect(deleteSummary(impact({ discord_channel_id: "830000000000000001" }), null))
      .toContain("Its Discord channel is kept, with a note; it is never deleted.");
  });
});

describe("after a project goes away", () => {
  const projects = [project({ id: "inbox", name: "Inbox", inbox: true }), project({ id: "p1" }),
                    project({ id: "p2", name: "site" })];
  const threads = [thread({ id: "t1", project_id: "p1", updated: "2026-10-05" }),
                   thread({ id: "t2", project_id: "p2", updated: "2026-10-04" })];
  const tasks = [task({ id: "k1", project_id: "p1" }), task({ id: "k2", project_id: "p2" })];
  const base = { projects, threads, tasks, threadId: null, compose: null, taskId: null };

  it("drops its rows", () => {
    const after = afterProjectGone(base, "p1", null);
    expect(after.projects.map((p) => p.id)).toEqual(["inbox", "p2"]);
    expect(after.threads.map((t) => t.id)).toEqual(["t2"]);
    expect(after.tasks.map((t) => t.id)).toEqual(["k2"]);
    expect(after.displaced).toBe(false);
  });

  it("re-aims a compose row that was aimed at it", () => {
    const after = afterProjectGone({ ...base, compose: { projectId: "p1" } }, "p1", "p1");
    expect(after.compose).toEqual({ projectId: "p2" });       // the most recent other chat
    expect(after.displaced).toBe(true);
  });

  it("leaves a compose row elsewhere alone", () => {
    const compose = { projectId: "p2" };
    expect(afterProjectGone({ ...base, compose }, "p1", null).compose).toBe(compose);
  });

  it("an open thread inside it closes into a new thread elsewhere", () => {
    const after = afterProjectGone({ ...base, threadId: "t1" }, "p1", null);
    expect(after.threadId).toBeNull();
    expect(after.compose).toEqual({ projectId: "p2" });
    expect(after.displaced).toBe(true);
  });

  it("an open task inside it is let go", () => {
    const after = afterProjectGone({ ...base, threadId: "t2", taskId: "k1" }, "p1", null);
    expect(after.taskId).toBeNull();
    expect(after.threadId).toBe("t2");
  });

  it("forgets the remembered project only when it is the one", () => {
    const s = memoryStorage();
    s.setItem(LAST_PROJECT_KEY, "p1");
    forgetLastProject("p2", s);
    expect(s.getItem(LAST_PROJECT_KEY)).toBe("p1");
    forgetLastProject("p1", s);
    expect(s.getItem(LAST_PROJECT_KEY)).toBeNull();
  });
});

describe("after a thread goes away", () => {
  const threads = [thread({ id: "t1", project_id: "p1" }), thread({ id: "t2", project_id: "p1" })];
  const base = { projects: [project({})], threads, tasks: [], threadId: null, compose: null, taskId: null };

  it("the open one becomes a new thread in the same project", () => {
    const after = afterThreadGone({ ...base, threadId: "t1" }, "t1");
    expect(after.threads.map((t) => t.id)).toEqual(["t2"]);
    expect(after.threadId).toBeNull();
    expect(after.compose).toEqual({ projectId: "p1" });
    expect(after.displaced).toBe(true);
  });

  it("another one leaves the conversation alone", () => {
    const after = afterThreadGone({ ...base, threadId: "t2" }, "t1");
    expect(after.threadId).toBe("t2");
    expect(after.displaced).toBe(false);
  });
});

describe("the delete notice says where the records went", () => {
  it("the trash, the Recycle Bin, or staged for retry", () => {
    expect(deletedNotice("calc", { deleted: "p1", trash: { where: "linux" } }))
      .toBe("Deleted calc; Jarvis's records are in the trash.");
    expect(deletedNotice("calc", { deleted: "p1", trash: { where: "windows" } })).toMatch(/Recycle Bin/);
    const staged = deletedNotice("calc", {
      deleted: "p1", trash: { where: "staged", path: "/d/deleting/x", error: "TrashError: disk full" } });
    expect(staged).toMatch(/staged for retry in \/d\/deleting\/x/);
    expect(staged).toMatch(/disk full/);
    expect(staged).not.toMatch(/are in the trash/);
  });
});
