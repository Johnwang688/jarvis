// xterm.js, loaded on demand (as Monaco is): a window that never opens a
// terminal never pays for it. Pinned exactly in package.json — @xterm/xterm
// 6.0.0, addon-fit 0.11.0, addon-web-links 0.12.0 — because this is the one
// front-end package that parses hostile bytes.
//
// Deliberately **not** loaded: the clipboard addon (a program could write the
// owner's clipboard with OSC 52 and set up a malicious paste), and anything
// that turns on `windowOptions` (window reports are a classic terminal-
// injection route; xterm keeps them all off by default and so does this).

import type { Terminal } from "@xterm/xterm";
import type { FitAddon } from "@xterm/addon-fit";
import type { WebLinksAddon } from "@xterm/addon-web-links";

export interface XtermKit {
  Terminal: typeof Terminal;
  FitAddon: typeof FitAddon;
  WebLinksAddon: typeof WebLinksAddon;
}

let loading: Promise<XtermKit> | null = null;

export function loadXterm(): Promise<XtermKit> {
  if (!loading) {
    loading = (async () => {
      const [xterm, fit, links] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
        import("@xterm/addon-web-links"),
        import("@xterm/xterm/css/xterm.css"),
      ]);
      return { Terminal: xterm.Terminal, FitAddon: fit.FitAddon, WebLinksAddon: links.WebLinksAddon };
    })();
    // A failed load can be tried again (the next mount), rather than caching the failure.
    loading.catch(() => {
      loading = null;
    });
  }
  return loading;
}
