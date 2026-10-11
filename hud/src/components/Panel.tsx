// The bottom panel (plan §2.3): the terminal's home, as VS Code's panel is.
// It spans the workspace's columns, between the side panes, below the panes;
// ⬓ in the title bar and Ctrl+` show and hide it (Ctrl+` also opens a
// terminal when there is none), and its top edge is a horizontal separator
// (Workspace.tsx). A hidden panel takes no room and has no rail, and is
// hidden, never unmounted.
//
// It holds only terminals (WP-D): a tab per terminal, `+` (the focused
// pane's folder) and `▾` (Home or a project), and × per tab. A terminal a
// pane shows is drawn there, not here — its tab says "in pane N" and jumps
// to it (one terminal in one place at a time, W-1). components/Terminal.tsx
// draws the tabs and the terminal.

import type { Project } from "../types";
import { TerminalPanel } from "./Terminal";

export function Panel(props: {
  open: boolean;
  height: number;
  blocked: boolean;
  zoom: number;
  projects: Project[];
  onHide: () => void;
}) {
  return (
    <div
      className="panel"
      data-testid="panel"
      data-open={props.open ? "true" : "false"}
      style={props.open ? { flexBasis: props.height, height: props.height } : { display: "none" }}
    >
      <TerminalPanel open={props.open} blocked={props.blocked} projects={props.projects} zoom={props.zoom}
                     onHide={props.onHide} />
    </div>
  );
}
