// `api.vexFresh(projectId)` is the Download chain's first question (PR #292,
// 2026-10-21): `GET /projects/{id}/vex/fresh` → `{fresh, reason}`. Only an
// exact `fresh: true` skips the clean and build; anything else, or a refusal,
// means "clean and build, as before".
//
// Mutations caught: a truthy parse (`!!r`, `!!r.fresh`), the wrong route or verb.

import { api } from "./api";

describe("api.vexFresh", () => {
  let calls: { url: string; init: RequestInit }[];

  const answering = (status: number, body: unknown) => {
    calls = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    });
  };

  afterEach(() => vi.unstubAllGlobals());

  it("GETs the project's fresh route once, and true is true", async () => {
    answering(200, { fresh: true, reason: null });
    expect(await api.vexFresh("p3")).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/projects/p3/vex/fresh");
    expect(calls[0].init.method ?? "GET").toBe("GET");
  });

  it("only an exact true counts", async () => {
    for (const body of [{ fresh: false, reason: "x" }, { fresh: "yes" }, { fresh: 1 }, {}, { reason: null }]) {
      answering(200, body);
      expect(await api.vexFresh("p3")).toBe(false);
    }
  });

  it("a refusal rejects, and the chain then cleans and builds", async () => {
    answering(404, { error: "Project p3 not found" });
    await expect(api.vexFresh("p3")).rejects.toBeTruthy();
  });
});
