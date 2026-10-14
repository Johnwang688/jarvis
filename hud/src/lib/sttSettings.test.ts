import { describe, expect, it } from "vitest";
import { parseSttSettings, sttSummary, STT_CHOICES } from "./sttSettings";

const daemon = (over: Record<string, unknown> = {}, local: Record<string, unknown> = {}) => ({
  backend: "cloud", cloud_fallback: true, error: "", backends: ["cloud", "local", "auto"],
  local: { installed: true, reason: "", loaded: false, threads: 2, idle_unload_min: 10, model: "m", fetch: "python -m jarvis.stt_local fetch", ...local },
  ...over,
});

describe("parseSttSettings", () => {
  it("reads the daemon's answer", () => {
    expect(parseSttSettings(daemon({ backend: "auto", cloud_fallback: false }))).toMatchObject({
      backend: "auto", cloudFallback: false, error: "",
      local: { installed: true, loaded: false, threads: 2, idleUnloadMin: 10 },
    });
    expect(STT_CHOICES).toEqual(["cloud", "local", "auto"]);
  });
  it("reads anything odd as local-only with no upload, never as upload", () => {
    for (const odd of [null, undefined, "x", 3, [], {}, { backend: "wifi", cloud_fallback: "yes" }, { cloud_fallback: 1 }]) {
      const s = parseSttSettings(odd);
      expect(s.backend === "local" || s.backend === "cloud").toBe(true);
      expect(s.cloudFallback).toBe(false);
      expect(s.local.installed).toBe(false);
    }
    expect(parseSttSettings({ backend: "wifi" }).backend).toBe("local");
  });
  it("bounds the daemon's text and numbers", () => {
    const s = parseSttSettings(daemon({ error: "x".repeat(5000) }, { reason: "y".repeat(5000), threads: -3, idle_unload_min: "ten" }));
    expect(s.error).toHaveLength(200);
    expect(s.local.reason).toHaveLength(200);
    expect(s.local.threads).toBe(0);
    expect(s.local.idleUnloadMin).toBe(0);
  });
});

describe("sttSummary says what happens to the next clip", () => {
  const s = (over = {}, local = {}) => parseSttSettings(daemon(over, local));
  it("cloud", () => {
    expect(sttSummary(s())).toMatch(/sent to the cloud/);
    expect(sttSummary(s({ cloud_fallback: false }))).toMatch(/will fail/);
  });
  it("local with and without the fallback", () => {
    expect(sttSummary(s({ backend: "local" }))).toMatch(/on this computer\. If that fails.*cloud/);
    const closed = sttSummary(s({ backend: "local", cloud_fallback: false }));
    expect(closed).toMatch(/Nothing is uploaded/);
    expect(closed).not.toMatch(/sent to the cloud/);
  });
  it("local that is not installed", () => {
    const missing = { installed: false, reason: "the speech model is not downloaded" };
    expect(sttSummary(s({ backend: "auto" }, missing))).toMatch(/goes to the cloud/);
    expect(sttSummary(s({ backend: "local" }, missing))).toMatch(/falls back to the cloud/);
    expect(sttSummary(s({ backend: "local", cloud_fallback: false }, missing))).toMatch(/will fail\. Nothing is uploaded/);
  });
});
