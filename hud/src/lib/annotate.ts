// Annotate (BR-2; browser plan §5). Annotate freezes a Browser pane as a still
// and the owner draws on it — pen, arrow, box and text, in three colours, with
// undo and clear — before Send to chat stages the picture in the selected
// chat's box (BR-4). This is the drawing model, ported from the whiteboard's
// (`jarvis/face/static/whiteboard.html`: an ops list redrawn in full, undo is
// a pop, `shapeOp`, `shapeIsReal`, `boardXY`'s letterbox maths); the view is
// BR-4's `Annotate.tsx`. `whiteboard.html` stays as it is: it is v1, with no
// module system to share through. Nothing here is wired in yet.
//
// **Ops are in the still's own pixels**, whatever size it is drawn at, so the
// ink lands where it was drawn when the picture is flattened and downscaled.
// Widths are chosen in the CSS px the owner sees (`inkWidth`), so a pen looks
// the same on a 1x still and a 2.4x one.
//
// **The view is under the HUD's CSS zoom**: a pointer's `clientX` and the
// view's `getBoundingClientRect()` are screen pixels, the fit is in the view's
// own CSS px, so a position is divided by the zoom (`toCss`) before it is fitted.
//
// The ink is content, not HUD chrome, so red is fine here (the amber and red
// rules are about the HUD's own meanings, not about what the owner draws).

import { toCss } from "./layout";

export type Tool = "pen" | "arrow" | "box" | "text";
export const TOOLS: readonly Tool[] = ["pen", "arrow", "box", "text"];

/** The three ink colours, drawn into the picture. */
export const INK = [
  { name: "red", value: "#ff3b30" },
  { name: "yellow", value: "#ffd60a" },
  { name: "blue", value: "#0a84ff" },
] as const;

export type Pt = [number, number];

/** One mark, in the still's pixels. */
export type Op =
  /** A pen stroke (one point is a dot). */
  | { t: "s"; c: string; w: number; pts: Pt[] }
  /** An arrow from a to b, its head at b. */
  | { t: "a"; ax: number; ay: number; bx: number; by: number; c: string; lw: number }
  /** A box. */
  | { t: "r"; x: number; y: number; w: number; h: number; c: string; lw: number }
  /** Text, its top-left at (x, y), `s` px high. */
  | { t: "t"; x: number; y: number; text: string; c: string; s: number };

/** Marks per picture, points per stroke and characters per text, at most. */
export const MAX_OPS = 500;
export const MAX_POINTS = 4000;
export const TEXT_MAX = 200;
/** Line and text sizes, in the CSS px the owner sees. */
export const LINE_CSS = 4;
export const TEXT_CSS = 18;

// ---- fitting the still into its view -----------------------------------------------

export interface Size {
  width: number;
  height: number;
}

/** Where the still sits in its view (the view's CSS px): its top-left, and CSS px per still px. */
export interface Fit {
  x: number;
  y: number;
  scale: number;
}

const pos = (n: number, d: number) => (Number.isFinite(n) && n > 0 ? n : d);

/** The still in a view of `view` (its own CSS px), as `object-fit: contain` draws it: whole, centred. */
export function fitStill(view: Size, image: Size): Fit {
  const iw = pos(image.width, 1);
  const ih = pos(image.height, 1);
  const vw = pos(view.width, iw);
  const vh = pos(view.height, ih);
  const scale = Math.min(vw / iw, vh / ih);
  return { x: (vw - iw * scale) / 2, y: (vh - ih * scale) / 2, scale };
}

/**
 * A pointer position (`clientX`/`clientY`, screen px) → a point on the still.
 * `box` is the view's `getBoundingClientRect()`, also screen px, so the offset
 * is divided by the zoom before the fit (view CSS px) applies. A point off the
 * still (its letterbox, or a drag that left the view) is held to its edge.
 */
export function clientToImage(
  client: { x: number; y: number }, box: { left: number; top: number }, zoomPct: number, fit: Fit, image: Size,
): Pt {
  const s = pos(fit.scale, 1);
  const x = (toCss(client.x - box.left, zoomPct) - fit.x) / s;
  const y = (toCss(client.y - box.top, zoomPct) - fit.y) / s;
  const clamp = (n: number, max: number) => Math.round(Math.min(max, Math.max(0, n)) * 10) / 10;
  return [clamp(x, pos(image.width, 1)), clamp(y, pos(image.height, 1))];
}

