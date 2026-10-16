# Owner pickers for native select, date and colour controls (B-PICKERS, BR-7/M9)

The feature map row B-PICKERS asked for the exact-element fallback design to be
written and reviewed before it was built. This is that design, the
measurements it rests on, and what was built.

## The problem, measured (Chrome 155.0.8059.39, headless, the owner host's launch)

A `<select>`, `<input type=date|time|datetime-local|month|week|color>` opens a
popup that the screencast never draws. Measured through the real owner socket
(`browser_routes_check`'s harness):

- **The popup really opens and takes the keyboard.** After an owner click on a
  select, `select.matches(':open')` is true. ArrowDown then reaches no page
  listener and leaves the value unchanged; Enter commits it (trusted `input`,
  `change`). The owner sees none of this.
- **`:open` is the signal.** It is true while the popup of a select, a date,
  time, datetime-local, month or week input, or a colour input is up, in the top
  document and in a same-process iframe, and readable from an isolated world.
  A date-like input opens from its calendar indicator (after the pointer
  hovered it, as a real pointer does) or from Space, Alt+Down or F4; a click
  on its text focuses a segment and opens nothing.
- **CDP says nothing.** No `Page.*` event fires when a popup opens, and
  `Target.setDiscoverTargets` reports no new target. The `Input` domain
  emits no events of its own.
- **One trusted Escape key-down closes it, silently.** A `rawKeyDown` Escape
  with no key-up closes the select, colour and iframe-select popups; the page
  sees no event at all, focus stays on the control and its value is unchanged.
- **`DOM.getNodeForLocation` takes document coordinates** (the viewport point
  plus the layout viewport's scroll, from `Page.getLayoutMetrics`): at a
  scroll of 800 the viewport point (100, 115) found nothing and (100, 915)
  found the select there. It descends into same-process iframes and reports
  the node's own `frameId`. `DOM.getBoxModel` answers in viewport CSS px,
  iframe offsets included.
- **A cross-site iframe is another process.** The hit test stops at the
  `<iframe>` element in the parent frame; the select inside still opens its
  invisible popup.
- **`<datalist>` shows nothing at all.** Typing a matching prefix leaves `:open`
  false, and ArrowDown then Enter leaves the typed text: no suggestion popup
  exists to capture keys. See *Datalist* below.

## Design

### 1. Detection: the owner's own click, on that element

Only the owner's ticketed socket reaches `BrowserHost._deliver`; driving sends
its own CDP input and never passes there. In `_deliver`, for an owner
**left press** that is about to be sent:

1. **Pin before the press goes out**: `Page.getLayoutMetrics` and
   `DOM.getNodeForLocation` at the press point (document coordinates, user
   agent shadow excluded, `pointer-events: none` honoured as a real click
   does) give `(frameId, backendNodeId)`. No page script runs. The pin also
   records the document generation, the socket attachment and the hold count.
2. **Probe after the matching release is answered**, never after a let-go
   (a release a card cut off): resolve that exact `backendNodeId` in a
   `jarvis-pickers` isolated world of the pinned frame and ask a fixed
   function whether it is still connected, belongs to that frame's current
   document (`ownerDocument === document` in that frame's world) and
   `matches(':open')`, and whether it is a picker kind (a single-line select
   whose `appearance` is not `base-select`, whose picker Chrome draws in the
   page, or one of the six input types).

Anything else (a page that opens a picker on another element in its own click
handler, `showPicker()` from a timer, an agent's click) is no overlay: the
pinned element must be the one that opened.

### 2. Closing the native popup and reading the control

If the pinned element is open, the daemon re-checks the hold and the binding and
sends one trusted `rawKeyDown` Escape, then confirms `:open` is false (else no
overlay). The same isolated world reads the control: for a select its options
(DOM index, `label`, `disabled` including a disabled optgroup, the optgroup
label; `hidden` options are left out of the list but keep their index), the
selected index and the count; for a date-like input its value, `min`, `max`,
`step` and `required`; for colour its value. `DOM.getBoxModel` gives the
element's rectangle. Never the page's world; nothing is written to the DOM.

**Page text is untrusted.** Labels are cleaned (controls, invisible and
format characters out, one line) and capped at 80 code points in the daemon
and again in the HUD, at most 500 options and 60,000 bytes of JSON (past it
the list says how many were not shown), rendered as React text. Option values
stay in the daemon's memory (they prove identity at apply). Nothing reaches the
bus, a log line or browser storage.

The window hears `{t:"picker", id, kind, rect, ...}` on its own attachment only.

### 3. The HUD overlay

A Graphite overlay anchored below the element's rectangle (flipped above, or
clamped, inside the visible Remote surface): a listbox with optgroup headers
for a select, a native HUD `<input>` of the same type with Set, Clear (when
not required) and Cancel for date-like controls (the owner's own Windows
Chrome draws that picker), and a colour input with Set and Cancel.

- **It never takes the keyboard.** Nothing in it calls `focus()`. Keys typed in
  the Remote proxy (where the owner's click already put the keyboard) are
  routed to the open overlay instead of the page: arrows, Home/End,
  Page Up/Down and type-ahead move, Enter applies, Escape cancels; the HUD's own
  shortcuts still work; everything else is swallowed, as the native popup
  would. The native HUD inputs take focus only from the owner's own click.
- **It needs the owner's gesture.** A `picker` message is accepted only within
  5 s after this window's own left press in this stream generation, once.
- **Never under a card, never hidden.** It is drawn only while the stream is
  connected and live and nothing blocks input: no card (`blocked`, the global
  hold), no auth prompt, dialog, file chooser or annotation. Any of those, a
  disconnect, an owner reset, a hidden pane or a new generation closes it.
- A press or wheel on the page outside it cancels it and is consumed whole.

### 4. Applying the choice to that exact element

The owner's `{t:"pick", id, index | value}` is queued as **owner input**, so it
keeps its order with the owner's other input, is dropped under a card (never
replayed after it), stamps owner input and pauses a driving agent. On the
Browser's thread the daemon:

1. takes the picker (one apply per picker) and re-checks the binding: the tab,
   page, page identity, main document generation, Chrome instance, socket
   attachment, hold count and approval epoch are those it was opened under;
   no dialog, chooser or auth prompt; the stream visible;
2. validates the value against the kind's grammar (`YYYY-MM-DD`, `HH:MM[:SS[.mmm]]`,
   `YYYY-MM-DDTHH:MM…`, `YYYY-MM`, `YYYY-Www`, `#rrggbb`; empty only for a
   date-like control that is not required);
3. resolves **the same `backendNodeId` in the same frame's isolated world** and
   requires it to be connected, in that frame's current document, the same tag
   and type, enabled (and not read-only); for a select the option count is
   unchanged and the chosen option has the same value and label and is
   enabled; a date-like value must pass a detached same-type input's
   `min`/`max`/`step` validity;
4. assigns (`selectedIndex` or `value`, the native setter), then dispatches
   `input` (bubbling, composed) and then `change` (bubbling) as **three separately
   awaited calls**, re-checking the binding and that the element is still
   connected and still holds the value before each event. A handler that
   replaces the control or changes its value stops the later event. The same
   value as before dispatches nothing, as a real popup does.

**Trusted where possible.** The click that opened the popup and the Escape
that closed it are trusted CDP input. The assignment and the two events are
not (`isTrusted: false`), as BR-9b's agent `select` already is: Chrome 155
offers no trusted path that sets an exact option or date. In an open select
popup ArrowDown does not change the value until Enter, so a key count cannot be
checked before the trusted `change` fires (an off-by-one would commit the wrong
option); on a closed select each arrow fires its own `change`; date segments
are ordered by locale. A page that rejects untrusted events is a stated limit.

### 5. Cancelling

Each of these drops the picker and tells the window `{t:"picker_closed", id,
reason}`: a main-frame navigation (any `framenavigated`, same-document
included), the tab closing, a re-attach or detach, a resize, the stream hidden,
a card, a dialog or file chooser, a new picker, and the owner's
`{t:"pick_cancel"}` (no reply). An apply that finds anything changed sets
nothing and answers `changed` (or `refused` for a value the control would not
take); the HUD says nothing was set.

## Datalist

Assessed, not built. Chrome 155 headless draws no datalist suggestions anywhere
(no `:open`; ArrowDown and Enter leave the typed text), so unlike the pickers
nothing invisible takes the keyboard: the owner simply gets no suggestions.
Offering them is not a small extension of this overlay: suggestions follow
every keystroke while the keyboard stays with the page, so the overlay would
have to be non-modal, refiltered from the field's live value on each owner key,
and must never swallow the typing that drives it, which is the opposite of the
modal picker here. It needs its own design.

## Stated limits

- **Cross-site (out-of-process) iframes** are not covered: the hit test stops at
  the `<iframe>`, no overlay is offered, and the invisible popup behaves as
  before (Escape closes it).
- **Keyboard-opened pickers** (Space, Alt+Down, F4 on a focused control) are not
  covered; a click opens the overlay.
- The assignment and the two events are untrusted (above).
- Between the `:open` check and the closing Escape (one CDP round trip) a page
  could close the popup itself; the Escape key-down would then reach the page.
- The rectangle is read once: a page that scrolls itself leaves the overlay
  where the control was. A wheel or a resize closes it.
- Each owner left press costs two CDP calls before it is sent (the pin, p50
  about 3 ms, p95 about 4 ms), and its release one script-free
  `DOM.describeNode` (p50 1.2 ms) unless the element is a select or a picker
  input.

## Built (2026-10-16, branch `claude/b-pickers`, [#232](https://github.com/Johnwang688/jarvis/pull/232), merged 2026-10-16)

`jarvis/v2/browser/pickers.py` and its hooks in `host.py` (`_deliver` and the
lifecycle invalidations) and `stream.py` (`pick`, `pick_cancel`);
`hud/src/lib/remotepicker.ts`, `components/RemotePicker.tsx` and RemoteView.
One measured addition to the design above: a date-like popup still loading its
own page ignores the first Escape, so the closing Escape is retried while
`:open` stays true (12 times, 25 ms apart). Evidence and mutants are in
`docs/notes/browser.md` ("Owner pickers for native select, date and colour
controls").

## Review of PR #232 (2026-10-16)

CHANGES NEEDED, fixed on the branch:

- **MED 1:** a select whose `mousedown` opened a file chooser was offered a
  picker and applied once the chooser closed (reproduced in real Chrome 155).
  The probe and the final check under the lock now require no dialog, file
  chooser or auth prompt and a window showing the tab. The HUD refuses a
  picker unless it could draw one, with dialogs and the chooser tracked as the
  socket reports them.
- **LOW 2:** a group's label is now charged against the 60,000-byte cap when it
  is first used, only used groups are sent, and a message over the cap is
  never offered. The message is built before the closing Escape, so a failed
  or invalid read leaves the native popup alone.
- **LOW 3 and LOW 4:** new tests cover a card raised during the pin and a
  second `pick` with the same id.
- **LOW 5:** the frame's `loaderId` is recorded at the probe and compared at
  apply.
- **NITs:** the object-group release is bounded at 1 s, a let-go clears the
  pin, and an IME commit is guarded while the picker is open. NIT 7 (one
  isolated world per probe) was skipped, as asked.

Mutant results are in `docs/notes/browser.md`.

