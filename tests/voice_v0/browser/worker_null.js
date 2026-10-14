// Baseline worker: receives frames and does nothing, to price message passing.
let n = 0;
self.onmessage = (e) => {
  const m = e.data;
  if (m.type === 'init') self.postMessage({ id: m.id, ok: true });
  else if (m.type === 'frame') n++;
  else if (m.type === 'flush') self.postMessage({ id: m.id, frames: n, busy_ms: 0 });
  else if (m.type === 'burst') self.postMessage({ id: m.id, frames: 0 });
};
