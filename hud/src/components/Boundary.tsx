// What a render error may take down: the window's work, never its approval
// card. React unmounts the whole root when a render throws and nothing catches
// it — the sidebar, every chat and an open authorization card all went at
// once (three quick clicks on ⊞ did it, PR #29's review). This boundary wraps
// the title bar, the shell (sidebar, workspace, panel, status pane) and the
// orb; the card (`ApprovalVeil`) is rendered beside it, outside, so a throw in
// any of those leaves the card on screen, its buttons live and Escape still
// denying.
//
// The fallback is a short sentence and a Reload button, all of it plain text
// (React text nodes, never markup). The boundary logs the error's *name* only:
// a message or a stack can quote what the window was showing. (React itself
// still prints the caught error to this window's own devtools console, as it
// does for every caught error; nothing leaves the machine.)

import { Component, type ReactNode } from "react";

/** A thrown value's name, when it is a plain identifier; "Error" otherwise. */
export function errorName(e: unknown): string {
  let name = "";
  try {
    name = e instanceof Error ? String(e.name) : "";
  } catch {
    /* a getter that throws names nothing */
  }
  return /^[A-Za-z][A-Za-z0-9]{0,39}$/.test(name) ? name : "Error";
}

export class WorkspaceBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: unknown): void {
    console.warn(`HUD: the window hit a ${errorName(error)} and stopped drawing; reload to recover.`);
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div id="crashed" data-testid="hud-crashed" role="alert">
        <p>Something in this window broke and it stopped drawing.</p>
        <p>An approval card, if one is up, still works. Reload to get the window back.</p>
        <button type="button" data-testid="hud-crashed-reload" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    );
  }
}

/**
 * Test-only: throws while the page global `__hudCrashProbe` names `where`, so
 * the checks can prove a render error stays inside the boundary. Nothing in
 * the HUD or the daemon sets it; a script that could set it could already do
 * anything to the page, so it gives nothing away. Renders nothing otherwise.
 */
export function CrashProbe(props: { where: string }): null {
  if ((window as any).__hudCrashProbe === props.where) {
    throw new Error(`crash probe: ${props.where}`);
  }
  return null;
}
