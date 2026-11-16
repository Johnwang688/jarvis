import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import { decodeDiff, decodeProjects, decodeReview, decodeTask, decodeThreads, readVerdict } from "./reads";
import type { Got } from "./net";
import { K1, K2, mkRow, mkTask, T1, T2 } from "./threads.fixtures";

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() { this.readyState = 2; }
}
const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach(app => app.stop()); vi.useRealTimers(); });
const settle = async () => { for (let i = 0; i < 160; i++) await Promise.resolve(); };
const ok = (body: unknown): Got => ({ ok: true, body });
const failure: Got = { ok: false, why: "error", status: 503, body: { error: "read unavailable" } };
const line = (seq: number) => ({ seq, role: "Claude", text: `answer ${seq}` });
const log = (seq: number) => ok({ rows: [line(seq)], next: seq + 1 });
const review = { v: 1, overnight: [], before: [] };
const thread = { id: T1, project_id: T2, title: "Read chat" };
const detail = { card: { tool: "Bash", command: "echo test" }, can: { allow: true, always: true }, secure: { ok: true } };
const diff = { files: [{ path: "test.txt", patch: "retained diff" }] };

async function harness() {
  vi.useFakeTimers();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const replies: Record<string, Got | Promise<Got>> = {
    "/m/api/me": ok({ secure: { ok: true } }),
    "/m/api/inbox": ok({ rows: [] }),
    "/m/api/working": ok({ entries: [] }),
    "/m/api/tasks": ok({ tasks: [] }),
    "/m/api/review": ok(review),
    "/m/api/projects": ok({ projects: [{ id: T2, name: "Read project" }] }),
    "/m/api/threads": ok({ threads: [thread] }),
    [`/m/api/threads/${T1}/log`]: log(1),
    [`/m/api/threads/${T2}/log`]: log(2),
    [`/m/api/tasks/${K1}`]: ok({ task: mkTask(K1, "running", { brief: "Read task" }) }),
    [`/m/api/tasks/${K1}/diff`]: ok(diff),
    "/m/api/inbox/appr%3Atest/detail": ok(detail),
  };
  const fetchJson = vi.fn(async (path: string): Promise<Got> => replies[path] ?? replies[path.split("?")[0]] ?? ok({}));
  const app = new PhoneApp({ root, fetchJson, onSignedOut: vi.fn(), signOut: async () => {}, openStream: () => new Stream() });
  apps.push(app);
  app.start(); await settle();
  const warning = (resource: string) => root.querySelector<HTMLElement>(`.m-resource-warning[data-resource="${resource}"]`);
  return { app, replies, root, warning };
}

function deferred() {
  let resolve!: (got: Got) => void;
  const promise = new Promise<Got>(r => { resolve = r; });
  return { promise, resolve };
}

describe("phone read follow-up decoders", () => {
  it.each(["since", "live"])("only absent or array %s counts as an optional Review list", key => {
    expect(readVerdict(decodeReview(review)!.lists)).toBe("valid");
    expect(readVerdict(decodeReview({ ...review, [key]: [] })!.lists)).toBe("valid");
    for (const wrong of [{}, null, undefined, false, 1, "bad"]) {
      expect(decodeReview({ ...review, [key]: wrong })).toBeNull();
    }
  });

  it("a task detail must parse and belong to the requested task", () => {
    for (const body of [{}, null, { task: null }, { task: mkTask(K2, "running") }]) {
      expect(decodeTask(body, K1)).toBeNull();
    }
    expect(readVerdict(decodeTask({ task: mkTask(K1, "running") }, K1)!.lists)).toBe("valid");
  });

  it.each([
    [decodeProjects, "projects", { id: T2 }],
    [decodeThreads, "threads", thread],
    [decodeDiff, "files", diff.files[0]],
  ] as const)("judges all remaining data lists with the same rule (%s, %s)", (decode, key, record) => {
    for (const body of [{}, { [key]: {} }, { [key]: null }]) expect(decode(body)).toBeNull();
    for (const [records, verdict] of [[[], "valid"], [[null], "malformed"], [[record, null], "partial"], [[record], "valid"]] as const) {
      expect(readVerdict(decode({ [key]: records })!.lists)).toBe(verdict);
    }
  });
});

