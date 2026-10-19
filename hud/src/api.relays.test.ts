// C5 (chat-to-chat plan §5.5, §7): the owner's Withdraw on a message a chat
// queued for another goes to C3's owner-only route
// `POST /threads/{sender}/relays/{message_id}/withdraw`, which answers
// 200 {ok, message_id, state: "withdrawn"}, 404 for an unknown message and 409
// once it is no longer queued. No HUD route ever sends a relay (P-3).

import { api, ApiError } from "./api";

let calls: { url: string; init?: RequestInit }[];
const answering = (status: number, body: unknown) => {
  calls = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  });
};
afterEach(() => vi.unstubAllGlobals());

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const withdraw = (...args: string[]) => (api as any).withdrawRelay(...args);

describe("api.withdrawRelay", () => {
  it("posts once to the sender's relay route, ids encoded", async () => {
    answering(200, { ok: true, message_id: "m0123456789ab", state: "withdrawn" });
    const out = await withdraw("aaaa1111", "m0123456789ab");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/threads/aaaa1111/relays/m0123456789ab/withdraw");
    expect(calls[0].init?.method).toBe("POST");
    expect(out).toEqual({ ok: true, message_id: "m0123456789ab", state: "withdrawn" });
    answering(200, { ok: true });
    await withdraw("a/b", "m?x");
    expect(calls[0].url).toBe("/threads/a%2Fb/relays/m%3Fx/withdraw");
  });

  it("a refusal rejects with the daemon's sentence", async () => {
    answering(409, { error: "message m0123456789ab is running, no longer queued" });
    const err = await withdraw("aaaa1111", "m0123456789ab").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).message).toBe("message m0123456789ab is running, no longer queued");
  });
});
