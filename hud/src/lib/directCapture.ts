// B-DIRECT-CAPTURE (BR-4/M6): one cropped frame of the owner's own Direct tab.
//
// Off until the owner's probe passes and the owner turns `direct_capture` on
// (docs/plans/2026-10-19-direct-capture-plan.md). Only the owner's trusted
// press on Annotate or Send to chat starts it. Chrome asks to share this tab;
// the stream is cropped to the iframe with CropTarget, exactly one frame is
// grabbed, and **every track stops at once on every path**: success, a
// cancelled prompt, an error at any step, a failed pixel check, or the gesture
// going stale (card, hidden pane, tab change, unmount) during any await.
//
// A tab capture shows whatever the HUD paints over the frame, so the frame is
// checked uncovered before the prompt and again after the grab. Nothing here
// focuses the frame. The pixels are checked before use: the frame's size is
// the iframe's on-screen size × devicePixelRatio, it is not the whole tab,
// and it is not blank. Anything else is refused with a sentence, and the
// caller falls back to the existing "Open in Remote" offer.

export const DIRECT_FALLBACK = "Open this in Remote to annotate.";
export const DIRECT_REASONS = {
  unavailable: "This Chrome cannot capture a Direct page. " + DIRECT_FALLBACK,
  gesture: "Press Annotate or Send to chat yourself to capture a Direct page.",
  hidden: "This page is not fully on screen, so it was not captured. " + DIRECT_FALLBACK,
  covered: "Something covers this page, so it was not captured. Close it and try again, or open this in Remote to annotate.",
  cancelled: "Chrome's share prompt was cancelled. Nothing was captured.",
  not_cropped: "Chrome did not crop the capture to this page, so it was discarded. " + DIRECT_FALLBACK,
  size: "The captured picture did not match this page's size, so it was discarded. " + DIRECT_FALLBACK,
  blank: "The captured picture was blank, so it was discarded. " + DIRECT_FALLBACK,
  failed: "The capture did not finish. " + DIRECT_FALLBACK,
  stale: "",
} as const;
export type DirectReason = keyof typeof DIRECT_REASONS;

export class DirectRefused extends Error {
  constructor(readonly reason: DirectReason, readonly errorName = "") { super(DIRECT_REASONS[reason]); }
}

export const DISPLAY_OPTIONS = {
  video: { displaySurface: "browser", frameRate: { max: 5 } },
  audio: false,
  preferCurrentTab: true,
  selfBrowserSurface: "include",
  surfaceSwitching: "exclude",
  monitorTypeSurfaces: "exclude",
} as const;

export interface Frame { width: number; height: number; close(): void }
export interface CaptureEnv {
  document: Document;
  mediaDevices?: { getDisplayMedia?: (options: unknown) => Promise<MediaStream> };
  CropTarget?: { fromElement(element: Element): Promise<unknown> };
  ImageCapture?: new (track: MediaStreamTrack) => { grabFrame(): Promise<Frame> };
  devicePixelRatio: number; innerWidth: number; innerHeight: number;
  now(): number;
  /** The press that asked for the capture was the owner's own. */
  trusted(event: { isTrusted: boolean }): boolean;
  /** A coarse 16×16 RGBA sample of the frame, for the blank check. */
  sample(frame: Frame): Uint8ClampedArray | null;
  /** RGBA at exact frame pixels (the probe's test pattern). */
  pixels(frame: Frame, points: readonly (readonly [number, number])[]): number[][];
  /** The frame as a staged still, at most 1920 px on its long edge. */
  encode(frame: Frame): Promise<{ mime: "image/png" | "image/jpeg"; data_b64: string; width: number; height: number }>;
}

/** The elements whose paint over a Direct frame would land in a tab capture. */
export const OVERLAYS = [
  '[role="dialog"]:not([hidden])', '[role="alertdialog"]', '[role="menu"]', '[role="listbox"]',
  '[role="tooltip"]', "dialog[open]", '[data-testid="approval-card"]', '[data-testid="browser-approval"]',
  '[data-testid="dev-server-prompt"]', ".approval-card", ".toast",
].join(",");