/** A width the owner sees as `css` px, in still px. */
export function inkWidth(css: number, fit: Fit): number {
  return Math.round((pos(css, 1) / pos(fit.scale, 1)) * 100) / 100;
}

// ---- making marks ------------------------------------------------------------------

/** A stroke begun at `at`. */
export function startStroke(at: Pt, colour: string, width: number): Op {
  return { t: "s", c: colour, w: width, pts: [at] };
}

/** One more point on a stroke, when it has moved at least `minStep` and the stroke has room. */
export function addPoint(op: Op, at: Pt, minStep = 1): Op {
  if (op.t !== "s" || op.pts.length >= MAX_POINTS) return op;
  const [lx, ly] = op.pts[op.pts.length - 1];
  if (Math.hypot(at[0] - lx, at[1] - ly) < minStep) return op;
  return { ...op, pts: [...op.pts, at] };
}

/**
 * A box or an arrow dragged from `from` to `to`. Shift makes the box a square
 * and snaps the arrow to 45° steps, as on the whiteboard.
 */
export function shapeOp(
  tool: "arrow" | "box", from: Pt, to: Pt, opts: { colour: string; width: number; shift?: boolean },
): Op {
  const [x0, y0] = from;
  let [x1, y1] = to;
  if (tool === "box") {
    let w = x1 - x0;
    let h = y1 - y0;
    if (opts.shift) {
      const side = Math.max(Math.abs(w), Math.abs(h));
      w = Math.sign(w || 1) * side;
      h = Math.sign(h || 1) * side;
    }
    return { t: "r", x: Math.min(x0, x0 + w), y: Math.min(y0, y0 + h), w: Math.abs(w), h: Math.abs(h), c: opts.colour, lw: opts.width };
  }
  if (opts.shift) {
    const angle = Math.round(Math.atan2(y1 - y0, x1 - x0) / (Math.PI / 4)) * (Math.PI / 4);
    const len = Math.hypot(x1 - x0, y1 - y0);
    x1 = x0 + Math.cos(angle) * len;
    y1 = y0 + Math.sin(angle) * len;
  }
  return { t: "a", ax: x0, ay: y0, bx: x1, by: y1, c: opts.colour, lw: opts.width };
}

/** Text the owner typed, as one line, capped; null when there is none. */
export function textOp(at: Pt, raw: string, colour: string, size: number): Op | null {
  // eslint-disable-next-line no-control-regex
  const one = (raw || "").replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, " ").replace(/\s+/g, " ").trim();
  if (!one) return null;
  const text = Array.from(one).slice(0, TEXT_MAX).join("");
  return { t: "t", x: at[0], y: at[1], text, c: colour, s: size };
}

/** Whether a mark is worth keeping: a slip of the pointer is not a box or an arrow. */
export function isReal(op: Op): boolean {
  switch (op.t) {
    case "s":
      return op.pts.length >= 1;
    case "r":
      return op.w > 3 || op.h > 3;
    case "a":
      return Math.hypot(op.bx - op.ax, op.by - op.ay) > 3;
    case "t":
      return op.text.trim().length > 0;
  }
}

/** The list with `op` added, when it is real and there is room. */
export function commit(ops: readonly Op[], op: Op | null): Op[] {
  if (!op || !isReal(op) || ops.length >= MAX_OPS) return ops.slice();
  return [...ops, op];
}

/** Undo: the last mark goes. */
export function undo(ops: readonly Op[]): Op[] {
  return ops.slice(0, -1);
}

/** Clear: every mark goes. */
export function clear(): Op[] {
  return [];
}

// ---- drawing -----------------------------------------------------------------------

/**
 * The arrow's head: its tip, its two barbs, and where the shaft stops (at the
 * head's base, so the shaft's round cap never pokes through the tip). Null
 * for an arrow of no length.
 */
export function arrowHead(op: Extract<Op, { t: "a" }>): { tip: Pt; left: Pt; right: Pt; shaftEnd: Pt } | null {
  const dx = op.bx - op.ax;
  const dy = op.by - op.ay;
  const len = Math.hypot(dx, dy);
  if (!(len > 0)) return null;
  const head = Math.min(len * 0.6, Math.max(op.lw * 4, 12));
  const angle = Math.atan2(dy, dx);
  const spread = Math.PI / 7;
  const at = (a: number): Pt => [op.bx - head * Math.cos(a), op.by - head * Math.sin(a)];
  const back = head * Math.cos(spread);
  return {
    tip: [op.bx, op.by],
    left: at(angle - spread),
    right: at(angle + spread),
    shaftEnd: [op.bx - back * Math.cos(angle), op.by - back * Math.sin(angle)],
  };
}

