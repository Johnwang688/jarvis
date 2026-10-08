import { describe, expect, it } from "vitest";
import { judgePreviewUrl, workshopPortFrom, workshopUrl } from "./preview";

describe("preview URL policy", () => {
  it("refuses the HUD's own origin, in every spelling", () => {
    // The HUD is the approval surface: an iframe that could reach /approvals
    // would let an agent approve itself.
    for (const bad of [
      "http://127.0.0.1:8402/",
      "http://localhost:8402/index.html",
      "http://foo.localhost:8402/",
      "http://127.0.0.2:8402/x",
    ]) {
      const v = judgePreviewUrl(bad);
      expect(v.ok).toBe(false);
      expect(v.reason).toMatch(/HUD/);
    }
  });

  it("accepts the workshop origin and localhost dev servers", () => {
    for (const ok of [
      "http://127.0.0.1:8403/p/proj1/index.html",
      "http://localhost:5173/",
      "http://localhost:3000/some/path?q=1",
    ])
      expect(judgePreviewUrl(ok).ok).toBe(true);
  });

  it("refuses anything that is not local", () => {
    for (const bad of ["https://example.com/", "http://192.168.1.10:8080/"])
      expect(judgePreviewUrl(bad).ok).toBe(false);
  });

  it("refuses schemes that are not pages", () => {
    for (const bad of ["javascript:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd"])
      expect(judgePreviewUrl(bad).ok).toBe(false);
  });

  it("says what is wrong rather than failing silently", () => {
    expect(judgePreviewUrl("").reason).toBeTruthy();
    expect(judgePreviewUrl("not a url").reason).toBeTruthy();
  });

  it("builds a workshop path on the separate origin", () => {
    expect(workshopUrl("p1", "docs/index.html")).toBe("http://127.0.0.1:8403/p/p1/docs/index.html");
    expect(workshopUrl("p1", "/leading")).toBe("http://127.0.0.1:8403/p/p1/leading");
    expect(workshopUrl("ab12cd34", "index.html", 41234)).toBe(
      "http://127.0.0.1:41234/p/ab12cd34/index.html",
    );
  });

  it("takes the workshop port from the serving daemon's /status", () => {
    // A hard-coded 8403 sent the HUD suite's fixture project to the owner's
    // live daemon (`GET /p/p1/index.html` -> 400 in its log).
    expect(workshopPortFrom({ workshop_port: 41234 })).toBe(41234);
    // Only a daemon too old to say falls back to the default.
    expect(workshopPortFrom({})).toBe(8403);
    expect(workshopPortFrom(null)).toBe(8403);
    for (const bad of ["8403", 0, -1, 70000, 1.5, null]) {
      expect(workshopPortFrom({ workshop_port: bad })).toBe(8403);
    }
  });
});
