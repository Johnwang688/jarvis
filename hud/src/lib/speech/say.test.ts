import { api, ApiError } from "../../api";
import { SAY_LOCAL_ONLY } from "./policy";

describe("api.say", () => {
  let calls: { url: string; init: RequestInit }[];

  beforeEach(() => {
    calls = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(new Uint8Array([82, 73, 70, 70]), { status: 200, headers: { "Content-Type": "audio/wav" } });
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("read-aloud asks the daemon for a local voice, and never sends a voice it was not given", async () => {
    expect(SAY_LOCAL_ONLY).toBe(true);
    const bytes = await api.say("Hello there.", { localOnly: SAY_LOCAL_ONLY });
    expect(bytes.byteLength).toBe(4);
    expect(calls[0].url).toBe("/say");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ text: "Hello there.", local_only: true });
    await api.say("Hello.", { voice: "bm_george", localOnly: true });
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ text: "Hello.", voice: "bm_george", local_only: true });
    await api.say("Plain.");
    expect(JSON.parse(String(calls[2].init.body))).toEqual({ text: "Plain." });
  });

  it("a refusal rejects, for the queue to skip the chunk", async () => {
    vi.stubGlobal("fetch", async () => new Response("{}", { status: 503 }));
    const err = await api.say("x", { localOnly: true }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(503);
  });
});
