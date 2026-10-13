# Input fit follow-ups

Checkpoint input-bar followups: model error/status/header fixed-height changes do not always refit. The current effect already depends on each pane's primitive text and file count; do not claim it measures every pane on a keystroke without reproduction. Add geometry probes proving only changed text's pane is fitted and batch requested panes' layout operations.

Extract fit work into a small shared requestAnimationFrame queue keyed by textarea. One flush groups computed-style reads, one-line writes, fixed-space reads, auto-height writes, scrollHeight reads and final writes. This keeps the existing geometry equations/minimums and required one-line fixed-space measurement while reducing interleaved document flushes when several panes actually resize. Requests deduplicate; disposal removes the element and cancels the outstanding frame when empty. Hidden panes skip and refit when drawn.

Observe textarea width, pane height and the log's height. The observer compares width/height plus fixed space (paneHeight - logHeight - textareaHeight), which remains constant when only the fitted textarea and flex log resize inversely. Thus model-error rows, empty-to-text status, headers/close prompts and thinking lines trigger refit, while the fit's own writes do not loop. Retain explicit local text/file/image/status/footLine dependencies (use actual status string). Disconnect/cancel on unmount.

Browser regressions: long40-line draft, panel closed→open; min-log in rows2+panel1280x800 and staged files; model-error/status/header growth with no keystroke; 8panes only edited pane fit; narrow/zoom; no observer loop. Existing mention popover rAF cleanup gets a lifecycle regression/mutation, no behavior redesign. Full HUD+units and independent review before merge.

Independent review accepted with post-fit observer baselines, 1px rounding
tolerance, and connection/visibility checks at flush time. Disposal removes
only the specific textarea and cancels an empty queue's outstanding frame.
