import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { noPointerFocus } from "../lib/talkkey";
import type { PaneNo } from "../lib/workspace";
import type { PendingDev } from "../state/devservers";

export interface DevTarget { pane: PaneNo; slot: number; label: string; disabled: boolean; reason?: string; terminalId?: string }
export function DevServerPrompts(props: {
  rows: readonly PendingDev[]; hidden: boolean; layoutKey: string; zoom: number;
  label: (row: PendingDev) => string; targets: readonly DevTarget[];
  onOpen: (id: string) => void; onPlace: (id: string, pane: PaneNo) => void; onDismiss: (id: string) => void;
}) {
  const [geometry, setGeometry] = useState({ top: 36, left: 400, width: 520, maxHeight: 200 });
  useLayoutEffect(() => {
    const title = document.getElementById("titlebar");
    const main = document.getElementById("main");
    const measure = () => {
      const zoom = props.zoom / 100, rect = main?.getBoundingClientRect();
      const top = (title?.getBoundingClientRect().bottom ?? 28) / zoom + 8;
      const inputTop = Math.min(...Array.from(main?.querySelectorAll<HTMLElement>('[data-testid="input"]') ?? [])
        .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== "hidden")
        .map(e => e.getBoundingClientRect().top), rect?.bottom ?? window.innerHeight);
      setGeometry({ top, left: ((rect?.left ?? 0) + (rect?.width ?? window.innerWidth) / 2) / zoom,
        width: Math.max(0, Math.min(540, (rect?.width ?? window.innerWidth) / zoom - 16)),
        maxHeight: Math.max(0, inputTop / zoom - top - 8) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    if (title) observer.observe(title);
    if (main) observer.observe(main);
    window.addEventListener("resize", measure);
    return () => { observer.disconnect(); window.removeEventListener("resize", measure); };
  }, [props.zoom, props.layoutKey, props.hidden]);
  if (props.hidden || !props.rows.length) return null;
  return <div className="devserver-prompts" data-testid="devserver-prompts" style={geometry}>
    {props.rows.slice(0, 3).map(row => <DevServerPrompt key={row.id} {...props} row={row} />)}
    {props.rows.length > 3 ? <div className="devserver-more">+{props.rows.length - 3} more</div> : null}
  </div>;
}
function DevServerPrompt(props: Parameters<typeof DevServerPrompts>[0] & { row: PendingDev }) {
  const ref = useRef<HTMLElement>(null);
  const until = useRef(Infinity);
  const [armed, setArmed] = useState(false);
  // A preceding prompt can move this card without resizing it. Observe its
  // actual bounds while visible, not only its dimensions or window resize.
  useEffect(() => {
    let previous = "", frame = 0;
    const arm = () => {
      const rect = ref.current?.getBoundingClientRect();
      const bounds = rect ? [rect.x, rect.y, rect.width, rect.height].map(x => Math.round(x * 2)).join(":") : "";
      if (bounds !== previous) { previous = bounds; until.current = performance.now() + 500; setArmed(false); }
      if (bounds && performance.now() >= until.current) setArmed(true);
      frame = requestAnimationFrame(arm);
    };
    until.current = performance.now() + 500; setArmed(false); frame = requestAnimationFrame(arm);
    return () => cancelAnimationFrame(frame);
  }, [props.layoutKey, props.row.phase]);
  const ready = () => !props.hidden && performance.now() >= until.current;
  const dismiss = () => { if (ready()) props.onDismiss(props.row.id); };
  const place = (target: DevTarget) => { if (ready() && !target.disabled) props.onPlace(props.row.id, target.pane); };
  const notice = props.row.phase === "notice";
  const lost = props.row.phase === "lost";
  let host = "local server";
  try { host = new URL(props.row.url).host; } catch { /* parsed again before any placement */ }
  return <section ref={ref} className="devserver-prompt" data-testid="devserver-prompt"
    data-dev-id={props.row.id} tabIndex={0} aria-label="Open development server"
    onKeyDown={e => {
      // Local handler: numbers elsewhere remain the owner's text.
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); dismiss(); return; }
      if (!/^[1-8]$/.test(e.key) || notice || lost) return;
      const target = props.targets.find(t => t.slot === Number(e.key));
      if (target) { e.preventDefault(); e.stopPropagation(); place(target); }
    }}>
    <div className="devserver-heading">Dev server at {host} from {props.label(props.row)}</div>
    {lost ? <div>Opened in another window.</div> : notice ? <div className="devserver-actions">
      <button type="button" aria-disabled={!armed} onMouseDown={noPointerFocus}
        onClick={() => { if (ready()) props.onOpen(props.row.id); }}>Open</button>
    </div> : <div className="devserver-targets">
      {props.targets.map(target => {
        const reason = target.terminalId && props.row.source.kind === "terminal" && target.terminalId === props.row.source.id
          ? "this is where it started" + (target.reason ? " · " + target.reason : "") : target.reason;
        return <button type="button" key={target.pane}
        data-dev-slot={target.slot} aria-disabled={!armed || target.disabled}
        title={reason} onMouseDown={noPointerFocus} onClick={() => place(target)}>
        {target.slot} · {target.label}{reason ? <small>{reason}</small> : null}
      </button>; })}
    </div>}
    <button type="button" className="devserver-dismiss" aria-disabled={!armed}
      onMouseDown={noPointerFocus} onClick={dismiss}>Don't open</button>
  </section>;
}
