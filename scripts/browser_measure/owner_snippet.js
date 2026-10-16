// B-PERF owner recipe (docs/notes/browser.md, "Remote input to picture"):
// paste into the HUD window's DevTools console (Ctrl+Shift+I; type "allow
// pasting" first if Chrome asks), click into the Remote pane showing
// flip.html, press about 100 keys one at a time (two or three a second),
// then run __remoteLatency(). It only listens: it records each key's
// timestamp and each Remote frame's draw, and changes nothing the HUD does.
// Reload the HUD (Ctrl+R) to remove it.
(() => {
  const keys = [];
  const frames = [];
  addEventListener("keydown", (e) => {
    const t = e.target;
    if (!e.repeat && t && t.closest && t.closest(".remotesurface")) keys.push(e.timeStamp);
  }, true);
  const draw = CanvasRenderingContext2D.prototype.drawImage;
  CanvasRenderingContext2D.prototype.drawImage = function (image, ...rest) {
    const out = draw.call(this, image, ...rest);
    if (image instanceof ImageBitmap && this.canvas.closest && this.canvas.closest(".remotesurface")) {
      const drawn = performance.now();
      const row = [drawn, null];
      frames.push(row);
      requestAnimationFrame(() => { row[1] = performance.now(); });
    }
    return out;
  };
  const pct = (v, p) => {
    if (!v.length) return null;
    const s = [...v].sort((a, b) => a - b);
    const k = (s.length - 1) * p / 100, lo = Math.floor(k), hi = Math.min(lo + 1, s.length - 1);
    return Math.round((s[lo] + (s[hi] - s[lo]) * (k - lo)) * 10) / 10;
  };
  window.__remoteLatency = () => {
    const drawnAfter = [], paintedAfter = [];
    for (let i = 0; i < keys.length; i++) {
      const next = i + 1 < keys.length ? keys[i + 1] : Infinity;
      const f = frames.find(([d]) => d >= keys[i] && d < next && d - keys[i] < 1000);
      if (!f) continue;            // pressed again before its frame came, or nothing changed
      drawnAfter.push(f[0] - keys[i]);
      if (f[1] !== null) paintedAfter.push(f[1] - keys[i]);
    }
    const out = {
      keys: keys.length, measured: drawnAfter.length,
      "key → frame drawn p50": pct(drawnAfter, 50), "key → frame drawn p95": pct(drawnAfter, 95),
      "key → next HUD frame p50": pct(paintedAfter, 50), "key → next HUD frame p95": pct(paintedAfter, 95),
    };
    console.table(out);
    return out;
  };
  console.log("Remote latency probe on: press keys in a Remote pane showing flip.html, then __remoteLatency()");
})();
