// Immutable owner captures. Only the daemon reference is resolved into page text.
import type { Attachment } from "../types";
import { exportSize, exportType, render, type Op } from "./annotate";

export const CAPTURE_MIME = "application/x-jarvis-page-capture";
export const CAPTURE_BYTES = 4 * 1024 * 1024;
export interface CaptureStill {
  capture_id: string;
  image: { mime: "image/png" | "image/jpeg"; data_b64: string; width: number; height: number };
  url: string; title: string; text_chars: number; whole: boolean; notes: string[];
}
export interface CaptureIdentity { window: string; pane_id: number; local_tab_id: string; view_id: string }
export const CAPTURE_NO_CHAT = "No chat is open to attach files to: open a chat first.";
export const CAPTURE_CHANGED = "This Browser view changed. Capture it again.";
export const DIRECT_CAPTURE = "Open this in Remote to annotate.";

export function captureReply(raw: unknown): CaptureStill {
  const r = raw as CaptureStill | null;
  const img = r?.image;
  if (!r || typeof r.capture_id !== "string" || !/^[0-9a-f]{32}$/.test(r.capture_id)
      || !img || !["image/png", "image/jpeg"].includes(img.mime)
      || typeof img.data_b64 !== "string" || !img.data_b64.length
      || img.data_b64.length > Math.ceil(CAPTURE_BYTES / 3) * 4
      || img.data_b64.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(img.data_b64)
      || img.data_b64.length * 3 / 4 - (img.data_b64.endsWith("==") ? 2 : img.data_b64.endsWith("=") ? 1 : 0) > CAPTURE_BYTES
      || !Number.isInteger(img.width) || !Number.isInteger(img.height)
      || img.width < 1 || img.height < 1 || Math.max(img.width, img.height) > 1920
      || typeof r.url !== "string" || r.url.length > 2048 || typeof r.title !== "string" || r.title.length > 120
      || !Number.isInteger(r.text_chars) || r.text_chars < 0 || r.text_chars > 100000
      || typeof r.whole !== "boolean" || !Array.isArray(r.notes) || r.notes.length > 8
      || r.notes.some(n => typeof n !== "string" || n.length > 300))
    throw new Error("This capture could not be read. Capture it again.");
  return { ...r, image: { ...img }, notes: r.notes.slice() };
}

export async function loadCaptureImage(still: CaptureStill): Promise<HTMLImageElement> {
  const image = new Image();
  try {
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("This capture image could not be read."));
      image.src = "data:" + still.image.mime + ";base64," + still.image.data_b64;
    });
    if (image.naturalWidth !== still.image.width || image.naturalHeight !== still.image.height)
      throw new Error("This capture image has changed size.");
    return image;
  } catch (error) {
    image.removeAttribute("src");
    throw error;
  } finally {
    image.onload = image.onerror = null;
  }
}

function encode(canvas: HTMLCanvasElement, mime: string, quality?: number): Promise<Blob> {
  return new Promise((resolve, reject) => canvas.toBlob(
    b => b ? resolve(b) : reject(new Error("This picture could not be exported.")), mime, quality));
}

export async function captureAttachments(still: CaptureStill, ops: readonly Op[]): Promise<Attachment[]> {
  const image = await loadCaptureImage(still);
  const canvas = document.createElement("canvas");
  try {
    const size = exportSize(still.image);
    canvas.width = size.width; canvas.height = size.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("This picture could not be exported.");
    context.drawImage(image, 0, 0, size.width, size.height);
    render(context, ops, size.scale);
    let blob = await encode(canvas, "image/png");
    const type = exportType(blob.size);
    if (type.mime === "image/jpeg") blob = await encode(canvas, type.mime, type.quality);
    if (!blob.size || blob.size > CAPTURE_BYTES) throw new Error("This picture is over 4 MB. Clear some ink and try again.");
    const data = new Uint8Array(await blob.arrayBuffer());
    let bytes = "";
    for (let i = 0; i < data.length; i += 8192)
      bytes += String.fromCharCode(...data.subarray(i, i + 8192));
    return [
      { name: type.name, mime: type.mime, data_b64: btoa(bytes) },
      { name: "page text", mime: CAPTURE_MIME, data_b64: still.capture_id },
    ];
  } finally {
    image.removeAttribute("src");
    canvas.width = canvas.height = 0;
  }
}

/** B-DIRECT-CAPTURE: the daemon's URL-only reference plus the HUD's own cropped frame. */
export function directStill(raw: unknown, image: CaptureStill["image"]): CaptureStill {
  const r = raw as Partial<CaptureStill> | null;
  if (!r || r.title !== "" || r.text_chars !== 0 || r.whole !== false || "image" in r)
    throw new Error("This capture could not be read. Capture it again.");
  return captureReply({ ...r, image });
}
