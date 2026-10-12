// The unsaved-changes dot in a File pane's tree (rules in lib/dirtyMarks.ts),
// and the hook a tree reads it from.
//
// The hook subscribes to the buffer registry but **redraws only when the
// dirty set this project shows changes**: its snapshot is a string key, and
// the registry speaks only when a buffer goes clean↔dirty (or opens, saves,
// lets go) — never per keystroke. The key is worked out once per registry
// version, however often React asks.

import { memo, useMemo, useSyncExternalStore } from "react";
import { buffers } from "../state/buffers";
import { dirtyKey, dirtyPathsIn, marksOfKey, type Marks } from "../lib/dirtyMarks";

/** The dirty marks project `projectId`'s tree shows; `root` finds a file read through another project. */
export function useDirtyMarks(projectId: string | null, root?: string | null): Marks {
  const getKey = useMemo(() => {
    let seen = -1;
    let key = "";
    return () => {
      const ver = buffers.version();
      if (ver !== seen) {
        seen = ver;
        key = projectId ? dirtyKey(dirtyPathsIn(buffers.snapshot(), projectId, root)) : "";
      }
      return key;
    };
  }, [projectId, root]);
  const key = useSyncExternalStore(buffers.subscribe, getKey);
  return useMemo(() => marksOfKey(key), [key]);
}

/** The dot itself: a light filled circle with a name and a tooltip, never amber or red. */
export const DirtyDot = memo(function DirtyDot(props: { folder?: boolean; testid?: string }) {
  return (
    <span
      className="dirtydot"
      role="img"
      aria-label="unsaved changes"
      title={props.folder ? "Unsaved changes inside" : "Unsaved changes"}
      data-testid={props.testid}
    />
  );
});
