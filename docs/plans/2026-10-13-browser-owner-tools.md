# Owner Remote find, menu and cursor

This package adds owner gestures to the existing ticketed Remote socket. It does
not add a public HTTP route, persisted preference, agent action or network grant.
The authentication package must be integrated first; its pending prompt blocks
these controls and ordinary page input.

Ctrl+F opens Find only from the focused Remote proxy. The context menu also
offers Find. Typing edits the local query; Enter, Shift+Enter, Next and Previous
are the explicit search gestures. Queries are limited to 256 Unicode code points
and 1024 UTF-8 bytes, with controls/invisible format characters refused. The
isolated top-document native Window.find operation wraps and returns only a
boolean match result. Hidden text and frame contents are outside this search.
Native find changes page selection/scroll and may run ordinary existing page
handlers. Closing the HUD bar preserves the page selection; there are no custom
DOM highlights or injected page-world scripts.

The fixed isolated-world operation supplies a 500ms V8 execution timeout. Its 2s
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
operations, with at most 8 open-shadow steps and default for frame targets. Only
the finite keyword vocabulary is admitted; custom CSS, url(), none and unknown
strings become default. Actual owner pointer movement coalesces to one latest
point, at most 10Hz. Cursor queries do not take over an agent. One actual CDP
operation runs per exact tab, and every request/result pins Tab, Page,
document, instance, owner socket attachment and hold identity. Lifecycle changes
invalidate receiving generations without replay. A missing reply expires locally
to default so a later actual move can try again.

Focused integrated checks passed 17 Python tests, 61 HUD tests and TypeScript.
The corrected actual owner-ticket/socket Chrome fixture passed in 4.298s:
native find, finite cursor/open shadow/frame fallback, 100-point coalescing,
same-URL reset, hold/privacy and closed-slot pruning. Sampled summed owned-Chrome
RSS was 1163.3/1168.2/1173.1 MiB, with no survivors after teardown. Its first
failure was preserved: a fixture listener observed selectionchange on window;
correcting it to document required no product change.

A subsequent independently reviewed document-input fix drops old queued input
and depressed-key/button bookkeeping on commit, and pins document/hold identity
before CDP acquisition and actual child dispatch. All 44 focused document-generation, owner-tools, host activity and pure stream
regressions passed, including the four targeted document-barrier cases.

The final strengthened native Chrome fixture passed in 4.260s, including old
queued Space across same-URL reload, cleared depressed-key bookkeeping and a
fresh actual click/type. Baseline guard cases passed, and all five deliberately
removed helper guards produced their intended assertion failures with no test
errors. TypeScript and the worktree production build passed.

The synthetic-socket real HUD acceptance passed all 22 assertions. It checks
focused-proxy Find, explicit Next/Previous, query storage privacy, the five fixed
menu actions, viewport bounds, right-button consumption, explicit clipboard,
complete outside dismissal and a fresh subsequent click, reset validation,
approval/auth priority, retained chat focus, and Find/menu bounds at 375px. No
control-listener requests or page errors occurred.

The earlier HUD failures were retained with full tracebacks. They identified
fixture setup errors: temporary HOME hid the Playwright cache; a nine-character
saved tab ID violated the six-character schema; an edge click hit the workspace
resize splitter; a supposed outside-dismiss click lay inside the menu; and a
single-Browser startup was replaced by the normal startup chat. The final fixture
uses the explicit existing browser cache with temporary HOME, valid IDs, actual
hit-target assertions and owner-focused resizing of the already-booted workspace.
No production changes were needed for those corrections.

The combined action regression exposed one production integration issue: using
an approval hold to discard document input made an expected agent navigation
stop at its own commit. Document retirement now has a dedicated atomic queue
cutoff; it leaves the approval counter unchanged. Queued and already-taken old
input stays undeliverable, new owner intent works, and the existing document
checks still run before CDP acquisition and actual send. Actual approval holds
still stop the unchanged driver guard.

The reviewed correction passed 70 focused pure checks, the unchanged 19-case
real Chrome action suite (including navigation and the existing full-upload
resource exception), the owner-tools Chrome case, and all 22 strengthened HUD
checks. The HUD poll now pumps routed Playwright callbacks, and reset focus is
asserted against the actual active element. Both additional mutation baselines
passed: restoring the old document approval hold failed the positive native
navigation assertion; omitting document discard failed the old-input cutoff
assertion. These were intended assertion failures with no test errors.

The coordinator's combined correction run at
`ac8bdedec0c824971d763f26a2f879f2055cbbfb` then passed all 14 selected suites,
including toolset audit, complete profiles, the 19-case real action suite,
document/driver/attention boundaries, native auth matrix and both focused HUD
fixtures. Aggregate exit was 0 in 115.34s; source manifests stayed unchanged and
the exact owned scope finished inactive with no remaining processes. Native
lifecycle capabilities and final release review remain separately scoped.

These receipts are not installation, deployment or complete-product validation.