describe("every chat log read shares a generation", () => {
  it("an incremental read supersedes the opening read, including its failure warning", async () => {
    const h = await harness();
    const old = deferred();
    const path = `/m/api/threads/${T1}/log`;
    h.replies[path] = old.promise;
    const opening = h.app.openThread(T1); await settle();
    h.replies[path] = log(9);
    await h.app.moreLog();
    old.resolve(failure); await opening;
    expect(h.app.thread?.rows.map(row => row.seq)).toEqual([9]);
    expect(h.app.thread?.next).toBe(10);
    expect(h.warning(`threads/${T1}/log`)).toBeNull();
  });

  it("a new opening supersedes an incremental response even on the same open object", async () => {
    const h = await harness();
    await h.app.openThread(T1);
    const old = deferred();
    const path = `/m/api/threads/${T1}/log`;
    h.replies[path] = old.promise;
    const reading = h.app.moreLog(); await settle();
    h.replies[path] = log(9);
    await h.app.openThread(T1);
    old.resolve(log(20)); await reading;
    expect(h.app.thread?.rows.map(row => row.seq)).toEqual([1, 9]);
    expect(h.app.thread?.next).toBe(10);
    expect(h.root.textContent).not.toContain("answer 20");
  });

  it("switching A to B to A rejects A's earlier incremental read", async () => {
    const h = await harness();
    const path = `/m/api/threads/${T1}/log`;
    await h.app.openThread(T1);
    const old = deferred(); h.replies[path] = old.promise;
    const reading = h.app.moreLog(); await settle();
    await h.app.openThread(T2);
    h.replies[path] = log(9); await h.app.openThread(T1);
    old.resolve(log(20)); await reading;
    expect(h.app.thread?.rows.map(row => row.seq)).toEqual([9]);
    expect(h.app.thread?.next).toBe(10);
  });

  it("a closed chat ignores its outstanding read", async () => {
    const h = await harness();
    const old = deferred(); h.replies[`/m/api/threads/${T1}/log`] = old.promise;
    const opening = h.app.openThread(T1); await settle();
    h.app.show("inbox"); await settle();
    old.resolve(log(20)); await opening;
    expect(h.app.thread?.hasRead).toBe(false);
    expect(h.root.textContent).not.toContain("answer 20");
  });

  it("a latest but older snapshot cannot rewind the cursor or erase drawn rows", async () => {
    const h = await harness();
    h.replies[`/m/api/threads/${T1}/log`] = log(9);
    await h.app.openThread(T1);
    h.replies[`/m/api/threads/${T1}/log`] = log(1);
    await h.app.openThread(T1);
    await h.app.moreLog();
    expect(h.app.thread?.next).toBe(10);
    expect(h.app.thread?.rows.map(row => row.seq)).toEqual([9]);
  });
});

