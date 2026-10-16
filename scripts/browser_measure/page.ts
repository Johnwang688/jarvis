// The HUD side of `scripts/browser_measure.py`: one Remote tab's socket and
// the pane's real frame pipeline (`FramePipe`, `decodeFrame`, `paintFrame`
// from hud/src/lib/remote.ts, wired as RemoteView wires them), with a clock
// on every stage. Bundled by the script into a temporary HUD folder; never
// part of the HUD build. Times are `performance.timeOrigin + now()` (ms since
// the epoch), so the script can line them up with the daemon's and the
// remote page's.
import { FramePipe, FRAME_MAX_BYTES, viewportFor, decodeFrame, paintFrame, type FrameMeta } from "../../hud/src/lib/remote";

const now = () => performance.timeOrigin + performance.now();
interface FrameTrace {
  id: number; bytes: number; w: number; h: number; px: number; recv: number;
  dec0?: number; dec1?: number; drawn?: number; ack?: number; mark?: number; raf?: number;
}
interface Sent { i: number; t: number; msg: any }
const M = {
  frames: [] as FrameTrace[],
  sent: [] as Sent[],
  errors: [] as string[],
  socket: null as WebSocket | null,
  css: { width: 1, height: 1 },
  markY: 3,
  // "decode": the product's FramePipe (acks when a decode starts); "draw":
  // the pre-B-PERF rule, emulated (ack after the draw); "recv": on receipt.
  ackOn: "decode" as "decode" | "draw" | "recv",
};
const byData = new WeakMap<Uint8Array, FrameTrace>();
const byBitmap = new WeakMap<ImageBitmap, FrameTrace>();
let probe: OffscreenCanvas | null = null;

/** The 16-bit mark the fixture page draws: 16 black/white blocks, 6 CSS px wide, at CSS y = markY. */
function readMark(bitmap: ImageBitmap, meta: FrameMeta): number {
  const s = bitmap.width / meta.width;
  if (!probe) probe = new OffscreenCanvas(16, 1);
  const ctx = probe.getContext("2d", { willReadFrequently: true })!;
  for (let b = 0; b < 16; b++) ctx.drawImage(bitmap, Math.round((b * 6 + 3) * s), Math.round(M.markY * s), 1, 1, b, 0, 1, 1);
  const px = ctx.getImageData(0, 0, 16, 1).data;
  let v = 0;
  for (let b = 0; b < 16; b++) if (px[b * 4] < 128) v |= 1 << b;
  return v;
}

const pipe = new FramePipe<ImageBitmap>(
  (bytes) => {
    const tr = byData.get(bytes);
    if (tr) tr.dec0 = now();
    return decodeFrame(bytes).then((bitmap) => {
      if (tr) { tr.dec1 = now(); tr.px = bitmap.width * 65536 + bitmap.height; byBitmap.set(bitmap, tr); }
      return bitmap;
    });
  },
  (bitmap, meta) => {
    const canvas = document.getElementById("c") as HTMLCanvasElement;
    paintFrame(canvas, bitmap, meta, M.css, 100, devicePixelRatio);
    const tr = byBitmap.get(bitmap);
    if (tr) {
      tr.drawn = now();
      requestAnimationFrame(() => { tr.raf = now(); });
      if (M.ackOn === "draw") { send({ t: "ack", id: tr.id }); tr.ack = now(); }
    }
  },
  (_gen, id) => {
    if (M.ackOn !== "decode") return;
    send({ t: "ack", id });
    const tr = M.frames.length ? M.frames[M.frames.length - 1] : null;
    if (tr && tr.id === id) tr.ack = now();
  },
  (bitmap) => {
    // After the ack: reading the mark costs the next frame nothing.
    const tr = byBitmap.get(bitmap);
    if (tr) { try { tr.mark = readMark(bitmap, { width: tr.w, height: tr.h }); } catch (e) { M.errors.push(String(e)); } }
    bitmap.close();
  },
);
const gen = pipe.next();

function send(body: object): boolean {
  const ws = M.socket;
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(body));
  return true;
}

async function start(tabId: string, width: number, height: number, ackOn: "decode" | "draw" | "recv"): Promise<void> {
  M.ackOn = ackOn;
  const box = document.getElementById("s") as HTMLDivElement;
  box.style.width = `${width}px`;
  box.style.height = `${height}px`;
  M.css = { width: box.clientWidth, height: box.clientHeight };
  const r = await fetch(`/browser/tabs/${tabId}/ticket`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
  const { ticket } = await r.json();
  const url = new URL(`/browser/tabs/${tabId}/attach`, location.href);
  url.protocol = "ws:";
  url.searchParams.set("ticket", ticket);
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  M.socket = ws;
  await new Promise<void>((ok, fail) => { ws.onopen = () => ok(); ws.onerror = () => fail(new Error("socket")); });
  ws.onmessage = (event) => {
    if (!(event.data instanceof ArrayBuffer)) return;
    const t = now();
    if (event.data.byteLength < 9 || event.data.byteLength > FRAME_MAX_BYTES + 8) return;
    const head = new DataView(event.data);
    const id = head.getUint32(0), w = head.getUint16(4), h = head.getUint16(6);
    const data = new Uint8Array(event.data, 8);
    const tr: FrameTrace = { id, bytes: data.length, w, h, px: 0, recv: t };
    M.frames.push(tr);
    byData.set(data, tr);
    if (M.ackOn === "recv") { send({ t: "ack", id }); tr.ack = now(); }
    pipe.frame(gen, { id, meta: { width: w, height: h }, data });
  };
  send({ t: "visible", visible: true });
  send({ t: "resize", ...viewportFor(M.css, 100, devicePixelRatio) });
}

/** Send `msgs` at `times` (ms after now), recording each send's time. */
function play(msgs: object[], times: number[]): Promise<void> {
  const t0 = performance.now();
  let i = 0;
  return new Promise((done) => {
    const step = () => {
      while (i < msgs.length && performance.now() - t0 >= times[i]) {
        const t = now();
        if (send(msgs[i])) M.sent.push({ i: M.sent.length, t, msg: msgs[i] });
        i++;
      }
      if (i >= msgs.length) return done();
      const wait = Math.max(0, times[i] - (performance.now() - t0) - 0.5);
      setTimeout(step, wait);
    };
    step();
  });
}

function take() {
  const out = { frames: M.frames, sent: M.sent, errors: M.errors };
  M.frames = []; M.sent = []; M.errors = [];
  return out;
}

(window as any).__m = { start, play, take, now, send, setMark: (y: number) => { M.markY = y; } };
