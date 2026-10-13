import { api, ApiError } from "./api";
afterEach(() => vi.unstubAllGlobals());
it("asks for tool history and retries only a 400 using the legacy route", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce({ ok: false, status: 400, text: async () => '{"error":"query"}' })
    .mockResolvedValueOnce({ ok: true, status: 200, text: async () => '{"messages":[]}' });
  vi.stubGlobal("fetch", fetcher);
  await expect(api.transcript("t")).resolves.toEqual({ messages: [] });
  expect(fetcher.mock.calls.map(c => c[0])).toEqual(["/threads/t/transcript?tools=1", "/threads/t/transcript"]);
});
it("does not hide an authentication or server failure with a second request", async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 503, text: async () => '{"error":"down"}' });
  vi.stubGlobal("fetch", fetcher);
  await expect(api.transcript("t")).rejects.toBeInstanceOf(ApiError);
  expect(fetcher).toHaveBeenCalledOnce();
});