describe("task and other successful read handlers", () => {
  it("retains a task through failures and malformed data; only its own valid read clears the warning", async () => {
    const h = await harness();
    const path = `/m/api/tasks/${K1}`;
    await h.app.openTask(K1);
    const held = h.app.task?.view;
    for (const reply of [failure, ok({}), ok({ task: null }), ok({ task: mkTask(K2, "running") })]) {
      h.replies[path] = reply; await h.app.openTask(K1);
      expect(h.app.task?.view).toBe(held);
      expect(h.warning(`tasks/${K1}`)?.dataset.read).toBe("failed");
    }
    await h.app.loadMe(); await h.app.loadTasks();
    expect(h.warning(`tasks/${K1}`)).not.toBeNull();
    h.replies[path] = ok({ task: mkTask(K1, "done", { brief: "Fresh task" }) });
    await h.app.openTask(K1);
    expect(h.root.textContent).toContain("Fresh task");
    expect(h.warning(`tasks/${K1}`)).toBeNull();
  });

  it("retains a diff and warns on malformed, all-invalid and failed refreshes; partial and empty reads apply", async () => {
    const h = await harness();
    await h.app.openTask(K1); await h.app.loadDiff();
    const held = h.app.task?.diff;
    const path = `/m/api/tasks/${K1}/diff`;
    for (const reply of [ok({}), ok({ files: [null] }), failure]) {
      h.replies[path] = reply; await h.app.loadDiff();
      expect(h.app.task?.diff).toBe(held);
      expect(h.warning(`tasks/${K1}/diff`)).not.toBeNull();
    }
    h.replies[path] = ok({ files: [...diff.files, null] }); await h.app.loadDiff();
    expect(h.warning(`tasks/${K1}/diff`)?.dataset.read).toBe("partial");
    h.replies[path] = ok({ files: [] }); await h.app.loadDiff();
    expect(h.app.task?.diff).toMatchObject({ files: [] });
    expect(h.warning(`tasks/${K1}/diff`)).toBeNull();
  });

  it("keeps projects and chats on malformed refreshes, and applies partial and empty lists", async () => {
    const h = await harness(); h.app.show("chats"); await settle();
    const heldProjects = h.app.projects, heldThreads = h.app.threads;
    h.replies["/m/api/projects"] = ok({}); h.replies["/m/api/threads"] = ok({});
    await h.app.loadChats();
    expect(h.app.projects).toBe(heldProjects); expect(h.app.threads).toBe(heldThreads);
    expect(h.warning("projects")).not.toBeNull(); expect(h.warning("threads")).not.toBeNull();
    h.replies["/m/api/projects"] = ok({ projects: [{ id: T2 }, null] });
    h.replies["/m/api/threads"] = ok({ threads: [thread, null] }); await h.app.loadChats();
    expect(h.warning("projects")?.dataset.read).toBe("partial"); expect(h.warning("threads")?.dataset.read).toBe("partial");
    h.replies["/m/api/projects"] = ok({ projects: [] }); h.replies["/m/api/threads"] = ok({ threads: [] });
    await h.app.loadChats();
    expect(h.app.projects).toEqual([]); expect(h.app.threads).toEqual([]);
    expect(h.warning("projects")).toBeNull(); expect(h.warning("threads")).toBeNull();
  });

  it("a malformed project-scoped chat refresh keeps that project's chats", async () => {
    const h = await harness(); h.app.show("chats"); await settle();
    const held = h.app.threads;
    h.replies[`/m/api/threads?project=${T2}`] = ok({});
    await h.app.loadProjectThreads(T2);
    expect(h.app.threads).toBe(held);
    expect(h.warning(`threads?project=${T2}`)).not.toBeNull();
    h.replies["/m/api/threads"] = ok({}); await h.app.loadChats();
    h.replies[`/m/api/threads?project=${T2}`] = ok({ threads: [thread] });
    await h.app.loadProjectThreads(T2);
    expect(h.warning(`threads?project=${T2}`)).toBeNull();
    expect(h.warning("threads")).not.toBeNull();
  });

  it("a healthy project read applies even if the thread-list read fails", async () => {
    const h = await harness();
    h.replies["/m/api/threads"] = failure; await h.app.loadChats();
    expect(h.app.projects?.map(project => project.id)).toEqual([T2]);
  });

  it("a successful project list does not imply that the chats were ever read", async () => {
    const h = await harness();
    h.replies["/m/api/threads"] = failure;
    h.app.show("chats"); await settle();
    expect(h.warning("threads")?.textContent).toContain("not read yet");
    h.replies["/m/api/threads"] = ok({ threads: [] }); await h.app.loadChats();
    h.replies["/m/api/threads"] = failure; await h.app.loadChats();
    expect(h.warning("threads")?.textContent).toContain("showing the last read");
  });

  it("malformed card details still remove old authorization controls", async () => {
    const h = await harness();
    h.app.openCard(mkRow("appr:test", 1, { kind: "approval" })); await settle();
    expect(h.app.card?.detail?.can.allow).toBe(true);
    h.replies["/m/api/inbox/appr%3Atest/detail"] = ok({}); await h.app.loadCard();
    expect(h.app.card?.detail).toBeNull();
    expect(h.app.card?.refused).toContain("could not be read");
    expect(h.root.querySelector('[data-testid="m-card-allow"]')).toBeNull();
  });

  it("a malformed security read replaces an earlier healthy display with a limited one", async () => {
    const h = await harness();
    expect(h.app.secure?.ok).toBe(true);
    h.replies["/m/api/me"] = ok({}); await h.app.loadMe();
    expect(h.app.secure?.ok).toBe(false);
    h.replies["/m/api/me"] = ok({ secure: { ok: true } }); await h.app.probe();
    expect(h.app.secure?.ok).toBe(true);
    h.replies["/m/api/me"] = ok({}); await h.app.probe();
    expect(h.app.secure?.ok).toBe(false);
  });

  it("a malformed rule preview removes the previously read confirmation", async () => {
    const h = await harness();
    h.app.openCard(mkRow("appr:test", 1, { kind: "approval" })); await settle();
    const path = "/m/api/inbox/appr%3Atest/answer/rule";
    h.replies[path] = ok({ preview: "a".repeat(64), rule: {}, rule_words: "Read rule" });
    await h.app.alwaysCard();
    expect(h.app.card?.rule?.words).toBe("Read rule");
    h.replies[path] = ok({}); await h.app.alwaysCard();
    expect(h.app.card?.rule).toBeNull();
    expect(h.app.card?.notice).toContain("could not be read");
  });
});
