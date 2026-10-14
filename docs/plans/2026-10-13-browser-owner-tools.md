# Owner Remote find, menu and cursor

This package adds owner gestures to the existing ticketed Remote socket. It does
not add a public HTTP route, persisted preference, agent action or network grant.
The authentication package must be integrated first; its pending prompt blocks
these controls and ordinary page input.

Ctrl+F opens Find only from the focused Remote proxy. The context menu also
offers Find. Typing edits the local query; Enter, Shift+Enter, Next and Previous
are the explicit search gestures. Queries are limited to256 Unicode code points
and1024 UTF-8 bytes, with controls/invisible format characters refused. The
isolated top-document native Window.find operation wraps and returns only a
boolean match result. Hidden text and frame contents are outside this search.
Native find changes page selection/scroll and may run ordinary existing page
handlers. Closing the HUD bar preserves the page selection; there are no custom
DOM highlights or injected page-world scripts.

The fixed isolated-world operation supplies a500ms V8 execution timeout. Its2s
host response deadline is a wait bound, not a promise that native layout/style
work is preemptible. Actual ActivityWork and the per-tab serialization worker
remain reserved until the child coroutine settles, including after a canceled
waiter or socket replacement. The HUD uses a bounded local deadline as well.

Owner right-click is consumed before any right-button press/release reaches
Chrome. The local menu offers only Back, Forward, Reload, Find and Copy
selection. It uses existing owner history and clipboard paths. Its placement is
clamped to the visible surface, and an outside dismissal consumes that entire
gesture. Higher approval Escape retains priority. Authentication, dialogs,
chooser/files, options, annotation, hidden/disconnected/replaced tabs and holds
disable or withdraw these controls. Page metadata cannot open them or focus a
HUD input.

Cursor lookup uses fixed isolated-world elementFromPoint/getComputedStyle
operations, with at most8 open-shadow steps and default for frame targets. Only
the finite keyword vocabulary is admitted; custom CSS, url(), none and unknown
strings become default. Actual owner pointer movement coalesces to one latest
point, at most10Hz. Cursor queries do not take over an agent. One actual CDP
operation runs per exact tab, and every request/result pins Tab, Page,
document, instance, owner socket attachment and hold identity. Lifecycle changes
invalidate receiving generations without replay. A missing reply expires locally
to default so a later actual move can try again.

Focused integrated checks passed17 Python tests,61 HUD tests and TypeScript.
The corrected actual owner-ticket/socket Chrome fixture passed in4.298s:
native find, finite cursor/open shadow/frame fallback,100-point coalescing,
same-URL reset, hold/privacy and closed-slot pruning. Sampled summed owned-Chrome
RSS was1163.3/1168.2/1173.1 MiB, with no survivors after teardown. Its first
failure was preserved: a fixture listener observed selectionchange on window;
correcting it to document required no product change.

A subsequent independently reviewed document-input fix drops old queued input
and depressed-key/button bookkeeping on commit, and pins document/hold identity
before CDP acquisition and actual child dispatch. Four targeted synthetic
regressions are prepared; execution is pending. The real HUD fixture includes
375px layout and auth/find/approval priority; it has not run at this writing.
These receipts are not installation, deployment or complete-product validation.
