"""Render committed HUD icons with the installed Playwright Chromium, offline.

Run from the repo: PLAYWRIGHT_BROWSERS_PATH=/home/johnw/.cache/ms-playwright
    /home/johnw/projects/Jarvis/.venv/bin/python hud/scripts/render_icons.py
This deliberately copies the orb's idle drawing at t=0 (src/lib/orbdraw.ts, the
films' JARVIS look since 2026-10-27); keep it in sync without moving the live
orb's code. The 16/32px versions reduce the rings to thick arcs.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "public" / "icons"
DRAW = """({size, maskable}) => {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  document.body.replaceChildren(canvas);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#141413'; ctx.fillRect(0, 0, size, size);
  const scale = size / 132 * (maskable ? 0.76 : 1);
  ctx.translate(size / 2, size / 2); ctx.scale(scale, scale);
  const R = 63, energy = 0.09, boost = 0.85, hue = '111,195,223';
  const small = size <= 32;
  const rings = small ? [
    {r: 0.92, arcs: [[0, 2.6], [3.1, 5.9]], w: size === 16 ? 9 : 6, alpha: 0.8},
    ...(size === 32 ? [{r: 0.69, arcs: [[0.2, 2.4], [2.9, 5.6]], w: 5, alpha: 0.65}] : []),
  ] : [
    {r: 0.99, arcs: [[0, Math.PI * 2]], w: 0.75, alpha: 0.35},
    {r: 0.95, ticks: 120, len: 0.03, major: 10, majorLen: 0.085, w: 1, alpha: 0.55},
    {r: 0.845, arcs: [[0, 0.5], [0.6, 0.75], [0.85, 1.9], [2.2, 2.35], [2.45, 3.6], [3.9, 4.6], [4.7, 4.8], [4.95, 5.9]], w: 5, alpha: 0.32},
    {r: 0.77, arcs: [[0.3, 2.6], [3.4, 5.5]], w: 1, alpha: 0.6},
    {r: 0.7, dash: [60, 0.5], w: 2.5, alpha: 0.42},
    {r: 0.6, arcs: [[0, 2.4], [2.9, 5.6]], w: 1, alpha: 0.45},
  ];
  for (const ring of rings) {
    ctx.strokeStyle = `rgba(${hue},${ring.alpha * boost * (0.75 + energy * 0.45)})`;
    ctx.lineWidth = ring.w;
    if (ring.ticks) {
      const outer = R * ring.r;
      ctx.beginPath();
      for (let i = 0; i < ring.ticks; i++) {
        const a = i / ring.ticks * Math.PI * 2;
        const inner = outer * (1 - (i % ring.major === 0 ? ring.majorLen : ring.len));
        ctx.moveTo(Math.cos(a) * inner, Math.sin(a) * inner);
        ctx.lineTo(Math.cos(a) * outer, Math.sin(a) * outer);
      }
      ctx.stroke();
    } else if (ring.dash) {
      const [count, duty] = ring.dash, step = Math.PI * 2 / count;
      ctx.beginPath();
      for (let i = 0; i < count; i++) {
        const a = i * step;
        ctx.moveTo(Math.cos(a) * R * ring.r, Math.sin(a) * R * ring.r);
        ctx.arc(0, 0, R * ring.r, a, a + step * duty);
      }
      ctx.stroke();
    } else for (const [a1, a2] of ring.arcs) {
      ctx.beginPath(); ctx.arc(0, 0, R * ring.r, a1, a2); ctx.stroke();
    }
  }
  const coreR = R * (0.3 + energy * 0.1);
  const glow = ctx.createRadialGradient(0, 0, 0, 0, 0, coreR * 1.9);
  glow.addColorStop(0, `rgba(240,238,232,${0.85 * boost * (0.55 + energy * 0.5)})`);
  glow.addColorStop(0.25, `rgba(${hue},${0.5 * (0.5 + energy * 0.6)})`);
  glow.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(0, 0, coreR * 1.9, 0, Math.PI * 2); ctx.fill();
  // The emblem: a core ring with three brackets (only the ring when small).
  ctx.strokeStyle = `rgba(240,238,232,${0.5 + energy * 0.5})`;
  ctx.shadowColor = `rgb(${hue})`; ctx.shadowBlur = small ? 0 : 18 * (0.6 + energy);
  ctx.lineWidth = small ? (size === 16 ? 9 : 5) : 2;
  ctx.beginPath(); ctx.arc(0, 0, R * (small ? 0.24 : 0.17), 0, Math.PI * 2); ctx.stroke();
  if (!small) {
    ctx.lineWidth = 1.5;
    for (let i = 0; i < 3; i++) {
      const a = -Math.PI / 2 + i / 3 * Math.PI * 2;
      ctx.beginPath(); ctx.arc(0, 0, R * 0.25, a, a + 1.2); ctx.stroke();
    }
  }
  ctx.shadowBlur = 0;
}"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<style>body{margin:0}canvas{display:block}</style>")
        for size, maskable in [(n, False) for n in (16, 32, 48, 192, 512)] + [(512, True)]:
            page.evaluate(DRAW, dict(size=size, maskable=maskable))
            name = f"orb-{size}{'-maskable' if maskable else ''}.png"
            page.locator("canvas").screenshot(path=str(OUT / name))
            print(name)
        browser.close()


if __name__ == "__main__":
    main()
