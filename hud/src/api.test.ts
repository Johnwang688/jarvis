// The wire, where a test can see it. `POST /tasks/{id}/answer` takes exactly
// `{index, text}` (daemon `task_verb_body`); the HUD sent `{question, answer}`
// for weeks and swallowed the 400 (2026-10-11).

import { api, ApiError } from "./api";

describe("api.answerTask", () => {
  let calls: { url: string; init: RequestInit }[];

  const answering = (status: number, body: unknown) => {
    calls = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    });
  };

  afterEach(() => vi.unstubAllGlobals());

  it("posts exactly {index, text} to the task's answer route", async () => {
    answering(200, { id: "k1", state: "clarifying" });
    const task = await api.answerTask("k1", 2, "a branch");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/tasks/k1/answer");
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ index: 2, text: "a branch" });
    expect(task.id).toBe("k1");
  });

  it("a refusal rejects with the daemon's sentence, for the view to show", async () => {
    answering(409, { error: "Task k1 is cancelled; it is not waiting on a question." });
    const err = await api.answerTask("k1", 0, "x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.message).toBe("Task k1 is cancelled; it is not waiting on a question.");
  });
});
