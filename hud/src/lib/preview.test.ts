import { describe, expect, it } from "vitest";
import {
  API_PORT, DEFAULT_PORTS, FACE_PORT, SANDBOX, WORKSHOP_PORT, daemonPortsFrom, judgeKeepOrigin, judgePreviewUrl,
  keptOrigin, originOf, sandboxFor, storableKeepOrigin, workshopPortFrom, workshopUrl, type DaemonPorts,
} from "./preview";

/** A daemon on ephemeral ports (a test's), served to a window on its HUD port, frame-hardened. */
const PORTS: DaemonPorts = { hud: 41001, api: 41002, workshop: 41003, self: 41001, frameHardened: true };

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

  it("refuses the daemon's API listener too (WP-E), default and reported", () => {
    for (const bad of ["http://127.0.0.1:8405/", "http://localhost:8405/status"]) {
      const v = judgePreviewUrl(bad);
      expect(v.ok).toBe(false);
      expect(v.reason).toMatch(/API/);
    }
    const v = judgePreviewUrl("http://127.0.0.1:41002/", PORTS);
    expect(v.ok).toBe(false);
    expect(v.reason).toMatch(/API/);
  });

  it("refuses the reported HUD port and this window's own port", () => {
    expect(judgePreviewUrl("http://localhost:41001/", PORTS).ok).toBe(false);
    expect(judgePreviewUrl("http://localhost:41009/", { ...PORTS, self: 41009 }).ok).toBe(false);
    // The defaults stay refused whatever a daemon reports: they are the owner's live daemon.
    expect(judgePreviewUrl("http://localhost:8402/", PORTS).ok).toBe(false);
    expect(judgePreviewUrl("http://localhost:8405/", PORTS).ok).toBe(false);
  });

  it("accepts the workshop origin and localhost dev servers", () => {
    for (const ok of [
      "http://127.0.0.1:8403/p/proj1/index.html",
      "http://localhost:5173/",
      "http://localhost:3000/some/path?q=1",
    ])
      expect(judgePreviewUrl(ok).ok).toBe(true);
    expect(judgePreviewUrl("http://127.0.0.1:41003/p/p1/index.html", PORTS).ok).toBe(true);
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

  it("reads the three ports and frame hardening from /status, defaults for what is missing", () => {
    expect(daemonPortsFrom({ hud_port: 41001, api_port: 41002, workshop_port: 41003, frame_hardened: true }, 41001))
      .toEqual(PORTS);
    expect(daemonPortsFrom({})).toEqual(DEFAULT_PORTS);
    expect(daemonPortsFrom(null, 8402)).toEqual({ ...DEFAULT_PORTS, self: 8402 });
    // Only a literal true: an older daemon says nothing, and nothing is not yes.
    for (const v of [undefined, "true", 1, {}, false]) {
      expect(daemonPortsFrom({ frame_hardened: v }).frameHardened).toBe(false);
    }
    expect(daemonPortsFrom({ api_port: "8405", hud_port: 0 }).api).toBe(API_PORT);
    expect(daemonPortsFrom({ api_port: "8405", hud_port: 0 }).hud).toBe(FACE_PORT);
  });
});

