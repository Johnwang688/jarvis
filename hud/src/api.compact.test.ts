// WP-C: `api.compact(id)` is the Compact chip's click, beside `interrupt`. The
// daemon route is `POST /threads/{id}/compact` (owner-only, 202
// {"status":"started","turn_id"}, 409 {"error": "<sentence>"}); the body is
// empty JSON now so a later `{focus}` (CB-4) can be added without breaking it.
// On today's code `api.compact` does not exist, so every case fails with
// "api.compact is not a function".
//
// Mutations caught: a double post (the call count), the wrong route or verb,
// a non-empty body, a swallowed 409 sentence.

import { api, ApiError } from "./api";

describe("api.compact", () => {
  let calls: { url: string; init: RequestInit }[];

  const answering = (status: number, body: unknown) => {
    calls = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    });
  };

  afterEach(() => vi.unstubAllGlobals());

  it("POSTs the thread's compact route once with an empty JSON body", async () => {
    answering(202, { status: "started", turn_id: "turn-9" });
    const result = await api.compact("t1");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/threads/t1/compact");
    expect(calls[0].init.method).toBe("POST");
    expect(new Headers(calls[0].init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({});
    expect(result).toEqual({ status: "started", turn_id: "turn-9" });
  });

  it("a refusal rejects with the daemon's sentence, for pane-status", async () => {
    answering(409, { error: "A turn is running; compact when it ends." });
    const err = await api.compact("t1").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.message).toBe("A turn is running; compact when it ends.");
    expect(calls).toHaveLength(1);
  });
});
