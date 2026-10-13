import { useEffect, useRef, useState } from "react";

/** One-line chat title; only an overflowing, hovered title animates. */
export function HoverTitle(props: {
  text: string;
  untitled: boolean;
  testid: string;
  onDoubleClick: React.MouseEventHandler<HTMLSpanElement>;
}) {
  const viewport = useRef<HTMLSpanElement>(null);
  const track = useRef<HTMLSpanElement>(null);
  const [hovered, setHovered] = useState(false);

  useEffect(() => {
    if (!hovered || !viewport.current || !track.current) return;
    const outer = viewport.current;
    const inner = track.current;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let animation: Animation | undefined;
    const reset = () => {
      animation?.cancel();
      animation = undefined;
      delete outer.dataset.scrolling;
    };
    const start = () => {
      reset();
      if (motion.matches) return;
      const distance = inner.offsetWidth - outer.clientWidth;
      if (distance <= 0) return;
      outer.dataset.scrolling = "true";
      // A short pause at either end; longer titles keep the same reading speed.
      const travel = distance / 35 * 1000;
      const duration = travel + 1600;
      animation = inner.animate([
        { transform: "translateX(0)", offset: 0 },
        { transform: "translateX(0)", offset: 800 / duration },
        { transform: `translateX(-${distance}px)`, offset: (800 + travel) / duration },
        { transform: `translateX(-${distance}px)`, offset: 1 },
      ], { duration, iterations: Infinity, direction: "alternate", easing: "linear" });
    };
    start();
    const resize = new ResizeObserver(start);
    resize.observe(outer);
    motion.addEventListener("change", start);
    return () => {
      resize.disconnect();
      motion.removeEventListener("change", start);
      reset();
    };
  }, [hovered, props.text]);

  return (
    <span
      ref={viewport}
      className={"nm ttl" + (props.untitled ? " untitled" : "")}
      data-testid={props.testid}
      data-untitled={props.untitled ? "true" : undefined}
      title={props.untitled ? undefined : props.text}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onDoubleClick={props.onDoubleClick}
    >
      <span ref={track} className="title-text">{props.text}</span>
    </span>
  );
}
