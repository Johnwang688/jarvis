import { useState } from "react";
import { browserFrames, EMPTY_FRAMES } from "../lib/browserframes";
import type { PaneNo, Workspace } from "../lib/workspace";
/** Adjust own derived state during render, before children mount any new frames. */
export function useBrowserFrames(ws: Workspace, drawn: readonly PaneNo[], covered = false): readonly string[] {
  const [previous, setPrevious] = useState(EMPTY_FRAMES);
  const next = browserFrames(previous, ws, covered ? [] : drawn);
  if (next !== previous) setPrevious(next);
  return next.recent;
}
