// One Browser iframe, created and owned imperatively (amendment 2 §E.8).
//
// React never renders the iframe: a `useLayoutEffect` makes the element, the
// way the terminal panel moves xterm's element, so the frame guard
// (state/frameguard.ts) can remove it from the DOM the instant it takes the
// keyboard without React's reconciler later tripping over a missing node
// (`removeChild` on a node that is gone). The host div React renders has no
// React children.
//
// A new element is made whenever the URL, the sandbox or the load number
// changes: the sandbox's flags apply when a frame navigates, so a change of
// them reloads the page under the new ones (as the old `key` did), and a load
// the owner asks for is always a fresh frame the guard can tie their gesture
// to. Every frame carries `data-browser-frame="<pane>:<tab>"`; until Direct
// tabs land (AO-2's second half) the tab is 0.
//
// Over the frame lies a transparent shield: how the guard learns the owner's
// pointer is over a page whose events the HUD never sees (state/frameguard.ts).

import { useLayoutEffect, useRef } from "react";
import { frameKey } from "../lib/frameguard";
import { registerFrame } from "../state/frameguard";

export function FrameHost(props: {
  pane: number;
  /** The tab index in the pane (0 until Direct tabs). */
  tab?: number;
  /** The pane's slot, for the console line a theft writes. */
  slot: number;
  /** The pane is drawn by this render. */
  drawn: boolean;
  /** This is the pane's active tab (always, until Direct tabs). */
  active?: boolean;
  src: string;
  sandbox: string;
  keepOrigin: boolean;
  /** Bumped by every load the pane asks for: a fresh frame each time. */
  load: number;
  /** The guard removed this frame: it took the keyboard. */
  onStolen: () => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const latest = useRef(props);
  latest.current = props;
  const tab = props.tab ?? 0;

  useLayoutEffect(() => {
    const el = host.current;
    if (!el) return;
    const frame = document.createElement("iframe");
    frame.className = "browserframe";
    frame.title = "Browser";
    frame.setAttribute("data-testid", "browser-frame");
    frame.setAttribute("data-keep-origin", props.keepOrigin ? "true" : "false");
    frame.setAttribute("data-browser-frame", frameKey(props.pane, tab));
    // The sandbox before the URL, both before the element is connected: the
    // first navigation must already run under the flags.
    frame.setAttribute("sandbox", props.sandbox);
    frame.setAttribute("src", props.src);
    const shield = document.createElement("div");
    shield.className = "frameshield";
    shield.setAttribute("data-testid", "browser-shield");
    shield.setAttribute("aria-hidden", "true");
    const off = registerFrame({
      frame,
      shield,
      host: el,
      key: frameKey(props.pane, tab),
      pane: props.pane,
      slot: () => latest.current.slot,
      drawn: () => latest.current.drawn && (latest.current.active ?? true),
      onStolen: () => latest.current.onStolen(),
    });
    el.append(frame, shield);
    return () => {
      off();
      frame.remove();
      shield.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.src, props.sandbox, props.load, props.pane, tab]);

  return <div ref={host} className="framehost" data-testid="browser-framehost" />;
}
