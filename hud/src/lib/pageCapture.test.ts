// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { CAPTURE_BYTES, CAPTURE_MIME, captureAttachments, captureReply, loadCaptureImage, type CaptureStill } from "./pageCapture";

const still = (): CaptureStill => ({
  capture_id: "a".repeat(32), image: { mime: "image/png", data_b64: "aGk=", width: 800, height: 600 },
  url: "https://example.test/", title: "Page", text_chars: 40, whole: false, notes: [],
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("immutable capture receipts", () => {
  it("refuses malformed references, image formats, sizes, base64 and text scope", () => {
    for (const change of [
      { capture_id: "../file" }, { image: { ...still().image, mime: "image/svg+xml" } },
      { image: { ...still().image, width: 1921 } }, { image: { ...still().image, height: 0 } },
      { image: { ...still().image, data_b64: "a===" } }, { image: { ...still().image, data_b64: "aGk=!" } },
      { title: "x".repeat(121) }, { text_chars: 100001 }, { whole: 1 }, { notes: ["x".repeat(301)] },
    ]) expect(() => captureReply({ ...still(), ...change })).toThrow();
  });
  it("handles a maximum-sized real base64 string without recursive regex overflow", () => {
    const raw = still(); raw.image.data_b64 = btoa("x".repeat(CAPTURE_BYTES));
    expect(captureReply(raw).image.data_b64.length).toBe(raw.image.data_b64.length);
    raw.image.data_b64 = btoa("x".repeat(CAPTURE_BYTES + 1));
    expect(() => captureReply(raw)).toThrow();
  });
  it("copies mutable metadata and notes", () => {
    const raw = still(), copy = captureReply(raw);
    raw.image.width = 1; raw.notes.push("late");
    expect(copy.image.width).toBe(800); expect(copy.notes).toEqual([]);
  });
});

function fakeImage(width = 800) {
  const removed = vi.fn();
  class ImageStub {
    naturalWidth = width; naturalHeight = 600;
    onload: null | (() => void) = null; onerror = null;
    set src(_: string) { queueMicrotask(() => this.onload?.()); }
    removeAttribute = removed;
  }
  vi.stubGlobal("Image", ImageStub);
  return removed;
}
function fakeCanvas(blobs: { size: number; data: string }[]) {
  const calls: string[] = [];
  const context = Object.fromEntries(["save", "restore", "scale", "drawImage", "beginPath", "arc", "fill"].map(k => [k, vi.fn()]));
  const canvas = { width: 0, height: 0, getContext: () => context,
    toBlob: (cb: (b: unknown) => void, mime: string, quality?: number) => {
      calls.push(mime + ":" + quality);
      const b = blobs.shift()!;
      cb({ size: b.size, arrayBuffer: async () => new TextEncoder().encode(b.data).buffer });
    } };
  vi.spyOn(document, "createElement").mockReturnValue(canvas as unknown as HTMLCanvasElement);
  return { canvas, context, calls };
}
describe("capture image export", () => {
  it("refuses metadata/pixel mismatch and releases the temporary image", async () => {
    const removed = fakeImage(801);
    await expect(loadCaptureImage(still())).rejects.toThrow("changed size");
    expect(removed).toHaveBeenCalledWith("src");
  });
  it("flattens ink and emits exactly one canonical image/reference pair", async () => {
    const removed = fakeImage();
    const { canvas, context, calls } = fakeCanvas([{ size: 2, data: "hi" }]);
    const pair = await captureAttachments(still(), [{ t: "s", c: "#ff3b30", w: 4, pts: [[20, 30]] }]);
    expect(pair).toEqual([
      { name: "page.png", mime: "image/png", data_b64: "aGk=" },
      { name: "page text", mime: CAPTURE_MIME, data_b64: "a".repeat(32) },
    ]);
    expect(context.drawImage).toHaveBeenCalled(); expect(context.arc).toHaveBeenCalledWith(20, 30, 2, 0, Math.PI * 2);
    expect(calls).toEqual(["image/png:undefined"]);
    expect(canvas.width).toBe(0); expect(canvas.height).toBe(0); expect(removed).toHaveBeenCalled();
  });
  it("switches a large PNG to JPEG0.9 and refuses a still-oversized result without a half pair", async () => {
    fakeImage();
    const first = fakeCanvas([{ size: 3.6 * 1024 * 1024, data: "" }, { size: 2, data: "hi" }]);
    expect((await captureAttachments(still(), []))[0].name).toBe("page.jpg");
    expect(first.calls).toEqual(["image/png:undefined", "image/jpeg:0.9"]);
    vi.restoreAllMocks();
    const second = fakeCanvas([{ size: CAPTURE_BYTES + 1, data: "" }, { size: CAPTURE_BYTES + 1, data: "" }]);
    await expect(captureAttachments(still(), [])).rejects.toThrow("over 4 MB");
    expect(second.canvas.width).toBe(0);
  });
});