type Box = { left: number; top: number; right: number; bottom: number; width: number; height: number };
const intersects = (a: Box, b: Box) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

/** "" when the frame is fully on screen and nothing paints over it. */
export function frameCovered(frame: Element, allowed: readonly Element[], env: Pick<CaptureEnv, "document" | "innerWidth" | "innerHeight">): "" | "hidden" | "covered" {
  const doc = env.document;
  if (doc.visibilityState !== "visible" || !frame.isConnected
      || frame.closest('[hidden],[inert],[aria-hidden="true"]')) return "hidden";
  const r = frame.getBoundingClientRect();
  if (!(r.width >= 2 && r.height >= 2) || r.left < 0 || r.top < 0
      || r.right > env.innerWidth + 0.5 || r.bottom > env.innerHeight + 0.5) return "hidden";
  if (typeof doc.elementFromPoint !== "function") return "covered";
  for (let i = 0; i < 5; i++) for (let j = 0; j < 5; j++) {
    const x = r.left + 1 + (r.width - 2) * i / 4, y = r.top + 1 + (r.height - 2) * j / 4;
    const hit = doc.elementFromPoint(x, y);
    if (!hit || (hit !== frame && !allowed.includes(hit))) return "covered";
  }
  // Overlays that do not hit-test (pointer-events: none) still paint.
  for (const el of Array.from(doc.querySelectorAll(OVERLAYS))) {
    if (el.contains(frame) || !el.getClientRects().length) continue;
    if (intersects(el.getBoundingClientRect(), r)) return "covered";
  }
  return "";
}

/** Device pixels the cropped frame should have: CSS size × devicePixelRatio. */
export function expectedSize(rect: { width: number; height: number }, dpr: number) {
  return { width: Math.round(rect.width * dpr), height: Math.round(rect.height * dpr) };
}

export const SIZE_SLACK = 2;
/** "" when the frame is the iframe's own size and not the whole tab. */
export function checkFrame(frame: { width: number; height: number }, rect: { width: number; height: number },
  dpr: number, viewport: { width: number; height: number }): "" | "size" | "not_cropped" {
  const want = expectedSize(rect, dpr);
  const whole = expectedSize(viewport, dpr);
  const near = (a: number, b: number) => Math.abs(a - b) <= SIZE_SLACK;
  if (near(frame.width, whole.width) && near(frame.height, whole.height)
      && !(near(want.width, whole.width) && near(want.height, whole.height))) return "not_cropped";
  if (!near(frame.width, want.width) || !near(frame.height, want.height) || !want.width || !want.height) return "size";
  return "";
}

/** No sample, all transparent, or one colour throughout. */
export function blank(samples: Uint8ClampedArray | null): boolean {
  if (!samples || samples.length < 8 || samples.length % 4) return true;
  if (samples.every((v, i) => i % 4 !== 3 || v === 0)) return true;
  for (let i = 4; i < samples.length; i++)
    if (Math.abs(samples[i] - samples[i % 4]) > 6) return false;
  return true;
}

export function stopAll(stream: MediaStream | null | undefined) {
  if (!stream) return;
  for (const track of stream.getTracks()) { try { track.stop(); } catch { /* already ended */ } }
}

export function errorName(error: unknown): string {
  const name = error && typeof error === "object" && "name" in error ? String((error as { name: unknown }).name) : "";
  return ["NotAllowedError", "AbortError", "InvalidStateError", "NotFoundError", "NotReadableError", "NotSupportedError",
    "OverconstrainedError", "SecurityError", "TypeError", "TimeoutError"].includes(name) ? name : "OtherError";
}