describe("keep its own origin (WP-E, decisions W-4)", () => {
  it("refuses all three daemon ports, reported and default, and this window's own", () => {
    for (const url of [
      "http://127.0.0.1:41001/", "http://127.0.0.1:41002/", "http://127.0.0.1:41003/p/p1/index.html",
      "http://localhost:8402/", "http://localhost:8403/p/p1/", "http://localhost:8405/",
    ]) {
      const v = judgeKeepOrigin(url, PORTS);
      expect(v.ok, url).toBe(false);
      expect(v.reason, url).toBeTruthy();
    }
    expect(judgeKeepOrigin("http://localhost:5173/", { ...PORTS, self: 5173 }).ok).toBe(false);
  });

  it("is allowed only for other loopback ports, and names the origin it covers", () => {
    for (const [url, origin] of [
      ["http://localhost:5173/", "http://localhost:5173"],
      ["http://127.0.0.1:3000/app?x=1", "http://127.0.0.1:3000"],
      ["https://localhost:8443/", "https://localhost:8443"],
      ["http://app.localhost:5173/", "http://app.localhost:5173"],
    ] as const) {
      const v = judgeKeepOrigin(url, PORTS);
      expect(v.ok, url).toBe(true);
      expect(v.origin).toBe(origin);
    }
    for (const url of ["https://example.com/", "http://192.168.1.10:5173/", "javascript:1", "data:text/html,x"]) {
      expect(judgeKeepOrigin(url, PORTS).ok, url).toBe(false);
    }
  });

  it("is never offered unless the daemon says it refuses to be framed", () => {
    const v = judgeKeepOrigin("http://localhost:5173/", { ...PORTS, frameHardened: false });
    expect(v.ok).toBe(false);
    expect(v.reason).toMatch(/framed/);
    expect(keptOrigin("http://localhost:5173", "http://localhost:5173/", DEFAULT_PORTS)).toBeNull();
  });

  it("a grant covers only its own origin: it drops on a port, host or scheme change", () => {
    const grant = "http://localhost:5173";
    expect(keptOrigin(grant, "http://localhost:5173/other/page", PORTS)).toBe(grant);
    expect(keptOrigin(grant, "http://localhost:5174/", PORTS)).toBeNull();        // port
    expect(keptOrigin(grant, "http://127.0.0.1:5173/", PORTS)).toBeNull();        // host
    expect(keptOrigin(grant, "https://localhost:5173/", PORTS)).toBeNull();       // scheme
    expect(keptOrigin(grant, "", PORTS)).toBeNull();
    expect(keptOrigin(null, "http://localhost:5173/", PORTS)).toBeNull();
  });

  it("a stored grant that is no longer allowed is dropped", () => {
    // Allowed when stored, then the daemon reports that port as its own.
    expect(keptOrigin("http://localhost:5173", "http://localhost:5173/", { ...PORTS, api: 5173 })).toBeNull();
    expect(keptOrigin("http://localhost:5173", "http://localhost:5173/", { ...PORTS, workshop: 5173 })).toBeNull();
    // Parse time: only exactly the stored URL's origin, never a daemon default.
    expect(storableKeepOrigin("http://localhost:5173", "http://localhost:5173/x")).toBe("http://localhost:5173");
    for (const [stored, url] of [
      ["http://localhost:5173/", "http://localhost:5173/"],      // not an origin
      ["http://localhost:5173", "http://localhost:5174/"],       // another port
      ["http://localhost:8402", "http://localhost:8402/"],       // the HUD
      ["http://127.0.0.1:8403", "http://127.0.0.1:8403/p/p1/"], // the workshop
      ["http://localhost:8405", "http://localhost:8405/"],       // the API
      ["http://example.com", "http://example.com/"],             // not local
      [true, "http://localhost:5173/"],
      ["http://localhost:5173", undefined],
      ["x".repeat(300), "http://localhost:5173/"],
    ] as const) {
      expect(storableKeepOrigin(stored, url as string | undefined), String(stored)).toBeNull();
    }
  });

  it("the sandbox gains allow-same-origin with a grant, and never anything else", () => {
    expect(SANDBOX).toBe("allow-scripts allow-forms");
    expect(sandboxFor(false)).toBe("allow-scripts allow-forms");
    expect(sandboxFor(true)).toBe("allow-scripts allow-forms allow-same-origin");
    for (const never of ["allow-top-navigation", "allow-popups", "allow-downloads", "allow-modals"]) {
      expect(sandboxFor(true)).not.toContain(never);
      expect(sandboxFor(false)).not.toContain(never);
    }
  });

  it("an origin is scheme, host and port", () => {
    expect(originOf("http://localhost:5173/a?b#c")).toBe("http://localhost:5173");
    expect(originOf("http://localhost/")).toBe("http://localhost");
    expect(originOf("javascript:alert(1)")).toBe("");
    expect(originOf("nope")).toBe("");
  });

  it("the defaults are the owner's live daemon", () => {
    expect([FACE_PORT, WORKSHOP_PORT, API_PORT]).toEqual([8402, 8403, 8405]);
  });
});
