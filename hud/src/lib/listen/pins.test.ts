// @vitest-environment node
// The model the worker will fetch is the file V0 pinned: the vendored bytes,
// the manifest, pins.ts and tests/voice_v0/models.json all name one digest.
import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { SILERO_BYTES, SILERO_CONTEXT, SILERO_FRAME, SILERO_SHA256, SILERO_STATE } from "./pins";

const HUD = resolve(__dirname, "../../..");
const model = resolve(HUD, "vendor/silero-vad/silero_vad_v5.1.2.onnx");

describe("the pinned Silero model", () => {
  it("the vendored file is the pinned bytes", () => {
    const bytes = readFileSync(model);
    expect(bytes.length).toBe(SILERO_BYTES);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(SILERO_SHA256);
    expect(statSync(model).size).toBe(SILERO_BYTES);
  });

  it("the manifest and V0's model list agree", () => {
    const manifest = JSON.parse(readFileSync(resolve(HUD, "vendor/silero-vad/manifest.json"), "utf8")).model;
    expect(manifest.sha256).toBe(SILERO_SHA256);
    expect(manifest.bytes).toBe(SILERO_BYTES);
    const v0 = JSON.parse(readFileSync(resolve(HUD, "../tests/voice_v0/models.json"), "utf8")).models["silero-vad-v5.1.2"];
    expect(v0.sha256).toBe(SILERO_SHA256);
  });

  it("the licence travels with it", () => {
    expect(readFileSync(resolve(HUD, "vendor/silero-vad/LICENSE.txt"), "utf8")).toContain("MIT License");
    expect(readFileSync(resolve(HUD, "vendor/onnxruntime-web/LICENSE.txt"), "utf8")).toContain("Microsoft Corporation");
    expect(JSON.parse(readFileSync(resolve(HUD, "vendor/silero-vad/manifest.json"), "utf8")).model.pinned_by).toContain("lib/listen/pins.ts");
  });

  it("the frame arithmetic is Silero v5's at 16 kHz", () => {
    expect([SILERO_FRAME, SILERO_CONTEXT, SILERO_STATE]).toEqual([512, 64, 256]);
  });

  it("onnxruntime-web is pinned exactly (no range) and to the version V0 measured", () => {
    const pkg = JSON.parse(readFileSync(resolve(HUD, "package.json"), "utf8"));
    expect(pkg.dependencies["onnxruntime-web"]).toBe("1.30.0");
  });
});
