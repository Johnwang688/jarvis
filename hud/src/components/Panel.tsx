// The bottom panel (plan §2.3): the terminal's home, as VS Code's panel is.
// It spans the workspace's columns, between the side panes, below the panes;
// ⬓ in the title bar and Ctrl+` show and hide it, and its top edge is a
// horizontal separator (Workspace.tsx). A hidden panel takes no room and has
// no rail, and is hidden, never unmounted.
//
// WP-A ships the dock and its geometry; the terminals that live in it (a tab
// per terminal, `+ ▾` for the folder, × per tab) arrive with WP-D, which
// replaces the placeholder body below and nothing else.

export function Panel(props: {
  open: boolean;
  height: number;
  blocked: boolean;
  onHide: () => void;
}) {
  return (
    <div
      className="panel"
      data-testid="panel"
      data-open={props.open ? "true" : "false"}
      style={props.open ? { flexBasis: props.height, height: props.height } : { display: "none" }}
    >
      <div className="panelhead">
        <span className="paneltitle">Terminal</span>
        <button
          type="button"
          className="collapse"
          data-testid="panel-hide"
          title="Hide the panel (Ctrl+`)"
          aria-label="Hide the panel"
          disabled={props.blocked}
          onClick={props.onHide}
        >
          ×
        </button>
      </div>
      <div className="panelbody pad muted" data-testid="panel-empty">
        No terminals yet. The integrated terminal is on its way; this panel is where it will open.
      </div>
    </div>
  );
}