export interface GrabFacts {
  prompt: "accepted" | "cancelled" | "error" | "not_shown"; error: string;
  track: { display_surface: string; width: number; height: number; frame_rate: number; cursor: string } | null;
  crop: { applied: boolean; error: string };
  tracksLiveAfter: number; timing: Record<string, number>;
}

/**
 * One cropped frame of `target`. The caller owns the returned frame and must
 * `close()` it. `live()` is checked after every await; a stale gesture stops
 * the stream the moment it arrives. `facts` is filled in as the capture goes,
 * for the owner's probe.
 */
export async function grabCroppedFrame(target: Element, env: CaptureEnv, live: () => boolean,
  facts: GrabFacts = newFacts()): Promise<Frame> {
  const start = env.now(), mark = (k: string, from: number) => { facts.timing[k] = Math.max(0, env.now() - from); };
  const gdm = env.mediaDevices?.getDisplayMedia;
  if (typeof gdm !== "function" || !env.CropTarget || !env.ImageCapture) throw new DirectRefused("unavailable");
  let crop: unknown;
  let t = env.now();
  try { crop = await env.CropTarget.fromElement(target); }
  catch (e) { throw new DirectRefused("unavailable", errorName(e)); }
  mark("crop_target", t);
  if (!live()) throw new DirectRefused("stale");
  let stream: MediaStream | null = null;
  try {
    t = env.now();
    try { stream = await gdm.call(env.mediaDevices, DISPLAY_OPTIONS); }
    catch (e) {
      facts.error = errorName(e);
      facts.prompt = facts.error === "NotAllowedError" || facts.error === "AbortError" ? "cancelled" : "error";
      throw new DirectRefused(facts.prompt === "cancelled" ? "cancelled" : "failed", facts.error);
    }
    mark("prompt", t);
    facts.prompt = "accepted";
    if (!live()) throw new DirectRefused("stale");
    const track = stream.getVideoTracks()[0];
    if (!track) throw new DirectRefused("failed");
    const s = (track.getSettings?.() ?? {}) as MediaTrackSettings & { displaySurface?: string; cursor?: string };
    facts.track = {
      display_surface: ["browser", "window", "monitor"].includes(s.displaySurface ?? "") ? s.displaySurface! : "unknown",
      width: Math.max(0, Math.round(s.width ?? 0)), height: Math.max(0, Math.round(s.height ?? 0)),
      frame_rate: Math.max(0, Number(s.frameRate ?? 0) || 0),
      cursor: ["never", "always", "motion"].includes(s.cursor ?? "") ? s.cursor! : "unknown",
    };
    if (facts.track.display_surface !== "browser" && facts.track.display_surface !== "unknown")
      throw new DirectRefused("not_cropped");
    const cropTo = (track as MediaStreamTrack & { cropTo?: (c: unknown) => Promise<void> }).cropTo;
    if (typeof cropTo !== "function") { facts.crop.error = "NotSupportedError"; throw new DirectRefused("not_cropped"); }
    t = env.now();
    try { await cropTo.call(track, crop); facts.crop.applied = true; }
    catch (e) { facts.crop.error = errorName(e); throw new DirectRefused("not_cropped", facts.crop.error); }
    mark("crop", t);
    if (!live()) throw new DirectRefused("stale");
    t = env.now();
    let frame: Frame;
    try { frame = await new env.ImageCapture(track).grabFrame(); }
    catch (e) { throw new DirectRefused("failed", errorName(e)); }
    mark("frame", t);
    // One frame is all: the tracks stop before anything looks at it.
    stopAll(stream);
    if (!live()) { frame.close(); throw new DirectRefused("stale"); }
    return frame;
  } finally {
    stopAll(stream);
    facts.tracksLiveAfter = stream ? stream.getTracks().filter(x => x.readyState !== "ended").length : 0;
    facts.timing.total = Math.max(0, env.now() - start);
  }
}

export function newFacts(): GrabFacts {
  return { prompt: "not_shown", error: "", track: null, crop: { applied: false, error: "" }, tracksLiveAfter: 0, timing: {} };
}

