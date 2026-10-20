// The owner's Direct capture probe (B-DIRECT-CAPTURE, BR-4/M6).
//
// Runs only on the owner's trusted press, and only in the installed app
// (`display-mode: standalone`). It takes one capture cropped to a HUD-made
// test pattern, a sandboxed srcdoc frame with no scripts, and reports facts
// only. Its pixel samples are the pattern's own known colours, never page
// content. The daemon re-checks every field, owns the pattern's colours, the
// sample positions and the size rule, and decides pass or fail itself
// (jarvis/v2/browser/direct_probe.py). The HUD sends only what it measured.
import { DirectRefused, errorName, frameCovered, grabCroppedFrame, newFacts, type CaptureEnv } from "./directCapture";

/**
 * The pattern is as wide as Settings allows, from 320 up to 1120 CSS px, so a
 * large capture (one Chrome may downscale) is measured as well as a small one.
 * 7:5 is not a window's usual shape, so a crop can't be mistaken for the whole
 * window at an accepted scale.
 */
export const PROBE_MIN = 320, PROBE_MAX = 1120, PROBE_ASPECT = 1.4;
export function probeSize(available: number) {
  const width = Math.max(PROBE_MIN, Math.min(PROBE_MAX, Math.floor(available || PROBE_MIN)));
  return { width, height: Math.round(width / PROBE_ASPECT) };
}
export const PATTERN = {
  tl: [220, 40, 40], tr: [40, 180, 60], bl: [40, 80, 220], br: [240, 200, 40], centre: [250, 250, 250],
} as const;
const rgb = (c: readonly number[]) => `rgb(${c.join(",")})`;

/** No script, no external resource: four solid quadrants and a centre block. */
export function patternHtml(): string {
  const q = (left: string, top: string, c: readonly number[]) =>
    `<div style="position:absolute;left:${left};top:${top};width:50%;height:50%;background:${rgb(c)}"></div>`;
  return "<!doctype html><html><body style=\"margin:0;overflow:hidden\">"
    + q("0", "0", PATTERN.tl) + q("50%", "0", PATTERN.tr) + q("0", "50%", PATTERN.bl) + q("50%", "50%", PATTERN.br)
    + `<div style="position:absolute;left:40%;top:40%;width:20%;height:20%;background:${rgb(PATTERN.centre)}"></div>`
    + "</body></html>";
}

/** Nine points, as fractions of the frame, each well inside one colour. */
export const SAMPLE_POINTS: readonly (readonly [number, number, readonly number[]])[] = [
  [0.15, 0.15, PATTERN.tl], [0.3, 0.33, PATTERN.tl], [0.85, 0.15, PATTERN.tr], [0.7, 0.33, PATTERN.tr],
  [0.15, 0.85, PATTERN.bl], [0.3, 0.67, PATTERN.bl], [0.85, 0.85, PATTERN.br], [0.7, 0.67, PATTERN.br],
  [0.5, 0.5, PATTERN.centre],
];

export function displayMode(match: (query: string) => boolean): string {
  for (const mode of ["standalone", "window-controls-overlay", "fullscreen", "minimal-ui", "browser"])
    if (match(`(display-mode: ${mode})`)) return mode;
  return "unknown";
}

export function chromeMajor(nav: { userAgent?: string; userAgentData?: { brands?: { brand: string; version: string }[] } }): number | null {
  const brand = nav.userAgentData?.brands?.find(b => b.brand === "Google Chrome" || b.brand === "Chromium");
  const n = Number(brand?.version ?? /Chrome\/(\d+)/.exec(nav.userAgent ?? "")?.[1]);
  return Number.isInteger(n) && n > 0 && n < 10000 ? n : null;
}

