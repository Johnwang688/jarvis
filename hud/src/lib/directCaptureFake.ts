// Test-only fakes for getDisplayMedia, CropTarget and ImageCapture. Imported
// only by *.test.ts files; nothing in the app imports it. Synthetic streams
// are not M6 acceptance: only the owner's probe in the installed app is.
import type { CaptureEnv, Frame } from "./directCapture";

export interface FakeTrack {
  readyState: "live" | "ended"; stops: number; cropped: unknown;
  stop(): void; getSettings(): Record<string, unknown>; cropTo?: (target: unknown) => Promise<void>;
}
export interface FakeOptions {
  /** What the prompt does: accept, or reject with this error name. */
  prompt?: "accept" | string;
  /** Hold the prompt until `release()` is called. */
  hold?: boolean;
  frame?: { width: number; height: number };
  cropError?: string; grabError?: string; noCropTo?: boolean;
  /** The first n grabs reject with InvalidStateError; `grabHang` never answers. */
  grabFails?: number; grabHang?: boolean;
  surface?: string; trusted?: boolean;
  sample?: Uint8ClampedArray | null;
  pixels?: number[][];
  dpr?: number; innerWidth?: number; innerHeight?: number;
}
export interface Fake {
  env: CaptureEnv; tracks: FakeTrack[]; frames: (Frame & { closed: boolean })[];
  calls: string[]; options: unknown[]; release: () => void;
}

const fail = (name: string) => Object.assign(new Error(name), { name });

export function fakeEnv(o: FakeOptions = {}): Fake {
  const tracks: FakeTrack[] = [], frames: (Frame & { closed: boolean })[] = [], calls: string[] = [], options: unknown[] = [];
  let release = () => {};
  const track = (): FakeTrack => {
    const t: FakeTrack = {
      readyState: "live", stops: 0, cropped: null,
      stop() { t.stops++; t.readyState = "ended"; calls.push("stop"); },
      getSettings: () => ({ displaySurface: o.surface ?? "browser", width: 1600, height: 1000, frameRate: 5, cursor: "never" }),
    };
    if (!o.noCropTo) t.cropTo = async target => {
      calls.push("cropTo");
      if (o.cropError) throw fail(o.cropError);
      t.cropped = target;
    };
    tracks.push(t);
    return t;
  };
  const sample = new Uint8ClampedArray(16 * 16 * 4).map((_, i) => i % 4 === 3 ? 255 : (i * 37) % 256);
  const env: CaptureEnv = {
    document,
    mediaDevices: {
      getDisplayMedia: (opts: unknown) => {
        calls.push("getDisplayMedia"); options.push(opts);
        const answer = () => {
          if (o.prompt && o.prompt !== "accept") throw fail(o.prompt);
          const v = track(), a = track();
          return { getTracks: () => [v, a], getVideoTracks: () => [v] } as unknown as MediaStream;
        };
        if (!o.hold) return Promise.resolve().then(answer);
        return new Promise<MediaStream>((ok, no) => { release = () => { try { ok(answer()); } catch (e) { no(e); } }; });
      },
    },
    CropTarget: { fromElement: async (el: Element) => { calls.push("fromElement"); return { el }; } },
    ImageCapture: class {
      constructor(readonly t: MediaStreamTrack) {}
      async grabFrame() {
        calls.push("grabFrame");
        if (o.grabHang) return new Promise<never>(() => {});
        if (o.grabError) throw fail(o.grabError);
        if (o.grabFails && calls.filter(c => c === "grabFrame").length <= o.grabFails) throw fail("InvalidStateError");
        const size = o.frame ?? { width: 800, height: 500 };
        const f = { ...size, closed: false, close() { f.closed = true; calls.push("close"); } };
        frames.push(f);
        return f;
      }
    },
    devicePixelRatio: o.dpr ?? 2, innerWidth: o.innerWidth ?? 1500, innerHeight: o.innerHeight ?? 1000,
    now: (() => { let n = 0; return () => (n += 5); })(),
    trusted: () => o.trusted ?? true,
    sample: () => { calls.push("sample"); return o.sample === undefined ? sample : o.sample; },
    pixels: (_f, points) => o.pixels ?? points.map(() => [0, 0, 0, 255]),
    encode: async f => { calls.push("encode"); return { mime: "image/png", data_b64: "aGk=", width: f.width, height: f.height }; },
  };
  return { env, tracks, frames, calls, options, release: () => release() };
}

/** Give `el` a layout box and make the document's hit test answer `hit`. */
export function place(el: Element, box: { left: number; top: number; width: number; height: number }) {
  const rect = { ...box, x: box.left, y: box.top, right: box.left + box.width, bottom: box.top + box.height, toJSON() {} };
  el.getBoundingClientRect = () => rect as DOMRect;
  el.getClientRects = () => [rect] as unknown as DOMRectList;
}
