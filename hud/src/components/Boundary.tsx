// What a render error may take down: the window's work, never its approval
// card. React unmounts the whole root when a render throws and nothing catches
// it — the sidebar, every chat and an open authorization card all went at
// once (three quick clicks on ⊞ did it, PR #29's review). Two boundaries:
//
//   - `WorkspaceBoundary` wraps the title bar, the shell (sidebar, workspace,
//     panel, status pane) and the orb, and draws a Reload prompt in their
//     place. The card (`ApprovalVeil`) is rendered beside it, outside, so a
//     throw in any of those leaves the card on screen, its buttons live and
//     Escape still denying. Its `onCrash` lets App stop what would otherwise
//     carry on unseen: a paste going out to a shell, and the microphone.
//   - `DialogBoundary` wraps the pickers and dialogs: one that throws is
//     closed (its `onCrash`), and the window carries on.
//
// A throw anywhere else — App's own render, the store's reducer, the veil
// itself — still unmounts the whole window. Fallback text is plain text
// (React text nodes, never markup). The boundaries log the error's *name*
// only: a message or a stack can quote what the window was showing. (React
// itself still prints a caught error to this window's own devtools console,
// as it does for every caught error; nothing leaves the machine.)

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

/**
 * Space and Enter on Reload are Reload's: never push-to-talk (App listens on
 * the document). Only those two: Escape must still reach a card's handler.
 */
const ownKey = (e: React.KeyboardEvent) => {
  if (e.code === "Space" || e.key === " " || e.key === "Enter") e.stopPropagation();
};

export class WorkspaceBoundary extends Component<{ children?: ReactNode; onCrash?: () => void }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: unknown): void {
    console.warn(`HUD: the window hit a ${errorName(error)} and stopped drawing; reload to recover.`);
    this.props.onCrash?.();
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    // While a card is up the veil makes this inert too (Approvals.tsx), as it
    // does everything else beside it.
    return (
      <div id="crashed" data-testid="hud-crashed" role="alert">
        <p>Something in this window broke and it stopped drawing.</p>
        <p>An approval card, if one is up, still works. Reload to get the window back.</p>
        <p>Reloading loses anything not yet sent: words typed in a box and files staged for it.</p>
        <button
          type="button"
          data-testid="hud-crashed-reload"
          onKeyDown={ownKey}
          onKeyUp={ownKey}
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </div>
    );
  }
}

/**
 * Around the pickers and dialogs: one that throws draws nothing and is closed
 * (`onCrash`). It draws its children again once `resetKey` (which dialogs are
 * open) moves on, so the next picker opened is drawn — without remounting a
 * dialog that stayed open.
 */
export class DialogBoundary extends Component<
  { children?: ReactNode; resetKey: string; onCrash: () => void },
  { failed: boolean; resetKey: string }
> {
  state = { failed: false, resetKey: this.props.resetKey };

  static getDerivedStateFromProps(
    props: { resetKey: string },
    state: { failed: boolean; resetKey: string },
  ): Partial<{ failed: boolean; resetKey: string }> | null {
    return props.resetKey === state.resetKey ? null : { failed: false, resetKey: props.resetKey };
  }

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: unknown): void {
    console.warn(`HUD: a dialog hit a ${errorName(error)} and was closed.`);
    this.props.onCrash();
  }

  render(): ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}

/**
 * Test-only: throws while the page global `__hudCrashProbe` names `where`, so
 * the checks can prove a render error stays inside its boundary. Nothing in
 * the HUD or the daemon sets it; a script that could set it could already do
 * anything to the page, so it gives nothing away. Renders nothing otherwise.
 */
export function CrashProbe(props: { where: "workspace" | "dialog" }): null {
  if ((window as any).__hudCrashProbe === props.where) {
    throw new Error(`crash probe: ${props.where}`);
  }
  return null;
}