export interface ProbeFacts {
  display_mode: string; chrome: number | null;
  apis: { get_display_media: boolean; crop_target: boolean; image_capture: boolean };
  prompt: string; error: string;
  track: { display_surface: string; width: number; height: number; frame_rate: number; cursor: string } | null;
  crop: { applied: boolean; error: string };
  frame: { width: number; height: number } | null; viewport: { width: number; height: number };
  rect: { width: number; height: number }; dpr: number;
  samples: { x: number; y: number; got: number[] }[];
  tracks_live_after: number; timing_ms: Record<string, number>;
}

export const PROBE_APP_ONLY = "Run this test in the installed Jarvis app (Chrome's app window), not a browser tab.";

/** Runs the probe against `target` (the pattern frame). Never throws: the facts say what happened. */
export async function runProbe(target: HTMLIFrameElement, env: CaptureEnv, mode: string, chrome: number | null,
  live: () => boolean): Promise<ProbeFacts> {
  const rect = target.getBoundingClientRect();
  const dpr = env.devicePixelRatio;
  const grab = newFacts();
  const facts: ProbeFacts = {
    display_mode: mode, chrome,
    apis: { get_display_media: typeof env.mediaDevices?.getDisplayMedia === "function",
      crop_target: !!env.CropTarget, image_capture: !!env.ImageCapture },
    prompt: "not_shown", error: "", track: null, crop: { applied: false, error: "" },
    frame: null, viewport: { width: Math.round(env.innerWidth), height: Math.round(env.innerHeight) },
    rect: { width: Math.round(rect.width * 1000) / 1000, height: Math.round(rect.height * 1000) / 1000 }, dpr,
    samples: [], tracks_live_after: 0, timing_ms: {},
  };
  const finish = () => {
    facts.prompt = grab.prompt; facts.error = grab.error; facts.track = grab.track; facts.crop = { ...grab.crop };
    facts.tracks_live_after = grab.tracksLiveAfter;
    for (const [k, v] of Object.entries(grab.timing)) facts.timing_ms[k] = Math.round(v * 10) / 10;
    return facts;
  };
  if (frameCovered(target, [], env)) return finish();
  let frame;
  const clear = () => { const c = frameCovered(target, [], env); if (c) throw new DirectRefused(c); };
  try { frame = await grabCroppedFrame(target, env, live, grab, { beforeGrab: clear, atGrab: clear }); }
  catch (e) {
    if (!(e instanceof DirectRefused) && !grab.error) grab.error = errorName(e);
    return finish();
  }
  try {
    facts.frame = { width: frame.width, height: frame.height };
    const points = SAMPLE_POINTS.map(([fx, fy]) => [Math.min(frame.width - 1, Math.floor(frame.width * fx)),
      Math.min(frame.height - 1, Math.floor(frame.height * fy))] as const);
    const got = env.pixels(frame, points);
    facts.samples = SAMPLE_POINTS.map((_, i) => ({ x: points[i][0], y: points[i][1],
      got: (got[i] ?? [0, 0, 0, 0]).slice(0, 4).map(v => Math.max(0, Math.min(255, Math.round(v)))) }));
  } finally { frame.close(); }
  return finish();
}

export const PROBE_REASONS: Record<string, string> = {
  "": "Passed: Chrome shared this tab and cropped one frame to the test pattern.",
  tracks_left: "Failed: the capture did not stop every track.",
  not_app_mode: "Failed: this window is not the installed app.",
  api_missing: "Failed: this Chrome lacks screen sharing, CropTarget or ImageCapture.",
  cancelled: "Cancelled: the share prompt was dismissed. Nothing was captured.",
  prompt_error: "Failed: Chrome refused the share prompt.",
  not_this_tab: "Failed: Chrome shared something other than this tab.",
  not_cropped: "Failed: Chrome did not crop the capture.",
  wrong_size: "Failed: the cropped frame was not the test pattern's shape at any accepted scale.",
  ambiguous: "Failed: the test pattern has the window's shape here, so the crop cannot be told apart. Resize the window and test again.",
  wrong_rect: "Failed: the test pattern was not laid out at its own shape.",
  wrong_pixels: "Failed: the cropped frame's colours did not match the test pattern.",
};
