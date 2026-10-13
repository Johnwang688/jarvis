# Shared editor navigation

VEX build-error clicks now use the same FileAsk request as quick open, search and the editor palette. App still chooses a drawn editor on the project, then a new pane beside the source, then its existing guarded fallback. A VEX view is preserved; no room produces the existing notice. FileTab pins the requested file and applies its standard tab limit: only a clean tab may close, and all-dirty refuses visibly.

There is one cursor reveal, bound to an accepted open and its actual editor model. A newer open, failed read, closed or deselected target, or approval card cancels it. A request arriving under a card is consumed without replay. A repeated request with a new nonce moves the cursor even when the tab was already selected; line and column clamp to that model. No real VEX tools are involved in this change or its tests.

Before changing the product, eight component probes produced six assertion failures: repeated same-tab requests, blocked requests, card arrival during load, failed and closed targets reopened later, and rejected-capacity requests leaving a stale reveal. The corrected implementation passes those cases plus no-coordinate supersession and model-column clamping. Independent plan and source reviews, mutation checks, build, complete units and isolated browser results are recorded in the PR.
