# WP12b — HUD v2 frontend

## Built
`hud/` — Vite 6 + React 18 + TypeScript, built to `hud/dist` (gitignored; the
daemon serves it from `/` on FACE_PORT). Runtime deps are exactly the four the
brief names: react, react-dom, markdown-it + dompurify, monaco-editor. The v1
skin over the §12.2 three-pane layout; `<title>` stays `J.A.R.V.I.S.`; nothing
overflows horizontally at 1280 px.

Everything the brief lists ships: sidebar (Windows badge from `/platform`,
threads, tasks expanding to their threads with role and provider), Chat
(rendered replies, tool ticker, live draft, `proposal_reply` + 60 s Cancel),
input bar (typed + attachments + AUTO/REVIEW/OFF), Task tab and right pane
(spec, per-question answer box, plan, status record, routing line, steer,
cancel/resume, §10.4 report), File (tree, Monaco, mtime guard, protected =
withheld, markdown split + rendered-only), Diff (list + Monaco diff,
truncation stated), Preview (sandboxed iframe refusing the HUD origin),
Approvals (card + queue), Usage, Schedules, Route, and the three pickers.

## Component map (ported vs rewritten)
| from `jarvis.html` | now | port or rewrite |
|---|---|---|
| orb canvas, `RING_SETS`, state palette, avatar `<img>` | `components/Orb.tsx` | **ported** — ring data and draw loop line for line |
| `WAKE_PATTERNS`, `matchesWake`, recognizer de-dup | `lib/wake.ts` | **ported** (`WakeGate` is the segment+floor rule as a class) |
| ring buffer, `VAD`, pre-roll, hangover, `wavBlob` | `lib/vad.ts` | **ported** — arithmetic unchanged |
| claim rules, follow-up window, mute clamp, PTT | `lib/capture.ts` | **ported**, with OFF standing in for `micMuted` |
| approval card + chime | `components/Approvals.tsx` | **ported** semantics, rewritten as JSX (text, never `innerHTML`) |
| `renderMarkdown` | `lib/markdown.ts` | **rewritten** on markdown-it + DOMPurify; the *rules* are v1's (no raw HTML, scheme-checked links, `_blank`, owner text verbatim) |
| mute row, session picker | dictation OFF; sidebar threads | **rewritten** — one control (§12.1); threads, not sessions |
| model / voice / avatar pickers | `components/Pickers.tsx` | **rewritten**, same rules (fast path only; `""` clears the voice override) |
| COMMS log, OPERATIONS, SYSTEMS | `ChatTab` / right pane | **rewritten** for the new IA |
| tabs, files, diff, preview, usage, schedules, route | — | new |

## Tests
```
cd hud && npm ci && npm run build && npm test
   Test Files  7 passed (7) · Tests  68 passed (68)
   ✓ built in 3.63s   (dist/index.html 0.62 kB, index 343.83 kB, editor.api 2,295.65 kB)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/face/hud_v2_check.py
   all 134 HUD v2 checks passed
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/face/hud_v2_live_check.py
   SKIPPED — nothing is serving http://127.0.0.1:8402. Start `jarvis daemon2` first.
```
`tests/face/hud_v2_mock.py` is the contract mock (scripted world + SSE on a
`queue.Queue`). **Verified to bite:** three mutations were built and re-run —
accepting the HUD origin (2 Playwright + 1 vitest failure), giving the card an
Enter default (3 failures), and not unwrapping a refused anchor. The third
caught nothing at first: markdown-it refuses `javascript:`/`data:` itself, so
my case never reached my own scheme check. It uses `ftp:`/`tel:` now, which
markdown-it allows, and fails against the mutation. `tests/desktop_check.py`
re-run green (64 checks) — no v1 file was touched.

## Workarounds worth knowing
- **A Playwright `evaluate` whose last expression is a function gets *called*.**
  A script ending `cap.onWake = () => {…}` installs the hook and then runs it
  once, which read as the wake word firing twice. Every instrumenting evaluate
  in the suite now ends in a plain value; the note is in the test.
- **Monaco's text layer intercepts clicks meant for its hidden textarea**, so
  the File tab test types via `.view-lines` + `Control+End`. Both editors fall
  back to a `<textarea>`/`<pre>` if Monaco fails to load: the save path has to
  stay reachable either way.
- Two real bugs the suite found, both fixed: the recognizer handle
  (`window.__hudRecog`) survived OFF, so the window claimed to be streaming to
  Google's speech service when it was not; and `SchedulesPanel` seeded its
  project `useState` from a list that had not loaded, so every create silently
  did nothing (the default is read per render now, the owner's pick an
  override).

## Proposed contract changes (none blocking)
1. `GET /threads` with no `?project` draws the whole sidebar in one call; WP7's
   table shows only `?project=…`. Confirm the unfiltered form, or the HUD fans
   out per project.
2. `approval_requested` needs `timeout_s` for the card's countdown (the HUD
   defaults to 120 s) and `allowlistable`, so ALWAYS is hidden on an escape-
   hatch request (§6.1). `ApprovalRequest.to_json` omits both today.
3. `POST /tasks/{id}/answer` is sent as `{question, answer}` — the text
   identifies the `OpenQuestion`. Confirm, or give the questions ids.
4. `proposal_reply` carries `{turn_id, reply}` (WP11); the HUD also needs
   `task_id` on it to offer the 60-second Cancel the brief specifies.
5. `GET /route`'s `table` is read as `{chains: {role: [provider…]}}`, matching
   `router.view()`. Nothing else is assumed about it.