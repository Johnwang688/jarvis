import { useRef } from "react";
import { browserLabel } from "../lib/browser";
import type { BrowserTabSpec } from "../lib/workspace";

/** Direct and future Remote tabs share this strip; page content lives elsewhere. */
export function BrowserTabStrip(props: {
  tabs: readonly BrowserTabSpec[]; active: string; blocked?: boolean; unseen?: readonly string[]; soundOn?: boolean;
  onActivate: (id: string) => void; onClose: (id: string) => void; onNew: () => void;
}) {
  const row = useRef<HTMLDivElement>(null);
  return <div className="browsertabs" role="tablist" aria-label="Browser pages" ref={row} data-testid="browser-tabs">
    {props.tabs.map((tab, i) => <div className="browsertab" key={tab.id} role="presentation"
      onAuxClick={(e) => { if (e.button === 1 && !props.blocked) { e.preventDefault(); props.onClose(tab.id); } }}
      onMouseDown={(e) => { if (e.button === 1) e.preventDefault(); }}>
      <button type="button" role="tab" aria-selected={tab.id === props.active}
        data-testid="browser-tab" data-browser-tab={tab.id}
        tabIndex={tab.id === props.active ? 0 : -1} disabled={props.blocked}
        onClick={() => props.onActivate(tab.id)}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (props.blocked || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
          e.preventDefault();
          const next = props.tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + props.tabs.length) % props.tabs.length];
          props.onActivate(next.id);
          row.current?.querySelector<HTMLButtonElement>('[data-browser-tab="' + next.id + '"]')?.focus();
        }}>
        {tab.auto ? <span className="browserautomark" aria-label="Opened automatically">·</span> : null}
        {props.unseen?.includes(tab.id) ? <span className="browser-unseen" aria-label="New tab" /> : null}
        <span>{tab.title || browserLabel(tab.url)}</span>
        {props.soundOn && tab.mode==="remote" ? <span className="small muted"> · sound on</span> : null}
      </button>
      <button type="button" className="browsertabclose" data-testid="browser-tab-close" tabIndex={-1}
        aria-label={"Close " + browserLabel(tab.url)} disabled={props.blocked} onClick={() => props.onClose(tab.id)}>×</button>
    </div>)}
    <button type="button" className="browsernew" data-testid="browser-tab-new" aria-label="New Browser tab"
      disabled={props.blocked} onClick={props.onNew}>+</button>
  </div>;
}
