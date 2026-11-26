import { useEffect, useRef } from "react";
import { DEFAULT_RINGS, drawEmblem, drawRing } from "@hud/lib/orbdraw";
import { palette, subscribeCool } from "@hud/lib/orbpalette";

export function Orb() {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const view = canvas.current;
    if (!view || typeof ResizeObserver === "undefined") return;
    const ctx = view.getContext("2d");
    if (!ctx) return;

    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let frame = 0;
    let start: number | undefined;

    const paint = (now: number) => {
      start ??= now;
      const elapsed = motion.matches ? 0 : (now - start) / 1000;
      const size = view.clientWidth;
      if (!size) return;
      const scale = Math.min(window.devicePixelRatio, 2);
      const pixels = Math.round(size * scale);
      if (view.width !== pixels || view.height !== pixels) {
        view.width = pixels;
        view.height = pixels;
      }
      ctx.setTransform(pixels / size, 0, 0, pixels / size, 0, 0);
      ctx.clearRect(0, 0, size, size);
      const center = size / 2;
      const radius = size * 0.44;
      const { hue, boost, spin } = palette("idle");
      for (const ring of DEFAULT_RINGS) {
        ctx.lineWidth = ring.w;
        ctx.strokeStyle = `rgba(${hue},${ring.alpha * boost})`;
        drawRing(ctx, ring, center, center, radius, elapsed * ring.speed * spin);
      }
      drawEmblem(ctx, center, center, radius, elapsed * 0.24 * spin, 0.9, hue, 15);
    };

    const tick = (now: number) => {
      paint(now);
      frame = requestAnimationFrame(tick);
    };
    const refresh = () => {
      cancelAnimationFrame(frame);
      paint(performance.now());
      if (!motion.matches && !document.hidden) frame = requestAnimationFrame(tick);
    };

    const resize = new ResizeObserver(refresh);
    resize.observe(view);
    const unsubscribe = subscribeCool(refresh);
    motion.addEventListener("change", refresh);
    document.addEventListener("visibilitychange", refresh);
    refresh();
    return () => {
      cancelAnimationFrame(frame);
      resize.disconnect();
      unsubscribe();
      motion.removeEventListener("change", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  // The id is where the startup animation's orb comes to rest.
  return <canvas className="agent-orb" id="orb" ref={canvas} data-orb aria-hidden="true" />;
}