/** The parts of a 2D context the ink uses (a recorder stands in for one in tests). */
export interface InkContext {
  lineCap: string;
  lineJoin: string;
  lineWidth: number;
  strokeStyle: unknown;
  fillStyle: unknown;
  font: string;
  textBaseline: string;
  save(): void;
  restore(): void;
  scale(x: number, y: number): void;
  beginPath(): void;
  closePath(): void;
  moveTo(x: number, y: number): void;
  lineTo(x: number, y: number): void;
  arc(x: number, y: number, r: number, start: number, end: number): void;
  stroke(): void;
  fill(): void;
  strokeRect(x: number, y: number, w: number, h: number): void;
  strokeText(text: string, x: number, y: number): void;
  fillText(text: string, x: number, y: number): void;
}

/** A halo under text, so it reads on any page. */
const HALO = "rgba(0, 0, 0, 0.7)";

function drawOp(ctx: InkContext, op: Op) {
  switch (op.t) {
    case "s": {
      if (op.pts.length === 1) {
        ctx.fillStyle = op.c;
        ctx.beginPath();
        ctx.arc(op.pts[0][0], op.pts[0][1], op.w / 2, 0, Math.PI * 2);
        ctx.fill();
        return;
      }
      ctx.strokeStyle = op.c;
      ctx.lineWidth = op.w;
      ctx.beginPath();
      op.pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
      return;
    }
    case "a": {
      const h = arrowHead(op);
      if (!h) return;
      ctx.strokeStyle = op.c;
      ctx.lineWidth = op.lw;
      ctx.beginPath();
      ctx.moveTo(op.ax, op.ay);
      ctx.lineTo(h.shaftEnd[0], h.shaftEnd[1]);
      ctx.stroke();
      ctx.fillStyle = op.c;
      ctx.beginPath();
      ctx.moveTo(h.tip[0], h.tip[1]);
      ctx.lineTo(h.left[0], h.left[1]);
      ctx.lineTo(h.right[0], h.right[1]);
      ctx.closePath();
      ctx.fill();
      return;
    }
    case "r":
      ctx.strokeStyle = op.c;
      ctx.lineWidth = op.lw;
      ctx.strokeRect(op.x, op.y, op.w, op.h);
      return;
    case "t":
      ctx.font = `${op.s}px sans-serif`;
      ctx.textBaseline = "top";
      ctx.strokeStyle = HALO;
      ctx.lineWidth = Math.max(1, op.s / 6);
      ctx.strokeText(op.text, op.x, op.y);
      ctx.fillStyle = op.c;
      ctx.fillText(op.text, op.x, op.y);
      return;
  }
}

/**
 * Every mark, drawn at `scale` context px per still px: the view's canvas
 * (its backing px per still px) or the flattened picture's (its size over the
 * still's). The context is left as it was found.
 */
export function render(ctx: InkContext, ops: readonly Op[], scale: number) {
  ctx.save();
  try {
    ctx.scale(pos(scale, 1), pos(scale, 1));
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    for (const op of ops) drawOp(ctx, op);
  } finally {
    ctx.restore();
  }
}

// ---- the picture Send to chat stages -----------------------------------------------

/** The flattened picture's longest edge, at most. */
export const EXPORT_EDGE = 1920;
/** Past this a PNG is sent as a JPEG instead (3.5 MB). */
export const EXPORT_MAX_BYTES = 3.5 * 1024 * 1024;
export const JPEG_QUALITY = 0.9;

/** The flattened picture's size: the still's, downscaled so its longest edge is at most 1920 px. */
export function exportSize(image: Size): Size & { scale: number } {
  const w = pos(image.width, 1);
  const h = pos(image.height, 1);
  const scale = Math.min(1, EXPORT_EDGE / Math.max(w, h));
  return { width: Math.max(1, Math.round(w * scale)), height: Math.max(1, Math.round(h * scale)), scale };
}

/** How the picture is staged, given the PNG's size: as `page.png`, or past 3.5 MB as `page.jpg` at quality 0.9. */
export function exportType(pngBytes: number): { mime: "image/png"; name: string } | { mime: "image/jpeg"; name: string; quality: number } {
  if (Number.isFinite(pngBytes) && pngBytes >= 0 && pngBytes <= EXPORT_MAX_BYTES) {
    return { mime: "image/png", name: "page.png" };
  }
  return { mime: "image/jpeg", name: "page.jpg", quality: JPEG_QUALITY };
}
