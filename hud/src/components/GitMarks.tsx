// The explorer's git colours (ED-9; rules in lib/gitMarks.ts): the hook a
// File pane's tree reads them from.
//
// It watches the project's status (state/gitStatus.ts — which polls while
// anyone watches) but **redraws only when the states change**: its snapshot
// is a string key, worked out once per status version, so a poll that saw
// nothing new — and every keystroke — leaves the tree's memoised rows alone.

import { useCallback, useMemo, useSyncExternalStore } from "react";
import { gitStatus, type GitStatusStore } from "../state/gitStatus";
import { gitKey, gitMarksOfKey, NO_GIT, type GitMarks } from "../lib/gitMarks";

export function useGitMarks(projectId: string | null, store: GitStatusStore = gitStatus): GitMarks {
  const subscribe = useCallback(
    (fn: () => void) => (projectId ? store.subscribe(projectId, fn) : () => {}),
    [projectId, store],
  );
  const getKey = useMemo(() => {
    let seen = -1;
    let key = "";
    return () => {
      const ver = projectId ? store.version(projectId) : 0;
      if (ver !== seen) {
        seen = ver;
        key = projectId ? gitKey(store.get(projectId)) : "";
      }
      return key;
    };
  }, [projectId, store]);
  const key = useSyncExternalStore(subscribe, getKey);
  return useMemo(() => (key ? gitMarksOfKey(key) : NO_GIT), [key]);
}