/** A Direct frame, checked: uncovered before and after, the right size, not blank. */
export async function captureDirect(frame: HTMLIFrameElement, env: CaptureEnv, live: () => boolean) {
  const shield = frame.nextElementSibling?.classList.contains("frameshield") ? [frame.nextElementSibling] : [];
  const before = frameCovered(frame, shield, env);
  if (before) throw new DirectRefused(before);
  const shot = await grabCroppedFrame(frame, env, live);
  try {
    const after = frameCovered(frame, shield, env);
    if (after) throw new DirectRefused(after);
    const rect = frame.getBoundingClientRect();
    const size = checkFrame(shot, rect, env.devicePixelRatio, { width: env.innerWidth, height: env.innerHeight });
    if (size) throw new DirectRefused(size);
    if (blank(env.sample(shot))) throw new DirectRefused("blank");
    const image = await env.encode(shot);
    if (!live()) throw new DirectRefused("stale");
    return image;
  } finally {
    shot.close();
  }
}

/** The real browser's capture environment. */
export function browserEnv(): CaptureEnv {
  const w = window as unknown as Record<string, unknown>;
  const canvas = (width: number, height: number) => {
    const c = document.createElement("canvas"); c.width = width; c.height = height;
    return c;
  };
  return {
    document,
    mediaDevices: navigator.mediaDevices as CaptureEnv["mediaDevices"],
    CropTarget: w.CropTarget as CaptureEnv["CropTarget"],
    ImageCapture: w.ImageCapture as CaptureEnv["ImageCapture"],
    get devicePixelRatio() { return window.devicePixelRatio || 1; },
    get innerWidth() { return window.innerWidth; },
    get innerHeight() { return window.innerHeight; },
    now: () => performance.now(),
    trusted: e => e.isTrusted === true,
    sample(frame) {
      const c = canvas(16, 16), ctx = c.getContext("2d", { willReadFrequently: true });
      try {
        if (!ctx) return null;
        ctx.drawImage(frame as unknown as CanvasImageSource, 0, 0, 16, 16);
        return ctx.getImageData(0, 0, 16, 16).data;
      } finally { c.width = c.height = 0; }
    },
    pixels(frame, points) {
      const c = canvas(frame.width, frame.height), ctx = c.getContext("2d", { willReadFrequently: true });
      try {
        if (!ctx) return points.map(() => [0, 0, 0, 0]);
        ctx.drawImage(frame as unknown as CanvasImageSource, 0, 0);
        return points.map(([x, y]) => Array.from(ctx.getImageData(x, y, 1, 1).data));
      } finally { c.width = c.height = 0; }
    },
    async encode(frame) {
      const scale = Math.min(1, 1920 / Math.max(frame.width, frame.height));
      const width = Math.max(1, Math.round(frame.width * scale)), height = Math.max(1, Math.round(frame.height * scale));
      const c = canvas(width, height), ctx = c.getContext("2d");
      try {
        if (!ctx) throw new DirectRefused("failed");
        ctx.drawImage(frame as unknown as CanvasImageSource, 0, 0, width, height);
        const blob = (mime: string, q?: number) => new Promise<Blob>((ok, no) =>
          c.toBlob(b => b ? ok(b) : no(new DirectRefused("failed")), mime, q));
        let out = await blob("image/png"), mime: "image/png" | "image/jpeg" = "image/png";
        if (out.size > 3.5 * 1024 * 1024) { out = await blob("image/jpeg", 0.9); mime = "image/jpeg"; }
        const data = new Uint8Array(await out.arrayBuffer());
        let bytes = "";
        for (let i = 0; i < data.length; i += 8192) bytes += String.fromCharCode(...data.subarray(i, i + 8192));
        return { mime, data_b64: btoa(bytes), width, height };
      } finally { c.width = c.height = 0; }
    },
  };
}
