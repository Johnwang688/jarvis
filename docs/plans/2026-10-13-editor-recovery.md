# Editor recovery follow-ups

The crash fallback offers a named Copy unsaved text action for each retained file. It snapshots exact text only on the owner click, waits for clipboard completion, reports failure truthfully, and leaves the edit dirty. Save conflicts do not hide it. Save and copy are disabled under an approval card; nothing is sent, logged or persisted by copy.

Shared buffers keep their model, etag and undo stack when their first project is permanently deleted. Successful reads establish project/path aliases and tab holders; only a surviving live holder can become the IO route. With no holder, the edit stays and IO refuses locally until reopened from a surviving project. Archive keeps its existing file route. Known alternate paths in the same project all receive dirty indicators and collapsed-folder dots.

Deletion tombstones prevent delayed opens from restoring deleted aliases. A separate route generation rejects stale reload, Compare and stat results. An already successful write still updates the shared saved version and etag; its queued follow-up resolves the current route when it runs. Failed writes are not silently retried. Rename transfers verified aliases and holder metadata only where the original root remains provable; other spellings need a new daemon read.

Validation is recorded in the PR: controlled before-fix failures, focused lifecycle/clipboard units, real Chromium crash409 recovery and approval checks, mutation checks, independent review, and the complete HUD sweep. Tests use synthetic content and temporary fixtures.
